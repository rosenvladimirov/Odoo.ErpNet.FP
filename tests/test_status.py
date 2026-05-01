"""
Status-byte decoder tests, anchored on PROTOCOL_REFERENCE §12 example.

Status from PDF: 80 80 88 80 86 9A 80 80
  byte 2 = 0x88 → bit 3 set → fiscal_open
  byte 4 = 0x86 → bits 1, 2 set → tax_number_set, serial_fm_set
  byte 5 = 0x9A → bits 1, 3, 4 set → fm_formatted, fiscalized, vat_set
"""

import pytest

from odoo_erpnet_fp.drivers.fiscal.datecs_pm.status import FiscalStatus

PDF_STATUS_OK = bytes.fromhex("80808880869a8080")


def test_pdf_example_decodes_no_critical_errors():
    fs = FiscalStatus.parse(PDF_STATUS_OK)
    assert fs.has_critical_error() is False
    assert fs.errors() == []


def test_pdf_example_receipt_state():
    fs = FiscalStatus.parse(PDF_STATUS_OK)
    assert fs.fiscal_open is True
    assert fs.nonfiscal_open is False
    assert fs.ej_full is False


def test_pdf_example_fiscal_memory_state():
    fs = FiscalStatus.parse(PDF_STATUS_OK)
    assert fs.fiscalized is True
    assert fs.tax_number_set is True
    assert fs.serial_fm_set is True
    assert fs.vat_set is True
    assert fs.fm_formatted is True
    assert fs.fm_full is False


def test_status_length_enforced():
    with pytest.raises(ValueError):
        FiscalStatus.parse(b"\x80" * 7)


def test_status_bit7_must_be_one():
    with pytest.raises(ValueError):
        # byte 0 missing bit 7
        FiscalStatus.parse(b"\x00" + b"\x80" * 7)


def test_syntax_error_flag():
    # byte 0 bit 0 set → syntax_error
    raw = bytes([0x81, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80])
    fs = FiscalStatus.parse(raw)
    assert fs.syntax_error is True
    assert fs.has_critical_error() is True
    assert "syntax error" in fs.errors()


def test_invalid_command_flag():
    raw = bytes([0x82, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80])
    fs = FiscalStatus.parse(raw)
    assert fs.invalid_command is True
    assert fs.has_critical_error() is True


def test_paper_end_flag():
    raw = bytes([0x80, 0x80, 0x81, 0x80, 0x80, 0x80, 0x80, 0x80])
    fs = FiscalStatus.parse(raw)
    assert fs.end_of_paper is True


def test_fm_full_aggregates():
    # byte 4 bit 4 set → fm_full (`*` flag) — aggregated by bit 5
    # We don't auto-set the aggregate; firmware does. Test the raw flag.
    raw = bytes([0x80, 0x80, 0x80, 0x80, 0x90, 0x80, 0x80, 0x80])
    fs = FiscalStatus.parse(raw)
    assert fs.fm_full is True
