"""
ErpNet.FP-compatible HTTP routes — 1:1 with `PrintersController.cs`.

All endpoints return JSON. Bodies and shapes follow PROTOCOL.md exactly,
so existing clients (e.g. `l10n_bg_erp_net_fp`) work unchanged.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, Request, status

from ...drivers.fiscal.datecs_pm.errors import FiscalError
from ...drivers.fiscal.datecs_isl.protocol import IslDeviceInfo
from ..adapters import messages as msg_adapter
from ..adapters import payment_type as pt_adapter
from ..adapters import tax_group as tg_adapter
from ..schemas import (
    CashAmountResult,
    DeviceInfo,
    DeviceStatusWithDateTime,
    GenericResult,
    Invoice,
    Payment,
    PrintReceiptResult,
    Receipt,
    RequestFrame,
    ReversalReceipt,
    SaleItem,
    SubtotalAmountItem,
    StatusMessage,
    TransferAmount,
    PaymentType,
    PriceModifierType,
    ItemType,
    MessageType,
)

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/printers", tags=["printers"])


def _registry(request: Request):
    return request.app.state.registry


def _require_printer(request: Request, id: str):
    registry = _registry(request)
    if not registry.has(id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Printer {id!r} not found")
    return registry


def _now_iso() -> str:
    """ErpNet.FP-style ISO datetime — `2019-05-17T13:55:18`."""
    return datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _device_info(entry) -> DeviceInfo:
    """Best-effort DeviceInfo without contacting the device.

    The on-device fields (serial, fiscal memory, firmware) populate
    asynchronously after first /status call. For now, expose the
    static config-derived metadata.
    """
    cfg = entry.config
    if entry.info is not None:
        return entry.info
    # Втори източник на info — cached IslDeviceInfo от opportunistic
    # detect (status route го попълва при първи успешен probe и го
    # persist-ва на диск). Map-ваме към ErpNet.FP-style DeviceInfo.
    isl_cache = getattr(entry, "_isl_info_cache", None)
    if isl_cache is not None:
        return DeviceInfo(
            uri=getattr(isl_cache, "uri", "") or "",
            manufacturer=getattr(isl_cache, "manufacturer", "") or "Datecs",
            model=getattr(isl_cache, "model", "") or "?",
            firmware_version=getattr(isl_cache, "firmware_version", "") or "",
            serial_number=getattr(isl_cache, "serial_number", "") or "",
            fiscal_memory_serial_number=getattr(
                isl_cache, "fiscal_memory_serial_number", "") or "",
            tax_identification_number=getattr(
                isl_cache, "tax_identification_number", "") or "",
            item_text_max_length=getattr(isl_cache, "item_text_max_length", 36),
            comment_text_max_length=getattr(isl_cache, "comment_text_max_length", 42),
            operator_password_max_length=getattr(
                isl_cache, "operator_password_max_length", 8),
            supported_payment_types=pt_adapter.supported_for(cfg.driver),
        )
    addr = cfg.port or f"{cfg.tcp_host}:{cfg.tcp_port}"
    transport_token = {"serial": "com", "tcp": "tcp"}.get(cfg.transport, "com")
    # ErpNet.FP-style URI: bg.<vendor>.<protocol>.<transport>://<addr>
    driver_to_uri = {
        "datecs.pm": "bg.dt.pm",
        "datecs.isl": "bg.dt.isl",
        "daisy.isl": "bg.dy.isl",
        "eltrade.isl": "bg.el.isl",
        "incotex.isl": "bg.is.icp",
        "tremol.isl": "bg.tr.isl",
    }
    uri_prefix = driver_to_uri.get(cfg.driver, "bg.unknown")
    uri = f"{uri_prefix}.{transport_token}://{addr}"
    driver_to_model = {
        "datecs.pm": "PM (v2.11.4)",
        "datecs.isl": "Datecs ISL (auto-detect)",
        "daisy.isl": "Daisy ISL",
        "eltrade.isl": "Eltrade ISL",
        "incotex.isl": "Incotex ISL",
        "tremol.isl": "Tremol ISL",
    }
    driver_to_manufacturer = {
        "datecs.pm": "Datecs",
        "datecs.isl": "Datecs",
        "daisy.isl": "Daisy",
        "eltrade.isl": "Eltrade",
        "incotex.isl": "Incotex",
        "tremol.isl": "Tremol",
    }
    return DeviceInfo(
        uri=uri,
        manufacturer=driver_to_manufacturer.get(cfg.driver, "Unknown"),
        model=driver_to_model.get(cfg.driver, "Unknown"),
        item_text_max_length=36,
        comment_text_max_length=42,
        operator_password_max_length=8,
        supported_payment_types=pt_adapter.supported_for(cfg.driver),
    )


# ─── 1. GET / ─────────────────────────────────────────────────────


@router.get("", response_model=dict[str, DeviceInfo])
@router.get("/", include_in_schema=False)
async def list_printers(request: Request):
    registry = _registry(request)
    return {pid: _device_info(entry) for pid, entry in registry.printers.items()}


# ─── 2. GET /{id} ─────────────────────────────────────────────────


@router.get("/{id}", response_model=DeviceInfo)
async def printer_info(id: str, request: Request):
    registry = _require_printer(request, id)
    return _device_info(registry.get(id))


# ─── 3. GET /{id}/status ──────────────────────────────────────────


@router.get("/{id}/status", response_model=DeviceStatusWithDateTime)
async def printer_status(id: str, request: Request):
    registry = _require_printer(request, id)
    is_pm = registry.is_pm(id)
    # Hard ceiling — status check must always resolve quickly so the
    # Odoo POS UI / backend buttons can react. With a paper-out or
    # otherwise unresponsive device the underlying ISL frame timeout
    # is 5s; we cap at 8s total and surface E101 on overrun rather
    # than wedging the calling browser.
    try:
        async def _do():
            async with registry.with_driver(id) as drv:
                if is_pm:
                    fs = await asyncio.to_thread(drv.read_status)
                    # Opportunistic populate of device info (serial,
                    # FM serial, firmware, TIN) — same trick as the
                    # ISL branch below. PM-only devices were missing
                    # this so /printers always reported empty serial.
                    if not fs.has_critical_error():
                        entry = registry.get(id)
                        if (getattr(entry, "_isl_info_cache", None) is None
                                and hasattr(drv, "detect")):
                            try:
                                pm_info = await asyncio.to_thread(drv.detect)
                                if pm_info:
                                    # Reuse the ISL IslDeviceInfo dataclass —
                                    # _device_info reads via getattr() so any
                                    # object exposing those names works.
                                    entry._isl_info_cache = IslDeviceInfo(
                                        manufacturer=pm_info.get(
                                            "manufacturer", "Datecs"),
                                        model=pm_info.get(
                                            "model", "PM (v2.11.4)"),
                                        firmware_version=pm_info.get(
                                            "firmware_version", ""),
                                        serial_number=pm_info.get(
                                            "serial_number", ""),
                                        fiscal_memory_serial_number=pm_info.get(
                                            "fiscal_memory_serial_number", ""),
                                        tax_identification_number=pm_info.get(
                                            "tax_identification_number", ""),
                                    )
                                    try:
                                        registry.persist_isl_info_cache()
                                    except Exception:
                                        pass
                            except Exception as exc:
                                _logger.debug(
                                    "PM opportunistic detect failed: %s", exc)
                    return DeviceStatusWithDateTime(
                        ok=not fs.has_critical_error(),
                        device_date_time=_now_iso(),
                        messages=msg_adapter.from_status(fs),
                    )
                isl_status = await asyncio.to_thread(drv.get_status)
                # Device отговаря — opportunistic populate на info
                # (FW, serial, FM serial, TIN) ако още не е cached.
                # Status обикновено успява първи (cheap), а info-то
                # после е готов за /printers и UI-то.
                if isl_status.ok:
                    entry = registry.get(id)
                    if (getattr(entry, "_isl_info_cache", None) is None
                            and hasattr(drv, "detect")):
                        try:
                            info = await asyncio.to_thread(drv.detect)
                            if info is not None:
                                entry._isl_info_cache = info
                                # Persist веднага — следващия restart на
                                # proxy-то ще започне с пълно info дори
                                # ако device е offline в момента
                                try:
                                    registry.persist_isl_info_cache()
                                except Exception:
                                    pass
                        except Exception as exc:
                            _logger.debug("opportunistic detect failed: %s", exc)
                return DeviceStatusWithDateTime(
                    ok=isl_status.ok,
                    device_date_time=_now_iso(),
                    messages=[
                        StatusMessage(type=m.type.value, code=m.code, text=m.text)
                        for m in (isl_status.messages + isl_status.errors)
                    ],
                )
        return await asyncio.wait_for(_do(), timeout=8.0)
    except asyncio.TimeoutError:
        _logger.warning("status check timed out for %s — likely paper-out / "
                        "cover-open / cable", id)
        return DeviceStatusWithDateTime(
            ok=False,
            messages=[StatusMessage(
                type="error", code="E101",
                text="Device unreachable — check paper, cover, cable",
            )],
        )
    except Exception as exc:
        _logger.exception("status check failed for %s", id)
        return DeviceStatusWithDateTime(
            ok=False, messages=[msg_adapter.from_fiscal_error(exc)]
        )


# ─── 4. GET /{id}/cash ────────────────────────────────────────────


@router.get("/{id}/cash", response_model=CashAmountResult)
async def printer_cash(id: str, request: Request):
    registry = _require_printer(request, id)
    try:
        async with registry.with_pm(id) as pm:
            safe, _ti, _to = await asyncio.to_thread(pm.read_cash_state)
        return CashAmountResult(ok=True, amount=safe)
    except Exception as exc:
        _logger.exception("cash read failed for %s", id)
        return CashAmountResult(
            ok=False, amount=0.0, messages=[msg_adapter.from_fiscal_error(exc)]
        )


# ─── 5. POST /{id}/receipt ────────────────────────────────────────


async def _isl_print_receipt(registry, id: str, receipt: Receipt) -> PrintReceiptResult:
    """ISL receipt path — uses IslDevice's open_receipt / add_item / add_payment / close."""
    from ...drivers.fiscal.datecs_isl.protocol import (
        PaymentType as IslPT,
        PriceModifierType as IslPMT,
        TaxGroup as IslTG,
    )

    payment_map = {
        "cash": IslPT.CASH,
        "card": IslPT.CARD,
        "check": IslPT.CHECK,
    }
    pmt_map = {
        PriceModifierType.discount_percent: IslPMT.DISCOUNT_PERCENT,
        PriceModifierType.discount_amount: IslPMT.DISCOUNT_AMOUNT,
        PriceModifierType.surcharge_percent: IslPMT.SURCHARGE_PERCENT,
        PriceModifierType.surcharge_amount: IslPMT.SURCHARGE_AMOUNT,
    }

    try:
        async with registry.with_driver(id) as isl:
            opened = False
            try:
                _logger.info(
                    "RECEIPT id=%s UNS=%r operator=%r items=%d total=%.2f",
                    id,
                    receipt.unique_sale_number,
                    receipt.operator or getattr(isl, "operator_id", None),
                    len(receipt.items),
                    sum((i.unit_price * i.quantity)
                        for i in receipt.items
                        if isinstance(i, SaleItem)),
                )
                st = await asyncio.to_thread(
                    isl.open_receipt,
                    receipt.unique_sale_number,
                    receipt.operator,
                    receipt.operator_password,
                )
                if not st.ok:
                    _logger.warning(
                        "ISL open_receipt failed: id=%s UNS=%r operator=%r errors=%s",
                        id, receipt.unique_sale_number, receipt.operator,
                        [(e.code, e.text) for e in st.errors],
                    )
                if not st.ok:
                    return PrintReceiptResult(
                        ok=False,
                        messages=[
                            StatusMessage(type=m.type.value, code=m.code, text=m.text)
                            for m in (st.messages + st.errors)
                        ],
                    )
                opened = True

                receipt_amount = 0.0
                for item in receipt.items:
                    if isinstance(item, SaleItem):
                        tg = IslTG(str(item.tax_group))
                        pmt = (
                            pmt_map.get(item.price_modifier_type, IslPMT.NONE)
                            if item.price_modifier_type
                            else IslPMT.NONE
                        )
                        st = await asyncio.to_thread(
                            isl.add_item,
                            text=item.text,
                            unit_price=item.unit_price,
                            tax_group=tg,
                            quantity=item.quantity,
                            department=item.department or 0,
                            price_modifier_type=pmt,
                            price_modifier_value=item.price_modifier_value or 0,
                        )
                        if not st.ok:
                            raise RuntimeError(
                                "; ".join(e.text for e in st.errors)
                            )
                        receipt_amount += float(item.quantity) * float(item.unit_price)

                if not receipt.payments:
                    st = await asyncio.to_thread(isl.full_payment)
                else:
                    for pay in receipt.payments:
                        st = await asyncio.to_thread(
                            isl.add_payment,
                            pay.amount,
                            payment_map.get(pay.payment_type.value, IslPT.CASH),
                        )
                        if not st.ok:
                            raise RuntimeError(
                                "; ".join(e.text for e in st.errors)
                            )
                    st = await asyncio.to_thread(isl.close_receipt)

                if not st.ok:
                    raise RuntimeError("; ".join(e.text for e in st.errors))

                return PrintReceiptResult(
                    ok=True,
                    receipt_date_time=_now_iso(),
                    receipt_amount=round(receipt_amount, 2),
                    fiscal_memory_serial_number=isl.info.fiscal_memory_serial_number or "",
                )
            except Exception:
                if opened:
                    try:
                        await asyncio.to_thread(isl.abort_receipt)
                    except Exception:
                        _logger.exception("ISL abort after error failed")
                raise
    except Exception as exc:
        _logger.exception("ISL receipt print failed on %s", id)
        return PrintReceiptResult(ok=False, messages=[msg_adapter.from_fiscal_error(exc)])


