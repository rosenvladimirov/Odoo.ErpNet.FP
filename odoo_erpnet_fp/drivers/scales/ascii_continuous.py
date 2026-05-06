"""
Generic ASCII continuous-mode scale driver.

Many cheap retail counter scales (ACS 6/15, ACS 15/30, JCS, no-name
Chinese OEM) don't implement request-response — they just stream weight
readings out the serial port at ~10 Hz. The host listens passively and
returns the most recent stable reading.

Recognised line formats (auto-detected; first match wins):

  Mettler standard           "ST,GS,+    1.234 kg\\r\\n"
  Mettler unstable           "US,GS,+    1.234 kg\\r\\n"
  Generic signed             "+    1.234 kg\\r\\n"
  Generic with status prefix "S  +    1.234 kg\\r\\n"   (S=stable / U=unstable)
  Toledo-like ASCII          "  1.234 kg\\r"
  Bare grams                 "1234 g\\r\\n"

Default settings: 9600 8N1, no flow control. Override per device.

The driver opens the port, listens for `read_timeout` seconds, returns
the last stable reading observed. If only unstable readings arrive
within the window, returns ok=False with status=["Scale unstable"].
"""

from __future__ import annotations

import logging
import re
import time
from typing import Optional

try:
    import serial
except ImportError:  # pragma: no cover
    serial = None

from .toledo_8217 import WeightReading

_logger = logging.getLogger(__name__)

# Permissive line regex: optional status prefix, optional sign, digits,
# optional decimal, digits, optional unit.
_LINE_RE = re.compile(
    rb"""
    (?:^|[\x02\s])                          # frame start or whitespace
    (?:(?P<status>S[T]?|U[S]?|ST|US)[,\s]+(?:GS|NT)[,\s]+)?  # opt status
    (?P<sign>[+\-])?\s*                     # opt sign
    (?P<num>\d+(?:\.\d+)?)\s*               # number
    (?P<unit>kg|g|lb|oz)?\s*                # opt unit
    \r?\n?
    """,
    re.IGNORECASE | re.VERBOSE,
)


class AsciiContinuousScale:
    """Listens for ASCII weight frames; returns most recent stable read."""

    BAUDRATE = 9600
    BYTESIZE = 8
    PARITY = "N"
    STOPBITS = 1

    def __init__(
        self,
        port: str,
        baudrate: int = BAUDRATE,
        read_timeout: float = 1.5,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
    ) -> None:
        if serial is None:
            raise RuntimeError(
                "pyserial is not installed. `pip install pyserial>=3.5`."
            )
        self.port = port
        self.baudrate = baudrate
        self.read_timeout = read_timeout
        self._bytesize_arg = bytesize
        self._parity_arg = parity
        self._stopbits_arg = stopbits
        self._conn: Optional["serial.Serial"] = None

    def open(self) -> None:
        if self._conn is not None and self._conn.is_open:
            return
        bytesize_map = {7: serial.SEVENBITS, 8: serial.EIGHTBITS}
        parity_map = {
            "N": serial.PARITY_NONE,
            "E": serial.PARITY_EVEN,
            "O": serial.PARITY_ODD,
        }
        stopbits_map = {1: serial.STOPBITS_ONE, 2: serial.STOPBITS_TWO}
        self._conn = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=bytesize_map.get(self._bytesize_arg, serial.EIGHTBITS),
            parity=parity_map.get(self._parity_arg.upper(), serial.PARITY_NONE),
            stopbits=stopbits_map.get(self._stopbits_arg, serial.STOPBITS_ONE),
            timeout=0.1,
        )

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None

    def __enter__(self) -> "AsciiContinuousScale":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._conn is not None and self._conn.is_open

    # ─── public API ─────────────────────────────────────────

    def read_weight(self) -> WeightReading:
        """Listen for `read_timeout` seconds, return last stable reading.

        Falls back to last unstable reading if none was stable.
        """
        if not self.is_open:
            raise RuntimeError("Scale not open")

        self._conn.reset_input_buffer()
        deadline = time.monotonic() + self.read_timeout
        buf = bytearray()
        last_stable: Optional[WeightReading] = None
        last_any: Optional[WeightReading] = None

        while time.monotonic() < deadline:
            chunk = self._conn.read(64)
            if chunk:
                buf.extend(chunk)
                # Parse complete lines (split on \n, leave trailing partial in buf)
                while True:
                    nl = buf.find(b"\n")
                    if nl < 0:
                        break
                    line = bytes(buf[: nl + 1])
                    del buf[: nl + 1]
                    parsed = self._parse_line(line)
                    if parsed is None:
                        continue
                    last_any = parsed
                    if parsed.ok:
                        last_stable = parsed
                        # one stable read is enough; no need to wait full timeout
                        return last_stable

        if last_stable is not None:
            return last_stable
        if last_any is not None:
            return last_any
        return WeightReading(
            ok=False, weight_kg=None,
            status=["No data from scale"], raw=bytes(buf),
        )

    def probe(self) -> bool:
        """Liveness check — true if ANY parseable frame arrived in 1 s."""
        if not self.is_open:
            return False
        try:
            self._conn.reset_input_buffer()
            time.sleep(1.0)
            data = self._conn.read(256)
            return bool(_LINE_RE.search(data))
        except Exception:  # noqa: BLE001
            return False

    # ─── internals ──────────────────────────────────────────

    @staticmethod
    def _parse_line(line: bytes) -> Optional[WeightReading]:
        m = _LINE_RE.search(line)
        if m is None:
            return None
        try:
            num = float(m.group("num"))
        except (TypeError, ValueError):
            return None
        sign = m.group("sign")
        if sign and sign == b"-":
            num = -num
        unit = (m.group("unit") or b"").decode("ascii", errors="replace").lower()
        if unit == "g":
            num = num / 1000.0
        elif unit == "lb":
            num = num * 0.45359237
        elif unit == "oz":
            num = num * 0.028349523125
        # else: kg or unspecified — assume kg

        status_raw = m.group("status")
        status = (status_raw or b"").decode("ascii", errors="replace").upper()
        # Common patterns: "ST"=stable, "US"=unstable. "S"/"U" alone too.
        unstable = status.startswith("U")
        if unstable:
            return WeightReading(
                ok=False, weight_kg=None,
                status=["Scale unstable"], raw=line,
            )
        return WeightReading(
            ok=True, weight_kg=num, status=[], raw=line,
        )
