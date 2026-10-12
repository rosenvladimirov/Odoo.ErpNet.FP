"""
Facade-level tests using MockDevice.

Verifies command parameter assembly, ErrorCode parsing, and the
NAK/SYN dispatch logic in PmDevice._read_one_frame.
"""

import pytest

from odoo_erpnet_fp.drivers.fiscal.datecs_pm import codec, errors, frame
from odoo_erpnet_fp.drivers.fiscal.datecs_pm.pm_v2_11_4 import PmDevice

from mock_device import MockDevice


# ---- Phase 1 happy paths --------------------------------------------


def test_read_status_round_trip():
    mock = MockDevice()
    pdf_status = bytes.fromhex("80808880869a8080")
    mock.expect_static(0x4A, data=b"", status=pdf_status)

    pm = PmDevice(mock)
    pm.open()

    fs = pm.read_status()
    assert fs.fiscal_open is True
    assert fs.fiscalized is True


def test_open_fiscal_receipt_returns_slip_number():
    mock = MockDevice()
    mock.expect_static(
        0x30, data=codec.encode_data(0, 472), status=b"\x80" * 8
    )

    pm = PmDevice(mock, op_code=1, op_password="1", till_number=24)
    pm.open()

    slip = pm.open_fiscal_receipt(invoice=True)
    assert slip == 472

    sent = mock.history[-1]
    # Without nsale → syntax #1: OpCode \t OpPwd \t TillNmb \t Invoice \t
    assert sent.cmd == 0x30
    assert codec.decode_data(sent.data) == ["1", "1", "24", "I"]


def test_open_fiscal_receipt_with_nsale_uses_syntax_2():
    mock = MockDevice()
    mock.expect_static(0x30, data=codec.encode_data(0, 1), status=b"\x80" * 8)

    pm = PmDevice(mock)
    pm.open()
    pm.open_fiscal_receipt(nsale="DT636533-0020-0010110")

    sent = mock.history[-1]
    fields = codec.decode_data(sent.data)
    # OpCode \t OpPwd \t NSale \t TillNmb \t Invoice
    assert fields[2] == "DT636533-0020-0010110"


def test_open_fiscal_receipt_propagates_error_code():
    mock = MockDevice()
    # Error code -100001 in DATA → FiscalError
    mock.expect_static(0x30, data=codec.encode_data(-100001), status=b"\x80" * 8)

    pm = PmDevice(mock)
    pm.open()
    with pytest.raises(errors.FiscalError) as excinfo:
        pm.open_fiscal_receipt()
    assert excinfo.value.code == -100001


def test_close_fiscal_receipt():
    mock = MockDevice()
    mock.expect_static(0x38, data=codec.encode_data(0, 472), status=b"\x80" * 8)
    pm = PmDevice(mock)
    pm.open()
    assert pm.close_fiscal_receipt() == 472


def test_cancel_fiscal_receipt():
    mock = MockDevice()
    mock.expect_static(0x3C, data=codec.encode_data(0), status=b"\x80" * 8)
    pm = PmDevice(mock)
    pm.open()
    pm.cancel_fiscal_receipt()  # must not raise


def test_payment_total_returns_change():
    mock = MockDevice()
    mock.expect_static(0x35, data=codec.encode_data(0, "0.05"), status=b"\x80" * 8)
    pm = PmDevice(mock)
    pm.open()
    change, _ = pm.payment_total(payment_type=0, amount=10.05)
    assert change == 0.05


# ---- transport behaviour --------------------------------------------


def test_seq_increments_per_call():
    mock = MockDevice()
    mock.expect_static(0x4A, data=b"", status=b"\x80" * 8)

    pm = PmDevice(mock)
    pm.open()
    pm.read_status()
    pm.read_status()
    pm.read_status()

    seqs = [r.seq for r in mock.history]
    assert seqs == [0x20, 0x21, 0x22]


def test_syn_then_response_unblocks_read():
    mock = MockDevice()
    # First the device sends a SYN, then on next write produces normal frame.
    # Our MockDevice queues per-write, so simulate by handler returning the
    # SYN byte first:
    call_count = {"n": 0}

    def handler(req):
        call_count["n"] += 1
        if call_count["n"] < 2:
            # First call: enqueue SYN + the real response together
            real = frame.encode_response(req.seq, req.cmd, b"", b"\x80" * 8)
            return bytes([frame.SYN]) + real
        return (b"", b"\x80" * 8)

    mock.expect(0x4A, handler)

    pm = PmDevice(mock)
    pm.open()
    fs = pm.read_status()
    assert fs is not None  # got through SYN to real response


def test_nak_triggers_retry():
    mock = MockDevice()

    attempts = {"n": 0}

    def handler(req):
        attempts["n"] += 1
        if attempts["n"] == 1:
            return frame.NAK  # first attempt → NAK → host retries
        return (b"", b"\x80" * 8)  # second succeeds

    mock.expect(0x4A, handler)

    pm = PmDevice(mock, retries=3)
    pm.open()
    pm.read_status()

    assert attempts["n"] == 2


def test_three_consecutive_naks_raises():
    mock = MockDevice()
    mock.expect(0x4A, lambda req: frame.NAK)
    pm = PmDevice(mock, retries=3)
    pm.open()
    with pytest.raises(Exception):  # TransportError (re-raised after retries)
        pm.read_status()


# ---- Отстъпка на ред (Баръмски, 12.10.2026: бонът излезе с „Надбавка 25%“) ----
# PDF §4.12 / §4.27: DiscountType 1 = надбавка %, 2 = отстъпка %, 3 = надбавка сума,
# 4 = отстъпка сума. Отстъпката от Odoo е отстъпка — трябва "2", не "1".


def test_register_sale_discount_is_discount_not_surcharge():
    mock = MockDevice()
    mock.expect_static(0x31, data=codec.encode_data(0, "59.25"), status=b"\x80" * 8)
    pm = PmDevice(mock)
    pm.open()
    pm.register_sale("Shoe", 79.00, 1.0, vat_group="Б", discount_percent=25.0)
    fields = codec.decode_data(mock.history[-1].data)
    assert fields[4:6] == ["2", "25.00"]


def test_sale_programmed_discount_is_discount_not_surcharge():
    mock = MockDevice()
    mock.expect_static(0x3A, data=codec.encode_data(0, "59.25"), status=b"\x80" * 8)
    pm = PmDevice(mock)
    pm.open()
    pm.sale_programmed(7, 1.0, price=79.00, discount_percent=25.0)
    fields = codec.decode_data(mock.history[-1].data)
    assert fields[3:5] == ["2", "25.00"]


def test_sale_without_discount_sends_no_discount_type():
    mock = MockDevice()
    mock.expect_static(0x3A, data=codec.encode_data(0, "79.00"), status=b"\x80" * 8)
    pm = PmDevice(mock)
    pm.open()
    pm.sale_programmed(7, 1.0, price=79.00)
    fields = codec.decode_data(mock.history[-1].data)
    assert fields[3:5] == ["", ""]
