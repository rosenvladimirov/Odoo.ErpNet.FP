"""
Generic network-relay actuator.

Covers the cheap TCP relay boards common at gates (KMtronic, Numato,
USR-R16, LC-Relay, generic ESP relay firmwares): open a socket, send a
small on/off command, close. Connect-per-command (no long-lived fd →
no stale-socket class of bug, same rationale as the per-use display
open).

`on_cmd` / `off_cmd` accept either literal text (`"on1"`, `"FF0106..."`)
or a `hex:` prefix for raw bytes (`"hex:A00101A2"`). A trailing `\\n`
is honoured via standard escapes.
"""

from __future__ import annotations

import logging
import socket
import time
from typing import Optional

from .common import AccessActuator, AccessResult

_logger = logging.getLogger(__name__)


def _to_bytes(s: str) -> bytes:
    s = s or ""
    if s.startswith("hex:"):
        return bytes.fromhex(s[4:].replace(" ", ""))
    # позволяваме \n \r \t \xNN escape-и в config-стойността
    return s.encode("utf-8").decode("unicode_escape").encode("latin-1")


class RelayTcpActuator(AccessActuator):
    def __init__(
        self,
        controller_id: str,
        host: str,
        port: int = 23,
        on_cmd: str = "on",
        off_cmd: str = "off",
        pulse_seconds: float = 3.0,
        timeout: float = 4.0,
        fail_secure: bool = True,
    ) -> None:
        super().__init__(controller_id, fail_secure=fail_secure)
        self.host = host
        self.port = int(port)
        self._on = _to_bytes(on_cmd)
        self._off = _to_bytes(off_cmd)
        self.default_pulse = float(pulse_seconds)
        self.timeout = float(timeout)

    def connect(self) -> None:  # noqa: D401 — lazy per-command
        if not self.host:
            raise ValueError(
                f"Access {self.controller_id!r}: relay_tcp needs host"
            )

    def disconnect(self) -> None:
        pass

    def _send(self, payload: bytes) -> None:
        with socket.create_connection(
            (self.host, self.port), timeout=self.timeout
        ) as s:
            s.sendall(payload)

    def open(self, pulse_seconds: Optional[float] = None) -> AccessResult:
        self.connect()
        self._send(self._on)
        secs = self.default_pulse if pulse_seconds is None else pulse_seconds
        if secs and secs > 0:
            time.sleep(max(0.1, min(float(secs), 30.0)))
            self._send(self._off)
            return AccessResult(
                self.controller_id, "open", True, "closed",
                f"pulsed {secs}s",
            )
        return AccessResult(self.controller_id, "open", True, "open",
                            "latched open")

    def deny(self) -> AccessResult:
        self.connect()
        self._send(self._off)
        return AccessResult(self.controller_id, "deny", True, "closed")
