"""
BlueCash shift-close sync endpoint.

POST /devices/<serial>/shift_close — Android client uploads a closed
shift (Z-report done) to Odoo via this proxy.

Auth — same HMAC scheme as the proxy↔Odoo registry link:
    raw      = json.dumps(body, separators=",:", sort_keys=True, ensure_ascii=False).encode("utf-8")
    expected = HMAC-SHA256(raw, <device-shared-secret>).hex()
    Header   : X-Registry-Signature: <expected>

Idempotency — SQLite cache keyed by (device_serial, fiscal_day_number,
z_report_number); a replay returns the cached Odoo response unchanged.

Forwarding — body is re-signed (canonicalised) and POSTed to
`iot_setup.odoo_url/erp_net_fp/shift_close` using the registry secret
(NOT the device secret). Two layers:
    Android → proxy : device secret (per-device, allow-list)
    proxy   → Odoo  : registry secret (paired during fleet registration)

Contract: `anchor_bluecash_shift_sync_contract.md`.
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, status
from pydantic import BaseModel, ConfigDict, Field

from ..odoo_forwarder import canonicalise, post_signed, verify_signature
from ..shift_dedup import get_dedup_cache

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/devices", tags=["shift_sync"])


# ── Schemas (camelCase out, snake_case in — both accepted) ────────────


class _Cml(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class PaymentDTO(_Cml):
    fiscal_slot: int = Field(..., alias="fiscalSlot")
    amount: float


class ReceiptDTO(_Cml):
    receipt_number: str = Field(..., alias="receiptNumber")
    uns: str
    gross_amount: float = Field(..., alias="grossAmount")
    line_count: int = Field(..., alias="lineCount")
    printed_at: str = Field(..., alias="printedAt")
    is_storno: bool = Field(False, alias="isStorno")
    original_uns: Optional[str] = Field(None, alias="originalUns")
    payments: list[PaymentDTO] = Field(default_factory=list)


class CashMovementDTO(_Cml):
    direction: str  # "IN" | "OUT"
    amount: float
    occurred_at: str = Field(..., alias="occurredAt")
    operator_code: str = Field(..., alias="operatorCode")


class TotalsDTO(_Cml):
    receipt_count: int = Field(..., alias="receiptCount")
    total_gross: float = Field(..., alias="totalGross")
    cash_in_total: float = Field(0.0, alias="cashInTotal")
    cash_out_total: float = Field(0.0, alias="cashOutTotal")


class ShiftCloseBody(_Cml):
    device_serial: str = Field(..., alias="deviceSerial")
    odoo_session_id: int = Field(..., alias="odooSessionId")
    fiscal_day_number: int = Field(..., alias="fiscalDayNumber")
    operator_code: str = Field(..., alias="operatorCode")
    opened_at: str = Field(..., alias="openedAt")
    closed_at: str = Field(..., alias="closedAt")
    z_report_number: str = Field(..., alias="zReportNumber")
    z_report_at: str = Field(..., alias="zReportAt")
    totals: TotalsDTO
    receipts: list[ReceiptDTO] = Field(default_factory=list)
    cash_movements: list[CashMovementDTO] = Field(
        default_factory=list, alias="cashMovements")


class ShiftCloseResp(_Cml):
    status: str
    odoo_session_id: int = Field(0, alias="odooSessionId")
    odoo_orders_created: int = Field(0, alias="odooOrdersCreated")
    odoo_refunds_created: int = Field(0, alias="odooRefundsCreated")
    warnings: list[str] = Field(default_factory=list)


# ── Helpers ───────────────────────────────────────────────────────────


def _get_device_secret(cfg, serial: str) -> Optional[str]:
    """Lookup per-device shared secret.

    Phase 1: ползваме `iot_setup.token` — същата стойност, която
    proxy-то използва при `iot_setup` регистрация и която Odoo
    запазва в `ir.config_parameter` ключа `iot_token`. Това дава
    симетрия с iot_oca controller-а: Android и Odoo дeлят един
    shared secret (per-tenant, не per-device).

    Future: per-device long-lived bearer от proxy registration flow."""
    return getattr(getattr(cfg, "iot_setup", None), "token", None) or None


def _get_odoo_endpoint(cfg) -> Optional[tuple[str, str]]:
    """Връща (full_url, secret) за proxy→Odoo POST или None ако не е
    конфигурирано. URL = `<iot_setup.odoo_url>/erp_net_fp/shift_close`;
    secret = `iot_setup.token` (същият shared secret като device-side)."""
    iot_url = getattr(getattr(cfg, "iot_setup", None), "odoo_url", "") or ""
    iot_url = iot_url.rstrip("/")
    sec = getattr(getattr(cfg, "iot_setup", None), "token", "") or ""
    if not iot_url or not sec:
        return None
    return f"{iot_url}/erp_net_fp/shift_close", sec


# ── Endpoint ──────────────────────────────────────────────────────────


@router.post("/{serial}/shift_close", response_model=ShiftCloseResp)
async def shift_close(
    serial: str,
    body: ShiftCloseBody,
    request: Request,
    x_registry_signature: Optional[str] = Header(
        None, alias="X-Registry-Signature"),
    dry_run: int = Query(0, ge=0, le=1),
) -> ShiftCloseResp:
    """Receive Android-side closed-shift payload, forward to Odoo.

    Replay-safe: same (serial, day, z) → cached response.
    """
    cfg = request.app.state.cfg
    if body.device_serial != serial:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(f"deviceSerial in body ({body.device_serial!r}) "
                    f"does not match URL serial ({serial!r})"),
        )

    # ── 1. Verify HMAC ───────────────────────────────────────────────
    device_secret = _get_device_secret(cfg, serial)
    if not device_secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Proxy not paired with a registry secret; cannot "
                   "verify device signature.",
        )
    raw = await request.body()
    if not verify_signature(raw, device_secret, x_registry_signature or ""):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="X-Registry-Signature mismatch",
        )

    # ── 2. Idempotency cache lookup ──────────────────────────────────
    cache = get_dedup_cache()
    cached = cache.get(
        body.device_serial, body.fiscal_day_number, body.z_report_number)
    if cached is not None and not dry_run:
        http_status, resp = cached
        _logger.info(
            "shift_close cache HIT for (%s, %s, %s) → status=%s",
            body.device_serial, body.fiscal_day_number,
            body.z_report_number, http_status,
        )
        if http_status >= 400:
            # Re-raise the cached failure as the same HTTP code.
            raise HTTPException(status_code=http_status, detail=resp)
        return ShiftCloseResp(**resp)

    # ── 3. Forward to Odoo ───────────────────────────────────────────
    endpoint = _get_odoo_endpoint(cfg)
    if not endpoint:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="iot_setup.odoo_url not configured; cannot forward "
                   "shift_close to Odoo.",
        )
    odoo_url, registry_secret = endpoint
    if dry_run:
        odoo_url = f"{odoo_url}/dry_run"

    # Каноничното тяло към Odoo НЕ е raw-а от Android (тъй като би имал
    # друг ordering на ключовете) — а свежа canonicalisation от
    # parsed-ия pydantic модел. Това гарантира че Odoo вижда stable
    # input независимо от Android-side JSON formatting.
    forward_body = body.model_dump(by_alias=False, exclude_none=False)
    http_status, parsed = await post_signed(
        odoo_url, forward_body, secret=registry_secret, timeout=30.0,
    )

    if http_status == 0:
        # Transport error → 502 Bad Gateway, не cache-ваме (transient).
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Odoo unreachable: {parsed.get('error', 'unknown')}",
        )
    if http_status >= 400:
        # 409 Conflict (session mismatch) и подобни — cache-ваме защото
        # резултатът е детерминистичен (Odoo ще върне същото при replay).
        if not dry_run:
            cache.put(
                body.device_serial, body.fiscal_day_number,
                body.z_report_number, parsed, http_status=http_status,
            )
        raise HTTPException(status_code=http_status, detail=parsed)

    # ── 4. Cache + return success ────────────────────────────────────
    if not dry_run:
        cache.put(
            body.device_serial, body.fiscal_day_number,
            body.z_report_number, parsed, http_status=200,
        )
    # Tolerant parsing — Odoo може да върне допълнителни полета.
    try:
        return ShiftCloseResp(**parsed)
    except Exception as exc:
        _logger.warning(
            "Odoo shift_close response shape unexpected: %s — body=%s",
            exc, parsed)
        return ShiftCloseResp(
            status=str(parsed.get("status", "ok")),
            odoo_session_id=int(parsed.get("odoo_session_id", 0)),
            odoo_orders_created=int(parsed.get("odoo_orders_created", 0)),
            odoo_refunds_created=int(parsed.get("odoo_refunds_created", 0)),
            warnings=list(parsed.get("warnings") or []),
        )


# ── Admin helper (forget cache entry) ─────────────────────────────────


@router.delete("/{serial}/shift_close/{fiscal_day}/{z_number}")
async def shift_close_forget(
    serial: str, fiscal_day: int, z_number: str, request: Request,
    x_registry_signature: Optional[str] = Header(
        None, alias="X-Registry-Signature"),
) -> dict:
    """Explicit dedup eviction. Used при forensic re-import; вижда се
    в `anchor_bluecash_shift_sync_contract.md` §Risk 7."""
    cfg = request.app.state.cfg
    device_secret = _get_device_secret(cfg, serial)
    if not device_secret:
        raise HTTPException(503, "Proxy not paired")
    # За DELETE няма body — signature е над URL-а.
    url_bytes = f"{serial}/{fiscal_day}/{z_number}".encode("utf-8")
    if not verify_signature(url_bytes, device_secret,
                            x_registry_signature or ""):
        raise HTTPException(401, "X-Registry-Signature mismatch")
    cache = get_dedup_cache()
    removed = cache.forget(serial, fiscal_day, z_number)
    return {"ok": True, "removed": removed}


__all__ = ["router"]
