"""
OHAUS Ranger 3000 / Count 3000 / Valor 7000 protocol parser tests.

Live tests (against a real scale at e.g. 192.168.3.162:9761) live
elsewhere — these run offline against synthetic byte buffers.
"""

import pytest

from odoo_erpnet_fp.drivers.scales.ohaus_ranger import (
    OhausRangerScale,
    DEFAULT_TCP_PORT,
    _convert_to_kg,
)


def parse(line: bytes):
    return OhausRangerScale._parse_line(line)


# ─── unit conversion ────────────────────────────────────────────


def test_unit_conversion_kg():
    assert _convert_to_kg(1.234, "kg") == pytest.approx(1.234)


def test_unit_conversion_g():
    assert _convert_to_kg(1234.0, "g") == pytest.approx(1.234)


def test_unit_conversion_ton():
    assert _convert_to_kg(1.5, "t") == pytest.approx(1500.0)


def test_unit_conversion_lb():
    assert _convert_to_kg(1.0, "lb") == pytest.approx(0.45359237)


def test_unit_conversion_oz():
    assert _convert_to_kg(16.0, "oz") == pytest.approx(0.45359237, rel=1e-6)


# ─── endpoint parsing ──────────────────────────────────────────


def test_endpoint_default_port():
    host, port = OhausRangerScale._split_endpoint("192.168.3.162")
    assert host == "192.168.3.162"
    assert port == DEFAULT_TCP_PORT == 9761


def test_endpoint_explicit_port():
    host, port = OhausRangerScale._split_endpoint("192.168.3.162:9761")
    assert host == "192.168.3.162"
    assert port == 9761


def test_endpoint_custom_port():
    host, port = OhausRangerScale._split_endpoint("scale.local:8888")
    assert host == "scale.local"
    assert port == 8888


# ─── parse: stable readings ────────────────────────────────────


def test_parse_stable_kg_padded():
    # 9-char weight, space, "kg   " unit (padded), space, blank=stable, G
    r = parse(b"    1.234 kg     G")
    assert r.ok is True
    assert r.weight_kg == pytest.approx(1.234)


def test_parse_stable_grams():
    r = parse(b" 1234.000 g      G")
    assert r.ok is True
    assert r.weight_kg == pytest.approx(1.234)


def test_parse_stable_zero():
    r = parse(b"    0.000 kg     G")
    assert r.ok is True
    assert r.weight_kg == pytest.approx(0.0)


def test_parse_stable_net():
    r = parse(b"    2.500 kg     N")
    assert r.ok is True
    assert r.weight_kg == pytest.approx(2.500)


def test_parse_stable_negative_g():
    r = parse(b"  -125.0 g       G")
    assert r.ok is True
    assert r.weight_kg == pytest.approx(-0.125)


# ─── parse: unstable ───────────────────────────────────────────


def test_parse_unstable_marked():
    r = parse(b"    1.234 kg    ?G")
    assert r.ok is False
    assert r.status == ["Scale unstable"]


def test_parse_unstable_with_question_separated():
    r = parse(b"    0.500 kg    ?N")
    assert r.ok is False


# ─── parse: lb:oz format ───────────────────────────────────────


def test_parse_lb_oz_stable():
    # 1 lb 0 oz exactly = 0.45359237 kg
    r = parse(b"  1 lb:0.0 oz   G")
    assert r.ok is True
    assert r.weight_kg == pytest.approx(0.45359237, rel=1e-5)


def test_parse_lb_oz_unstable():
    r = parse(b"  3 lb:5.6 oz  ?G")
    assert r.ok is False
    assert r.status == ["Scale unstable"]


# ─── parse: edge cases ─────────────────────────────────────────


def test_parse_empty_line():
    r = parse(b"")
    assert r.ok is False
    assert "No data" in r.status[0]


def test_parse_garbage():
    r = parse(b"\x00\xff random binary noise")
    assert r.ok is False
    assert any("Unparseable" in s for s in r.status)


def test_parse_only_whitespace():
    r = parse(b"        \r\n")
    assert r.ok is False
