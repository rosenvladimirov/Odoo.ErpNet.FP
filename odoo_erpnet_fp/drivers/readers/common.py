"""
Common types for barcode reader drivers.

A `BarcodeReader` is an abstract long-lived object that delivers
`BarcodeScan` events through a callback. Subclasses implement transport
specifics — HID (evdev) or serial (pyserial).
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Optional

_logger = logging.getLogger(__name__)

# Listener callback signature: (BarcodeScan) -> None
ScanListener = Callable[["BarcodeScan"], None]


@dataclass
class BarcodeScan:
    """A single scanned barcode event."""

    reader_id: str
    barcode: str
    timestamp: datetime = field(default_factory=datetime.now)
    raw_bytes: Optional[bytes] = None  # for serial scanners; HID has none

    def to_json(self) -> dict:
        return {
            "readerId": self.reader_id,
            "barcode": self.barcode,
            "timestamp": self.timestamp.strftime("%Y-%m-%dT%H:%M:%S.%f"),
        }


class BarcodeReader(ABC):
    """ABC for barcode-reader drivers.

    Lifecycle:
        reader = HidBarcodeReader(reader_id="scan1", device_path="/dev/input/event5")
        reader.set_listener(my_callback)
        reader.start()      # spawns background thread, calls listener for each scan
        ...
        reader.stop()       # join thread + close device
    """

    def __init__(self, reader_id: str) -> None:
        self.reader_id = reader_id
        self._listener: Optional[ScanListener] = None
        self._running = False

    def set_listener(self, listener: Optional[ScanListener]) -> None:
        self._listener = listener

    @abstractmethod
    def start(self) -> None:
        """Begin background scanning. Non-blocking."""

    @abstractmethod
    def stop(self) -> None:
        """Stop background thread + release the device."""

    @property
    def is_running(self) -> bool:
        return self._running

    def _emit(self, barcode: str, raw: Optional[bytes] = None) -> None:
        """Called from the scanning thread for each completed barcode."""
        if not barcode:
            return
        scan = BarcodeScan(
            reader_id=self.reader_id, barcode=barcode, raw_bytes=raw
        )
        if self._listener is not None:
            try:
                self._listener(scan)
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "Reader %s listener raised — scan dropped", self.reader_id
                )
