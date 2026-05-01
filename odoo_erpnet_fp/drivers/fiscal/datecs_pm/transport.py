"""
Transport ABC for Datecs PM v2.11.4 driver.

Implementations (`transport_serial`, `transport_tcp`, `transport_agent`) are
equivalent and interchangeable. The frame layer never imports them and never
checks instance type — it only calls `send_frame` / `recv_byte` / `recv_frame`.

Three topologies map to three implementations:
  serial  — direct RS232/USB on host running Odoo (Topology A)
  tcp     — device exposes raw TCP port over LAN/VPN (Topology B)
  agent   — remote IoT agent forwards over HTTP+JWT (Topology C)
"""

from abc import ABC, abstractmethod


class TransportError(Exception):
    """Base for transport-level errors (timeout, connection lost, etc.)."""


class TransportTimeout(TransportError):
    """Receive timed out before slave responded."""


class TransportClosed(TransportError):
    """Underlying connection closed unexpectedly."""


class Transport(ABC):
    """Stateless wire transport for the framing layer.

    Implementations must be safe to use serially (one outstanding frame at a
    time). Concurrent access is the caller's responsibility — typically the
    caller serializes through Odoo's per-device record lock.
    """

    @abstractmethod
    def open(self) -> None:
        """Establish the underlying connection. Idempotent."""

    @abstractmethod
    def close(self) -> None:
        """Tear down the underlying connection. Idempotent."""

    @abstractmethod
    def is_open(self) -> bool: ...

    @abstractmethod
    def write(self, data: bytes) -> None:
        """Push raw bytes to the device. Blocks until written or raises."""

    @abstractmethod
    def read(self, n: int, timeout: float) -> bytes:
        """Read up to `n` bytes within `timeout` seconds.

        Returns whatever arrived (may be < n on timeout). Empty bytes
        means timeout elapsed without any data.
        """

    @abstractmethod
    def read_until(self, terminator: bytes, max_bytes: int, timeout: float) -> bytes:
        """Read until `terminator` byte appears or timeout/max_bytes hit.

        Used to read a complete framed response (terminator = b'\\x03' EOT)
        without parsing the LEN field upfront.
        """

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()
