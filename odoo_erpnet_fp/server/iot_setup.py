"""
Outbound `/iot/setup` announcer — registers this proxy as an iot.box +
iot.device records on a remote Odoo instance.

Why outbound:

The official Odoo IoT Box image runs a `connect_to_server` script
that POSTs `/iot/setup` to the configured Odoo on boot and every
60 seconds. This module is the equivalent for ErpNet.FP — same wire
format, same cadence — so a stock Odoo EE iot module sees us as a
"normal" IoT box without any custom code on the Odoo side.

It also dodges the LAN-only DNS problem: when the proxy hostname
only resolves on the operator's laptop (browser-side Cloudflare
tunnel, /etc/hosts), the Odoo server can't reach the proxy with
`requests.get(...)` for a discovery scan. Going the other way (proxy
→ Odoo over plain HTTPS) is always routable.

Wire format (matches Odoo EE iot/controllers/main.py:71 update_box):

    POST <odoo_url>/iot/setup
    Content-Type: application/json
    Body: {
      "params": {
        "iot_box": {
          "identifier": "rosen-laptop-ddb6f47",
          "name": "ErpNet.FP proxy",
          "ip": "erpnet.lan.mcpworks.net",
          "version": "0.4.6",
          "token": "<from Odoo iot_token system param>"
        },
        "devices": {
          "printer.dp150":  {"name": "...", "type": "printer",
                             "manufacturer": "ErpNet.FP",
                             "connection": "network"},
          "reader.scanner1": ...,
          ...
        }
      }
    }

Token is checked only on first setup (record creation). After that,
subsequent announcements use the existing iot.box.identifier as the
match key — no token replay needed.
"""
from __future__ import annotations

import asyncio
import logging
import socket
from pathlib import Path
from typing import Any

import httpx

from .. import __version__
from ..config.loader import IotSetupConfig, load_config

_logger = logging.getLogger(__name__)



# ErpNet.FP device kind → (iot.device.type, default subtype) mapping.
# `subtype` is optional in /iot/setup (Odoo defaults to '') but POS
# uses it for printers to distinguish fiscal/receipt/label hardware,
# and quality_iot uses it for fiscal_data_module BODO001 routing. We
# tag every printer as `fiscal` since that's the only kind ErpNet.FP
# drives.
_KIND_TO_IOT_TYPE = {
    "printer": ("printer", "fiscal"),
    "reader":  ("scanner", ""),
    "scale":   ("scale", ""),
    "display": ("display", ""),
    "pinpad":  ("payment", ""),
}


def _resolve_advertised_host(cfg: IotSetupConfig) -> str:
    """Return the hostname to put in iot.box.ip — what browsers will
    fetch the proxy at. Operator-set value wins; fallback to
    `socket.gethostname()` with a warning if it looks Docker-y.
    """
    if cfg.advertised_host:
        return cfg.advertised_host
    host = socket.gethostname()
    if "." not in host:
        # Docker default container IDs (12 hex chars) and unqualified
        # short hostnames are useless for browser fetches.
        _logger.warning(
            "iot_setup.advertised_host not set; falling back to %r — "
            "this is rarely browser-routable. Set advertised_host to "
            "the public hostname browsers reach the proxy at.",
            host,
        )
    return host


def _device_inventory(app) -> dict[str, dict[str, Any]]:
    """Walk `app.state.*_registry` and build the `devices` map for
    /iot/setup. Mirrors the registry layout used by `_device_summary`
    in `registry.py`."""
    state = getattr(app, "state", None)
    if state is None:
        return {}
    out: dict[str, dict[str, Any]] = {}
    layout = (
        ("registry", "printers", "printer"),
        ("pinpad_registry", "pinpads", "pinpad"),
        ("scale_registry", "scales", "scale"),
        ("reader_registry", "readers", "reader"),
        ("display_registry", "displays", "display"),
    )
    for attr, _key, kind in layout:
        try:
            reg = getattr(state, attr, None)
            inner = getattr(reg, _key, None) if reg else None
            if not inner:
                continue
            iot_type, subtype = _KIND_TO_IOT_TYPE.get(kind, (kind, ""))
            for dev_id in inner.keys():
                identifier = f"{kind}.{dev_id}"
                out[identifier] = {
                    "name": f"{kind.title()} {dev_id}",
                    "type": iot_type,
                    "manufacturer": "ErpNet.FP",
                    "connection": "network",
                    "subtype": subtype,
                }
        except Exception:  # noqa: BLE001
            _logger.exception("iot_setup: enumerating %s failed", attr)
    return out


async def _post_setup(client: httpx.AsyncClient,
                      cfg: IotSetupConfig,
                      app) -> bool:
    """One-shot POST. Returns True on 200 OK, False otherwise.
    Best-effort — never raises into the caller."""
    body = {
        "params": {
            "iot_box": {
                "identifier": cfg.identifier,
                "name": cfg.name,
                "ip": _resolve_advertised_host(cfg),
                "version": __version__,
                "token": cfg.token,
            },
            "devices": _device_inventory(app),
        }
    }
    url = f"{cfg.odoo_url.rstrip('/')}/iot/setup"
    try:
        r = await client.post(url, json=body, timeout=15.0)
    except httpx.HTTPError as exc:
        _logger.debug("iot/setup POST %s failed: %s", url, exc)
        return False
    if r.status_code != 200:
        _logger.warning("iot/setup → HTTP %d: %s",
                        r.status_code, r.text[:200])
        return False
    try:
        data = r.json()
    except ValueError:
        _logger.warning("iot/setup non-JSON response: %s", r.text[:200])
        return False
    # Odoo JSON-RPC wraps response in {"result": <iot_channel>}
    result = data.get("result") if isinstance(data, dict) else None
    devices = body["params"]["devices"]
    _logger.info("iot/setup ok — iot_channel=%r, %d device(s) announced",
                 result, len(devices))
    return True


async def iot_setup_loop(app, config_path: Path) -> None:
    """Background task — announce on boot, then every interval seconds."""
    cfg = load_config(config_path).server.iot_setup
    if not cfg.enabled:
        return
    if not cfg.odoo_url or not cfg.identifier:
        _logger.warning(
            "iot_setup enabled but odoo_url / identifier missing — skipping")
        return

    _logger.info(
        "iot_setup loop started → %s as %r (every %ds)",
        cfg.odoo_url, cfg.identifier, cfg.interval_seconds,
    )
    async with httpx.AsyncClient() as client:
        while True:
            try:
                await _post_setup(client, cfg, app)
            except asyncio.CancelledError:
                _logger.info("iot_setup loop cancelled")
                raise
            except Exception:  # noqa: BLE001
                _logger.exception("iot_setup tick raised")
            try:
                await asyncio.sleep(cfg.interval_seconds)
            except asyncio.CancelledError:
                _logger.info("iot_setup loop cancelled (during sleep)")
                raise
