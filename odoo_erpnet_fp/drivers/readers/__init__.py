"""
Barcode reader drivers — pure-Python.

Two transport types:
  hid     — USB-HID keyboard-emulating scanners (Symbol/Zebra/Honeywell/...)
            via evdev /dev/input/eventN
  serial  — RS232 / USB-CDC industrial scanners via pyserial,
            line-terminated barcodes

Both expose the same `BarcodeReader` ABC that emits `BarcodeScan` events
through a listener callback (push model — no polling).
"""

from .common import BarcodeReader, BarcodeScan
from .hid import HidBarcodeReader, SCANCODE_MAP, SCANCODE_SHIFT_MAP
from .serial_reader import SerialBarcodeReader

__all__ = [
    "BarcodeReader",
    "BarcodeScan",
    "HidBarcodeReader",
    "SerialBarcodeReader",
    "SCANCODE_MAP",
    "SCANCODE_SHIFT_MAP",
]
