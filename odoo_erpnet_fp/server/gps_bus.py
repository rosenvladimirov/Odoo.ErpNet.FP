# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""
Per-tracker pub/sub event bus for GPS position fixes.

Each fix is broadcast to:

  1. **WebSocket subscribers** — `/gps/{id}/ws` (lowest latency LAN).
  2. **SSE subscribers** — `/gps/{id}/events`.
  3. **bus_inject** — a raw ``vehicle.position`` envelope to the Odoo
     addon ``l10n_bg_erp_net_fp_bus_inject``, broadcast on ``bus.bus``
     channel ``erpnet_fp_proxy_events``; the Odoo ``l10n_bg_live_refresh``
     hub fans it out to every open backend tab (event ``PROXY_EVENT``),
     where the map/fleet/waybill refreshers react.

The bus keeps a small ring buffer of the latest fix per unit so late
subscribers can fetch it (`/gps/{id}/last`) without being connected at
emit time. The proxy stays a thin relay: NO vehicle/driver resolution
here — Odoo matches on plate/unit_id in one authoritative place.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from ..drivers.gps.common import PositionEvent

_logger = logging.getLogger(__name__)


class GpsEventBus:
    """One bus per tracker. Owned by a TrackerEntry in the registry."""

    def __init__(
        self,
        tracker_id: str,
        loop: Optional[asyncio.AbstractEventLoop] = None,
        app=None,
    ) -> None:
        self.tracker_id = tracker_id
        self._app = app
        self._loop = loop  # captured at start; tracker thread schedules onto it
        self._subscribers: set[asyncio.Queue] = set()
        # Последен fix per unit — за /last и за късни абонати.
        self._latest: dict[str, PositionEvent] = {}

    # ─── Subscriber management ──────────────────────────────

    def subscribe(self) -> "asyncio.Queue[PositionEvent]":
        q: asyncio.Queue[PositionEvent] = asyncio.Queue(maxsize=128)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # ─── Publish (called from tracker thread) ───────────────

    def publish_threadsafe(self, event: PositionEvent) -> None:
        if self._loop is None or self._loop.is_closed():
            _logger.warning(
                "Tracker %r publishing fix but no loop attached — dropped",
                self.tracker_id,
            )
            return
        asyncio.run_coroutine_threadsafe(self._publish(event), self._loop)

    async def _publish(self, event: PositionEvent) -> None:
        # 1. Latest snapshot (always — supports late subscribers).
        if event.unit_id:
            self._latest[event.unit_id] = event

        # 2. Live WS/SSE subscribers — non-blocking; drop on full queue.
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                _logger.warning(
                    "Tracker %r subscriber queue full — dropping fix",
                    self.tracker_id,
                )

        # 3. bus_inject — raw vehicle.position envelope to Odoo.
        try:
            from ..clients.bus_inject import BusInjectClient
            client = BusInjectClient.from_app(self._app) if self._app else None
            if client is not None:
                client.emit(
                    "vehicle.position",
                    device=self.tracker_id,
                    device_kind="gps",
                    data=event.to_json(),
                )
                client.close()
        except Exception:  # noqa: BLE001
            _logger.debug(
                "Tracker %r bus_inject emit failed", self.tracker_id,
                exc_info=True,
            )

    # ─── Latest access ──────────────────────────────────────

    def latest(self, unit_id: Optional[str] = None):
        if unit_id is not None:
            return self._latest.get(unit_id)
        return list(self._latest.values())

    async def close(self) -> None:
        self._subscribers.clear()
