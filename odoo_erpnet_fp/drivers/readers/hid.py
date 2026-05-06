"""
HID barcode reader (USB keyboard-emulating scanners).

Linux input-event subsystem via `evdev`. The reader grabs exclusive
access to a `/dev/input/eventN` device so its keystrokes do NOT bleed
into the rest of the system, then translates scancodes to characters
using a fixed US-keyboard layout (which all major BG retail scanners —
Symbol/Zebra, Honeywell, Datalogic, Newland, Mindeo — emit by default).

Three integration paths:

  1. **device_path** — explicit path under /dev/input/by-id/usb-...-event-kbd.
     Most stable; survives reboot when the scanner stays in the same USB port.

  2. **vid + pid** — match the first input-event device whose vendor and
     product IDs match the configured pair. Survives port changes.

  3. **name_regex** — pattern match on the device's reported name string
     (e.g. "Honeywell|Datalogic|Symbol"). Useful when VID/PID isn't known
     ahead of time (replacement units, mixed-vendor fleets).

A complete barcode is recognised when the scanner sends a configured
terminator key (default ENTER + KP_ENTER). The string is stripped of
optional prefix/suffix, length-bounded, and emitted via the
BarcodeReader.set_listener callback.
"""

from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass
from typing import Iterable, Optional

try:
    import evdev
except ImportError:  # pragma: no cover
    evdev = None

from .common import BarcodeReader

_logger = logging.getLogger(__name__)


# US-layout scancode → unshifted character (row-by-row).
SCANCODE_MAP: dict[int, str] = {
    2: "1", 3: "2", 4: "3", 5: "4", 6: "5",
    7: "6", 8: "7", 9: "8", 10: "9", 11: "0",
    12: "-", 13: "=",
    16: "q", 17: "w", 18: "e", 19: "r", 20: "t",
    21: "y", 22: "u", 23: "i", 24: "o", 25: "p",
    26: "[", 27: "]",
    30: "a", 31: "s", 32: "d", 33: "f", 34: "g",
    35: "h", 36: "j", 37: "k", 38: "l", 39: ";",
    40: "'", 41: "`",
    43: "\\",
    44: "z", 45: "x", 46: "c", 47: "v", 48: "b",
    49: "n", 50: "m", 51: ",", 52: ".", 53: "/",
    57: " ",
    # Numpad (some scanners use it for digits)
    71: "7", 72: "8", 73: "9",
    75: "4", 76: "5", 77: "6",
    79: "1", 80: "2", 81: "3",
    82: "0", 83: ".",
    98: "/", 55: "*",
    74: "-", 78: "+",
}

# Shift overlay for printable characters
SCANCODE_SHIFT_MAP: dict[str, str] = {
    "1": "!", "2": "@", "3": "#", "4": "$", "5": "%",
    "6": "^", "7": "&", "8": "*", "9": "(", "0": ")",
    "-": "_", "=": "+", "[": "{", "]": "}", ";": ":",
    "'": '"', "`": "~", ",": "<", ".": ">", "/": "?",
    "\\": "|",
}

KEY_ENTER = 28
KEY_KPENTER = 96
KEY_LEFT_SHIFT = 42
KEY_RIGHT_SHIFT = 54
KEY_CAPSLOCK = 58
KEY_TAB = 15

# Default terminator keys — Honeywell, Datalogic, Symbol, Newland all
# default to ENTER. Some shipping setups configure TAB; allow override
# in config.yaml via `terminator` field but keep these as default.
DEFAULT_TERMINATOR_KEYS = frozenset({KEY_ENTER, KEY_KPENTER})


# Vendor presets — for auto-discovery and friendly naming. Keep this
# list short and conservative; users with quirky scanners override
# with `device_path` or `vid`+`pid` directly.
SCANNER_VENDOR_PRESETS = [
    # (vendor_name, regex_on_device_name, [vid, ...] or [] for any)
    ("Honeywell", re.compile(r"honeywell|voyager|xenon|eclipse", re.I), [0x0c2e, 0x0536]),
    ("Datalogic", re.compile(r"datalogic|quickscan|gryphon", re.I), [0x05f9]),
    ("Symbol/Zebra", re.compile(r"symbol|zebra|motorola|ls\d{4}|ds\d{4}", re.I), [0x05e0, 0x0c2e]),
    ("Newland", re.compile(r"newland|nls", re.I), [0x1eab]),
    ("Mindeo", re.compile(r"mindeo|md\d{4}", re.I), [0x2dd6]),
    ("Mertech", re.compile(r"mertech", re.I), []),
    ("Argox", re.compile(r"argox", re.I), [0x1664]),
]


