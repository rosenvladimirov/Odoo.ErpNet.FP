"""
Toledo 8217 protocol parser tests.
"""

from odoo_erpnet_fp.drivers.scales.toledo_8217 import (
    Toledo8217Scale,
    _MEASURE_RE,
    _STATUS_RE,
)


def test_measure_regex_basic():
    raw = b"\x021.234N\r"
    m = _MEASURE_RE.search(raw)
    assert m
    assert float(m.group(1)) == 1.234


def test_measure_regex_zero():
    raw = b"\x020.000\r"
    m = _MEASURE_RE.search(raw)
    assert m
    assert float(m.group(1)) == 0.0


def test_measure_regex_with_leading_space():
    raw = b"\x02   12.345N\r"
    m = _MEASURE_RE.search(raw)
    assert m
    assert float(m.group(1)) == 12.345


def test_status_regex():
    # b'\x02?D\r' — D = 0x44 = 0100_0100 → bits 2 + 6 set → "Under zero" + "Bad command"
    raw = b"\x02?D\r"
    m = _STATUS_RE.search(raw)
    assert m
    assert m.group(1) == b"D"


def test_decode_status_byte():
    # Bit 0 = scale in motion
    assert Toledo8217Scale._decode_status_byte(0b0000_0001) == ["Scale in motion"]
    # Bit 1 = over capacity
    assert Toledo8217Scale._decode_status_byte(0b0000_0010) == ["Over capacity"]
    # 0x44 = 0100_0100 → bits 2+6 → "Under zero" + "Bad command from host"
    out = Toledo8217Scale._decode_status_byte(0x44)
    assert "Under zero" in out
    assert "Bad command from host" in out


def test_decode_status_no_errors():
    assert Toledo8217Scale._decode_status_byte(0x00) == []


def test_status_message_type_count():
    # All 7 bits set → all 7 error labels
    out = Toledo8217Scale._decode_status_byte(0x7F)
    assert len(out) == 7
