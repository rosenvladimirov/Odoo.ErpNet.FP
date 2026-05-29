"""
Access-control actuators (Phase B) — config / registry / transports.

No real hardware: the TCP relay uses a fake socket; ONVIF delegation
is monkeypatched. Pure unit tests.
"""

import socket

import pytest
import yaml

from odoo_erpnet_fp.config.loader import _yaml_to_app_config
from odoo_erpnet_fp.drivers.access import (
    GpioActuator,
    MivActuator,
    OnvifRelayActuator,
    RelayTcpActuator,
    WiegandActuator,
)
from odoo_erpnet_fp.drivers.access.relay_tcp import _to_bytes
from odoo_erpnet_fp.server.service import AccessRegistry

_YAML = """
access:
  - {id: gate1, driver: relay_tcp, host: 10.0.0.9, port: 23,
     on_cmd: "hex:A00101A2", off_cmd: "hex:A00100A1", pulse_seconds: 0.05}
  - {id: gate2, driver: onvif, host: 10.0.0.7, user: admin, password: x}
  - {id: gate3, driver: gpio, pin: 17}
  - {id: gate4, driver: miv, host: 10.0.0.5}
  - {id: gate5, driver: wiegand}
"""


def _cfg():
    return _yaml_to_app_config(yaml.safe_load(_YAML))


def test_config_and_registry():
    cfg = _cfg()
    assert [a.id for a in cfg.access] == \
        ["gate1", "gate2", "gate3", "gate4", "gate5"]
    reg = AccessRegistry.from_config(cfg)
    assert reg.has("gate1") and reg.has("gate5")

    dup = _cfg()
    dup.access.append(dup.access[0])
    with pytest.raises(ValueError):
        AccessRegistry.from_config(dup)

    bad = _cfg()
    bad.access[0].driver = "teleport"
    with pytest.raises(ValueError):
        AccessRegistry.from_config(bad)


def test_make_actuator_types():
    cfg = _cfg()
    reg = AccessRegistry.from_config(cfg)
    by = {a.id: a for a in cfg.access}
    assert isinstance(reg._make(by["gate1"]), RelayTcpActuator)
    assert isinstance(reg._make(by["gate2"]), OnvifRelayActuator)
    assert isinstance(reg._make(by["gate3"]), GpioActuator)
    assert isinstance(reg._make(by["gate4"]), MivActuator)
    assert isinstance(reg._make(by["gate5"]), WiegandActuator)


def test_to_bytes():
    assert _to_bytes("hex:A0 01 01 A2") == b"\xa0\x01\x01\xa2"
    assert _to_bytes("on1\\n") == b"on1\n"
    assert _to_bytes("plain") == b"plain"


class _FakeSock:
    sent: list[bytes] = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def sendall(self, b):
        _FakeSock.sent.append(bytes(b))


def test_relay_tcp_open_pulse_and_deny(monkeypatch):
    _FakeSock.sent = []
    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: _FakeSock()
    )
    a = RelayTcpActuator("g", host="10.0.0.9", on_cmd="hex:AA",
                         off_cmd="hex:BB", pulse_seconds=0.02)
    res = a.open()  # default pulse → on, then off
    assert res.ok and res.state == "closed"
    assert _FakeSock.sent == [b"\xaa", b"\xbb"]

    _FakeSock.sent = []
    res2 = a.open(pulse_seconds=0)  # latched
    assert res2.state == "open" and _FakeSock.sent == [b"\xaa"]

    _FakeSock.sent = []
    a.deny()
    assert _FakeSock.sent == [b"\xbb"]


def test_onvif_relay_delegates(monkeypatch):
    a = OnvifRelayActuator("g", host="10.0.0.7", user="u", password="p",
                           pulse_seconds=2)
    calls = []
    monkeypatch.setattr(a._cli, "pulse_relay",
                        lambda s: calls.append(("pulse", s)))
    monkeypatch.setattr(a._cli, "set_relay",
                        lambda s: calls.append(("set", s)))
    a.open()                 # default pulse=2 → pulse_relay
    a.open(pulse_seconds=0)  # latched → set_relay active
    a.deny()                 # → set_relay inactive
    assert calls == [("pulse", 2.0), ("set", "active"), ("set", "inactive")]


