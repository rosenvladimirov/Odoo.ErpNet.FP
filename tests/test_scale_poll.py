# Copyright 2026 Rosen Vladimirov
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl).
"""Фоновият четец на везни — спусъкът за живото тегло.

Измереното на MEC (06.08.2026), което наложи този цикъл: нула
`scale.weighed` на шината за цялото съществуване на базата, при 1 929
други събития за десет минути. Картата в Shop Floor е пасивен слушател,
а `/weight` праща събитие само когато го попитат — значи никой не питаше.
"""
import asyncio
import logging
import types

import pytest

from odoo_erpnet_fp.config.loader import ScaleConfig
from odoo_erpnet_fp.server import scale_bus as sb


def _data(weight=1.0, ok=True, stable=True, mode="weight", count=None):
    return {"weight": weight, "ok": ok, "stable": stable,
            "mode": mode, "count": count, "unit": "kg"}


# ─── Кога заслужава събитие ────────────────────────────────────────

def test_first_reading_always_emits():
    assert sb._changed(None, _data(), sb.DEFAULT_EPSILON_KG) is True


def test_noise_below_epsilon_is_silence():
    """🔑 Иначе една везна дава 2.5 събития/сек завинаги."""
    prev = _data(weight=1.0000)
    assert sb._changed(prev, _data(weight=1.0002),
                       sb.DEFAULT_EPSILON_KG) is False


def test_real_movement_emits():
    prev = _data(weight=1.0)
    assert sb._changed(prev, _data(weight=1.5),
                       sb.DEFAULT_EPSILON_KG) is True


def test_stability_change_emits_even_at_same_weight():
    """Операторът трябва да види кога стойността се е успокоила."""
    prev = _data(weight=1.0, stable=False)
    assert sb._changed(prev, _data(weight=1.0, stable=True),
                       sb.DEFAULT_EPSILON_KG) is True


def test_ok_change_emits():
    prev = _data(ok=True)
    assert sb._changed(prev, _data(ok=False), sb.DEFAULT_EPSILON_KG) is True


def test_mode_change_emits():
    prev = _data(mode="weight")
    assert sb._changed(prev, _data(mode="count", count=7),
                       sb.DEFAULT_EPSILON_KG) is True


def test_count_mode_compares_pieces_not_kilograms():
    """В броячен режим `weight` е празно — сравнява се `count`."""
    prev = _data(weight=None, mode="count", count=7)
    assert sb._changed(prev, _data(weight=None, mode="count", count=7),
                       sb.DEFAULT_EPSILON_KG) is False
    assert sb._changed(prev, _data(weight=None, mode="count", count=8),
                       sb.DEFAULT_EPSILON_KG) is True


def test_none_weight_transitions_emit():
    """Изчезнало и появило се тегло са събития, не шум."""
    assert sb._changed(_data(weight=None), _data(weight=1.0),
                       sb.DEFAULT_EPSILON_KG) is True
    assert sb._changed(_data(weight=1.0), _data(weight=None),
                       sb.DEFAULT_EPSILON_KG) is True


def test_garbage_weight_is_treated_as_change_not_crash():
    assert sb._changed(_data(weight="кофти"), _data(weight=1.0),
                       sb.DEFAULT_EPSILON_KG) is True


# ─── Настройката ───────────────────────────────────────────────────

def test_num_tolerates_strings_and_nonsense():
    assert sb._num({"poll_ms": "400"}, "poll_ms", 0) == 400.0
    assert sb._num({"poll_ms": "кофти"}, "poll_ms", 7) == 7.0
    assert sb._num({}, "poll_ms", 7) == 7.0
    assert sb._num({"poll_ms": None}, "poll_ms", 7) == 7.0


def test_polling_is_off_by_default():
    """🚨 Заварените инсталации не сменят поведението си."""
    assert sb.DEFAULT_POLL_MS == 0


def _app_with(scales):
    reg = types.SimpleNamespace(
        scales={s.id: types.SimpleNamespace(config=s) for s in scales},
        has=lambda i: i in {s.id for s in scales},
    )
    return types.SimpleNamespace(state=types.SimpleNamespace(
        scale_registry=reg))


def test_no_poll_ms_spawns_nothing(caplog):
    caplog.set_level(logging.INFO, logger=sb.__name__)
    app = _app_with([ScaleConfig(id="s1", driver="ohaus_ranger")])
    asyncio.run(sb.scale_poll_loop(app))
    assert "no scale has poll_ms set" in caplog.text


def test_missing_registry_is_a_no_op():
    app = types.SimpleNamespace(state=types.SimpleNamespace())
    asyncio.run(sb.scale_poll_loop(app))


def test_interval_below_floor_is_raised_to_it(caplog, monkeypatch):
    """Под 100 мс китът не смогва и четенията се редят на лока."""
    seen = {}

    async def _fake_poll(app, scale_id, cfg, interval_s, epsilon, heartbeat_s):
        seen["interval_s"] = interval_s

    monkeypatch.setattr(sb, "_poll_one", _fake_poll)
    cfg = ScaleConfig(id="s1", driver="ohaus_ranger", extras={"poll_ms": 5})
    asyncio.run(sb.scale_poll_loop(_app_with([cfg])))
    assert seen["interval_s"] == sb.MIN_POLL_MS / 1000.0
    assert "below the" in caplog.text


def test_extras_drive_the_thresholds(monkeypatch):
    seen = {}

    async def _fake_poll(app, scale_id, cfg, interval_s, epsilon, heartbeat_s):
        seen.update(interval_s=interval_s, epsilon=epsilon,
                    heartbeat_s=heartbeat_s)

    monkeypatch.setattr(sb, "_poll_one", _fake_poll)
    cfg = ScaleConfig(id="s1", driver="ohaus_ranger", extras={
        "poll_ms": 400, "poll_epsilon_kg": 0.01, "poll_heartbeat_s": 5})
    asyncio.run(sb.scale_poll_loop(_app_with([cfg])))
    assert seen == {"interval_s": 0.4, "epsilon": 0.01, "heartbeat_s": 5.0}
