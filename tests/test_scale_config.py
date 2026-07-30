"""
Odoo → proxy seam for scales: endpoint assembly and fail-soft registry.

Мрежова везна никога не е минавала по този път, затова шевът си беше
скъсан мълчаливо: Odoo подаваше `host` + число `port`, а конфигурацията
четеше само `port`. Тези тестове заковават договора в двете посоки.
"""

from types import SimpleNamespace

import pytest

from odoo_erpnet_fp.config.loader import ScaleConfig
from odoo_erpnet_fp.server.service import ScaleRegistry


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