async def _print_receipt_impl(
    request: Request, id: str, receipt: Receipt, *, is_reversal: bool = False
) -> PrintReceiptResult:
    registry = _require_printer(request, id)
    cfg = registry.get(id).config

    if registry.is_isl(id):
        return await _isl_print_receipt(registry, id, receipt)

    try:
        async with registry.with_pm(id) as pm:
            opened = False
            try:
                slip_number = await asyncio.to_thread(
                    pm.open_fiscal_receipt,
                    nsale=receipt.unique_sale_number,
                    invoice=False,
                    op_code=int(receipt.operator) if receipt.operator else None,
                    op_password=receipt.operator_password,
                )
                opened = True

                receipt_amount = 0.0

                for item in receipt.items:
                    if isinstance(item, SaleItem):
                        await asyncio.to_thread(
                            pm.register_sale,
                            text=item.text[:cfg.extras.get("item_text_max_length", 36)],
                            price=item.unit_price,
                            quantity=item.quantity,
                            vat_group=tg_adapter.to_letter(item.tax_group, cfg.driver),
                            discount_percent=(
                                item.price_modifier_value
                                if item.price_modifier_type
                                == PriceModifierType.discount_percent
                                else None
                            ),
                            department=item.department,
                        )
                        receipt_amount += item.quantity * item.unit_price
                    elif isinstance(item, SubtotalAmountItem):
                        # Subtotal-level amount modifier — Datecs PM doesn't
                        # have a direct cmd; we'd emit cmd 0x33 subtotal then
                        # adjust — TODO for Phase 2.
                        _logger.warning(
                            "subtotal %s not yet supported on Datecs PM", item.type
                        )
                    # comment / footer-comment — TODO via cmd 0x36 (free text)

                await asyncio.to_thread(pm.subtotal, print_subtotal=False)

                if not receipt.payments:
                    await asyncio.to_thread(pm.payment_total, payment_type=0)
                else:
                    for pay in receipt.payments:
                        await asyncio.to_thread(
                            pm.payment_total,
                            payment_type=pt_adapter.to_code(
                                pay.payment_type, cfg.driver
                            ),
                            amount=pay.amount,
                        )

                closed_slip = await asyncio.to_thread(pm.close_fiscal_receipt)
            except Exception:
                if opened:
                    try:
                        await asyncio.to_thread(pm.cancel_fiscal_receipt)
                    except Exception:
                        _logger.exception("Cancel after partial print failed")
                raise

            return PrintReceiptResult(
                ok=True,
                receipt_number=str(closed_slip or slip_number),
                receipt_date_time=_now_iso(),
                receipt_amount=round(receipt_amount, 2),
                fiscal_memory_serial_number=registry.get(id).info.fiscal_memory_serial_number
                if registry.get(id).info
                else "",
            )
    except FiscalError as exc:
        _logger.warning("Fiscal error printing on %s: %s", id, exc)
        return PrintReceiptResult(ok=False, messages=[msg_adapter.from_fiscal_error(exc)])
    except Exception as exc:
        _logger.exception("Receipt print failed on %s", id)
        return PrintReceiptResult(ok=False, messages=[msg_adapter.from_fiscal_error(exc)])