def test_stub_drivers_raise():
    with pytest.raises(RuntimeError):
        WiegandActuator("g").open()
    with pytest.raises(NotImplementedError):
        MivActuator("g", host="x").open()


def test_access_result_json():
    from odoo_erpnet_fp.drivers.access.common import AccessResult
    j = AccessResult("g", "open", True, "closed", "pulsed 3s").to_json()
    assert j["controllerId"] == "g" and j["action"] == "open"
    assert j["ok"] is True and j["state"] == "closed"
    assert "timestamp" in j


def test_fail_secure_default():
    cfg = _cfg()
    assert all(a.fail_secure for a in cfg.access)  # default True
    a = RelayTcpActuator("g", host="h")
    assert a.fail_secure is True


def test_polimex_door_payload_encoding():
    from odoo_erpnet_fp.drivers.access.polimex import PolimexWebSdkActuator
    p = PolimexWebSdkActuator._door_payload
    assert p(1, 1, 3) == "010103"      # output 1, open, 3 s
    assert p(1, 0, 0) == "010000"      # output 1, close, latched
    assert p(10, 1, 5) == "0a0105"     # output hex, open, 5 s
    assert p(1, 1, 250) == "010199"    # time clamped to 99


def test_polimex_relay_payload_encoding():
    # Ported 1:1 from the AGPL reference; hand-computed expectations
    # lock the byte layout (1F<reader> + 4 bytes as 3-dec, each char
    # prefixed with '0' → 24 chars).
    from odoo_erpnet_fp.drivers.access.polimex import PolimexWebSdkActuator
    r = PolimexWebSdkActuator._relay_payload
    z22 = "0" * 22
    assert r(1, 2) == "1F01" + z22 + "01"   # reader1, data=1<<0
    assert r(2, 2) == "1F01" + z22 + "02"   # data=1<<1
    # output17,mode2 → reader2, data=1<<16 → b2=1 → inner "000001000000"
    assert r(17, 2) == "1F02" + "000000000001000000000000"
    assert r(5, 3) == "1F01" + z22 + "05"   # mode3 → data=output
    with pytest.raises(ValueError):
        r(1, 9)                              # unsupported mode


def test_polimex_direct_command(monkeypatch):
    import httpx
    from odoo_erpnet_fp.drivers.access.polimex import PolimexWebSdkActuator

    captured = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"ok": True}

    class _Client:
        def __init__(self, *a, **k):
            captured["auth"] = k.get("auth")
        def post(self, url, json=None, **k):
            captured["url"] = url
            captured["body"] = json
            return _Resp()
        def close(self): pass

    monkeypatch.setattr(httpx, "Client", _Client)
    a = PolimexWebSdkActuator("gate4", host="192.168.1.20", user="SDK",
                              password="0000", bus_id=1, output=1,
                              pulse_seconds=3)
    res = a.open()
    assert res.ok and res.state == "closed"
    assert captured["auth"] == ("SDK", "0000")
    assert captured["url"] == "http://192.168.1.20/sdk/cmd.json"
    assert captured["body"] == {"cmd": {"id": 1, "c": "DB", "d": "010103"}}
    a.deny()
    assert captured["body"] == {"cmd": {"id": 1, "c": "DB", "d": "010000"}}


def test_polimex_d1_card_encoding():
    # D1 d-body ported 1:1 from the AGPL reference send_command (door
    # branch): card_num + pin + ts + rights_data + rights_mask, each
    # card/pin char prefixed with '0'; rights as 2 hex.
    from odoo_erpnet_fp.drivers.access.polimex import PolimexWebSdkActuator
    enc = PolimexWebSdkActuator._encode_d1_body
    # reader 1, TS slot 1, pin 0000 → 20+8+8+2+2 = 40 chars
    d = enc("0003201160", "0000", "01000000", 1, 1)
    assert d == "0000000302000101060000000000010000000101"
    assert len(d) == 40
    # remove (rights_data 0, mask set)
    assert enc("0003201160", "0000", "00000000", 0, 1) == \
        "0000000302000101060000000000000000000001"


