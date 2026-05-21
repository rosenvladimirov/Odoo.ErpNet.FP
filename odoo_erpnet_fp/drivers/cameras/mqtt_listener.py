# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.

"""MQTT subscriber → CameraEventBus fan-out.

Some LPR cameras (mostly older Hikvision/Dahua/Vivotek with custom
firmware, and the wide range of "smart" parking gates) publish plate
events to an MQTT broker instead of HTTP-POST-ing them. This module
adds **one MqttCameraIngest instance per broker** that:

  1. Subscribes to the configured topic patterns.
  2. Parses each incoming JSON message into a PlateEvent.
  3. Looks up the matching camera via `cameraId` / `camera_id` in
     the payload (fallback: parse from topic name if `<prefix>/<id>`).
  4. Publishes the event through the existing CameraEventBus on the
     resolved camera — same fan-out path as Hik ISAPI / Dahua CGI /
     Polimex WebSDK / native ONVIF (WS / SSE / webhooks / native-IoT
     long-poll `camera.<id>`).

Multi-broker (R5): the proxy can run several MqttCameraIngest
instances simultaneously — e.g. one for a parking-lot broker and
one for a warehouse broker. Each is keyed by `spec.name` in
`MqttIngestRegistry`. NOT a per-camera driver — camera-stream drivers
(rtsp/onvif/go2rtc/external) live in CameraConfig.driver; MQTT lives
in its own MqttBrokerSpec because the lifecycle is fundamentally
different (broker connection, multiplexed topics, no go2rtc involvement).

Lazy paho-mqtt import — config-gated; pure-fiscal/pure-POS deployments
without any `mqtt:` brokers in config.yaml never import paho-mqtt and
never open a connection.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:
    from .common import PlateEvent
    from ..service import CameraRegistry

_logger = logging.getLogger(__name__)

# Recognise plate field aliases used by various camera firmwares.
_PLATE_KEYS = ("plate", "plate_number", "plateNumber", "license", "lp")
_CAMERA_KEYS = ("cameraId", "camera_id", "camera", "deviceId", "device_id")
_CONFIDENCE_KEYS = ("confidence", "conf", "score")
_TIMESTAMP_KEYS = ("timestamp", "time", "ts", "datetime")
# Topic suffix that carries the camera id (e.g. `lpr/gate-north` → gate-north).
_TOPIC_CAMERA_RX = re.compile(r"[^/]+$")


class MqttCameraIngest:
    """One MQTT subscriber publishing PlateEvents into CameraEventBus.

    Owned by `MqttIngestRegistry`; many instances may co-exist
    (one per `MqttBrokerSpec`, keyed by `spec.name`). Start/stop are
    idempotent. paho-mqtt is imported lazily inside the subscriber
    thread so the fiscal-only deployment stays import-clean.
    """

    def __init__(self, config, camera_registry: "CameraRegistry"):
        self.config = config  # MqttBrokerSpec (single broker)
        self.name = getattr(config, "name", "default")
        self.camera_registry = camera_registry
        self._client = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

        # Telemetry
        self.connected = False
        self.last_message_time: Optional[float] = None
        self.messages_received = 0
        self.messages_published = 0
        self.messages_dropped = 0

    # ─── lifecycle ──────────────────────────────────────────────

    def start(self) -> bool:
        """Spawn the subscriber thread. Returns True if newly started."""
        if not self.config or not self.config.enabled:
            return False
        if self._thread and self._thread.is_alive():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"mqtt-ingest-{self.name}", daemon=True)
        self._thread.start()
        _logger.info("MQTT ingest[%s] started (host=%s topics=%r)",
                     self.name, self.config.host, self.config.topics)
        return True

    def stop(self) -> bool:
        """Signal the thread to exit + close the broker connection."""
        if not self._thread or not self._thread.is_alive():
            return False
        self._stop.set()
        try:
            if self._client is not None:
                self._client.disconnect()
        except Exception as exc:  # noqa: BLE001
            _logger.warning("MQTT disconnect raised: %s", exc)
        self._thread.join(timeout=5)
        self._thread = None
        self._client = None
        self.connected = False
        _logger.info("MQTT ingest[%s] stopped", self.name)
        return True

    def status(self) -> dict:
        return {
            "name": self.name,
            "enabled": bool(self.config and self.config.enabled),
            "running": bool(self._thread and self._thread.is_alive()),
            "connected": self.connected,
            "host": self.config.host if self.config else None,
            "port": self.config.port if self.config else None,
            "topics": list(self.config.topics) if self.config else [],
            "last_message_time": self.last_message_time,
            "messages_received": self.messages_received,
            "messages_published": self.messages_published,
            "messages_dropped": self.messages_dropped,
        }

    # ─── subscriber loop ────────────────────────────────────────

    def _run(self):
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            _logger.error("paho-mqtt is not installed; MQTT ingest disabled")
            return

        # paho-mqtt 2.x callback API selector — fall back to 1.x.
        # Включваме `name` в client_id за multi-broker debugging — иначе
        # няколко брокера в един процес ще се появят с еднакво име в
        # логовете на брокера.
        client_id = f"erpnet-fp-{self.name}-{int(time.time())}"
        try:
            client = mqtt.Client(
                client_id=client_id,
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            )
        except (TypeError, AttributeError):
            client = mqtt.Client(client_id)

        if self.config.user:
            client.username_pw_set(self.config.user, self.config.password or "")
        if self.config.tls:
            client.tls_set()

        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        if self.config.debug:
            client.on_log = lambda c, u, lvl, buf: _logger.debug("mqtt: %s", buf)

        self._client = client

        attempt = 0
        while not self._stop.is_set():
            try:
                client.connect(
                    self.config.host,
                    self.config.port,
                    self.config.keepalive)
                # loop_forever blocks; on broker-side disconnect it
                # returns and we'll retry below.
                client.loop_forever(retry_first_connection=False)
                if self._stop.is_set():
                    break
            except Exception as exc:  # noqa: BLE001
                _logger.warning("MQTT connect/loop failed (attempt %d): %s",
                                attempt + 1, exc)
            attempt += 1
            if attempt >= self.config.reconnect_attempts:
                _logger.error("MQTT max reconnect attempts (%d) reached; giving up",
                              self.config.reconnect_attempts)
                break
            time.sleep(self.config.reconnect_delay)

    # ─── callbacks ──────────────────────────────────────────────

    def _on_connect(self, client, userdata, flags, rc, properties=None):
        if rc == 0:
            self.connected = True
            _logger.info("MQTT[%s] connected to %s:%s",
                         self.name, self.config.host, self.config.port)
            for topic in self.config.topics:
                client.subscribe(topic, qos=self.config.qos)
                _logger.info("MQTT[%s] subscribed: %s qos=%d",
                             self.name, topic, self.config.qos)
        else:
            _logger.warning("MQTT[%s] connect rc=%s flags=%s", self.name, rc, flags)

    def _on_disconnect(self, client, userdata, *args):
        self.connected = False
        _logger.info("MQTT[%s] disconnected", self.name)

    def _on_message(self, client, userdata, msg):
        self.messages_received += 1
        self.last_message_time = time.time()
        try:
            payload = msg.payload.decode("utf-8", errors="replace")
            if self.config.debug:
                _logger.debug("mqtt rx topic=%s payload=%s",
                              msg.topic, payload[:self.config.max_log_payload])
            data = self._parse_payload(payload)
            if data is None:
                self.messages_dropped += 1
                return
            event = self._build_event(msg.topic, data)
            if event is None:
                self.messages_dropped += 1
                return
            # Resolve the camera + publish via its bus
            entry = self.camera_registry.get(event.camera_id)
            if entry is None:
                _logger.warning("MQTT plate for unknown camera %r — dropped",
                                event.camera_id)
                self.messages_dropped += 1
                return
            entry.bus.publish_threadsafe(event)
            self.messages_published += 1
        except Exception as exc:  # noqa: BLE001
            _logger.exception("MQTT message handler crashed: %s", exc)
            self.messages_dropped += 1

    # ─── payload → PlateEvent ───────────────────────────────────

    def _parse_payload(self, payload: str) -> Optional[dict]:
        """Parse an MQTT payload to a dict. Accepts JSON; non-JSON
        falls through to a trivial dict so topic-based plate
        identification still works."""
        s = payload.strip()
        if s.startswith("{"):
            try:
                return json.loads(s)
            except json.JSONDecodeError:
                return None
        # Non-JSON payload — wrap. Some firmwares publish just the
        # plate string; the camera id comes from the topic.
        return {"plate": s}

    def _build_event(self, topic: str, data: dict) -> Optional["PlateEvent"]:
        from .common import PlateEvent, normalize_plate

        plate_raw = next(
            (data[k] for k in _PLATE_KEYS if k in data and data[k]), None)
        if not plate_raw:
            return None
        try:
            plate = normalize_plate(str(plate_raw))
        except Exception:  # noqa: BLE001
            return None

        camera_id = next(
            (str(data[k]) for k in _CAMERA_KEYS if k in data and data[k]), None)
        if not camera_id:
            # Fallback: last topic segment (e.g. `lpr/gate-north` → gate-north)
            m = _TOPIC_CAMERA_RX.search(topic or "")
            camera_id = m.group(0) if m else None
        if not camera_id:
            return None

        confidence = 0.0
        for k in _CONFIDENCE_KEYS:
            if k in data:
                try:
                    confidence = float(data[k])
                except (TypeError, ValueError):
                    pass
                break

        # Timestamp parsing is intentionally permissive: keep payload
        # value if it parses; otherwise fall back to now.
        return PlateEvent(
            camera_id=camera_id,
            plate=plate,
            confidence=confidence,
            source="mqtt",
        )
