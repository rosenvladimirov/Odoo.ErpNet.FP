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

    # Transient error codes from the Polimex webstack — both indicate the
    # bridge couldn't reach the controller this round (RS-485 collision,
    # internal bookkeeping). Retry with backoff before giving up.
    #   20 = "No Response from controller (WebSDK)"
    #   24 = "Internal Error, Try Again (WebSDK)"
    # Source: hr_rfid_command.py errors enum in github.com/polimex/polimex-rfid.
    _RETRYABLE_ERRORS = (20, 24)

    def _send_frame(self, c: str, d: str) -> dict:
        """POST a raw SDK frame `{"cmd":{"id":bus_id,"c":c,"d":d}}`,
        retrying on transient e=20/24 codes.

        Raises RuntimeError with a meaningful message on any non-transient
        error or after the retry budget is exhausted — the caller (door
        route or card-sync handler) translates that into a failure.
        """
        import time
        self.connect()
        body = {"cmd": {"id": self.bus_id, "c": c, "d": d}}
        base = f"http://{self.host}"
        if self.port and self.port != 80:
            base = f"http://{self.host}:{self.port}"
        url = f"{base}/sdk/cmd.json"

        last_err = "unknown"
        for attempt in range(1, 4):  # 3 attempts total
            resp = self._cli.post(url, json=body)
            resp.raise_for_status()
            try:
                data = resp.json()
            except Exception:  # noqa: BLE001
                data = {}
            inner = (data or {}).get("response") or {}
            err_code = inner.get("e")
            if err_code in (0, None):
                # Success or no error code reported — happy path.
                return data
            if err_code not in self._RETRYABLE_ERRORS:
                # Non-transient — give up immediately. e=21 means we sent
                # a malformed frame; e=14 unknown command; etc.
                raise RuntimeError(
                    f"Polimex error e={err_code} (non-transient) "
                    f"on bus_id={self.bus_id}, c={c}")
            last_err = f"e={err_code}"
            if attempt < 3:
                # Linear backoff: 0.4s, 1.0s.
                time.sleep(0.4 * attempt + 0.0)
        raise RuntimeError(
            f"Polimex {last_err} after 3 retries on bus_id={self.bus_id}, "
            f"c={c} — controller unreachable on the RS-485 bus")

    def _send(self, state: int, seconds: int) -> dict:
        """Open Output (DB) — momentary/latched door pulse."""
        return self._send_frame("DB", self._payload(state, seconds))

    # ── Card management (D1 Add/Delete Card) ─────────────────────────
    @staticmethod
    def _encode_d1_body(card_number: str, pin_code: str, ts_code: str,
                        rights_data: int, rights_mask: int) -> str:
        """Build the D1 d-body for a standard door controller (non-relay,
        non-temperature, non-alarm). Ported 1:1 from the AGPL reference
        hr_rfid `send_command` (github.com/polimex/polimex-rfid):

            d = card_num + pin_code + ts_code + rights_data + rights_mask

        - card_num / pin_code: each character prefixed with '0'
          (10-digit card → 20 chars; pin "0000" → "00000000")
        - ts_code: 8 hex chars = 4 bytes, one Time-Schedule slot number
          per reader (reader N → ts_code[(N-1)*2:][:2])
        - rights_data / rights_mask: 2 hex chars each — per-reader bitmask
          (reader N bit = 1 << (N-1)); mask marks which bits we set.
        """
        card_enc = "".join("0" + ch for ch in str(card_number))
        pin_enc = "".join("0" + ch for ch in str(pin_code or "0000"))
        ts = str(ts_code or "00000000")
        return card_enc + pin_enc + ts \
            + "{:02X}".format(int(rights_data) & 0xFF) \
            + "{:02X}".format(int(rights_mask) & 0xFF)

    def add_card(self, card_number: str, rights_data: int = 1,
                 rights_mask: int = 1, ts_code: str = "01000000",
                 pin_code: str = "0000") -> dict:
        """Write (or update) a card into the controller's local memory.

        Semantic inputs from Odoo; this driver owns the wire format.
        Defaults grant reader 1 (`rights_data=rights_mask=1`) on TS slot 1
        (`ts_code="01000000"` = always, reader 1) — Odoo overrides with the
        real per-reader rights + resource.calendar-mapped TS slot.

        relay-type controllers use a different D1 body — not yet ported;
        guard so we don't send a malformed frame.
        """
        if self.relay_ctrl:
            raise RuntimeError(
                f"Polimex {self.controller_id!r}: card programming on "
                f"relay-type controllers not yet supported")
        d = self._encode_d1_body(card_number, pin_code, ts_code,
                                  rights_data, rights_mask)
        return self._send_frame("D1", d)

    # ── Time schedules (D3 Write Time Schedules) ─────────────────────
    @staticmethod
    def _encode_ts_data(ts_number: int, week) -> str:
        """Build the D3 ts_data blob, ported 1:1 from the AGPL reference
        (hr_rfid_ctrl_time_schedule save_ts + get_set_str):

            ts_data = '%02X' % ts_number + 8 days × 4 intervals × 8 chars

        `week`: list of 8 days (0=Mon … 6=Sun, 7=Holiday); each day a list
        of up to 4 (begin, end) float-hour pairs. Missing intervals are
        padded with 00:00–00:00. begin/end encode as '%02d%02d' (HH, MM):
        9.5 → "0930". Total length = 2 + 8*4*8 = 258 chars.
        """
        def _hhmm(f):
            f = max(0.0, float(f or 0.0))
            return "%02d%02d" % (int(f), int(round((f - int(f)) * 60)))

        out = ["%02X" % (int(ts_number) & 0xFF)]
        week = list(week or [])
        for day in range(8):
            intervals = list(week[day]) if day < len(week) and week[day] \
                else []
            for n in range(4):
                if n < len(intervals):
                    begin, end = intervals[n]
                    out.append(_hhmm(begin) + _hhmm(end))
                else:
                    out.append("00000000")
        return "".join(out)

    def write_time_schedule(self, ts_number: int, week) -> dict:
        """Write a Time-Schedule slot into the controller (D3). `week` is
        the semantic 8-day interval spec (see _encode_ts_data); the driver
        builds the wire blob. The card then references `ts_number` in its
        ts_code so it enforces the window standalone/offline."""
        d = self._encode_ts_data(ts_number, week)
        return self._send_frame("D3", d)

    def read_time_schedule(self, ts_number: int) -> dict:
        """Read a Time-Schedule slot (F3) — for verification/diffing."""
        return self._send_frame("F3", "%02X" % (int(ts_number) & 0xFF))

    def remove_card(self, card_number: str, rights_mask: int = 1,
                    pin_code: str = "0000") -> dict:
        """Delete a card from the controller's local memory. A D1 with
        rights_data=0 + rights_mask set clears the card's rights (the
        reference toggles bits off via the mask; mask with data 0 revokes).
        """
        if self.relay_ctrl:
            raise RuntimeError(
                f"Polimex {self.controller_id!r}: card programming on "
                f"relay-type controllers not yet supported")
        d = self._encode_d1_body(card_number, pin_code, "00000000",
                                  rights_data=0, rights_mask=rights_mask)
        return self._send_frame("D1", d)

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
