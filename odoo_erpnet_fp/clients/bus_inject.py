# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.

"""Bus-inject client — POST /erpnet_fp/bus/inject on the Fleet receiver.

Emits a live event envelope to the Odoo addon `l10n_bg_erp_net_fp_bus_inject`,
which validates HMAC, stamps server-side `ts`+`id`, and broadcasts on
`bus.bus` channel `erpnet_fp_proxy_events`. Frontend listens via the
`l10n_bg_live_refresh` hub (event `PROXY_EVENT`).

Schema (the contract the Fleet side validates):

    {
      "v": 1,
      "type": "plate.detected",
      "source": {
        "proxy": "<our name>",
        "device": "<device-id in our config>",
        "device_kind": "camera|access|reader|mqtt|biometric|controller"
      },
      "data": {...}
    }

Auth: HMAC-SHA256 of body keyed on `registry_secret` (same scheme as
the Fleet heartbeat — no separate token to manage). Two headers:

    X-Bus-Inject-Signature: <hex>
    X-Bus-Inject-Proxy:     <proxy name>

Failure is non-fatal — bus_inject is a live signal. If Odoo is
unreachable, we log a warning and move on; the proxy's primary
function (open the door, print the receipt) is unaffected.

Reference doc lives in the Odoo repo at
`l10n_bg_erp_net_fp_bus_inject/docs/proxy_push_schema.md`.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

_logger = logging.getLogger(__name__)


def _iso_utc_ms() -> str:
    """ISO-8601 UTC with millisecond precision + trailing Z."""
    return (datetime.now(timezone.utc)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"))


def _sign(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


class BusInjectClient:
    """Thin synchronous client. Thread-safe by virtue of the underlying
    httpx.Client being thread-safe and no shared mutable state besides
    config snapshot.

    Usage from any thread (driver callbacks, async tasks):

        client = BusInjectClient.from_app(app)
        client.emit("plate.detected",
                    device="camera.front", device_kind="camera",
                    data={"plate": "СК1234БГ", "confidence": 0.94})

    Use `from_app(app)` to grab the live config; the client snapshots
    fleet URL + secret + proxy name once per construction. Recreate on
    config reload (the Fleet companion already restarts the fleet loop
    on rescue/grab, so reaching for `from_app` again is cheap).
    """

    def __init__(self, registry_url: str, registry_secret: str,
                 proxy_name: str, timeout: float = 5.0) -> None:
        self.url = (registry_url or "").rstrip("/") + "/erpnet_fp/bus/inject"
        self.secret = registry_secret or ""
        self.proxy_name = proxy_name or ""
        self.timeout = timeout
        self._client = httpx.Client(timeout=timeout)
        self._lock = threading.Lock()  # only for stat counters
        self.emits_ok = 0
        self.emits_failed = 0

    @classmethod
    def from_app(cls, app) -> Optional["BusInjectClient"]:
        """Construct from FastAPI app state. Returns None if the
        prerequisites aren't satisfied (no registry URL, no secret,
        no name) — caller should treat as "bus inject disabled here".
        """
        cfg = getattr(app.state, "config", None)
        if cfg is None:
            return None
        reg = cfg.server.registry
        if not reg.enabled or not reg.url or not reg.name:
            return None
        # Lazy-load the persistent secret without importing
        # server.registry (which would cause a circular import). Read
        # the same file path the registry module uses.
        import os
        secret_path = os.environ.get(
            "ERPNET_REGISTRY_SECRET_FILE", "/app/data/registry_secret")
        secret = ""
        try:
            with open(secret_path) as f:
                secret = f.read().strip()
        except OSError:
            pass
        if not secret:
            return None
        return cls(
            registry_url=reg.url,
            registry_secret=secret,
            proxy_name=reg.name,
        )

    def emit(self, event_type: str,
             device: str = "",
             device_kind: str = "",
             data: Optional[dict] = None) -> Optional[dict]:
        """Send one event envelope. Returns the envelope on success
        (with server-side `ts`/`id` if Odoo populates them in response;
        otherwise our local stamps), or None on failure.
        """
        if not self.secret or not self.proxy_name or not self.url:
            return None
        envelope = {
            "v": 1,
            "type": event_type,
            "source": {
                "proxy": self.proxy_name,
                "device": device,
                "device_kind": device_kind,
            },
            "ts": _iso_utc_ms(),
            "id": str(uuid.uuid4()),
            "data": data or {},
        }
        body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
        sig = _sign(body, self.secret)
        try:
            r = self._client.post(self.url, content=body, headers={
                "Content-Type": "application/json",
                "X-Bus-Inject-Signature": sig,
                "X-Bus-Inject-Proxy": self.proxy_name,
            })
            if r.status_code != 200:
                _logger.warning(
                    "bus_inject %s → HTTP %d: %s",
                    event_type, r.status_code, r.text[:200])
                with self._lock:
                    self.emits_failed += 1
                return None
            with self._lock:
                self.emits_ok += 1
            return envelope
        except Exception as exc:  # noqa: BLE001
            _logger.warning("bus_inject %s emit failed: %s", event_type, exc)
            with self._lock:
                self.emits_failed += 1
            return None

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:  # noqa: BLE001
            pass