@router.post("/{id}/receipt", response_model=PrintReceiptResult)
async def print_receipt(
    id: str,
    receipt: Receipt,
    request: Request,
    taskId: Annotated[str | None, Query()] = None,
    timeout: Annotated[str | None, Query()] = None,
    asyncTimeout: Annotated[int, Query()] = 30000,
):
    return await _print_receipt_impl(request, id, receipt)


# ─── 5b. POST /{id}/invoice ───────────────────────────────────────


async def _isl_print_invoice(registry, id: str, inv: Invoice) -> PrintReceiptResult:
    """ISL invoice path — native (FW 3.00+) OR free-text fallback."""
    from ...drivers.fiscal.datecs_isl.protocol import (
        PaymentType as IslPT,
        TaxGroup as IslTG,
    )
    payment_map = {"cash": IslPT.CASH, "card": IslPT.CARD, "check": IslPT.CHECK}

    try:
        async with registry.with_driver(id) as isl:
            opened = False
            try:
                native = bool(isl.info.supports_native_invoice)
                _logger.info(
                    "INVOICE id=%s UNS=%r native=%s recipient=%r EIK=%s items=%d",
                    id, inv.unique_sale_number, native,
                    inv.customer_name, inv.customer_eik, len(inv.items),
                )

                if native:
                    st = await asyncio.to_thread(
                        isl.open_invoice_receipt,
                        unique_sale_number=inv.unique_sale_number,
                        recipient_name=inv.customer_name,
                        recipient_eik=inv.customer_eik,
                        recipient_eik_type=inv.customer_eik_type,
                        recipient_address=inv.customer_address,
                        recipient_buyer=inv.customer_buyer,
                        recipient_vat=inv.customer_vat,
                        invoice_number=inv.invoice_number,
                        operator_id=inv.operator,
                        operator_password=inv.operator_password,
                    )
                    # Native invoice failed (firmware variant mismatch?) —
                    # try the fallback path automatically. The capability
                    # flag is best-effort; some FW 3.00+ devices use a
                    # slightly different invoice payload that we don't
                    # cover yet (e.g. requires explicit invoice_number,
                    # different EIK-type encoding, or vendor-locked).
                    if not st.ok and any(
                            "E401" in (e.code or "") for e in st.errors):
                        _logger.warning(
                            "Native invoice rejected (E401) — "
                            "aborting any half-opened receipt and "
                            "falling back to free-text comment header. "
                            "Errors: %s",
                            [(e.code, e.text) for e in st.errors],
                        )
                        # Abort BEFORE retry — the failed native invoice
                        # may have left the device half-way through the
                        # OPEN sequence (some firmwares start printing
                        # the invoice header before validating the full
                        # payload). Without abort we'd see two receipts
                        # come out: a partially-printed invoice header
                        # plus the fallback fiscal receipt.
                        try:
                            await asyncio.to_thread(isl.abort_receipt)
                        except Exception:
                            _logger.debug("abort_receipt before fallback raised — likely no open receipt, continuing")
                        native = False
                        st = await asyncio.to_thread(
                            isl.open_receipt,
                            inv.unique_sale_number,
                            inv.operator,
                            inv.operator_password,
                        )
                else:
                    # Fallback: open normal fiscal receipt, then prefix
                    # with comment lines (CMD_FISCAL_RECEIPT_COMMENT).
                    st = await asyncio.to_thread(
                        isl.open_receipt,
                        inv.unique_sale_number,
                        inv.operator,
                        inv.operator_password,
                    )

                if not st.ok:
                    return PrintReceiptResult(
                        ok=False,
                        messages=[
                            StatusMessage(type=m.type.value, code=m.code, text=m.text)
                            for m in (st.messages + st.errors)
                        ],
                    )
                opened = True

                # In fallback mode, inject invoice header as comments.
                if not native:
                    invoice_header_lines = [
                        "===== ФАКТУРА =====",
                        f"Купувач: {inv.customer_name}"[:46],
                        f"ЕИК: {inv.customer_eik}"[:46],
                    ]
                    if inv.customer_vat:
                        invoice_header_lines.append(f"ИН по ЗДДС: {inv.customer_vat}"[:46])
                    if inv.customer_address:
                        invoice_header_lines.append(f"Адрес: {inv.customer_address}"[:46])
                    if inv.customer_buyer:
                        invoice_header_lines.append(f"МОЛ: {inv.customer_buyer}"[:46])
                    invoice_header_lines.append("=" * 30)
                    for line in invoice_header_lines:
                        cst = await asyncio.to_thread(isl.add_comment, line)
                        if not cst.ok:
                            _logger.warning("comment line failed: %s",
                                            [(e.code, e.text) for e in cst.errors])

                # Items + payments — same as regular receipt
                receipt_amount = 0.0
                for item in inv.items:
                    if isinstance(item, SaleItem):
                        tg = IslTG(str(item.tax_group))
                        st = await asyncio.to_thread(
                            isl.add_item,
                            text=item.text,
                            unit_price=item.unit_price,
                            tax_group=tg,
                            quantity=item.quantity,
                        )
                        if not st.ok:
                            raise RuntimeError("; ".join(e.text for e in st.errors))
                        receipt_amount += float(item.quantity) * float(item.unit_price)

                if not inv.payments:
                    st = await asyncio.to_thread(isl.full_payment)
                else:
                    for pay in inv.payments:
                        st = await asyncio.to_thread(
                            isl.add_payment,
                            pay.amount,
                            payment_map.get(pay.payment_type.value, IslPT.CASH),
                        )
                        if not st.ok:
                            raise RuntimeError("; ".join(e.text for e in st.errors))

                st = await asyncio.to_thread(isl.close_receipt)
                if not st.ok:
                    raise RuntimeError("; ".join(e.text for e in st.errors))

                return PrintReceiptResult(
                    ok=True,
                    receipt_date_time=_now_iso(),
                    receipt_amount=round(receipt_amount, 2),
                    fiscal_memory_serial_number=isl.info.fiscal_memory_serial_number or "",
                )
            except Exception:
                if opened:
                    try:
                        await asyncio.to_thread(isl.abort_receipt)
                    except Exception:
                        _logger.exception("ISL abort after invoice error failed")
                raise
    except Exception as exc:
        _logger.exception("ISL invoice print failed on %s", id)
        return PrintReceiptResult(ok=False, messages=[msg_adapter.from_fiscal_error(exc)])


