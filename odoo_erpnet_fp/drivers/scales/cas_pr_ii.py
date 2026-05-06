"""
CAS PR-II / PD-II RS-232 protocol — and CAS-compatible clones.

The same protocol covers a large slice of the Bulgarian retail counter-
scale market:

  * Native CAS scales — PR-II, PD-II, PSD, PDS, PR-C, PD-I family
  * Elicom EVL / S300 in "CASH47" jumper mode (CAS-compatible)
  * Datecs scales in "CAS" protocol mode (selectable via menu)

Protocol (from CAS PR-II User Manual, ch. 6):
    Serial settings: 9600 8N1 (no flow control), 3-wire (TX/RX/GND)

    Request flow (PC → scale):
        1. PC sends ENQ (0x05)
        2. Scale replies ACK (0x06)
        3. PC sends DC1 (0x11) to request weight
        4. Scale replies with a 15-byte data frame

    Response frame (15 bytes):
        SOH STX STA SIGN W5 W4 W3 W2 W1 W0 UN1 UN2 BCC ETX EOT
        01h 02h ... ... 6 ASCII digits ... K  G  XOR 03h 04h

    STA  : 'S' (0x53) = stable, 'U' (0x55) = unstable
    SIGN : '-' (0x2D) negative, ' ' (0x20) positive
    W5..0: 6 ASCII digits — weight × 1000 (units = grams when KG)
    UN1+UN2: "KG" or "LB" (sometimes "kg" / "lb" / " g")
    BCC  : XOR of all bytes from STA to UN2 (inclusive)

The driver enforces a stable-only read: if STA = 'U' it retries up to
`max_retries` times with `retry_delay` between attempts. If still
unstable, returns ok=False with status reflecting the unstable flag —
matches the contract used by `Toledo8217Scale`.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

try:
    import serial
except ImportError:  # pragma: no cover
    serial = None

from .toledo_8217 import WeightReading

_logger = logging.getLogger(__name__)

_ENQ = 0x05
_ACK = 0x06
_DC1 = 0x11
_SOH = 0x01
_STX = 0x02
_ETX = 0x03
_EOT = 0x04


class CasPrIIScale:
    """CAS PR-II + CAS-compatible scale driver.

    Aliases registered in ScaleRegistry: ``cas``, ``cas.pr2``, ``cas.pd2``,
    ``elicom.cash47``, ``datecs.cas`` — same code path. Pick the alias
    that documents your hardware best in the YAML.
    """

    BAUDRATE = 9600
    BYTESIZE = 8
    PARITY = "N"
    STOPBITS = 1
    TIMEOUT = 1.0

    def __init__(
        self,
        port: str,
        baudrate: int = BAUDRATE,
        max_retries: int = 5,
        retry_delay: float = 0.15,
    ) -> None:
        if serial is None:
            raise RuntimeError(
                "pyserial is not installed. `pip install pyserial>=3.5`."
            )
        self.port = port
        self.baudrate = baudrate
        self.max_retries = max_retries
        self.retry_delay = retry_delay
        self._conn: Optional["serial.Serial"] = None

    def open(self) -> None:
        if self._conn is not None and self._conn.is_open:
            return
        self._conn = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
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

    def __enter__(self) -> "CasPrIIScale":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._conn is not None and self._conn.is_open

    # ─── public API ─────────────────────────────────────────

    def read_weight(self) -> WeightReading:
        """Read current stable weight. Retries on STA='U' (unstable)."""
        if not self.is_open:
            raise RuntimeError("Scale not open")

        last_raw = b""
        for attempt in range(self.max_retries):
            try:
                raw = self._exchange()
            except Exception as exc:  # noqa: BLE001
                _logger.warning(
                    "CAS exchange failed on %s (attempt %d/%d): %s",
                    self.port, attempt + 1, self.max_retries, exc,
                )
                last_raw = b""
                time.sleep(self.retry_delay)
                continue
            last_raw = raw
            parsed = self._parse(raw)
            if parsed.ok:
                return parsed
            # Unstable → retry; other errors → give up immediately
            if parsed.status != ["Scale unstable"]:
                return parsed
            time.sleep(self.retry_delay)

        return WeightReading(
            ok=False,
            weight_kg=None,
            status=["Scale unstable"],
            raw=last_raw,
        )

    def probe(self) -> bool:
        """Quick liveness check — send ENQ, expect ACK within timeout."""
        if not self.is_open:
            return False
        try:
            self._conn.reset_input_buffer()
            self._conn.write(bytes([_ENQ]))
            self._conn.flush()
            ack = self._conn.read(1)
            return ack == bytes([_ACK])
        except Exception:  # noqa: BLE001
            return False

    # ─── internals ──────────────────────────────────────────

    def _exchange(self) -> bytes:
        """Run one ENQ/ACK/DC1 → frame round-trip. Returns raw 15 bytes."""
        self._conn.reset_input_buffer()
        self._conn.write(bytes([_ENQ]))
        self._conn.flush()
        ack = self._conn.read(1)
        if not ack:
            raise IOError("No ACK from scale (no reply)")
        if ack[0] != _ACK:
            raise IOError(f"Expected ACK 0x06, got 0x{ack[0]:02x}")
        self._conn.write(bytes([_DC1]))
        self._conn.flush()
        # Frame is exactly 15 bytes. Read with a timeout cushion.
        raw = self._conn.read(15)
        if len(raw) < 15:
            # Some clones (Elicom in CASH47) drop SOH and emit only 14 bytes.
            # Try to compensate by prepending SOH if the frame starts with STX.
            if raw and raw[0] == _STX:
                raw = bytes([_SOH]) + raw
            else:
                raise IOError(
                    f"Short frame ({len(raw)} bytes): {raw.hex()}"
                )
        return raw

    @staticmethod
    def _parse(raw: bytes) -> WeightReading:
        if len(raw) != 15 or raw[0] != _SOH or raw[1] != _STX:
            return WeightReading(
                ok=False,
                weight_kg=None,
                status=[f"Bad framing: {raw.hex()}"],
                raw=raw,
            )
        if raw[13] != _ETX or raw[14] != _EOT:
            return WeightReading(
                ok=False,
                weight_kg=None,
                status=[f"Bad terminators: ETX=0x{raw[13]:02x} EOT=0x{raw[14]:02x}"],
                raw=raw,
            )
        sta = raw[2]
        sign = raw[3]
        digits = raw[4:10].decode("ascii", errors="replace")
        unit = raw[10:12].decode("ascii", errors="replace").strip().lower()
        bcc_recv = raw[12]
        bcc_calc = 0
        for b in raw[2:12]:
            bcc_calc ^= b

        if bcc_calc != bcc_recv:
            return WeightReading(
                ok=False,
                weight_kg=None,
                status=[f"BCC mismatch: 0x{bcc_calc:02x} vs 0x{bcc_recv:02x}"],
                raw=raw,
            )

        if sta == 0x55:  # 'U' = unstable
            return WeightReading(
                ok=False, weight_kg=None, status=["Scale unstable"], raw=raw,
            )
        if sta != 0x53:  # not 'S' either
            return WeightReading(
                ok=False,
                weight_kg=None,
                status=[f"Bad STA byte: 0x{sta:02x}"],
                raw=raw,
            )

        try:
            grams = int(digits)
        except ValueError:
            return WeightReading(
                ok=False,
                weight_kg=None,
                status=[f"Non-numeric weight digits: {digits!r}"],
                raw=raw,
            )
        sign_mul = -1 if sign == 0x2D else 1
        weight = sign_mul * grams / 1000.0  # 6 ASCII digits = grams (× 0.001 = kg)

        # Pound output? convert to kg (1 lb = 0.45359237 kg). Default = kg.
        if unit.startswith("l"):
            weight = weight * 0.45359237

        return WeightReading(
            ok=True,
            weight_kg=weight,
            status=[],
            raw=raw,
        )
