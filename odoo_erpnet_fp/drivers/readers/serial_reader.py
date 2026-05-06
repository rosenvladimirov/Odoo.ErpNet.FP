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
import time
from typing import Optional

try:
    import serial
except ImportError:  # pragma: no cover
    serial = None

from .common import BarcodeReader

# Reconnect retries do not give up — a sleeping BLE scanner can take
# minutes to wake up and the user expects "scan to resume" to just
# work without manually restarting the proxy. The retry loop emits a
# heartbeat log every _RECONNECT_HEARTBEAT_S seconds so the operator
# can see we are still trying instead of silently spinning.
_RECONNECT_BACKOFF_MAX_S = 5.0
_RECONNECT_HEARTBEAT_S = 30.0

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
        # Set externally (e.g. by /readers/{id}/reset) to force the
        # read loop to drop its current fd and re-enter _reopen — even
        # if pyserial isn't yet seeing an I/O error. Useful when the
        # operator (via tray button) knows the daemon was restarted
        # and wants to short-circuit the auto-detection.
        self._force_reopen_evt = threading.Event()

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

    def reset(self) -> None:
        """Public hook — force the read loop to drop its current fd
        and reopen the port from scratch. Called from
        POST /readers/{id}/reset (tray menu, ops dashboard button).
        Safe to call concurrently; the loop checks the flag on its
        next read iteration and re-enters _reopen.
        """
        self._force_reopen_evt.set()
        # Also close the underlying fd so any blocked read returns
        # immediately with SerialException, which the loop then
        # treats as a normal disconnect.
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        _logger.info(
            "Serial reader %r: external reset requested", self.reader_id,
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

    def _reopen(self) -> bool:
        """Close current serial and reopen it, retrying indefinitely.

        Returns True when the port is reopened, False only if we are
        asked to stop. Never gives up on its own — a BLE scanner
        sleeping for hours and then waking up still resumes scanning
        without operator intervention.

        Logging strategy: each retry is DEBUG (so it's available when
        diagnosing) but every _RECONNECT_HEARTBEAT_S we also emit a
        single INFO heartbeat with attempt count + last error, so the
        operator sees a clear "still waiting" trail in the regular log.
        """
        try:
            if self._conn is not None:
                self._conn.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._conn = None

        delay = 1.0
        attempt = 0
        last_error: str = ""
        last_heartbeat = time.monotonic()
        while not self._stop_evt.is_set():
            time.sleep(delay)
            attempt += 1
            try:
                self._conn = serial.Serial(
                    port=self.port,
                    baudrate=self.baudrate,
                    bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE,
                    stopbits=serial.STOPBITS_ONE,
                    timeout=0.5,
                )
                _logger.info(
                    "Serial reader %r: reconnected to %s after %d attempt(s)",
                    self.reader_id, self.port, attempt,
                )
                return True
            except (serial.SerialException, OSError) as exc:
                last_error = str(exc)
                _logger.debug(
                    "Serial reader %r: reopen failed (%s), retrying in %.0fs",
                    self.reader_id, exc, delay,
                )
                now = time.monotonic()
                if now - last_heartbeat >= _RECONNECT_HEARTBEAT_S:
                    _logger.info(
                        "Serial reader %r: still waiting for %s "
                        "(attempt %d, last error: %s)",
                        self.reader_id, self.port, attempt, last_error,
                    )
                    last_heartbeat = now
                delay = min(delay + 1.0, _RECONNECT_BACKOFF_MAX_S)
        return False

    def _loop(self) -> None:
        buf = bytearray()
        try:
            while not self._stop_evt.is_set():
                if self._force_reopen_evt.is_set():
                    self._force_reopen_evt.clear()
                    _logger.warning(
                        "Serial reader %r: forced reopen requested — reconnecting",
                        self.reader_id,
                    )
                    buf.clear()
                    if not self._reopen():
                        break
                    continue
                try:
                    chunk = self._conn.read(64)  # type: ignore[union-attr]
                except (serial.SerialException, OSError, TypeError, AttributeError) as exc:
                    # TypeError / AttributeError happen when an external
                    # caller closes self._conn while we're blocked in
                    # read() (pyserial sets self.fd = None during close).
                    # Treat it the same as a normal disconnect and
                    # re-enter the reopen loop.
                    _logger.warning(
                        "Serial reader %r: read failed (%s) — reconnecting",
                        self.reader_id, exc,
                    )
                    buf.clear()  # drop partial frame; new pty starts fresh
                    if not self._reopen():
                        # _reopen returned False only because we were
                        # asked to stop — this is clean shutdown, not
                        # an error.
                        _logger.info(
                            "Serial reader %r: shutdown requested during reconnect",
                            self.reader_id,
                        )
                        break
                    continue
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
