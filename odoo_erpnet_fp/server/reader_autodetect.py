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
    "fff0": {  # Datecs — pinpads (BluePad/BlueCash, e.g. fff0:0100
        # "DATECS PinPad") and fiscal devices enumerate as CDC-ACM on
        # the SAME VID as barcode scanners. They are NOT readers — the
        # pinpad is driven by drivers/pinpad/datecs_pay and fiscal
        # devices by explicit `printers:` config. Skip so the reader
        # autodetect never grabs /dev/ttyACM* belonging to a Datecs
        # pinpad/printer and mis-registers it as a barcode scanner.
        "vendor_name": "datecs",
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


def _prefer_stable_symlink(devnode: str) -> str:
    """Връща най-добрия наличен стабилен path към устройството.

    Сканираме `/dev/` за symlink-ове сочещи към същия char device. Предпочитаме:
      1) Кратки custom-named symlink-ове в `/dev/` (напр. `/dev/honeywell_scanner`
         от нашите 99-erpnet-fp.rules — оцелява power-cycle/re-enumeration).
      2) `/dev/serial/by-id/usb-<vendor>_<product>_<serial>-*` (винаги stable).
      3) Raw devnode (`/dev/ttyACM0`) като fallback.

    БЕЛЕЖКА: `pyudev.device_links` не работи вътре в Docker контейнер (udev DB
    е на хоста, не се mount-ва). Затова правим чист OS-ниво scan с `os.readlink`,
    което работи навсякъде.
    """
    basename = os.path.basename(devnode)
    candidates_short: list[str] = []
    candidates_by_id: list[str] = []

    def _resolves_to(link: str) -> bool:
        try:
            # Резолва веригата от symlinks докрай и сравнява с devnode.
            return os.path.realpath(link) == devnode
        except OSError:
            return False

    # Кратки symlinks директно в /dev (custom rules)
    try:
        for entry in os.listdir("/dev"):
            full = f"/dev/{entry}"
            if entry == basename:
                continue
            if os.path.islink(full) and _resolves_to(full):
                candidates_short.append(full)
    except OSError:
        pass

    # /dev/serial/by-id/* (винаги генерирани от udev по vendor+product+serial)
    try:
        by_id_dir = "/dev/serial/by-id"
        if os.path.isdir(by_id_dir):
            for entry in os.listdir(by_id_dir):
                full = f"{by_id_dir}/{entry}"
                if os.path.islink(full) and _resolves_to(full):
                    candidates_by_id.append(full)
    except OSError:
        pass

    # Предпочитаме по-СПЕЦИФИЧНИЯ symlink (по-дълъг → най-вероятно съдържа
    # serial suffix `_<serial>`), за да оцеляваме multi-device инсталации.
    # В single-device case това дава `/dev/datecs_pinpad_<serial>` (по-дълго
    # но винаги уникално) пред генеричния `/dev/datecs_pinpad` (който при
    # 2+ устройства би сочил към случайно от тях).
    if candidates_short:
        return sorted(candidates_short, key=lambda p: (-len(p), p))[0]
    if candidates_by_id:
        return sorted(candidates_by_id, key=lambda p: (-len(p), p))[0]
    return devnode


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

    # Предпочитаме стабилен symlink ако udev е създал такъв (напр. /dev/
    # honeywell_scanner от 99-erpnet-fp.rules). Така UI-ът и логовете показват
    # smysleno име, не разменчивия /dev/ttyACM*. Резолва се обратно до същия
    # char device, така че комуникацията не се променя.
    preferred_port = _prefer_stable_symlink(devnode)

    return ReaderConfig(
        id=reader_id,
        transport="serial",
        port=preferred_port,
        baudrate=int(spec.get("baudrate", 9600)),
        encoding=spec.get("encoding", "ascii"),
        # USB descriptor info — surfaced read-only on /readers + dashboard
        # so the operator can see WHAT scanner is attached without probing
        # the device (CDC-ACM scanners don't expose a query command).
        extras={"usb": {
            "vid": vendor_info.get("vid") or None,
            "pid": vendor_info.get("pid") or None,
            "serial": vendor_info.get("serial") or None,
            "product": vendor_info.get("product") or None,
            "manufacturer": vendor_info.get("manufacturer") or None,
            "vendorName": spec.get("vendor_name") or None,
        }},
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
    # Try to resolve the device through udev so we can pick vendor-
    # specific defaults. If it can't be resolved (typical for pseudo
    # ttys like /dev/ttyV0 → /dev/pts/N from hid2serial — they live
    # under devpts, not tty), fall back to generic CDC defaults.
    vendor_info: dict = {}
    try:
        ctx = pyudev.Context()
        device = pyudev.Devices.from_device_file(ctx, devnode)
        vendor_info = _vendor_info_for(device)
    except Exception:
        _logger.debug(
            "autodetect: %s not in udev (likely pseudo-tty) — "
            "registering with generic defaults", devnode,
        )
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
    # Two passes:
    #   1. pyudev tty subsystem — covers physical CDC ACM (ttyACM*).
    #   2. Direct glob — pyudev's list_devices does NOT enumerate
    #      pseudo-ttys (e.g. /dev/ttyV0 → /dev/pts/N from hid2serial)
    #      because they live under devpts, not tty. The glob picks
    #      them up; _register_one's `_devnode_in_use` check de-dupes
    #      anything pass 1 already added.
    import glob
    ctx = pyudev.Context()
    seen_devnodes: set[str] = set()
    for device in ctx.list_devices(subsystem="tty"):
        devnode = device.device_node or ""
        if not _is_tty_of_interest(devnode):
            continue
        seen_devnodes.add(devnode)
        await _register_one(devnode, app)
    for devnode in sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyV*")):
        if devnode in seen_devnodes:
            continue
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
