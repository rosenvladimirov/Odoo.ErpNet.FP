# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.

"""Emergency rescue endpoint — master-password proxy takeover.

When a proxy has lost its admin_token / registry_secret (operator
turnover, disk failure, fleet URL changed without prior planning),
the standard re-pair flow needs interactive access to the proxy host.
That's not always possible — the box may be at a remote customer
site behind NAT, no SSH, no console.

Solution: at image-build time the operator burns a **master rescue
token** into the proxy via `ERPNET_FP_RESCUE_TOKEN` env (or a file at
`/app/data/rescue_token`). With that token and the proxy's public
URL, ANY operator can:

  POST /admin/rescue/grab
    X-Rescue-Token: <the master token>
    {
      "new_fleet_url": "https://newfleet.example.com",
      "new_name": "sofia-shop-1",          // optional
      "clear_secret": true,                 // default — forces re-pair
      "pairing_token": "abc..."             // optional — for stricter pair
    }

The handler mutates `app.state.config.server.registry` in-memory + drops
the on-disk `/app/data/registry_secret` so the next heartbeat falls
through to auto-enrol against the new Fleet. The fiscal/POS surface is
not touched.

Security model:
  * The rescue token is the trust anchor. Treat it like an SSH master
    key — store it in a password manager, never commit it.
  * `secrets.compare_digest` guards against timing attacks.
  * If `ERPNET_FP_RESCUE_TOKEN` is unset (and no file at
    `/app/data/rescue_token`), the endpoint returns 503 — no grab is
    possible. Operators who don't want this capability simply don't
    set the token.
  * Every successful grab logs `name`, source IP, new URL — show up
    in `docker logs` for audit.
"""

from __future__ import annotations

import logging
import os
import secrets
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request, status
from pydantic import BaseModel, Field

_logger = logging.getLogger(__name__)

router = APIRouter(prefix="/admin/rescue", tags=["admin"])


# ─── Token resolution ────────────────────────────────────────────────

_RESCUE_TOKEN_FILE = os.environ.get(
    "ERPNET_FP_RESCUE_TOKEN_FILE", "/app/data/rescue_token")

_REGISTRY_SECRET_FILE = os.environ.get(
    "ERPNET_REGISTRY_SECRET_FILE", "/app/data/registry_secret")


def _rescue_token() -> str:
    """Return the master rescue token, or empty if none is configured.

    Resolution: env var ERPNET_FP_RESCUE_TOKEN > file at
    ERPNET_FP_RESCUE_TOKEN_FILE (default /app/data/rescue_token).
    """
    env = (os.environ.get("ERPNET_FP_RESCUE_TOKEN") or "").strip()
    if env:
        return env
    try:
        with open(_RESCUE_TOKEN_FILE) as f:
            return f.read().strip()
    except OSError:
        return ""


def _check_rescue_token(provided: Optional[str]) -> None:
    expected = _rescue_token()
    if not expected:
        # 503 vs 401 — operator wanted to disable rescue entirely.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Rescue endpoint is disabled. Set "
                   "ERPNET_FP_RESCUE_TOKEN to enable.",
        )
    if not provided or not secrets.compare_digest(provided, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid rescue token",
        )


# ─── Payload ─────────────────────────────────────────────────────────


class GrabBody(BaseModel):
    new_fleet_url: Optional[str] = Field(
        None, description="New Fleet registry URL the proxy should "
                          "heartbeat to. Leave empty to keep current.")
    new_name: Optional[str] = Field(
        None, description="New stable name (overrides config.yaml). "
                          "Leave empty to keep current.")
    clear_secret: bool = Field(
        True, description="Drop the on-disk registry secret so the proxy "
                          "re-enrols on next heartbeat. Default True.")
    pairing_token: Optional[str] = Field(
        None, description="Optional manual pairing token — used by "
                          "stricter setups instead of auto-enrol.")


# ─── Routes ──────────────────────────────────────────────────────────


@router.get("/status")
async def rescue_status(
    request: Request,
    x_rescue_token: Optional[str] = Header(None, alias="X-Rescue-Token"),
) -> dict:
    """Probe whether the rescue token is configured. Returns
    `{enabled: bool}` for unauthenticated callers (no leakage).
    With a valid token, also reports current registry url + name
    so the operator can confirm they grabbed the right box.
    """
    expected = _rescue_token()
    enabled = bool(expected)
    matched = bool(
        enabled and x_rescue_token
        and secrets.compare_digest(x_rescue_token, expected))
    if not matched:
        return {"enabled": enabled}
    cfg = request.app.state.config
    return {
        "enabled": True,
        "matched": True,
        "current": {
            "url": cfg.server.registry.url,
            "name": cfg.server.registry.name,
            "enabled": cfg.server.registry.enabled,
        },
    }


@router.post("/grab")
async def rescue_grab(
    request: Request,
    body: GrabBody,
    x_rescue_token: Optional[str] = Header(None, alias="X-Rescue-Token"),
) -> dict:
    _check_rescue_token(x_rescue_token)

    cfg = request.app.state.config
    src_ip = request.client.host if request.client else "?"
    old_url = cfg.server.registry.url
    old_name = cfg.server.registry.name

    changes: list[str] = []
    if body.new_fleet_url:
        cfg.server.registry.url = body.new_fleet_url.rstrip("/")
        changes.append(f"url: {old_url!r} → {cfg.server.registry.url!r}")
    if body.new_name:
        cfg.server.registry.name = body.new_name
        changes.append(f"name: {old_name!r} → {cfg.server.registry.name!r}")
    if body.pairing_token:
        cfg.server.registry.pairing_token = body.pairing_token
        changes.append("pairing_token: set")
    if body.clear_secret:
        cfg.server.registry.secret = ""
        try:
            os.unlink(_REGISTRY_SECRET_FILE)
            changes.append(f"on-disk secret at {_REGISTRY_SECRET_FILE} removed")
        except FileNotFoundError:
            changes.append("on-disk secret was already absent")
        except OSError as e:
            _logger.warning("rescue/grab: failed to remove secret file: %s", e)
            changes.append(f"secret file removal failed: {e}")
    # Make sure the fleet loop continues running with the new config.
    cfg.server.registry.enabled = True

    _logger.warning(
        "RESCUE GRAB applied from %s — %s",
        src_ip, "; ".join(changes) or "(no-op)")

    return {
        "ok": True,
        "applied": changes,
        "current": {
            "url": cfg.server.registry.url,
            "name": cfg.server.registry.name,
            "enabled": cfg.server.registry.enabled,
        },
        "message": "Registry config overridden in-memory. Next heartbeat "
                   "(within interval_seconds) will use the new URL. "
                   "Auto-enrol will run automatically because the secret "
                   "was cleared.",
    }
