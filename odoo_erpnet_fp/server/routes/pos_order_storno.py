"""
BlueCash storno Phase 2 — pos.order fiscal_receipt lookup + refund-printed
notification.

Implements `anchor_bluecash_storno_phase2_contract.md` endpoints. Sibling
of `shift_close` + `shift_signal` route groups; same HMAC auth scheme
(`iot_setup.token` shared secret) and same canonical-JSON rule.

Endpoints:

    GET  /pos.order/<id>/fiscal_receipt
        Lookup by Odoo pos.order ID. Returns lookup data the Android
        StornoRunner needs to issue Datecs cmd 0x2B (open storno
        receipt): UNS, doc_number, issued_at, device_serial, till,
        lines[], payments[].

    GET  /pos.order/by_uns/<uns>/fiscal_receipt
        Lookup by UNS (cross-device storno — cashier on device B refunds
        a sale issued on device A; only the UNS is available from the
        physical receipt).

    POST /pos.order/<id>/refund_printed
        Android notifies after the storno is printed. Proxy forwards
        body {storno_uns, storno_doc_number, device_serial} to Odoo
        which creates the matching pos.order.refund (or links the
        existing draft).

Auth: same X-Registry-Signature HMAC-SHA256 scheme as shift_close.
For GET endpoints (no body), the signature is computed over the URL
path bytes — e.g. `pos.order/42/fiscal_receipt`. The leading slash is
NOT part of the signed bytes.

Forwarding: bodies + signed-URLs are re-signed by the proxy and sent
to `<iot_setup.odoo_url>/erp_net_fp/pos_order/...`.
"""

from __future__ import annotations

import logging
from typing import Optional
from urllib.parse import quote as _urlquote

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from ..odoo_forwarder import canonicalise, post_signed, sign_body, verify_signature

_logger = logging.getLogger(__name__)
# Точка в prefix-а — мирен с anchor-а (`/pos.order/...`); FastAPI
# приема dots в path-а без специален escaping.
router = APIRouter(prefix="/pos.order", tags=["pos_order_storno"])


# ── Schemas ──────────────────────────────────────────────────────────


class _Cml(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class RefundLineDTO(_Cml):
    plu: int
    qty: float
    unit_price: float = Field(..., alias="unitPrice")
    vat_letter: str = Field(..., alias="vatLetter")


class RefundPaymentDTO(_Cml):
    fiscal_slot: int = Field(..., alias="fiscalSlot")
    amount: float


class FiscalReceiptResp(_Cml):
    uns: str
    doc_number: str = Field(..., alias="docNumber")
    issued_at: str = Field(..., alias="issuedAt")
    device_serial: str = Field(..., alias="deviceSerial")
    till_number: int = Field(0, alias="tillNumber")
    lines: list[RefundLineDTO] = Field(default_factory=list)
    payments: list[RefundPaymentDTO] = Field(default_factory=list)


class RefundPrintedBody(_Cml):
    storno_uns: str = Field(..., alias="stornoUns")
    storno_doc_number: str = Field(..., alias="stornoDocNumber")
    device_serial: str = Field(..., alias="deviceSerial")


class RefundPrintedResp(_Cml):
    ok: bool
    refund_order_id: int = Field(0, alias="refundOrderId")
    warnings: list[str] = Field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────


def _shared_secret(cfg) -> Optional[str]:
    return getattr(getattr(cfg, "iot_setup", None), "token", None) or None


def _odoo_base(cfg) -> Optional[str]:
    url = (getattr(getattr(cfg, "iot_setup", None), "odoo_url", "")
           or "").rstrip("/")
    return url or None


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("/{order_id:int}/fiscal_receipt", response_model=FiscalReceiptResp)
async def fiscal_receipt_by_id(
    order_id: int, request: Request,
    x_registry_signature: Optional[str] = Header(
        None, alias="X-Registry-Signature"),
) -> FiscalReceiptResp:
    """Lookup pos.order fiscal metadata by Odoo ID. HMAC over URL path."""
    cfg = request.app.state.config.server
    secret = _shared_secret(cfg)
    if not secret:
        raise HTTPException(503, "iot_setup.token not configured")
    # HMAC payload = URL path bytes (without leading slash).
    sign_payload = f"pos.order/{order_id}/fiscal_receipt".encode("utf-8")
    if not verify_signature(sign_payload, secret, x_registry_signature or ""):
        raise HTTPException(401, "X-Registry-Signature mismatch")

    base = _odoo_base(cfg)
    if not base:
        raise HTTPException(503, "iot_setup.odoo_url not configured")
    # Forward — same auth scheme towards Odoo (HMAC over URL path).
    url = f"{base}/erp_net_fp/pos_order/{order_id}/fiscal_receipt"
    forward_payload = f"erp_net_fp/pos_order/{order_id}/fiscal_receipt"\
        .encode("utf-8")
    sig = sign_body(forward_payload, secret)
    import httpx
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                url, timeout=30.0,
                headers={
                    "X-Registry-Signature": sig,
                    "User-Agent": "Odoo.ErpNet.FP/1.0 (proxy bridge)",
                    "Accept": "application/json",
                })
        except httpx.HTTPError as exc:
            raise HTTPException(
                502, f"Odoo unreachable: {exc}") from exc
    if r.status_code == 404:
        raise HTTPException(404, "pos.order not found")
    if r.status_code == 409:
        # Already refunded — bubble up Odoo's body verbatim.
        try:
            raise HTTPException(409, r.json())
        except ValueError:
            raise HTTPException(409, {"detail": r.text[:300]})
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    try:
        parsed = r.json()
    except ValueError as exc:
        raise HTTPException(
            502, f"Odoo returned non-JSON: {exc}") from exc
    return FiscalReceiptResp(**parsed)


