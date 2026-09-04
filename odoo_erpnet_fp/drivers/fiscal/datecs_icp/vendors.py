"""
Vendor variants of the ICP driver.

All BG fiscal-printer vendors that use the ICP framing layer share the
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

This module supplies one subclass of `IcpDevice` per vendor:

  DatecsIcpDevice    — Cyrillic А..З + P/C/N/D (C variant — DP-150 etc.)
  DatecsIcpXDevice   — same vendor, X variant encoding (DP-150X / FP-700X)
  DaisyIcpDevice     — same as Datecs (Cyrillic + P/C/N/D)
  EltradeIcpDevice   — Latin A..H + 11 payment letters
  IncotexIcpDevice   — Latin A..D only (4 VAT slots) + P/C/N/D

⛔ Tremol НЯМА клас тук. Оригиналът също няма драйвер от това
семейство за Tremol — техният протокол е ZFP (`bg.zk.zfp`,
BgTremolZfpFiscalPrinterDriver). Заварената `TremolIslDevice` беше
изобретена и е премахната.

──────────────────────────────────────────────────────────────────────
Защо семейството се казва ICP, а не ISL
──────────────────────────────────────────────────────────────────────

Дотук се казваше **ISL**. Това е ИМЕ НА ФИРМА: „ISL Integrated Systems
Laboratory", София (www.isl.bg), която прави свои фискални принтери
(ISL5011S-KL). Оригиналният ErpNet.FP (erp.bg), който този порт
замества, беше лепнал името на един производител върху протокола на
друг. Преименувано с решение на Росен, 04.09.2026.

🔍 Какво показа проверката на първоизточниците:

  * Нито един вендорски протоколен документ — Datecs P/C/PM, Daisy,
    Eltrade — не съдържа „ISL", „ICP" или „ICL". Нула срещания.
    Datecs кръщава своя документ просто „Programmer's Manual".
  * Единственият документ с „ISL" е на самата фирма ISL:
    `bg.icp.is.protocol-v.10.4.pdf`.
  * Протокол „ICL" не съществува — това е разчитане на ICP.

⚠️ **Цената на решението, записана нарочно.** В оригинала ICP беше
името на протокола на ISL, който е ДРУГА рамка от нашата:

      това семейство  преамбюл 0x01 · PST 0x05 · SEP 0x04 · TERM 0x03 · sequence
      ICP на ISL      STX 0x02 · ETX 0x03 · ACK/NACK/WAIT · без sequence
      ZFP на Tremol   STX 0x02 · ACK 0x06 · NAK 0x15 · sequence до 0x9F

⇒ След преименуването `bg.dt.c.icp` и `bg.is.icp` носят едно име за две
различни рамки. Разделя ги ВЕНДОРСКИЯТ код, не протоколният. Ако някой
ден се пише драйвер за ISL5011S-KL, той НЕ бива да наследява този клас —
рамката е друга (в оригинала: `BgIcpFiscalPrinter`, не
`BgIslFiscalPrinter`).

Схемата на URI-то е `bg.<вендор>[.<вариант>].<протокол>`, където
**вендорът е префиксът на СЕРИЙНИЯ НОМЕР**:

    DT/DA → Datecs · DY → Daisy · ED → Eltrade · IN → Incotex
    IS    → ISL    · ZK → Tremol

  bg.dt.c.icp   Datecs, C вариант      BgDatecsCIslFiscalPrinterDriver
  bg.dt.p.icp   Datecs, P вариант      BgDatecsPIslFiscalPrinterDriver
  bg.dt.x.icp   Datecs, X вариант      BgDatecsXIslFiscalPrinterDriver
  bg.dy.icp     Daisy                  BgDaisyIslFiscalPrinterDriver
  bg.ed.icp     Eltrade                BgEltradeIslFiscalPrinterDriver
  bg.in.icp     Incotex                BgIncotexIslFiscalPrinterDriver
  bg.is.icp     ISL Bulgaria           BgIslIcpFiscalPrinterDriver
  bg.zk.zfp     Tremol                 BgTremolZfpFiscalPrinterDriver

⛔ `bg.is.*` е ЗАПАЗЕНО за ISL Bulgaria. Не го давай на друг вендор —
точно това беше сгрешено при Incotex.
"""

from __future__ import annotations

from typing import Optional

from . import commands as cmd
from .protocol import (
    DeviceStatus,
    IcpDevice,
    PaymentType,
    TaxGroup,
)


