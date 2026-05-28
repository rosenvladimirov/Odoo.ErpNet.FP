"""Shift-bridge HTTP API — Odoo's gateway to Android ShiftBridgeService.

Endpoints (all auth: HMAC-SHA256 X-Registry-Signature over canonical body,
secret = `cfg.iot_setup.token`; symmetric с Odoo `pos_session_signal`
client + shift_bridge_client):

    GET  /shifts/<serial>/pending
        → JSON: {"shifts": [<ShiftPayload>, ...]}
        Proxy calls `bridge.call("shift.pull_pending", {})`. Returns the
        list of unsynced Z-closed shifts on the Android device. Empty list
        if nothing pending. Replay-safe (Android tracks `synced_at`).

    POST /shifts/<serial>/mark_synced
        Body: {"shift_id": <int>, "odoo_session_id": <int>, "synced_at": <iso>}
        → {"ok": true}
        Tells Android that Odoo accepted shift `shift_id` and stored
        Odoo's `pos.session.id` (so cross-references work later).

    GET  /shifts/<serial>/status
        → {"open_shift": <summary>|null, "pending_count": <int>,
            "last_z_at": <iso>|null}
        Live snapshot of device state. Used от Odoo UI dashboard +
        debugging.

    POST /shifts/<serial>/signal
        Body: {"type": "shift.open"|"shift.close.request",
                "pos_session_id": <int>, "operator_code": <str>,
                "fiscal_day_number": <int>, "issued_at": <iso>,
                "reason"?: <str>}
        → {"ok": true, "acknowledged": true}
        Push shift lifecycle event from Odoo to Android. Replaces the
        old WS/SSE pub-sub (routes/shift_signal.py — DELETED).

The proxy holds one persistent TCP connection per device; this HTTP
layer is stateless and just multiplexes Odoo's requests onto the
correct bridge.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from ..drivers.shifts import ShiftBridgeError, get_shift_registry
from ..odoo_forwarder import verify_signature

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/shifts", tags=["shifts"])


# ─── Auth helper ─────────────────────────────────────────────────────


def _shared_secret(cfg) -> Optional[str]:
    """Same `iot_setup.token` shared secret като shift_signal/shift_close
    (deprecated). Symmetric с Odoo `ir.config_parameter('iot_token')`.
    """
    return getattr(getattr(cfg, "iot_setup", None), "token", None) or None


async def _verify_request(
    request: Request,
    signature: Optional[str],
    raw_body: bytes,
) -> None:
    """HMAC verify; raises HTTPException на failure."""
    cfg = request.app.state.config.server
    secret = _shared_secret(cfg)
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Proxy not paired (iot_setup.token unset); "
                   "cannot verify shift bridge requests.",
        )
    # For GET requests с empty body, signature is computed over the
    # path bytes (URL без host) — consistent с DELETE pattern в стария
    # shift_close_forget endpoint.
    if not raw_body:
        raw_body = request.url.path.encode("utf-8")
    if not verify_signature(raw_body, secret, signature or ""):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Registry-Signature mismatch",
        )


def _get_bridge(serial: str):
    reg = get_shift_registry()
    bridge = reg.get(serial)
    if bridge is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No shift bridge configured for device {serial!r}",
        )
    return bridge


# ─── Schemas ─────────────────────────────────────────────────────────


class _Cml(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class MarkSyncedBody(_Cml):
    shift_id: int = Field(..., alias="shiftId")
    odoo_session_id: int = Field(..., alias="odooSessionId")
    synced_at: str = Field(..., alias="syncedAt")


class SignalBody(_Cml):
    type: str
    pos_session_id: int = Field(..., alias="posSessionId")
    operator_code: str = Field("", alias="operatorCode")
    fiscal_day_number: int = Field(0, alias="fiscalDayNumber")
    issued_at: str = Field("", alias="issuedAt")
    reason: Optional[str] = None


class PendingResp(_Cml):
    shifts: list[dict] = Field(default_factory=list)


class OkResp(_Cml):
    ok: bool
    acknowledged: bool = False


class StatusResp(_Cml):
    open_shift: Optional[dict] = Field(None, alias="openShift")
    pending_count: int = Field(0, alias="pendingCount")
    last_z_at: Optional[str] = Field(None, alias="lastZAt")


# ─── Endpoints ───────────────────────────────────────────────────────


@router.get("/{serial}/pending", response_model=PendingResp)
async def shifts_pending(
    serial: str,
    request: Request,
    x_registry_signature: Optional[str] = Header(
        None, alias="X-Registry-Signature"),
) -> PendingResp:
    """Pull pending (Z-closed, not-yet-synced) shifts from Android."""
    raw = await request.body()
    await _verify_request(request, x_registry_signature, raw)
    bridge = _get_bridge(serial)
    try:
        result = await bridge.call("shift.pull_pending", {})
    except ShiftBridgeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Android bridge error: {exc} (code={exc.code})",
        ) from exc
    return PendingResp(shifts=list(result.get("shifts") or []))


@router.post("/{serial}/mark_synced", response_model=OkResp)
async def shifts_mark_synced(
    serial: str,
    body: MarkSyncedBody,
    request: Request,
    x_registry_signature: Optional[str] = Header(
        None, alias="X-Registry-Signature"),
) -> OkResp:
    """Notify Android that Odoo accepted `shift_id` (stored Odoo's session id).

    Replay-safe: Android side checks `synced_at`. Posting the same
    shift_id twice is idempotent (Android updates the row only ако
    `synced_at IS NULL`).
    """
    raw = await request.body()
    await _verify_request(request, x_registry_signature, raw)
    bridge = _get_bridge(serial)
    try:
        await bridge.call("shift.mark_synced", body.model_dump(
            by_alias=False, exclude_none=False))
    except ShiftBridgeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Android bridge error: {exc} (code={exc.code})",
        ) from exc
    return OkResp(ok=True, acknowledged=True)


@router.get("/{serial}/status", response_model=StatusResp)
async def shifts_status(
    serial: str,
    request: Request,
    x_registry_signature: Optional[str] = Header(
        None, alias="X-Registry-Signature"),
) -> StatusResp:
    """Live state on the device — current open shift + pending counter."""
    raw = await request.body()
    await _verify_request(request, x_registry_signature, raw)
    bridge = _get_bridge(serial)
    try:
        result = await bridge.call("shift.status", {})
    except ShiftBridgeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Android bridge error: {exc} (code={exc.code})",
        ) from exc
    return StatusResp(
        open_shift=result.get("open_shift"),
        pending_count=int(result.get("pending_count") or 0),
        last_z_at=result.get("last_z_at"),
    )


@router.post("/{serial}/signal", response_model=OkResp)
async def shifts_signal(
    serial: str,
    body: SignalBody,
    request: Request,
    x_registry_signature: Optional[str] = Header(
        None, alias="X-Registry-Signature"),
) -> OkResp:
    """Push a shift lifecycle event from Odoo to Android.

    Types currently accepted by Android handler (string-validated там, не
    тук — proxy passes through whatever Odoo sent):
      * `shift.open` — Odoo opened a pos.session; Android should
        `ShiftTracker.openShift(pos_session_id, fiscal_day_number,
        operator_code)`.
      * `shift.close.request` — Odoo is about to close pos.session;
        Android prompts cashier to run Z.
    """
    raw = await request.body()
    await _verify_request(request, x_registry_signature, raw)
    bridge = _get_bridge(serial)
    try:
        result = await bridge.call("shift.signal", body.model_dump(
            by_alias=False, exclude_none=False))
    except ShiftBridgeError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Android bridge error: {exc} (code={exc.code})",
        ) from exc
    return OkResp(
        ok=bool(result.get("ok", True)),
        acknowledged=bool(result.get("acknowledged", True)),
    )


# ─── Admin / debug ───────────────────────────────────────────────────


@router.get("/_/registry")
async def shifts_registry_info() -> dict:
    """Debug: list configured shift bridges + their connection status."""
    reg = get_shift_registry()
    out = {}
    for serial in reg.all_serials():
        bridge = reg.get(serial)
        out[serial] = {
            "id": bridge.cfg.id if bridge else None,
            "tcp_host": bridge.cfg.tcp_host if bridge else None,
            "tcp_port": bridge.cfg.tcp_port if bridge else None,
            "is_connected": bridge.is_connected if bridge else False,
        }
    return {"bridges": out}


__all__ = ["router"]
