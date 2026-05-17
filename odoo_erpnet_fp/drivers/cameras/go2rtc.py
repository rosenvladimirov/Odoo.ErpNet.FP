"""
go2rtc-backed camera stream — the universal media hub.

Every camera type (generic RTSP, ONVIF) funnels through a single
**go2rtc** sibling process. go2rtc owns all the heavy media work
(RTSP/ONVIF/WebRTC/MSE/HLS/MJPEG, reconnection, codec negotiation);
this driver only:

  1. registers the camera's source as a go2rtc stream (idempotent
     `PUT /api/streams`);
  2. pulls a JPEG still every `interval_seconds` from
     `GET /api/frame.jpeg?src=<name>` and feeds it to the pluggable
     LPR engine;
  3. emits a `PlateEvent` for each recognised plate (de-duplicated
     within a short cooldown so a car dwelling in frame fires once).

Live view is delegated wholesale to go2rtc — see `stream_urls()`.
"""

from __future__ import annotations

import base64
import logging
import threading
import time
from typing import Optional

import httpx

from .common import CameraStream
from .lpr import LprEngine, NullLprEngine

_logger = logging.getLogger(__name__)


class Go2RtcCameraStream(CameraStream):
    """Base driver — concrete source resolved by `_resolve_source()`.

    Subclasses (`GenericRtspCameraStream`, `OnvifCameraStream`) only
    override how the go2rtc `src` URL is computed.
    """

    def __init__(
        self,
        camera_id: str,
        go2rtc_url: str = "http://127.0.0.1:1984",
        go2rtc_public_url: str = "",
        stream_name: Optional[str] = None,
        source: Optional[str] = None,
        lpr_engine: Optional[LprEngine] = None,
        interval_seconds: float = 1.0,
        dedupe_cooldown_seconds: float = 8.0,
        include_image: bool = True,
    ) -> None:
        super().__init__(camera_id)
        # Вътрешен: proxy → go2rtc API / snapshot (бърз, Docker мрежа).
        self.go2rtc_url = go2rtc_url.rstrip("/")
        # Публичен (Cloudflare/Traefik): това дава на браузъра/Odoo, за
        # да тече видеото ДИРЕКТНО browser↔go2rtc без proxy в пътя.
        # Празно → fallback към вътрешния (LAN-only).
        self.go2rtc_public_url = (
            go2rtc_public_url.rstrip("/") or self.go2rtc_url
        )
        # go2rtc stream key — по подразбиране = camera_id (стабилно име)
        self.stream_name = stream_name or camera_id
        self._source = source
        self.lpr = lpr_engine or NullLprEngine()
        self.interval = max(0.2, float(interval_seconds))
        self.dedupe_cooldown = float(dedupe_cooldown_seconds)
        self.include_image = include_image

        self._http = httpx.Client(timeout=5.0)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # plate → последен timestamp на който е emit-нат (дедупликация)
        self._last_seen: dict[str, float] = {}

    # ─── Source resolution (subclass hook) ──────────────────────

    def _resolve_source(self) -> str:
        """Return the go2rtc `src` URL. Default: the configured one."""
        if not self._source:
            raise ValueError(
                f"Camera {self.camera_id!r}: no source configured"
            )
        return self._source

    # ─── go2rtc REST helpers ────────────────────────────────────

    def _register_stream(self) -> None:
        """Idempotently tell go2rtc about this camera's source."""
        src = self._resolve_source()
        try:
            resp = self._http.put(
                f"{self.go2rtc_url}/api/streams",
                params={"name": self.stream_name, "src": src},
            )
            resp.raise_for_status()
            _logger.info(
                "Camera %s registered in go2rtc as %r",
                self.camera_id, self.stream_name,
            )
        except Exception as exc:  # noqa: BLE001
            # Не фатално — go2rtc може да има стрийма от config.yaml вече
            _logger.warning(
                "Camera %s go2rtc register failed (%s) — assuming "
                "stream %r already configured server-side",
                self.camera_id, exc, self.stream_name,
            )

    def snapshot(self) -> bytes:
        resp = self._http.get(
            f"{self.go2rtc_url}/api/frame.jpeg",
            params={"src": self.stream_name},
        )
        resp.raise_for_status()
        return resp.content

    def stream_urls(self) -> dict[str, str]:
        """Browser/Odoo-facing URLs — built from the PUBLIC go2rtc so
        the live video flows directly browser↔go2rtc (proxy NOT in the
        path → zero proxy load during streaming). WebRTC/MSE are the
        low-latency paths; MJPEG/HLS are the Cloudflare-safe fallbacks
        go2rtc's own player auto-negotiates."""
        base = self.go2rtc_public_url
        name = self.stream_name
        return {
            "page": f"{base}/stream.html?src={name}",
            "webrtc": f"{base}/api/webrtc?src={name}",
            "mse": f"{base}/api/ws?src={name}",
            "mjpeg": f"{base}/api/stream.mjpeg?src={name}",
            "hls": f"{base}/api/stream.m3u8?src={name}",
            "snapshot": f"{base}/api/frame.jpeg?src={name}",
        }

    def internal_mjpeg_url(self) -> str:
        """INTERNAL go2rtc MJPEG — used only by the optional proxy
        relay (LAN-only deployments without a public go2rtc). Going
        through the proxy loads the fiscal process, so this is opt-in."""
        return (
            f"{self.go2rtc_url}/api/stream.mjpeg?src={self.stream_name}"
        )

    # ─── Lifecycle ──────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._register_stream()
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"camera-{self.camera_id}",
            daemon=True,
        )
        self._running = True
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._running = False
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=5.0)
        self._thread = None
        try:
            self.lpr.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            self._http.close()
        except Exception:  # noqa: BLE001
            pass

    def reset(self) -> None:
        """Re-push the stream definition to go2rtc — fallback button,
        analogous to `BarcodeReader.reset()`."""
        self._register_stream()

    # ─── Sampling loop (background thread) ──────────────────────

    def _loop(self) -> None:
        # Disabled LPR → нищо за sample-ване; стриймът пак работи през
        # go2rtc, само не emit-ваме plate events.
        if isinstance(self.lpr, NullLprEngine):
            _logger.info(
                "Camera %s: LPR disabled — stream-only mode", self.camera_id
            )
            self._stop.wait()
            return

        _logger.info(
            "Camera %s: LPR sampling every %.1fs via go2rtc %s",
            self.camera_id, self.interval, self.lpr.name,
        )
        while not self._stop.is_set():
            t0 = time.monotonic()
            try:
                jpeg = self.snapshot()
                for cand in self.lpr.recognize(jpeg):
                    if self._is_duplicate(cand.plate):
                        continue
                    img_b64 = None
                    if self.include_image:
                        img_b64 = base64.b64encode(jpeg).decode("ascii")
                    self._emit(
                        plate=cand.plate,
                        confidence=cand.confidence,
                        bbox=cand.bbox,
                        source="lpr",
                        image_b64=img_b64,
                    )
            except Exception:  # noqa: BLE001
                _logger.debug(
                    "Camera %s sampling tick failed", self.camera_id,
                    exc_info=True,
                )
            # Постоянна каденция независимо от latency на тика
            elapsed = time.monotonic() - t0
            self._stop.wait(max(0.0, self.interval - elapsed))

    def _is_duplicate(self, plate: str) -> bool:
        """True if `plate` was emitted within the dedupe cooldown."""
        now = time.monotonic()
        last = self._last_seen.get(plate)
        self._last_seen[plate] = now
        # Чистене на стари записи за да не расте речникът безкрайно
        if len(self._last_seen) > 256:
            cutoff = now - self.dedupe_cooldown
            self._last_seen = {
                p: t for p, t in self._last_seen.items() if t >= cutoff
            }
        return last is not None and (now - last) < self.dedupe_cooldown


class GenericRtspCameraStream(Go2RtcCameraStream):
    """Plain `rtsp://user:pass@host:554/path` source via go2rtc."""

    def __init__(self, camera_id: str, rtsp_url: str, **kw) -> None:
        super().__init__(camera_id, source=rtsp_url, **kw)

    def _resolve_source(self) -> str:
        if not self._source or not self._source.startswith(
            ("rtsp://", "rtsps://", "http://", "https://", "ffmpeg:")
        ):
            raise ValueError(
                f"Camera {self.camera_id!r}: rtsp driver needs a "
                f"valid rtsp:// (or ffmpeg:) source"
            )
        return self._source
