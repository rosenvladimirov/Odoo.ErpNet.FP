"""
MIV Electronics access controller — VENDOR SLOT (protocol pending).

The roadmap names MIV Electronics as Phase B target hardware. Their
controller protocol is not yet specified (Rosen to obtain it from the
vendor). `AccessActuator` is deliberately vendor-agnostic, so the MIV
protocol slots in here as one transport — exactly the Borica/myPOS
pinpad approach — without touching the others.

Until the spec arrives this is an honest stub: config validates and
the slot is reserved, but commands raise a clear error rather than
pretend. `host`/`port`/`extras` are already wired so the real
implementation only fills in `connect/open/deny`.
"""

from __future__ import annotations

from typing import Optional

from .common import AccessActuator, AccessResult

_MSG = ("MIV Electronics protocol not specified yet — pending vendor "
        "spec. AccessActuator slot is reserved (host/port wired); "
        "drop in the protocol here when available.")


class MivActuator(AccessActuator):
    def __init__(
        self,
        controller_id: str,
        host: str = "",
        port: int = 0,
        extras: Optional[dict] = None,
        fail_secure: bool = True,
    ) -> None:
        super().__init__(controller_id, fail_secure=fail_secure)
        self.host = host
        self.port = int(port or 0)
        self.extras = extras or {}

    def connect(self) -> None:
        raise NotImplementedError(_MSG)

    def disconnect(self) -> None:
        pass

    def open(self, pulse_seconds: Optional[float] = None) -> AccessResult:
        raise NotImplementedError(_MSG)

    def deny(self) -> AccessResult:
        raise NotImplementedError(_MSG)
