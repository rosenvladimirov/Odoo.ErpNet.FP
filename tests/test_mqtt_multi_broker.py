# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.

"""R5 — multi-broker MQTT contract tests.

Covers the YAML loader's accepted shapes and the registry's granular
diff/reload semantics. NO paho-mqtt is imported (registry never starts
brokers in these tests; we only build / diff specs).
"""

from odoo_erpnet_fp.config.loader import (
    AppConfig,
    MqttBrokerSpec,
    _yaml_to_app_config,
)
from odoo_erpnet_fp.server.service import (
    MqttIngestRegistry,
    _broker_spec_dirty,
)


def test_loader_legacy_dict_promotes_to_list_of_one():
    """Pre-R5 single-broker `mqtt:` dict still works; name defaults to
    `default` and the legacy `.mqtt_ingest` accessor returns it."""
    cfg = _yaml_to_app_config({
        "mqtt": {"enabled": True, "host": "old.local", "topics": ["lpr"]},
    })
    assert len(cfg.mqtt_brokers) == 1
    only = cfg.mqtt_brokers[0]
    assert only.name == "default"
    assert only.host == "old.local"
    assert only.topics == ["lpr"]
    # Legacy accessor returns the first enabled broker.
    assert cfg.mqtt_ingest.host == "old.local"


def test_loader_list_shape_preserves_names():
    """The new list shape preserves explicit names and per-broker fields."""
    cfg = _yaml_to_app_config({
        "mqtt": [
            {"name": "parking", "host": "p.local"},
            {"name": "warehouse", "host": "w.local", "enabled": False},
        ],
    })
    assert [b.name for b in cfg.mqtt_brokers] == ["parking", "warehouse"]
    assert cfg.mqtt_brokers[0].enabled is True   # default
    assert cfg.mqtt_brokers[1].enabled is False
    # Legacy accessor returns the FIRST enabled broker.
    assert cfg.mqtt_ingest.name == "parking"


def test_loader_dedupes_duplicate_names():
    """Two brokers with the same `name` get auto-suffixed; the registry
    contract depends on unique keys."""
    cfg = _yaml_to_app_config({
        "mqtt": [
            {"name": "x", "host": "a"},
            {"name": "x", "host": "b"},
            {"name": "x", "host": "c"},
        ],
    })
    assert [b.name for b in cfg.mqtt_brokers] == ["x", "x-2", "x-3"]


def test_loader_empty_or_absent_yields_no_brokers():
    """No `mqtt:` block at all (or empty list) ⇒ no brokers and paho-mqtt
    is never touched. The legacy accessor returns a disabled placeholder."""
    assert _yaml_to_app_config({}).mqtt_brokers == []
    assert _yaml_to_app_config({"mqtt": []}).mqtt_brokers == []
    cfg = _yaml_to_app_config({})
    assert cfg.mqtt_ingest.enabled is False  # safe placeholder


def test_registry_diff_added_restarted_unchanged():
    """Granular reload returns a structured diff so callers can show
    operators exactly what changed broker-by-broker."""
    cfg1 = _yaml_to_app_config({"mqtt": [
        {"name": "a", "host": "a.local"},
        {"name": "b", "host": "b.local"},
    ]})
    reg = MqttIngestRegistry.from_config(cfg1)
    assert set(reg.specs) == {"a", "b"}

    cfg2 = _yaml_to_app_config({"mqtt": [
        {"name": "a", "host": "a.local"},      # unchanged
        {"name": "b", "host": "b2.local"},     # changed → restart
        {"name": "c", "host": "c.local"},      # new → start
    ]})
    diff = reg.reload_from_config(cfg2)
    assert diff["unchanged"] == ["a"]
    assert diff["restarted"] == ["b"]
    assert diff["added"] == ["c"]
    assert diff["removed"] == []


def test_registry_diff_removed_and_disabled():
    """Removing a broker entirely and disabling another both end up
    in the right diff buckets."""
    cfg1 = _yaml_to_app_config({"mqtt": [
        {"name": "a", "host": "a.local"},
        {"name": "b", "host": "b.local"},
        {"name": "c", "host": "c.local"},
    ]})
    reg = MqttIngestRegistry.from_config(cfg1)

    cfg2 = _yaml_to_app_config({"mqtt": [
        {"name": "a", "host": "a.local"},                         # unchanged
        {"name": "b", "host": "b.local", "enabled": False},       # disabled
        # c removed
    ]})
    diff = reg.reload_from_config(cfg2)
    assert diff["unchanged"] == ["a"]
    assert "b" in diff["restarted"]
    assert diff["removed"] == ["c"]


def test_broker_spec_dirty_matches_relevant_fields():
    """`_broker_spec_dirty` triggers a reconnect only when something
    meaningful actually changed — name is the key so it's ignored;
    topics are list-compared."""
    a = MqttBrokerSpec(name="x", host="h", topics=["t1"])
    b = MqttBrokerSpec(name="x", host="h", topics=["t1"])
    assert _broker_spec_dirty(a, b) is False
    b2 = MqttBrokerSpec(name="x", host="h2", topics=["t1"])  # host changed
    assert _broker_spec_dirty(a, b2) is True
    b3 = MqttBrokerSpec(name="x", host="h", topics=["t1", "t2"])  # topics changed
    assert _broker_spec_dirty(a, b3) is True
