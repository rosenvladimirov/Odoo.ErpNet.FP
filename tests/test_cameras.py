"""
Camera-stream driver family — config / registry / LPR / dedupe.

Pure unit tests: no network, no go2rtc, no ALPR sidecar. Drivers are
constructed but never `start()`-ed.
"""

import time

import pytest
import yaml

from odoo_erpnet_fp.config.loader import _yaml_to_app_config
from odoo_erpnet_fp.drivers.cameras import (
    FastAlprSiblingEngine,
    GenericRtspCameraStream,
    Go2RtcCameraStream,
    NullLprEngine,
    OnvifCameraStream,
    PlateEvent,
    make_lpr_engine,
)
from odoo_erpnet_fp.server.service import CameraRegistry

_YAML = """
cameras:
  - id: gate1
    driver: rtsp
    source: rtsp://u:p@10.0.0.5:554/Streaming/Channels/101
    go2rtc_url: http://go2rtc:1984/
    lpr:
      enabled: true
      engine: fast_alpr
      url: http://alpr:8002
      region: bg
      min_confidence: 0.6
      interval_seconds: 2
    dedupe_cooldown_seconds: 5
    webhooks: [https://odoo.example.com/hac/plate]
  - id: door2
    driver: onvif
    onvif: {host: 10.0.0.7, port: 8000, user: admin, password: "p@ss/w", subtype: 1}
  - id: ext3
    driver: external
"""


def _cfg():
    return _yaml_to_app_config(yaml.safe_load(_YAML))


def test_config_parse_nested_and_flat():
    cams = {c.id: c for c in _cfg().cameras}
    assert set(cams) == {"gate1", "door2", "ext3"}
    g = cams["gate1"]
    assert g.driver == "rtsp" and g.lpr_enabled is True
    assert g.lpr_min_confidence == 0.6 and g.lpr_region == "bg"
    assert g.go2rtc_url == "http://go2rtc:1984"  # trailing / stripped
    assert g.webhooks == ["https://odoo.example.com/hac/plate"]
    d = cams["door2"]
    assert d.onvif_host == "10.0.0.7" and d.onvif_port == 8000
    assert d.onvif_subtype == 1 and d.lpr_enabled is False


def test_registry_build_and_validation():
    reg = CameraRegistry.from_config(_cfg())
    assert reg.has("gate1") and reg.has("ext3")
    assert reg.get_bus("gate1").webhooks  # bus carries webhooks

    dup = _cfg()
    dup.cameras.append(dup.cameras[0])
    with pytest.raises(ValueError):
        CameraRegistry.from_config(dup)

    bad = _cfg()
    bad.cameras[0].driver = "telepathy"
    with pytest.raises(ValueError):
        CameraRegistry.from_config(bad)


def test_make_driver_types_and_onvif_source():
    cfg = _cfg()
    reg = CameraRegistry.from_config(cfg)
    by_id = {c.id: c for c in cfg.cameras}

    d0 = reg._make_driver(by_id["gate1"])
    assert isinstance(d0, GenericRtspCameraStream)
    assert isinstance(d0.lpr, FastAlprSiblingEngine)
    urls = d0.stream_urls()
    assert {"page", "webrtc", "mse", "mjpeg", "hls", "snapshot"} <= set(urls)
    assert urls["snapshot"].endswith("/api/frame.jpeg?src=gate1")

    d1 = reg._make_driver(by_id["door2"])
    assert isinstance(d1, OnvifCameraStream)
    # креденшълите се URL-енкодват (/ в паролата → %2F)
    assert d1._resolve_source() == "onvif://admin:p%40ss%2Fw@10.0.0.7:8000?subtype=1"
    assert isinstance(d1.lpr, NullLprEngine)  # lpr disabled


def test_lpr_engine_factory():
    assert isinstance(make_lpr_engine(False, "fast_alpr", "x", 0.5, "bg"),
                      NullLprEngine)
    assert isinstance(make_lpr_engine(True, "none", "x", 0.5, "bg"),
                      NullLprEngine)
    assert isinstance(make_lpr_engine(True, "fast_alpr", "http://a", 0.5, "bg"),
                      FastAlprSiblingEngine)
    assert isinstance(make_lpr_engine(True, "no-such", "x", 0.5, "bg"),
                      NullLprEngine)


def test_dedupe_cooldown():
    cam = Go2RtcCameraStream("c", source="rtsp://x", dedupe_cooldown_seconds=0.3)
    assert cam._is_duplicate("CA1111AA") is False  # first sighting
    assert cam._is_duplicate("CA1111AA") is True   # within cooldown
    assert cam._is_duplicate("CA2222BB") is False  # different plate
    time.sleep(0.35)
    assert cam._is_duplicate("CA1111AA") is False  # cooldown elapsed


def test_plate_event_json():
    e = PlateEvent(camera_id="g", plate="ca1234ab", confidence=0.9123456,
                   bbox=(1, 2, 3, 4), source="lpr", image_b64="QUJD")
    j = e.to_json()
    assert j["cameraId"] == "g" and j["plate"] == "ca1234ab"
    assert j["confidence"] == 0.9123 and j["bbox"] == {"x": 1, "y": 2, "w": 3, "h": 4}
    assert "imageB64" not in j  # excluded by default
    assert e.to_json(include_image=True)["imageB64"] == "QUJD"


def test_emit_uppercases_and_drops_empty():
    seen = []
    cam = Go2RtcCameraStream("c", source="rtsp://x")
    cam.set_listener(seen.append)
    cam._emit("  ca1234ab ", confidence=0.8)
    cam._emit("   ")  # празно → дроп
    assert len(seen) == 1
    assert seen[0].plate == "CA1234AB" and seen[0].camera_id == "c"


def test_normalize_plate_bg_cyrillic_and_separators():
    from odoo_erpnet_fp.drivers.cameras.common import normalize_plate
    # кирилица (камера emit-ва Cyrillic) → латиница БГ канон
    assert normalize_plate("СА1234АВ") == "CA1234AB"
    assert normalize_plate(" cа-1234 ав ") == "CA1234AB"  # mix + сепаратори
    assert normalize_plate("EE 0000 KH") == "EE0000KH"
    assert normalize_plate("") == ""


def test_http_listener_parser_vendor_shapes():
    from odoo_erpnet_fp.server.routes.cameras import _scan, _scan_xml
    f = {}
    _scan({"Picture": {"Plate": {"PlateNumber": "CA 1234 AB",
                                 "Confidence": 92}}}, f)
    assert f["plate"] == "CA1234AB" and abs(f["conf"] - 0.92) < 1e-6
    f2 = {}
    _scan_xml(
        "<EventNotificationAlert><ANPR>"
        "<licensePlate>СА1234АВ</licensePlate>"
        "<confidenceLevel>88</confidenceLevel></ANPR>"
        "</EventNotificationAlert>", f2)
    assert f2["plate"] == "CA1234AB" and abs(f2["conf"] - 0.88) < 1e-6


def test_onvif_anpr_driver_and_control_selection():
    cfg = _yaml_to_app_config(yaml.safe_load("""
cameras:
  - {id: gA, driver: onvif, onvif: {host: 10.0.0.7, user: u, password: p, anpr: true, control: true}}
  - {id: gB, driver: onvif, onvif: {host: 10.0.0.8, user: u, password: p}}
"""))
    reg = CameraRegistry.from_config(cfg)
    a = reg._make_driver(cfg.cameras[0])
    b = reg._make_driver(cfg.cameras[1])
    assert type(a).__name__ == "OnvifAnprCameraStream" and a.has_control
    assert type(b).__name__ == "OnvifCameraStream" and not b.has_control
