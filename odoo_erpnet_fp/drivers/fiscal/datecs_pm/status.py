"""
Decode the 8-byte status payload of a Datecs PM v2.11.4 response.

Bit map per docs/status_bits.csv. Bit 7 of every byte is always 1, which
distinguishes a wrapped frame from a non-wrapped control byte (NAK 0x15 /
SYN 0x16, where bit 7 is 0).

Two aggregate flags are exposed by the device itself:
  byte 0 bit 5 = OR of all `#` flags  (general_error)
  byte 4 bit 5 = OR of all `*` flags  (fm_error_aggregate)
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class FiscalStatus:
    """Structured view over 8 raw status bytes."""

    raw: bytes

    # Byte 0 — General
    cover_open: bool
    general_error: bool  # OR of all `#` flags
    print_failure: bool
    rtc_unsynced: bool
    invalid_command: bool  # `#`
    syntax_error: bool  # `#`

    # Byte 1 — General
    not_permitted: bool  # `#`
    overflow: bool  # `#`

    # Byte 2 — Receipt state
    nonfiscal_open: bool
    ej_nearly_full: bool
    fiscal_open: bool
    ej_full: bool
    near_paper_end: bool
    end_of_paper: bool  # `#`

    # Byte 4 — Fiscal Memory
    fm_damaged: bool
    fm_error_aggregate: bool  # OR of all `*` flags
    fm_full: bool  # `*`
    fm_low_space: bool  # < 60 reports remaining
    serial_fm_set: bool
    tax_number_set: bool
    fm_access_error: bool  # `*`

    # Byte 5 — General
    vat_set: bool
    fiscalized: bool
    fm_formatted: bool

    @classmethod
    def parse(cls, b: bytes) -> "FiscalStatus":
        if len(b) != 8:
            raise ValueError(f"Status must be 8 bytes, got {len(b)}")
        for i, byte in enumerate(b):
            if not (byte & 0x80):
                raise ValueError(
                    f"Status byte {i} bit 7 must be 1 (got 0x{byte:02X})"
                )

        b0, b1, b2, _b3, b4, b5, _b6, _b7 = b
        return cls(
            raw=bytes(b),
            cover_open=bool(b0 & 0x40),
            general_error=bool(b0 & 0x20),
            print_failure=bool(b0 & 0x10),
            rtc_unsynced=bool(b0 & 0x04),
            invalid_command=bool(b0 & 0x02),
            syntax_error=bool(b0 & 0x01),
            not_permitted=bool(b1 & 0x02),
            overflow=bool(b1 & 0x01),
            nonfiscal_open=bool(b2 & 0x20),
            ej_nearly_full=bool(b2 & 0x10),
            fiscal_open=bool(b2 & 0x08),
            ej_full=bool(b2 & 0x04),
            near_paper_end=bool(b2 & 0x02),
            end_of_paper=bool(b2 & 0x01),
            fm_damaged=bool(b4 & 0x40),
            fm_error_aggregate=bool(b4 & 0x20),
            fm_full=bool(b4 & 0x10),
            fm_low_space=bool(b4 & 0x08),
            serial_fm_set=bool(b4 & 0x04),
            tax_number_set=bool(b4 & 0x02),
            fm_access_error=bool(b4 & 0x01),
            vat_set=bool(b5 & 0x10),
            fiscalized=bool(b5 & 0x08),
            fm_formatted=bool(b5 & 0x02),
        )

    def has_critical_error(self) -> bool:
        """True if any error that aborts the current command is set.

        These are the flags that mean *the command was not executed* —
        the host must NOT retry the same command (only NAK retries
        indicate transport-level failure).
        """
        return (
            self.syntax_error
            or self.invalid_command
            or self.not_permitted
            or self.overflow
            or self.general_error
            or self.fm_error_aggregate
        )

    def errors(self) -> list[str]:
        """Human-readable list of set error/warning flags."""
        out: list[str] = []
        if self.syntax_error:
            out.append("syntax error")
        if self.invalid_command:
            out.append("invalid command")
        if self.not_permitted:
            out.append("command not permitted")
        if self.overflow:
            out.append("overflow")
        if self.cover_open:
            out.append("cover open")
        if self.print_failure:
            out.append("printing mechanism failure")
        if self.rtc_unsynced:
            out.append("RTC not synchronized")
        if self.end_of_paper:
            out.append("end of paper")
        if self.near_paper_end:
            out.append("near end of paper")
        if self.ej_full:
            out.append("EJ is full")
        if self.ej_nearly_full:
            out.append("EJ nearly full")
        if self.fm_damaged:
            out.append("fiscal memory damaged")
        if self.fm_full:
            out.append("fiscal memory full")
        if self.fm_low_space:
            out.append("less than 60 reports remaining in FM")
        if self.fm_access_error:
            out.append("error accessing fiscal memory")
        return out
