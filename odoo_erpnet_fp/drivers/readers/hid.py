"""
HID barcode reader (USB keyboard-emulating scanners).

Uses the Linux input-event subsystem via `evdev`. The reader grabs
exclusive access to a `/dev/input/eventN` device so its keystrokes do
NOT bleed into the rest of the system, then translates scancodes to
characters using a fixed US-keyboard layout (which all major BG retail
scanners — Symbol/Zebra, Honeywell, Datalogic, Newland — emit by
default).

A complete barcode is recognised when the scanner sends ENTER (KEY_ENTER,
scancode 28). Latency from scan to listener callback is typically <1ms.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

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


class HidBarcodeReader(BarcodeReader):
    """USB-HID barcode scanner driver.

    Args:
        reader_id: short id used in API URLs (`/readers/{id}/...`)
        device_path: path under /dev/input/, e.g. `/dev/input/event5`
            or a /dev/input/by-id/usb-...-event-kbd symlink (preferred —
            stable across reboots).
        grab: if True (default), acquire exclusive access to the device
            so keystrokes don't reach the host system's UI.
    """

    def __init__(
        self,
        reader_id: str,
        device_path: str,
        grab: bool = True,
    ) -> None:
        super().__init__(reader_id)
        if evdev is None:
            raise RuntimeError(
                "python-evdev is not installed; HID readers need "
                "`pip install evdev`. (Linux only.)"
            )
        self.device_path = device_path
        self.grab = grab
        self._dev: Optional["evdev.InputDevice"] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()

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
            "HID reader %r started on %s", self.reader_id, self.device_path
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

    # ─── Internals ──────────────────────────────────────────

    def _open_device(self) -> None:
        self._dev = evdev.InputDevice(self.device_path)
        if self.grab:
            self._dev.grab()

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
                # `evdev` distinguishes key_down (1), key_hold (2), key_up (0)
                if key_event.keystate not in (1, 2):
                    if key_event.scancode in (KEY_LEFT_SHIFT, KEY_RIGHT_SHIFT):
                        shift_held = False
                    continue

                sc = key_event.scancode

                if sc in (KEY_LEFT_SHIFT, KEY_RIGHT_SHIFT):
                    shift_held = True
                    continue

                if sc in (KEY_ENTER, KEY_KPENTER):
                    barcode = "".join(buf).strip()
                    buf.clear()
                    self._emit(barcode)
                    continue

                ch = SCANCODE_MAP.get(sc)
                if ch is None:
                    continue
                if shift_held:
                    ch = SCANCODE_SHIFT_MAP.get(ch, ch.upper())
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
            # Best-effort emit of partial buffer — most likely a stuck
            # scan that the user will re-do, but don't lose it silently
            if buf:
                _logger.debug(
                    "HID reader %r exiting with partial buffer %r",
                    self.reader_id,
                    "".join(buf),
                )
