# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""
Common types for GPS / vehicle-tracking drivers.

A ``GpsTracker`` is an abstract long-lived object that delivers
``PositionEvent`` fixes through a callback. Subclasses implement the
source specifics — Wialon Remote API polling, serial NMEA, or an
external push endpoint.

The proxy stays a thin relay: it does NOT resolve vehicles/drivers
against any master data. It forwards the raw fix (plate hint, unit id,
lat/lon/speed/heading) and lets Odoo resolve routing/matching in one
authoritative place.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Optional

_logger = logging.getLogger(__name__)

# Listener callback signature: (PositionEvent) -> None
PositionListener = Callable[["PositionEvent"], None]


@dataclass
class PositionEvent:
    """A single vehicle position fix.

    ``unit_id`` is the tracking-platform unit id (Wialon unit, tracker
    serial…); ``plate`` is a license-plate hint (Wialon unit name often
    carries it) used by Odoo to match ``fleet.vehicle``. Both may be
    empty for exotic sources; Odoo matches on whatever is present.
    """

    tracker_id: str
    unit_id: str = ""
    plate: str = ""
    lat: Optional[float] = None
    lon: Optional[float] = None
    speed: float = 0.0            # km/h
    heading: float = 0.0          # degrees, 0..360
    altitude: float = 0.0         # meters
    sat_count: int = 0
    driver_code: str = ""         # currently-logged driver key (iButton/tag)
    odometer_km: Optional[float] = None  # optional CAN mileage snapshot
    fix_utc: Optional[int] = None        # UNIX UTC of the fix (from source)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def has_fix(self) -> bool:
        return self.lat is not None and self.lon is not None

    def to_json(self) -> dict:
        """Envelope ``data`` payload for bus_inject / WS / SSE clients.

        Keys use the flat names the Odoo side expects on
        ``vehicle.position`` events. ``ts`` is ISO-8601 UTC.
        """
        return {
            "tracker_id": self.tracker_id,
            "unit_id": self.unit_id,
            "plate": self.plate,
            "lat": self.lat,
            "lon": self.lon,
            "speed": self.speed,
            "heading": self.heading,
            "altitude": self.altitude,
            "sat_count": self.sat_count,
            "driver_code": self.driver_code,
            "odometer_km": self.odometer_km,
            "fix_utc": self.fix_utc,
            "ts": self.timestamp.astimezone(timezone.utc)
            .isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        }


class GpsTracker(ABC):
    """ABC for GPS-tracker drivers.

    Lifecycle:
        tracker = WialonTracker(tracker_id="fleet", base_url=..., token=...)
        tracker.set_listener(my_callback)
        tracker.start()      # spawns background thread, calls listener per fix
        ...
        tracker.stop()       # join thread + release resources
    """

    def __init__(self, tracker_id: str) -> None:
        self.tracker_id = tracker_id
        self._listener: Optional[PositionListener] = None
        self._running = False

    def set_listener(self, listener: Optional[PositionListener]) -> None:
        self._listener = listener

    @abstractmethod
    def start(self) -> None:
        """Begin background polling/streaming. Non-blocking."""

    @abstractmethod
    def stop(self) -> None:
        """Stop background thread + release resources."""

    @property
    def is_running(self) -> bool:
        return self._running

    def _emit(self, event: PositionEvent) -> None:
        """Called from the tracker thread for each fix."""
        if not event.has_fix:
            return
        if self._listener is not None:
            try:
                self._listener(event)
            except Exception:  # noqa: BLE001
                _logger.exception(
                    "Tracker %s listener raised — fix dropped", self.tracker_id
                )
