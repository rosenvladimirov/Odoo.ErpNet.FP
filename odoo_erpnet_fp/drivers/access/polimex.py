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

ONE driver covers the whole genuine Polimex range on the BG market
(iCON50/110/115/130/180 + SmartVend) — same WebSDK, same `DB`
command. **Relay-type** controllers (iCON-R, iCON110R; hw 30/31/32)
use the SAME `DB` command but a different `d` body — set
`relay_ctrl: true` (+ `mode` 1/2/3). The relay `d` layout is ported
1:1 from the AGPL reference (`hr_rfid_ctrl.change_output_state` /
`convert_int_to_cmd_data_for_output_control`); relay activation
duration is controller-side config, so relay-mode `deny()` is a
best-effort re-send (controllers auto-release).

NOT this driver: IDTECK "iCON100/iCON100SR" (different vendor —
name-collision trap, IDTECK serial/TCP) and SOYAL "iTDC" (AR-721,
SOYAL protocol). SBR-01CR/02CR only if behind a Polimex LAN converter.
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
        relay_ctrl: bool = False,
        mode: int = 2,
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
        self.output = int(output)          # door lock / relay output number
        self.relay_ctrl = bool(relay_ctrl)  # iCON-R / iCON110R hw 30/31/32
        self.mode = int(mode)              # relay-type: 1/2/3
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
    def _door_payload(output: int, state: int, seconds: int) -> str:
        # Door-type (iCON50/110/115/130/180/SmartVend):
        # '%02x%02d%02d' — output hex, state dec, time dec.
        secs = max(0, min(int(seconds), 99))
        return "%02x%02d%02d" % (output, 1 if state else 0, secs)

    @staticmethod
    def _relay_payload(output: int, mode: int) -> str:
        """Relay-type (iCON-R / iCON110R) — ported 1:1 from the AGPL
        reference (change_output_state relay branch +
        convert_int_to_cmd_data_for_output_control). Activation
        duration is controller-side, so no state/time here."""
        reader = 1 if mode != 2 else (1 if output < 17 else 2)
        if mode in (1, 2):
            data = 1 << (output - 1)
        elif mode == 3:
            data = output
        else:
            raise ValueError(f"polimex relay mode {mode!r} unsupported (1/2/3)")
        inner = "%03d%03d%03d%03d" % (
            (data >> 24) & 0xFF, (data >> 16) & 0xFF,
            (data >> 8) & 0xFF, data & 0xFF,
        )
        inner = "".join("0" + ch for ch in inner)  # → 24 chars
        return ("1F%02X" % reader) + inner

    def _payload(self, state: int, seconds: int) -> str:
        if self.relay_ctrl:
            return self._relay_payload(self.output, self.mode)
        return self._door_payload(self.output, state, seconds)

    def _send(self, state: int, seconds: int) -> dict:
        self.connect()
        body = {"cmd": {"id": self.bus_id, "c": "DB",
                        "d": self._payload(state, seconds)}}
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
        if self.relay_ctrl:
            return AccessResult(self.controller_id, "open", True, "open",
                                "relay activated (duration controller-side)")
        if secs > 0:
            # Контролерът сам затваря след `secs` — single round-trip.
            return AccessResult(self.controller_id, "open", True,
                                "closed", f"pulsed {secs}s (auto-close)")
        return AccessResult(self.controller_id, "open", True, "open",
                            "latched open")

    def deny(self) -> AccessResult:
        # Relay-type няма отделна „close" в reference-а (releases е
        # controller-timed) → best-effort пресъздаване на командата.
        self._send(state=0, seconds=0)
        return AccessResult(self.controller_id, "deny", True, "closed")
