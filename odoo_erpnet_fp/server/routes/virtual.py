"""
Управлението на виртуалния апарат (`transport: virtual`) — за тестове и
пясъчник: дневникът на „отпечатаните“ документи, инжектирани грешки и
нулиране. Отговаря само за принтер с виртуален транспорт; за истински
апарат е 404 — грешка не може да се инжектира в жива каса.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from ...drivers.fiscal.datecs_pm import transport_virtual as tv
from ...drivers.pinpad import virtual as vp

router = APIRouter(prefix="/printers/{id}/virtual", tags=["virtual printer"])


class FaultsBody(BaseModel):
    faults: list[str] = []


def _require_virtual(request: Request, id: str):
    registry = request.app.state.registry
    if not registry.has(id) or registry.get(id).config.transport != "virtual":
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Virtual printer {id!r} not found")
    return registry.get(id).config


def _snapshot(state: tv.VirtualPmState) -> dict:
    with state.lock:
        return {
            "faults": sorted(state.faults),
            "slipNumber": state.slip_no,
            "zReportNumber": state.z_no,
            "open": state.open,
            "documents": list(state.journal),
        }


@router.get("")
async def virtual_state(id: str, request: Request) -> dict:
    cfg = _require_virtual(request, id)
    return _snapshot(tv.get_state(id, cfg.extras.get("virtual_journal")))


@router.put("/faults")
async def virtual_faults(id: str, body: FaultsBody, request: Request) -> dict:
    _require_virtual(request, id)
    try:
        faults = tv.set_faults(id, body.faults)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {"faults": sorted(faults), "known": sorted(tv.KNOWN_FAULTS)}


@router.post("/reset")
async def virtual_reset(id: str, request: Request) -> dict:
    cfg = _require_virtual(request, id)
    tv.reset_state(id)
    return _snapshot(tv.get_state(id, cfg.extras.get("virtual_journal"), cfg.extras.get("virtual_faults")))


# ─── виртуалният пинпад (`driver: virtual`) ─────────────────────────

pinpad_router = APIRouter(prefix="/pinpads/{id}/virtual", tags=["virtual pinpad"])


class ModeBody(BaseModel):
    mode: str


def _require_virtual_pinpad(request: Request, id: str):
    reg = request.app.state.pinpad_registry
    if reg is None or not reg.has(id) or reg.get(id).config.driver != "virtual":
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Virtual pinpad {id!r} not found")
    return reg.get(id).config


def _pinpad_snapshot(state: vp.VirtualPinpadState) -> dict:
    with state.lock:
        return {
            "mode": state.mode,
            "journal": list(state.journal),
            "approved": {rrn: dict(tx) for rrn, tx in state.approved.items()},
        }


@pinpad_router.get("")
async def virtual_pinpad_state(id: str, request: Request) -> dict:
    cfg = _require_virtual_pinpad(request, id)
    return _pinpad_snapshot(vp.get_state(id, (cfg.extras or {}).get("virtual_mode")))


@pinpad_router.put("/mode")
async def virtual_pinpad_mode(id: str, body: ModeBody, request: Request) -> dict:
    _require_virtual_pinpad(request, id)
    try:
        mode = vp.set_mode(id, body.mode)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {"mode": mode, "known": sorted(vp.KNOWN_MODES)}


@pinpad_router.post("/reset")
async def virtual_pinpad_reset(id: str, request: Request) -> dict:
    cfg = _require_virtual_pinpad(request, id)
    vp.reset_state(id)
    return _pinpad_snapshot(vp.get_state(id, (cfg.extras or {}).get("virtual_mode")))
