"""
Dahua access controller / terminal / VTO — accessControl.cgi.

Covers Dahua ASC controller panels (CGI-capable FW), ASI face/card
terminals and VTO video-intercom door stations — the same
synchronous HTTP-CGI call:

    GET http://<host>/cgi-bin/accessControl.cgi
        ?action=openDoor&channel=<N>&Type=Remote[&UserID=<id>]
    Authorization: Digest <device user/pass>

One GET → relay fires → body literally `OK`. No SDK for VTO/ASI/
new-FW ASC. (Clean-room from the public Dahua HTTP API spec, not a
code copy.)

⚠ Caveats (surface to operators):
- Legacy first-gen ASC panels without CGI are **SDK-only** (Dahua
  NetSDK binary) — explicitly UNSUPPORTED by this thin proxy. The
  driver probes the CGI on the first command and fails clearly
  instead of shipping the SDK.
- A `200/OK` means the command was ACCEPTED, not that the relay
  physically energised (device door-mode / lock config can swallow
  it) — a device-config issue, not an API failure.
- Modern FW mandates HTTP Digest; very old FW also accepts Basic.
"""

from __future__ import annotations

import logging
from typing import Optional

import httpx

from .common import AccessActuator, AccessResult

_logger = logging.getLogger(__name__)


class DahuaCgiActuator(AccessActuator):
    def __init__(
        self,
        controller_id: str,
        host: str,
        port: int = 80,
        user: str = "admin",
        password: str = "",
        channel: int = 1,
        user_id: str = "",
        timeout: float = 4.0,
        fail_secure: bool = True,
    ) -> None:
        super().__init__(controller_id, fail_secure=fail_secure)
        self.host = host
        self.port = int(port)
        self.user = user or "admin"
        self.password = password or ""
        self.channel = int(channel)
        self.user_id = user_id
        self.timeout = float(timeout)
        self._cli: Optional[httpx.Client] = None

    def connect(self) -> None:
        if not self.host:
            raise ValueError(
                f"Access {self.controller_id!r}: dahua needs host"
            )
        if self._cli is None:
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

    def _cgi(self, action: str) -> str:
        self.connect()
        params = {"action": action, "channel": self.channel,
                  "Type": "Remote"}
        if self.user_id:
            params["UserID"] = self.user_id
        r = self._cli.get(
            f"{self._base()}/cgi-bin/accessControl.cgi", params=params
        )
        if r.status_code == 404:
            # Няма CGI → legacy SDK-only ASC. Не доставяме NetSDK.
            raise RuntimeError(
                f"Dahua {self.controller_id!r}: accessControl.cgi not "
                f"found (404) — legacy SDK-only panel, unsupported by "
                f"the thin proxy. Use a CGI-capable FW / VTO / ASI."
            )
        r.raise_for_status()
        return (r.text or "").strip()

    def open(self, pulse_seconds: Optional[float] = None) -> AccessResult:
        body = self._cgi("openDoor")
        ok = body.upper().startswith("OK") or body == ""
        return AccessResult(
            self.controller_id, "open", ok,
            "open" if ok else "unknown",
            f"CGI openDoor → {body!r} (device-side relock)",
        )

    def deny(self) -> AccessResult:
        # closeDoor се поддържа на по-новите FW; ако не — best-effort.
        try:
            body = self._cgi("closeDoor")
            return AccessResult(self.controller_id, "deny", True,
                                "closed", f"CGI closeDoor → {body!r}")
        except Exception as exc:  # noqa: BLE001
            return AccessResult(self.controller_id, "deny", True,
                                "unknown",
                                f"closeDoor unsupported ({exc})")

    def status(self) -> AccessResult:
        try:
            body = self._cgi("getDoorStatus")
            return AccessResult(self.controller_id, "status", True,
                                "unknown", body[:120])
        except Exception as exc:  # noqa: BLE001
            return AccessResult(self.controller_id, "status", False,
                                "unknown", str(exc))
