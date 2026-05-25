"""
TCP barcode reader driver.

Connects to a remote TCP socket that streams newline-delimited barcode
strings. Used for BlueCash-50 Android `ScannerBridgeService` (channel
3, port 9102) and any other "push-only event source" the proxy needs
to ingest.

Wire format expected on the socket (mirror of BridgeService comment):

    <barcode>\\n   eg "3800000000017\\n"

No framing, no metadata, no auth — keep it dumb so any line-buffered
producer (`echo … | nc`, Python socket.sendall, busybox netcat, an
Android service) interoperates.

Lifecycle: like `SerialBarcodeReader`, the connection is held open
for the lifetime of the proxy; an indefinite reconnect loop survives
the producer rebooting (e.g. Android device sleep / WiFi blip).
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from typing import Optional

from .common import BarcodeReader

_RECONNECT_BACKOFF_MAX_S = 5.0
_RECONNECT_HEARTBEAT_S = 30.0
_RECV_BUFFER = 256
_RECV_TIMEOUT_S = 0.5

_logger = logging.getLogger(__name__)


class TcpBarcodeReader(BarcodeReader):
    """Line-based TCP barcode scanner.

    Args:
        reader_id: short id used in API URLs
        host: TCP host (IPv4/IPv6/hostname)
        port: TCP port (e.g. 9102 for BlueCash ScannerBridge)
        terminator: ignored at split time; we accept both CR and LF
            and any pair (CRLF / LFCR) — same lenient handling as
            SerialBarcodeReader for max producer interop
        encoding: how to decode bytes to text (default 'ascii' with
            errors='replace'; barcodes are usually pure ASCII)
        connect_timeout: per-attempt connect timeout (s)
    """

    def __init__(
        self,
        reader_id: str,
        host: str,
        port: int,
        terminator: bytes = b"\n",
        encoding: str = "ascii",
        connect_timeout: float = 5.0,
    ) -> None:
        super().__init__(reader_id)
        self.host = host
        self.port = int(port)
        self.terminator = terminator
        self.encoding = encoding
        self.connect_timeout = float(connect_timeout)
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        # Hook за /readers/<id>/reset — drop текущия сокет и реконектай.
        self._force_reopen_evt = threading.Event()

    # ─── Lifecycle ──────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        # Първоначален connect — failure тук НЕ означава да fail-нем
        # стартирането; _loop() ще влезе в _reopen и ще ретрайва.
        try:
            self._sock = self._open_socket()
        except OSError as exc:
            _logger.warning(
                "TCP reader %r: initial connect to %s:%d failed (%s) — "
                "thread will retry indefinitely",
                self.reader_id, self.host, self.port, exc,
            )
            self._sock = None
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"TcpReader[{self.reader_id}]",
            daemon=True,
        )
        self._running = True
        self._thread.start()
        _logger.info(
            "TCP reader %r started → %s:%d",
            self.reader_id, self.host, self.port,
        )

    def reset(self) -> None:
        """Force the read loop to drop current socket and reconnect."""
        self._force_reopen_evt.set()
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:  # noqa: BLE001
            pass
        _logger.info(
            "TCP reader %r: external reset requested", self.reader_id,
        )

    def stop(self) -> None:
        self._stop_evt.set()
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None
        _logger.info("TCP reader %r stopped", self.reader_id)

    # ─── Internals ──────────────────────────────────────────────────

    def _open_socket(self) -> socket.socket:
        """Open a fresh TCP connection. Raises OSError on failure.

        Sets a short recv timeout so the read loop can periodically
        check stop / force-reopen flags.
        """
        s = socket.create_connection(
            (self.host, self.port), timeout=self.connect_timeout)
        # След connect-а ползваме per-recv timeout — close-ва TIME_WAIT
        # отказите бързо и позволява на loop-а да види stop_evt.
        s.settimeout(_RECV_TIMEOUT_S)
        # TCP_NODELAY → barcode-ите идват малки пакети, no buffering.
        s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return s

    def _reopen(self) -> bool:
        """Close current socket and reopen indefinitely until stop."""
        try:
            if self._sock is not None:
                self._sock.close()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._sock = None

        delay = 1.0
        attempt = 0
        last_error: str = ""
        last_heartbeat = time.monotonic()
        while not self._stop_evt.is_set():
            time.sleep(delay)
            attempt += 1
            try:
                self._sock = self._open_socket()
                _logger.info(
                    "TCP reader %r: connected to %s:%d after %d attempt(s)",
                    self.reader_id, self.host, self.port, attempt,
                )
                return True
            except OSError as exc:
                last_error = str(exc)
                _logger.debug(
                    "TCP reader %r: reopen failed (%s), retrying in %.0fs",
                    self.reader_id, exc, delay,
                )
                now = time.monotonic()
                if now - last_heartbeat >= _RECONNECT_HEARTBEAT_S:
                    _logger.info(
                        "TCP reader %r: still waiting for %s:%d "
                        "(attempt %d, last error: %s)",
                        self.reader_id, self.host, self.port,
                        attempt, last_error,
                    )
                    last_heartbeat = now
                delay = min(delay + 1.0, _RECONNECT_BACKOFF_MAX_S)
        return False

    def _loop(self) -> None:
        buf = bytearray()
        # Ако стартовият connect е fail-нал — влизаме в reopen веднага.
        if self._sock is None and not self._reopen():
            self._running = False
            return
        try:
            while not self._stop_evt.is_set():
                if self._force_reopen_evt.is_set():
                    self._force_reopen_evt.clear()
                    _logger.warning(
                        "TCP reader %r: forced reopen requested",
                        self.reader_id,
                    )
                    buf.clear()
                    if not self._reopen():
                        break
                    continue
                try:
                    chunk = self._sock.recv(_RECV_BUFFER)  # type: ignore[union-attr]
                except socket.timeout:
                    # Очаквано — позволява на loop-а да види stop_evt.
                    continue
                except (OSError, AttributeError) as exc:
                    # AttributeError при self._sock=None по време на reset.
                    _logger.warning(
                        "TCP reader %r: recv failed (%s) — reconnecting",
                        self.reader_id, exc,
                    )
                    buf.clear()
                    if not self._reopen():
                        _logger.info(
                            "TCP reader %r: shutdown during reconnect",
                            self.reader_id,
                        )
                        break
                    continue
                if not chunk:
                    # Peer затвори connection — еквивалент на EOF.
                    _logger.info(
                        "TCP reader %r: peer closed — reconnecting",
                        self.reader_id,
                    )
                    buf.clear()
                    if not self._reopen():
                        break
                    continue
                buf.extend(chunk)
                # Сплитваме на CR/LF (lenient — pair-ите CRLF/LFCR също
                # се обработват, същата логика като SerialBarcodeReader).
                while True:
                    idx_cr = buf.find(b"\r")
                    idx_lf = buf.find(b"\n")
                    candidates = [i for i in (idx_cr, idx_lf) if i >= 0]
                    if not candidates:
                        break
                    idx = min(candidates)
                    raw = bytes(buf[:idx])
                    skip = idx + 1
                    if skip < len(buf) and buf[skip:skip + 1] in (
                            b"\r", b"\n"):
                        skip += 1
                    del buf[:skip]
                    text = raw.decode(
                        self.encoding, errors="replace").strip()
                    if text:
                        self._emit(text, raw=raw)
        except Exception:  # noqa: BLE001
            _logger.exception(
                "TCP reader %r loop crashed", self.reader_id,
            )
        finally:
            self._running = False


__all__ = ["TcpBarcodeReader"]
