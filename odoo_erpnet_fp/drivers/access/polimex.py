"""
Polimex iCON access controller — WebSDK direct-command actuator.

Polimex Holding (Sofia, BG) iCON 50/110/115/130/180 controllers are
fronted by a "webstack" LAN/WiFi module exposing an open HTTP/JSON
**WebSDK**. The synchronous "direct command" mode is one immediate
HTTP POST that fires the relay and returns the result — exactly our
zero-latency requirement (NOT the queued event-answer / NAT mode,
which is the equivalent of the rejected Fleet 60 s queue, and which
requires the webstack to be directly reachable from this proxy).

Wire protocol (re-implemented from the public WebSDK spec —
https://websdk.polimex.online/docs/ — and the AGPL-3 reference impl
github.com/polimex/polimex-rfid `hr_rfid`; this is an independent
clean implementation of the documented protocol, not a code copy):

    POST http://<webstack>/sdk/cmd.json        (HTTP Basic: SDK / key)
    {"cmd": {"id": <bus controller id>,
             "c":  "DB",                        # "Open Output"
             "d":  "<OO><SS><TT>"}}             # see _payload()
      OO = output number, 2 hex digits  (e.g. door lock output)
      SS = state, 2 dec digits          1 = open, 0 = close
      TT = auto-close seconds, 2 dec    00 = latched, 01–99 = momentary

The controller auto-closes after TT seconds, so a momentary "open
3 s" is a single round-trip — no second call, no thread sleep.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from .common import AccessActuator, AccessResult

_logger = logging.getLogger(__name__)


class PolimexWebSdkActuator(AccessActuator):
    def __init__(
        self,
        controller_id: str,
        host: str,
        port: int = 80,
        user: str = "SDK",
        password: str = "0000",
        bus_id: int = 1,
        output: int = 1,
        pulse_seconds: float = 3.0,
        timeout: float = 4.0,
        fail_secure: bool = True,
    ) -> None:
        super().__init__(controller_id, fail_secure=fail_secure)
        self.host = host
        self.port = int(port)
        self.user = user or "SDK"
        self.password = password or "0000"
        self.bus_id = int(bus_id)          # iCON id on the RS-485 bus
        self.output = int(output)          # door lock output number
        self.default_pulse = float(pulse_seconds)
        self.timeout = float(timeout)
        self._cli: Optional[httpx.Client] = None

    def connect(self) -> None:
        if not self.host:
            raise ValueError(
                f"Access {self.controller_id!r}: polimex needs host "
                f"(webstack IP)"
            )
        if self._cli is None:
            self._cli = httpx.Client(
                timeout=self.timeout,
                auth=(self.user, self.password),
            )

    def disconnect(self) -> None:
        if self._cli is not None:
            try:
                self._cli.close()
            finally:
                self._cli = None

    @staticmethod
    def _payload(output: int, state: int, seconds: int) -> str:
        # '%02x%02d%02d' — output hex, state dec, time dec (как в
        # референтната имплементация за non-relay контролер).
        secs = max(0, min(int(seconds), 99))
        return "%02x%02d%02d" % (output, 1 if state else 0, secs)

    def _send(self, state: int, seconds: int) -> dict:
        self.connect()
        body = {"cmd": {"id": self.bus_id, "c": "DB",
                        "d": self._payload(self.output, state, seconds)}}
        base = f"http://{self.host}"
        if self.port and self.port != 80:
            base = f"http://{self.host}:{self.port}"
        resp = self._cli.post(f"{base}/sdk/cmd.json", json=body)
        resp.raise_for_status()
        try:
            return resp.json()
        except Exception:  # noqa: BLE001
            return {}

    def open(self, pulse_seconds: Optional[float] = None) -> AccessResult:
        secs = self.default_pulse if pulse_seconds is None else pulse_seconds
        secs = int(secs or 0)
        self._send(state=1, seconds=secs)
        if secs > 0:
            # Контролерът сам затваря след `secs` — single round-trip.
            return AccessResult(self.controller_id, "open", True,
                                "closed", f"pulsed {secs}s (auto-close)")
        return AccessResult(self.controller_id, "open", True, "open",
                            "latched open")

    def deny(self) -> AccessResult:
        self._send(state=0, seconds=0)
        return AccessResult(self.controller_id, "deny", True, "closed")