@router.post("/{id}/invoice", response_model=PrintReceiptResult)
async def print_invoice(
    id: str,
    invoice: Invoice,
    request: Request,
):
    """Print a fiscal invoice (фактура).

    On firmware that supports native invoice opcode (Datecs ISL FW 3.00+):
      header includes recipient + EIK + address + МОЛ + ИН по ЗДДС;
      device assigns invoice number from EEPROM auto-increment.
    On older firmware: regular fiscal receipt prefixed with comment
      lines containing the same data — visually similar but NOT a
      Naredba H-18 fiscal invoice.
    """
    registry = _require_printer(request, id)
    if registry.is_isl(id):
        return await _isl_print_invoice(registry, id, invoice)
    return PrintReceiptResult(
        ok=False,
        messages=[StatusMessage(
            type="error", code="E501",
            text="Invoice not yet implemented on Datecs PM driver",
        )],
    )


# ─── 6. POST /{id}/reversalreceipt ────────────────────────────────


@router.post("/{id}/reversalreceipt", response_model=PrintReceiptResult)
async def print_reversal(
    id: str,
    reversal: ReversalReceipt,
    request: Request,
    taskId: Annotated[str | None, Query()] = None,
    timeout: Annotated[str | None, Query()] = None,
    asyncTimeout: Annotated[int, Query()] = 30000,
):
    # cmd 0x2B (open storno document) is not yet exposed in pm_v2_11_4 —
    # Phase 3 backlog. Return a structured error for now.
    return PrintReceiptResult(
        ok=False,
        messages=[
            StatusMessage(
                type=MessageType.error,
                code="E_NOT_IMPLEMENTED",
                text="Storno (cmd 0x2B) not yet implemented in Datecs PM facade",
            )
        ],
    )


