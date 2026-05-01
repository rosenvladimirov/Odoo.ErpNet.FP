"""
Convert vendor errors / status into ErpNet.FP `messages[]` envelope.

ErpNet.FP responses follow the shape:
    { "ok": bool, "messages": [{"type": "info|warning|error", "code": "...",
                                 "text": "..."}], ... }

Our drivers raise `FiscalError` with negative `.code` and produce a
`FiscalStatus` from cmd 0x4A's 8-byte response. This module converts
both to `messages[]` entries so the HTTP layer never has to know about
vendor-specific exception types.
"""

from __future__ import annotations

from typing import Iterable

from ...drivers.fiscal.datecs_pm.errors import FiscalError
from ...drivers.fiscal.datecs_pm.status import FiscalStatus
from ..schemas import MessageType, StatusMessage


def from_fiscal_error(exc: Exception) -> StatusMessage:
    """Wrap any exception as a `messages[]` entry."""
    if isinstance(exc, FiscalError):
        return StatusMessage(
            type=MessageType.error,
            code=f"E{abs(exc.code):06d}" if exc.code else None,
            text=exc.description or exc.name or str(exc),
        )
    return StatusMessage(
        type=MessageType.error,
        code=None,
        text=str(exc) or exc.__class__.__name__,
    )


def from_status(status: FiscalStatus) -> list[StatusMessage]:
    """Translate non-OK status flags into `messages[]` entries.

    Critical flags become `error`, paper / EJ-near-full warnings become
    `warning`, and the FM "set" flags become `info` (matches ErpNet.FP
    convention — see PROTOCOL.md §"Get Printer Status" example).
    """
    out: list[StatusMessage] = []

    # Errors
    if status.syntax_error:
        out.append(StatusMessage(type=MessageType.error, code="E001", text="Syntax error"))
    if status.invalid_command:
        out.append(StatusMessage(type=MessageType.error, code="E002", text="Invalid command"))
    if status.not_permitted:
        out.append(StatusMessage(type=MessageType.error, code="E003", text="Command not permitted"))
    if status.overflow:
        out.append(StatusMessage(type=MessageType.error, code="E004", text="Overflow"))
    if status.cover_open:
        out.append(StatusMessage(type=MessageType.error, code="E005", text="Cover is open"))
    if status.print_failure:
        out.append(StatusMessage(type=MessageType.error, code="E006", text="Print mechanism failure"))
    if status.end_of_paper:
        out.append(StatusMessage(type=MessageType.error, code="E007", text="End of paper"))
    if status.fm_damaged:
        out.append(StatusMessage(type=MessageType.error, code="E008", text="Fiscal memory damaged"))
    if status.fm_full:
        out.append(StatusMessage(type=MessageType.error, code="E201", text="Fiscal memory full"))
    if status.fm_access_error:
        out.append(StatusMessage(type=MessageType.error, code="E202", text="Error accessing fiscal memory"))
    if status.ej_full:
        out.append(StatusMessage(type=MessageType.error, code="E101", text="Electronic Journal is full"))

    # Warnings
    if status.near_paper_end:
        out.append(StatusMessage(type=MessageType.warning, code="W001", text="Near end of paper"))
    if status.fm_low_space:
        out.append(StatusMessage(type=MessageType.warning, code="W201", text="The fiscal memory almost full"))
    if status.ej_nearly_full:
        out.append(StatusMessage(type=MessageType.warning, code="W101", text="Electronic Journal nearly full"))
    if status.rtc_unsynced:
        out.append(StatusMessage(type=MessageType.warning, code="W002", text="Real-time clock not synchronised"))

    # Info (matches ErpNet.FP "Get Printer Status" example)
    if status.serial_fm_set:
        out.append(StatusMessage(type=MessageType.info, text="Serial number and number of FM are set"))
    if status.tax_number_set:
        out.append(StatusMessage(type=MessageType.info, text="Tax number is set"))
    if status.fm_formatted:
        out.append(StatusMessage(type=MessageType.info, text="FM is formatted"))
    if status.fiscalized:
        out.append(StatusMessage(type=MessageType.info, text="Device is fiscalized"))
    if status.vat_set:
        out.append(StatusMessage(type=MessageType.info, text="VAT rates are set at least once"))

    return out


def merge(*groups: Iterable[StatusMessage]) -> list[StatusMessage]:
    """Concatenate several message lists, preserving order."""
    out: list[StatusMessage] = []
    for g in groups:
        out.extend(g)
    return out
