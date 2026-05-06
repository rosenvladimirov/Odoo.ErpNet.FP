"""
Datecs DPD-201 pole display driver — and ESC/POS-compatible clones.

Command set is documented in `Pole Display DPD-201 User's Manual` (Datecs).
The same byte sequences work on ICD CD-5220, Birch DSP-V9, and many
other 2x20 VFD pole displays sold under various OEM brand names —
all of them follow the de-facto ESC/POS pole-display protocol.

Hardware notes:
  * 2 lines × 20 chars, 5×7 dot matrix VFD (blue-green 505 nm)
  * Default 9600 8N1 (jumper-selectable: 2400 / 4800 / 9600 / 19200)
  * 4 brightness levels (40 / 60 / 80 / 100 %)
  * PC437 (Latin) or PC850 (multilingual Latin-1) — switch #6 on power-up
  * For Bulgarian Cyrillic: configure jumpers to "Work with DATECS ECR"
    mode + use encoding="cp1251" — the Datecs ECR firmware variant
    accepts cp1251 bytes directly.

Protocol bytes (from manual §3):
  0x0C            CLR             — clear screen, cursor → home
  0x18            CAN             — clear current line
  0x1B 0x40       ESC @           — initialize (full reset)
  0x1B 0x52 n     ESC R n         — international charset (0=USA, ..., 0x0A)
  0x1B 0x74 n     ESC t n         — code table (0=PC437, 1=PC850)
  0x1B 0x48 n     ESC H n         — cursor → position 0..39
  0x1B 0x7A       ESC z           — reset annunciators
  0x1F 0x01       US MD1          — overwrite mode (default)
  0x1F 0x02       US MD2          — vertical scroll mode
  0x1F 0x03       US MD3          — horizontal scroll mode
  0x1F 0x24 c r   US $ c r        — cursor → (col c, row r), 1-based
  0x1F 0x40       US @            — self-test (~20 s blocking on device)
  0x1F 0x42       US B            — cursor → right-end of bottom line
  0x1F 0x45 n     US E n          — blink interval (n × 13ms; 0=steady, 0xFF=off)
  0x1F 0x58 n     US X n          — brightness 1..4 (40/60/80/100 %)
  0x1F 0x23 n m   US # n m        — annunciator at column m on/off
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

try:
    import serial
except ImportError:  # pragma: no cover
    serial = None

from .common import CustomerDisplay, DisplayCapabilities

_logger = logging.getLogger(__name__)


# Control-byte constants
_CLR = b"\x0c"
_CAN = b"\x18"
_ESC = b"\x1b"
_US = b"\x1f"


class DatecsDpd201(CustomerDisplay):
    """Datecs DPD-201 (and ESC/POS-compatible 2×20 VFD pole displays).

    Args:
        display_id: short id used in /displays/{id} URLs
        port: serial port path (e.g. /dev/ttyUSB1)
        baudrate: must match jumper setting (default 9600)
        encoding: text codec — cp437 / cp850 / cp1251 (BG ECR mode)
        chars_per_line: 20 (DPD-201 is fixed; clones may differ)
        lines: 2 (DPD-201 is fixed)
    """

    capabilities = DisplayCapabilities(
        chars_per_line=20,
        lines=2,
        brightness_levels=4,
        supports_blink=True,
        supports_cursor=True,
        supports_annunciators=True,
        encoding="cp437",
    )

    def __init__(
        self,
        display_id: str,
        port: str,
        baudrate: int = 9600,
        encoding: str = "cp437",
        chars_per_line: int = 20,
        lines: int = 2,
    ) -> None:
        super().__init__(display_id)
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        self.port = port
        self.baudrate = baudrate
        self.encoding = encoding
        self.capabilities = DisplayCapabilities(
            chars_per_line=chars_per_line,
            lines=lines,
            brightness_levels=4,
            supports_blink=True,
            supports_cursor=True,
            supports_annunciators=True,
            encoding=encoding,
        )
        self._conn: Optional["serial.Serial"] = None
        self._lock = threading.Lock()

    def open(self) -> None:
        if self._conn is not None:
            return
        self._conn = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.5,
            write_timeout=2.0,
        )
        # Power-on jitter — devices fail to receive the first byte
        # when the host writes too fast after open().
        self._send(_ESC + b"@")
        _logger.info(
            "DPD-201 %r opened on %s @ %d (encoding=%s)",
            self.display_id, self.port, self.baudrate, self.encoding,
        )

    def close(self) -> None:
        if self._conn is None:
            return
        try:
            self._conn.close()
        finally:
            self._conn = None
        _logger.info("DPD-201 %r closed", self.display_id)

    # ─── Commands ────────────────────────────────────────────────

    def clear(self) -> None:
        self._send(_CLR)

    def clear_line(self) -> None:
        self._send(_CAN)

    def initialize(self) -> None:
        self._send(_ESC + b"@")

    def select_code_table(self, n: int) -> None:
        """0 = PC437, 1 = PC850. Default depends on switch #6."""
        if n not in (0, 1):
            raise ValueError("code table must be 0 or 1")
        self._send(_ESC + b"t" + bytes([n]))

    def select_international_charset(self, n: int) -> None:
        """0..10 — see DPD-201 manual table 2."""
        if not 0 <= n <= 10:
            raise ValueError("international charset must be 0..10")
        self._send(_ESC + b"R" + bytes([n]))

    def set_cursor(self, col: int, row: int) -> None:
        """1-based. col 1..20, row 1..2."""
        cap = self.capabilities
        if not 1 <= col <= cap.chars_per_line:
            raise ValueError(f"col must be 1..{cap.chars_per_line}")
        if not 1 <= row <= cap.lines:
            raise ValueError(f"row must be 1..{cap.lines}")
        self._send(_US + b"$" + bytes([col, row]))

    def set_cursor_position(self, n: int) -> None:
        """Linear position 0..(cols*lines - 1)."""
        max_pos = self.capabilities.chars_per_line * self.capabilities.lines - 1
        if not 0 <= n <= max_pos:
            raise ValueError(f"position must be 0..{max_pos}")
        self._send(_ESC + b"H" + bytes([n]))

    def set_brightness(self, level: int) -> None:
        """0 = off (blank), 1..4 = 40/60/80/100 %."""
        if level == 0:
            # 0xFF on US E = display off, contents preserved
            self._send(_US + b"E" + b"\xff")
            return
        if not 1 <= level <= 4:
            raise ValueError("brightness must be 0..4")
        # Cancel any blink / off mode first, then set brightness
        self._send(_US + b"E" + b"\x00")
        self._send(_US + b"X" + bytes([level]))

    def set_blink(self, n: int) -> None:
        """0 = steady, 1..254 = blink (n × 13 ms cycle), 255 = off."""
        if not 0 <= n <= 255:
            raise ValueError("blink must be 0..255")
        self._send(_US + b"E" + bytes([n]))

    def set_annunciator(self, column: int, on: bool) -> None:
        """column 1..20; column 0 = all annunciators (broadcast)."""
        if not 0 <= column <= self.capabilities.chars_per_line:
            raise ValueError(
                f"annunciator column must be 0..{self.capabilities.chars_per_line}"
            )
        self._send(_US + b"#" + bytes([1 if on else 0, column]))

    def reset_annunciators(self) -> None:
        self._send(_ESC + b"z")

    def self_test(self) -> None:
        """Triggers the on-device self-test routine (blocks the device
        for ~20 s — driver does NOT wait, returns immediately)."""
        self._send(_US + b"@")

    # ─── Text output ────────────────────────────────────────────

    def display_line(self, line: int, text: str) -> None:
        if not 1 <= line <= self.capabilities.lines:
            raise ValueError(f"line must be 1..{self.capabilities.lines}")
        self.set_cursor(1, line)
        self.clear_line()
        self.set_cursor(1, line)
        self._write_text(text)

    def display_two_lines(self, top: str, bottom: str) -> None:
        self.clear()
        self.set_cursor(1, 1)
        self._write_text(top)
        self.set_cursor(1, 2)
        self._write_text(bottom)

    # ─── Internals ──────────────────────────────────────────────

    def _write_text(self, text: str) -> None:
        cap = self.capabilities
        truncated = text[: cap.chars_per_line]
        self._send(truncated.encode(self.encoding, errors="replace"))

    def _send(self, payload: bytes) -> None:
        if self._conn is None:
            raise RuntimeError(f"DPD-201 {self.display_id!r} not open")
        with self._lock:
            self._conn.write(payload)
            self._conn.flush()
