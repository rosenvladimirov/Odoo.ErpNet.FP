"""
Common types for camera-stream drivers.

A `CameraStream` is an abstract long-lived object that delivers
`PlateEvent` events (license-plate recognitions) through a callback,
exactly like `BarcodeReader` delivers `BarcodeScan`. Subclasses funnel
every physical camera through a **go2rtc** sibling (the universal media
hub): RTSP / ONVIF / generic sources are registered as go2rtc streams,
JPEG frames are pulled from go2rtc for the LPR sampling loop, and live
view is served by go2rtc (MSE / WebRTC / MJPEG).

The proxy never decodes video itself — heavy media stays in the go2rtc
process and the ALPR model stays in a sibling μservice (same principle
as the Phase C face-auth driver: no native AV / ML deps in the fiscal
Python process).
"""

from __future__ import annotations

import logging
import unicodedata
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

_logger = logging.getLogger(__name__)

# БГ регистрационните номера de jure са латиница, но физически ползват
# само 12-те „общи" с кирилицата букви. Камери (Hikvision/Dahua) често
# emit-ват кирилица → каноникализираме към латиница, за да съвпадне с
# каквото Odoo пази по партньор/превозно средство.
_BG_CYR2LAT = str.maketrans(
    "АВЕКМНОРСТУХ", "ABEKMHOPCTYX"
)


def normalize_plate(raw: str) -> str:
    """Canonical plate: NFKC, upper, Cyrillic→Latin (BG set), drop any
    separators / whitespace / non-alphanumerics. EU plates are Latin
    already; this only rescues Cyrillic-emitting cameras."""
    s = unicodedata.normalize("NFKC", raw or "").strip().upper()
    s = s.translate(_BG_CYR2LAT)
    return "".join(ch for ch in s if ch.isascii() and ch.isalnum())

# Listener callback signature: (PlateEvent) -> None
PlateListener = Callable[["PlateEvent"], None]


@dataclass
class PlateEvent:
    """A single license-plate recognition event.

    `source` distinguishes how the plate was produced:
      lpr     — emitted by the camera's LPR sampling loop
      inject  — pushed via POST /cameras/{id}/inject (external LPR / test)
      manual  — operator-triggered single-shot recognition
    """

    camera_id: str
    plate: str
    confidence: float = 0.0
    timestamp: datetime = field(default_factory=datetime.now)
    # (x, y, w, h) в пиксели от кадъра; None ако двигателят не го връща
    bbox: Optional[tuple[int, int, int, int]] = None
    source: str = "lpr"
    # Base64 JPEG изрезка на номера — попълва се само при поискване
    # (webhook/IoT payload-ите остават леки по подразбиране)
    image_b64: Optional[str] = None

    def to_json(self, *, include_image: bool = False) -> dict:
        out = {
            "cameraId": self.camera_id,
            "plate": self.plate,
            "confidence": round(self.confidence, 4),
            "timestamp": self.timestamp.strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "source": self.source,
        }
        if self.bbox is not None:
            x, y, w, h = self.bbox
            out["bbox"] = {"x": x, "y": y, "w": w, "h": h}
        if include_image and self.image_b64:
            out["imageB64"] = self.image_b64
        return out


class CameraStream(ABC):
    """ABC for camera-stream drivers.

    Lifecycle (identical contract to `BarcodeReader`):
        cam = Go2RtcCameraStream(camera_id="gate1", ...)
        cam.set_listener(my_callback)
        cam.start()      # spawns LPR sampling thread, calls listener per plate
        ...
        cam.stop()       # join thread + release go2rtc-side resources
    """

    def __init__(self, camera_id: str) -> None:
        self.camera_id = camera_id
        self._listener: Optional[PlateListener] = None
        self._running = False

    def set_listener(self, listener: Optional[PlateListener]) -> None:
        self._listener = listener

    @abstractmethod
    def start(self) -> None:
        """Begin background plate sampling. Non-blocking."""

    @abstractmethod
    def stop(self) -> None:
        """Stop background thread + release resources."""

    @abstractmethod
    def snapshot(self) -> bytes:
        """Return a fresh JPEG still frame (raises on failure)."""

    def stream_urls(self) -> dict[str, str]:
        """Live-view URLs by format (mse / webrtc / mjpeg / hls / snapshot).

        Empty by default; the go2rtc-backed base fills it in.
        """
        return {}

    @property
    def is_running(self) -> bool:
        return self._running

    def _emit(
        self,
        plate: str,
        confidence: float = 0.0,
        bbox: Optional[tuple[int, int, int, int]] = None,
        source: str = "lpr",
        image_b64: Optional[str] = None,
    ) -> None:
        """Called from the sampling thread for each recognised plate."""
        plate = normalize_plate(plate)
        if not plate:
            return
        evt = PlateEvent(
            camera_id=self.camera_id,
            plate=plate,
            confidence=confidence,
            bbox=bbox,
            source=source,
            image_b64=image_b64,
        )
        if self._listener is not None:
            try:
                self._listener(evt)
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "Camera %s listener raised — plate event dropped",
                    self.camera_id,
                )
