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
from odoo_erpnet_fp.drivers.pinpad.datecs_pay.facade import DatecsPayPinpad

pytestmark = pytest.mark.skipif(
    _native._lib is None,
    reason=f"{_native._LIB_NAME} is not bundled (proprietary, not in git)",
)

PING_REQUEST = bytes([0x3E, 0x3D, 0x00, 0x00, 0x01, 0x00])  # без CSUM


def _frame(status: int = 0, data: bytes = b"", cmd: int = 0x00) -> bytes:
    body = bytes([0x3E, cmd, status, len(data) >> 8, len(data) & 0xFF]) + data
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


class ScriptedPinpad:
    """Цяла транзакция по една връзка — както я кара C цикълът.

    START (подкоманда 0x01) → отказ със `refuse_status`, или ACK и
    събитие TRANSACTION COMPLETE (0x0E/0x01); GET RECEIPT TAGS (0x02) →
    `receipt`; END (0x03) → ACK. Подкомандите се записват по ред.
    """

    def __init__(self, refuse_status: int = 0, receipt: bytes = b""):
        self.refuse_status = refuse_status
        self.receipt = receipt
        self.subcmds: list[int] = []
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
            conn.settimeout(15)
            buf = b""
            while True:
                try:
                    data = conn.recv(256)
                except OSError:
                    return
                if not data:
                    return
                buf += data
                # '>' CMD 00 LH LL <данни> CSUM
                while len(buf) >= 6:
                    size = 6 + ((buf[3] << 8) | buf[4])
                    if len(buf) < size:
                        break
                    frame, buf = buf[:size], buf[size:]
                    self._answer(conn, frame[5])

    def _answer(self, conn, sub: int):
        self.subcmds.append(sub)
        if sub == 0x01:
            if self.refuse_status:
                conn.sendall(_frame(status=self.refuse_status))
                return
            conn.sendall(_frame(status=0))
            time.sleep(0.1)
            conn.sendall(_frame(data=b"\x01" + self.receipt, cmd=0x0E))
        elif sub == 0x02:
            conn.sendall(_frame(data=self.receipt))
        else:
            conn.sendall(_frame(status=0))

    def close(self):
        self.srv.close()


# DF05 резултат = 0 (одобрено), DF06 грешка = 0, 81 сума = 42.94.
APPROVED_RECEIPT = bytes.fromhex("DF050400000000" "DF060400000000" "8104000010C6")


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


def _facade(port: int) -> DatecsPayPinpad:
    pp = DatecsPayPinpad(f"tcp://127.0.0.1:{port}")
    pp.open()
    return pp


def test_refused_start_returns_at_once_with_the_device_status():
    # Баръмски, 22.09.2026: пинпадът отказва старта (иска приключване на
    # деня), а касата чакаше целия бюджет и четеше „timeout“.
    fake = ScriptedPinpad(refuse_status=7)
    pp = _facade(fake.port)
    t = time.time()
    try:
        res = pp.purchase(amount_cents=4294, timeout=10)
    finally:
        pp.close()
        fake.close()
    assert time.time() - t < 5
    assert res.ok is False
    assert res.error == "refused_7 (Operation not permitted)"
    assert fake.subcmds == [0x01, 0x03]  # START, после END — без чакане


def test_approved_purchase_on_windows_stays_approved(monkeypatch, caplog):
    # На Windows ctypes.pythonapi няма free — одобрената транзакция излизаше
    # като грешка „function 'free' not found“, а картата вече е платила.
    monkeypatch.setattr(_native.ctypes, "pythonapi", object())
    fake = ScriptedPinpad(receipt=APPROVED_RECEIPT)
    pp = _facade(fake.port)
    try:
        res = pp.purchase(amount_cents=4294, timeout=10)
    finally:
        pp.close()
        fake.close()
    assert res.ok is True, res.error
    assert res.amount_cents == 4294
    assert fake.subcmds == [0x01, 0x02, 0x03]
    # Освободено през библиотеката, не изтекло.
    assert "not freed" not in caplog.text


def test_failed_free_does_not_turn_an_approval_into_a_decline(monkeypatch, caplog):
    def broken_free(ptr):
        raise OSError("foreign heap")

    assert hasattr(_native._lib, "datecs_free")
    monkeypatch.setattr(_native._lib, "datecs_free", broken_free)
    fake = ScriptedPinpad(receipt=APPROVED_RECEIPT)
    pp = _facade(fake.port)
    try:
        res = pp.purchase(amount_cents=4294, timeout=10)
    finally:
        pp.close()
        fake.close()
    assert res.ok is True, res.error
    assert "not freed" in caplog.text