@dataclass
class HidScannerInfo:
    """Result of HidBarcodeReader.discover()."""
    device_path: str
    name: str
    vendor_id: int
    product_id: int
    bustype: int
    vendor_guess: Optional[str] = None  # human-friendly vendor (Honeywell, ...)


class HidBarcodeReader(BarcodeReader):
    """USB-HID barcode scanner driver.

    Args:
        reader_id: short id used in API URLs (`/readers/{id}/...`)
        device_path: path under /dev/input/, e.g. `/dev/input/event5` or
            a /dev/input/by-id/usb-...-event-kbd symlink. Mutually
            exclusive with vid+pid / name_regex auto-resolution; if
            both are passed, device_path wins.
        vid, pid: USB vendor / product IDs (integers). Used to find the
            device automatically at start() if device_path is not set.
        name_regex: alternative auto-match — regex against the device
            name as reported by the kernel.
        grab: exclusive access (default True) so scans don't bleed into
            other applications on the host's UI.
        terminator: bytes-style string interpreted as one of:
            "enter" (default) → KEY_ENTER + KEY_KPENTER
            "tab"             → KEY_TAB
            "lf" / "newline"  → KEY_ENTER (alias)
            integer scancodes joined by ","  → custom set
        strip_prefix, strip_suffix: substrings to strip from each scan
            before emit. Common when the scanner is configured with
            framing chars the consumer doesn't want.
        max_length: drop scans longer than this (defensive — corrupted
            framing or stuck scanner can otherwise emit huge buffers).
        caps_lock_strategy: "ignore" (default — recommended for retail,
            scancodes are translated through the internal US keymap)
            or "respect" (toggle uppercase/lowercase per Caps Lock).
    """

    def __init__(
        self,
        reader_id: str,
        device_path: Optional[str] = None,
        *,
        vid: Optional[int] = None,
        pid: Optional[int] = None,
        name_regex: Optional[str] = None,
        grab: bool = True,
        terminator: str = "enter",
        strip_prefix: str = "",
        strip_suffix: str = "",
        max_length: int = 4096,
        caps_lock_strategy: str = "ignore",
    ) -> None:
        super().__init__(reader_id)
        if evdev is None:
            raise RuntimeError(
                "python-evdev is not installed; HID readers need "
                "`pip install evdev`. (Linux only.)"
            )
        if not (device_path or vid or pid or name_regex):
            raise ValueError(
                f"HID reader {reader_id!r} needs at least one of: "
                "device_path, vid+pid, or name_regex"
            )
        self.device_path = device_path
        self.vid = vid
        self.pid = pid
        self.name_regex = re.compile(name_regex, re.I) if name_regex else None
        self.grab = grab
        self.terminator_keys = _parse_terminator(terminator)
        self.strip_prefix = strip_prefix
        self.strip_suffix = strip_suffix
        self.max_length = max_length
        self.caps_lock_strategy = caps_lock_strategy
        self._dev: Optional["evdev.InputDevice"] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._caps_on = False

    # ─── Lifecycle ──────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._open_device()
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"HidReader[{self.reader_id}]",
            daemon=True,
        )
        self._running = True
        self._thread.start()
        _logger.info(
            "HID reader %r started on %s (name=%r grab=%s terminators=%s)",
            self.reader_id, self._dev.path, self._dev.name,
            self.grab, sorted(self.terminator_keys),
        )

    def stop(self) -> None:
        self._stop_evt.set()
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._dev is not None:
            try:
                if self.grab:
                    self._dev.ungrab()
            except Exception:  # noqa: BLE001
                pass
            self._dev.close()
            self._dev = None
        _logger.info("HID reader %r stopped", self.reader_id)

    # ─── Discovery (used by CLI + auto-config) ──────────────

    @classmethod
    def discover(cls) -> list[HidScannerInfo]:
        """Enumerate /dev/input/event* devices that look like keyboards
        (have KEY_ENTER capability). Returns one entry per candidate."""
        if evdev is None:
            return []
        results: list[HidScannerInfo] = []
        for path in evdev.list_devices():
            try:
                d = evdev.InputDevice(path)
            except Exception:  # noqa: BLE001
                continue
            try:
                caps = d.capabilities().get(evdev.ecodes.EV_KEY, [])
                if KEY_ENTER not in caps:
                    continue
                guess = None
                for vendor, name_re, vids in SCANNER_VENDOR_PRESETS:
                    if name_re.search(d.name or ""):
                        guess = vendor
                        break
                    if vids and d.info.vendor in vids:
                        guess = vendor
                        break
                results.append(HidScannerInfo(
                    device_path=path,
                    name=d.name or "?",
                    vendor_id=d.info.vendor,
                    product_id=d.info.product,
                    bustype=d.info.bustype,
                    vendor_guess=guess,
                ))
            finally:
                d.close()
        return results

    # ─── Internals ──────────────────────────────────────────

    def _open_device(self) -> None:
        path = self.device_path or self._auto_resolve_path()
        if not path:
            raise FileNotFoundError(
                f"HID reader {self.reader_id!r}: no matching device "
                f"(vid={self.vid} pid={self.pid} name={self.name_regex})"
            )
        self._dev = evdev.InputDevice(path)
        if self.grab:
            self._dev.grab()

    def _auto_resolve_path(self) -> Optional[str]:
        """Return path of the first input device matching vid/pid/name."""
        for info in self.discover():
            if self.vid and info.vendor_id != self.vid:
                continue
            if self.pid and info.product_id != self.pid:
                continue
            if self.name_regex and not self.name_regex.search(info.name):
                continue
            return info.device_path
        return None

    def _emit_barcode(self, barcode: str) -> None:
        """Apply strip + length bound, then publish to listener."""
        if self.strip_prefix and barcode.startswith(self.strip_prefix):
            barcode = barcode[len(self.strip_prefix):]
        if self.strip_suffix and barcode.endswith(self.strip_suffix):
            barcode = barcode[: -len(self.strip_suffix)]
        barcode = barcode.strip()
        if not barcode:
            return
        if len(barcode) > self.max_length:
            _logger.warning(
                "HID reader %r dropping oversized scan (%d bytes)",
                self.reader_id, len(barcode),
            )
            return
        self._emit(barcode)

    def _loop(self) -> None:
        """Background thread — translate scancodes to barcode strings."""
        buf: list[str] = []
        shift_held = False
        try:
            for event in self._dev.read_loop():  # type: ignore[union-attr]
                if self._stop_evt.is_set():
                    break
                if event.type != evdev.ecodes.EV_KEY:
                    continue
                key_event = evdev.categorize(event)
                # 0=release, 1=press, 2=autorepeat. Scanners don't need
                # autorepeat, but we accept it defensively.
                if key_event.keystate == 0:
                    if key_event.scancode in (KEY_LEFT_SHIFT, KEY_RIGHT_SHIFT):
                        shift_held = False
                    continue
                if key_event.keystate == 2:
                    continue

                sc = key_event.scancode

                if sc in (KEY_LEFT_SHIFT, KEY_RIGHT_SHIFT):
                    shift_held = True
                    continue

                if sc == KEY_CAPSLOCK:
                    if self.caps_lock_strategy == "respect":
                        self._caps_on = not self._caps_on
                    continue  # never emit Caps as a char

                if sc in self.terminator_keys:
                    self._emit_barcode("".join(buf))
                    buf.clear()
                    continue

                ch = SCANCODE_MAP.get(sc)
                if ch is None:
                    continue
                if shift_held:
                    ch = SCANCODE_SHIFT_MAP.get(ch, ch.upper())
                elif self._caps_on and ch.isalpha():
                    ch = ch.upper()
                buf.append(ch)
        except OSError as exc:
            _logger.warning(
                "HID reader %r device error: %s — stopping", self.reader_id, exc
            )
        except Exception:  # noqa: BLE001
            _logger.exception(
                "HID reader %r loop crashed", self.reader_id
            )
        finally:
            self._running = False
            if buf:
                _logger.debug(
                    "HID reader %r exiting with partial buffer %r",
                    self.reader_id, "".join(buf),
                )


# ─── Helpers ────────────────────────────────────────────────────────


def _parse_terminator(spec: str) -> frozenset[int]:
    """Parse the YAML `terminator` field into a set of scancodes."""
    spec = (spec or "enter").strip().lower()
    if spec in ("enter", "lf", "newline", "\n", "\\n"):
        return DEFAULT_TERMINATOR_KEYS
    if spec in ("tab", "\t", "\\t"):
        return frozenset({KEY_TAB})
    # Numeric scancodes separated by comma
    keys = set()
    for tok in spec.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            keys.add(int(tok))
        except ValueError:
            _logger.warning(
                "Ignoring unrecognised terminator token %r", tok
            )
    return frozenset(keys) if keys else DEFAULT_TERMINATOR_KEYS
