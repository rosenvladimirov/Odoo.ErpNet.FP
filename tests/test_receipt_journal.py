"""
Един бон дори при прекъсване (Ф2) — дневникът на бона по УНП.

Доказателството е дневникът на виртуалния апарат: колко документа е
„отпечатал“ — не какво е върнал маршрутът.
"""

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient

from odoo_erpnet_fp.config.loader import AppConfig, PrinterConfig, ServerConfig
from odoo_erpnet_fp.drivers.fiscal.datecs_pm import transport_virtual as tv
from odoo_erpnet_fp.server.main import create_app
from odoo_erpnet_fp.server.receipt_journal import ReceiptJournal

UNS = "VF000001-0001-0000042"
TOKEN = "test-admin-token"


def _receipt(uns=UNS, qty=1):
    return {
        "uniqueSaleNumber": uns,
        "items": [{"type": "sale", "text": "Хляб", "quantity": qty, "unitPrice": 1.50, "taxGroup": 2}],
        "payments": [{"amount": round(1.50 * qty, 2), "paymentType": "cash"}],
    }


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ERPNET_ADMIN_TOKEN", TOKEN)
    tv.reset_state("vfp")
    config = AppConfig(server=ServerConfig(), printers=[
        PrinterConfig(id="vfp", driver="datecs.pm", transport="virtual",
                      operator="1", operator_password="0000", till_number=1),
    ])
    application = create_app(config)
    application.state.receipt_journal = ReceiptJournal(str(tmp_path / "journal.sqlite"))
    yield application
    tv.reset_state("vfp")


@pytest.fixture
def client(app):
    return TestClient(app)


def _sales():
    return tv.get_state("vfp").documents("sale")


def _codes(body):
    return [m["code"] for m in body.get("messages", [])]


def test_two_parallel_posts_print_one_receipt(app):
    async def both():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as c:
            return await asyncio.gather(
                c.post("/printers/vfp/receipt", json=_receipt()),
                c.post("/printers/vfp/receipt", json=_receipt()),
            )

    r1, r2 = asyncio.run(both())
    assert r1.json()["ok"] and r2.json()["ok"]
    assert r1.json()["receiptNumber"] == r2.json()["receiptNumber"]
    assert len(_sales()) == 1


def test_retry_after_success_returns_the_stored_result(client):
    first = client.post("/printers/vfp/receipt", json=_receipt()).json()
    again = client.post("/printers/vfp/receipt", json=_receipt()).json()
    assert again["ok"] and again["receiptNumber"] == first["receiptNumber"]
    assert "I_ALREADY_PRINTED" in _codes(again)
    assert len(_sales()) == 1


def test_timeout_on_close_never_prints_twice(client):
    tv.set_faults("vfp", ["timeout_on_close"])
    first = client.post("/printers/vfp/receipt", json=_receipt()).json()
    assert first["ok"] is False and "E_RECEIPT_UNKNOWN" in _codes(first)
    assert len(_sales()) == 1  # апаратът го е отпечатал
    tv.set_faults("vfp", [])
    again = client.post("/printers/vfp/receipt", json=_receipt()).json()
    assert again["ok"] is False and "E_RECEIPT_UNKNOWN" in _codes(again)
    assert len(_sales()) == 1  # и НЕ е отпечатан втори път

    row = client.get(f"/printers/vfp/receipt/{UNS}").json()
    assert (row["state"], row["stage"]) == ("pending", "closing")

    # без токен — не
    assert client.post(f"/printers/vfp/receipt/{UNS}/resolve?state=done").status_code in (401, 503)
    resolved = client.post(f"/printers/vfp/receipt/{UNS}/resolve?state=done&receiptNumber=1",
                           headers={"X-Admin-Token": TOKEN})
    assert resolved.status_code == 200 and resolved.json()["state"] == "done"
    after = client.post("/printers/vfp/receipt", json=_receipt()).json()
    assert after["ok"] and after["receiptNumber"] == "1"
    assert len(_sales()) == 1


def test_failure_before_payment_allows_a_new_attempt(client):
    tv.set_faults("vfp", ["no_paper"])
    first = client.post("/printers/vfp/receipt", json=_receipt()).json()
    assert first["ok"] is False and "E_RECEIPT_UNKNOWN" not in _codes(first)
    assert client.get(f"/printers/vfp/receipt/{UNS}").json()["state"] == "failed"
    tv.set_faults("vfp", [])
    again = client.post("/printers/vfp/receipt", json=_receipt()).json()
    assert again["ok"] is True
    assert len(_sales()) == 1


def test_other_uns_prints_normally(client):
    client.post("/printers/vfp/receipt", json=_receipt())
    client.post("/printers/vfp/receipt", json=_receipt(uns="VF000001-0001-0000043"))
    assert len(_sales()) == 2


def test_partial_stornos_of_one_sale(client):
    sale = client.post("/printers/vfp/receipt", json=_receipt(qty=3)).json()

    def storno(qty):
        body = _receipt(qty=qty)
        body.update(receiptNumber=sale["receiptNumber"], receiptDateTime=sale["receiptDateTime"],
                    fiscalMemorySerialNumber=sale["fiscalMemorySerialNumber"], reason="refund")
        return client.post("/printers/vfp/reversalreceipt", json=body).json()

    assert storno(1)["ok"]
    assert storno(2)["ok"]          # друго частично връщане — печата
    again = storno(2)               # повторен същият опит — не печата
    assert again["ok"] and "I_ALREADY_PRINTED" in _codes(again)
    assert len(tv.get_state("vfp").documents("storno")) == 2


def test_interrupted_receipt_stays_unknown_after_restart(app, tmp_path):
    client = TestClient(app)
    tv.set_faults("vfp", ["timeout_on_close"])
    client.post("/printers/vfp/receipt", json=_receipt())
    tv.set_faults("vfp", [])
    # рестарт на проксито: нов процес, същият файл на дневника
    app.state.receipt_journal = ReceiptJournal(str(tmp_path / "journal.sqlite"))
    again = TestClient(app).post("/printers/vfp/receipt", json=_receipt()).json()
    assert "E_RECEIPT_UNKNOWN" in _codes(again)
    assert len(_sales()) == 1


def test_healthz_announces_the_features(client):
    features = client.get("/healthz").json()["features"]
    assert features == {"idempotentReceipt": True, "receiptByUns": True}
