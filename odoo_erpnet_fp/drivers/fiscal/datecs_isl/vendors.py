"""
Vendor variants of the ISL driver.

All BG fiscal-printer vendors that use the ISL framing layer share the
same protocol envelope (PRE/PST/BCC, command opcodes, status bytes) but
differ in:

  * Tax-group letters (А..З Cyrillic vs A..H Latin vs A..D)
  * Payment-type letters (P/C/N/D vs Eltrade's 11-letter set)
  * Detection field separators (CSV vs TAB)
  * URI scheme prefix used in `DeviceInfo.uri`

Within Datecs there are also two protocol-encoding variants:

  * **C variant** (DP-150 base, FW 3.00 BG) — `op,pw,UNS,1` —
    COMMA-separated, 4 fields, admin password default "9999"
  * **X variant** (DP-150X, FP-700X, FMP-350X) — `op\tpw\tUNS\t1\t\t\t` —
    TAB-separated, 6 fields, admin password default "0000"

This module supplies one subclass of `IslDevice` per vendor:

  DatecsIslDevice    — Cyrillic А..З + P/C/N/D (C variant — DP-150 etc.)
  DatecsIslXDevice   — same vendor, X variant encoding (DP-150X / FP-700X)
  DaisyIslDevice     — same as Datecs (Cyrillic + P/C/N/D)
  EltradeIslDevice   — Latin A..H + 11 payment letters
  IncotexIslDevice   — Latin A..D only (4 VAT slots) + P/C/N/D
  TremolIslDevice    — Cyrillic + P/C/N/D (Tremol legacy ISL)
"""

from __future__ import annotations

from typing import Optional

from . import commands as cmd
from .protocol import (
    DeviceStatus,
    IslDevice,
    PaymentType,
    TaxGroup,
)


class DatecsIslDevice(IslDevice):
    """Datecs ISL — same as the default `IslDevice`, present for naming
    parity with other vendors.

    C variant (comma-separated headers, 4 fields, admin pw "9999").
    Verified on real Datecs DP-150 (DT737851, FW 3.00 22Jul25 1109).
    """

    URI_PREFIX = "bg.dt.isl"


class DatecsIslXDevice(DatecsIslDevice):
    """Datecs ISL — X variant (DP-150X, FP-700X, FMP-350X).

    Differences from the C-variant base:

    * `open_receipt`/`open_invoice_receipt` headers are TAB-separated
      with 6 trailing fields (vs 4 comma-separated for C variant).
    * Default admin password is "0000" (vs "9999" for C variant).

    NOT YET VERIFIED on real hardware — subclass scaffolding only.
    `program_plu` (TAB-separated, 6 fields per ISL X spec) is left
    inherited from the C base; it must be overridden once real-device
    test confirms the exact field order. Mark as TODO at call site.
    """

    URI_PREFIX = "bg.dt.islx"

    def __init__(
        self,
        transport,
        operator_id: str = "1",
        operator_password: str = "1",
        admin_id: str = "20",
        admin_password: str = "0000",  # X-variant default
    ) -> None:
        super().__init__(
            transport,
            operator_id=operator_id,
            operator_password=operator_password,
            admin_id=admin_id,
            admin_password=admin_password,
        )

    def open_receipt(
        self,
        unique_sale_number: str,
        operator_id: Optional[str] = None,
        operator_password: Optional[str] = None,
    ) -> DeviceStatus:
        """X-variant header: TAB-separated, 6 fields.

        `op\\tpw\\tUNS\\t1\\t\\t\\t` — last 3 fields are reserved /
        future-use slots that must be present (empty) for the device
        to accept the frame.
        """
        op = operator_id or self.operator_id
        pw = operator_password or self.operator_password
        header = "\t".join([op, pw, unique_sale_number, "1", "", "", ""])
        _t, status, _r = self._isl_request(
            cmd.CMD_OPEN_FISCAL_RECEIPT, header)
        return status

    def open_invoice_receipt(
        self,
        unique_sale_number: str,
        recipient_name: str,
        recipient_eik: str,
        recipient_eik_type: str = "0",
        recipient_address: str = "",
        recipient_buyer: str = "",
        recipient_vat: str = "",
        invoice_number: Optional[str] = None,
        operator_id: Optional[str] = None,
        operator_password: Optional[str] = None,
    ) -> DeviceStatus:
        """X-variant invoice header: TAB-separated, flag '2'.

        Same field order as the C variant invoice header but with TAB
        separator. NOT YET VERIFIED on real DP-150X — keep an eye on
        the device-returned status bytes when first run.
        """
        op = operator_id or self.operator_id
        pw = operator_password or self.operator_password
        inv = (invoice_number or "").rjust(10, "0") if invoice_number else ""
        header = "\t".join([
            op, pw, unique_sale_number,
            "2",                                   # invoice flag
            inv,
            recipient_name[:26],
            recipient_buyer[:16],
            recipient_address[:30],
            recipient_eik[:13],
            recipient_eik_type,
            recipient_vat[:13],
        ])
        _t, status, _r = self._isl_request(
            cmd.CMD_OPEN_FISCAL_RECEIPT, header)
        return status


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
    "DatecsIslXDevice",
    "DaisyIslDevice",
    "EltradeIslDevice",
    "IncotexIslDevice",
    "TremolIslDevice",
]
