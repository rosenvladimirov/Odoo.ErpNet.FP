"""
CORS + Private Network Access (PNA) за browser-facing проксито.

🚨 21.09.2026, касата на Баръмски (Windows, Starlette 1.6.0): preflight от
odoo.sh с `Access-Control-Request-Private-Network: true` падаше с 400
„Disallowed CORS private-network“. Starlette сам обработва PNA и го
отказва, ако CORSMiddleware не получи `allow_private_network=True` —
нашият `_pna_header` middleware слагаше заглавката, но статусът оставаше
400. Без PNA заглавката същият preflight минаваше, затова грешката не
личеше при ръчна проверка с curl.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from odoo_erpnet_fp.config.loader import AppConfig, ServerConfig
from odoo_erpnet_fp.server.main import create_app

ODOO = "https://baramski.odoo.com"
REGEX = r"^https://baramski(-[a-z0-9-]+)?\.(dev\.)?odoo\.com$"


def _client() -> TestClient:
    cfg = AppConfig(server=ServerConfig(cors_origin_regex=REGEX))
    return TestClient(create_app(cfg))


def _preflight(client: TestClient, origin: str, pna: bool = True):
    headers = {"Origin": origin, "Access-Control-Request-Method": "POST"}
    if pna:
        headers["Access-Control-Request-Private-Network"] = "true"
    return client.options("/printers/fp1/receipt", headers=headers)


def test_pna_preflight_allowed_for_configured_origin():
    r = _preflight(_client(), ODOO)
    assert r.status_code == 200, r.text
    assert r.headers["access-control-allow-origin"] == ODOO
    assert r.headers["access-control-allow-private-network"] == "true"


def test_preflight_without_pna_still_allowed():
    r = _preflight(_client(), ODOO, pna=False)
    assert r.status_code == 200, r.text
    assert r.headers["access-control-allow-origin"] == ODOO


def test_foreign_origin_rejected_even_with_pna():
    # PNA не бива да отваря вратата за чужд сайт — произходът решава.
    r = _preflight(_client(), "https://evil.example.com")
    assert r.status_code == 400
    assert "access-control-allow-origin" not in r.headers
