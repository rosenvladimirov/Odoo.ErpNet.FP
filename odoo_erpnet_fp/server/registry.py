"""
Fleet registry client — auto-enrol + heartbeat.

The proxy enrols itself with a central Odoo control plane (default
`https://iot.mcpworks.net`) so administrators can monitor the fleet,
trigger remote `/admin/self-update`, read `/admin/logs`, and program
VAT rates from a single backend.

Default flow (zero-touch):
    1. Operator sets `server.registry.enabled: true` in config.yaml
       and optionally a stable `name`.
    2. On startup the proxy POSTs
       `/erp_net_fp/registry/auto-enrol` with
       `{name, host, version, admin_token, public_url}`. The
       admin_token (auto-bootstrapped on first run) is the
       proof-of-possession.
    3. The Odoo controller creates a new `erpnet.fp.proxy` record (or
       refreshes one with matching name+admin_token), generates a
       long-lived secret, returns it. The proxy writes the secret to
       `/app/data/registry_secret` (config.yaml is usually mounted
       read-only).
    4. Every `interval_seconds`, the proxy POSTs
       `/erp_net_fp/registry/heartbeat` with body HMAC-signed using
       the secret.

Manual pairing flow (for stricter setups): set
`server.registry.pairing_token` to a one-time token issued from the
Fleet UI; auto-enrol is then skipped.

Disabled by default — `registry.enabled` must be true.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
from typing import Optional

import httpx
import yaml

from ..config.loader import AppConfig, RegistryConfig

_logger = logging.getLogger(__name__)

# Lower bound — Odoo `last_seen` window is 3× this; setting it lower
# than 30 s makes the dashboard flap on transient network blips.
_MIN_INTERVAL_SECONDS = 30

# Persistent location for the long-lived secret. Lives in /app/data
# (the same volume that holds /app/data/admin_token) so secrets
# survive container recreation even when /app/config is mounted
# read-only.
_SECRET_FILE = os.environ.get(
    "ERPNET_REGISTRY_SECRET_FILE", "/app/data/registry_secret")


def _admin_token_value() -> str:
    """Read the proxy's admin token using the same resolution order
    as routes.admin._admin_token() — env var, then file. Imported
    lazily to avoid a circular import."""
    try:
        from .routes.admin import _admin_token  # type: ignore
        return _admin_token() or ""
    except Exception:  # noqa: BLE001
        return ""


def _device_summary(app) -> dict[str, list[str]]:
    """Inventory the currently-loaded devices for the heartbeat body."""
    out: dict[str, list[str]] = {}
    for attr, key in (
        ("registry", "printers"),
        ("pinpad_registry", "pinpads"),
        ("scale_registry", "scales"),
        ("reader_registry", "readers"),
        ("display_registry", "displays"),
    ):
        try:
            reg = getattr(app.state, attr, None)
            inner = getattr(reg, key, None) if reg else None
            out[key] = sorted(list(inner.keys())) if inner else []
        except Exception:  # noqa: BLE001
            out[key] = []
    return out


def _sign_body(body: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _load_persistent_secret() -> str:
    """Read the long-lived registry secret from /app/data/registry_secret.
    Returns '' if the file is missing or unreadable.
    """
    try:
        with open(_SECRET_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _persist_secret(secret: str) -> bool:
    """Write the secret to /app/data/registry_secret with mode 600.
    Returns True on success.
    """
    try:
        os.makedirs(os.path.dirname(_SECRET_FILE), exist_ok=True)
        tmp = _SECRET_FILE + ".tmp"
        fd = os.open(tmp,
                     os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secret + "\n")
        os.replace(tmp, _SECRET_FILE)
        os.chmod(_SECRET_FILE, 0o600)
        return True
    except OSError as exc:
        _logger.warning(
            "Could not persist registry secret to %s: %s — auto-enrol "
            "will repeat on next restart.", _SECRET_FILE, exc)
        return False


async def _auto_enrol(client: httpx.AsyncClient, cfg: RegistryConfig,
                      name: str, host: str, version: str,
                      admin_token: str, public_url: str) -> Optional[str]:
    """POST /auto-enrol — returns the long-lived secret or None."""
    body = {
        "name": name,
        "host": host,
        "version": version,
        "admin_token": admin_token,
        "public_url": public_url,
    }
    try:
        r = await client.post(
            f"{cfg.url}/erp_net_fp/registry/auto-enrol",
            json=body, timeout=30.0,
        )
    except httpx.HTTPError as exc:
        _logger.warning("Auto-enrol request failed: %s", exc)
        return None
    if r.status_code != 200:
        _logger.warning("Auto-enrol rejected (%s): %s",
                        r.status_code, r.text[:200])
        return None
    try:
        data = r.json()
        if isinstance(data, dict) and "result" in data:
            data = data["result"]
        secret = data.get("secret") if isinstance(data, dict) else None
    except (ValueError, AttributeError):
        secret = None
    if not secret:
        _logger.warning("Auto-enrol returned no secret: %s", r.text[:200])
        return None
    return str(secret)


async def _pair(client: httpx.AsyncClient, cfg: RegistryConfig,
                host: str, version: str) -> Optional[str]:
    """POST /pair — exchange one-time pairing_token for a secret."""
    body = {
        "pairing_token": cfg.pairing_token,
        "host": host,
        "version": version,
    }
    try:
        r = await client.post(
            f"{cfg.url}/erp_net_fp/registry/pair",
            json=body, timeout=30.0,
        )
    except httpx.HTTPError as exc:
        _logger.warning("Fleet pair request failed: %s", exc)
        return None
    if r.status_code != 200:
        _logger.warning("Fleet pair rejected (%s): %s",
                        r.status_code, r.text[:200])
        return None
    try:
        data = r.json()
        if isinstance(data, dict) and "result" in data:
            data = data["result"]
        secret = data.get("secret") if isinstance(data, dict) else None
    except (ValueError, AttributeError):
        secret = None
    return str(secret) if secret else None


async def _heartbeat(client: httpx.AsyncClient, cfg: RegistryConfig,
                     app, name: str, host: str, version: str,
                     public_url: str) -> bool:
    body = {
        "name": name,
        "host": host,
        "version": version,
        "admin_token": _admin_token_value(),
        "public_url": public_url,
        "devices": _device_summary(app),
    }
    raw = json.dumps(body, separators=(",", ":"),
                     sort_keys=True).encode("utf-8")
    sig = _sign_body(raw, cfg.secret)
    try:
        r = await client.post(
            f"{cfg.url}/erp_net_fp/registry/heartbeat",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Registry-Signature": sig,
                "X-Registry-Host": host,
                "X-Registry-Name": name,
            },
            timeout=15.0,
        )
    except httpx.HTTPError as exc:
        _logger.debug("Heartbeat failed: %s", exc)
        return False
    if r.status_code != 200:
        _logger.warning("Heartbeat rejected (%s): %s",
                        r.status_code, r.text[:200])
        return False
    return True


async def fleet_loop(app, config_path: Path) -> None:
    """Background task — auto-enrols if needed, then heartbeats forever."""
    cfg: RegistryConfig = app.state.config.server.registry
    if not cfg.enabled:
        _logger.info("Fleet registry disabled (server.registry.enabled=false)")
        return
    if not cfg.url:
        _logger.warning("server.registry.url is empty — fleet loop disabled")
        return

    import socket
    container_host = (os.environ.get("HOSTNAME") or socket.gethostname()
                      or "unknown").strip()
    name = cfg.name or container_host  # stable identifier used by auto-enrol
    public_url = cfg.public_url or ""

    try:
        from importlib.metadata import version as _v
        version = _v("odoo-erpnet-fp")
    except Exception:  # noqa: BLE001
        version = "dev"

    interval = max(_MIN_INTERVAL_SECONDS, int(cfg.interval_seconds or 60))

    async with httpx.AsyncClient() as client:
        # ─── Resolve secret ─────────────────────────────────────
        # 1. Persistent file (cross-restart) takes priority
        if not cfg.secret:
            cfg.secret = _load_persistent_secret()
            if cfg.secret:
                _logger.info("Loaded registry secret from %s", _SECRET_FILE)

        # 2. Manual pairing flow (legacy, for stricter setups)
        if not cfg.secret and cfg.pairing_token:
            _logger.info("Fleet pairing → %s (manual token flow)", cfg.url)
            new_secret = await _pair(client, cfg, container_host, version)
            if new_secret:
                cfg.secret = new_secret
                _persist_secret(new_secret)
            else:
                _logger.warning(
                    "Fleet pairing failed — heartbeat loop will not start.")
                return

        # 3. Auto-enrol via admin_token (default zero-touch flow)
        if not cfg.secret:
            admin_token = _admin_token_value()
            if not admin_token:
                _logger.warning(
                    "Auto-enrol skipped — admin_token unavailable. "
                    "Wait for the proxy to bootstrap one (banner in logs) "
                    "and restart, OR set ERPNET_ADMIN_TOKEN explicitly.")
                return
            _logger.info("Fleet auto-enrol → %s as %r", cfg.url, name)
            new_secret = await _auto_enrol(
                client, cfg, name, container_host, version,
                admin_token, public_url,
            )
            if new_secret:
                cfg.secret = new_secret
                _persist_secret(new_secret)
            else:
                _logger.warning(
                    "Auto-enrol failed — heartbeat loop will not start. "
                    "Check Fleet UI for an existing record with name %r "
                    "and an admin_token mismatch (409). Reset Secret on "
                    "that record to allow re-enrolment.", name)
                return

        if not cfg.secret:
            return

        _logger.info("Fleet heartbeat loop started → %s as %r (every %ds)",
                     cfg.url, name, interval)
        while True:
            try:
                await _heartbeat(client, cfg, app, name, container_host,
                                  version, public_url)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _logger.exception("Heartbeat tick raised unexpectedly")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                _logger.info("Fleet heartbeat loop cancelled — exiting")
                raise