# ─── 7. POST /{id}/withdraw ───────────────────────────────────────


async def _dispatch_simple(
    registry, id: str, pm_method: str, isl_method: str, *args
) -> GenericResult:
    """Generic dispatcher for endpoints that just call a one-shot method
    on either driver and return ok/messages.
    """
    is_pm = registry.is_pm(id)
    try:
        async with registry.with_driver(id) as drv:
            if is_pm:
                await asyncio.to_thread(getattr(drv, pm_method), *args)
                return GenericResult(ok=True)
            # datecs.isl
            isl_status = await asyncio.to_thread(getattr(drv, isl_method), *args)
            messages = [
                StatusMessage(type=m.type.value, code=m.code, text=m.text)
                for m in (isl_status.messages + isl_status.errors)
            ]
            return GenericResult(ok=isl_status.ok, messages=messages)
    except Exception as exc:
        return GenericResult(ok=False, messages=[msg_adapter.from_fiscal_error(exc)])


@router.post("/{id}/withdraw", response_model=GenericResult)
async def print_withdraw(
    id: str,
    body: TransferAmount,
    request: Request,
    asyncTimeout: Annotated[int, Query()] = 30000,
):
    registry = _require_printer(request, id)
    return await _dispatch_simple(registry, id, "cash_out", "cash_out", body.amount)


