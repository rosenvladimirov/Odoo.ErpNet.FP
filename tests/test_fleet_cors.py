"""CORS от флота, приложен в самото прокси (ADR erpnet-kasi-fleet/0006).

30.10.2026, каса на Windows: записана с прода в кода EFP1; нов
staging получава 400 на preflight-а → „NetworkError“ в браузъра. Флотът в
Odoo връща `cors_origins` с всеки heartbeat, но проксито ги пишеше само в
`dynamic.yml` на Traefik — на Windows такъв файл няма и списъкът се губеше.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from odoo_erpnet_fp.config.loader import AppConfig, RegistryConfig, ServerConfig
from odoo_erpnet_fp.server import cors_writer, fleet_cors, registry
from odoo_erpnet_fp.server.main import create_app

PROD = "https://shop.odoo.com"
STAGING = "https://shop-staging-123.dev.odoo.com"


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    monkeypatch.setattr(fleet_cors, "_origins", frozenset())
    monkeypatch.setattr(fleet_cors, "_path", None)
    monkeypatch.setattr(cors_writer, "_DYN_PATH", tmp_path / "no-traefik" / "dynamic.yml")


def _client(tmp_path, cors=(PROD,), fleet=True) -> TestClient:
    cfg = AppConfig(server=ServerConfig(cors_origins=list(cors),
                                        registry=RegistryConfig(enabled=fleet)))
    return TestClient(create_app(cfg, config_path=tmp_path / "config.yaml"))


def _preflight(client: TestClient, origin: str):
    return client.options("/printers/daisy/status", headers={
        "Origin": origin, "Access-Control-Request-Method": "GET",
        "Access-Control-Request-Headers": "content-type",
        "Access-Control-Request-Private-Network": "true"})


def test_staging_is_refused_until_the_fleet_sends_it(tmp_path):
    c = _client(tmp_path)
    assert _preflight(c, STAGING).status_code == 400
    assert fleet_cors.apply([STAGING])
    r = _preflight(c, STAGING)
    assert r.status_code == 200, r.text
    assert r.headers["access-control-allow-origin"] == STAGING
    assert r.headers["access-control-allow-private-network"] == "true"
    # и при самата заявка, не само при preflight-а
    g = c.get("/healthz", headers={"Origin": STAGING})
    assert g.headers.get("access-control-allow-origin") == STAGING


def test_config_yaml_origin_stays_whatever_the_fleet_says(tmp_path):
    c = _client(tmp_path)
    fleet_cors.apply([STAGING])
    assert _preflight(c, PROD).status_code == 200


def test_fleet_list_replaces_the_previous_one(tmp_path):
    c = _client(tmp_path)
    other = "https://other.odoo.com"
    fleet_cors.apply([STAGING, other])
    fleet_cors.apply([other])
    assert _preflight(c, STAGING).status_code == 400
    assert _preflight(c, other).status_code == 200


def test_empty_fleet_list_keeps_the_last_one(tmp_path):
    fleet_cors.apply([STAGING])
    assert fleet_cors.apply([]) is False
    assert fleet_cors.current() == {STAGING}


def test_bad_origins_are_dropped(tmp_path):
    fleet_cors.apply([STAGING + "/", "javascript:alert(1)", "https://x.odoo.com/odoo", "ftp://x.example", "*"])
    assert fleet_cors.current() == {STAGING}


def test_list_survives_a_restart_without_odoo(tmp_path):
    _client(tmp_path)                     # зарежда (празен) и запомня пътя
    fleet_cors.apply([STAGING])
    saved = json.loads((tmp_path / "fleet_cors.json").read_text(encoding="utf-8"))
    assert saved == {"origins": [STAGING]}
    fleet_cors._origins = frozenset()     # „рестарт“
    c = _client(tmp_path)
    assert _preflight(c, STAGING).status_code == 200


def test_middleware_is_there_with_fleet_on_and_no_static_origins(tmp_path):
    c = _client(tmp_path, cors=())
    fleet_cors.apply([STAGING])
    assert _preflight(c, STAGING).status_code == 200


def test_heartbeat_answer_reaches_the_cors_list(tmp_path, monkeypatch):
    """Пътят флот → heartbeat → CORS, без Traefik (както на Windows)."""
    monkeypatch.setattr(registry, "_admin_token_value", lambda: "t")
    monkeypatch.setattr(registry, "_device_summary", lambda app: {})
    monkeypatch.setattr(registry, "_runtime_config_versions", lambda app: {})
    monkeypatch.setattr(registry, "_zen_graphs_summary", lambda: {})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/erp_net_fp/registry/heartbeat"
        return httpx.Response(200, json={"commands": [], "cors_origins": [PROD, STAGING]})

    cfg = RegistryConfig(enabled=True, url="https://shop.odoo.com", secret="s")

    async def go():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await registry._heartbeat(client, cfg, None, "kasa1", "h", "3.0.28", "")

    result, _ = asyncio.run(go())
    assert result == registry.HeartbeatResult.OK
    assert fleet_cors.current() == {PROD, STAGING}
