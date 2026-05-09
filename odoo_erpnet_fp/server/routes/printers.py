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


# ─── 10b. POST /{id}/zreport-totals ───────────────────────────────


@router.post("/{id}/zreport-totals")
async def print_z_report_with_totals(
    id: str,
    request: Request,
    asyncTimeout: Annotated[int, Query()] = 120000,
):
    """Print Z-report AND return parsed totals when the driver supports it.

    Some drivers (Datecs PM v2.11.4) return `(report_number, dict[group, turnover])`
    from `print_z_report()`; others (Datecs ISL) only flip device status. The
    response shape adapts:

        Driver returns tuple    → {ok, report_number, totals_by_group, device_returned_totals: true}
        Driver returns DeviceStatus → {ok, device_returned_totals: false, status: ...}

    Designed for Odoo's pos.session close-shift hook: gives the backend a
    real reconcile baseline against pos.order aggregates when the device
    cooperates, and a clean "totals unavailable" signal when it doesn't.
    Caller should always have its own Odoo-side aggregate as fallback.
    """
    registry = _require_printer(request, id)
    entry = registry.get(id)
    try:
        async with entry.lock:
            with entry.opened() as driver:
                result = driver.print_z_report()
        # PM-style return: (report_number, dict)
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], dict):
            report_number, totals = result
            return {
                "ok": True,
                "report_number": int(report_number),
                "totals_by_group": {k: float(v) for k, v in totals.items()},
                "device_returned_totals": True,
            }
        # ISL-style return: DeviceStatus (just an ack — no totals)
        ok = bool(getattr(result, "ok", False))
        messages = [str(m) for m in getattr(result, "messages", []) or []]
        return {
            "ok": ok,
            "report_number": None,
            "totals_by_group": {},
            "device_returned_totals": False,
            "messages": messages,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "report_number": None,
            "totals_by_group": {},
            "device_returned_totals": False,
            "messages": [str(exc)],
        }


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


# ─── 16. POST /{id}/plu/sync — bulk PLU programming ───────────────


@router.post("/{id}/plu/sync")
async def sync_plu_bulk(id: str, body: dict, request: Request):
    """Bulk-program PLUs on the device (Datecs PM only for now).

    Request body: `{items: [{plu, name, price, vat_group, department,
                             barcode?, currency?, measurement_unit?}, ...]}`

    Iterates items in submission order; per-item failures don't abort
    the batch — Odoo client marks each PLU's push_state from the
    aggregate result.

    Returns: `{ok, programmed: int, errors: [{plu, error}, ...]}`.

    ISL devices currently return 501 — TODO when ISL PLU programming
    command (typically 0x6F) is added to the ISL driver.
    """
    registry = _require_printer(request, id)
    if not registry.is_pm(id):
        raise HTTPException(
            status_code=status.HTTP_501_NOT_IMPLEMENTED,
            detail="PLU programming is currently only implemented for "
                   "the Datecs PM driver.",
        )
    items = body.get("items") or []
    if not isinstance(items, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`items` must be a list",
        )
    programmed = 0
    errors: list[dict] = []
    try:
        async with registry.with_driver(id) as drv:
            for item in items:
                try:
                    await asyncio.to_thread(
                        drv.program_plu,
                        plu_number=int(item["plu"]),
                        name=str(item.get("name", ""))[:72],
                        price=float(item.get("price", 0.0)),
                        vat_group=str(item.get("vat_group", "Б"))[:1],
                        department=int(item.get("department", 0)),
                        barcodes=tuple(
                            b for b in (
                                item.get("barcode"),
                                *(item.get("barcodes") or []),
                            ) if b
                        )[:4],
                        measurement_unit=int(
                            item.get("measurement_unit", 0)
                        ),
                    )
                    programmed += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append({
                        "plu": item.get("plu"),
                        "error": str(exc)[:200],
                    })
        return {"ok": not errors, "programmed": programmed, "errors": errors}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "programmed": programmed,
                "errors": errors + [{"plu": None, "error": str(exc)[:300]}]}


# ─── 17. POST /{id}/operators — program cashier operators ────────


