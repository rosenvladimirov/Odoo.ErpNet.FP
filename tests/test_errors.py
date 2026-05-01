"""
Error code lookup + raise_for_code semantics.
"""

import pytest

from odoo_erpnet_fp.drivers.fiscal.datecs_pm import errors


def test_lookup_zero_is_none():
    assert errors.lookup(0) is None


def test_lookup_known_code():
    info = errors.lookup(-100001)
    assert info is not None
    assert info.name == "ERR_IO"
    assert "GENERIC" in info.category


def test_lookup_unknown_code_returns_none():
    assert errors.lookup(-999999) is None


def test_raise_for_code_zero_is_noop():
    errors.raise_for_code(0)  # must not raise


def test_raise_for_code_known():
    with pytest.raises(errors.FiscalError) as excinfo:
        errors.raise_for_code(-100001)
    assert excinfo.value.code == -100001
    assert excinfo.value.name == "ERR_IO"


def test_raise_for_code_unknown_still_raises():
    with pytest.raises(errors.FiscalError) as excinfo:
        errors.raise_for_code(-999999)
    assert excinfo.value.code == -999999
    assert excinfo.value.name == "UNKNOWN"


def test_all_codes_loaded():
    table = errors.all_codes()
    assert len(table) >= 400  # CSV ships 457
    assert all(code < 0 for code in table.keys())