class DatecsIcpDevice(IcpDevice):
    """Datecs ICP — DP-150 family (C variant, comma-separated headers).

    Verified on real Datecs DP-150 (DT737851, FW 3.00 22Jul25 1109).

    Payment letters override: базовият `IcpDevice` дава `CARD = "C"`, но
    реалният DP-150 (FW 3.00) отказва `\\tC` при close с E404 "Command not
    allowed in the current fiscal mode". Per upstream Odoo IoT box driver +
    Eltrade variant, картовото плащане е **`L`**. Едновременно добавяме
    разширените типове (Coupons=C, ExtCoupons=D, Packaging=I, ...) за пълно
    NRA съответствие.
    """

    # C вариантът в оригинала е bg.dt.c.icp — вариантът е СРЕДНАТА
    # част, не суфикс на протокола (BgDatecsCIcpFiscalPrinterDriver).
    URI_PREFIX = "bg.dt.c.icp"

    _PAYMENT_LETTERS = {
        # Empirically verified labels on real DP-150 (DT737851,
        # FW 3.00 22Jul25 1109) — 2026-05-25 (single-receipt tests
        # with timestamp-matched receipts; two independent batches).
        #
        # The C-variant firmware exposes only THREE active payment
        # letters; every other letter (C/D/I/J/K/M/Q/R/etc.) falls
        # back to slot 0 and prints "В БРОЙ" on the receipt.
        #
        #   Letter │ Label
        #   ───────┼─────────
        #     P    │ В БРОЙ
        #     L    │ КУПОН
        #     N    │ КРЕДИТ
        #     *    │ В БРОЙ (device default fallback for unmapped)
        #
        # NOTE: upstream ErpNet.FP C# driver claims L=Card / N=Check.
        # Wrong for DP-150 fw 3.00 — verified empirically.
        #
        # Mapping rationale:
        #   cash    → P  (only slot for cash; matches В БРОЙ)
        #   card    → N  (closest to "card" semantics; prints КРЕДИТ)
        #   bank    → N  (bank-account payment ≈ card semantically;
        #                 handled via adapter, see below)
        #   coupons → L  (only slot for coupons; prints КУПОН)
        #   Everything else → P (no dedicated slot; device would
        #   fall back to cash anyway — explicit so admins see one
        #   source-of-truth instead of "magic" behaviour).
        #
        # NOTE: this PaymentType is the ICP-local one in
        # `protocol.py` with only 4 UPPERCASE members (CASH, CARD,
        # CHECK, RESERVED1). The server-side schemas.PaymentType has
        # 11 lowercase members; the adapter
        # (server/adapters/payment_type.py) translates between the
        # two before reaching this letter map.
        PaymentType.CASH: "P",      # → В БРОЙ
        PaymentType.CARD: "N",      # → КРЕДИТ — closest to card semantics
        # CHECK has no dedicated ЧЕК label on DP-150 fw 3.00 —
        # КУПОН (letter L) is the closest non-cash slot. Admin can
        # reprogram via PROG menu if a separate ЧЕК label is wanted.
        PaymentType.CHECK: "L",     # → КУПОН (no ЧЕК slot on this fw)
        PaymentType.RESERVED1: "P", # → В БРОЙ (no slot)
    }


class DatecsIcpXDevice(DatecsIcpDevice):
    """Datecs ICP — X variant (DP-150X, FP-700X, FMP-350X).

    Differences from the C-variant base:

    * `open_receipt`/`open_invoice_receipt` headers are TAB-separated
      with 6 trailing fields (vs 4 comma-separated for C variant).
    * Default admin password is "0000" (vs "9999" for C variant).

    NOT YET VERIFIED on real hardware — subclass scaffolding only.
    `program_plu` (TAB-separated, 6 fields per ICP X spec) is left
    inherited from the C base; it must be overridden once real-device
    test confirms the exact field order. Mark as TODO at call site.
    """

    # BgDatecsXIcpFiscalPrinterDriver → bg.dt.x.icp. „icpx" не е
    # протокол — X е вариант на записа, ICP е семейството.
    URI_PREFIX = "bg.dt.x.icp"

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
        _t, status, _r = self._icp_request(
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
        _t, status, _r = self._icp_request(
            cmd.CMD_OPEN_FISCAL_RECEIPT, header)
        return status


class DaisyIcpDevice(IcpDevice):
    """Daisy fiscal printers (ICP family).

    Tax group / payment letters identical to Datecs ICP.
    """

    URI_PREFIX = "bg.dy.icp"
    # _TAX_LETTERS, _PAYMENT_LETTERS inherited from IcpDevice (Datecs default)


class EltradeIcpDevice(IcpDevice):
    """Eltrade fiscal printers.

    Latin A..H tax groups; rich 11-letter payment alphabet covering all
    ErpNet.FP payment types one-to-one.
    """

    # Серийните на Eltrade започват с ED, не EL
    # (BgEltradeIcpFiscalPrinterDriver: SerialNumberPrefix = "ED").
    URI_PREFIX = "bg.ed.icp"

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


class IncotexIcpDevice(IcpDevice):
    """Incotex fiscal printers.

    Only 4 VAT slots A..D — `tax_group_letter` raises for G5..G8.
    """

    # 🚨 Тук стоеше "bg.is.icp" — това е URI-то на ICP Bulgaria
    # (вендор IS, протокол ICP), чужд производител. Incotex е IN и
    # говори ICP: BgIncotexIcpFiscalPrinterDriver → bg.in.icp.
    URI_PREFIX = "bg.in.icp"

    _TAX_LETTERS = {
        TaxGroup.G1: "A",
        TaxGroup.G2: "B",
        TaxGroup.G3: "C",
        TaxGroup.G4: "D",
    }
    # Payment letters identical to Datecs default


__all__ = [
    "DatecsIcpDevice",
    "DatecsIcpXDevice",
    "DaisyIcpDevice",
    "EltradeIcpDevice",
    "IncotexIcpDevice",
]
