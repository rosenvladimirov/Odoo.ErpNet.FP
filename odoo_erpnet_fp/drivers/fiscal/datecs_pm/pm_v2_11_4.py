"""
High-level facade for Datecs PM Communication Protocol v2.11.4.

Combines a `Transport` with the framing layer to provide a command-level
API. Sequence number management, NAK retry, and SYN waiting all live
here; per-command parameter assembly and result parsing are the only
command-aware logic.

Threading: PmDevice instances are NOT thread-safe. Serialise access
through the Odoo per-device record lock.

Bounded timeouts: every public method completes within
`BOUNDED_OP_TIMEOUT` seconds (default 60) per CLAUDE.md invariant #5.
Long-running ops (Z-report) must run in a queue_job / cron, not inline.
"""

from __future__ import annotations

import base64
import time

from . import codec, commands, errors, frame, status
from .transport import Transport, TransportError, TransportTimeout

# Cmd 0xCA / 0xCB chunked-Base64 protocol: each Parameter field is up
# to 72 chars per PDF §4.70. We send 72-char chunks of base64 data so
# that the START/STOPP/RESTART tokens (5..7 chars) and base64 chunks
# both fit cleanly under the 72-char cap.
_IMAGE_B64_CHUNK = 72


class PmDevice:
    SLAVE_REPLY_TIMEOUT = 0.5  # 500 ms — per spec, before retry
    BOUNDED_OP_TIMEOUT = 60.0  # CLAUDE.md invariant #5

    def __init__(
        self,
        transport: Transport,
        retries: int = 3,
        op_code: int = 1,
        op_password: str = "0000",
        till_number: int = 1,
    ) -> None:
        self._t = transport
        self._retries = retries
        self.op_code = op_code
        self.op_password = op_password
        self.till_number = till_number
        self._seq = 0x20

    # ---- session management --------------------------------------------

    def open(self) -> None:
        if not self._t.is_open():
            self._t.open()

    def close(self) -> None:
        self._t.close()

    def __enter__(self) -> "PmDevice":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ---- low-level exchange --------------------------------------------

    def _next_seq(self) -> int:
        seq = self._seq
        self._seq = 0x20 if seq >= 0xFF else seq + 1
        return seq

    def _exchange(
        self, cmd: int, data: bytes = b"", timeout: float | None = None
    ) -> frame.Response:
        """Send a request and return the parsed response.

        Retries on NAK with the same SEQ. Waits through SYN bytes while
        the slave is processing. Raises TransportError if the device is
        offline after all retries.
        """
        deadline = time.monotonic() + (timeout or self.BOUNDED_OP_TIMEOUT)
        seq = self._next_seq()
        request = frame.encode_request(seq, cmd, data)

        last_exc: Exception | None = None
        for _ in range(self._retries):
            try:
                self._t.write(request)
                response = self._read_one_frame(deadline)
            except (frame.FrameError, TransportTimeout) as exc:
                last_exc = exc
                continue
            if response.seq != seq:
                last_exc = frame.FormatError(
                    f"SEQ mismatch: sent 0x{seq:02X}, "
                    f"received 0x{response.seq:02X}"
                )
                continue
            if response.cmd != cmd:
                last_exc = frame.FormatError(
                    f"CMD mismatch: sent 0x{cmd:04X}, "
                    f"received 0x{response.cmd:04X}"
                )
                continue
            return response

        raise TransportError(
            f"Device unreachable after {self._retries} retries: {last_exc}"
        )

    def _read_one_frame(self, deadline: float) -> frame.Response:
        """Read first byte, dispatch by NAK/SYN/PRE; return parsed frame."""
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise TransportTimeout("Bounded operation deadline exceeded")
            first = self._t.read(1, min(self.SLAVE_REPLY_TIMEOUT, deadline - now))
            if not first:
                raise TransportTimeout("No first byte from slave within 500ms")

            b = first[0]
            if b == frame.NAK:
                raise frame.ChecksumError("Slave sent NAK (host frame invalid)")
            if b == frame.SYN:
                # Slave still processing — keep waiting
                continue
            if b == frame.PRE:
                # Read until EOT (max envelope ≈ LEN+SEQ+CMD+DATA+SEP+STAT+PST+BCC ~ 510)
                remaining = self._t.read_until(
                    bytes([frame.EOT]),
                    max_bytes=600,
                    timeout=deadline - time.monotonic(),
                )
                if not remaining or remaining[-1] != frame.EOT:
                    raise frame.FormatError("Frame missing EOT terminator")
                raw = bytes([frame.PRE]) + remaining
                return frame.decode_response(raw)
            # Unexpected byte; ignore and continue scanning.

    # ---- command-level helpers -----------------------------------------

    @staticmethod
    def _parse_error_code(data: bytes) -> tuple[int, list[str]]:
        """Split DATA fields and pull off the leading ErrorCode."""
        fields = codec.decode_data(data)
        if not fields:
            return 0, []
        try:
            code = int(fields[0])
        except ValueError:
            return 0, fields  # not all responses lead with ErrorCode
        return code, fields[1:]

    # ---- Phase 1 commands ---------------------------------------------

    def read_status(self) -> status.FiscalStatus:
        """0x4A — Read 8-byte status. Sends empty DATA."""
        resp = self._exchange(commands.CMD_READ_STATUS)
        # cmd 0x4A response DATA is empty — only status bytes carry info
        return status.FiscalStatus.parse(resp.status)

    def read_device_info(self) -> dict:
        """0x7B — Device information.

        Returns a dict with keys: model, firmware_version, firmware_date,
        certificate, serial_number, fiscal_memory_serial_number. The
        device sends them TAB-separated in the response data.

        Per Datecs PM v2.11.4 §4.63: the response format is
            <Type>\\t<Version>\\t<Date>\\t<Time>\\t<Cert>\\t<SerialNumber>\\t<FmSerialNumber>
        Some firmware revisions may add or omit the time field; we
        unpack tolerantly so a short response still yields a partial
        info dict instead of raising.
        """
        resp = self._exchange(commands.CMD_DEVICE_INFO)
        # No leading error code in this response — payload is just the
        # TAB-separated fields. Decode as cp1251 (Cyrillic-friendly,
        # matches PDF §1.5 character encoding).
        text = resp.data.decode("cp1251", errors="replace").rstrip("\r\n\t ")
        parts = text.split("\t")
        # Tolerantly extract by index; pad with "" so callers don't
        # crash on legacy short responses.
        while len(parts) < 7:
            parts.append("")
        return {
            "model": parts[0].strip(),
            "firmware_version": parts[1].strip(),
            "firmware_date": parts[2].strip(),
            "firmware_time": parts[3].strip(),
            "certificate": parts[4].strip(),
            "serial_number": parts[5].strip(),
            "fiscal_memory_serial_number": parts[6].strip(),
        }

    def read_tax_number(self) -> str:
        """0x63 — Read the programmed tax (BULSTAT) number."""
        resp = self._exchange(commands.CMD_TAX_NUMBER_READ)
        text = resp.data.decode("cp1251", errors="replace").rstrip("\r\n\t ")
        # Some firmware prefixes with the error code; _parse_error_code
        # is designed for that.
        code, rest = self._parse_error_code(resp.data)
        errors.raise_for_code(code)
        return (rest[0].strip() if rest else text).strip()

    # ---- VAT-rate programming (cmd 0x53) ------------------------------

    # 1..8 → CP-1251 letter sequence the device expects internally.
    # Slots 5..8 are device-programmable but rarely used in BG retail.
    _VAT_LETTERS = ("А", "Б", "В", "Г", "Д", "Е", "Ж", "З")

    def read_vat_rates(self) -> dict:
        """0x53 'I' — Read currently programmed VAT rates.

        Returns: dict like {'А': 2000, 'Б': 900, 'В': 0, 'Г': None,
                            'decimal_point': 2}
        Each rate is stored as int × 100 (so 2000 = 20.00%). `None`
        means the slot is disabled ('X' from the device).
        """
        data = codec.encode_data("I")
        resp = self._exchange(commands.CMD_VAT_PROGRAMMING, data)
        code, rest = self._parse_error_code(resp.data)
        errors.raise_for_code(code)

        # rest is a list of TAB-separated string fields. Pad to 9 to be
        # tolerant of firmware that truncates trailing fields.
        fields = list(rest) if rest else []
        while len(fields) < 9:
            fields.append("")
        out: dict = {}
        for idx, letter in enumerate(self._VAT_LETTERS):
            v = fields[idx].strip().upper()
            if not v or v == "X":
                out[letter] = None
            else:
                try:
                    out[letter] = int(v)
                except ValueError:
                    out[letter] = None
        try:
            out["decimal_point"] = int(fields[8].strip() or "2")
        except ValueError:
            out["decimal_point"] = 2
        return out

    def program_vat_rates(self, rates: dict, decimal_point: int = 2) -> None:
        """0x53 'P' — Program VAT rates.

        `rates` keys: any of 'А', 'Б', 'В', 'Г', 'Д', 'Е', 'Ж', 'З'
        (CP-1251 Cyrillic) OR 'A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'
        (Latin shortcuts — auto-translated).

        Values: integer × 100 (e.g. 2000 = 20.00%, 900 = 9.00%, 0 =
        zero-rated). Use `None` to disable a slot.

        FISCAL CAVEAT: BG devices typically reject VAT programming
        unless a Z-report has been printed first today (daily totals
        zeroed) and may require service-mode unlock for certain rate
        changes. The proxy surfaces the device's error code unchanged
        — interpret it through the Datecs PM error-code reference.
        """
        # Normalize Latin → Cyrillic
        latin_map = dict(zip("ABCDEFGH", self._VAT_LETTERS))
        normalized = {}
        for k, v in rates.items():
            if not isinstance(k, str):
                raise ValueError(f"VAT slot key must be str, got {type(k)}")
            up = k.strip().upper()
            cyr = latin_map.get(up, up)  # already cyrillic OR translated
            if cyr not in self._VAT_LETTERS:
                raise ValueError(
                    f"Unknown VAT slot {k!r} — valid: А-З or A-H")
            normalized[cyr] = v

        # Build TAB-separated fields: P\t<v0>\t<v1>...\t<v7>\t<dcpp>
        fields: list = ["P"]
        for letter in self._VAT_LETTERS:
            if letter in normalized:
                v = normalized[letter]
                if v is None:
                    fields.append("X")
                elif isinstance(v, (int, float)):
                    fields.append(str(int(v)))
                else:
                    raise ValueError(
                        f"VAT rate for {letter!r} must be int×100 or None, "
                        f"got {v!r}")
            else:
                fields.append("X")  # not provided → keep disabled
        fields.append(str(int(decimal_point)))
        data = codec.encode_data(*fields)
        resp = self._exchange(commands.CMD_VAT_PROGRAMMING, data, timeout=10.0)
        code, _ = self._parse_error_code(resp.data)
        errors.raise_for_code(code)

    def detect(self) -> dict:
        """Aggregate `read_device_info` + `read_tax_number` into a single
        dict shaped like the ISL `IslDeviceInfo` cache used by
        `printers.py:_device_info`. Tolerant — partial reads survive.
        """
        info: dict = {
            "manufacturer": "Datecs",
            "model": "PM (v2.11.4)",
            "firmware_version": "",
            "serial_number": "",
            "fiscal_memory_serial_number": "",
            "tax_identification_number": "",
        }
        try:
            di = self.read_device_info()
            info["model"] = di.get("model") or info["model"]
            info["firmware_version"] = di.get("firmware_version", "")
            info["serial_number"] = di.get("serial_number", "")
            info["fiscal_memory_serial_number"] = (
                di.get("fiscal_memory_serial_number", ""))
        except Exception:
            pass
        try:
            info["tax_identification_number"] = self.read_tax_number()
        except Exception:
            pass
        return info

    def open_fiscal_receipt(
        self,
        nsale: str | None = None,
        invoice: bool = False,
        op_code: int | None = None,
        op_password: str | None = None,
        till_number: int | None = None,
    ) -> int:
        """0x30 — Open a fiscal receipt. Returns SlipNumber.

        Uses syntax #2 (with UNS — Unique Sale Number) when `nsale` is
        given, which is the recommended idempotent form. NSale format:
        `LLDDDDDD-CCCC-DDDDDDD` (PROTOCOL_REFERENCE §10).
        """
        op = op_code if op_code is not None else self.op_code
        pw = op_password if op_password is not None else self.op_password
        till = till_number if till_number is not None else self.till_number
        invoice_flag = "I" if invoice else ""

        if nsale:
            data = codec.encode_data(op, pw, nsale, till, invoice_flag)
        else:
            data = codec.encode_data(op, pw, till, invoice_flag)

        resp = self._exchange(commands.CMD_OPEN_FISCAL_RECEIPT, data)
        code, rest = self._parse_error_code(resp.data)
        errors.raise_for_code(code)
        if not rest:
            raise frame.FormatError(
                "Open receipt response missing SlipNumber"
            )
        return int(rest[0])

    def register_sale(
        self,
        text: str,
        price: float,
        quantity: float = 1.0,
        vat_group: str = "А",
        discount_percent: float | None = None,
        department: int | None = None,
    ) -> tuple[float, status.FiscalStatus]:
        """0x31 — Register a sale line.

        VAT group is one of А/Б/В/Г (CP-1251). Returns (running total,
        status). PROTOCOL_REFERENCE Phase-1 column.
        """
        # Parameter order per PDF §4.12: PluName \t TaxCd \t Price \t Quantity
        # \t DiscountType \t DiscountValue \t Department \t
        # The exact ordering and optionality is device-firmware dependent;
        # this is the canonical syntax. Empty optional parameters keep
        # their TAB separator (codec.encode_data handles None as empty).
        data = codec.encode_data(
            text,
            vat_group,
            f"{price:.2f}",
            f"{quantity:.3f}",
            None if discount_percent is None else "1",  # DiscountType: %
            None if discount_percent is None else f"{discount_percent:.2f}",
            department,
        )
        resp = self._exchange(commands.CMD_REGISTER_SALE, data)
        code, rest = self._parse_error_code(resp.data)
        errors.raise_for_code(code)
        running_total = float(rest[0]) if rest else 0.0
        return running_total, status.FiscalStatus.parse(resp.status)

    def subtotal(
        self, print_subtotal: bool = False, display: bool = False
    ) -> tuple[float, status.FiscalStatus]:
        """0x33 — Compute and optionally print the subtotal."""
        data = codec.encode_data(
            "1" if print_subtotal else "0",
            "1" if display else "0",
        )
        resp = self._exchange(commands.CMD_SUBTOTAL, data)
        code, rest = self._parse_error_code(resp.data)
        errors.raise_for_code(code)
        amount = float(rest[0]) if rest else 0.0
        return amount, status.FiscalStatus.parse(resp.status)

    def payment_total(
        self,
        payment_type: int = 0,
        amount: float | None = None,
    ) -> tuple[float, status.FiscalStatus]:
        """0x35 — Register payment, compute total. Returns (change, status).

        payment_type: 0 = Cash, 1 = Credit card (no pinpad), 2-7 = device-
        programmed methods. See PROTOCOL_REFERENCE §9.
        """
        data = codec.encode_data(
            payment_type,
            None if amount is None else f"{amount:.2f}",
        )
        resp = self._exchange(commands.CMD_PAYMENT_TOTAL, data)
        code, rest = self._parse_error_code(resp.data)
        errors.raise_for_code(code)
        change = float(rest[0]) if rest else 0.0
        return change, status.FiscalStatus.parse(resp.status)

    def close_fiscal_receipt(self) -> int:
        """0x38 — Close the open fiscal receipt. Returns the closed slip
        number (echo from open). After this the receipt is committed to
        EJ + FM and pushed to NRA when connectivity allows.
        """
        resp = self._exchange(commands.CMD_CLOSE_FISCAL_RECEIPT)
        code, rest = self._parse_error_code(resp.data)
        errors.raise_for_code(code)
        return int(rest[0]) if rest else 0

    def cancel_fiscal_receipt(self) -> None:
        """0x3C — Cancel the open fiscal receipt.

        ONLY valid before payment_total (0x35) — after total, the receipt
        is fiscalized and cannot be cancelled (PROTOCOL_REFERENCE §11).
        Use this for error recovery between open and total.
        """
        resp = self._exchange(commands.CMD_CANCEL_FISCAL_RECEIPT)
        code, _ = self._parse_error_code(resp.data)
        errors.raise_for_code(code)

    # ---- Phase 2 — PLU programming (cmd 0x6B) -------------------------

    def program_plu(
        self,
        plu_number: int,
        name: str,
        price: float,
        vat_group: str = "А",
        department: int = 0,
        group: int = 1,
        price_type: int = 0,
        quantity: float | None = None,
        barcodes: tuple[str, ...] = (),
        measurement_unit: int = 0,
    ) -> None:
        """0x6B 'P' — Program a PLU item (syntax #2 with Measurement unit).

        Args:
            plu_number: 1..100000 (capable devices) or 1..3000 (basic).
            name: up to 72 CP-1251 characters.
            price: item price (always 2 decimals on wire, e.g. "1.09").
            vat_group: single char A..H (Latin) or А..З (Cyrillic).
            department: 0..99.
            group: 1..99 (item group).
            price_type: 0=fixed, 1=free, 2=max.
            quantity: when set, sends with AddQty='A' (stock update).
            barcodes: up to 4 barcodes (each up to 13 digits).
            measurement_unit: 0=бр., 1=кг, 2=м, 3=л, 4=ч, 5..19=custom.
        """
        if not 1 <= plu_number <= 100000:
            raise ValueError(f"PLU number {plu_number} out of 1..100000")
        if len(name) > 72:
            raise ValueError(f"Name exceeds 72 chars: {len(name)}")
        if len(vat_group) != 1:
            raise ValueError(f"VAT group must be single char: {vat_group!r}")
        if not 0 <= department <= 99:
            raise ValueError(f"Department {department} out of 0..99")
        if not 1 <= group <= 99:
            raise ValueError(f"Group {group} out of 1..99")
        if price_type not in (0, 1, 2):
            raise ValueError(f"PriceType {price_type} not in 0..2")
        if not 0 <= measurement_unit <= 19:
            raise ValueError(
                f"Measurement unit {measurement_unit} out of 0..19"
            )
        if len(barcodes) > 4:
            raise ValueError(f"At most 4 barcodes, got {len(barcodes)}")

        bars = list(barcodes) + [None] * (4 - len(barcodes))
        add_qty = "A" if quantity is not None else None

        data = codec.encode_data(
            "P",
            plu_number,
            vat_group,
            department,
            group,
            price_type,
            f"{price:.2f}",
            add_qty,
            None if quantity is None else f"{quantity:.3f}",
            bars[0],
            bars[1],
            bars[2],
            bars[3],
            name,
            measurement_unit,
        )
        resp = self._exchange(commands.CMD_PLU, data)
        code, _ = self._parse_error_code(resp.data)
        errors.raise_for_code(code)

    def delete_plu(
        self, first_plu: int, last_plu: int | None = None
    ) -> None:
        """0x6B 'D' — Delete a PLU range (inclusive).

        If `last_plu` is None, deletes only `first_plu`.
        """
        data = codec.encode_data("D", first_plu, last_plu)
        resp = self._exchange(commands.CMD_PLU, data)
        code, _ = self._parse_error_code(resp.data)
        errors.raise_for_code(code)

    def read_plu_info(self) -> tuple[int, int, int]:
        """0x6B 'I' — Returns (capacity, programmed_count, max_name_length)."""
        resp = self._exchange(commands.CMD_PLU, codec.encode_data("I"))
        code, rest = self._parse_error_code(resp.data)
        errors.raise_for_code(code)
        if len(rest) < 3:
            raise frame.FormatError("PLU info response missing fields")
        return int(rest[0]), int(rest[1]), int(rest[2])

    # ---- Phase 2 — Image upload (cmd 0xCA / 0xCB) ----------------------

    def upload_logo(
        self, image_data: bytes, restart_after: bool = True
    ) -> int:
        """0xCA — Upload customer logo (chunked Base64). Returns CheckSum.

        Workflow per PDF §4.70: START → N base64 chunks → STOPP (returns
        CheckSum) → RESTART (device reboots and applies the new logo).
        """
        return self._upload_image(
            commands.CMD_CUSTOMER_LOGO, image_data, restart_after
        )

    def upload_stamp(self, image_data: bytes) -> int:
        """0xCB — Upload stamp image (chunked Base64). Returns CheckSum.

        Same chunked protocol as `upload_logo`, but the stamp command's
        documented sub-options are only START / STOPP / data — no
        RESTART / POWEROFF.
        """
        return self._upload_image(
            commands.CMD_STAMP_IMAGE, image_data, restart=False
        )

    def _upload_image(
        self, cmd: int, image_data: bytes, restart: bool
    ) -> int:
        if not image_data:
            raise ValueError("image_data is empty")

        # 1. START
        self._exchange(cmd, codec.encode_data("START"))

        # 2. Base64 chunks
        b64 = base64.b64encode(image_data).decode("ascii")
        for i in range(0, len(b64), _IMAGE_B64_CHUNK):
            chunk = b64[i : i + _IMAGE_B64_CHUNK]
            self._exchange(cmd, codec.encode_data(chunk))

        # 3. STOPP — returns CheckSum
        resp = self._exchange(cmd, codec.encode_data("STOPP"))
        code, rest = self._parse_error_code(resp.data)
        errors.raise_for_code(code)
        checksum = int(rest[0], 16) if rest and rest[0] else 0

        # 4. RESTART — only for the customer logo per PDF
        if restart:
            self._exchange(cmd, codec.encode_data("RESTART"))

        return checksum

    # ---- Phase 2 — Parameter read/write (cmd 0xFF) --------------------

    def read_parameter(self, name: str, index: int = 0) -> str:
        """0xFF — Read a device parameter as a string.

        Per PDF §4.73, parameters cover header/footer text, VAT rates,
        operator names, print options, network/Bluetooth/WLAN config,
        currency rates, and many more.
        """
        data = codec.encode_data(name, index, None)
        resp = self._exchange(commands.CMD_PARAMETERS, data)
        code, rest = self._parse_error_code(resp.data)
        errors.raise_for_code(code)
        return rest[0] if rest else ""

    def write_parameter(self, name: str, value, index: int = 0) -> None:
        """0xFF — Write a device parameter."""
        data = codec.encode_data(name, index, value)
        resp = self._exchange(commands.CMD_PARAMETERS, data)
        code, _ = self._parse_error_code(resp.data)
        errors.raise_for_code(code)

    # ---- Phase 2 — Reports (cmd 0x45) ---------------------------------

    def print_x_report(self) -> tuple[int, dict[str, float]]:
        """0x45 'X' — Print X-report (interim, no zeroing).

        Returns (report_number, {tax_group_letter: turnover_amount}).
        Per PDF §4.29.1 the response includes per-group sell totals
        (TotA..TotH) and per-group storno totals (StorA..StorH).
        """
        return self._exchange_report("X")

    def print_z_report(self) -> tuple[int, dict[str, float]]:
        """0x45 'Z' — Print Z-report (daily fiscal, with zeroing).

        Per Наредба Н-18 a Z-report MUST be issued at least once every
        24h. After Z, daily totals reset to 0 on the device.
        """
        return self._exchange_report("Z")

    def _exchange_report(
        self, report_type: str
    ) -> tuple[int, dict[str, float]]:
        if report_type not in ("X", "Z"):
            raise ValueError(f"report_type must be 'X' or 'Z', got {report_type!r}")
        # X/Z reports can take longer than the default 60 s on busy
        # devices — give them 120 s of headroom.
        resp = self._exchange(
            commands.CMD_REPORTS, codec.encode_data(report_type), timeout=120.0
        )
        code, rest = self._parse_error_code(resp.data)
        errors.raise_for_code(code)
        # rest = [nRep, TotA, TotB, ..., TotH, StorA, ..., StorH]
        # 1 + 8 + 8 = 17 fields total
        n_rep = int(rest[0]) if rest else 0
        groups = "ABCDEFGH"
        per_group: dict[str, float] = {}
        for i, letter in enumerate(groups, start=1):
            if i < len(rest):
                per_group[letter] = float(rest[i] or 0)
        return n_rep, per_group

    def print_department_report(self) -> None:
        """0x45 'D' — Departments report (no zeroing)."""
        resp = self._exchange(
            commands.CMD_REPORTS, codec.encode_data("D"), timeout=120.0
        )
        code, _ = self._parse_error_code(resp.data)
        errors.raise_for_code(code)

    def print_item_groups_report(self) -> None:
        """0x45 'G' — Item groups report (no zeroing)."""
        resp = self._exchange(
            commands.CMD_REPORTS, codec.encode_data("G"), timeout=120.0
        )
        code, _ = self._parse_error_code(resp.data)
        errors.raise_for_code(code)

    def print_periodical_report(
        self,
        sub_type: int,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> None:
        """0x45 'P' — Periodical report.

        sub_type: 1=by payments, 2=by departments, 3=by items.
        Dates in 'DD-MM-YY' format per PDF §4.29.3.
        """
        if sub_type not in (1, 2, 3):
            raise ValueError(f"sub_type must be 1..3, got {sub_type}")
        resp = self._exchange(
            commands.CMD_REPORTS,
            codec.encode_data("P", sub_type, start_date, end_date),
            timeout=120.0,
        )
        code, _ = self._parse_error_code(resp.data)
        errors.raise_for_code(code)

    # ---- Phase 2 — Cash in / out (cmd 0x46) ----------------------------

    def cash_in(self, amount: float) -> tuple[float, float, float]:
        """0x46 type=0 — Cash in (служебно въведено).

        Returns (cash_in_safe, total_cash_in, total_cash_out).
        """
        return self._cash_op(0, amount)

    def cash_out(self, amount: float) -> tuple[float, float, float]:
        """0x46 type=1 — Cash out (служебно изведено).

        Returns (cash_in_safe, total_cash_in, total_cash_out).
        """
        return self._cash_op(1, amount)

    def read_cash_state(self) -> tuple[float, float, float]:
        """0x46 with Amount=0 — Read current cash state without printing.

        Per PDF §4.30: "If Amount is 0, only answer is returned, and
        receipt is not printed."
        """
        return self._cash_op(0, 0.0)

    def _cash_op(
        self, op_type: int, amount: float
    ) -> tuple[float, float, float]:
        if op_type not in (0, 1, 2, 3):
            raise ValueError(f"op_type must be 0..3, got {op_type}")
        if amount < 0:
            raise ValueError(f"amount must be non-negative, got {amount}")
        data = codec.encode_data(op_type, f"{amount:.2f}")
        resp = self._exchange(commands.CMD_CASH_IN_OUT, data)
        code, rest = self._parse_error_code(resp.data)
        errors.raise_for_code(code)
        if len(rest) < 3:
            raise frame.FormatError(
                "Cash op response missing fields"
            )
        return float(rest[0]), float(rest[1]), float(rest[2])

    # ---- Phase 2 — Misc ------------------------------------------------

    def print_duplicate(self) -> None:
        """0x6D — Print a duplicate of the last fiscal receipt."""
        resp = self._exchange(commands.CMD_PRINT_DUPLICATE)
        code, _ = self._parse_error_code(resp.data)
        errors.raise_for_code(code)

    def daily_taxation_info(
        self, info_type: int = 0
    ) -> tuple[int, dict[str, float]]:
        """0x41 — Daily taxation info (no print).

        info_type: 0=turnover on TAX group (default), 1=amount on TAX
        group, 2=storno turnover, 3=storno amount on TAX group.
        Returns (n_rep, {group_letter: amount}).
        """
        if info_type not in (0, 1, 2, 3):
            raise ValueError(f"info_type must be 0..3, got {info_type}")
        resp = self._exchange(
            commands.CMD_DAILY_TAXATION, codec.encode_data(info_type)
        )
        code, rest = self._parse_error_code(resp.data)
        errors.raise_for_code(code)
        n_rep = int(rest[0]) if rest else 0
        groups = "ABCDEFGH"
        per_group: dict[str, float] = {}
        for i, letter in enumerate(groups, start=1):
            if i < len(rest):
                per_group[letter] = float(rest[i] or 0)
        return n_rep, per_group

    # ---- Phase 2+ — yet to land ---------------------------------------
    # invoice_data (0x39), fiscal_transaction_status (0x4C), pinpad
    # (0x37, 15 sub-options), storno (0x2B), …
