"""Резервният УНП от проксито (3.0.23): касата без порция от Odoo пак издава
бон, щом апаратът е наличен — номерът не се повтаря никога."""

import pytest
from fastapi.testclient import TestClient

from odoo_erpnet_fp.config.loader import AppConfig, PrinterConfig, ServerConfig
from odoo_erpnet_fp.drivers.fiscal.datecs_pm import transport_virtual as tv
from odoo_erpnet_fp.server.main import create_app
from odoo_erpnet_fp.server.receipt_journal import FALLBACK_FIRST, FALLBACK_LAST, ReceiptJournal


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("ERPNET_ADMIN_TOKEN", "test-admin-token")
    tv.reset_state("vfp")
    config = AppConfig(server=ServerConfig(), printers=[
        PrinterConfig(id="vfp", driver="datecs.pm", transport="virtual",
                      operator="1", operator_password="0000", till_number=1),
    ])
    application = create_app(config)
    application.state.receipt_journal = ReceiptJournal(str(tmp_path / "journal.sqlite"))
    yield application
    tv.reset_state("vfp")


def _take(client):
    return client.post("/printers/vfp/uns/fallback")


def test_numbers_come_from_the_reserved_range_and_never_repeat(app):
    client = TestClient(app)
    serial = client.get("/printers/vfp").json()["serialNumber"]
    got = [_take(client).json() for _ in range(3)]
    assert [g["number"] for g in got] == [FALLBACK_FIRST, FALLBACK_FIRST + 1, FALLBACK_FIRST + 2]
    assert {g["serialNumber"] for g in got} == {serial}


def test_the_counter_survives_a_restart(app, tmp_path):
    client = TestClient(app)
    first = _take(client).json()["number"]
    app.state.receipt_journal = ReceiptJournal(str(tmp_path / "journal.sqlite"))
    assert _take(TestClient(app)).json()["number"] == first + 1


def test_a_wiped_counter_starts_above_the_used_numbers(app, tmp_path):
    journal = app.state.receipt_journal
    journal.put("vfp", "sale", f"VF000001-0001-{FALLBACK_FIRST + 41:07d}", state="done")
    assert _take(TestClient(app)).json()["number"] == FALLBACK_FIRST + 42


def test_no_number_from_a_journal_in_memory(app):
    app.state.receipt_journal = ReceiptJournal(":memory:")
    resp = _take(TestClient(app))
    assert resp.status_code == 503


def test_no_number_after_the_range_is_used_up(app):
    journal = app.state.receipt_journal
    with journal._db:
        journal._db.execute("INSERT INTO uns_fallback (printer_id, next) VALUES ('vfp', ?)", (FALLBACK_LAST + 1,))
    assert _take(TestClient(app)).status_code == 503


def test_no_number_without_the_device_serial(app, monkeypatch):
    from odoo_erpnet_fp.server.routes import printers
    from odoo_erpnet_fp.server.schemas import DeviceInfo

    async def no_serial(id, request):
        return DeviceInfo.model_validate({"uri": "x", "serialNumber": ""})
    monkeypatch.setattr(printers, "printer_info", no_serial)
    assert _take(TestClient(app)).status_code == 409


def test_healthz_announces_the_fallback(app):
    assert TestClient(app).get("/healthz").json()["features"]["unsFallback"] is True