# ─── 8. POST /{id}/deposit ────────────────────────────────────────


@router.post("/{id}/deposit", response_model=GenericResult)
async def print_deposit(
    id: str,
    body: TransferAmount,
    request: Request,
    asyncTimeout: Annotated[int, Query()] = 30000,
):
    registry = _require_printer(request, id)
    return await _dispatch_simple(registry, id, "cash_in", "cash_in", body.amount)


# ─── 9. POST /{id}/datetime ───────────────────────────────────────


@router.post("/{id}/datetime", response_model=GenericResult)
async def set_datetime(
    id: str,
    body: dict,
    request: Request,
):
    # cmd 0x3D (set date/time) not yet in facade — Phase 3.
    return GenericResult(
        ok=False,
        messages=[
            StatusMessage(
                type=MessageType.error,
                code="E_NOT_IMPLEMENTED",
                text="Set datetime (cmd 0x3D) not yet implemented",
            )
        ],
    )


# ─── 10. POST /{id}/zreport ───────────────────────────────────────


@router.post("/{id}/zreport", response_model=GenericResult)
async def print_z_report(
    id: str,
    request: Request,
    asyncTimeout: Annotated[int, Query()] = 60000,
):
    registry = _require_printer(request, id)
    return await _dispatch_simple(registry, id, "print_z_report", "print_z_report")


