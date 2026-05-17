"""
Camera-stream drivers — go2rtc-backed, LPR-emitting.

Every camera funnels through a single go2rtc sibling (the universal
media hub). Drivers differ only in how the go2rtc `src` is computed:

  go2rtc  — source already configured server-side in go2rtc.yaml,
            or any go2rtc-native source string
  rtsp    — plain rtsp:// URL
  onvif   — onvif://user:pass@host (go2rtc resolves the RTSP URI)

All expose the same `CameraStream` ABC: a background sampling thread
pulls JPEG frames from go2rtc, runs them through a pluggable LPR
engine, and emits `PlateEvent`s through a listener callback — the
exact push model `BarcodeReader` uses for `BarcodeScan`.
"""

from .common import CameraStream, PlateEvent
from .go2rtc import GenericRtspCameraStream, Go2RtcCameraStream
from .lpr import (
    FastAlprSiblingEngine,
    LprEngine,
    NullLprEngine,
    PlateCandidate,
    make_lpr_engine,
)
from .onvif_cam import OnvifAnprCameraStream, OnvifCameraStream
from .onvif_native import OnvifNativeClient

__all__ = [
    "CameraStream",
    "PlateEvent",
    "Go2RtcCameraStream",
    "GenericRtspCameraStream",
    "OnvifCameraStream",
    "OnvifAnprCameraStream",
    "OnvifNativeClient",
    "LprEngine",
    "NullLprEngine",
    "FastAlprSiblingEngine",
    "PlateCandidate",
    "make_lpr_engine",
]
