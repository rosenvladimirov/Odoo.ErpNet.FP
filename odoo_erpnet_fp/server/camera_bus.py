"""
Per-camera pub/sub event bus.

A 1:1 copy of `reader_bus.py` — every recognised license plate is
broadcast to:

  1. **WebSocket subscribers** — `/cameras/{id}/ws`
  2. **SSE subscribers** — `/cameras/{id}/events`
  3. **Long-poll waiters** — `GET /cameras/{id}/next`
  4. **Configured webhooks** — outbound POST (typically the Odoo
     `hr_attendance_access_control` controller; Odoo takes the
     access decision — the proxy only reports the plate).
  5. **Native Odoo IoT long-poll** — pushed as an `iot.device` event
     under identifier `camera.<camera_id>`.

A small ring buffer of recent plates lets late subscribers fetch the
last recognition (`/cameras/{id}/last`) without being connected at the
moment the car passed.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Optional

import httpx

from ..drivers.cameras.common import PlateEvent

_logger = logging.getLogger(__name__)


class CameraEventBus:
    """One bus per camera. Owned by a CameraEntry in the registry."""

    HISTORY_SIZE = 32
    WEBHOOK_TIMEOUT = 4.0
    WEBHOOK_RETRIES = 2

    def __init__(
        self,
        camera_id: str,
        webhooks: Optional[list[str]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self.camera_id = camera_id
        self.webhooks = webhooks or []
        self._loop = loop  # captured at create time; sampling thread schedules onto it
        self._subscribers: set[asyncio.Queue] = set()
        self._history: deque[PlateEvent] = deque(maxlen=self.HISTORY_SIZE)
        self._http: Optional[httpx.AsyncClient] = None

    # ─── Subscriber management ──────────────────────────────

    def subscribe(self) -> asyncio.Queue[PlateEvent]:
        q: asyncio.Queue[PlateEvent] = asyncio.Queue(maxsize=64)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # ─── Publish (called from sampling thread) ──────────────

    def publish_threadsafe(self, evt: PlateEvent) -> None:
        if self._loop is None or self._loop.is_closed():
            _logger.warning(
                "Camera %r publishing plate but no loop attached — dropped",
                self.camera_id,
            )
            return
        asyncio.run_coroutine_threadsafe(self._publish(evt), self._loop)

    async def _publish(self, evt: PlateEvent) -> None:
        # 0. Metrics — guarded (metric може да липсва на стари билдове)
        try:
            from . import metrics as _m
            _m.camera_plates_total.labels(camera_id=self.camera_id).inc()
        except Exception:
            pass

        # 1. History (always — supports late subscribers)
        self._history.append(evt)

        # 2. Live subscribers — non-blocking; drop on full queue
        for q in list(self._subscribers):
            try:
                q.put_nowait(evt)
            except asyncio.QueueFull:
                _logger.warning(
                    "Camera %r subscriber queue full — dropping plate",
                    self.camera_id,
                )

        # 3. Outbound webhooks (fire-and-forget; image included so the
        #    Odoo access log can store the evidence frame)
        if self.webhooks:
            asyncio.create_task(self._deliver_webhooks(evt))

        # 4. Native Odoo IoT long-poll subscribers — push under
        #    identifier `camera.<camera_id>`.
        try:
            from .routes.iot_compat import get_iot_sessions
            sessions = get_iot_sessions()
            ident = f"camera.{self.camera_id}"
            waiter_count = len(sessions._waiters.get(ident, []))
            _logger.info(
                "IoT push: %s plate=%r conf=%.2f waiters=%d",
                ident, evt.plate, evt.confidence, waiter_count,
            )
            await sessions.push(
                ident,
                {
                    "result": evt.plate,
                    "value": evt.plate,  # legacy alias
                    "plate": evt.plate,
                    "confidence": evt.confidence,
                    "event": evt.to_json(include_image=False),
                    "status": {"status": "success"},
                },
            )
        except Exception:  # noqa: BLE001
            _logger.debug(
                "Camera %r IoT push failed (compat layer not loaded?)",
                self.camera_id, exc_info=True,
            )

    # ─── History access ─────────────────────────────────────

    def last_event(self) -> Optional[PlateEvent]:
        return self._history[-1] if self._history else None

    def history(self, limit: int = 10) -> list[PlateEvent]:
        return list(self._history)[-limit:]

    # ─── Webhook delivery ───────────────────────────────────

    async def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.WEBHOOK_TIMEOUT)
        return self._http

    async def _deliver_webhooks(self, evt: PlateEvent) -> None:
        client = await self._http_client()
        payload = evt.to_json(include_image=True)
        for url in self.webhooks:
            for attempt in range(self.WEBHOOK_RETRIES + 1):
                try:
                    resp = await client.post(url, json=payload)
                    resp.raise_for_status()
                    break
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "Camera %r webhook %s attempt %d failed: %s",
                        self.camera_id, url, attempt + 1, exc,
                    )
                    if attempt < self.WEBHOOK_RETRIES:
                        await asyncio.sleep(0.5 * (attempt + 1))

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
