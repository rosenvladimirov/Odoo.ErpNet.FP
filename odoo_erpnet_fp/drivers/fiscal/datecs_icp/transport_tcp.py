"""
TCP transport for Datecs PM v2.11.4 devices that expose a raw socket
(Topology B — cloud Odoo + printer over LAN/VPN).

Uses synchronous `socket` rather than asyncio so the same call-site works
inside Odoo's sync workers. The frame envelope is identical to serial.
"""

from __future__ import annotations

import socket
import time

from .transport import Transport, TransportClosed, TransportError, TransportTimeout


class TcpTransport(Transport):
    """Raw TCP socket transport.

    Args:
        host: device IP / hostname reachable from Odoo (often via VPN).
        port: device-configured TCP port (model-specific; check user manual).
        connect_timeout: seconds to wait for the initial connect.
    """

    def __init__(
        self,
        host: str,
        port: int,
        connect_timeout: float = 5.0,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._sock: socket.socket | None = None

    def open(self) -> None:
        if self._sock is not None:
            return
        try:
            self._sock = socket.create_connection(
                (self.host, self.port), timeout=self.connect_timeout
            )
            self._sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError as exc:
            self._sock = None
            raise TransportError(
                f"TCP connect to {self.host}:{self.port} failed: {exc}"
            ) from exc

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            finally:
                self._sock = None

    def is_open(self) -> bool:
        return self._sock is not None

    def _require(self) -> socket.socket:
        if self._sock is None:
            raise TransportClosed("TCP socket not connected")
        return self._sock

    def write(self, data: bytes) -> None:
        sock = self._require()
        try:
            sock.sendall(data)
        except socket.timeout as exc:
            raise TransportTimeout(f"TCP write timeout: {exc}") from exc
        except OSError as exc:
            raise TransportError(f"TCP write failed: {exc}") from exc

    def read(self, n: int, timeout: float) -> bytes:
        sock = self._require()
        deadline = time.monotonic() + timeout
        chunks: list[bytes] = []
        remaining = n
        while remaining > 0:
            now = time.monotonic()
            if now >= deadline:
                break
            sock.settimeout(deadline - now)
            try:
                chunk = sock.recv(remaining)
            except socket.timeout:
                break
            except OSError as exc:
                raise TransportError(f"TCP read failed: {exc}") from exc
            if not chunk:
                # peer closed
                raise TransportClosed("TCP peer closed connection")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read_until(
        self, terminator: bytes, max_bytes: int, timeout: float
    ) -> bytes:
        sock = self._require()
        deadline = time.monotonic() + timeout
        if not terminator:
            raise ValueError("terminator must be at least 1 byte")
        buf = bytearray()
        while len(buf) < max_bytes:
            now = time.monotonic()
            if now >= deadline:
                break
            sock.settimeout(deadline - now)
            try:
                chunk = sock.recv(min(256, max_bytes - len(buf)))
            except socket.timeout:
                break
            except OSError as exc:
                raise TransportError(f"TCP read_until failed: {exc}") from exc
            if not chunk:
                raise TransportClosed("TCP peer closed connection")
            buf.extend(chunk)
            if terminator in buf:
                idx = buf.index(terminator) + len(terminator)
                # Some bytes after terminator may have arrived in same recv;
                # we discard those — caller frames one response at a time.
                return bytes(buf[:idx])
        return bytes(buf)
