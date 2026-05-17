"""
Channel-1 access-control: an access-flagged reader publishes to BOTH
the standard `reader.<id>` native-IoT identifier (tablet/POS — never
blocked) AND a dedicated `hac.card/<id>` channel (consumed by
hr_attendance_access_control, independent waiter queue → zero
competition with POS). A non-flagged reader pushes only `reader.<id>`.
"""

import asyncio

import yaml

from odoo_erpnet_fp.config.loader import _yaml_to_app_config
from odoo_erpnet_fp.drivers.readers.common import BarcodeScan
from odoo_erpnet_fp.server.reader_bus import ReaderEventBus
from odoo_erpnet_fp.server.routes.iot_compat import get_iot_sessions


def _publish(bus, scan):
    loop = asyncio.new_event_loop()
    try:
        bus._loop = loop
        loop.run_until_complete(bus._publish(scan))
    finally:
        loop.close()


def test_access_flag_dual_push_does_not_block_standard():
    s = get_iot_sessions()
    s._pending.clear()
    b = ReaderEventBus("gate1", access_control=True)
    _publish(b, BarcodeScan(reader_id="gate1", barcode="1786802811"))
    keys = set(s._pending)
    # standard POS identifier ALWAYS present (not replaced/blocked)
    assert "reader.gate1" in keys
    # dedicated access-control channel also present
    assert "hac.card/gate1" in keys
    hac = s._pending["hac.card/gate1"]
    assert hac["card"] == "1786802811" and hac["reader_id"] == "gate1"
    assert hac["result"] == "1786802811"  # same scan value as standard


def test_no_flag_only_standard_channel():
    s = get_iot_sessions()
    s._pending.clear()
    b = ReaderEventBus("gate2", access_control=False)
    _publish(b, BarcodeScan(reader_id="gate2", barcode="X"))
    keys = set(s._pending)
    assert "reader.gate2" in keys
    assert "hac.card/gate2" not in keys  # opt-in only


def test_reader_access_control_config_parsed():
    cfg = _yaml_to_app_config(yaml.safe_load("""
readers:
  - { id: r1, transport: external, access_control: true }
  - { id: r2, transport: hid, device_path: /dev/input/event5 }
"""))
    by = {r.id: r for r in cfg.readers}
    assert by["r1"].access_control is True
    assert by["r2"].access_control is False  # default
