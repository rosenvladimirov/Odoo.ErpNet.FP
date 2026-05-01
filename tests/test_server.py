"""
End-to-end HTTP tests for the ErpNet.FP-compatible API.

These tests use FastAPI's `TestClient` and inject a `MockDevice` into
the printer registry by monkey-patching `_make_transport`. No real
serial / TCP device is required.

The assertions verify both the wire format (status codes, JSON envelope
shapes per ErpNet.FP `PROTOCOL.md`) and that the underlying driver
calls produce the right Datecs PM frames (decoded from MockDevice's
captured request history).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from mock_device import MockDevice
from odoo_erpnet_fp.config.loader import (
    AppConfig,
    PrinterConfig,
    ServerConfig,
)
from odoo_erpnet_fp.drivers.fiscal.datecs_pm import codec
from odoo_erpnet_fp.server.main import create_app


@pytest.fixture
def mock_device():
    """A MockDevice with the standard handlers most tests need."""
    mock = MockDevice()
    # Status returns no-error PDF-example status bytes
    pdf_status = bytes.fromhex("80808880869a8080")
    mock.expect_static(0x4A, data=b"", status=pdf_status)
    return mock


@pytest.fixture
def app_with_mock(mock_device):
    """FastAPI app with one printer 'fp1' wired to `mock_device`."""
    config = AppConfig(
        server=ServerConfig(),
        printers=[
            PrinterConfig(
                id="fp1",
                driver="datecs.pm",
                transport="serial",
                port="/dev/null",  # never actually opened
                baudrate=115200,
                operator="1",
                operator_password="1",
                till_number=24,
            )
        ],
    )
    app = create_app(config)

    # Inject mock — replace registry's PM factory so make_driver returns
    # a PmDevice wired to our MockDevice transport.
    registry = app.state.registry

    from odoo_erpnet_fp.drivers.fiscal.datecs_pm import PmDevice

    def _fake_pm(cfg):
        return PmDevice(
            transport=mock_device,
            op_code=int(cfg.operator),
            op_password=cfg.operator_password,
            till_number=cfg.till_number,
        )

    registry._make_pm = _fake_pm  # type: ignore[method-assign]

    return app


@pytest.fixture
def client(app_with_mock):
    return TestClient(app_with_mock)


# ─── 404 + healthz ────────────────────────────────────────────────


def test_healthz_lists_printers(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert "fp1" in body["printers"]


def test_unknown_printer_returns_404(client):
    r = client.get("/printers/nope/status")
    assert r.status_code == 404


# ─── GET /printers, GET /printers/{id} ────────────────────────────


def test_list_printers_returns_dict_keyed_by_id(client):
    r = client.get("/printers")
    assert r.status_code == 200
    body = r.json()
    assert "fp1" in body
    info = body["fp1"]
    # ErpNet.FP-mandated camelCase fields
    assert info["uri"].startswith("bg.dt.pm.")
    assert info["manufacturer"] == "Datecs"
    assert "supportedPaymentTypes" in info
    assert "cash" in info["supportedPaymentTypes"]
    assert "card" in info["supportedPaymentTypes"]


def test_printer_info_endpoint(client):
    r = client.get("/printers/fp1")
    assert r.status_code == 200
    info = r.json()
    assert info["uri"].startswith("bg.dt.pm.")


# ─── GET /printers/{id}/status ────────────────────────────────────


def test_status_returns_ok_envelope(client):
    r = client.get("/printers/fp1/status")
    assert r.status_code == 200
    body = r.json()
    # ErpNet.FP envelope per PROTOCOL.md §"Get Printer Status"
    assert body["ok"] is True
    assert "deviceDateTime" in body
    assert "messages" in body
    msg_types = [m["type"] for m in body["messages"]]
    # PDF status example has fiscalized + tax_set + fm_formatted set
    # → our message adapter emits info entries for each
    assert "info" in msg_types


# ─── POST /printers/{id}/receipt ──────────────────────────────────


def test_print_receipt_happy_path(client, mock_device):
    """End-to-end receipt: open + sale + subtotal + payment + close."""
    # Pre-canned responses for each command
    mock_device.expect_static(
        0x30, data=codec.encode_data(0, 472), status=b"\x80" * 8  # open → slip 472
    )
    mock_device.expect_static(
        0x31, data=codec.encode_data(0, "1.50"), status=b"\x80" * 8  # register sale
    )
    mock_device.expect_static(
        0x33, data=codec.encode_data(0, "1.50"), status=b"\x80" * 8  # subtotal
    )
    mock_device.expect_static(
        0x35, data=codec.encode_data(0, "0.00"), status=b"\x80" * 8  # payment
    )
    mock_device.expect_static(
        0x38, data=codec.encode_data(0, 472), status=b"\x80" * 8  # close
    )

    payload = {
        "uniqueSaleNumber": "DT636533-0020-0010110",
        "items": [
            {
                "type": "sale",
                "text": "Хляб ръжен",
                "quantity": 1,
                "unitPrice": 1.50,
                "taxGroup": 1,  # А = 20% in our default mapping
            }
        ],
        "payments": [{"amount": 1.50, "paymentType": "cash"}],
    }

    r = client.post("/printers/fp1/receipt", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["receiptNumber"] == "472"
    assert body["receiptAmount"] == 1.50
    assert "receiptDateTime" in body

    # Verify the wire calls
    cmds_seen = [req.cmd for req in mock_device.history]
    assert cmds_seen == [0x30, 0x31, 0x33, 0x35, 0x38]


def test_print_receipt_with_card_payment(client, mock_device):
    """Card payment maps to Datecs PM payment_type=1."""
    mock_device.expect_static(0x30, data=codec.encode_data(0, 100), status=b"\x80" * 8)
    mock_device.expect_static(
        0x31, data=codec.encode_data(0, "10.00"), status=b"\x80" * 8
    )
    mock_device.expect_static(
        0x33, data=codec.encode_data(0, "10.00"), status=b"\x80" * 8
    )
    mock_device.expect_static(
        0x35, data=codec.encode_data(0, "0.00"), status=b"\x80" * 8
    )
    mock_device.expect_static(0x38, data=codec.encode_data(0, 100), status=b"\x80" * 8)

    payload = {
        "uniqueSaleNumber": "DT000001-0001-0000001",
        "items": [
            {
                "type": "sale",
                "text": "Service",
                "unitPrice": 10.00,
                "taxGroup": 2,  # Б
            }
        ],
        "payments": [{"amount": 10.00, "paymentType": "card"}],
    }
    r = client.post("/printers/fp1/receipt", json=payload)
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Verify cmd 0x35 (payment) had payment_type=1 (card)
    pay_req = [req for req in mock_device.history if req.cmd == 0x35][0]
    fields = codec.decode_data(pay_req.data)
    assert fields[0] == "1"  # payment_type code 1 = card

    # Verify cmd 0x31 (sale) used vat_group "Б" for taxGroup=2
    sale_req = [req for req in mock_device.history if req.cmd == 0x31][0]
    fields = codec.decode_data(sale_req.data)
    assert fields[1] == "Б"


def test_print_receipt_device_error_returns_ok_false(client, mock_device):
    """Driver FiscalError → ok:false envelope, not HTTP 500."""
    mock_device.expect_static(
        0x30,
        data=codec.encode_data(-100008),  # ERR_NOT_READY
        status=b"\x80" * 8,
    )
    payload = {
        "uniqueSaleNumber": "DT000001-0001-0000001",
        "items": [
            {"type": "sale", "text": "x", "unitPrice": 1, "taxGroup": 1}
        ],
    }
    r = client.post("/printers/fp1/receipt", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert len(body["messages"]) == 1
    assert body["messages"][0]["type"] == "error"
    assert body["messages"][0]["code"] == "E100008"


def test_print_receipt_invalid_tax_group(client):
    """ErpNet.FP requires taxGroup 1..8 — pydantic rejects 0 / 9."""
    payload = {
        "uniqueSaleNumber": "DT000001-0001-0000001",
        "items": [
            {"type": "sale", "text": "x", "unitPrice": 1, "taxGroup": 9}
        ],
    }
    r = client.post("/printers/fp1/receipt", json=payload)
    assert r.status_code == 422  # pydantic validation error


# ─── POST /printers/{id}/withdraw + /deposit ──────────────────────


def test_deposit_endpoint(client, mock_device):
    mock_device.expect_static(
        0x46,
        data=codec.encode_data(0, "150.00", "150.00", "0.00"),
        status=b"\x80" * 8,
    )
    r = client.post("/printers/fp1/deposit", json={"amount": 50.0, "text": "Float"})
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Verify cmd 0x46 type=0 (cash in)
    req = mock_device.history[-1]
    fields = codec.decode_data(req.data)
    assert fields[0] == "0"  # type 0 = cash in
    assert fields[1] == "50.00"


def test_withdraw_endpoint(client, mock_device):
    mock_device.expect_static(
        0x46,
        data=codec.encode_data(0, "100.00", "150.00", "-50.00"),
        status=b"\x80" * 8,
    )
    r = client.post("/printers/fp1/withdraw", json={"amount": 50.0})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    req = mock_device.history[-1]
    fields = codec.decode_data(req.data)
    assert fields[0] == "1"  # type 1 = cash out


# ─── X / Z reports ────────────────────────────────────────────────


def test_xreport(client, mock_device):
    mock_device.expect_static(
        0x45,
        data=codec.encode_data(0, 5, *["0.00"] * 16),
        status=b"\x80" * 8,
    )
    r = client.post("/printers/fp1/xreport")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert codec.decode_data(mock_device.history[-1].data) == ["X"]


def test_zreport(client, mock_device):
    mock_device.expect_static(
        0x45,
        data=codec.encode_data(0, 7, "22.40", "127.22", *["0.00"] * 14),
        status=b"\x80" * 8,
    )
    r = client.post("/printers/fp1/zreport")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert codec.decode_data(mock_device.history[-1].data) == ["Z"]


# ─── Duplicate, cash, reset ───────────────────────────────────────


def test_duplicate_endpoint(client, mock_device):
    mock_device.expect_static(0x6D, data=codec.encode_data(0), status=b"\x80" * 8)
    r = client.post("/printers/fp1/duplicate")
    assert r.status_code == 200
    assert r.json()["ok"] is True


def test_cash_endpoint(client, mock_device):
    mock_device.expect_static(
        0x46,
        data=codec.encode_data(0, "300.00", "500.00", "-200.00"),
        status=b"\x80" * 8,
    )
    r = client.get("/printers/fp1/cash")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["amount"] == 300.00
    # Amount=0 (state read) was sent
    fields = codec.decode_data(mock_device.history[-1].data)
    assert fields[1] == "0.00"


def test_reset_endpoint_idempotent(client, mock_device):
    """Reset issues cmd 0x3C cancel — fine even if no receipt is open."""
    mock_device.expect_static(0x3C, data=codec.encode_data(0), status=b"\x80" * 8)
    r = client.post("/printers/fp1/reset")
    assert r.status_code == 200
    assert r.json()["ok"] is True


# ─── Not-yet-implemented stubs return ok:false (not 500) ──────────


def test_reversal_returns_not_implemented(client):
    payload = {
        "uniqueSaleNumber": "DT000001-0001-0000001",
        "receiptNumber": "0000100",
        "receiptDateTime": "2026-05-01T12:00:00",
        "fiscalMemorySerialNumber": "02517985",
        "reason": "refund",
        "items": [
            {"type": "sale", "text": "x", "unitPrice": 1, "taxGroup": 1}
        ],
    }
    r = client.post("/printers/fp1/reversalreceipt", json=payload)
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["messages"][0]["code"] == "E_NOT_IMPLEMENTED"


def test_datetime_returns_not_implemented(client):
    r = client.post("/printers/fp1/datetime", json={"deviceDateTime": "2026-05-01T12:00:00"})
    assert r.status_code == 200
    assert r.json()["ok"] is False
