"""
ONVIF camera drivers.

`OnvifCameraStream`
    Streaming goes through go2rtc's native ONVIF support (`onvif://`
    source — zero extra dependency). Optionally attaches a thin
    `OnvifNativeClient` for **synchronous Device IO relay / PTZ
    control** so Odoo can drive a barrier with no queue latency.

`OnvifAnprCameraStream`
    For cameras with **on-board ANPR**: live video still flows through
    go2rtc, but plates come from the camera's own ONVIF analytics
    events (PullPoint) instead of the snapshot→sidecar loop. No
    sidecar, no proxy frame work, lowest latency — the camera is the
    AI. Inherits the control client too.
"""

from __future__ import annotations

import logging
from urllib.parse import quote

from .go2rtc import Go2RtcCameraStream
from .onvif_native import OnvifNativeClient

_logger = logging.getLogger(__name__)


class OnvifCameraStream(Go2RtcCameraStream):
    """ONVIF camera — `onvif://user:pass@host:port?subtype=N` via go2rtc.

    `subtype` selects the ONVIF media profile / sub-stream (0 = main,
    1 = sub). When `control=True` a synchronous ONVIF Device IO / PTZ
    client is attached (`.relay()`, `.pulse()`, `.ptz()`).
    """

    def __init__(
        self,
        camera_id: str,
        host: str,
        port: int = 80,
        user: str = "",
        password: str = "",
        subtype: int = 0,
        control: bool = False,
        relay_output: str = "",
        **kw,
    ) -> None:
        self._onvif_host = host
        self._onvif_port = int(port)
        self._onvif_user = user
        self._onvif_password = password
        self._onvif_subtype = int(subtype)
        # Контрол клиентът се ползва и от ANPR подкласа за events —
        # затова го създаваме при control ИЛИ когато подкласът го иска.
        self._onvif: OnvifNativeClient | None = None
        if control or getattr(self, "_needs_onvif_client", False):
            self._onvif = OnvifNativeClient(
                host=host, port=port, user=user, password=password,
                relay_output=relay_output,
            )
        super().__init__(camera_id, source=None, **kw)

    def _resolve_source(self) -> str:
        if not self._onvif_host:
            raise ValueError(
                f"Camera {self.camera_id!r}: onvif driver needs `host`"
            )
        cred = ""
        if self._onvif_user:
            cred = (
                f"{quote(self._onvif_user, safe='')}:"
                f"{quote(self._onvif_password, safe='')}@"
            )
        return (
            f"onvif://{cred}{self._onvif_host}:{self._onvif_port}"
            f"?subtype={self._onvif_subtype}"
        )

    # ─── Synchronous control (zero queue latency) ───────────────

    @property
    def has_control(self) -> bool:
        return self._onvif is not None

    def _ctl(self) -> OnvifNativeClient:
        if self._onvif is None:
            raise RuntimeError(
                f"Camera {self.camera_id!r}: ONVIF control not enabled "
                f"(set onvif_control: true)"
            )
        return self._onvif

    def relay(self, state: str) -> dict:
        return self._ctl().set_relay(state)

    def pulse(self, seconds: float = 2.0) -> dict:
        return self._ctl().pulse_relay(seconds)

    def ptz(self, action: str, **kw) -> dict:
        return self._ctl().ptz(action, **kw)


class OnvifAnprCameraStream(OnvifCameraStream):
    """ONVIF camera with edge ANPR — plates arrive via ONVIF events.

    Live video stays on go2rtc (inherited). The base sampling thread
    (`_loop`) is replaced by an ONVIF PullPoint subscription that emits
    each camera-recognised plate. The pluggable LPR engine is unused
    (and should be NullLprEngine) — the camera is the recogniser.
    """

    def __init__(self, camera_id: str, host: str, events_topic: str = "",
                 **kw) -> None:
        # Гарантираме ONVIF клиент дори без control (нужен за events).
        self._needs_onvif_client = True
        self._events_topic = events_topic
        super().__init__(camera_id, host=host, **kw)
        if self._onvif is None:  # control=False но пак ни трябва клиент
            self._onvif = OnvifNativeClient(
                host=host, port=self._onvif_port,
                user=self._onvif_user, password=self._onvif_password,
                relay_output=kw.get("relay_output", ""),
            )

    def _loop(self) -> None:
        """Override: subscribe to the camera's ONVIF ANPR events
        instead of pulling JPEG snapshots for a sidecar."""
        _logger.info(
            "Camera %s: ONVIF on-board ANPR (PullPoint) — no sidecar",
            self.camera_id,
        )

        def _on_plate(plate: str, conf: float, raw: dict) -> None:
            if self._is_duplicate(plate):
                return
            self._emit(plate, confidence=conf, source="onvif")

        # Блокиращ цикъл с вграден reconnect; спира на self._stop.
        self._onvif.watch_plates(_on_plate, self._stop,
                                 topic=self._events_topic)
