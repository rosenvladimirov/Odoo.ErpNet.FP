"""
Sanity tests for ISL vendor variants — each subclass uses its own
tax-group and payment-type letter mappings.
"""

import pytest

from odoo_erpnet_fp.drivers.fiscal.datecs_isl import (
    DaisyIslDevice,
    DatecsIslDevice,
    DatecsIslXDevice,
    EltradeIslDevice,
    IncotexIslDevice,
    TremolIslDevice,
)
from odoo_erpnet_fp.drivers.fiscal.datecs_isl.protocol import (
    PaymentType,
    TaxGroup,
)


# ─── Tax group mapping per vendor ────────────────────────────────


def _make(cls):
    """Construct a vendor instance bypassing the transport (we only test
    pure mapping logic)."""
    return cls.__new__(cls)


@pytest.mark.parametrize(
    "cls,tg,expected",
    [
        # Datecs / Daisy / Tremol — Cyrillic
        (DatecsIslDevice, TaxGroup.G1, "А"),
        (DatecsIslDevice, TaxGroup.G2, "Б"),
        (DatecsIslDevice, TaxGroup.G8, "З"),
        (DaisyIslDevice, TaxGroup.G1, "А"),
        (DaisyIslDevice, TaxGroup.G3, "В"),
        (TremolIslDevice, TaxGroup.G1, "А"),
        # Eltrade — Latin A..H
        (EltradeIslDevice, TaxGroup.G1, "A"),
        (EltradeIslDevice, TaxGroup.G2, "B"),
        (EltradeIslDevice, TaxGroup.G8, "H"),
        # Incotex — only A..D
        (IncotexIslDevice, TaxGroup.G1, "A"),
        (IncotexIslDevice, TaxGroup.G4, "D"),
    ],
)
def test_tax_group_letter_per_vendor(cls, tg, expected):
    dev = _make(cls)
    assert dev.tax_group_letter(tg) == expected


def test_incotex_rejects_high_groups():
    dev = _make(IncotexIslDevice)
    with pytest.raises(ValueError):
        dev.tax_group_letter(TaxGroup.G5)
    with pytest.raises(ValueError):
        dev.tax_group_letter(TaxGroup.G8)


# ─── Payment type mapping per vendor ─────────────────────────────


@pytest.mark.parametrize(
    "cls,pt,expected",
    [
        (DatecsIslDevice, PaymentType.CASH, "P"),
        (DatecsIslDevice, PaymentType.CARD, "C"),
        (DatecsIslDevice, PaymentType.CHECK, "N"),
        (DaisyIslDevice, PaymentType.CASH, "P"),
        (TremolIslDevice, PaymentType.CARD, "C"),
        (IncotexIslDevice, PaymentType.CASH, "P"),
        # Eltrade has different letters for card/check
        (EltradeIslDevice, PaymentType.CASH, "P"),
        (EltradeIslDevice, PaymentType.CHECK, "N"),
        (EltradeIslDevice, PaymentType.CARD, "L"),
        (EltradeIslDevice, PaymentType.RESERVED1, "Q"),
    ],
)
def test_payment_letter_per_vendor(cls, pt, expected):
    dev = _make(cls)
    assert dev.payment_type_letter(pt) == expected


# ─── URI prefix ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "cls,prefix",
    [
        (DatecsIslDevice, "bg.dt.isl"),
        (DatecsIslXDevice, "bg.dt.islx"),
        (DaisyIslDevice, "bg.dy.isl"),
        (EltradeIslDevice, "bg.el.isl"),
        (IncotexIslDevice, "bg.is.icp"),
        (TremolIslDevice, "bg.tr.isl"),
    ],
)
def test_uri_prefix(cls, prefix):
    assert cls.URI_PREFIX == prefix


# ─── Datecs C vs X variant — header encoding ─────────────────────


class _FakeTransport:
    """Pretend transport — never sends bytes, just satisfies the
    constructor's `transport` arg."""

    def is_open(self):
        return True

    def open(self):
        pass

    def close(self):
        pass

    def write(self, _data):
        pass

    def read_until(self, *_a, **_kw):
        return b""


def _capture_isl_request(monkeypatch):
    """Patch IslDevice._isl_request so test sees the (cmd, data) it
    would have sent. Returns the captured list."""
    captured = []

    def fake(self, command, data="", timeout=5.0):
        captured.append((command, data))
        return ("", None, b"")

    from odoo_erpnet_fp.drivers.fiscal.datecs_isl import protocol as _proto
    monkeypatch.setattr(_proto.IslDevice, "_isl_request", fake)
    return captured


def test_datecs_c_variant_open_receipt_uses_comma(monkeypatch):
    """C variant header: `op,pw,UNS,1` — comma-separated, 4 fields."""
    captured = _capture_isl_request(monkeypatch)
    dev = DatecsIslDevice(_FakeTransport())
    dev.open_receipt("DT123456-1234-1234567")
    cmd, data = captured[-1]
    assert cmd == 0x30  # CMD_OPEN_FISCAL_RECEIPT
    assert data == "1,1,DT123456-1234-1234567,1"
    assert "\t" not in data


def test_datecs_x_variant_open_receipt_uses_tab(monkeypatch):
    """X variant header: `op\\tpw\\tUNS\\t1\\t\\t\\t` — TAB, 6 fields."""
    captured = _capture_isl_request(monkeypatch)
    dev = DatecsIslXDevice(_FakeTransport())
    dev.open_receipt("DT123456-1234-1234567")
    cmd, data = captured[-1]
    assert cmd == 0x30
    assert data == "1\t1\tDT123456-1234-1234567\t1\t\t\t"
    assert "," not in data


def test_datecs_x_variant_default_admin_password():
    """X variant defaults admin_password to '0000' (vs '9999' for C)."""
    c = DatecsIslDevice(_FakeTransport())
    x = DatecsIslXDevice(_FakeTransport())
    assert c.admin_password == "9999"
    assert x.admin_password == "0000"


def test_datecs_x_inherits_tax_and_payment_letters():
    """X variant inherits Datecs Cyrillic tax letters + P/C/N/D payment."""
    dev = DatecsIslXDevice(_FakeTransport())
    assert dev.tax_group_letter(TaxGroup.G1) == "А"
    assert dev.tax_group_letter(TaxGroup.G2) == "Б"
    assert dev.payment_type_letter(PaymentType.CASH) == "P"
    assert dev.payment_type_letter(PaymentType.CARD) == "C"


def test_datecs_x_invoice_header_uses_tab(monkeypatch):
    """Invoice mode (flag '2') also TAB-separated on X variant."""
    captured = _capture_isl_request(monkeypatch)
    dev = DatecsIslXDevice(_FakeTransport())
    dev.open_invoice_receipt(
        unique_sale_number="DT123456-1234-1234567",
        recipient_name="ACME OOD",
        recipient_eik="123456789",
    )
    _cmd, data = captured[-1]
    assert "\t" in data
    assert "," not in data
    assert "ACME OOD" in data
    assert "123456789" in data
    # Flag '2' should be the 4th tab-separated field
    fields = data.split("\t")
    assert fields[3] == "2"