@router.post("/{id}/operators")
async def program_operators(id: str, body: dict, request: Request):
    """Program cashier operator codes & passwords on the device.

    Request body: `{operators: [{code: str, name: str, password: str},
                                ...]}`

    PM driver: cmd 0x66 'P'. ISL: 0x65 (different framing). Both will
    be wired up as the drivers gain a `program_operator` helper —
    until then we return 501 for missing capability.
    """
    registry = _require_printer(request, id)
    operators = body.get("operators") or []
    if not isinstance(operators, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`operators` must be a list",
        )
    try:
        async with registry.with_driver(id) as drv:
            program_op = getattr(drv, "program_operator", None)
            if program_op is None:
                raise HTTPException(
                    status_code=status.HTTP_501_NOT_IMPLEMENTED,
                    detail=("Driver %s has no `program_operator` method "
                            "yet." % type(drv).__name__),
                )
            programmed = 0
            errors: list[dict] = []
            for op in operators:
                try:
                    await asyncio.to_thread(
                        program_op,
                        code=str(op.get("code", ""))[:8],
                        name=str(op.get("name", ""))[:24],
                        password=str(op.get("password", ""))[:8],
                    )
                    programmed += 1
                except Exception as exc:  # noqa: BLE001
                    errors.append({
                        "code": op.get("code"),
                        "error": str(exc)[:200],
                    })
            return {"ok": not errors, "programmed": programmed,
                    "errors": errors}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}


# ─── 18. POST /{id}/logo — upload customer logo ─────────────────


@router.post("/{id}/logo")
async def upload_logo(id: str, body: dict, request: Request):
    """Upload base64-encoded image as the device's customer logo.

    Request body: `{image_b64: str}` — PNG or BMP, device-specific
    size constraints (Datecs PM accepts up to 384×128 monochrome).

    Both driver families need a `program_logo(image_bytes)` helper;
    ISL family doesn't have one yet — returns 501 there.
    """
    registry = _require_printer(request, id)
    image_b64 = body.get("image_b64") or ""
    if not image_b64:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`image_b64` required (base64-encoded image)",
        )
    import base64
    try:
        image_bytes = base64.b64decode(image_b64)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`image_b64` is not valid base64",
        )
    try:
        async with registry.with_driver(id) as drv:
            program_logo = getattr(drv, "program_logo", None)
            if program_logo is None:
                raise HTTPException(
                    status_code=status.HTTP_501_NOT_IMPLEMENTED,
                    detail=("Driver %s has no `program_logo` method "
                            "yet." % type(drv).__name__),
                )
            await asyncio.to_thread(program_logo, image_bytes)
            return {"ok": True, "bytes": len(image_bytes)}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}


# ─── 19. POST /{id}/template — header/footer text lines ─────────


@router.post("/{id}/template")
async def program_template_endpoint(id: str, body: dict, request: Request):
    """Program the receipt header and footer text lines.

    Request body: `{header: [str, ...], footer: [str, ...]}` — up to
    10 lines each (device-specific limits apply).
    """
    registry = _require_printer(request, id)
    header = body.get("header") or []
    footer = body.get("footer") or []
    if not isinstance(header, list) or not isinstance(footer, list):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="`header` and `footer` must be lists of strings",
        )
    try:
        async with registry.with_driver(id) as drv:
            program_template = getattr(drv, "program_template", None)
            if program_template is None:
                raise HTTPException(
                    status_code=status.HTTP_501_NOT_IMPLEMENTED,
                    detail=("Driver %s has no `program_template` method "
                            "yet." % type(drv).__name__),
                )
            await asyncio.to_thread(
                program_template,
                header=[str(l)[:32] for l in header[:10]],
                footer=[str(l)[:32] for l in footer[:10]],
            )
            return {"ok": True,
                    "header_lines": len(header),
                    "footer_lines": len(footer)}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300]}


# ─── 20. GET /{id}/journal — pull electronic journal (КЛЕН) ─────


@router.get("/{id}/journal")
async def get_journal(
    id: str, request: Request,
    fromDate: Annotated[str | None, Query()] = None,
    toDate: Annotated[str | None, Query()] = None,
):
    """Pull receipts from the device's electronic journal (КЛЕН).

    Query: `?fromDate=ISO8601&toDate=ISO8601` (both optional; default
    is "since last Z" on most devices).

    Response: `{ok, receipts: [{number, datetime, operator, items[],
                                payments[], total}, ...]}`

    Used by `pos.session.action_pos_session_closing_control` in
    external-POS mode to import the day's receipts back into Odoo.
    """
    registry = _require_printer(request, id)
    try:
        async with registry.with_driver(id) as drv:
            read_journal = getattr(drv, "read_journal", None)
            if read_journal is None:
                raise HTTPException(
                    status_code=status.HTTP_501_NOT_IMPLEMENTED,
                    detail=("Driver %s has no `read_journal` method "
                            "yet." % type(drv).__name__),
                )
            data = await asyncio.to_thread(
                read_journal, from_date=fromDate, to_date=toDate,
            )
            return {"ok": True, "receipts": data or []}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)[:300], "receipts": []}
