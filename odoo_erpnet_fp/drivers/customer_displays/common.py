"""
Common types for customer display drivers.

A `CustomerDisplay` is an unidirectional output device — host writes
text + control commands; the display has no readback channel (status,
ack, etc.). Drivers wrap a serial transport and translate semantic
calls (`display_total`, `display_change`) into vendor-specific byte
streams.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal
from typing import Optional


@dataclass
class DisplayCapabilities:
    """Static capability matrix — used by /displays/{id} to advertise
    what the host may invoke without surprises."""

    chars_per_line: int = 20
    lines: int = 2
    brightness_levels: int = 4   # 0=off, 1-4 for DPD-201; 0 for fixed
    supports_blink: bool = True
    supports_cursor: bool = True
    supports_annunciators: bool = False
    encoding: str = "cp437"


class CustomerDisplay(ABC):
    """ABC for pole / customer-facing displays (VFD, LCD).

    Lifecycle:
        d = DatecsDpd201(port="/dev/ttyUSB1", baudrate=9600)
        d.open()
        d.clear()
        d.display_two_lines("MILK 2L", "    1.99 EUR")
        ...
        d.close()
    """

    capabilities: DisplayCapabilities = DisplayCapabilities()

    def __init__(self, display_id: str) -> None:
        self.display_id = display_id

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def clear(self) -> None: ...

    @abstractmethod
    def display_two_lines(self, top: str, bottom: str) -> None: ...

    @abstractmethod
    def display_line(self, line: int, text: str) -> None:
        """`line` is 1-based (1 = top, 2 = bottom)."""

    @abstractmethod
    def set_brightness(self, level: int) -> None:
        """`level` 0–4. 0 = off (display blank). 1-4 = 40/60/80/100 %."""

    def display_total(
        self,
        label: str,
        amount: Decimal,
        currency: str = "",
    ) -> None:
        """Convenience: top = label, bottom = right-aligned amount + currency."""
        cap = self.capabilities
        amt = f"{amount:.2f} {currency}".strip()
        bottom = amt.rjust(cap.chars_per_line)
        self.display_two_lines(label[: cap.chars_per_line], bottom)

    def display_change(
        self,
        amount: Decimal,
        currency: str = "",
        label: str = "ЗА ВРЪЩАНЕ",
    ) -> None:
        self.display_total(label, amount, currency)

    def self_test(self) -> None:
        """Optional — drivers without self-test should leave default no-op."""
