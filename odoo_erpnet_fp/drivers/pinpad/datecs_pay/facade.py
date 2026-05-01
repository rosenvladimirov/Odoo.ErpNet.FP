"""
High-level facade over `DatecsPinpadDriver` (the ctypes wrapper).

Wraps the raw transaction state machine (start_transaction → wait for
result → get_receipt_tags → end_transaction) into single-call methods
that match the expected POS workflow. Returns structured `TransactionResult`
dataclasses with parsed TLV fields.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from ._native import (
    DatecsPinpadDriver,
    PinpadInfo,
    PinpadStatus,
    TAG_AMOUNT,
    TAG_AUTH_ID,
    TAG_HOST_AUTH_ID,
    TAG_HOST_RRN,
    TAG_RRN,
    TAG_TERMINAL_ID,
    TAG_TRANS_ERROR,
    TAG_TRANS_RESULT,
    TRANS_END_OF_DAY,
    TRANS_PURCHASE,
    TRANS_TEST_CONNECTION,
    TRANS_VOID_PURCHASE,
    create_purchase_params,
    create_void_params,
)

_logger = logging.getLogger(__name__)


@dataclass
class TransactionResult:
    """Structured outcome of a pinpad transaction."""

    ok: bool
    error: Optional[str] = None  # raw error code from device, if any
    amount_cents: Optional[int] = None
    rrn: Optional[str] = None
    auth_id: Optional[str] = None
    host_rrn: Optional[str] = None
    host_auth_id: Optional[str] = None
    terminal_id: Optional[str] = None
    raw_tlv: bytes = b""  # full TLV reply for debugging
    extras: dict = field(default_factory=dict)


class DatecsPayPinpad:
    """High-level pinpad driver — wraps `DatecsPinpadDriver`.

    Usage:

        pp = DatecsPayPinpad(port="/dev/ttyUSB0", baudrate=115200)
        with pp:
            info = pp.get_info()
            result = pp.purchase(amount_cents=2050)
            if result.ok:
                print("RRN:", result.rrn)
    """

    # Result tags we extract for the structured response
    _RECEIPT_TAGS = [
        TAG_TRANS_RESULT,
        TAG_TRANS_ERROR,
        TAG_AMOUNT,
        TAG_RRN,
        TAG_AUTH_ID,
        TAG_HOST_RRN,
        TAG_HOST_AUTH_ID,
        TAG_TERMINAL_ID,
    ]

    # How long we wait for the customer to insert the card and PIN-in.
    DEFAULT_TRANS_TIMEOUT = 60.0

    def __init__(self, port: str, baudrate: int = 115200) -> None:
        self.port = port
        self.baudrate = baudrate
        self._drv: Optional[DatecsPinpadDriver] = None

    # ─── lifecycle ──────────────────────────────────────────

    def open(self) -> None:
        if self._drv is not None:
            return
        self._drv = DatecsPinpadDriver(port=self.port, baudrate=self.baudrate)
        self._drv.open()

    def close(self) -> None:
        if self._drv is None:
            return
        try:
            self._drv.close()
        finally:
            self._drv = None

    def __enter__(self) -> "DatecsPayPinpad":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._drv is not None

    # ─── status / info ──────────────────────────────────────

    def ping(self) -> bool:
        if not self._drv:
            return False
        return self._drv.ping()

    def get_info(self) -> PinpadInfo:
        if not self._drv:
            raise RuntimeError("Pinpad not open")
        return self._drv.get_info()

    def get_status(self) -> PinpadStatus:
        if not self._drv:
            raise RuntimeError("Pinpad not open")
        return self._drv.get_status()

    # ─── transactions ───────────────────────────────────────

    def purchase(
        self,
        amount_cents: int,
        tip_cents: Optional[int] = None,
        cashback_cents: Optional[int] = None,
        reference: Optional[str] = None,
        timeout: float = DEFAULT_TRANS_TIMEOUT,
    ) -> TransactionResult:
        """Run a card purchase transaction. Amounts are in smallest currency
        units (e.g. stotinki for BGN).

        Blocks until the customer completes (or aborts) the transaction
        on the pinpad keypad, up to `timeout` seconds.
        """
        if not self._drv:
            raise RuntimeError("Pinpad not open")
        params = create_purchase_params(
            amount=amount_cents,
            tip=tip_cents,
            cashback=cashback_cents,
            reference=reference,
        )
        return self._run_transaction(TRANS_PURCHASE, params, timeout)

    def void_purchase(
        self,
        amount_cents: int,
        rrn: str,
        auth_id: str,
        tip_cents: Optional[int] = None,
        cashback_cents: Optional[int] = None,
        timeout: float = DEFAULT_TRANS_TIMEOUT,
    ) -> TransactionResult:
        """Cancel a previous purchase by RRN + auth_id."""
        if not self._drv:
            raise RuntimeError("Pinpad not open")
        params = create_void_params(
            amount=amount_cents,
            rrn=rrn,
            auth_id=auth_id,
            tip=tip_cents,
            cashback=cashback_cents,
        )
        return self._run_transaction(TRANS_VOID_PURCHASE, params, timeout)

    def end_of_day(
        self, timeout: float = DEFAULT_TRANS_TIMEOUT
    ) -> TransactionResult:
        """Daily settlement (sends batch totals to the host bank)."""
        if not self._drv:
            raise RuntimeError("Pinpad not open")
        return self._run_transaction(TRANS_END_OF_DAY, b"", timeout)

    def test_connection(
        self, timeout: float = 30.0
    ) -> TransactionResult:
        """Probe the host bank — useful for nightly health checks."""
        if not self._drv:
            raise RuntimeError("Pinpad not open")
        return self._run_transaction(TRANS_TEST_CONNECTION, b"", timeout)

    # ─── internals ──────────────────────────────────────────

    def _run_transaction(
        self,
        trans_type: int,
        params: bytes,
        timeout: float,
    ) -> TransactionResult:
        """Driver state-machine: start → poll until result → fetch tags → end."""
        try:
            self._drv.start_transaction(trans_type, params or None)
        except RuntimeError as exc:
            return TransactionResult(ok=False, error=str(exc))

        deadline = time.monotonic() + timeout
        result_tlv = b""
        while time.monotonic() < deadline:
            try:
                # Some firmware revisions surface intermediate TLV here;
                # treat empty / not-ready as "still processing".
                result_tlv = self._drv.get_receipt_tags(self._RECEIPT_TAGS)
                if result_tlv:
                    break
            except RuntimeError:
                # Pinpad busy — keep polling
                pass
            time.sleep(0.5)

        if not result_tlv:
            try:
                self._drv.end_transaction(success=False)
            except Exception:
                pass
            return TransactionResult(ok=False, error="timeout")

        parsed = self._parse_tlv(result_tlv)
        ok = (parsed.get("trans_result", b"") == b"\x00") and not parsed.get(
            "trans_error"
        )
        try:
            self._drv.end_transaction(success=ok)
        except RuntimeError as exc:
            _logger.warning("Pinpad end_transaction failed: %s", exc)

        return TransactionResult(
            ok=ok,
            error=(
                parsed.get("trans_error", b"").hex()
                if parsed.get("trans_error")
                else None
            ),
            amount_cents=parsed.get("amount_cents"),
            rrn=parsed.get("rrn"),
            auth_id=parsed.get("auth_id"),
            host_rrn=parsed.get("host_rrn"),
            host_auth_id=parsed.get("host_auth_id"),
            terminal_id=parsed.get("terminal_id"),
            raw_tlv=result_tlv,
        )

    @staticmethod
    def _parse_tlv(tlv: bytes) -> dict:
        """Parse the receipt-tags TLV reply into a flat dict.

        Uses the underlying driver's own parser (`DatecsPinpadDriver.parse_tlv`)
        which understands EMV-style TLV encoding.
        """
        out: dict = {}
        try:
            parsed = DatecsPinpadDriver.parse_tlv(tlv)
        except Exception:  # noqa: BLE001
            return out

        def _bytes_to_str(val) -> Optional[str]:
            if not val:
                return None
            try:
                return val.decode("ascii", errors="ignore").strip("\x00").strip()
            except Exception:  # noqa: BLE001
                return None

        if TAG_TRANS_RESULT in parsed:
            out["trans_result"] = parsed[TAG_TRANS_RESULT]
        if TAG_TRANS_ERROR in parsed:
            out["trans_error"] = parsed[TAG_TRANS_ERROR]
        if TAG_AMOUNT in parsed:
            try:
                out["amount_cents"] = DatecsPinpadDriver.decode_amount(
                    parsed[TAG_AMOUNT]
                )
            except Exception:  # noqa: BLE001
                pass
        if TAG_RRN in parsed:
            out["rrn"] = _bytes_to_str(parsed[TAG_RRN])
        if TAG_AUTH_ID in parsed:
            out["auth_id"] = _bytes_to_str(parsed[TAG_AUTH_ID])
        if TAG_HOST_RRN in parsed:
            out["host_rrn"] = _bytes_to_str(parsed[TAG_HOST_RRN])
        if TAG_HOST_AUTH_ID in parsed:
            out["host_auth_id"] = _bytes_to_str(parsed[TAG_HOST_AUTH_ID])
        if TAG_TERMINAL_ID in parsed:
            out["terminal_id"] = _bytes_to_str(parsed[TAG_TERMINAL_ID])

        return out
