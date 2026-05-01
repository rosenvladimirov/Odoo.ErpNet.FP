"""
Phase 2-C facade tests: X/Z reports, cash in/out, duplicate.

Anchors on PDF examples:
  * §4.29.1 — Z-report request `Z\\t` returns ErrorCode + nRep + 8 TotX
              + 8 StorX. Human log: `0\\t7\\t22.40\\t127.22\\t0.00\\t…`
  * §4.30   — Cash in: `0\\t50.00\\t` returns `0\\t599.59\\t1050.00\\t-1000.00\\t`
"""

import pytest

from odoo_erpnet_fp.drivers.fiscal.datecs_pm import codec, errors
from odoo_erpnet_fp.drivers.fiscal.datecs_pm.pm_v2_11_4 import PmDevice

from mock_device import MockDevice


# ---- X / Z reports ---------------------------------------------------


def test_z_report_pdf_response_parsing():
    """PDF §4.29.1 Z-report response example.

    Human log: 0\\t7\\t22.40\\t127.22\\t0.00\\t0.00\\t0.00\\t0.00\\t
                0.00\\t0.00\\t0.00\\t0.00\\t0.00\\t0.00\\t0.00\\t0.00\\t
                0.00\\t0.00\\t
    Fields: ErrorCode=0, nRep=7, TotA=22.40, TotB=127.22, rest 0.00.
    """
    mock = MockDevice()
    response_data = codec.encode_data(
        0,           # ErrorCode
        7,           # nRep
        "22.40",     # TotA
        "127.22",    # TotB
        "0.00",      # TotC
        "0.00",      # TotD
        "0.00",      # TotE
        "0.00",      # TotF
        "0.00",      # TotG
        "0.00",      # TotH
        "0.00",      # StorA
        "0.00",      # StorB
        "0.00",
        "0.00",
        "0.00",
        "0.00",
        "0.00",
        "0.00",
    )
    mock.expect_static(0x45, data=response_data, status=b"\x80" * 8)

    pm = PmDevice(mock)
    pm.open()
    n_rep, totals = pm.print_z_report()

    assert n_rep == 7
    assert totals["A"] == 22.40
    assert totals["B"] == 127.22
    assert totals["C"] == 0.0
    assert totals["H"] == 0.0

    sent = mock.history[-1]
    assert codec.decode_data(sent.data) == ["Z"]


def test_x_report_sends_X_subcommand():
    mock = MockDevice()
    response_data = codec.encode_data(0, 5, *["0.00"] * 16)
    mock.expect_static(0x45, data=response_data, status=b"\x80" * 8)
    pm = PmDevice(mock)
    pm.open()
    n_rep, _totals = pm.print_x_report()
    assert n_rep == 5
    assert codec.decode_data(mock.history[-1].data) == ["X"]


def test_x_z_report_propagates_device_error():
    mock = MockDevice()
    mock.expect_static(0x45, data=codec.encode_data(-101000), status=b"\x80" * 8)
    pm = PmDevice(mock)
    pm.open()
    with pytest.raises(errors.FiscalError):
        pm.print_z_report()


def test_print_periodical_report_sends_dates():
    """PDF §4.29.3 example: P\\t1\\t01-01-20\\t13-02-20\\t"""
    mock = MockDevice()
    mock.expect_static(0x45, data=codec.encode_data(0), status=b"\x80" * 8)
    pm = PmDevice(mock)
    pm.open()
    pm.print_periodical_report(sub_type=1, start_date="01-01-20", end_date="13-02-20")
    fields = codec.decode_data(mock.history[-1].data)
    assert fields == ["P", "1", "01-01-20", "13-02-20"]


def test_print_periodical_report_rejects_bad_subtype():
    pm = PmDevice(MockDevice())
    pm.open()
    with pytest.raises(ValueError):
        pm.print_periodical_report(sub_type=4)


# ---- Cash in / cash out ---------------------------------------------


def test_cash_in_pdf_example():
    """PDF §4.30 example: request `0\\t50\\t`, response `0\\t599.59\\t
    1050.00\\t-1000.00\\t` (CashSum=599.59, CashIn=1050.00, CashOut=-1000.00).
    """
    mock = MockDevice()
    mock.expect_static(
        0x46,
        data=codec.encode_data(0, "599.59", "1050.00", "-1000.00"),
        status=b"\x80" * 8,
    )
    pm = PmDevice(mock)
    pm.open()
    safe, total_in, total_out = pm.cash_in(50.0)
    assert safe == 599.59
    assert total_in == 1050.00
    assert total_out == -1000.00

    sent = mock.history[-1]
    fields = codec.decode_data(sent.data)
    assert fields == ["0", "50.00"]


def test_cash_out_sends_type_1():
    mock = MockDevice()
    mock.expect_static(
        0x46,
        data=codec.encode_data(0, "100.00", "0.00", "-25.00"),
        status=b"\x80" * 8,
    )
    pm = PmDevice(mock)
    pm.open()
    safe, _ti, _to = pm.cash_out(25.0)
    assert safe == 100.00
    assert codec.decode_data(mock.history[-1].data) == ["1", "25.00"]


def test_read_cash_state_zero_amount():
    """Amount=0 reads state without printing (PDF §4.30 note)."""
    mock = MockDevice()
    mock.expect_static(
        0x46,
        data=codec.encode_data(0, "300.00", "500.00", "-200.00"),
        status=b"\x80" * 8,
    )
    pm = PmDevice(mock)
    pm.open()
    safe, total_in, total_out = pm.read_cash_state()
    assert safe == 300.00
    assert codec.decode_data(mock.history[-1].data) == ["0", "0.00"]


def test_cash_op_rejects_negative_amount():
    pm = PmDevice(MockDevice())
    pm.open()
    with pytest.raises(ValueError):
        pm.cash_in(-1.0)


# ---- Duplicate ------------------------------------------------------


def test_print_duplicate():
    mock = MockDevice()
    mock.expect_static(0x6D, data=codec.encode_data(0), status=b"\x80" * 8)
    pm = PmDevice(mock)
    pm.open()
    pm.print_duplicate()
    # No params expected — empty DATA
    assert mock.history[-1].data == b""


# ---- Daily taxation info -------------------------------------------


def test_daily_taxation_info_pdf_example():
    """PDF §4.26 answer human log: 0\\t7\\t22.40\\t127.22\\t0.00\\t…"""
    mock = MockDevice()
    response_data = codec.encode_data(
        0, 7, "22.40", "127.22", *["0.00"] * 6
    )
    mock.expect_static(0x41, data=response_data, status=b"\x80" * 8)
    pm = PmDevice(mock)
    pm.open()
    n_rep, totals = pm.daily_taxation_info(info_type=0)
    assert n_rep == 7
    assert totals["A"] == 22.40
    assert totals["B"] == 127.22
