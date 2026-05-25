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

WHY DEVICE FAMILY MATTERS
─────────────────────────
Different printers in the same protocol family have DIFFERENT factory
slot programming. FP-700MX, DP-150 and BC-50MX all speak PM, but the
operator-visible labels at each slot differ:

    Slot │ FP-700 / DP-150       │ BC-50MX
    ─────┼───────────────────────┼──────────────────
     0   │ В БРОЙ                │ В БРОЙ
     1   │ С КАРТА  (generic)    │ КРЕДИТ  (credit)
     2   │ ЧЕК                   │ ДЕБ.КАРТА (debit)
     3   │ КУПОНИ                │ ЧЕК
     4   │ Ext. coupons          │ ВАУЧЕР
     5   │ Опаковки              │ КУПОН
     6   │ bank                  │ (does not exist)
     7   │ internal-usage        │ (does not exist)
     8-10│ damage / reserved     │ (do not exist)

If we paid by `coupons` and printed the receipt on a BC-50MX with the
FP-700 mapping (slot 3), the receipt would print ЧЕК — wrong label.
The fix is a per-family lookup keyed by the model string the device
returns via GET_INFO.

Empirically verified 2026-05-25 by reading `PayName` parameter slots
on each device via cmd 0xFF — see HANDOFF_PROXY_PAYMENT_ADAPTER.md.
"""

from __future__ import annotations

from ..schemas import PaymentType


# ─── family detection ───────────────────────────────────────────────


def detect_family(model_name: str) -> str:
    """Map an arbitrary model string (from device info / driver detect)
    to a payment-mapping family key.

    Patterns:
      "BC-50MX" / "BlueCash-50" / "BC-50"  → "BC_50"
      anything else (FP-700, FP-700MX, DP-150, FMP-350, …)  → "FP_700"

    Default to FP_700 because that's the dominant install base and
    matches the historical hardcoded mapping (so behaviour for known
    devices is unchanged after this refactor).
    """
    if not model_name:
        return "FP_700"
    m = model_name.upper()
    if m.startswith("BC-") or "BLUECASH" in m or "BLUE CASH" in m:
        return "BC_50"
    return "FP_700"


# ─── per-family payment slot maps ───────────────────────────────────

# FP-700 / FP-700MX / DP-150 — the layout we used to hard-code as the
# "PM default". Slot 6+ are factory-programmed for the rare types so
# they print sensible labels on the receipt.
_DATECS_PM_FP_700: dict[PaymentType, int] = {
    PaymentType.cash: 0,
    PaymentType.card: 1,
    PaymentType.check: 2,
    PaymentType.coupons: 3,
    PaymentType.ext_coupons: 4,
    PaymentType.packaging: 5,
    PaymentType.bank: 6,
    PaymentType.internal_usage: 7,
    # Last 3 collapse onto reserved slot 7 — admin should reprogram the
    # device label if they need a distinct one.
    PaymentType.damage: 7,
    PaymentType.reserved1: 7,
    PaymentType.reserved2: 7,
}


# BC-50MX — six active slots only. Types without a slot fall back to
# the closest semantic equivalent.
_DATECS_PM_BC_50: dict[PaymentType, int] = {
    PaymentType.cash: 0,         # В БРОЙ
    PaymentType.card: 1,         # КРЕДИТ — generic card payment
                                  # (slot 2 = ДЕБ.КАРТА; no debit alias yet)
    PaymentType.check: 3,        # ЧЕК
    PaymentType.coupons: 5,      # КУПОН (single coupon slot here)
    PaymentType.ext_coupons: 4,  # ВАУЧЕР (closest match)
    # Slots 6-10 do not exist on BC-50MX. Fall back to the
    # closest type so we still print SOMETHING legal. The admin can
    # extend the device's PayName table (cmd 0xFF set) if a custom
    # label is wanted.
    PaymentType.packaging: 5,    # → КУПОН (no packaging slot)
    PaymentType.bank: 1,         # → КРЕДИТ
    PaymentType.internal_usage: 5,
    PaymentType.damage: 5,
    PaymentType.reserved1: 5,
    PaymentType.reserved2: 5,
}


_DATECS_PM_BY_FAMILY: dict[str, dict[PaymentType, int]] = {
    "FP_700": _DATECS_PM_FP_700,
    "BC_50": _DATECS_PM_BC_50,
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


# Top-level dispatch — driver string → either a flat map (ISL, Eltrade)
# or a family-keyed dict (Datecs PM). `_resolve_map` picks the right
# leaf based on driver + optional `model_name`.
_VENDOR_OVERRIDES: dict[str, object] = {
    "datecs.pm": _DATECS_PM_BY_FAMILY,   # nested by family
    "datecs.isl": _DATECS_ISL_DEFAULT,
    "daisy.isl": _DATECS_ISL_DEFAULT,
    "tremol.isl": _DATECS_ISL_DEFAULT,
    "incotex.isl": _DATECS_ISL_DEFAULT,
    "eltrade.isl": _ELTRADE_DEFAULT,
}


def _resolve_map(driver: str,
                 model_name: str = "") -> dict[PaymentType, int]:
    table = _VENDOR_OVERRIDES.get(driver, _DATECS_PM_FP_700)
    if isinstance(table, dict) and table and isinstance(
            next(iter(table.values())), dict):
        # Family-keyed (datecs.pm). Pick by model.
        family = detect_family(model_name)
        return table.get(family) or table.get("FP_700") or _DATECS_PM_FP_700
    return table   # flat map (ISL, Eltrade)


def to_code(payment_type: PaymentType, driver: str = "datecs.pm",
            model_name: str = "") -> int:
    """Map an ErpNet.FP `paymentType` string to the device's numeric
    slot. `model_name` (free-text, as returned by GET_INFO) is used
    to disambiguate Datecs-PM family layouts; safe to leave empty
    for non-PM drivers.
    """
    table = _resolve_map(driver, model_name)
    return table[payment_type]


def supported_for(driver: str = "datecs.pm",
                  model_name: str = "") -> list[str]:
    """Return the list of `paymentType` strings the driver+model can
    render — used for the `supportedPaymentTypes` field in `DeviceInfo`.
    For BC-50MX this is the strict 5-type set (cash/card/check/
    coupons/ext-coupons); for FP-700 the full 11.
    """
    table = _resolve_map(driver, model_name)
    return [pt.value for pt in table]
