"""
Odoo → proxy seam for scales: endpoint assembly and fail-soft registry.

Мрежова везна никога не е минавала по този път, затова шевът си беше
скъсан мълчаливо: Odoo подаваше `host` + число `port`, а конфигурацията
четеше само `port`. Тези тестове заковават договора в двете посоки.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from odoo_erpnet_fp.config.loader import ScaleConfig, load_config
from odoo_erpnet_fp.server.service import (
    PUSH_CONFIG_AC_KINDS,
    ScaleRegistry,
    _write_fragment_atomic,
)

# Точно каквото `cas.scale.get_config_payload()` връща в uat-mec.
ODOO_PAYLOAD = [{
    'id': 'ohaus_ranger_count_3000',
    'driver': 'ohaus_ranger',
    'transport': 'network',
    'host': '192.168.3.162',
    'port': 9761,
}]


# ─── endpoint assembly ─────────────────────────────────────────


def test_serial_endpoint_is_the_device_path():
    cfg = ScaleConfig(id="cas1", driver="cas", port="/dev/ttyUSB1")
    assert cfg.endpoint() == "/dev/ttyUSB1"


def test_network_endpoint_joins_host_and_port():
    cfg = ScaleConfig(
        id="ohaus1", driver="ohaus_ranger",
        transport="network", host="192.168.3.162", port=9761,
    )
    assert cfg.endpoint() == "192.168.3.162:9761"


def test_network_endpoint_without_port_is_bare_host():
    # Драйверът сам слага 9761 — фиксиран е във фърмуера на кита.
    cfg = ScaleConfig(
        id="ohaus1", driver="ohaus_ranger",
        transport="network", host="192.168.3.162", port=None,
    )
    assert cfg.endpoint() == "192.168.3.162"


def test_network_endpoint_accepts_pre_joined_port():
    # Ръчно писан конфиг слепва двете в `port`; трябва да мине и така.
    cfg = ScaleConfig(
        id="ohaus1", driver="ohaus_ranger",
        port="192.168.3.162:9761",
    )
    assert cfg.endpoint() == "192.168.3.162:9761"


def test_blank_host_does_not_shadow_serial_path():
    cfg = ScaleConfig(
        id="cas1", driver="cas", port="/dev/ttyUSB1", host="   ",
    )
    assert cfg.endpoint() == "/dev/ttyUSB1"


# ─── fail-soft registry ────────────────────────────────────────


def _config(*scales):
    return SimpleNamespace(scales=list(scales))


def test_unknown_driver_is_skipped_not_fatal(caplog):
    """🚨 Регистърът се строи в `create_app()` без предпазител — вдигане
    оттук сваля ЦЯЛОТО прокси, вкл. достъпния контрол и CFX ingest-а."""
    good = ScaleConfig(id="ohaus1", driver="ohaus_ranger",
                       transport="network", host="192.168.3.162", port=9761)
    bad = ScaleConfig(id="broken", driver="cas.scale", port="/dev/ttyUSB1")

    reg = ScaleRegistry.from_config(_config(bad, good))

    assert "ohaus1" in reg.scales
    assert "broken" not in reg.scales


def test_duplicate_id_keeps_the_first_and_survives():
    first = ScaleConfig(id="dup", driver="cas", port="/dev/ttyUSB1")
    second = ScaleConfig(id="dup", driver="cas", port="/dev/ttyUSB2")

    reg = ScaleRegistry.from_config(_config(first, second))

    assert list(reg.scales) == ["dup"]
    assert reg.scales["dup"].config.port == "/dev/ttyUSB1"


def test_every_scale_broken_still_yields_a_registry():
    reg = ScaleRegistry.from_config(_config(
        ScaleConfig(id="a", driver="nonesuch"),
        ScaleConfig(id="b", driver="also-nonesuch"),
    ))
    assert reg.scales == {}


# ─── driver construction through the registry ──────────────────


def test_make_scale_builds_ohaus_from_split_host_and_port():
    reg = ScaleRegistry.from_config(_config(ScaleConfig(
        id="ohaus1", driver="ohaus_ranger",
        transport="network", host="192.168.3.162", port=9761,
    )))
    scale = reg.make_scale("ohaus1")
    assert scale.host == "192.168.3.162"
    assert scale.tcp_port == 9761


def test_make_scale_uses_kit_default_port_when_odoo_sends_none():
    reg = ScaleRegistry.from_config(_config(ScaleConfig(
        id="ohaus1", driver="ohaus_ranger",
        transport="network", host="192.168.3.162",
    )))
    scale = reg.make_scale("ohaus1")
    assert (scale.host, scale.tcp_port) == ("192.168.3.162", 9761)


@pytest.mark.parametrize("alias", [
    "ohaus", "ohaus_ranger", "ranger3000", "ranger.count3000", "valor7000",
])
def test_all_ohaus_aliases_resolve(alias):
    reg = ScaleRegistry.from_config(_config(ScaleConfig(
        id="s", driver=alias, transport="network", host="10.0.0.1",
    )))
    assert reg.make_scale("s").tcp_port == 9761


# ─── the Odoo → disk → driver round trip ───────────────────────


def test_scales_is_an_accepted_push_kind():
    """🚨 Липсваше. Без него Odoo нареждаше командата, проксито я
    изпълняваше и не записваше нищо — а и двете страни рапортуваха
    успех."""
    assert "scales" in PUSH_CONFIG_AC_KINDS


def test_pushed_fragment_reaches_the_driver(tmp_path: Path):
    """Целият път: payload от Odoo → config.d фрагмент → AppConfig →
    регистър → драйвер с верен адрес и порт."""
    (tmp_path / "config.yaml").write_text(
        "server:\n  host: 0.0.0.0\n  port: 8001\n", encoding="utf-8")
    _write_fragment_atomic(
        tmp_path / "config.d" / "scales.yaml", "scales", ODOO_PAYLOAD)

    cfg = load_config(tmp_path / "config.yaml")

    assert [s.id for s in cfg.scales] == ["ohaus_ranger_count_3000"]
    entry = cfg.scales[0]
    assert entry.endpoint() == "192.168.3.162:9761"

    scale = ScaleRegistry.from_config(cfg).make_scale(
        "ohaus_ranger_count_3000")
    assert (scale.host, scale.tcp_port) == ("192.168.3.162", 9761)


def test_info_response_survives_a_numeric_port():
    """Мрежова везна носи порта като число. Отговорът обявява `str`, тъй
    че суровият `port` го чупеше — `/scales` и `/scales/{id}` връщаха 500,
    докато `/weight` си работеше."""
    from odoo_erpnet_fp.server.routes.scales import _info

    cfg = ScaleConfig(
        id="ohaus1", driver="ohaus_ranger",
        transport="network", host="192.168.3.162", port=9761,
    )
    resp = _info("ohaus1", cfg)
    assert resp.port == "192.168.3.162:9761"
    assert resp.transport == "network"
    assert resp.host == "192.168.3.162"


def test_weight_response_carries_the_identity_of_the_scale():
    """Адресът е идентичността на станцията. Без него слушалката, която
    подрежда четенията по работна карта, няма по какво да филтрира."""
    from odoo_erpnet_fp.server.routes.scales import WeightReadResp

    resp = WeightReadResp(
        ok=True, weight_kg=1.234, scale_id="ohaus_ranger_count_3000",
        host="192.168.3.162",
    )
    dumped = resp.model_dump(by_alias=True)
    assert dumped["scaleId"] == "ohaus_ranger_count_3000"
    assert dumped["host"] == "192.168.3.162"
    assert dumped["weightKg"] == pytest.approx(1.234)


class _RecordingBus:
    def __init__(self, boom=False):
        self.calls = []
        self.boom = boom

    def emit(self, event_type, device="", device_kind="", data=None):
        if self.boom:
            raise RuntimeError("Odoo е недостъпен")
        self.calls.append((event_type, device, device_kind, data))


def test_event_type_matches_what_odoo_already_listens_for():
    """🚨 `l10n_bg_live_refresh` префирва САМО `scale.weighed` като
    `SCALE_READ`. Всеки друг низ минава по канала и не задейства нищо."""
    from odoo_erpnet_fp.server.routes.scales import WEIGHT_EVENT_TYPE

    assert WEIGHT_EVENT_TYPE == "scale.weighed"


def test_weight_event_matches_the_scale_handler_contract():
    """`scale_handler.js` чака `{weight, unit, stable}`; `host` е добавката,
    по която работната карта познава своята станция."""
    from odoo_erpnet_fp.drivers.scales.toledo_8217 import WeightReading
    from odoo_erpnet_fp.server.routes.scales import _weight_event_data

    cfg = ScaleConfig(
        id="ohaus1", driver="ohaus_ranger",
        transport="network", host="192.168.3.162", port=9761)
    data = _weight_event_data(
        "ohaus1", cfg,
        WeightReading(ok=True, weight_kg=1.5, status=[], raw=b""))

    assert data["weight"] == pytest.approx(1.5)
    assert data["unit"] == "kg"
    assert data["stable"] is True
    assert data["host"] == "192.168.3.162"
    assert data["scale_id"] == "ohaus1"


def test_counting_reading_reports_pieces_not_kilograms():
    from odoo_erpnet_fp.drivers.scales.toledo_8217 import WeightReading
    from odoo_erpnet_fp.server.routes.scales import _weight_event_data

    data = _weight_event_data(
        "ohaus1", ScaleConfig(id="ohaus1", driver="ohaus_ranger"),
        WeightReading(ok=True, weight_kg=None, status=[], raw=b"",
                      count=12, mode="count"))

    assert data["mode"] == "count"
    assert data["count"] == 12
    assert data["unit"] == "pcs"
    # Везната не е мерила маса — не си измисляме такава.
    assert data["weight"] is None


def test_unstable_reading_is_marked_unstable():
    from odoo_erpnet_fp.drivers.scales.toledo_8217 import WeightReading
    from odoo_erpnet_fp.server.routes.scales import _weight_event_data

    data = _weight_event_data(
        "ohaus1", ScaleConfig(id="ohaus1", driver="ohaus_ranger"),
        WeightReading(ok=False, weight_kg=None,
                      status=["Scale unstable"], raw=b""))
    assert data["stable"] is False
    assert data["weight"] is None


def test_weight_event_is_emitted_with_the_right_type(monkeypatch):
    from odoo_erpnet_fp.server.routes import scales as sc

    bus = _RecordingBus()
    monkeypatch.setattr(sc, "_bus_client", lambda request: bus)
    sc._emit_weight(None, "ohaus1", ScaleConfig(id="ohaus1", driver="cas"),
                    {"weight": 1.0, "unit": "kg", "stable": True})

    (event_type, device, kind, data), = bus.calls
    assert event_type == "scale.weighed"
    assert (device, kind) == ("ohaus1", "scale")


def test_emit_does_not_block_the_event_loop(monkeypatch):
    """🚨 `BusInjectClient.emit` е синхронен httpx. Извикан направо в async
    маршрут, той блокира event loop-а на цялото прокси. Измерено на живо:
    мерене от ~200 ms стана 3158 ms, с достъпа и CFX-а спрели зад него."""
    import asyncio
    import time

    from odoo_erpnet_fp.server.routes import scales as sc

    started = []

    class _SlowBus:
        def emit(self, *a, **kw):
            started.append(time.monotonic())
            time.sleep(0.3)

    monkeypatch.setattr(sc, "_bus_client", lambda request: _SlowBus())
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace()))

    async def scenario():
        t0 = time.monotonic()
        sc._schedule_emit(request, "ohaus1",
                          ScaleConfig(id="ohaus1", driver="cas"),
                          {"weight": 1.0, "unit": "kg", "stable": True})
        elapsed = time.monotonic() - t0
        # Задачата се пуска настрани — планирането не чака бавния HTTP.
        assert elapsed < 0.1, f"планирането отне {elapsed:.3f}s"
        tasks = request.app.state.scale_emit_tasks
        assert len(tasks) == 1
        await asyncio.gather(*list(tasks))

    asyncio.run(scenario())
    assert len(started) == 1


def test_scheduling_outside_a_loop_still_emits(monkeypatch):
    # Тестове и синхронни повиквания нямат цикъл — тогава се върши направо,
    # вместо да се губи мълчаливо.
    from odoo_erpnet_fp.server.routes import scales as sc

    bus = _RecordingBus()
    monkeypatch.setattr(sc, "_bus_client", lambda request: bus)
    sc._schedule_emit(None, "ohaus1", ScaleConfig(id="ohaus1", driver="cas"),
                      {"weight": 1.0, "unit": "kg", "stable": True})
    assert len(bus.calls) == 1


def test_a_dead_bus_never_breaks_the_weighing(monkeypatch):
    """Меренето е първичното. Ако каналът към Odoo е паднал, операторът
    пак трябва да получи теглото си."""
    from odoo_erpnet_fp.server.routes import scales as sc

    monkeypatch.setattr(sc, "_bus_client",
                        lambda request: _RecordingBus(boom=True))
    sc._emit_weight(None, "ohaus1", ScaleConfig(id="ohaus1", driver="cas"),
                    {"weight": 1.0, "unit": "kg", "stable": True})


def test_info_response_for_a_serial_scale():
    from odoo_erpnet_fp.server.routes.scales import _info

    resp = _info("cas1", ScaleConfig(
        id="cas1", driver="cas", port="/dev/ttyUSB1"))
    assert resp.port == "/dev/ttyUSB1"
    assert resp.host is None


def test_healthz_reports_the_live_registry_not_the_startup_one():
    """🚨 `/healthz` четеше closure променливата, а `hot_reload_ac_fragment`
    ПОДМЕНЯ обекта в `app.state`. Резултат: след push на конфигурация
    наблюдението виждаше прокси без устройства, докато /scales, /access и
    /cfx/status връщаха вярното."""
    from fastapi.testclient import TestClient

    from odoo_erpnet_fp.config.loader import AppConfig, ServerConfig
    from odoo_erpnet_fp.server.main import create_app

    app = create_app(AppConfig(server=ServerConfig()))
    client = TestClient(app)
    assert client.get("/healthz").json()["scales"] == []

    # Точно каквото прави горещото презареждане.
    app.state.scale_registry = ScaleRegistry.from_config(_config(ScaleConfig(
        id="ohaus1", driver="ohaus_ranger",
        transport="network", host="192.168.3.162", port=9761,
    )))

    assert client.get("/healthz").json()["scales"] == ["ohaus1"]
    assert client.get("/server/info").json()["dynamic"]["scales"] == ["ohaus1"]


def test_fragment_overrides_an_inline_scales_section(tmp_path: Path):
    # Фрагментът е по-силен от вписаното в основния файл — иначе push от
    # Odoo не би могъл да замени ръчно въведена везна.
    (tmp_path / "config.yaml").write_text(
        "server:\n  host: 0.0.0.0\n  port: 8001\n"
        "scales:\n  - id: old\n    driver: cas\n    port: /dev/ttyUSB1\n",
        encoding="utf-8")
    _write_fragment_atomic(
        tmp_path / "config.d" / "scales.yaml", "scales", ODOO_PAYLOAD)

    cfg = load_config(tmp_path / "config.yaml")
    assert [s.id for s in cfg.scales] == ["ohaus_ranger_count_3000"]
