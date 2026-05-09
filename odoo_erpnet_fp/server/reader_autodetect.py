"""
Hot-plug autodetection of CDC ACM barcode scanners (and pseudo-tty
readers from hid2serial) via pyudev.

Why:
    Manually maintaining `readers:` blocks in config.yaml is tedious
    and error-prone — operator forgets to update it when scanners are
    swapped, and proxy starts up with the wrong port mapping. Linux
    already exposes USB hot-plug + pty creation events through
    netlink/udev; a small async listener lets the proxy register and
    unregister readers in lockstep with the kernel's view.

Strategy:
    On startup (when `auto_detect: true`), enumerate /dev/ttyACM* +
    /dev/ttyV* present, register a dynamic reader for each. Then
    listen for udev "add"/"remove" events on the `tty` subsystem and
    keep the registry in sync.

    Vendor matching is done from the USB device descriptor walked up
    from the tty device. Known scanner vendors get vendor-specific
    defaults (baudrate, terminator); unknown vendors that present a
    CDC ACM `Communications` interface get generic 9600/8N1 defaults
    that work for the vast majority of Bulgarian-market scanners.

    Manual `readers:` entries in config.yaml take precedence — if an
    auto-detected tty matches a manually-configured reader's port,
    the auto-detect skips it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Optional

try:
    import pyudev  # type: ignore
except ImportError:
    pyudev = None  # type: ignore

from ..config.loader import ReaderConfig

_logger = logging.getLogger(__name__)


# Known scanner vendors → (driver hint, baudrate, encoding). Add new
# entries as you encounter them in the field. The shape mirrors what
# ReaderConfig accepts so a vendor-pinned driver picks up the right
# defaults without operator intervention. Keys are lowercase hex
# strings (USB VID).
_KNOWN_SCANNER_VENDORS: dict[str, dict] = {
    "0c2e": {  # Metrologic / Honeywell (incl. Voyager 1470g)
        "vendor_name": "honeywell",
        "baudrate": 9600,
        "encoding": "ascii",
    },
    "0536": {  # Hand Held Products (Honeywell legacy)
        "vendor_name": "honeywell",
        "baudrate": 9600,
        "encoding": "ascii",
    },
    "1a86": {  # QinHeng (CH340/CH341 — cheap CN scanners)
        "vendor_name": "qinheng",
        "baudrate": 9600,
        "encoding": "ascii",
    },
    "0483": {  # STM-based generic scanners
        "vendor_name": "stm",
        "baudrate": 115200,
        "encoding": "ascii",
    },
    "05f9": {  # Symbol / Zebra — HID-only on Linux, see
        # feedback_symbol_zebra_linux_no_serial. Skip CDC-ACM path —
        # the device shouldn't even show up here. Listed so a future
        # operator that does see it knows we recognised it.
        "vendor_name": "symbol",
        "skip": True,
    },
}


def _walk_to_usb(device) -> Optional[object]:
    """Walk the udev parent chain to find the closest USB device. ttyACM*
    devices have an `usb` ancestor with idVendor / idProduct attrs."""
    parent = device
    while parent is not None:
        if parent.subsystem == "usb" and parent.device_type == "usb_device":
            return parent
        parent = parent.parent
    return None


def _vendor_info_for(device) -> dict:
    """Return vendor info dict (vid, pid, name, serial). Empty when the
    tty has no USB ancestor (e.g. /dev/ttyV0 from hid2serial — pseudo-tty
    backed by evdev, no USB metadata)."""
    usb = _walk_to_usb(device)
    if usb is None:
        return {}
    vid = (usb.attributes.get("idVendor") or b"").decode("ascii").lower()
    pid = (usb.attributes.get("idProduct") or b"").decode("ascii").lower()
    serial = (usb.attributes.get("serial") or b"").decode("ascii")
    product = (usb.attributes.get("product") or b"").decode("ascii")
    manufacturer = (usb.attributes.get("manufacturer") or b"").decode("ascii")
    return {
        "vid": vid,
        "pid": pid,
        "serial": serial,
        "product": product,
        "manufacturer": manufacturer,
    }


def _build_reader_config(devnode: str, vendor_info: dict) -> Optional[ReaderConfig]:
    """Translate a tty device + USB descriptor into a ReaderConfig.

    Returns None when we should NOT register this device (e.g. known
    HID-only vendors that happen to expose a CDC ACM interface
    transiently, or generic ttys that don't match any scanner shape).
    """
    vid = vendor_info.get("vid", "")
    spec = _KNOWN_SCANNER_VENDORS.get(vid, {})
    if spec.get("skip"):
        _logger.info(
            "autodetect: skipping %s — vendor %s flagged as HID-only",
            devnode, spec.get("vendor_name", "?"),
        )
        return None

    # Reader id: stable + identifiable. Vendor + serial when we have
    # them; fall back to the tty basename for pseudo-ttys and unknown
    # USB.
    if vendor_info.get("serial") and vid:
        vendor_name = spec.get("vendor_name") or vid
        reader_id = f"auto-{vendor_name}-{vendor_info['serial']}"
    else:
        # /dev/ttyACM0 → auto-ttyACM0; /dev/ttyV0 → auto-ttyV0
        reader_id = f"auto-{os.path.basename(devnode)}"
    # Slugify defensively — reader ids land in URLs and Odoo identifiers.
    reader_id = re.sub(r"[^a-z0-9_-]+", "-", reader_id.lower()).strip("-")

    return ReaderConfig(
        id=reader_id,
        transport="serial",
        port=devnode,
        baudrate=int(spec.get("baudrate", 9600)),
        encoding=spec.get("encoding", "ascii"),
    )


def _devnode_in_use(devnode: str, app) -> bool:
    """Don't autodetect a tty that an explicit `readers:` config entry
    already owns — the operator's manual settings (baudrate overrides,
    custom encoding, etc.) should win."""
    reg = getattr(app.state, "reader_registry", None)
    if reg is None:
        return False
    for entry in reg.readers.values():
        if entry.config.port == devnode:
            return True
    return False


async def _register_one(devnode: str, app) -> None:
    """Build + add a dynamic reader for a single tty devnode. Logs on
    skip/failure; never raises."""
    if pyudev is None:
        return
    if _devnode_in_use(devnode, app):
        _logger.debug("autodetect: %s already configured manually — skipping",
                      devnode)
        return
    try:
        ctx = pyudev.Context()
        device = pyudev.Devices.from_device_file(ctx, devnode)
    except Exception:
        _logger.exception("autodetect: cannot resolve %s in udev", devnode)
        return
    vendor_info = _vendor_info_for(device)
    cfg = _build_reader_config(devnode, vendor_info)
    if cfg is None:
        return
    reg = getattr(app.state, "reader_registry", None)
    if reg is None:
        return
    ok = await reg.add_dynamic(cfg)
    if ok:
        _logger.info(
            "autodetect: added reader %r (vendor=%s/%s product=%r)",
            cfg.id, vendor_info.get("vid") or "?",
            vendor_info.get("pid") or "?",
            vendor_info.get("product") or "?",
        )


async def _unregister_for(devnode: str, app) -> None:
    """Remove the dynamic reader bound to a removed tty devnode.

    Static (manually-configured) readers are NOT touched — the operator
    chose them, the operator removes them.
    """
    reg = getattr(app.state, "reader_registry", None)
    if reg is None:
        return
    target_id: Optional[str] = None
    for rid, entry in list(reg.readers.items()):
        if entry.config.port == devnode and rid.startswith("auto-"):
            target_id = rid
            break
    if target_id:
        await reg.remove_dynamic(target_id)


async def autodetect_loop(app) -> None:
    """Background task — initial sweep + udev event listener.

    Runs only if `pyudev` imports cleanly (Linux only) and the config
    has `auto_detect: true`. Otherwise it returns immediately so the
    lifespan owner can keep the task object semantics simple.
    """
    if pyudev is None:
        _logger.warning(
            "autodetect: pyudev not installed — autodetect disabled. "
            "Install 'pyudev>=0.24' or set auto_detect: false to silence."
        )
        return

    # ─── Initial sweep — register what's already plugged in. ──────
    ctx = pyudev.Context()
    for device in ctx.list_devices(subsystem="tty"):
        devnode = device.device_node or ""
        if not _is_tty_of_interest(devnode):
            continue
        await _register_one(devnode, app)

    # ─── Listen for hot-plug events forever. ──────────────────────
    monitor = pyudev.Monitor.from_netlink(ctx)
    monitor.filter_by(subsystem="tty")

    loop = asyncio.get_running_loop()

    def _next_event_blocking():
        # pyudev's Monitor.poll(timeout) is blocking — run it in a
        # thread so we don't stall the event loop.
        return monitor.poll(timeout=1.0)

    _logger.info(
        "autodetect: udev listener started (subsystem=tty, "
        "currently registered=%d)",
        len(getattr(app.state.reader_registry, "readers", {})),
    )

    while True:
        try:
            device = await loop.run_in_executor(None, _next_event_blocking)
        except asyncio.CancelledError:
            _logger.info("autodetect: cancelled")
            raise
        if device is None:
            continue
        devnode = device.device_node or ""
        if not _is_tty_of_interest(devnode):
            continue
        action = device.action
        try:
            if action == "add":
                # Wait briefly — udev fires `add` before the device
                # node is fully ready for open(). 200ms is enough for
                # CDC ACM enumeration to settle.
                await asyncio.sleep(0.2)
                await _register_one(devnode, app)
            elif action == "remove":
                await _unregister_for(devnode, app)
        except Exception:
            _logger.exception("autodetect: handler failed on %s %s",
                              action, devnode)


def _is_tty_of_interest(devnode: str) -> bool:
    """True for ttyACM* (CDC ACM) and ttyV* (hid2serial pseudo-ttys)."""
    if not devnode.startswith("/dev/"):
        return False
    base = os.path.basename(devnode)
    return base.startswith("ttyACM") or base.startswith("ttyV")
