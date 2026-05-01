"""
In-memory mock that satisfies `Transport` and replays canned responses
keyed by command opcode. Used to exercise the high-level facade
without real hardware.

A scripted entry can return either a (data, status) tuple (= a normal
wrapped response) or a single byte (NAK / SYN) to test retry / wait.
"""

from collections import deque
from typing import Callable

from odoo_erpnet_fp.drivers.fiscal.datecs_pm import frame
from odoo_erpnet_fp.drivers.fiscal.datecs_pm.transport import Transport, TransportClosed


# A response handler may return either:
#   bytes — already-encoded full frame (will be sent as-is), or
#   (data, status) — frame layer encodes a response, or
#   int — a single non-wrapped control byte (NAK / SYN)
ResponseHandler = Callable[[frame.Request], object]


class MockDevice(Transport):
    """In-memory replay device.

    Usage:
        mock = MockDevice()
        mock.expect(0x4A, lambda req: (b"", b"\\x80" * 8))
        pm = PmDevice(mock)
        pm.read_status()
    """

    def __init__(self) -> None:
        self._opened = False
        self._handlers: dict[int, ResponseHandler] = {}
        self._inbox: deque[bytes] = deque()  # pending bytes to deliver to host
        self.history: list[frame.Request] = []

    # ---- mock setup ------------------------------------------------

    def expect(self, cmd: int, handler: ResponseHandler) -> None:
        """Install a handler for a given command opcode."""
        self._handlers[cmd] = handler

    def expect_static(
        self, cmd: int, data: bytes = b"", status: bytes = b"\x80" * 8
    ) -> None:
        self._handlers[cmd] = lambda req, d=data, s=status: (d, s)

    # ---- Transport ABC ---------------------------------------------

    def open(self) -> None:
        self._opened = True

    def close(self) -> None:
        self._opened = False

    def is_open(self) -> bool:
        return self._opened

    def write(self, data: bytes) -> None:
        if not self._opened:
            raise TransportClosed("MockDevice not opened")
        # Decode the request, run handler, queue the canned response
        req = frame.decode_request(data)
        self.history.append(req)
        handler = self._handlers.get(req.cmd)
        if handler is None:
            raise AssertionError(
                f"MockDevice has no handler for cmd 0x{req.cmd:02X}"
            )
        result = handler(req)
        if isinstance(result, int):
            # Single control byte (NAK / SYN)
            self._inbox.append(bytes([result]))
        elif isinstance(result, bytes):
            self._inbox.append(result)
        elif isinstance(result, tuple) and len(result) == 2:
            resp_data, resp_status = result
            wrapped = frame.encode_response(
                req.seq, req.cmd, resp_data, resp_status
            )
            self._inbox.append(wrapped)
        else:
            raise AssertionError(
                f"Bad handler return for cmd 0x{req.cmd:02X}: {result!r}"
            )

    def read(self, n: int, timeout: float) -> bytes:
        if not self._opened:
            raise TransportClosed("MockDevice not opened")
        if not self._inbox:
            return b""
        head = self._inbox[0]
        if len(head) <= n:
            self._inbox.popleft()
            return head
        out = head[:n]
        self._inbox[0] = head[n:]
        return out

    def read_until(
        self, terminator: bytes, max_bytes: int, timeout: float
    ) -> bytes:
        if not self._opened:
            raise TransportClosed("MockDevice not opened")
        if not self._inbox:
            return b""
        head = self._inbox.popleft()
        # Check inline whether terminator appears
        if terminator in head:
            idx = head.index(terminator) + len(terminator)
            if idx < len(head):
                # leftover after terminator — push back so subsequent reads
                # don't lose it. (Real transport would frame one at a time.)
                self._inbox.appendleft(head[idx:])
            return head[:idx]
        return head
