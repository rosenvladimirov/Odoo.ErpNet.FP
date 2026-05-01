"""
Datecs PM v2.11.4 error code → FiscalError.

457 codes in 28 categories, all negative integers. Source of truth:
data/error_codes.csv (distilled from PDF v2.11.4 / 17-Nov-2022).

Convention from the spec:
  * ErrorCode = 0 → operation OK; lookup() returns None.
  * Otherwise the code is in DATA payload of the response.
"""

import csv
from dataclasses import dataclass
from pathlib import Path

_CSV_PATH = Path(__file__).resolve().parent / "data" / "error_codes.csv"


@dataclass(frozen=True)
class ErrorInfo:
    code: int
    name: str
    category: str
    description: str


class FiscalError(Exception):
    """Raised when the device returns a non-zero ErrorCode in DATA."""

    def __init__(
        self,
        code: int,
        name: str = "UNKNOWN",
        category: str = "UNKNOWN",
        description: str = "",
    ):
        self.code = code
        self.name = name
        self.category = category
        self.description = description
        super().__init__(f"[{code} {name}] {description}")


_TABLE: dict[int, ErrorInfo] = {}


def _load() -> None:
    if _TABLE:
        return
    with _CSV_PATH.open("r", encoding="utf-8") as fp:
        for row in csv.DictReader(fp):
            code = int(row["code"])
            _TABLE[code] = ErrorInfo(
                code=code,
                name=row["name"],
                category=row["category"],
                description=row["description"],
            )


def lookup(code: int) -> ErrorInfo | None:
    """Return the ErrorInfo for `code`, or None if `code == 0` (OK)."""
    if code == 0:
        return None
    _load()
    return _TABLE.get(code)


def raise_for_code(code: int) -> None:
    """Raise FiscalError if code is non-zero."""
    if code == 0:
        return
    info = lookup(code)
    if info is None:
        raise FiscalError(code=code)
    raise FiscalError(
        code=info.code,
        name=info.name,
        category=info.category,
        description=info.description,
    )


def all_codes() -> dict[int, ErrorInfo]:
    """Return a copy of the full error table."""
    _load()
    return dict(_TABLE)
