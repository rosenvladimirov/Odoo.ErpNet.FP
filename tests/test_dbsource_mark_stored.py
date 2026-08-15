"""Обратната връзка към Test Adapter — маршрутът и неговата охрана.

Чист unit тест: източникът е фалшив, база не се отваря. Проверява се
точно това, което Odoo отсреща очаква — че писането в чужда база иска
админския токен, че отговорът носи `CommandResult` (`ok`/`affected`), и
че провалът на заявката се връща като `ok: false` с HTTP 200, а не като
500. Разликата е важна: Odoo различава „базата отказа" от „маршрутът го
няма" по точно този признак.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from odoo_erpnet_fp.drivers.dbsource.commands import CommandResult
from odoo_erpnet_fp.server.routes.dbsource import router

_TOKEN = "zz-test-token"


class _FakeCommands:
    """Заместител на `DbSourceCommands` — помни какво е поискано."""

    def __init__(self, result=None):
        self.result = result or CommandResult(True, 2)
        self.seen = None

    async def mark_stored(self, keys):
        self.seen = list(keys)
        return self.result


class _FakeRegistry:
    def __init__(self, commands):
        self.commands = commands

    def snapshot(self):
        return {"sources": list(self.commands)}


@pytest.fixture
def fake():
    return _FakeCommands()


@pytest.fixture
def client(fake, monkeypatch):
    # Токенът се чете от средата през `admin._admin_token()`.
    monkeypatch.setenv("ERPNET_ADMIN_TOKEN", _TOKEN)
    app = FastAPI()
    app.include_router(router)
    app.state.dbsource_registry = _FakeRegistry({"ta": fake})
    return TestClient(app)


def test_marks_the_requested_keys(client, fake):
    r = client.post("/dbsource/ta/mark_stored",
                    headers={"X-Admin-Token": _TOKEN},
                    json={"row_keys": ["4711", "4712"]})

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["affected"] == 2
    assert body["source"] == "ta"
    assert body["requested"] == 2
    assert fake.seen == ["4711", "4712"]


def test_without_token_nothing_is_touched(client, fake):
    """Писането в чужда база не е свободно като четенето."""
    r = client.post("/dbsource/ta/mark_stored",
                    json={"row_keys": ["4711"]})

    assert r.status_code == 401
    assert fake.seen is None


def test_unknown_source_is_404(client, fake):
    r = client.post("/dbsource/nope/mark_stored",
                    headers={"X-Admin-Token": _TOKEN},
                    json={"row_keys": ["4711"]})

    assert r.status_code == 404
    assert fake.seen is None


def test_row_keys_must_be_a_list(client, fake):
    r = client.post("/dbsource/ta/mark_stored",
                    headers={"X-Admin-Token": _TOKEN},
                    json={"row_keys": "4711"})

    assert r.status_code == 400
    assert fake.seen is None


def test_empty_body_is_an_empty_batch(client, fake):
    """Празен списък е позволен — нула засегнати, не грешка."""
    r = client.post("/dbsource/ta/mark_stored",
                    headers={"X-Admin-Token": _TOKEN},
                    json={})

    assert r.status_code == 200
    assert fake.seen == []


def test_database_failure_returns_ok_false_not_500(monkeypatch):
    """Отказът на базата е ОТГОВОР, не срив на маршрута.

    Odoo чете и двете нива: HTTP-то може да е 200, докато `UPDATE`-ът е
    паднал — тогава опашката НЕ се източва.
    """
    monkeypatch.setenv("ERPNET_ADMIN_TOKEN", _TOKEN)
    broken = _FakeCommands(CommandResult(False, 0, "login failed"))
    app = FastAPI()
    app.include_router(router)
    app.state.dbsource_registry = _FakeRegistry({"ta": broken})
    client = TestClient(app)

    r = client.post("/dbsource/ta/mark_stored",
                    headers={"X-Admin-Token": _TOKEN},
                    json={"row_keys": ["4711"]})

    assert r.status_code == 200
    assert r.json()["ok"] is False
    assert r.json()["error"] == "login failed"
