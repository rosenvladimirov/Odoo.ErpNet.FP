"""
Vendor variants of the ISL driver.

All BG fiscal-printer vendors that use the ISL framing layer share the
same protocol envelope (PRE/PST/BCC, command opcodes, status bytes) but
differ in:

  * Tax-group letters (А..З Cyrillic vs A..H Latin vs A..D)
  * Payment-type letters (P/C/N/D vs Eltrade's 11-letter set)
  * Detection field separators (CSV vs TAB)
  * URI scheme prefix used in `DeviceInfo.uri`

This module supplies one subclass of `IslDevice` per vendor:

  DatecsIslDevice    — Cyrillic А..З + P/C/N/D (already the default)
  DaisyIslDevice     — same as Datecs (Cyrillic + P/C/N/D)
  EltradeIslDevice   — Latin A..H + 11 payment letters
  IncotexIslDevice   — Latin A..D only (4 VAT slots) + P/C/N/D
  TremolIslDevice    — Cyrillic + P/C/N/D (Tremol legacy ISL)
"""

from __future__ import annotations

from .protocol import IslDevice, PaymentType, TaxGroup


class DatecsIslDevice(IslDevice):
    """Datecs ISL — same as the default `IslDevice`, present for naming
    parity with other vendors.
    """

    URI_PREFIX = "bg.dt.isl"


class DaisyIslDevice(IslDevice):
    """Daisy fiscal printers (ISL family).

    Tax group / payment letters identical to Datecs ISL.
    """

    URI_PREFIX = "bg.dy.isl"
    # _TAX_LETTERS, _PAYMENT_LETTERS inherited from IslDevice (Datecs default)


class EltradeIslDevice(IslDevice):
    """Eltrade fiscal printers.

    Latin A..H tax groups; rich 11-letter payment alphabet covering all
    ErpNet.FP payment types one-to-one.
    """

    URI_PREFIX = "bg.el.isl"

    _TAX_LETTERS = {
        TaxGroup.G1: "A",
        TaxGroup.G2: "B",
        TaxGroup.G3: "C",
        TaxGroup.G4: "D",
        TaxGroup.G5: "E",
        TaxGroup.G6: "F",
        TaxGroup.G7: "G",
        TaxGroup.G8: "H",
    }
    _PAYMENT_LETTERS = {
        # Per upstream IoT box driver:
        # Cash→P, Check→N, Coupons→C, ExtCoupons→D, Packaging→I,
        # InternalUsage→J, Damage→K, Card→L, Bank→M, Reserved1→Q, Reserved2→R
        PaymentType.CASH: "P",
        PaymentType.CHECK: "N",
        PaymentType.CARD: "L",
        PaymentType.RESERVED1: "Q",
    }


class IncotexIslDevice(IslDevice):
    """Incotex fiscal printers.

    Only 4 VAT slots A..D — `tax_group_letter` raises for G5..G8.
    """

    URI_PREFIX = "bg.is.icp"

    _TAX_LETTERS = {
        TaxGroup.G1: "A",
        TaxGroup.G2: "B",
        TaxGroup.G3: "C",
        TaxGroup.G4: "D",
    }
    # Payment letters identical to Datecs default


class TremolIslDevice(IslDevice):
    """Tremol fiscal printers running the ISL profile.

    Note: Tremol also has a "master/slave" framing protocol on older
    devices (TremolFiscalPrinterDriver in Odoo IoT box). That one is
    NOT covered here — only the ISL variant.
    """

    URI_PREFIX = "bg.tr.isl"
    # Inherits Datecs defaults (Cyrillic А..З + P/C/N/D)


__all__ = [
    "DatecsIslDevice",
    "DaisyIslDevice",
    "EltradeIslDevice",
    "IncotexIslDevice",
    "TremolIslDevice",
]
