"""
Polimex WebSDK event-stream ingestion → reader bus.

`resolve_reader` is pure (fake reader_registry). Canonical payload is
the one from the Polimex reference `simulate_event.py`.
"""

import yaml

from odoo_erpnet_fp.config.loader import _yaml_to_app_config
from odoo_erpnet_fp.server.main import create_app
from odoo_erpnet_fp.server.routes.polimex_events import resolve_reader

_PAYLOAD = {
    "convertor": 414468, "key": "7411",
    "event": {"bos": 1, "card": "1786802811", "cmd": "FA",
              "date": "05.06.25", "day": 2,
              "dt": "000000000000000000000004", "err": 0,
              "event_n": 3, "id": 40, "reader": 1,
              "time": "15:40:55", "tos": 1},
}


class _Cfg:
    def __init__(self, transport, extras):
        self.transport = transport
        self.extras = extras


class _Entry:
    def __init__(self, transport, extras):
        self.config = _Cfg(transport, extras)


class _Reg:
    def __init__(self, readers):
        self.readers = readers


def test_resolve_exact_match():
    reg = _Reg({
        "gate_card": _Entry("external", {
            "polimex": {"convertor": 414468, "controller_id": 40,
                        "reader": 1}}),
        "other": _Entry("external", {
            "polimex": {"controller_id": 99}}),
    })
    rid, card = resolve_reader(reg, _PAYLOAD)
    assert rid == "gate_card" and card == "1786802811"


def test_resolve_wildcard_subset():
    # only controller_id specified → reader/convertor are wildcards
    reg = _Reg({"r": _Entry("external", {"polimex": {"controller_id": 40}})})
    assert resolve_reader(reg, _PAYLOAD)[0] == "r"


def test_resolve_key_mismatch_excluded():
    reg = _Reg({"r": _Entry("external",
                            {"polimex": {"key": "9999"}})})
    assert resolve_reader(reg, _PAYLOAD)[0] is None


def test_resolve_non_external_ignored():
    reg = _Reg({"r": _Entry("hid", {"polimex": {"controller_id": 40}})})
    assert resolve_reader(reg, _PAYLOAD)[0] is None


def test_resolve_zero_card_skipped():
    p = {"convertor": 414468, "event": {"card": "0000000000",
                                        "id": 40, "reader": 1}}
    reg = _Reg({"r": _Entry("external", {"polimex": {}})})
    rid, card = resolve_reader(reg, p)
    assert rid is None  # not a card read


def test_routes_registered_and_reader_extras_parsed():
    cfg = _yaml_to_app_config(yaml.safe_load("""
readers:
  - id: gate_card
    transport: external
    extras: { polimex: { convertor: 414468, controller_id: 40, reader: 1 } }
"""))
    # ReaderConfig.extras carries the polimex map untouched
    assert cfg.readers[0].extras["polimex"]["controller_id"] == 40
    app = create_app(cfg)
    paths = {r.path for r in app.routes if getattr(r, "path", "")}
    assert "/polimex/event" in paths and "/hr/rfid/event" in paths
    # the external reader is resolvable from the canonical payload
    rid, card = resolve_reader(app.state.reader_registry, _PAYLOAD)
    assert rid == "gate_card" and card == "1786802811"