# ─── 11. POST /{id}/xreport ───────────────────────────────────────


@router.post("/{id}/xreport", response_model=GenericResult)
async def print_x_report(
    id: str,
    request: Request,
    asyncTimeout: Annotated[int, Query()] = 60000,
):
    registry = _require_printer(request, id)
    return await _dispatch_simple(registry, id, "print_x_report", "print_x_report")


# ─── 12. POST /{id}/duplicate ─────────────────────────────────────


@router.post("/{id}/duplicate", response_model=GenericResult)
async def print_duplicate(
    id: str,
    request: Request,
    asyncTimeout: Annotated[int, Query()] = 30000,
):
    registry = _require_printer(request, id)
    return await _dispatch_simple(registry, id, "print_duplicate", "print_duplicate")


# ─── 13. POST /{id}/reset ─────────────────────────────────────────


@router.post("/{id}/reset", response_model=GenericResult)
async def reset_printer(id: str, request: Request):
    """ErpNet.FP "reset" cancels a stuck open receipt. Both PM (cmd 0x3C)
    and ISL (CMD_ABORT_FISCAL_RECEIPT 0x3C) treat this as idempotent.
    """
    registry = _require_printer(request, id)
    is_pm = registry.is_pm(id)
    try:
        async with registry.with_driver(id) as drv:
            if is_pm:
                try:
                    await asyncio.to_thread(drv.cancel_fiscal_receipt)
                except FiscalError:
                    pass  # no receipt open is fine
            else:  # datecs.isl
                await asyncio.to_thread(drv.abort_receipt)
        return GenericResult(ok=True)
    except Exception as exc:
        return GenericResult(ok=False, messages=[msg_adapter.from_fiscal_error(exc)])