@router.get("/by_uns/{uns}/fiscal_receipt", response_model=FiscalReceiptResp)
async def fiscal_receipt_by_uns(
    uns: str, request: Request,
    x_registry_signature: Optional[str] = Header(
        None, alias="X-Registry-Signature"),
) -> FiscalReceiptResp:
    """Cross-device lookup by UNS. Same response shape as by_id."""
    cfg = request.app.state.config.server
    secret = _shared_secret(cfg)
    if not secret:
        raise HTTPException(503, "iot_setup.token not configured")
    # HMAC payload = URL path. UNS може да съдържа dashes; квoting
    # за безопасност (макар че fmt е XXXXXXXX-NNNN-NNNNNNN — safe).
    safe_uns = _urlquote(uns, safe="-")
    sign_payload = f"pos.order/by_uns/{safe_uns}/fiscal_receipt"\
        .encode("utf-8")
    if not verify_signature(sign_payload, secret, x_registry_signature or ""):
        raise HTTPException(401, "X-Registry-Signature mismatch")

    base = _odoo_base(cfg)
    if not base:
        raise HTTPException(503, "iot_setup.odoo_url not configured")
    url = f"{base}/erp_net_fp/pos_order/by_uns/{safe_uns}/fiscal_receipt"
    forward_payload = f"erp_net_fp/pos_order/by_uns/{safe_uns}/fiscal_receipt"\
        .encode("utf-8")
    sig = sign_body(forward_payload, secret)
    import httpx
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(
                url, timeout=30.0,
                headers={
                    "X-Registry-Signature": sig,
                    "User-Agent": "Odoo.ErpNet.FP/1.0 (proxy bridge)",
                    "Accept": "application/json",
                })
        except httpx.HTTPError as exc:
            raise HTTPException(
                502, f"Odoo unreachable: {exc}") from exc
    if r.status_code == 404:
        raise HTTPException(404, f"pos.order with UNS {uns!r} not found")
    if r.status_code == 409:
        try:
            raise HTTPException(409, r.json())
        except ValueError:
            raise HTTPException(409, {"detail": r.text[:300]})
    if r.status_code >= 400:
        raise HTTPException(r.status_code, r.text[:300])
    try:
        parsed = r.json()
    except ValueError as exc:
        raise HTTPException(
            502, f"Odoo returned non-JSON: {exc}") from exc
    return FiscalReceiptResp(**parsed)


@router.post("/{order_id:int}/refund_printed", response_model=RefundPrintedResp)
async def refund_printed(
    order_id: int, body: RefundPrintedBody, request: Request,
    x_registry_signature: Optional[str] = Header(
        None, alias="X-Registry-Signature"),
) -> RefundPrintedResp:
    """Android notifies after storno printed. Proxy forwards to Odoo."""
    cfg = request.app.state.config.server
    secret = _shared_secret(cfg)
    if not secret:
        raise HTTPException(503, "iot_setup.token not configured")
    raw = await request.body()
    if not verify_signature(raw, secret, x_registry_signature or ""):
        raise HTTPException(401, "X-Registry-Signature mismatch")

    base = _odoo_base(cfg)
    if not base:
        raise HTTPException(503, "iot_setup.odoo_url not configured")
    url = f"{base}/erp_net_fp/pos_order/{order_id}/refund_printed"
    # Re-canonicalise via pydantic dump (eliminates Android JSON drift).
    forward_body = body.model_dump(by_alias=False, exclude_none=False)
    http_status, parsed = await post_signed(
        url, forward_body, secret=secret, timeout=30.0)
    if http_status == 0:
        raise HTTPException(
            502, f"Odoo unreachable: {parsed.get('error', 'unknown')}")
    if http_status >= 400:
        raise HTTPException(http_status, parsed)
    try:
        return RefundPrintedResp(**parsed)
    except Exception as exc:
        _logger.warning("Odoo refund_printed unexpected shape: %s", exc)
        return RefundPrintedResp(
            ok=bool(parsed.get("ok", True)),
            refund_order_id=int(parsed.get("refund_order_id", 0)),
            warnings=list(parsed.get("warnings") or []),
        )


__all__ = ["router"]
