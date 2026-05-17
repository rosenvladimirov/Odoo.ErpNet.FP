"""
Wiegand-out actuator — SCAFFOLD.

Driving a Wiegand reader port *as output* (emulating a card to an
existing access panel) means bit-banging the D0/D1 lines with tight
sub-millisecond timing — realistically a small MCU/Pi co-process, not
the fiscal Python process. Wired into the registry as a known driver
so config validates; the implementation is deferred until there is
real hardware to test against (same honesty rule as the rest).
"""

from __future__ import annotations

from typing import Optional

from .common import AccessActuator, AccessResult

_MSG = ("wiegand-out actuator not implemented yet — needs a "
        "timing-accurate D0/D1 bit-banger (MCU/Pi co-process). "
        "Use relay_tcp / onvif / gpio for now.")


class WiegandActuator(AccessActuator):
    def __init__(self, controller_id: str, **kw) -> None:
        super().__init__(controller_id,
                         fail_secure=kw.get("fail_secure", True))

    def connect(self) -> None:
        raise RuntimeError(_MSG)

    def disconnect(self) -> None:
        pass

    def open(self, pulse_seconds: Optional[float] = None) -> AccessResult:
        raise RuntimeError(_MSG)

    def deny(self) -> AccessResult:
        raise RuntimeError(_MSG)
