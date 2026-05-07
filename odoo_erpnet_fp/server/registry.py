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


class HeartbeatResult:
    OK = "ok"
    TRANSIENT = "transient"  # network glitch, retry next tick
    REENROL = "reenrol"      # 410 — server forgot us, drop secret + auto-enrol
    BANNED = "banned"        # 403 — proxy archived, give up


async def _execute_command(cmd: dict) -> tuple[bool, dict | None, str]:
    """Run a queued command locally on this proxy.

    Returns (ok, result_dict, error_string). All commands are short
    HTTP calls against our own /admin/* or /printers/* endpoints,
    authenticated with our own admin_token. We use httpx for this so
    we don't depend on the FastAPI app instance — the loop runs out-
    of-band.
    """
    kind = (cmd.get("kind") or "").strip()
    payload = cmd.get("payload") or {}
    token = _admin_token_value()
    base = "http://127.0.0.1:8001"
    headers = {"X-Admin-Token": token}
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            if kind == "self_update":
                r = await c.post(f"{base}/admin/self-update",
                                  headers=headers)
            elif kind == "get_logs":
                tail = int(payload.get("tail", 200))
                r = await c.get(
                    f"{base}/admin/logs",
                    headers=headers, params={"tail": tail},
                )
            elif kind == "program_vat":
                printer_id = (payload.get("printer_id") or "").strip()
                if not printer_id:
                    return False, None, "printer_id required"
                r = await c.post(
                    f"{base}/printers/{printer_id}/vat-rates",
                    headers=headers,
                    json={"rates": payload.get("rates") or {}},
                )
            else:
                return False, None, f"Unknown command kind: {kind!r}"
            ok = r.status_code < 400
            try:
                data = r.json()
            except ValueError:
                data = {"raw": r.text[:2000]}
            err = ""
            if not ok:
                err = f"HTTP {r.status_code}: {r.text[:500]}"
            return ok, data, err
    except Exception as exc:  # noqa: BLE001
        return False, None, f"{type(exc).__name__}: {exc}"


async def _post_command_result(client: httpx.AsyncClient,
                                cfg: RegistryConfig,
                                command_id: int, ok: bool,
                                result: dict | None, error: str) -> None:
    body = {
        "command_id": command_id,
        "ok": ok,
        "result": result,
        "error": error,
    }
    raw = json.dumps(body, separators=(",", ":"),
                     sort_keys=True).encode("utf-8")
    sig = _sign_body(raw, cfg.secret)
    try:
        r = await client.post(
            f"{cfg.url}/erp_net_fp/registry/command-result",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Registry-Signature": sig,
            },
            timeout=15.0,
        )
        if r.status_code != 200:
            _logger.warning("Command-result POST rejected (%s): %s",
                            r.status_code, r.text[:200])
    except httpx.HTTPError as exc:
        _logger.warning("Command-result POST failed: %s", exc)


async def _heartbeat(client: httpx.AsyncClient, cfg: RegistryConfig,
                     app, name: str, host: str, version: str,
                     public_url: str) -> tuple[str, list[dict]]:
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
        return HeartbeatResult.TRANSIENT, []
    if r.status_code == 200:
        try:
            data = r.json()
        except ValueError:
            data = {}
        commands = data.get("commands") if isinstance(data, dict) else []
        return HeartbeatResult.OK, list(commands or [])
    if r.status_code == 410:
        _logger.info("Heartbeat → 410 Gone: server forgot us, will re-enrol")
        return HeartbeatResult.REENROL, []
    if r.status_code == 403:
        _logger.warning("Heartbeat → 403 Forbidden: proxy is archived "
                        "in fleet UI; will not retry until restart "
                        "after Unarchive.")
        return HeartbeatResult.BANNED, []
    _logger.warning("Heartbeat rejected (%s): %s",
                    r.status_code, r.text[:200])
    return HeartbeatResult.TRANSIENT, []


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
        # Adaptive cadence: when commands flow we want sub-second
        # responsiveness so the UI shows results in real time. After
        # execution we drain the queue immediately, and stay in
        # "fast mode" (polling every 5 s) for `_FAST_WINDOW` seconds
        # of inactivity before relaxing back to the configured interval.
        import time as _t
        _FAST_WINDOW = 60   # seconds of fast polling after activity
        _FAST_TICK = 5      # poll cadence during fast window
        fast_until = 0.0
        while True:
            commands: list[dict] = []
            try:
                result, commands = await _heartbeat(
                    client, cfg, app, name, container_host, version,
                    public_url)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                _logger.exception("Heartbeat tick raised unexpectedly")
                result = HeartbeatResult.TRANSIENT

            # Execute any commands the server queued for us. Done in
            # series — keeps logs readable and lets one bad command
            # not block the next.
            for cmd in commands or []:
                cid = cmd.get("id")
                _logger.info("Executing queued command: id=%s kind=%r",
                             cid, cmd.get("kind"))
                try:
                    ok, payload, err = await _execute_command(cmd)
                except Exception as exc:  # noqa: BLE001
                    ok, payload, err = False, None, f"{type(exc).__name__}: {exc}"
                if cid:
                    await _post_command_result(client, cfg, cid, ok,
                                                payload, err)
                # Any activity → enter fast mode so the UI sees
                # follow-up status promptly.
                fast_until = _t.monotonic() + _FAST_WINDOW

            if result == HeartbeatResult.REENROL:
                # Drop our local secret and auto-enrol again on this tick.
                cfg.secret = ""
                try:
                    os.unlink(_SECRET_FILE)
                except OSError:
                    pass
                admin_token = _admin_token_value()
                if admin_token:
                    new_secret = await _auto_enrol(
                        client, cfg, name, container_host, version,
                        admin_token, public_url,
                    )
                    if new_secret:
                        cfg.secret = new_secret
                        _persist_secret(new_secret)
                        _logger.info("Re-enrolled successfully")
                    else:
                        _logger.warning("Re-enrol failed; will retry next tick")
            elif result == HeartbeatResult.BANNED:
                # Proxy is archived in the fleet UI; sleep MUCH longer
                # to avoid log spam, but don't exit (operator may
                # un-archive, in which case we resume normally).
                try:
                    await asyncio.sleep(15 * 60)
                except asyncio.CancelledError:
                    raise
                continue

            # If we just executed commands, drain immediately —
            # there might be more queued behind them. Otherwise stay
            # in fast mode for `_FAST_WINDOW` after the last activity
            # so admins see real-time feedback in the Fleet UI.
            if commands:
                tick_sleep = 0.5  # tiny gap so we don't hammer
            elif _t.monotonic() < fast_until:
                tick_sleep = _FAST_TICK
            else:
                tick_sleep = interval
            try:
                await asyncio.sleep(tick_sleep)
            except asyncio.CancelledError:
                _logger.info("Fleet heartbeat loop cancelled — exiting")
                raise
