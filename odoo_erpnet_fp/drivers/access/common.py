"""
Common types for access-control actuators (Phase B).

An `AccessActuator` is a command-style output device (barrier / relay /
turnstile) — the same shape as `CustomerDisplay`: the host issues
discrete commands, there is no continuous readback (an optional
`status()` is best-effort).

**The access DECISION is taken in Odoo** (Channel-1 ⊕ Channel-2,
fail-secure). This proxy only EXECUTES an explicit, already-authorised
command. It never auto-opens: no call → barrier stays shut. `deny()`
exists for turnstiles / audit symmetry and is a safe no-op on a plain
barrier relay.

Latency: every method is a single synchronous transport op (one TCP
write / one SOAP call / one GPIO toggle), awaited and returned — the
zero-queue-latency path Odoo drives directly (`POST /access/{id}/...`
or native IoT `/action`), NOT the 60 s Fleet command-queue.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class AccessResult:
    controller_id: str
    action: str            # open | deny | pulse | status
    ok: bool = True
    state: str = ""        # open | closed | unknown
    detail: str = ""
    timestamp: datetime = field(default_factory=datetime.now)

    def to_json(self) -> dict:
        return {
            "controllerId": self.controller_id,
            "action": self.action,
            "ok": self.ok,
            "state": self.state,
            "detail": self.detail,
            "timestamp": self.timestamp.strftime("%Y-%m-%dT%H:%M:%S.%f"),
        }


class AccessActuator(ABC):
    """ABC for barrier / relay / turnstile controllers.

    Lifecycle:
        a = RelayTcpActuator(controller_id="gate1", host="10.0.0.9", ...)
        a.connect()
        a.open(pulse_seconds=3)     # authorised by Odoo → execute
        ...
        a.disconnect()
    """

    def __init__(self, controller_id: str, fail_secure: bool = True) -> None:
        self.controller_id = controller_id
        # fail_secure е семантичен флаг за Odoo/оператора; прокситo и
        # без друго никога не отваря самò — документира намерението.
        self.fail_secure = fail_secure

    @abstractmethod
    def connect(self) -> None:
        """Acquire the transport (idempotent). Cheap transports may
        no-op and connect lazily per command."""

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def open(self, pulse_seconds: Optional[float] = None) -> AccessResult:
        """Open / grant. If `pulse_seconds` given → momentary (open,
        wait, close); else latched open until an explicit `deny()`."""

    @abstractmethod
    def deny(self) -> AccessResult:
        """Explicit close / deny. Safe no-op-ish on a plain barrier."""

    def status(self) -> AccessResult:
        """Best-effort state read. Default: unknown (write-only device)."""
        return AccessResult(
            controller_id=self.controller_id, action="status",
            ok=True, state="unknown",
            detail="actuator is write-only — no status channel",
        )
