"""
Payment type mapping: ErpNet.FP string ↔ vendor numeric code.

ErpNet.FP exposes 11 named payment types (PROTOCOL.md §"payments"):
  cash, check, card, coupons, ext-coupons, packaging, internal-usage,
  damage, bank, reserved1, reserved2

Datecs PM cmd 0x35 (Payment & total) takes payment_type 0..7 per PDF §9.1
where 0=Cash, 1=Card (no pinpad), 2..7 = device-programmed methods.

The 11→8 collision is resolved by mapping less common ErpNet.FP types
onto the same physical Datecs slots — the device's payment-type
programming (cmd 0xFF parameter `PayName`) determines the receipt label.

Bulgarian convention (mirrors what `l10n_bg_erp_net_fp` JS picks):
  cash            → 0    (slot 0 = "В БРОЙ")
  card            → 1    (slot 1 = "С КАРТА")
  check           → 2    (slot 2 = "ЧЕК")
  coupons         → 3
  ext-coupons     → 4
  packaging       → 5
  bank            → 6
  internal-usage  → 7
  damage          → 7    (overflow — admin should reprogram device)
  reserved1       → 7
  reserved2       → 7
"""

from __future__ import annotations

from ..schemas import PaymentType


_DATECS_PM_DEFAULT: dict[PaymentType, int] = {
    PaymentType.cash: 0,
    PaymentType.card: 1,
    PaymentType.check: 2,
    PaymentType.coupons: 3,
    PaymentType.ext_coupons: 4,
    PaymentType.packaging: 5,
    PaymentType.bank: 6,
    PaymentType.internal_usage: 7,
    PaymentType.damage: 7,
    PaymentType.reserved1: 7,
    PaymentType.reserved2: 7,
}


_DATECS_ISL_DEFAULT: dict[PaymentType, int] = {
    # ISL drivers use letter codes (P=cash, C=card, N=check, D=reserved1).
    # The integer here is informational — only `supported_for` reads it.
    PaymentType.cash: 0,
    PaymentType.card: 1,
    PaymentType.check: 2,
    PaymentType.reserved1: 3,
}


_ELTRADE_DEFAULT: dict[PaymentType, int] = {
    # Eltrade exposes the full ErpNet.FP set 1:1.
    PaymentType.cash: 0,
    PaymentType.check: 1,
    PaymentType.coupons: 2,
    PaymentType.ext_coupons: 3,
    PaymentType.packaging: 4,
    PaymentType.internal_usage: 5,
    PaymentType.damage: 6,
    PaymentType.card: 7,
    PaymentType.bank: 8,
    PaymentType.reserved1: 9,
    PaymentType.reserved2: 10,
}


_VENDOR_OVERRIDES: dict[str, dict[PaymentType, int]] = {
    "datecs.pm": _DATECS_PM_DEFAULT,
    "datecs.isl": _DATECS_ISL_DEFAULT,
    "daisy.isl": _DATECS_ISL_DEFAULT,
    "tremol.isl": _DATECS_ISL_DEFAULT,
    "incotex.isl": _DATECS_ISL_DEFAULT,
    "eltrade.isl": _ELTRADE_DEFAULT,
}


def to_code(payment_type: PaymentType, driver: str = "datecs.pm") -> int:
    table = _VENDOR_OVERRIDES.get(driver, _DATECS_PM_DEFAULT)
    return table[payment_type]


def supported_for(driver: str = "datecs.pm") -> list[str]:
    """Return the list of `paymentType` strings the driver can render —
    used for the `supportedPaymentTypes` field in `DeviceInfo`.
    """
    table = _VENDOR_OVERRIDES.get(driver, _DATECS_PM_DEFAULT)
    return [pt.value for pt in table]
