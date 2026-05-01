"""
Reader driver + bus tests — pure-Python (no real device).
"""

import asyncio

import pytest

from odoo_erpnet_fp.drivers.readers.common import BarcodeReader, BarcodeScan
from odoo_erpnet_fp.drivers.readers.hid import (
    SCANCODE_MAP,
    SCANCODE_SHIFT_MAP,
)
from odoo_erpnet_fp.server.reader_bus import ReaderEventBus


# ─── HID scancode table ──────────────────────────────────────────


def test_scancode_map_covers_digits():
    assert SCANCODE_MAP[2] == "1"
    assert SCANCODE_MAP[11] == "0"


def test_scancode_map_covers_letters():
    assert SCANCODE_MAP[16] == "q"
    assert SCANCODE_MAP[44] == "z"


def test_shift_overlay():
    # 1 → !, 0 → )
    assert SCANCODE_SHIFT_MAP["1"] == "!"
    assert SCANCODE_SHIFT_MAP["0"] == ")"
    # Letters fall through to .upper() at runtime — not in the map


# ─── BarcodeScan serialization ───────────────────────────────────


def test_barcode_scan_to_json():
    s = BarcodeScan(reader_id="scan1", barcode="3800013116159")
    j = s.to_json()
    assert j["readerId"] == "scan1"
    assert j["barcode"] == "3800013116159"
    assert "timestamp" in j


# ─── ReaderEventBus pub/sub ──────────────────────────────────────


@pytest.mark.asyncio
async def test_bus_delivers_to_subscriber():
    loop = asyncio.get_running_loop()
    bus = ReaderEventBus(reader_id="r1", loop=loop)
    q = bus.subscribe()
    bus.publish_threadsafe(BarcodeScan(reader_id="r1", barcode="ABC123"))

    # Allow the run_coroutine_threadsafe to fire
    scan = await asyncio.wait_for(q.get(), timeout=1.0)
    assert scan.barcode == "ABC123"

    # History also captures it
    last = bus.last_scan()
    assert last is not None and last.barcode == "ABC123"


@pytest.mark.asyncio
async def test_bus_unsubscribe():
    loop = asyncio.get_running_loop()
    bus = ReaderEventBus(reader_id="r1", loop=loop)
    q = bus.subscribe()
    assert bus.subscriber_count == 1
    bus.unsubscribe(q)
    assert bus.subscriber_count == 0


@pytest.mark.asyncio
async def test_bus_history_limited():
    loop = asyncio.get_running_loop()
    bus = ReaderEventBus(reader_id="r1", loop=loop)
    bus._history.maxlen  # access the configured size
    for i in range(50):
        bus._history.append(BarcodeScan(reader_id="r1", barcode=f"B{i}"))
    assert len(bus._history) <= ReaderEventBus.HISTORY_SIZE


@pytest.mark.asyncio
async def test_bus_multiple_subscribers_get_same_scan():
    loop = asyncio.get_running_loop()
    bus = ReaderEventBus(reader_id="r1", loop=loop)
    q1, q2 = bus.subscribe(), bus.subscribe()
    bus.publish_threadsafe(BarcodeScan(reader_id="r1", barcode="X"))

    scan_a = await asyncio.wait_for(q1.get(), timeout=1.0)
    scan_b = await asyncio.wait_for(q2.get(), timeout=1.0)
    assert scan_a.barcode == "X"
    assert scan_b.barcode == "X"
