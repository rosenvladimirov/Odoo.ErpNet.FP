"""
Hikvision access controller / door terminal — ISAPI RemoteControlDoor.

Covers Hikvision DS-K2600/K2700 access-controller panels, DS-K1T/K1A
face/card door terminals, and DS-KD video-intercom door stations —
they all expose the SAME synchronous ISAPI call:

    PUT http://<host>/ISAPI/AccessControl/RemoteControl/door/<doorNo>
    Authorization: Digest <device admin user/pass>
    Content-Type: application/xml

    <RemoteControlDoor version="2.0"
        xmlns="http://www.isapi.org/ver20/XMLSchema">
      <cmd>open</cmd>
    </RemoteControlDoor>

`cmd` ∈ {open (momentary — device-side open-duration auto-relocks),
close (locked), alwaysOpen (latched free), alwaysClose (disabled)}.
One request → relay fires → 200 + ResponseStatus. No SDK, no paid
licence on LAN. ISAPI is plain HTTP/REST (clean-room from the public
schema — not a code copy).

⚠ Caveat (surface to operators): a 200 means the command was
ACCEPTED, not that the relay physically energised — if the device's
door-mode is `alwaysClose`/disabled or the relay/open-duration is
misconfigured on the terminal, the call still returns OK. That is a
device-config issue, not an API failure.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from .common import AccessActuator, AccessResult

_logger = logging.getLogger(__name__)

_XML = ('<RemoteControlDoor version="2.0" '
        'xmlns="http://www.isapi.org/ver20/XMLSchema">'
        '<cmd>%s</cmd></RemoteControlDoor>')


class HikvisionIsapiActuator(AccessActuator):
    def __init__(
        self,
        controller_id: str,
        host: str,
        port: int = 80,
        user: str = "admin",
        password: str = "",
        door_no: int = 1,
        timeout: float = 4.0,
        fail_secure: bool = True,
    ) -> None:
        super().__init__(controller_id, fail_secure=fail_secure)
        self.host = host
        self.port = int(port)
        self.user = user or "admin"
        self.password = password or ""
        self.door_no = int(door_no)
        self.timeout = float(timeout)
        self._cli: Optional[httpx.Client] = None

    def connect(self) -> None:
        if not self.host:
            raise ValueError(
                f"Access {self.controller_id!r}: hikvision needs host"
            )
        if self._cli is None:
            # Recent FW мандатира HTTP Digest; httpx ползва Digest и
            # пада обратно на Basic ако сървърът го поиска.
            self._cli = httpx.Client(
                timeout=self.timeout,
                auth=httpx.DigestAuth(self.user, self.password),
            )

    def disconnect(self) -> None:
        if self._cli is not None:
            try:
                self._cli.close()
            finally:
                self._cli = None

    def _base(self) -> str:
        if self.port and self.port != 80:
            return f"http://{self.host}:{self.port}"
        return f"http://{self.host}"

    def _cmd(self, value: str) -> dict:
        self.connect()
        url = (f"{self._base()}/ISAPI/AccessControl/RemoteControl/"
               f"door/{self.door_no}")
        resp = self._cli.put(
            url, content=(_XML % value).encode("utf-8"),
            headers={"Content-Type": "application/xml"},
        )
        resp.raise_for_status()
        return {"http": resp.status_code, "cmd": value}

    def open(self, pulse_seconds: Optional[float] = None) -> AccessResult:
        # `open` = momentary; relock duration е device-side config →
        # pulse_seconds е само информативен (single round-trip).
        self._cmd("open")
        return AccessResult(self.controller_id, "open", True, "open",
                            "ISAPI cmd accepted (device-side relock)")

    def deny(self) -> AccessResult:
        # `close` е реална команда (по-добре от polimex re-send).
        self._cmd("close")
        return AccessResult(self.controller_id, "deny", True, "closed")

    def status(self) -> AccessResult:
        self.connect()
        try:
            url = (f"{self._base()}/ISAPI/AccessControl/RemoteControl/"
                   f"door/capabilities")
            r = self._cli.get(url)
            ok = r.status_code < 400
            return AccessResult(self.controller_id, "status", ok,
                                "unknown",
                                f"capabilities HTTP {r.status_code}")
        except Exception as exc:  # noqa: BLE001
            return AccessResult(self.controller_id, "status", False,
                                "unknown", str(exc))
