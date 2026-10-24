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
    assert features == {"idempotentReceipt": True, "receiptByUns": True, "cashbox": True}


# ─── сверката на прекъснатите бонове с апарата (0x4C) ──────────────

from odoo_erpnet_fp.server import receipt_journal as rj  # noqa: E402


def test_reconcile_finds_the_printed_receipt(client):
    tv.set_faults("vfp", ["timeout_on_close"])
    client.post("/printers/vfp/receipt", json=_receipt())
    tv.set_faults("vfp", [])
    assert client.get(f"/printers/vfp/receipt/{UNS}").json()["slip"] == 1
    decisions = client.post("/printers/vfp/receipt/reconcile").json()
    assert [d["decision"] for d in decisions] == ["done"]
    after = client.post("/printers/vfp/receipt", json=_receipt()).json()
    assert after["ok"] and after["receiptNumber"] == "1"
    assert len(_sales()) == 1


def test_reconcile_leaves_pending_when_the_device_does_not_tell(client):
    # no_status: апаратът не дава 0x4C — сверката не гадае, бонът остава pending
    tv.set_faults("vfp", ["timeout_on_close", "no_status"])
    client.post("/printers/vfp/receipt", json=_receipt())
    decisions = client.post("/printers/vfp/receipt/reconcile").json()
    assert [d["decision"] for d in decisions] == ["pending"]
    again = client.post("/printers/vfp/receipt", json=_receipt()).json()
    assert again["messages"][0]["code"] == "E_RECEIPT_UNKNOWN"
    assert len(_sales()) == 1


def test_reconcile_leaves_a_receipt_still_open_on_the_device(client):
    tv.set_faults("vfp", ["error_on_close"])
    client.post("/printers/vfp/receipt", json=_receipt())
    decisions = client.post("/printers/vfp/receipt/reconcile").json()
    assert decisions[0]["decision"] == "pending"
    assert "still open" in decisions[0]["reason"]


def test_reconcile_marks_a_never_closed_receipt_failed(app, client):
    # бонът е отворен (номер 1), но апаратът е бил рестартиран и го е отказал
    app.state.receipt_journal.put("vfp", "sale", UNS, state="pending", stage="closing", slip=1)
    decisions = client.post("/printers/vfp/receipt/reconcile").json()
    assert decisions[0]["decision"] == "failed"
    again = client.post("/printers/vfp/receipt", json=_receipt()).json()
    assert again["ok"] is True
    assert len(_sales()) == 1


def test_decide_never_guesses():
    assert rj.decide({"slip": None}, {"is_open": 0, "number": 9})[0] == "pending"
    assert rj.decide({"slip": 3}, None)[0] == "pending"
    assert rj.decide({"slip": 3}, {"is_open": None, "number": 3})[0] == "pending"
    assert rj.decide({"slip": 3}, {"is_open": 1, "number": 4})[0] == "pending"
    assert rj.decide({"slip": 3}, {"is_open": 0, "number": 3})[0] == "done"
    assert rj.decide({"slip": 3}, {"is_open": 0, "number": 2})[0] == "failed"


def test_old_journal_gets_the_slip_column(tmp_path):
    import sqlite3
    path = tmp_path / "old.sqlite"
    db = sqlite3.connect(path)
    db.execute("CREATE TABLE receipts (printer_id TEXT NOT NULL, kind TEXT NOT NULL, uns TEXT NOT NULL,"
               " state TEXT NOT NULL, stage TEXT, result TEXT, created TEXT NOT NULL, updated TEXT NOT NULL,"
               " PRIMARY KEY (printer_id, kind, uns))")
    db.commit()
    db.close()
    journal = ReceiptJournal(str(path))
    journal.put("vfp", "sale", UNS, state="pending", stage="closing", slip=7)
    assert journal.get("vfp", "sale", UNS)["slip"] == 7


def test_reconcile_runs_at_start(app):
    # прекъснат бон отпреди рестарта; апаратът го е затворил (номер 1)
    with TestClient(app) as c:
        tv.set_faults("vfp", ["timeout_on_close"])
        c.post("/printers/vfp/receipt", json=_receipt())
        tv.set_faults("vfp", [])
    import time
    with TestClient(app):  # старт: сверката тръгва като задача
        for _ in range(50):
            if app.state.receipt_journal.get("vfp", "sale", UNS)["state"] != "pending":
                break
            time.sleep(0.05)
    assert app.state.receipt_journal.get("vfp", "sale", UNS)["state"] == "done"


def test_status_is_busy_while_printing_not_blocked(app):
    import time

    async def scenario():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as c:
            async with app.state.registry.get("vfp").lock:  # „бонът се печата“
                t0 = time.monotonic()
                r = await c.get("/printers/vfp/status")
                return r.json(), time.monotonic() - t0

    body, took = asyncio.run(scenario())
    assert took < 1.0
    assert body["ok"] is True and _codes(body) == ["I_BUSY"]


def test_drawer_under_both_names(client):
    assert client.post("/printers/vfp/drawer").json()["ok"] is True
    assert client.post("/printers/vfp/cashbox").json()["ok"] is True
    assert len(tv.get_state("vfp").documents("drawer")) == 2
    assert client.get("/healthz").json()["features"]["cashbox"] is True


def test_prune_keeps_pending(tmp_path):
    journal = ReceiptJournal(str(tmp_path / "p.sqlite"))
    for uns, state in (("A", "done"), ("B", "failed"), ("C", "pending")):
        journal.put("vfp", "sale", uns, state=state)
    journal._db.execute("UPDATE receipts SET updated='2000-01-01T00:00:00'")
    journal._db.commit()
    assert journal.prune(90) == 2
    assert journal.get("vfp", "sale", "C")["state"] == "pending"


def test_last_document(client):
    assert client.get("/printers/vfp/lastdocument").json() == {"number": 0, "isOpen": False}
    client.post("/printers/vfp/receipt", json=_receipt())
    assert client.get("/printers/vfp/lastdocument").json() == {"number": 1, "isOpen": False}