# ─── 14. GET / POST /{id}/vat-rates ───────────────────────────────


@router.get("/{id}/vat-rates")
async def get_vat_rates(id: str, request: Request):
    """Read currently programmed VAT rates from the device.

    Response shape: `{ok, rates: {А: 2000, Б: 900, В: 0, Г: null, ...},
                     decimal_point: 2}`.
    Each rate is an integer × 100 (so 2000 = 20.00%); `null` means
    the slot is disabled.

    PM-only for now; ISL devices use a different rate-read path
    (cmd 0x21 sub 'I' on most variants) — TODO when first ISL request
    lands.
    """
    registry = _require_printer(request, id)
    if not registry.is_pm(id):
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="VAT-rate reading is currently only implemented "
                   "for the Datecs PM driver.",
        )
    try:
        async with registry.with_driver(id) as drv:
            rates = await asyncio.to_thread(drv.read_vat_rates)
        return {
            "ok": True,
            "rates": {k: v for k, v in rates.items() if k != "decimal_point"},
            "decimal_point": rates.get("decimal_point", 2),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


@router.post("/{id}/vat-rates")
async def set_vat_rates(id: str, body: dict, request: Request):
    """Program VAT rates on the device (cmd 0x53 'P').

    Request body: `{rates: {"А": 2000, "Б": 900, "В": 0, "Г": null, ...},
                   decimal_point: 2}` — same shape as GET response.

    Cyrillic letters are preferred; Latin shortcuts A-H are auto-
    translated to А-З. Rate values are integer × 100. Use `null` to
    disable a slot.

    FISCAL CAVEAT: Bulgarian devices typically reject VAT changes
    unless a Z-report has zeroed the daily totals first; some changes
    require service-mode unlock. The proxy passes through the device's
    error code unchanged.
    """
    registry = _require_printer(request, id)
    if not registry.is_pm(id):
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="VAT-rate programming is currently only implemented "
                   "for the Datecs PM driver.",
        )
    rates = body.get("rates") or {}
    if not isinstance(rates, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`rates` must be a dict of {letter: int×100 | null}",
        )
    decimal_point = int(body.get("decimal_point", 2))
    try:
        async with registry.with_driver(id) as drv:
            await asyncio.to_thread(drv.program_vat_rates, rates, decimal_point)
            new_rates = await asyncio.to_thread(drv.read_vat_rates)
        return {
            "ok": True,
            "rates": {k: v for k, v in new_rates.items() if k != "decimal_point"},
            "decimal_point": new_rates.get("decimal_point", 2),
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:300]}


# ─── 15. POST /{id}/rawrequest ────────────────────────────────────


@router.post("/{id}/rawrequest", response_model=GenericResult)
async def raw_request(id: str, frame_body: RequestFrame, request: Request):
    # Raw command pass-through — unsafe, but ErpNet.FP exposes it for
    # diagnostics. Phase 3 backlog (needs careful framing on PM side).
    return GenericResult(
        ok=False,
        messages=[
            StatusMessage(
                type=MessageType.error,
                code="E_NOT_IMPLEMENTED",
                text="Raw request not yet supported",
            )
        ],
    )
