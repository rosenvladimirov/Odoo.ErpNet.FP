"""
Serial barcode reader driver — RS232 / USB-CDC.

Industrial scanners (Cognex, Datalogic Matrix, Newland NLS-FM430) often
expose a serial port that emits each barcode as a UTF-8 / ASCII line
terminated by CR/LF. Default settings are 9600 8N1 (override per
device).
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

try:
    import serial
except ImportError:  # pragma: no cover
    serial = None

from .common import BarcodeReader

_logger = logging.getLogger(__name__)


class SerialBarcodeReader(BarcodeReader):
    """Line-based serial barcode scanner.

    Args:
        reader_id: short id used in API URLs
        port: serial port path
        baudrate: per device manual; default 9600
        terminator: byte sequence at end of barcode (default CR or LF)
        encoding: how to decode bytes to text (default 'ascii' with
            errors='replace'; barcodes are usually pure ASCII)
    """

    def __init__(
        self,
        reader_id: str,
        port: str,
        baudrate: int = 9600,
        terminator: bytes = b"\r",
        encoding: str = "ascii",
    ) -> None:
        super().__init__(reader_id)
        if serial is None:
            raise RuntimeError("pyserial is not installed")
        self.port = port
        self.baudrate = baudrate
        self.terminator = terminator
        self.encoding = encoding
        self._conn: Optional["serial.Serial"] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

    def start(self) -> None:
        if self._running:
            return
        self._conn = serial.Serial(
            port=self.port,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=0.5,
        )
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"SerialReader[{self.reader_id}]",
            daemon=True,
        )
        self._running = True
        self._thread.start()
        _logger.info(
            "Serial reader %r started on %s @ %d",
            self.reader_id, self.port, self.baudrate,
        )

    def stop(self) -> None:
        self._stop_evt.set()
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._conn is not None:
            try:
                self._conn.close()
            finally:
                self._conn = None
        _logger.info("Serial reader %r stopped", self.reader_id)

    def _loop(self) -> None:
        buf = bytearray()
        try:
            while not self._stop_evt.is_set():
                chunk = self._conn.read(64)  # type: ignore[union-attr]
                if not chunk:
                    continue
                buf.extend(chunk)
                # Split on terminator(s); accept both CR and LF
                while True:
                    idx_cr = buf.find(b"\r")
                    idx_lf = buf.find(b"\n")
                    candidates = [i for i in (idx_cr, idx_lf) if i >= 0]
                    if not candidates:
                        break
                    idx = min(candidates)
                    raw = bytes(buf[:idx])
                    # Skip the matched terminator byte AND any companion
                    # one immediately following (CRLF / LFCR pairs)
                    skip = idx + 1
                    if skip < len(buf) and buf[skip:skip + 1] in (b"\r", b"\n"):
                        skip += 1
                    del buf[:skip]
                    text = raw.decode(self.encoding, errors="replace").strip()
                    if text:
                        self._emit(text, raw=raw)
        except Exception:  # noqa: BLE001
            _logger.exception(
                "Serial reader %r loop crashed", self.reader_id
            )
        finally:
            self._running = False
