"""
Mettler-Toledo 8217 weight-only protocol — pure-Python driver.

Ported from Odoo IoT box `serial_scale_driver.py`. Removes the Driver/
Thread/event_manager scaffolding; exposes a simple `read_weight()` call
that the FastAPI server can `await asyncio.to_thread()` on demand.

Wire format (Mettler-Toledo Ariva-S Service Manual):
  Request:  b'W'
  Response: b'\\x02 <weight>N? \\r'   (e.g. b'\\x021.234N\\r' = 1.234 kg)
  Status:   b'\\x02 ?<byte>\\r'        when scale is in motion / over capacity / etc.

Serial settings: 9600 7E1.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

try:
    import serial
except ImportError:  # pragma: no cover
    serial = None

_logger = logging.getLogger(__name__)

# Toledo 8217 status-byte error bits (LSB first; ignore the parity bit).
_STATUS_ERROR_BITS = (
    "Scale in motion",      # bit 0
    "Over capacity",        # bit 1
    "Under zero",           # bit 2
    "Outside zero capture", # bit 3
    "Center of zero",       # bit 4
    "Net weight",           # bit 5
    "Bad command from host",# bit 6
)

_MEASURE_RE = re.compile(rb"\x02\s*([0-9.]+)N?\r")
_STATUS_RE = re.compile(rb"\x02\s*\?([^\x00])\r")


@dataclass
class WeightReading:
    """Result of `read_weight()`."""

    ok: bool
    weight_kg: Optional[float]
    status: list[str]
    raw: bytes = b""


class Toledo8217Scale:
    """Mettler-Toledo 8217 weight-only scale driver.

    Usage:

        from odoo_erpnet_fp.drivers.scales import Toledo8217Scale

        s = Toledo8217Scale(port="/dev/ttyUSB1")
        s.open()
        reading = s.read_weight()
        if reading.ok:
            print(f"{reading.weight_kg} kg")
        s.close()
    """

    BAUDRATE = 9600
    BYTESIZE = 7
    PARITY_EVEN = "E"
    STOPBITS = 1
    TIMEOUT = 1.0
    COMMAND_DELAY = 0.2

    def __init__(self, port: str, baudrate: int = BAUDRATE) -> None:
        if serial is None:
            raise RuntimeError(
                "pyserial is not installed. `pip install pyserial>=3.5`."
            )
        self.port = port
        self.baudrate = baudrate
        self._conn: Optional[serial.Serial] = None

    def open(self) -> None:
        if self._conn is not None and self._conn.is_open:
            return
        self._conn = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.SEVENBITS,
            parity=serial.PARITY_EVEN,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.TIMEOUT,
            write_timeout=self.TIMEOUT,
        )

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "Toledo8217Scale":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._conn is not None and self._conn.is_open

    # ─── public API ─────────────────────────────────────────

    def read_weight(self) -> WeightReading:
        """Send 'W' and parse the response into a `WeightReading`."""
        if not self.is_open:
            raise RuntimeError("Scale not open")

        self._conn.reset_input_buffer()
        self._conn.write(b"W")
        self._conn.flush()

        # Drain response (Toledo 8217 sends within ~50ms)
        raw = self._read_until_eot(timeout=0.5)

        match = _MEASURE_RE.search(raw)
        if match:
            try:
                weight = float(match.group(1))
                return WeightReading(ok=True, weight_kg=weight, status=[], raw=raw)
            except ValueError:
                return WeightReading(
                    ok=False,
                    weight_kg=None,
                    status=[f"Bad weight value: {match.group(1)!r}"],
                    raw=raw,
                )

        # Not a weight — try status byte
        status_match = _STATUS_RE.search(raw)
        if status_match:
            status_char = status_match.group(1)
            return WeightReading(
                ok=False,
                weight_kg=None,
                status=self._decode_status_byte(status_char[0]),
                raw=raw,
            )

        return WeightReading(
            ok=False,
            weight_kg=None,
            status=["No response from scale"] if not raw else [f"Unparseable: {raw!r}"],
            raw=raw,
        )

    def probe(self) -> bool:
        """Quick check that the scale answers — sends echo `Ehello`."""
        if not self.is_open:
            return False
        try:
            self._conn.reset_input_buffer()
            self._conn.write(b"Ehello")
            self._conn.flush()
            answer = self._conn.read(8)
            return answer == b"\x02E\rhello"
        except Exception:  # noqa: BLE001
            return False

    # ─── helpers ────────────────────────────────────────────

    def _read_until_eot(self, timeout: float) -> bytes:
        """Drain whatever the scale sent within `timeout`. Toledo 8217
        terminates frames with `\\r` (0x0D)."""
        import time
        deadline = time.monotonic() + timeout
        buf = bytearray()
        while time.monotonic() < deadline:
            chunk = self._conn.read(64)
            if not chunk:
                if buf:
                    break
                continue
            buf.extend(chunk)
            if b"\r" in chunk:
                break
        return bytes(buf)

    @staticmethod
    def _decode_status_byte(b: int) -> list[str]:
        """Convert a Toledo status byte into a list of error labels.

        The 7 LSBs encode error flags; the MSB is parity (ignored).
        """
        out: list[str] = []
        for bit_idx, label in enumerate(_STATUS_ERROR_BITS):
            if b & (1 << bit_idx):
                out.append(label)
        return out
