"""
Виртуалният апарат Datecs PM (`transport: virtual`) — без хардуер.

Тестовете минават през истинската фасада `PmDevice` и през истинския HTTP
маршрут: симулира се само апаратът. Дневникът на апарата е доказателството
колко документа са „отпечатани“.
"""

import pytest
from fastapi.testclient import TestClient

from odoo_erpnet_fp.config.loader import AppConfig, PrinterConfig, ServerConfig
from odoo_erpnet_fp.drivers.fiscal.datecs_pm import PmDevice, errors
from odoo_erpnet_fp.drivers.fiscal.datecs_pm.transport import TransportError
from odoo_erpnet_fp.drivers.fiscal.datecs_pm import transport_virtual as tv
from odoo_erpnet_fp.server.main import create_app

UNS = "VT000001-0001-0000001"


@pytest.fixture(autouse=True)
def fresh_state():
    tv.reset_state("vfp")
    yield
    tv.reset_state("vfp")


def _pm():
    pm = PmDevice(transport=tv.VirtualPmTransport("vfp"), op_code=1, op_password="0000", till_number=1)
    pm.open()
    return pm


def _sale(pm, uns=UNS, pay=None):
    slip = pm.open_fiscal_receipt(nsale=uns)
    pm.register_sale("Хляб", 1.50, 2, vat_group="Б")
    pm.register_sale("Книга", 10.00, 1, vat_group="Г", discount_percent=10)
    total, _ = pm.subtotal()
    change, _ = pm.payment_total(payment_type=0, amount=pay)
    closed = pm.close_fiscal_receipt()
    return slip, total, change, closed


def test_receipt_through_the_facade():
    pm = _pm()
    slip, total, change, closed = _sale(pm, pay=20.0)
    assert (slip, closed) == (1, 1)
    assert total == pytest.approx(12.0)  # 2 × 1,50 + 10 × 0,9
    assert change == pytest.approx(8.0)
    docs = tv.get_state("vfp").documents("sale")
    assert len(docs) == 1
    assert docs[0]["nsale"] == UNS
    assert [l["group"] for l in docs[0]["lines"]] == ["B", "D"]
    assert not pm.read_status().fiscal_open


def test_z_report_per_group_and_reset():
    pm = _pm()
    _sale(pm)
    n_rep, groups = pm.print_z_report()
    assert n_rep == 1
    assert groups["B"] == pytest.approx(3.0)
    assert groups["D"] == pytest.approx(9.0)
    n_rep, groups = pm.print_z_report()
    assert n_rep == 2
    assert sum(groups.values()) == 0


def test_no_paper_refuses_to_open():
    tv.set_faults("vfp", ["no_paper"])
    pm = _pm()
    assert pm.read_status().end_of_paper
    with pytest.raises(errors.FiscalError) as exc:
        pm.open_fiscal_receipt(nsale=UNS)
    assert exc.value.code == tv.ERR_FP_NO_PAPER
    assert tv.get_state("vfp").documents() == []


def test_timeout_on_close_prints_but_does_not_answer():
    # най-опасният случай: бонът е в дневника, а касата получава таймаут
    tv.set_faults("vfp", ["timeout_on_close"])
    pm = _pm()
    pm.open_fiscal_receipt(nsale=UNS)
    pm.register_sale("Хляб", 1.50, 1, vat_group="Б")
    pm.payment_total(payment_type=0)
    with pytest.raises(TransportError):
        pm.close_fiscal_receipt()
    # повторенията на фасадата (същият SEQ) НЕ печатат втори бон
    assert len(tv.get_state("vfp").documents("sale")) == 1


def test_cancel_only_before_payment():
    pm = _pm()
    pm.open_fiscal_receipt(nsale=UNS)
    pm.register_sale("Хляб", 1.50, 1, vat_group="Б")
    pm.cancel_fiscal_receipt()
    assert not pm.read_status().fiscal_open
    pm.open_fiscal_receipt(nsale=UNS)
    pm.register_sale("Хляб", 1.50, 1, vat_group="Б")
    pm.payment_total(payment_type=0)
    with pytest.raises(errors.FiscalError):
        pm.cancel_fiscal_receipt()


def test_second_open_is_refused():
    pm = _pm()
    pm.open_fiscal_receipt(nsale=UNS)
    with pytest.raises(errors.FiscalError) as exc:
        pm.open_fiscal_receipt(nsale=UNS)
    assert exc.value.code == tv.ERR_R_OPEN_BON


def test_unknown_fault_is_rejected():
    with pytest.raises(ValueError):
        tv.set_faults("vfp", ["paper_jam"])


def test_journal_file(tmp_path):
    tv.reset_state("vfp")
    path = tmp_path / "journal.json"
    pm = PmDevice(transport=tv.VirtualPmTransport("vfp", journal_path=str(path)),
                  op_code=1, op_password="0000", till_number=1)
    pm.open()
    _sale(pm)
    assert '"nsale": "VT000001-0001-0000001"' in path.read_text()


# ─── през HTTP маршрута на проксито ─────────────────────────────────

@pytest.fixture
def client():
    config = AppConfig(server=ServerConfig(), printers=[
        PrinterConfig(id="vfp", driver="datecs.pm", transport="virtual",
                      operator="1", operator_password="0000", till_number=1),
    ])
    return TestClient(create_app(config))


def test_receipt_over_http(client):
    payload = {
        "uniqueSaleNumber": UNS,
        "items": [{"type": "sale", "text": "Хляб", "quantity": 2, "unitPrice": 1.50, "taxGroup": 2}],
        "payments": [{"amount": 3.00, "paymentType": "cash"}],
    }
    r = client.post("/printers/vfp/receipt", json=payload)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True, body
    assert body["receiptNumber"] == "1"
    assert body["fiscalMemorySerialNumber"] == "VF000001"
    assert body["uniqueSaleNumber"] == UNS
    docs = tv.get_state("vfp").documents("sale")
    assert len(docs) == 1 and docs[0]["lines"][0]["group"] == "B"


def test_control_endpoints(client):
    r = client.put("/printers/vfp/virtual/faults", json={"faults": ["no_paper"]})
    assert r.status_code == 200 and r.json()["faults"] == ["no_paper"]
    body = client.post("/printers/vfp/receipt", json={
        "uniqueSaleNumber": UNS,
        "items": [{"type": "sale", "text": "Хляб", "quantity": 1, "unitPrice": 1.50, "taxGroup": 2}],
        "payments": [{"amount": 1.50, "paymentType": "cash"}],
    }).json()
    assert body["ok"] is False
    assert client.get("/printers/vfp/virtual").json()["documents"] == []
    assert client.put("/printers/vfp/virtual/faults", json={"faults": ["paper_jam"]}).status_code == 422
    assert client.post("/printers/vfp/virtual/reset").json()["faults"] == []


def test_control_endpoints_refuse_real_printers():
    config = AppConfig(server=ServerConfig(), printers=[
        PrinterConfig(id="real", driver="datecs.pm", transport="serial", port="/dev/null"),
    ])
    c = TestClient(create_app(config))
    assert c.get("/printers/real/virtual").status_code == 404
    assert c.put("/printers/real/virtual/faults", json={"faults": ["no_paper"]}).status_code == 404
