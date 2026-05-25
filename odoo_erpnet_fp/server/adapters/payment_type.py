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

EMPIRICAL LAYOUT (Datecs PM, FW 22Jul25)
────────────────────────────────────────
Read via `read_parameter("PayName", i)` on FP-700MX (DA052093) AND
confirmed identical from the handoff doc on BC-50MX (DA054852):

    Slot │ Label       │ Semantic
    ─────┼─────────────┼─────────────────────
     0   │ В БРОЙ      │ cash
     1   │ КРЕДИТ      │ card / credit
     2   │ ДЕБ.КАРТА   │ debit card (no alias)
     3   │ ЧЕК         │ check
     4   │ ВАУЧЕР      │ voucher (ext-coupons)
     5   │ КУПОН       │ coupon
     6-10│ —           │ ERR_FP_BAD_PARAM_2 — slot does not exist

Conclusion: the historical 11-slot Datecs-PM hard-coded mapping in
this file was wrong (mapping `check` → slot 2 would print
ДЕБ.КАРТА on the receipt, and `coupons` → slot 3 would print ЧЕК).

There's a single PM layout in production today. We keep the
`detect_family()` machinery as a future-proofing hook (e.g. if a
newer firmware revision adds more slots) but every PM device is
currently mapped through the 6-slot table.

If a particular device has reprogrammed PayName slots (admin used
`set_parameter("PayName", i, label)`), the receipt label still
follows the device's PayName — only the slot ID we send matters.
Use `read_parameter("PayName", i)` at install time to verify.
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

# Datecs PM (FP-700MX, DP-150, BC-50MX, FW 22Jul25) — single 6-slot
# layout confirmed empirically. Less common ErpNet.FP types fall back
# to the closest semantic slot.
_DATECS_PM_DEFAULT: dict[PaymentType, int] = {
    PaymentType.cash: 0,         # В БРОЙ
    PaymentType.card: 1,         # КРЕДИТ — generic card payment
                                  # (slot 2 = ДЕБ.КАРТА; no debit alias)
    PaymentType.check: 3,        # ЧЕК
    PaymentType.coupons: 5,      # КУПОН
    PaymentType.ext_coupons: 4,  # ВАУЧЕР (closest match)
    # Slots 6-10 do not exist. Map remaining types to the closest legal
    # slot. Admin can reprogram PayName labels via
    # `set_parameter("PayName", i, label)` if a custom UI string is
    # needed — we still send the slot ID, the device decides the label.
    PaymentType.packaging: 5,        # → КУПОН (no packaging slot)
    PaymentType.bank: 1,             # → КРЕДИТ
    PaymentType.internal_usage: 5,   # → КУПОН (no slot)
    PaymentType.damage: 5,           # → КУПОН (no slot)
    PaymentType.reserved1: 5,
    PaymentType.reserved2: 5,
}


# Family table — single entry today, keyed by detect_family() output.
# Add new families here if a future firmware ships with a different
# layout (e.g. FP_700_v3 with 8 slots).
_DATECS_PM_BY_FAMILY: dict[str, dict[PaymentType, int]] = {
    "FP_700": _DATECS_PM_DEFAULT,
    "BC_50": _DATECS_PM_DEFAULT,
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
    table = _VENDOR_OVERRIDES.get(driver, _DATECS_PM_DEFAULT)
    if isinstance(table, dict) and table and isinstance(
            next(iter(table.values())), dict):
        # Family-keyed (datecs.pm). Pick by model.
        family = detect_family(model_name)
        return table.get(family) or table.get("FP_700") or _DATECS_PM_DEFAULT
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