def test_polimex_card_add_remove_frames(monkeypatch):
    import httpx
    from odoo_erpnet_fp.drivers.access.polimex import PolimexWebSdkActuator
    captured = {}

    class _Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"response": {"e": 0}}

    class _Client:
        def __init__(self, *a, **k): pass
        def post(self, url, json=None, **k):
            captured["url"] = url
            captured["body"] = json
            return _Resp()
        def close(self): pass

    monkeypatch.setattr(httpx, "Client", _Client)
    a = PolimexWebSdkActuator("gate", host="192.168.3.151", user="admin",
                              password="", bus_id=38, output=1)
    a.add_card("0003201160", rights_data=1, rights_mask=1,
               ts_code="01000000", pin_code="0000")
    assert captured["url"] == "http://192.168.3.151/sdk/cmd.json"
    assert captured["body"] == {"cmd": {
        "id": 38, "c": "D1",
        "d": "0000000302000101060000000000010000000101"}}
    a.remove_card("0003201160", rights_mask=1)
    assert captured["body"]["cmd"]["c"] == "D1"
    assert captured["body"]["cmd"]["d"].endswith("000001")
    # relay-type controllers reject card programming (different D1 body)
    rel = PolimexWebSdkActuator("g", host="h", bus_id=1, relay_ctrl=True)
    with pytest.raises(RuntimeError, match="relay-type"):
        rel.add_card("0003201160")


def test_polimex_ts_data_encoding():
    # D3 ts_data: '%02X'%number + 8 days × 4 intervals × 8 chars = 258.
    from odoo_erpnet_fp.drivers.access.polimex import PolimexWebSdkActuator
    enc = PolimexWebSdkActuator._encode_ts_data
    # Mon-Fri 09:00-18:00, weekend+holiday empty
    week = [[(9.0, 18.0)]] * 5 + [[], [], []]
    d = enc(1, week)
    assert len(d) == 258
    assert d[:2] == "01"                       # TS number 1
    assert d[2:10] == "09001800"               # Mon interval 1
    assert d[2 + 32:2 + 32 + 8] == "09001800"  # Tue interval 1
    # weekend (day 5) all-zero
    assert d[2 + 5 * 32:2 + 6 * 32] == "00000000" * 4
    # half-hour boundary 09:30
    d2 = enc(2, [[(9.5, 17.5)]])
    assert d2[:2] == "02" and d2[2:10] == "09301730"


def test_polimex_write_ts_frame(monkeypatch):
    import httpx
    from odoo_erpnet_fp.drivers.access.polimex import PolimexWebSdkActuator
    cap = {}

    class _R:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return {"response": {"e": 0}}

    class _C:
        def __init__(self, *a, **k): pass
        def post(self, url, json=None, **k):
            cap["body"] = json
            return _R()
        def close(self): pass

    monkeypatch.setattr(httpx, "Client", _C)
    a = PolimexWebSdkActuator("g", host="192.168.3.151", user="admin",
                              password="", bus_id=38)
    a.write_time_schedule(1, [[(9.0, 18.0)]] * 5 + [[], [], []])
    assert cap["body"]["cmd"]["c"] == "D3"
    assert cap["body"]["cmd"]["id"] == 38
    assert len(cap["body"]["cmd"]["d"]) == 258
    a.read_time_schedule(1)
    assert cap["body"]["cmd"] == {"id": 38, "c": "F3", "d": "01"}


