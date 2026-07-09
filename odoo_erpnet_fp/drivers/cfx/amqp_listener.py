# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""CFX-IPC AMQP ingest → bus_inject (live) + signed audit POST.

``CfxAmqpIngest`` is the CFX analogue of ``MqttCameraIngest``: one
instance per CFX **station** (``CfxBrokerSpec``), owned by
``CfxIngestRegistry`` and keyed by ``spec.name``. Each station embeds
**one** Phase-0 ipc-cfx ``CFXPlugin`` façade, which itself brings up every
configured endpoint (RabbitMQ broker AND/OR AMQP 1.0 P2P) on its own
daemon thread — so a single station consumes broker + peer sources in
parallel (PLAN §4 dual-transport requirement).

Seam (where Phase-0 is imported/started):
    * ``spec.sdk_path`` (default ``/home/rosen/Проекти/amqp/proton-amqp/src``)
      is prepended to ``sys.path`` at ``start()`` so the embeddable SDK
      is importable without being pip-installed;
    * ``from server.cfx_plugin import CFXPlugin`` (the amqp repo's
      top-level ``server`` package — distinct from this proxy's
      ``odoo_erpnet_fp.server``, so no name collision);
    * ``CFXPlugin().start(config_dict, on_message=self._on_cfx_message)``
      where ``config_dict = {"endpoints": [...per CfxEndpointSpec...]}``;
    * the SDK invokes ``self._on_cfx_message(cfx_payload, properties)``
      for every received CFX message (runs in an SDK transport thread).

Lazy-import: the ipc-cfx SDK + ``rabbitmq-amqp-python-client`` are only
touched inside ``start()`` (config-gated), so a pure-fiscal deployment
without a ``cfx:`` section never imports them.

⚠ Coupling: importing ``server.cfx_plugin`` transitively runs the SDK's
``server/__init__.py`` (``from . import amqp`` → ``from proton import
Delivery``), so ``python-qpid-proton`` must be installed even for a
broker-only station. A missing dep is caught + logged in ``_bootstrap``
(the station goes idle; the rest of the proxy is unaffected).
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from typing import TYPE_CHECKING, Optional

from .common import (
    CfxEvent,
    extract_body,
    extract_counters,
    extract_handle,
    extract_message_name,
    extract_transaction_id,
    extract_wo_name,
)

if TYPE_CHECKING:
    from ...config.loader import CfxBrokerSpec

_logger = logging.getLogger(__name__)

# Дефолтен път до вградимия ipc-cfx SDK (proton-amqp/src) — override-ва се
# per-station чрез `sdk_path:` в конфигурацията. SDK-то НЕ е pip-инсталирано;
# ако не е и на sys.path, `sdk_path` трябва да сочи към него.
_DEFAULT_SDK_PATH = "/home/rosen/Проекти/amqp/proton-amqp/src"


