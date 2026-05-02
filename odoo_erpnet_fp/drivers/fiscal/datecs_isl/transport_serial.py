"""
RS232 / USB-CDC transport via pyserial (Topology A).

Uses 8N1 framing, no flow control. Baud rates per PROTOCOL_REFERENCE.md:
1200 / 2400 / 4800 / 9600 / 19200 / 38400 / 57600 / 115200.
"""

from __future__ import annotations

import time

from .transport import Transport, TransportClosed, TransportError, TransportTimeout

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover
    serial = None


class SerialTransport(Transport):
    """Pyserial-backed transport.

    Args:
        port: device path (e.g. '/dev/ttyUSB0' on Linux, 'COM3' on Windows).
        baudrate: must match the device configuration.
        write_timeout: seconds before write() raises.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = 115200,
        write_timeout: float = 1.0,
    ) -> None:
        if serial is None:
            raise RuntimeError(
                "pyserial is not installed. "
                "`pip install pyserial>=3.5` to use SerialTransport."
            )
        self.port = port
        self.baudrate = baudrate
        self.write_timeout = write_timeout
        self._ser: serial.Serial | None = None

    def open(self) -> None:
        if self._ser is not None and self._ser.is_open:
            return
        self._ser = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0,
            write_timeout=self.write_timeout,
            rtscts=False,
            dsrdtr=False,
            xonxoff=False,
        )

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            finally:
                self._ser = None

    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def _require(self) -> "serial.Serial":
        if self._ser is None or not self._ser.is_open:
            raise TransportClosed("Serial port not open")
        return self._ser

    def write(self, data: bytes) -> None:
        ser = self._require()
        try:
            written = ser.write(data)
        except serial.SerialTimeoutException as exc:
            raise TransportTimeout(f"Serial write timeout: {exc}") from exc
        except serial.SerialException as exc:
            raise TransportError(f"Serial write failed: {exc}") from exc
        if written != len(data):
            raise TransportError(
                f"Short write: {written} of {len(data)} bytes"
            )

    def read(self, n: int, timeout: float) -> bytes:
        ser = self._require()
        ser.timeout = timeout
        return ser.read(n)

    def read_until_byte(self, terminator: int, max_bytes: int,
                        timeout: float) -> bytes:
        """Read bytes until `terminator` is seen or `max_bytes` reached.
        Returns AS SOON AS terminator arrives — the key property the
        ISL protocol layer needs to avoid eating the full 5s timeout
        on every device round-trip.
        """
        ser = self._require()
        ser.timeout = timeout
        buf = ser.read_until(bytes([terminator]), size=max_bytes)
        return buf

    def read_until(
        self, terminator: bytes, max_bytes: int, timeout: float
    ) -> bytes:
        ser = self._require()
        ser.timeout = timeout
        # pyserial's read_until honours the device timeout for the duration
        return ser.read_until(terminator, size=max_bytes)
