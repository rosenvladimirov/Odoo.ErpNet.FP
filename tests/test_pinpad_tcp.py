"""
TCP транспортът на DatecsPay пинпада — без апарат.

Родната библиотека отваря `tcp://host:port` направо към моста на BlueCash
(PinpadBridgeService :9101), вместо сериен порт през socat PTY. На Windows
това е единственият транспорт. Тук срещу нея стои фалшив пинпад: чете
заявката и отговаря с рамка `'>' 00 ST LH LL [данни] CSUM`, където CSUM е
XOR на всичко преди нея — същото, което праща истинският.

Тестовете се пропускат, ако родната библиотека не е в `datecs_pay/lib/`
(тя е собственическа и не е в git — виж INSTALL_PINPAD.md).
"""

from __future__ import annotations

import socket
import threading
import time
from functools import reduce

import pytest

from odoo_erpnet_fp.drivers.pinpad.datecs_pay import _native

pytestmark = pytest.mark.skipif(
    _native._lib is None,
    reason=f"{_native._LIB_NAME} is not bundled (proprietary, not in git)",
)

PING_REQUEST = bytes([0x3E, 0x3D, 0x00, 0x00, 0x01, 0x00])  # без CSUM


def _frame(status: int = 0, data: bytes = b"") -> bytes:
    body = bytes([0x3E, 0x00, status, len(data) >> 8, len(data) & 0xFF]) + data
    return body + bytes([reduce(lambda a, b: a ^ b, body, 0)])


class FakePinpad:
    """Един клиент: чете една заявка и отговаря по сценарий."""

    def __init__(self, reply: bytes = b"", chunks: int = 1, close_instead: bool = False):
        self.reply = reply
        self.chunks = chunks
        self.close_instead = close_instead
        self.received = b""
        self.srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(1)
        self.port = self.srv.getsockname()[1]
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self):
        conn, _ = self.srv.accept()
        with conn:
            conn.settimeout(5)
            while len(self.received) < len(PING_REQUEST) + 1:
                data = conn.recv(64)
                if not data:
                    return
                self.received += data
            if self.close_instead:
                return  # мостът изчезва по средата
            step = max(1, len(self.reply) // self.chunks)
            for i in range(0, len(self.reply), step):
                conn.sendall(self.reply[i:i + step])
                time.sleep(0.05)  # отделни TCP сегменти
            time.sleep(0.2)

    def close(self):
        self.srv.close()


def _driver(port: int) -> _native.DatecsPinpadDriver:
    return _native.DatecsPinpadDriver(f"tcp://127.0.0.1:{port}")


def test_library_filename_per_platform():
    assert _native.library_filename("nt") == "datecs_pinpad.dll"
    assert _native.library_filename("posix") == "libdatecs_pinpad.so"


def test_ping_over_tcp():
    fake = FakePinpad(reply=_frame(status=0))
    d = _driver(fake.port)
    d.open()
    try:
        assert d.ping() is True
    finally:
        d.close()
        fake.close()
    # Заявката стига непокътната — протоколът не е пипан от транспорта.
    assert fake.received[:len(PING_REQUEST)] == PING_REQUEST


def test_reply_split_across_tcp_segments():
    # Мостът на Android чете на парчета; рамката може да дойде разцепена.
    fake = FakePinpad(reply=_frame(status=0, data=b"\x01\x02\x03\x04"), chunks=4)
    d = _driver(fake.port)
    d.open()
    try:
        assert d.ping() is True
    finally:
        d.close()
        fake.close()


def test_closed_port_fails_fast():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()  # никой не слуша
    t = time.time()
    with pytest.raises(RuntimeError, match="Failed to open device"):
        _driver(port).open()
    assert time.time() - t < 6  # не виси до безкрай


def test_bridge_closing_mid_request_is_an_error_not_a_hang():
    fake = FakePinpad(close_instead=True)
    d = _driver(fake.port)
    d.open()
    t = time.time()
    try:
        assert d.ping() is False
    finally:
        d.close()
        fake.close()
    assert time.time() - t < 6
