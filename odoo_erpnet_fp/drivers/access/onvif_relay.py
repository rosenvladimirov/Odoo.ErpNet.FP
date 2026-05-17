"""
ONVIF Device IO relay actuator — reuses the Phase A `OnvifNativeClient`.

Many edge-ANPR cameras have an on-board relay that drives the barrier
directly. Instead of a separate controller, the camera IS the
actuator. This wraps the existing synchronous client so the access
layer treats it identically to any other `AccessActuator`.
"""

from __future__ import annotations

from typing import Optional

from ..cameras.onvif_native import OnvifNativeClient
from .common import AccessActuator, AccessResult


class OnvifRelayActuator(AccessActuator):
    def __init__(
        self,
        controller_id: str,
        host: str,
        port: int = 80,
        user: str = "",
        password: str = "",
        relay_output: str = "",
        pulse_seconds: float = 3.0,
        fail_secure: bool = True,
    ) -> None:
        super().__init__(controller_id, fail_secure=fail_secure)
        self.default_pulse = float(pulse_seconds)
        self._cli = OnvifNativeClient(
            host=host, port=port, user=user, password=password,
            relay_output=relay_output,
        )

    def connect(self) -> None:
        if not self._cli.host:
            raise ValueError(
                f"Access {self.controller_id!r}: onvif needs host"
            )

    def disconnect(self) -> None:
        pass

    def open(self, pulse_seconds: Optional[float] = None) -> AccessResult:
        secs = self.default_pulse if pulse_seconds is None else pulse_seconds
        if secs and secs > 0:
            self._cli.pulse_relay(secs)
            return AccessResult(self.controller_id, "open", True,
                                "closed", f"pulsed {secs}s")
        self._cli.set_relay("active")
        return AccessResult(self.controller_id, "open", True, "open",
                            "latched open")

    def deny(self) -> AccessResult:
        self._cli.set_relay("inactive")
        return AccessResult(self.controller_id, "deny", True, "closed")
