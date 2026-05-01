"""
Sanity tests for ISL vendor variants — each subclass uses its own
tax-group and payment-type letter mappings.
"""

import pytest

from odoo_erpnet_fp.drivers.fiscal.datecs_isl import (
    DaisyIslDevice,
    DatecsIslDevice,
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
        (DaisyIslDevice, "bg.dy.isl"),
        (EltradeIslDevice, "bg.el.isl"),
        (IncotexIslDevice, "bg.is.icp"),
        (TremolIslDevice, "bg.tr.isl"),
    ],
)
def test_uri_prefix(cls, prefix):
    assert cls.URI_PREFIX == prefix