class CfxAmqpIngest:
    """One CFX station: embeds a CFXPlugin driving broker + P2P endpoints.

    Start/stop are idempotent. The CFXPlugin runs the actual transport
    threads; we wrap ``start()`` in a short-lived bootstrap daemon thread
    so a slow/hanging broker connect can't block the FastAPI lifespan
    startup (parity with ``MqttCameraIngest`` being fully off-thread).
    """

    def __init__(self, config, app=None):
        self.config = config          # CfxBrokerSpec (single station)
        self.name = getattr(config, "name", "default")
        self._app = app               # FastAPI app — for BusInjectClient.from_app
        self._plugin = None           # ipc-cfx CFXPlugin instance
        self._thread: Optional[threading.Thread] = None
        self._started = False

        # Telemetry (mirror MqttCameraIngest)
        self.last_message_time: Optional[float] = None
        self.messages_received = 0
        self.events_live_ok = 0
        self.events_audit_ok = 0
        self.messages_dropped = 0

    # ─── lifecycle ──────────────────────────────────────────────

    def start(self) -> bool:
        """Bootstrap the CFXPlugin on a daemon thread. True if newly started."""
        if not self.config or not self.config.enabled:
            return False
        if self._started:
            return False
        self._started = True
        self._thread = threading.Thread(
            target=self._bootstrap, name=f"cfx-ingest-{self.name}", daemon=True)
        self._thread.start()
        _logger.info("CFX ingest[%s] starting (machine_kind=%s endpoints=%d)",
                     self.name, getattr(self.config, "machine_kind", "?"),
                     len(getattr(self.config, "endpoints", []) or []))
        return True

    def stop(self) -> bool:
        """Stop the CFXPlugin (disconnects every transport) + join."""
        if not self._started:
            return False
        plugin = self._plugin
        if plugin is not None:
            try:
                plugin.stop()
            except Exception as exc:  # noqa: BLE001
                _logger.warning("CFX ingest[%s] plugin stop raised: %s",
                                self.name, exc)
        if self._thread is not None:
            self._thread.join(timeout=5)
        self._thread = None
        self._plugin = None
        self._started = False
        _logger.info("CFX ingest[%s] stopped", self.name)
        return True

    def status(self) -> dict:
        cfg = self.config
        running = bool(self._plugin is not None and getattr(
            self._plugin, "is_running", False))
        return {
            "name": self.name,
            "enabled": bool(cfg and cfg.enabled),
            "running": running,
            "machine_kind": getattr(cfg, "machine_kind", None) if cfg else None,
            "cfx_handle": getattr(cfg, "cfx_handle", None) if cfg else None,
            "endpoints": [self._endpoint_summary(e)
                          for e in (getattr(cfg, "endpoints", []) or [])],
            "last_message_time": self.last_message_time,
            "messages_received": self.messages_received,
            "events_live_ok": self.events_live_ok,
            "events_audit_ok": self.events_audit_ok,
            "messages_dropped": self.messages_dropped,
        }

    @staticmethod
    def _endpoint_summary(e) -> dict:
        # Не издаваме credentials в status — само транспорт + топология.
        return {
            "transport": getattr(e, "transport", None),
            "queue": getattr(e, "queue", None),
            "exchange": getattr(e, "exchange", None),
            "routing_key": getattr(e, "routing_key", None),
            "source": getattr(e, "source", None),
            "mode": getattr(e, "mode", None),
        }

    # ─── bootstrap (embeds Phase-0 CFXPlugin) ───────────────────

    def _bootstrap(self):
        # 1. Make the (non-pip-installed) ipc-cfx SDK importable.
        sdk_path = getattr(self.config, "sdk_path", "") or _DEFAULT_SDK_PATH
        if sdk_path and os.path.isdir(sdk_path) and sdk_path not in sys.path:
            sys.path.insert(0, sdk_path)

        # 2. Lazy-import the Phase-0 façade. NB: the amqp repo exposes a
        #    TOP-LEVEL `server` package (server.cfx_plugin); this proxy's
        #    own package is `odoo_erpnet_fp.server`, so there is no clash.
        try:
            from server.cfx_plugin import CFXPlugin  # type: ignore
        except Exception as exc:  # noqa: BLE001
            _logger.error(
                "CFX ingest[%s]: cannot import ipc-cfx CFXPlugin from %r "
                "(install the `amqp` SDK or set sdk_path): %s",
                self.name, sdk_path, exc)
            self._started = False
            return

        # 3. Build the plugin config dict from the endpoint specs.
        plugin_config = self._build_plugin_config()
        if not plugin_config.get("endpoints"):
            _logger.warning("CFX ingest[%s]: no endpoints configured — idle",
                            self.name)
            self._started = False
            return

        # 4. Start — CFXPlugin spawns its own per-transport daemon threads
        #    and returns immediately; on_message fires from those threads.
        try:
            plugin = CFXPlugin()
            plugin.start(plugin_config, on_message=self._on_cfx_message)
            self._plugin = plugin
            _logger.info("CFX ingest[%s] plugin up (%d endpoint(s))",
                         self.name, len(plugin_config["endpoints"]))
        except Exception as exc:  # noqa: BLE001
            _logger.exception("CFX ingest[%s] plugin start failed: %s",
                              self.name, exc)
            self._started = False

    def _build_plugin_config(self) -> dict:
        """CfxEndpointSpec list → CFXPlugin `{"endpoints": [...]}` dict."""
        endpoints: list[dict] = []
        for e in (getattr(self.config, "endpoints", []) or []):
            spec: dict = {
                "transport": getattr(e, "transport", "broker"),
                "uri": getattr(e, "amqp_uri", "") or "",
            }
            # broker fields
            if getattr(e, "queue", None):
                spec["queue"] = e.queue
            if getattr(e, "exchange", None):
                spec["exchange"] = e.exchange
            if getattr(e, "routing_key", None):
                spec["routing_key"] = e.routing_key
            # p2p fields
            if getattr(e, "source", None):
                spec["source"] = e.source
            if getattr(e, "mode", None):
                spec["mode"] = e.mode
            # TLS — the SDK reads a whole ssl_config dict; a bare tls flag
            # just signals amqps. Pass whichever the spec carries.
            ssl_config = getattr(e, "ssl_config", None)
            if ssl_config:
                spec["ssl_config"] = ssl_config
            endpoints.append(spec)
        return {"endpoints": endpoints}

    # ─── on_message (SDK transport thread) ──────────────────────

    def _on_cfx_message(self, cfx_payload, properties) -> None:
        """Forwarder invoked by CFXPlugin for every received CFX message.

        ``cfx_payload`` is the normalised CFX dict; ``properties`` are the
        AMQP message properties (carry `cfx-message` = the message path).
        Runs in an SDK transport thread — forwarding is synchronous httpx.
        """
        self.messages_received += 1
        self.last_message_time = time.time()
        try:
            payload = cfx_payload if isinstance(cfx_payload, dict) else {}
            props = properties if isinstance(properties, dict) else {}
            message_name = extract_message_name(payload, props)
            body = extract_body(payload)
            event = CfxEvent(
                machine_kind=getattr(self.config, "machine_kind", "") or "generic",
                message_name=message_name,
                cfx_handle=(extract_handle(payload, props)
                            or getattr(self.config, "cfx_handle", "") or ""),
                transaction_id=extract_transaction_id(payload, body, props),
                wo_name=extract_wo_name(payload, body),
                data=body,
                counters=extract_counters(message_name, body),
            )
            self._forward(event)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("CFX ingest[%s] message handler crashed: %s",
                              self.name, exc)
            self.messages_dropped += 1

    def forward(self, event: "CfxEvent") -> None:
        """Public forward entry — used by the /cfx/{station}/inject test
        route to push a synthetic event through the exact same path."""
        self.last_message_time = time.time()
        self._forward(event)

    # ─── forward: LIVE (bus_inject) + AUDIT (signed POST) ───────

    def _forward(self, event: "CfxEvent") -> None:
        # (a) LIVE → bus_inject; (b) AUDIT → signed /erpnet_fp/cfx/ingest.
        # Both reuse the proxy's existing HMAC clients — no new transport.
        from ...clients.bus_inject import BusInjectClient
        client = BusInjectClient.from_app(self._app) if self._app else None
        try:
            if client is not None:
                # LIVE
                try:
                    if client.emit(
                        event.bus_type,
                        device=event.cfx_handle or self.name,
                        device_kind="machine",
                        data=event.to_bus_data(),
                    ) is not None:
                        self.events_live_ok += 1
                except Exception:  # noqa: BLE001
                    _logger.debug("CFX ingest[%s] live emit failed",
                                  self.name, exc_info=True)
                # AUDIT — reuse the SAME registry url + secret + proxy name
                # that BusInjectClient already resolved.
                if self._forward_audit(event, client):
                    self.events_audit_ok += 1
        finally:
            if client is not None:
                client.close()

    def _forward_audit(self, event: "CfxEvent", client) -> bool:
        """Signed sync POST to the Odoo AUDIT endpoint.

        Derives the CFX-ingest URL from the bus_inject client's already
        resolved base + reuses its HMAC secret/proxy name. Signs with the
        proxy's canonical HMAC helper (odoo_forwarder.sign_body over
        sort_keys canonical JSON) — the same scheme the Fleet controllers
        verify. Best-effort: never raises into the caller.
        """
        try:
            import httpx

            from ...server.odoo_forwarder import canonicalise, sign_body

            # bus_inject url is `<base>/erpnet_fp/bus/inject`; the audit
            # endpoint is `<base>/erpnet_fp/cfx/ingest`.
            audit_url = client.url.replace("/erpnet_fp/bus/inject",
                                           "/erpnet_fp/cfx/ingest")
            secret = getattr(client, "secret", "")
            if not secret:
                return False
            body = event.audit_body()
            raw = canonicalise(body)
            sig = sign_body(raw, secret)
            with httpx.Client(timeout=5.0) as hc:
                r = hc.post(audit_url, content=raw, headers={
                    "Content-Type": "application/json",
                    "X-Registry-Signature": sig,
                    "X-Bus-Inject-Proxy": getattr(client, "proxy_name", ""),
                    "User-Agent": "Odoo.ErpNet.FP/1.0 (cfx ingest)",
                    "Accept": "application/json",
                })
            if r.status_code != 200:
                _logger.debug("CFX audit[%s] → HTTP %d: %s",
                              self.name, r.status_code, r.text[:200])
                return False
            return True
        except Exception:  # noqa: BLE001
            _logger.debug("CFX ingest[%s] audit POST failed", self.name,
                          exc_info=True)
            return False
