"""
Thin ONVIF-native client — used for two things the go2rtc hub does
NOT cover:

  1. **On-board ANPR events.** Cameras with edge LPR push recognised
     plates over ONVIF analytics/metadata (Events / PullPoint). We
     subscribe and emit them — no sidecar, no proxy frame work, the
     lowest possible latency (the camera is the AI).

  2. **ONVIF control (Device IO relay / PTZ).** Many ANPR cameras have
     a built-in relay output that drives the barrier directly. Odoo
     must be able to fire it **synchronously with no queue latency**,
     exactly the way it triggers a device over the native IoT
     `/action` channel (NOT the 60 s Fleet command-queue).

`onvif-zeep` is a LAZY optional import — pure-fiscal deployments never
load it. A missing lib / failed call degrades to a clear RuntimeError;
it never takes the proxy down.

ONVIF analytics payloads are vendor-variable (Hikvision, Dahua,
Uniview, Axis all name the plate field differently), so the parser is
deliberately tolerant: it walks the notification for any item whose
name looks like a plate / confidence and takes the first hit. Tune
per vendor with `onvif_events_topic` if a camera emits several event
kinds on the same PullPoint.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from typing import Callable, Optional

_logger = logging.getLogger(__name__)

from .common import normalize_plate

_PLATE_RE = re.compile(r"plate|licens", re.I)
_CONF_RE = re.compile(r"conf|score|reliab", re.I)

PlateCb = Callable[[str, float, dict], None]


def _onvif_camera(host: str, port: int, user: str, password: str):
    """Lazy-construct an ONVIFCamera. Raises RuntimeError with an
    actionable message if `onvif-zeep` is not installed."""
    try:
        from onvif import ONVIFCamera  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            "onvif-zeep not installed — `pip install "
            "'odoo-erpnet-fp[onvif]'` (or add onvif-zeep) to use "
            "onvif_anpr / onvif_control"
        ) from exc
    return ONVIFCamera(host, int(port), user, password)


def _walk_items(node, out: list[tuple[str, str]]) -> None:
    """Recursively collect (name, value) from an ONVIF message tree —
    handles SimpleItem lists, dicts and zeep objects uniformly."""
    if node is None:
        return
    name = getattr(node, "Name", None)
    val = getattr(node, "Value", None)
    if name is not None and val is not None:
        out.append((str(name), str(val)))
    # zeep objects expose __values__/__iter__ unevenly — probe broadly
    for attr in ("SimpleItem", "ElementItem", "Source", "Data", "Key",
                 "Item", "Message"):
        child = getattr(node, attr, None)
        if child is None:
            continue
        if isinstance(child, (list, tuple)):
            for c in child:
                _walk_items(c, out)
        else:
            _walk_items(child, out)


def _extract_plate(message) -> Optional[tuple[str, float]]:
    """Return (plate, confidence) from one ONVIF NotificationMessage,
    or None if it carries no plate."""
    items: list[tuple[str, str]] = []
    try:
        msg = getattr(getattr(message, "Message", None), "_value_1", None) \
            or getattr(message, "Message", None)
        _walk_items(msg, items)
    except Exception:  # noqa: BLE001
        return None
    plate = ""
    conf = 0.0
    for name, value in items:
        if not plate and _PLATE_RE.search(name) and value.strip():
            plate = normalize_plate(value)
        elif _CONF_RE.search(name):
            try:
                c = float(value)
                conf = c / 100.0 if c > 1.0 else c
            except ValueError:
                pass
    if not plate:
        return None
    return plate, (conf or 1.0)


class OnvifNativeClient:
    """One client per camera. Streaming stays with go2rtc; this only
    does ONVIF Events + Device IO/PTZ control."""

    def __init__(
        self,
        host: str,
        port: int = 80,
        user: str = "",
        password: str = "",
        relay_output: str = "",
    ) -> None:
        self.host = host
        self.port = int(port)
        self.user = user
        self.password = password
        self.relay_output = relay_output
        self._cam = None  # lazy ONVIFCamera
        self._lock = threading.Lock()  # SOAP клиентът не е thread-safe

    def _camera(self):
        if self._cam is None:
            self._cam = _onvif_camera(
                self.host, self.port, self.user, self.password
            )
        return self._cam

    # ─── ANPR event subscription (background thread) ────────────

    def watch_plates(
        self,
        on_plate: PlateCb,
        stop: threading.Event,
        topic: str = "",
    ) -> None:
        """Blocking PullPoint loop — call from a daemon thread. Emits
        every recognised plate via `on_plate(plate, conf, raw)`.
        Reconnects with backoff (same resilience pattern as the serial
        reader); never raises out."""
        backoff = 1
        while not stop.is_set():
            try:
                cam = self._camera()
                cam.create_pullpoint_subscription()
                pp = cam.create_pullpoint_service()
                _logger.info(
                    "ONVIF ANPR subscribed on %s:%s%s",
                    self.host, self.port,
                    f" topic={topic!r}" if topic else "",
                )
                backoff = 1
                while not stop.is_set():
                    resp = pp.PullMessages(
                        {"Timeout": "PT30S", "MessageLimit": 50}
                    )
                    for m in getattr(resp, "NotificationMessage", []) or []:
                        if topic:
                            t = str(getattr(m, "Topic", "") or "")
                            if topic not in t:
                                continue
                        hit = _extract_plate(m)
                        if hit is None:
                            continue
                        plate, conf = hit
                        try:
                            on_plate(plate, conf, {"topic":
                                     str(getattr(m, "Topic", "") or "")})
                        except Exception:  # noqa: BLE001
                            _logger.exception(
                                "ONVIF plate callback raised — dropped"
                            )
            except Exception as exc:  # noqa: BLE001
                if stop.is_set():
                    break
                _logger.warning(
                    "ONVIF events %s:%s lost (%s) — reconnect in %ds",
                    self.host, self.port, exc, backoff,
                )
                self._cam = None
                stop.wait(backoff)
                backoff = min(backoff * 2, 30)

    # ─── Synchronous control (zero queue latency) ───────────────

    def _relay_token(self, deviceio):
        if self.relay_output:
            return self.relay_output
        outs = deviceio.GetRelayOutputs()
        if not outs:
            raise RuntimeError("camera reports no ONVIF relay outputs")
        return outs[0].token

    def set_relay(self, state: str) -> dict:
        """state ∈ {'active','inactive'}. Single SOAP call — синхронно,
        sub-100ms по LAN."""
        if state not in ("active", "inactive"):
            raise ValueError("relay state must be 'active' or 'inactive'")
        with self._lock:
            deviceio = self._camera().create_deviceio_service()
            tok = self._relay_token(deviceio)
            deviceio.SetRelayOutputState(
                {"RelayOutputToken": tok, "LogicalState": state}
            )
        return {"ok": True, "relay": tok, "state": state}

    def pulse_relay(self, seconds: float = 2.0) -> dict:
        """Open the barrier: active → wait → inactive. Synchronous so
        the caller (Odoo /action) gets a definitive result."""
        self.set_relay("active")
        time.sleep(max(0.1, min(float(seconds), 30.0)))
        out = self.set_relay("inactive")
        out["pulsed_seconds"] = seconds
        return out

    def ptz(self, action: str, **kw) -> dict:
        """Minimal PTZ: action ∈ {'goto_preset','stop'} (+ preset=...).
        Continuous/relative move is a documented extension point."""
        with self._lock:
            cam = self._camera()
            media = cam.create_media_service()
            ptz = cam.create_ptz_service()
            profile = media.GetProfiles()[0]
            ptoken = profile.token
            if action == "goto_preset":
                ptz.GotoPreset({"ProfileToken": ptoken,
                                "PresetToken": str(kw.get("preset"))})
            elif action == "stop":
                ptz.Stop({"ProfileToken": ptoken,
                          "PanTilt": True, "Zoom": True})
            else:
                raise ValueError(f"Unsupported ptz action: {action!r}")
        return {"ok": True, "action": action}
