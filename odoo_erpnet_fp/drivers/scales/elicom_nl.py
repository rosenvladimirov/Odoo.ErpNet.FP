"""
Elicom NL indicator driver — continuous broadcast protocol.

The NL indicator (platform scales EEP xxx N / NL, e.g. EEP60-2-1-NL-X)
streams weight frames over RS232 at ~80 Hz and accepts no commands —
the host listens passively. Frame format (9600 8N1, CR-terminated,
no LF):

    t+000.000\\r     zero / empty platform
    p+000.900\\r     load on the platform (0.900 kg)

One leading status letter, a sign, and a fixed-point value in
kilograms. Letters observed on a live EEP60-2-1-NL-X: ``t`` at zero,
``p`` under load. Unknown letters still parse — the letter is carried
in the reading status so a new firmware variant surfaces instead of
failing silently.

Stability is judged from the value stream, not from the status letter:
a reading is stable once STABLE_RUN consecutive frames carry the same
value (~60 ms at 80 Hz). This works regardless of what the
(undocumented) letters mean during motion.
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

# "t+000.000" / "p+012.345" — one optional letter, mandatory sign,
# fixed-point kilograms. Digit counts are left loose: other capacities
# of the same indicator family shift the decimal point.
_FRAME_RE = re.compile(
    rb"^(?P<flag>[A-Za-z])?(?P<sign>[+\-])(?P<num>\d{1,4}(?:\.\d{1,4})?)$"
)

# Consecutive identical values that count as a stable reading.
STABLE_RUN = 5


class ElicomNlScale:
    """Passive listener for the Elicom NL continuous frame stream."""

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

    def __enter__(self) -> "ElicomNlScale":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._conn is not None and self._conn.is_open

    # ─── public API ─────────────────────────────────────────

    def read_weight(self) -> WeightReading:
        """Listen until STABLE_RUN identical frames arrive, or time out.

        The stream never stops, so "no data" genuinely means a dead
        port/cable, and "unstable" means the value kept changing for
        the whole window.
        """
        if not self.is_open:
            raise RuntimeError("Scale not open")

        self._conn.reset_input_buffer()
        deadline = time.monotonic() + self.read_timeout
        buf = bytearray()
        last_value: Optional[float] = None
        last_flag = ""
        run = 0
        saw_frame = False

        while time.monotonic() < deadline:
            chunk = self._conn.read(64)
            if not chunk:
                continue
            buf.extend(chunk)
            while True:
                # Кадрите завършват на голо CR; приемаме и CRLF.
                idx = buf.find(b"\r")
                if idx < 0:
                    break
                frame = bytes(buf[:idx])
                del buf[: idx + 1]
                if buf[:1] == b"\n":
                    del buf[:1]
                parsed = self._parse_frame(frame)
                if parsed is None:
                    continue
                value, flag = parsed
                saw_frame = True
                if value == last_value:
                    run += 1
                else:
                    last_value = value
                    last_flag = flag
                    run = 1
                if run >= STABLE_RUN:
                    return WeightReading(
                        ok=True,
                        weight_kg=value,
                        status=[f"flag:{flag}"] if flag not in ("t", "p") else [],
                        raw=frame,
                    )

        if saw_frame:
            return WeightReading(
                ok=False,
                weight_kg=None,
                status=["Scale unstable", f"flag:{last_flag}"],
                raw=bytes(buf),
            )
        return WeightReading(
            ok=False,
            weight_kg=None,
            status=["No data from scale"],
            raw=bytes(buf),
        )

    def probe(self) -> bool:
        """Liveness check — true if ANY parseable frame arrived in 1 s."""
        if not self.is_open:
            return False
        try:
            self._conn.reset_input_buffer()
            time.sleep(1.0)
            data = self._conn.read(256)
            for piece in data.split(b"\r"):
                if _FRAME_RE.match(piece.strip(b"\n")):
                    return True
            return False
        except Exception:  # noqa: BLE001
            return False

    # ─── internals ──────────────────────────────────────────

    @staticmethod
    def _parse_frame(frame: bytes) -> Optional[tuple[float, str]]:
        m = _FRAME_RE.match(frame)
        if m is None:
            return None
        try:
            value = float(m.group("num"))
        except (TypeError, ValueError):
            return None
        if m.group("sign") == b"-":
            value = -value
        flag = (m.group("flag") or b"").decode("ascii", errors="replace")
        return value, flag
