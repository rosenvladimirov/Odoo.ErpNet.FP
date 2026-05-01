"""
Tax group mapping: ErpNet.FP integer (1..8) ↔ vendor letter.

ErpNet.FP exposes taxGroup as an integer per PROTOCOL.md §"items":
  > "taxGroup" - the government regulated tax group. An integer from 1 to 8.

Datecs PM v2.11.4 (PDF §4.55.1) takes a single character — Latin
'A'..'H' or Cyrillic 'А'..'З' — corresponding to `valVat[0..7]`
(parameter cmd 0xFF).

Bulgarian convention (per `l10n_bg_erp_net_fp.account_tax_group`):
  1 → А (or A) — VAT 0%        (rate index 0)
  2 → Б (or B) — VAT 20%       (rate index 1, standard)
  3 → В (or C) — VAT 9%        (rate index 2, reduced)
  4 → Г (or D) — VAT exempt    (rate index 3)

Slots 5..8 are device-programmable; defaults below match common BG
configurations and can be overridden via `_VENDOR_OVERRIDES`.
"""

from __future__ import annotations

# Default integer → Cyrillic letter map for Datecs PM. Indices match
# ErpNet.FP's 1..8 numbering.
_DATECS_PM_DEFAULT: dict[int, str] = {
    1: "А",
    2: "Б",
    3: "В",
    4: "Г",
    5: "Д",
    6: "Е",
    7: "Ж",
    8: "З",
}

# Per-vendor-driver overrides, keyed by dotted driver path.
_VENDOR_OVERRIDES: dict[str, dict[int, str]] = {
    "datecs.pm": _DATECS_PM_DEFAULT,
}


def to_letter(tax_group: int, driver: str = "datecs.pm") -> str:
    """ErpNet.FP integer (1..8) → Cyrillic letter for the given driver."""
    if not 1 <= tax_group <= 8:
        raise ValueError(f"taxGroup must be 1..8, got {tax_group}")
    table = _VENDOR_OVERRIDES.get(driver, _DATECS_PM_DEFAULT)
    return table[tax_group]


def to_int(letter: str, driver: str = "datecs.pm") -> int:
    """Cyrillic letter → ErpNet.FP integer (inverse of `to_letter`)."""
    table = _VENDOR_OVERRIDES.get(driver, _DATECS_PM_DEFAULT)
    inverse = {v: k for k, v in table.items()}
    if letter not in inverse:
        raise ValueError(
            f"Unknown VAT letter {letter!r} for driver {driver!r}"
        )
    return inverse[letter]
