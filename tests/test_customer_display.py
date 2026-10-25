"""Customer display through the proxy: POS → /customer-display → screens and pole."""

from contextlib import asynccontextmanager

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from odoo_erpnet_fp.server.customer_display_bus import CustomerDisplayBus, encodable, pole_lines
from odoo_erpnet_fp.server.routes.customer_display import router


ORDER = {
    "amount": "51,70\xa0€",
    "lines": [
        {"productName": "Безжична мишка", "qty": "2", "unitPrice": "24,90\xa0€", "price": "49,80\xa0€", "isSelected": False},
        {"productName": "Кабел USB-C – USB-C 1 м", "qty": "1", "unitPrice": "1,90\xa0€", "price": "1,90\xa0€", "isSelected": True},
    ],
    "paymentLines": [],
}


class FakeDisplay:
    def __init__(self):
        self.calls = []

    def display_two_lines(self, top, bottom):
        self.calls.append((top, bottom))


class FakeRegistry:
    class _Entry:
        class config:  # noqa: N801 — като DisplayConfig
            chars_per_line = 20
            encoding = "cp437"

    def __init__(self, fail=False):
        self.display = FakeDisplay()
        self.fail = fail

    def has(self, id):
        return id == "dpd1"

    def get(self, id):
        return self._Entry()

    @asynccontextmanager
    async def with_display(self, id):
        if self.fail:
            raise RuntimeError("port gone")
        yield self.display


@pytest.fixture
def app():
    app = FastAPI()
    app.state.customer_display_bus = CustomerDisplayBus()
    app.state.display_registry = FakeRegistry()
    app.include_router(router)
    return app


@pytest.fixture
def client(app):
    return TestClient(app)


# ─── pole_lines ──────────────────────────────────────────────


def test_pole_selected_line_name_and_price():
    top, bottom = pole_lines({"order": ORDER})
    assert top == "Кабел USB-C – USB-C "
    # количество 1 — без „1 x“, само цената вдясно
    assert bottom == "1,90 €".rjust(20)


def test_pole_quantity_and_unit_price():
    order = {**ORDER, "lines": [{**ORDER["lines"][0], "isSelected": True}]}
    top, bottom = pole_lines({"order": order})
    assert top == "Безжична мишка"
    assert bottom == "2 x 24,90 €  49,80 €"
    assert len(bottom) == 20


def test_pole_total_on_payment_and_change_after():
    paying = {**ORDER, "paymentLines": [{"name": "В брой", "amount": "60,00 €"}]}
    assert pole_lines({"order": paying}) == ("ОБЩО", "51,70 €".rjust(20))
    paid = {**paying, "finalized": True, "change": "8,30\xa0€"}
    assert pole_lines({"order": paid}) == ("РЕСТО", "8,30 €".rjust(20))


def test_pole_idle_welcome_and_labels():
    top, bottom = pole_lines({"order": {}, "shop": "Демо Електроника"})
    assert top.strip() == "Добре дошли"
    assert bottom.strip() == "Демо Електроника"
    top, _ = pole_lines({"order": {}, "labels": {"welcome": "Welcome"}})
    assert top.strip() == "Welcome"


def test_euro_sign_is_spelled_out_where_the_code_page_has_none():
    assert encodable("5,00 €", "cp437") == "5,00 EUR"
    assert encodable("5,00 €", "cp1251") == "5,00 €"
    order = {**ORDER, "lines": [{**ORDER["lines"][0], "isSelected": True}]}
    _, bottom = pole_lines({"order": order}, 20, "cp437")
    assert len(bottom) == 20 and bottom.endswith("49,80 EUR")


# ─── routes ──────────────────────────────────────────────────


def test_update_state_and_listing(client):
    r = client.post("/customer-display/kasa1", json={"order": ORDER, "shop": "Демо"})
    assert r.status_code == 200 and r.json()["ok"] and r.json()["version"] == 1
    state = client.get("/customer-display/kasa1/state").json()
    assert state["version"] == 1 and state["shop"] == "Демо"
    assert state["order"]["lines"][1]["isSelected"]
    assert "pole" not in state
    assert client.get("/customer-display").json()["kasa1"]["version"] == 1


def test_update_drives_the_pole_display(client, app):
    r = client.post("/customer-display/kasa1", json={"order": ORDER, "pole": "dpd1"})
    assert r.json()["pole"] == {"ok": True}
    # cp437 няма €: „EUR“ се пише ПРЕДИ подравняването — редът е точно 20 знака
    assert app.state.display_registry.display.calls == [("Кабел USB-C – USB-C ", "1,90 EUR".rjust(20))]
    assert all(len(line) <= 20 for line in app.state.display_registry.display.calls[0])


def test_a_failing_pole_does_not_fail_the_update(client, app):
    app.state.display_registry.fail = True
    r = client.post("/customer-display/kasa1", json={"order": ORDER, "pole": "dpd1"})
    assert r.status_code == 200 and r.json()["ok"]
    assert r.json()["pole"]["ok"] is False
    assert client.get("/customer-display/kasa1/state").json()["version"] == 1
    r = client.post("/customer-display/kasa1", json={"order": ORDER, "pole": "nope"})
    assert r.json()["pole"]["ok"] is False


def test_page_is_served_by_the_proxy(client):
    r = client.get("/customer-display/kasa1")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "EventSource" in r.text


def test_bad_channel_name_is_refused(client):
    assert client.post("/customer-display/a b", json={"order": {}}).status_code in (400, 404)


def test_publish_reaches_the_listening_screens():
    bus = CustomerDisplayBus()
    ch = bus.channel("kasa1")
    queue = ch.subscribe()
    ch.publish({"order": ORDER})
    ch.publish({"order": {}})
    assert queue.qsize() == 2
    ch.unsubscribe(queue)
    assert ch.listeners == 0
