"""
GPIO relay actuator — Raspberry Pi / SBC at the gate.

`gpiozero` is a LAZY optional import (extra `[gpio]`). On a non-Pi host
or without the lib, `connect()` raises a clear RuntimeError — it never
takes the proxy down.
"""

from __future__ import annotations

import time
from typing import Optional

from .common import AccessActuator, AccessResult


class GpioActuator(AccessActuator):
    def __init__(
        self,
        controller_id: str,
        pin: int,
        active_high: bool = True,
        pulse_seconds: float = 3.0,
        fail_secure: bool = True,
    ) -> None:
        super().__init__(controller_id, fail_secure=fail_secure)
        self.pin = int(pin)
        self.active_high = bool(active_high)
        self.default_pulse = float(pulse_seconds)
        self._dev = None

    def connect(self) -> None:
        if self._dev is not None:
            return
        try:
            from gpiozero import OutputDevice  # type: ignore
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(
                "gpiozero not available — `pip install "
                "'odoo-erpnet-fp[gpio]'` on the Pi at the gate"
            ) from exc
        self._dev = OutputDevice(
            self.pin, active_high=self.active_high, initial_value=False
        )

    def disconnect(self) -> None:
        if self._dev is not None:
            try:
                self._dev.close()
            finally:
                self._dev = None

    def open(self, pulse_seconds: Optional[float] = None) -> AccessResult:
        self.connect()
        self._dev.on()
        secs = self.default_pulse if pulse_seconds is None else pulse_seconds
        if secs and secs > 0:
            time.sleep(max(0.1, min(float(secs), 30.0)))
            self._dev.off()
            return AccessResult(self.controller_id, "open", True,
                                "closed", f"pulsed {secs}s")
        return AccessResult(self.controller_id, "open", True, "open")

    def deny(self) -> AccessResult:
        self.connect()
        self._dev.off()
        return AccessResult(self.controller_id, "deny", True, "closed")
