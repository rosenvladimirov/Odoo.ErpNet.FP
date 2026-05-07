"""
Fleet registry client — pair + heartbeat.

The proxy enrols itself with a central Odoo control plane (default
`https://iot.mcpworks.net`) so administrators can monitor the fleet,
trigger remote `/admin/self-update`, read `/admin/logs`, and program
VAT rates from a single backend.

Flow:
    1. Operator generates a one-time pairing token in Odoo
       (`erpnet.fp.proxy` form view → button "Generate pairing token")
       and pastes it into `server.registry.pairing_token` in
       `config.yaml`.
    2. On startup, if `pairing_token` is set and `secret` is empty,
       the proxy POSTs `/erp_net_fp/registry/pair` once. The Odoo
       endpoint validates the token (single-use, time-limited),
       returns a long-lived `secret`, and we persist it back to
       `config.yaml` (the pairing token is then cleared).
    3. Every `interval_seconds`, the proxy POSTs
       `/erp_net_fp/registry/heartbeat` with body HMAC-signed using
       `secret`. Odoo upserts the proxy record (last_seen, version,
       devices, encrypted admin_token).

The admin_token sent in the heartbeat lets the Odoo backend make
authenticated back-channel calls to `/admin/*` endpoints on the
proxy without requiring operators to copy tokens around manually.

Disabled by default — `registry.enabled` must be true in
`config.yaml` for the loop to start.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml

from ..config.loader import AppConfig, RegistryConfig

_logger = logging.getLogger(__name__)

# Lower bound — Odoo `last_seen` window is 3× this; setting it lower
# than 30 s makes the dashboard flap on transient network blips.
_MIN_INTERVAL_SECONDS = 30


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
    """Inventory the currently-loaded devices for the heartbeat body.

    Best-effort — registries may not exist yet on the very first
    heartbeat after startup. Empty lists are valid; Odoo treats them
    as 'proxy is up but has no devices configured'.
    """
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
    """HMAC-SHA256 hex digest of the raw request body — protects
    against tampering even though TLS already encrypts in flight."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _persist_secret(config_path: Path, secret: str) -> None:
    """Write `secret` back to the on-disk config.yaml, clearing the
    pairing token (single-use). Preserves all other config fields by
    re-loading the raw YAML, mutating just `server.registry.*`, and
    re-dumping. Comments are lost — acceptable for a generated config.
    """
    try:
        text = config_path.read_text(encoding="utf-8")
        data = yaml.safe_load(text) or {}
        srv = data.setdefault("server", {})
        reg = srv.setdefault("registry", {})
        reg["secret"] = secret
        reg["pairing_token"] = ""
        config_path.write_text(
            yaml.safe_dump(data, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        _logger.info("Fleet pairing successful — secret persisted to %s",
                     config_path)
    except Exception:  # noqa: BLE001
        _logger.exception(
            "Could not persist registry secret to %s — pairing succeeded "
            "but you must paste the secret manually into server.registry.secret "
            "before the next restart",
            config_path,
        )


async def _pair(client: httpx.AsyncClient, cfg: RegistryConfig,
                host: str, version: str) -> Optional[str]:
    """Exchange the one-time pairing token for a long-lived secret.

    Returns the new secret on success, None on failure. Caller persists
    it to config.yaml.
    """
    body = {
        "pairing_token": cfg.pairing_token,
        "host": host,
        "version": version,
    }
    try:
        r = await client.post(
            f"{cfg.url}/erp_net_fp/registry/pair",
            json=body,
            timeout=30.0,
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
        # Odoo wraps JSON-RPC responses; accept both shapes.
        if isinstance(data, dict) and "result" in data:
            data = data["result"]
        secret = data.get("secret") if isinstance(data, dict) else None
    except (ValueError, AttributeError):
        secret = None
    if not secret:
        _logger.warning(
            "Fleet pair returned no secret in body: %s", r.text[:200])
        return None
    return str(secret)


async def _heartbeat(client: httpx.AsyncClient, cfg: RegistryConfig,
                     app, host: str, version: str) -> bool:
    """Send a single heartbeat. Returns True if Odoo accepted it."""
    body = {
        "host": host,
        "version": version,
        "admin_token": _admin_token_value(),
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
    """Background task — pairs once if needed, then heartbeats forever.

    Cancellable via the standard asyncio task lifecycle; main.py wires
    it into the FastAPI lifespan.
    """
    cfg: RegistryConfig = app.state.config.server.registry
    if not cfg.enabled:
        _logger.info("Fleet registry disabled (server.registry.enabled=false) — "
                     "skipping heartbeat loop")
        return
    if not cfg.url:
        _logger.warning("server.registry.url is empty — fleet loop disabled")
        return

    # Resolve host identifier — prefer the container hostname (compose
    # sets it deterministically), fall back to socket.gethostname().
    import os
    import socket
    host = (os.environ.get("HOSTNAME") or socket.gethostname()
            or "unknown").strip()

    # Read our own version once — same source as `_read_version` in main.
    try:
        from importlib.metadata import version as _v
        version = _v("odoo-erpnet-fp")
    except Exception:  # noqa: BLE001
        version = "dev"

    interval = max(_MIN_INTERVAL_SECONDS, int(cfg.interval_seconds or 60))

    async with httpx.AsyncClient() as client:
        # ─── Pair (one-time) ────────────────────────────────────
        if not cfg.secret and cfg.pairing_token:
            _logger.info("Fleet pairing → %s", cfg.url)
            new_secret = await _pair(client, cfg, host, version)
            if new_secret:
                cfg.secret = new_secret
                cfg.pairing_token = ""
                _persist_secret(config_path, new_secret)
            else:
                _logger.warning(
                    "Fleet pairing failed — heartbeat loop will not start. "
                    "Verify pairing_token, regenerate it in Odoo if expired, "
                    "and restart the proxy.")
                return

        if not cfg.secret:
            _logger.warning(
                "server.registry.secret is empty and no pairing_token to "
                "exchange — heartbeat loop disabled. Generate a pairing "
                "token in Odoo and paste it into config.yaml.")
            return

        _logger.info("Fleet heartbeat loop started → %s (every %ds)",
                     cfg.url, interval)
        # ─── Heartbeat loop ─────────────────────────────────────
        while True:
            try:
                await _heartbeat(client, cfg, app, host, version)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _logger.exception("Heartbeat tick raised unexpectedly")
            try:
                await asyncio.sleep(interval)
            except asyncio.CancelledError:
                _logger.info("Fleet heartbeat loop cancelled — exiting")
                raise
