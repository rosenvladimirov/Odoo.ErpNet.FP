"""
Extension API for POS payment terminals (pinpads).

Not part of ErpNet.FP. URL prefix is `/pinpads` so it doesn't collide
with the printer namespace. JSON-only.

Endpoints:
  GET  /pinpads                         — list configured pinpads
  GET  /pinpads/{id}                    — info (model, serial, terminal_id)
  GET  /pinpads/{id}/status             — reversal / hang state
  GET  /pinpads/{id}/ping               — quick health check
  POST /pinpads/{id}/purchase           — card purchase transaction
  POST /pinpads/{id}/void               — void a previous purchase
  POST /pinpads/{id}/end_of_day         — daily settlement
  POST /pinpads/{id}/test_connection    — host bank reachability probe
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/pinpads", tags=["pinpads"])


# ─── Request / response schemas ──────────────────────────────────


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class PinpadInfoResp(_CamelModel):
    id: str
    driver: str
    port: Optional[str] = None
    model: Optional[str] = None
    serial_number: Optional[str] = Field(None, alias="serialNumber")
    terminal_id: Optional[str] = Field(None, alias="terminalId")
    software_version: Optional[str] = Field(None, alias="softwareVersion")


class PinpadStatusResp(_CamelModel):
    ok: bool = True
    online: bool = False
    has_reversal: bool = Field(False, alias="hasReversal")
    has_hang_transaction: bool = Field(False, alias="hasHangTransaction")
    end_day_required: bool = Field(False, alias="endDayRequired")
    error: Optional[str] = None


class PurchaseBody(_CamelModel):
    amount: float = Field(..., description="Purchase amount (e.g. 12.50 BGN)")
    tip: Optional[float] = None
    cashback: Optional[float] = None
    reference: Optional[str] = None


class VoidBody(_CamelModel):
    amount: float
    rrn: str
    auth_id: str = Field(..., alias="authId")
    tip: Optional[float] = None
    cashback: Optional[float] = None


class TransactionResp(_CamelModel):
    ok: bool = True
    error: Optional[str] = None
    amount: Optional[float] = None
    rrn: Optional[str] = None
    auth_id: Optional[str] = Field(None, alias="authId")
    host_rrn: Optional[str] = Field(None, alias="hostRrn")
    host_auth_id: Optional[str] = Field(None, alias="hostAuthId")
    terminal_id: Optional[str] = Field(None, alias="terminalId")


# ─── Helpers ─────────────────────────────────────────────────────


def _pinpad_registry(request: Request):
    return request.app.state.pinpad_registry


def _require(request: Request, id: str):
    reg = _pinpad_registry(request)
    if reg is None or not reg.has(id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Pinpad {id!r} not found"
        )
    return reg


def _to_cents(amount_bgn: float) -> int:
    """Pinpad protocol uses smallest currency units (stotinki)."""
    return int(round(float(amount_bgn) * 100))


def _from_cents(cents: Optional[int]) -> Optional[float]:
    return None if cents is None else round(cents / 100.0, 2)


def _result_to_resp(result, *, fallback_error: Optional[str] = None) -> TransactionResp:
    return TransactionResp(
        ok=result.ok,
        error=result.error or fallback_error,
        amount=_from_cents(result.amount_cents),
        rrn=result.rrn,
        auth_id=result.auth_id,
        host_rrn=result.host_rrn,
        host_auth_id=result.host_auth_id,
        terminal_id=result.terminal_id,
    )


# ─── Listing / info / status / ping ──────────────────────────────


@router.get("", response_model=dict[str, PinpadInfoResp])
@router.get("/", include_in_schema=False)
async def list_pinpads(request: Request):
    reg = _pinpad_registry(request)
    if reg is None:
        return {}
    return {
        pid: PinpadInfoResp(
            id=pid,
            driver=entry.config.driver,
            port=entry.config.port,
        )
        for pid, entry in reg.pinpads.items()
    }


@router.get("/{id}", response_model=PinpadInfoResp)
async def pinpad_info(id: str, request: Request):
    reg = _require(request, id)
    cfg = reg.get(id).config
    try:
        async with reg.with_pinpad(id) as pp:
            info = await asyncio.to_thread(pp.get_info)
        return PinpadInfoResp(
            id=id,
            driver=cfg.driver,
            port=cfg.port,
            model=info.model_name,
            serial_number=info.serial_number,
            terminal_id=info.terminal_id,
            software_version=".".join(str(x) for x in info.software_version),
        )
    except Exception as exc:
        _logger.exception("pinpad info failed for %s", id)
        return PinpadInfoResp(
            id=id, driver=cfg.driver, port=cfg.port, model=str(exc)
        )


@router.get("/{id}/status", response_model=PinpadStatusResp)
async def pinpad_status(id: str, request: Request):
    reg = _require(request, id)
    try:
        async with reg.with_pinpad(id) as pp:
            online = await asyncio.to_thread(pp.ping)
            if not online:
                return PinpadStatusResp(ok=False, online=False, error="ping failed")
            st = await asyncio.to_thread(pp.get_status)
        return PinpadStatusResp(
            ok=True,
            online=True,
            has_reversal=st.has_reversal,
            has_hang_transaction=st.has_hang_transaction,
            end_day_required=st.end_day_required,
        )
    except Exception as exc:
        _logger.exception("pinpad status failed for %s", id)
        return PinpadStatusResp(ok=False, online=False, error=str(exc))


@router.get("/{id}/ping")
async def pinpad_ping(id: str, request: Request):
    reg = _require(request, id)
    try:
        async with reg.with_pinpad(id) as pp:
            ok = await asyncio.to_thread(pp.ping)
        return {"ok": bool(ok)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ─── Transactions ────────────────────────────────────────────────


@router.post("/{id}/purchase", response_model=TransactionResp)
async def pinpad_purchase(id: str, body: PurchaseBody, request: Request):
    reg = _require(request, id)
    try:
        async with reg.with_pinpad(id) as pp:
            result = await asyncio.to_thread(
                pp.purchase,
                amount_cents=_to_cents(body.amount),
                tip_cents=_to_cents(body.tip) if body.tip else None,
                cashback_cents=_to_cents(body.cashback) if body.cashback else None,
                reference=body.reference,
            )
        return _result_to_resp(result)
    except Exception as exc:
        _logger.exception("pinpad purchase failed for %s", id)
        return TransactionResp(ok=False, error=str(exc))


@router.post("/{id}/void", response_model=TransactionResp)
async def pinpad_void(id: str, body: VoidBody, request: Request):
    reg = _require(request, id)
    try:
        async with reg.with_pinpad(id) as pp:
            result = await asyncio.to_thread(
                pp.void_purchase,
                amount_cents=_to_cents(body.amount),
                rrn=body.rrn,
                auth_id=body.auth_id,
                tip_cents=_to_cents(body.tip) if body.tip else None,
                cashback_cents=_to_cents(body.cashback) if body.cashback else None,
            )
        return _result_to_resp(result)
    except Exception as exc:
        _logger.exception("pinpad void failed for %s", id)
        return TransactionResp(ok=False, error=str(exc))


@router.post("/{id}/end_of_day", response_model=TransactionResp)
async def pinpad_end_of_day(id: str, request: Request):
    reg = _require(request, id)
    try:
        async with reg.with_pinpad(id) as pp:
            result = await asyncio.to_thread(pp.end_of_day)
        return _result_to_resp(result)
    except Exception as exc:
        _logger.exception("pinpad end_of_day failed for %s", id)
        return TransactionResp(ok=False, error=str(exc))


@router.post("/{id}/test_connection", response_model=TransactionResp)
async def pinpad_test_connection(id: str, request: Request):
    reg = _require(request, id)
    try:
        async with reg.with_pinpad(id) as pp:
            result = await asyncio.to_thread(pp.test_connection)
        return _result_to_resp(result)
    except Exception as exc:
        _logger.exception("pinpad test_connection failed for %s", id)
        return TransactionResp(ok=False, error=str(exc))
