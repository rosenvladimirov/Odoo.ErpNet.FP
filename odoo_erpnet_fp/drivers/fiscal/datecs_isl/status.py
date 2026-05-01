"""
ISL status byte decoder.

Per Datecs ISL spec the response carries 6 status bytes (8 on FMP/FP v2).
Each bit signals a specific condition (paper, fiscal memory, syntax error,
etc.). This module converts the raw bytes into a list of ErpNet.FP-style
`StatusMessage`s ready to be returned by the HTTP layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class StatusMessageType(str, Enum):
    info = "info"
    warning = "warning"
    error = "error"
    reserved = "reserved"


@dataclass
class StatusMessage:
    type: StatusMessageType
    code: str | None
    text: str


@dataclass
class DeviceStatus:
    messages: list[StatusMessage] = field(default_factory=list)
    errors: list[StatusMessage] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def add(self, msg: StatusMessage) -> None:
        if msg.type == StatusMessageType.error:
            self.errors.append(msg)
        else:
            self.messages.append(msg)

    def add_error(self, code: str, text: str) -> None:
        self.errors.append(StatusMessage(StatusMessageType.error, code, text))

    def add_warning(self, code: str, text: str) -> None:
        self.messages.append(StatusMessage(StatusMessageType.warning, code, text))

    def add_info(self, text: str) -> None:
        self.messages.append(StatusMessage(StatusMessageType.info, None, text))


def parse_status_bytes(status_bytes: bytes) -> DeviceStatus:
    """Decode 6 (or 8) status bytes per Datecs ISL spec."""
    status = DeviceStatus()
    if not status_bytes or len(status_bytes) < 6:
        return status

    # Byte 0 — Syntax & communication errors
    if status_bytes[0] & 0x01:
        status.add_error("E401", "Syntax error in the received data")
    if status_bytes[0] & 0x02:
        status.add_error("E402", "Invalid command code received")
    if status_bytes[0] & 0x04:
        status.add_error("E103", "The clock is not set")
    if status_bytes[0] & 0x20:
        status.add_error("E199", "General error")
    if status_bytes[0] & 0x40:
        status.add_error("E302", "The printer cover is open")

    # Byte 1 — Command execution
    if status_bytes[1] & 0x01:
        status.add_error("E403", "Overflow of some amount fields")
    if status_bytes[1] & 0x02:
        status.add_error("E404", "Command not allowed in the current fiscal mode")

    # Byte 2 — Paper / receipt
    if status_bytes[2] & 0x01:
        status.add_error("E301", "No paper")
    if status_bytes[2] & 0x02:
        status.add_warning("W301", "Near end of paper")
    if status_bytes[2] & 0x04:
        status.add_error("E206", "End of the EJ")
    if status_bytes[2] & 0x10:
        status.add_warning("W202", "The end of the EJ is near")

    # Byte 4 — Fiscal memory
    if status_bytes[4] & 0x01:
        status.add_error("E202", "Fiscal memory store error")
    if status_bytes[4] & 0x08:
        status.add_warning(
            "W201", "There is space for less than 50 records remaining in the FP"
        )
    if status_bytes[4] & 0x10:
        status.add_error("E201", "The fiscal memory is full")
    if status_bytes[4] & 0x20:
        status.add_error("E299", "FM general error")
    if status_bytes[4] & 0x40:
        status.add_error("E304", "The printing head is overheated")

    return status
