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


def test_polimex_payload_encoding():
    from odoo_erpnet_fp.drivers.access.polimex import PolimexWebSdkActuator
    p = PolimexWebSdkActuator._payload
    assert p(1, 1, 3) == "010103"      # output 1, open, 3 s
    assert p(1, 0, 0) == "010000"      # output 1, close, latched
    assert p(10, 1, 5) == "0a0105"     # output hex, open, 5 s
    assert p(1, 1, 250) == "010199"    # time clamped to 99


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