def test_hikvision_isapi_open_deny(monkeypatch):
    import httpx
    from odoo_erpnet_fp.drivers.access.hikvision import HikvisionIsapiActuator
    cap = {}

    class _R:
        status_code = 200
        def raise_for_status(self): pass

    class _C:
        def __init__(self, *a, **k): cap["auth"] = type(k.get("auth")).__name__
        def put(self, url, content=None, headers=None):
            cap["url"] = url
            cap["body"] = content.decode() if content else ""
            cap["ctype"] = (headers or {}).get("Content-Type")
            return _R()
        def close(self): pass

    monkeypatch.setattr(httpx, "Client", _C)
    a = HikvisionIsapiActuator("g", host="10.0.0.30", user="admin",
                               password="x", door_no=2)
    r = a.open()
    assert r.ok
    assert cap["auth"] == "DigestAuth"
    assert cap["url"] == \
        "http://10.0.0.30/ISAPI/AccessControl/RemoteControl/door/2"
    assert "<cmd>open</cmd>" in cap["body"] and cap["ctype"] == "application/xml"
    a.deny()
    assert "<cmd>close</cmd>" in cap["body"]


def test_dahua_cgi_open(monkeypatch):
    import httpx
    from odoo_erpnet_fp.drivers.access.dahua import DahuaCgiActuator
    cap = {}

    class _R:
        status_code = 200
        text = "OK"
        def raise_for_status(self): pass

    class _C:
        def __init__(self, *a, **k): pass
        def get(self, url, params=None):
            cap["url"] = url
            cap["params"] = params
            return _R()
        def close(self): pass

    monkeypatch.setattr(httpx, "Client", _C)
    a = DahuaCgiActuator("g", host="10.0.0.31", user="admin",
                         password="x", channel=3, user_id="101")
    r = a.open()
    assert r.ok and r.state == "open"
    assert cap["url"] == "http://10.0.0.31/cgi-bin/accessControl.cgi"
    assert cap["params"] == {"action": "openDoor", "channel": 3,
                             "Type": "Remote", "UserID": "101"}


def test_dahua_legacy_sdk_only_404(monkeypatch):
    import httpx
    from odoo_erpnet_fp.drivers.access.dahua import DahuaCgiActuator

    class _R:
        status_code = 404
        text = "Not Found"
        def raise_for_status(self): pass

    class _C:
        def __init__(self, *a, **k): pass
        def get(self, *a, **k): return _R()
        def close(self): pass

    monkeypatch.setattr(httpx, "Client", _C)
    a = DahuaCgiActuator("g", host="10.0.0.99")
    with pytest.raises(RuntimeError, match="SDK-only"):
        a.open()


def test_hik_dahua_registry_and_http_port():
    cfg = _yaml_to_app_config(yaml.safe_load("""
access:
  - {id: h, driver: hikvision, host: 10.0.0.30, user: admin, password: x, output: 2}
  - {id: d, driver: dahua, host: 10.0.0.31, output: 1, user_id: "9"}
"""))
    from odoo_erpnet_fp.drivers.access import (
        DahuaCgiActuator, HikvisionIsapiActuator)
    reg = AccessRegistry.from_config(cfg)
    h = reg._make(cfg.access[0])
    d = reg._make(cfg.access[1])
    assert isinstance(h, HikvisionIsapiActuator) and h.door_no == 2
    # AccessConfig.port default 23 (telnet) must NOT leak → coerced 80
    assert h.port == 80 and d.port == 80
    assert isinstance(d, DahuaCgiActuator) and d.channel == 1 and d.user_id == "9"


def test_polimex_registry_select():
    cfg = _yaml_to_app_config(yaml.safe_load("""
access:
  - {id: g, driver: polimex, host: 10.0.0.20, user: SDK, password: "0000",
     bus_id: 2, output: 3, pulse_seconds: 5}
"""))
    from odoo_erpnet_fp.drivers.access import PolimexWebSdkActuator
    reg = AccessRegistry.from_config(cfg)
    act = reg._make(cfg.access[0])
    assert isinstance(act, PolimexWebSdkActuator)
    assert act.bus_id == 2 and act.output == 3 and act.default_pulse == 5
