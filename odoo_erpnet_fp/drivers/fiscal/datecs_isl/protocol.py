"""
High-level Datecs ISL fiscal-printer driver.

Wraps the bare frame layer (`frame.py`) with a stateful command-level
API: open/close fiscal receipt, add item, add payment, X/Z reports,
auto-detection of the Datecs sub-protocol (P/C, X, FP, FMP v2).

Threading: instances are NOT thread-safe; the Odoo.ErpNet.FP server
serialises access via per-printer asyncio.Lock.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from . import commands as cmd
from . import frame as fr
from .status import DeviceStatus, parse_status_bytes
from .transport import Transport, TransportError, TransportTimeout

_logger = logging.getLogger(__name__)


# ─── Public enums (mirror IslFiscalPrinterBase) ──────────────────


class TaxGroup(str, Enum):
    """8 VAT slots A..H (or А..З Cyrillic) — matches ErpNet.FP taxGroup 1..8."""

    G1 = "1"
    G2 = "2"
    G3 = "3"
    G4 = "4"
    G5 = "5"
    G6 = "6"
    G7 = "7"
    G8 = "8"


class PriceModifierType(str, Enum):
    NONE = "None"
    DISCOUNT_PERCENT = "DiscountPercent"
    DISCOUNT_AMOUNT = "DiscountAmount"
    SURCHARGE_PERCENT = "SurchargePercent"
    SURCHARGE_AMOUNT = "SurchargeAmount"


class PaymentType(str, Enum):
    CASH = "cash"
    CARD = "card"
    CHECK = "check"
    RESERVED1 = "reserved1"


class ReversalReason(str, Enum):
    OPERATOR_ERROR = "operator_error"
    REFUND = "refund"
    TAX_BASE_REDUCTION = "tax_base_reduction"


@dataclass
class IslDeviceInfo:
    """Static device info populated after `detect()`."""

    manufacturer: str = ""
    model: str = ""
    firmware_version: str = ""
    serial_number: str = ""
    fiscal_memory_serial_number: str = ""
    protocol: str = ""  # 'datecs.p.isl' / 'datecs.x.isl' / 'datecs.fp.isl' / 'datecs.fmp.isl'
    item_text_max_length: int = 34
    comment_text_max_length: int = 46
    operator_password_max_length: int = 8
    tax_identification_number: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "manufacturer": self.manufacturer,
            "model": self.model,
            "firmwareVersion": self.firmware_version,
            "serialNumber": self.serial_number,
            "fiscalMemorySerialNumber": self.fiscal_memory_serial_number,
            "protocol": self.protocol,
            "itemTextMaxLength": self.item_text_max_length,
            "commentTextMaxLength": self.comment_text_max_length,
            "operatorPasswordMaxLength": self.operator_password_max_length,
            "taxIdentificationNumber": self.tax_identification_number,
        }


# ─── Tax group / payment type mapping (Datecs ISL default) ──────
#
# Datecs ISL uses Cyrillic А..З for VAT slots and P/C/N/D for payments.
# Other vendors override these via class attributes on `IslDevice`
# subclasses — see `vendors.py`.


_REVERSAL_CODES = {
    ReversalReason.OPERATOR_ERROR: "1",
    ReversalReason.REFUND: "0",
    ReversalReason.TAX_BASE_REDUCTION: "2",
}


# ─── Auto-detection (4 sub-protocol parsers) ─────────────────────


def _parse_pc_info(data_bytes: bytes) -> Optional[IslDeviceInfo]:
    """Datecs P/C: 6 comma-separated fields (DP-25/DP-05/WP-50/DP-35)."""
    text = data_bytes.decode("cp1251", errors="ignore")
    fields = text.split(",")
    if len(fields) < 6:
        return None
    return IslDeviceInfo(
        manufacturer="Datecs",
        model=fields[0].strip(),
        firmware_version=fields[1].strip(),
        serial_number=fields[4].strip(),
        fiscal_memory_serial_number=fields[5].strip(),
        protocol="datecs.p.isl",
    )


def _parse_x_info(data_bytes: bytes) -> Optional[IslDeviceInfo]:
    """Datecs X: 8 TAB-separated fields (FP-700X / WP-500X / **DP-150X**)."""
    text = data_bytes.decode("cp1251", errors="ignore")
    fields = text.split("\t")
    if len(fields) < 8:
        return None
    return IslDeviceInfo(
        manufacturer="Datecs",
        model=fields[0].strip(),
        firmware_version=f"{fields[1].strip()} {fields[2].strip()} {fields[3].strip()}".strip(),
        serial_number=fields[6].strip(),
        fiscal_memory_serial_number=fields[7].strip(),
        protocol="datecs.x.isl",
    )


def _parse_fp_info(data_bytes: bytes) -> Optional[IslDeviceInfo]:
    """Datecs FP: comma-separated, 3+ fields (FP-800/FP-2000/FP-650)."""
    text = data_bytes.decode("cp1251", errors="ignore")
    fields = text.split(",")
    if len(fields) < 3:
        return None
    return IslDeviceInfo(
        manufacturer="Datecs",
        model=fields[0].strip(),
        firmware_version=fields[1].strip(),
        serial_number=fields[2].strip() if len(fields) > 2 else "",
        fiscal_memory_serial_number=fields[-1].strip() if fields else "",
        protocol="datecs.fp.isl",
        comment_text_max_length=70,
        item_text_max_length=72,
    )


# Detection chain: try parsers in order; the first that yields a
# DeviceInfo with a populated `model` wins. X (TAB-separated) is most
# distinctive so check it first.
_DETECTION_PARSERS = [
    _parse_x_info,
    _parse_pc_info,
    _parse_fp_info,
]


# ─── Default baudrate probe order ────────────────────────────────


DEFAULT_BAUDRATES = [115200, 57600, 38400, 19200, 9600]


# ─── Driver ──────────────────────────────────────────────────────


class IslDevice:
    """High-level ISL fiscal-printer driver.

    Default mappings target Datecs ISL (Cyrillic А..З + P/C/N/D). Other
    vendors override `_TAX_LETTERS` / `_PAYMENT_LETTERS` via subclasses
    in `vendors.py`.

    `transport.open()` is called by `IslDevice.open()`. Commands are
    sent via `_isl_request(cmd, data)` which handles SEQ increment,
    BCC, and NAK/SYN retries.
    """

    # Vendor-specific letter mappings — subclasses override.
    _TAX_LETTERS: dict[TaxGroup, str] = {
        TaxGroup.G1: "А",
        TaxGroup.G2: "Б",
        TaxGroup.G3: "В",
        TaxGroup.G4: "Г",
        TaxGroup.G5: "Д",
        TaxGroup.G6: "Е",
        TaxGroup.G7: "Ж",
        TaxGroup.G8: "З",
    }
    _PAYMENT_LETTERS: dict[PaymentType, str] = {
        PaymentType.CASH: "P",
        PaymentType.CARD: "C",
        PaymentType.CHECK: "N",
        PaymentType.RESERVED1: "D",
    }

    def __init__(
        self,
        transport: Transport,
        operator_id: str = "1",
        operator_password: str = "0000",
        admin_id: str = "20",
        admin_password: str = "9999",
    ) -> None:
        self._t = transport
        self._seq = 0
        self.info = IslDeviceInfo()
        self.operator_id = operator_id
        self.operator_password = operator_password
        self.admin_id = admin_id
        self.admin_password = admin_password

    def tax_group_letter(self, tg: TaxGroup) -> str:
        if tg not in self._TAX_LETTERS:
            raise ValueError(f"VAT group {tg} not supported by {type(self).__name__}")
        return self._TAX_LETTERS[tg]

    def payment_type_letter(self, pt: PaymentType) -> str:
        if pt not in self._PAYMENT_LETTERS:
            raise ValueError(f"Payment type {pt} not supported by {type(self).__name__}")
        return self._PAYMENT_LETTERS[pt]

    # ─── connection lifecycle ────────────────────────────────

    def open(self) -> None:
        if not self._t.is_open():
            self._t.open()

    def close(self) -> None:
        self._t.close()

    def __enter__(self) -> "IslDevice":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ─── low-level exchange ──────────────────────────────────

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = 0 if self._seq >= fr.MAX_SEQUENCE_NUMBER else self._seq + 1
        return seq

    def _isl_request(
        self, command: int, data: str = "", timeout: float = 5.0
    ) -> Tuple[str, DeviceStatus, bytes]:
        """Send one ISL command, return (response_text, status, raw_status_bytes).

        Retries on NAK; waits through SYN (slave still working).
        """
        encoded = data.encode("cp1251") if data else b""
        seq = self._next_seq()
        request = fr.encode_request(seq, command, encoded)
        _logger.debug(
            "ISL >>> cmd=0x%02X seq=%d data=%r raw=%s",
            command, seq, data, request.hex(" "),
        )

        last_exc: Exception | None = None
        for _ in range(fr.MAX_WRITE_RETRIES):
            try:
                self._t.write(request)
                raw = self._read_one_frame(timeout)
            except (fr.FrameError, TransportTimeout) as exc:
                last_exc = exc
                continue
            try:
                data_bytes, status_bytes = fr.parse_response(raw)
            except fr.FrameError as exc:
                last_exc = exc
                continue
            text = data_bytes.decode("cp1251", errors="ignore")
            status = parse_status_bytes(status_bytes)
            log_fn = _logger.warning if status.errors else _logger.debug
            log_fn(
                "ISL <<< cmd=0x%02X seq=%d data=%r status_bytes=%s errors=%s",
                command, seq, text, status_bytes.hex(" "),
                [(e.code, e.text) for e in status.errors],
            )
            return text, status, bytes(status_bytes)

        status = DeviceStatus()
        status.add_error("E101", f"Device unreachable: {last_exc}")
        return "", status, b""

    def _read_one_frame(self, timeout: float) -> bytes:
        """Collect bytes until ETX, dispatching NAK / SYN inline."""
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self._t.read(fr.DEFAULT_READ_BUF, max(0.0, deadline - time.monotonic()))
            if not chunk:
                continue
            for b in chunk:
                if not buf:
                    if b == fr.NAK:
                        raise fr.ChecksumError("Slave sent NAK")
                    if b == fr.SYN:
                        continue  # keep waiting
                    if b != fr.PRE:
                        continue  # ignore stray bytes
                buf.append(b)
                if buf and buf[-1] == fr.ETX:
                    return bytes(buf)
        raise TransportTimeout(f"No complete frame within {timeout}s")

    # ─── auto-detection ──────────────────────────────────────

    def detect(self, baudrates: Optional[List[int]] = None) -> Optional[IslDeviceInfo]:
        """Probe for a Datecs ISL device on the open transport.

        Note: baudrate probing is the transport's responsibility. This
        method only sends `CMD_GET_STATUS` followed by `CMD_GET_DEVICE_INFO`
        and parses the result. Caller can swap transports if multi-baudrate
        scanning is needed.
        """
        # 1. STATUS — confirms we have an ISL responder
        text, status, _raw = self._isl_request(cmd.CMD_GET_STATUS, "")
        if not status.ok or status.errors:
            _logger.debug("Detection: STATUS errors: %s", status.errors)

        # 2. DEVICE INFO with param "1"
        text, status, _raw = self._isl_request(cmd.CMD_GET_DEVICE_INFO, "1")
        if not text:
            _logger.debug("Detection: empty device info")
            return None

        raw_data = text.encode("cp1251", errors="ignore")
        for parser in _DETECTION_PARSERS:
            info = parser(raw_data)
            if info and info.model:
                self.info = info
                _logger.info(
                    "Detected %s (%s) — fw=%s serial=%s",
                    info.model,
                    info.protocol,
                    info.firmware_version,
                    info.serial_number,
                )
                return info
        return None

    # ─── high-level commands ────────────────────────────────

    def get_status(self) -> DeviceStatus:
        _t, status, _r = self._isl_request(cmd.CMD_GET_STATUS, "")
        return status

    def get_tax_identification_number(self) -> Tuple[str, DeviceStatus]:
        text, status, _r = self._isl_request(cmd.CMD_GET_TAX_ID_NUMBER)
        return text.strip(), status

    def get_date_time(self) -> Tuple[Optional[datetime], DeviceStatus]:
        text, status, _r = self._isl_request(cmd.CMD_GET_DATE_TIME)
        if not status.ok:
            return None, status
        for fmt in ("%d-%m-%y %H:%M:%S", "%d.%m.%y %H:%M:%S", "%d-%m-%Y %H:%M:%S"):
            try:
                return datetime.strptime(text.strip(), fmt), status
            except ValueError:
                continue
        status.add_error("E409", f"Bad datetime format: {text!r}")
        return None, status

    def set_date_time(self, dt: datetime) -> DeviceStatus:
        payload = dt.strftime("%d-%m-%y %H:%M:%S")
        _t, status, _r = self._isl_request(cmd.CMD_SET_DATE_TIME, payload)
        return status

    # ─── fiscal receipt lifecycle ────────────────────────────

    def open_receipt(
        self,
        unique_sale_number: str,
        operator_id: Optional[str] = None,
        operator_password: Optional[str] = None,
    ) -> DeviceStatus:
        op = operator_id or self.operator_id
        pw = operator_password or self.operator_password
        # ISL DATA fields are separated by TAB (0x09) per protocol spec —
        # see docs/PROTOCOL_REFERENCE.md §"Frame layout". Comma is a
        # plain printable byte that the device treats as part of the
        # operator-id token, triggering E401 'Syntax error'.
        header = "\t".join([op, pw, unique_sale_number])
        _t, status, _r = self._isl_request(cmd.CMD_OPEN_FISCAL_RECEIPT, header)
        return status

    def open_reversal_receipt(
        self,
        reason: ReversalReason,
        receipt_number: str,
        receipt_dt: datetime,
        fm_serial: str,
        unique_sale_number: str,
        operator_id: Optional[str] = None,
        operator_password: Optional[str] = None,
    ) -> DeviceStatus:
        op = operator_id or self.admin_id
        pw = operator_password or self.admin_password
        reason_code = _REVERSAL_CODES[reason]
        dt_str = receipt_dt.strftime("%d-%m-%y %H:%M:%S")
        header = (
            f"{op},{pw},{unique_sale_number}\t"
            f"R{reason_code},{receipt_number},{dt_str}\t"
            f"{fm_serial}"
        )
        _t, status, _r = self._isl_request(cmd.CMD_OPEN_FISCAL_RECEIPT, header)
        return status

    def add_comment(self, text: str) -> DeviceStatus:
        text = text[: self.info.comment_text_max_length or 40]
        _t, status, _r = self._isl_request(cmd.CMD_FISCAL_RECEIPT_COMMENT, text)
        return status

    def add_item(
        self,
        text: str,
        unit_price: Decimal | float,
        tax_group: TaxGroup,
        quantity: Decimal | float = Decimal("0"),
        department: int = 0,
        price_modifier_type: PriceModifierType = PriceModifierType.NONE,
        price_modifier_value: Decimal | float = Decimal("0"),
    ) -> DeviceStatus:
        unit_price = Decimal(str(unit_price))
        quantity = Decimal(str(quantity))
        price_modifier_value = Decimal(str(price_modifier_value))

        name = text[: self.info.item_text_max_length or 40]
        if department <= 0:
            payload = f"{name}\t{self.tax_group_letter(tax_group)}{unit_price:.2f}"
        else:
            payload = f"{name}\t{department}\t{unit_price:.2f}"

        if quantity != Decimal("0"):
            payload += f"*{quantity}"

        if price_modifier_type != PriceModifierType.NONE:
            sep = (
                ","
                if price_modifier_type
                in (
                    PriceModifierType.DISCOUNT_PERCENT,
                    PriceModifierType.SURCHARGE_PERCENT,
                )
                else "$"
            )
            value = price_modifier_value
            if price_modifier_type in (
                PriceModifierType.DISCOUNT_PERCENT,
                PriceModifierType.DISCOUNT_AMOUNT,
            ):
                value = -value
            payload += f"{sep}{value:.2f}"

        _t, status, _r = self._isl_request(cmd.CMD_FISCAL_RECEIPT_SALE, payload)
        return status

    def add_payment(self, amount: Decimal | float, payment_type: PaymentType) -> DeviceStatus:
        amount = Decimal(str(amount))
        payload = f"\t{self.payment_type_letter(payment_type)}{amount:.2f}"
        _t, status, _r = self._isl_request(cmd.CMD_FISCAL_RECEIPT_TOTAL, payload)
        return status

    def full_payment(self) -> DeviceStatus:
        _t, status, _r = self._isl_request(cmd.CMD_FISCAL_RECEIPT_TOTAL, "\t")
        return status

    def subtotal(self) -> DeviceStatus:
        _t, status, _r = self._isl_request(cmd.CMD_SUBTOTAL, "")
        return status

    def close_receipt(self) -> DeviceStatus:
        _t, status, _r = self._isl_request(cmd.CMD_CLOSE_FISCAL_RECEIPT)
        return status

    def abort_receipt(self) -> DeviceStatus:
        _t, status, _r = self._isl_request(cmd.CMD_ABORT_FISCAL_RECEIPT)
        return status

    # ─── reports / cash ─────────────────────────────────────

    def print_x_report(self) -> DeviceStatus:
        _t, status, _r = self._isl_request(cmd.CMD_PRINT_DAILY_REPORT, "2", timeout=120.0)
        return status

    def print_z_report(self) -> DeviceStatus:
        _t, status, _r = self._isl_request(cmd.CMD_PRINT_DAILY_REPORT, "", timeout=120.0)
        return status

    def print_duplicate(self) -> DeviceStatus:
        _t, status, _r = self._isl_request(cmd.CMD_PRINT_LAST_RECEIPT_DUPLICATE, "1")
        return status

    def cash_in(self, amount: Decimal | float) -> DeviceStatus:
        amount = Decimal(str(amount))
        _t, status, _r = self._isl_request(cmd.CMD_MONEY_TRANSFER, f"{amount:.2f}")
        return status

    def cash_out(self, amount: Decimal | float) -> DeviceStatus:
        amount = Decimal(str(amount))
        _t, status, _r = self._isl_request(cmd.CMD_MONEY_TRANSFER, f"-{amount:.2f}")
        return status
