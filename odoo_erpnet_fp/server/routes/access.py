"""
Access-control API (Phase B) — barrier / relay / turnstile.

SYNCHRONOUS request→response, zero queue latency — Odoo POSTs the
already-authorised decision and gets a definitive result, the same
channel pattern a barcode read uses (NOT the 60 s Fleet
command-queue; that exists only as a secondary remote-management
path in registry._execute_command kind=access_open).

The access DECISION is taken in Odoo (Channel-1 ⊕ Channel-2,
fail-secure). This endpoint only EXECUTES it. No call → barrier stays
shut.

Endpoints:
  GET  /access                  — list controllers
  GET  /access/{id}             — controller info
  POST /access/{id}/open        — grant (body: {"seconds": 3} → pulse)
  POST /access/{id}/deny        — explicit close / deny
  GET  /access/{id}/status      — best-effort state
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/access", tags=["access"])


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class AccessInfoResp(_CamelModel):
    id: str
    driver: str
    fail_secure: bool = Field(True, alias="failSecure")


class OpenReq(BaseModel):
    # Празно/0 → latched open; иначе momentary pulse за толкова секунди.
    seconds: Optional[float] = None


class CardReq(BaseModel):
    """Card-management request — write/remove a card into the
    controller's LOCAL memory (offline / standalone validation).
    Odoo computes the semantic rights + TS slot; the driver owns the
    wire frame. `op`: 'add' (default) or 'remove'."""
    card_number: str
    op: str = "add"
    rights_data: int = 1   # per-reader bitmask (reader N → 1<<(N-1))
    rights_mask: int = 1   # which bits we set/clear
    ts_code: str = "01000000"  # 4-byte hex: TS slot per reader
    pin_code: str = "0000"


class TimeScheduleReq(BaseModel):
    """Write a Time-Schedule slot into the controller (D3) so LOCAL cards
    enforce the window standalone/offline. `week` = 8 days (0=Mon … 6=Sun,
    7=Holiday), each a list of [begin, end] float-hour pairs (max 4)."""
    ts_number: int
    week: list[list[list[float]]] = []


def _registry(request: Request):
    return getattr(request.app.state, "access_registry", None)


def _require(request: Request, id: str):
    reg = _registry(request)
    if reg is None or not reg.has(id):
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"Access controller {id!r} not found")
    return reg


@router.get("", response_model=dict[str, AccessInfoResp])
@router.get("/", include_in_schema=False)
async def list_access(request: Request):
    reg = _registry(request)
    if reg is None:
        return {}
    return {
        aid: AccessInfoResp(id=aid, driver=e.config.driver,
                            fail_secure=e.config.fail_secure)
        for aid, e in reg.access.items()
    }


@router.get("/{id}", response_model=AccessInfoResp)
async def access_info(id: str, request: Request):
    reg = _require(request, id)
    e = reg.get(id)
    return AccessInfoResp(id=id, driver=e.config.driver,
                          fail_secure=e.config.fail_secure)


def _bus_emit(request: Request, event_type: str, device: str,
              data: dict) -> None:
    """Best-effort live signal to Odoo via bus_inject. Never raises —
    a Fleet outage must not break the door command."""
    try:
        from ...clients.bus_inject import BusInjectClient
        client = BusInjectClient.from_app(request.app)
        if client is not None:
            client.emit(event_type, device=device,
                        device_kind="access", data=data)
            client.close()
    except Exception as exc:  # noqa: BLE001
        _logger.debug("access bus_emit suppressed: %s", exc)


@router.post("/{id}/open")
async def access_open(id: str, request: Request,
                      req: OpenReq | None = None):
    """Execute an Odoo-authorised OPEN. Synchronous — returns the
    actuator result. `{"seconds": N}` → momentary pulse."""
    reg = _require(request, id)
    secs = req.seconds if req else None
    try:
        async with reg.with_access(id) as act:
            res = await asyncio.to_thread(act.open, secs)
    except Exception as exc:  # noqa: BLE001
        # Even on failure, signal denial to Odoo so the UI knows
        # the operator's button press actually got rejected by the
        # device.
        _bus_emit(request, "door.denied", id,
                  {"reason": str(exc)[:200], "seconds": secs})
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Access {id!r} open failed: {exc}",
        ) from exc
    _bus_emit(request, "door.opened", id, {
        "seconds": secs,
        "state": res.state,
        "detail": res.detail,
    })
    return res.to_json()


@router.post("/{id}/deny")
async def access_deny(id: str, request: Request):
    """Explicit close / deny (safe no-op-ish on a plain barrier)."""
    reg = _require(request, id)
    try:
        async with reg.with_access(id) as act:
            res = await asyncio.to_thread(act.deny)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Access {id!r} deny failed: {exc}",
        ) from exc
    _bus_emit(request, "door.denied", id, {
        "state": res.state,
        "detail": res.detail,
        "reason": "operator deny",
    })
    return res.to_json()


@router.post("/{id}/card")
async def access_card(id: str, request: Request, req: CardReq):
    """Program (or remove) a card in the controller's LOCAL memory.

    Used by the Odoo `polimex.card.sync` Fleet command for `local`/`both`
    credentials — the card then validates standalone on the controller,
    no server round-trip. Drivers without card management (relay/gpio/
    onvif) return 501. Synchronous — returns the driver's raw result.
    """
    reg = _require(request, id)
    op = (req.op or "add").strip().lower()
    try:
        async with reg.with_access(id) as act:
            fn = getattr(act, "add_card" if op == "add" else "remove_card",
                         None)
            if fn is None:
                raise HTTPException(
                    status.HTTP_501_NOT_IMPLEMENTED,
                    f"Access {id!r} driver does not support card "
                    f"management")
            if op == "add":
                raw = await asyncio.to_thread(
                    fn, req.card_number, req.rights_data, req.rights_mask,
                    req.ts_code, req.pin_code)
            else:
                raw = await asyncio.to_thread(
                    fn, req.card_number, req.rights_mask, req.pin_code)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Access {id!r} card {op} failed: {exc}",
        ) from exc
    _bus_emit(request, "access.card_synced", id, {
        "card_number": req.card_number, "op": op,
        "rights_data": req.rights_data, "ts_code": req.ts_code,
    })
    return {"ok": True, "id": id, "op": op,
            "card_number": req.card_number, "raw": raw}


@router.post("/{id}/time_schedule")
async def access_time_schedule(id: str, request: Request,
                               req: TimeScheduleReq):
    """Write a Polimex Time-Schedule slot (D3) into the controller. Used
    by `polimex.ts.sync` before a local card.sync so the burned card
    enforces its window offline. 501 for drivers without TS support."""
    reg = _require(request, id)
    try:
        async with reg.with_access(id) as act:
            fn = getattr(act, "write_time_schedule", None)
            if fn is None:
                raise HTTPException(
                    status.HTTP_501_NOT_IMPLEMENTED,
                    f"Access {id!r} driver does not support time "
                    f"schedules")
            raw = await asyncio.to_thread(fn, req.ts_number, req.week)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Access {id!r} time-schedule write failed: {exc}",
        ) from exc
    return {"ok": True, "id": id, "ts_number": req.ts_number, "raw": raw}


@router.get("/{id}/status")
async def access_status(id: str, request: Request):
    reg = _require(request, id)
    try:
        async with reg.with_access(id) as act:
            res = await asyncio.to_thread(act.status)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Access {id!r} status failed: {exc}",
        ) from exc
    return res.to_json()
