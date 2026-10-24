"""Виртуалният пинпад (`driver: virtual`) през маршрутите на проксито."""

import threading
import time

import pytest
from fastapi.testclient import TestClient

from odoo_erpnet_fp.config.loader import AppConfig, PinpadConfig, ServerConfig
from odoo_erpnet_fp.drivers.pinpad import virtual as vp
from odoo_erpnet_fp.server.main import create_app


@pytest.fixture
def client():
    vp.reset_state("vpp")
    config = AppConfig(server=ServerConfig(), pinpads=[
        PinpadConfig(id="vpp", driver="virtual", extras={"virtual_timeout": 5}),
        PinpadConfig(id="real", driver="datecs_pay", port="/dev/null"),
    ])
    return TestClient(create_app(config))


def _ops(client):
    return [e["op"] for e in client.get("/pinpads/vpp/virtual").json()["journal"]]


def test_approve_then_void_once(client):
    r = client.post("/pinpads/vpp/purchase", json={"amount": 12.5}).json()
    assert r["ok"] and r["rrn"] and r["authId"]
    void = {"amount": 12.5, "rrn": r["rrn"], "authId": r["authId"]}
    assert client.post("/pinpads/vpp/void", json=void).json()["ok"] is True
    # втори VOID на същото — няма такава (неанулирана) транзакция
    assert client.post("/pinpads/vpp/void", json=void).json()["ok"] is False
    assert _ops(client) == ["purchase", "void", "void"]


def test_void_needs_the_same_amount(client):
    r = client.post("/pinpads/vpp/purchase", json={"amount": 12.5}).json()
    bad = client.post("/pinpads/vpp/void", json={"amount": 10, "rrn": r["rrn"]}).json()
    assert bad["ok"] is False


@pytest.mark.parametrize("mode", ["decline", "immediate_decline"])
def test_declines(client, mode):
    client.put("/pinpads/vpp/virtual/mode", json={"mode": mode})
    r = client.post("/pinpads/vpp/purchase", json={"amount": 3}).json()
    assert r["ok"] is False and not r.get("rrn")


def test_timeout_hangs_until_cancel(client):
    client.put("/pinpads/vpp/virtual/mode", json={"mode": "timeout"})
    out = {}
    t = threading.Thread(target=lambda: out.update(
        client.post("/pinpads/vpp/purchase", json={"amount": 3}).json()))
    t.start()
    time.sleep(0.3)
    assert client.post("/pinpads/vpp/cancel").json()["cancelled"] is True
    t.join(5)
    assert out["ok"] is False and out["error"] == "cancelled"
    client.get("/pinpads/vpp/status")
    assert _ops(client) == ["purchase", "cancel", "status"]


def test_control_endpoints(client):
    assert client.put("/pinpads/vpp/virtual/mode", json={"mode": "nope"}).status_code == 422
    assert client.get("/pinpads/real/virtual").status_code == 404
    client.post("/pinpads/vpp/purchase", json={"amount": 1})
    assert client.post("/pinpads/vpp/virtual/reset").json()["journal"] == []
