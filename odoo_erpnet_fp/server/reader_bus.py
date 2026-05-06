"""
Per-reader pub/sub event bus.

Each barcode scan is broadcast to:

  1. **WebSocket subscribers** — POS UI clients connected to
     `/readers/{id}/ws`. Lowest latency (≤1ms within LAN).
  2. **SSE subscribers** — POS UI clients on `/readers/{id}/events`.
     Same latency; preferred when WS is blocked by intermediate proxies.
  3. **Long-poll waiters** — clients on `GET /readers/{id}/next`.
  4. **Configured webhooks** — outbound POST to one or more URLs
     (e.g. an Odoo backend `/web/dataset/call_kw/...` endpoint).

The bus also keeps a small ring buffer of recent scans so late
subscribers can fetch the last barcode (`/readers/{id}/last`) without
needing to be connected at the moment of the scan.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from typing import Optional

import httpx

from ..drivers.readers.common import BarcodeScan

_logger = logging.getLogger(__name__)


class ReaderEventBus:
    """One bus per reader. Owned by a ReaderEntry in the registry."""

    HISTORY_SIZE = 32  # how many recent scans to keep
    WEBHOOK_TIMEOUT = 3.0
    WEBHOOK_RETRIES = 2

    def __init__(
        self,
        reader_id: str,
        webhooks: Optional[list[str]] = None,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> None:
        self.reader_id = reader_id
        self.webhooks = webhooks or []
        self._loop = loop  # captured at create time; reader thread schedules onto it
        self._subscribers: set[asyncio.Queue] = set()
        self._history: deque[BarcodeScan] = deque(maxlen=self.HISTORY_SIZE)
        self._lock = asyncio.Lock()
        # Reused HTTP client for webhook delivery
        self._http: Optional[httpx.AsyncClient] = None

    # ─── Subscriber management ──────────────────────────────

    def subscribe(self) -> asyncio.Queue[BarcodeScan]:
        """WS / SSE / long-poll endpoint calls this to get a fresh queue."""
        q: asyncio.Queue[BarcodeScan] = asyncio.Queue(maxsize=64)
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    # ─── Publish (called from reader thread) ────────────────

    def publish_threadsafe(self, scan: BarcodeScan) -> None:
        """Reader thread calls this for every scan.

        Schedules `_publish` on the asyncio loop so coroutine-based
        delivery (subscribers, webhooks) runs in the right context.
        """
        if self._loop is None or self._loop.is_closed():
            _logger.warning(
                "Reader %r publishing scan but no loop attached — dropped",
                self.reader_id,
            )
            return
        asyncio.run_coroutine_threadsafe(self._publish(scan), self._loop)

    async def _publish(self, scan: BarcodeScan) -> None:
        # 0. Metrics
        try:
            from . import metrics as _m
            _m.reader_scans_total.labels(reader_id=self.reader_id).inc()
        except Exception:
            pass

        # 1. History (always — supports late subscribers)
        self._history.append(scan)

        # 2. Live subscribers — non-blocking; if a queue is full, drop
        #    rather than block other listeners
        for q in list(self._subscribers):
            try:
                q.put_nowait(scan)
            except asyncio.QueueFull:
                _logger.warning(
                    "Reader %r subscriber queue full — dropping scan",
                    self.reader_id,
                )

        # 3. Outbound webhooks (fire-and-forget)
        if self.webhooks:
            asyncio.create_task(self._deliver_webhooks(scan))

        # 4. Native Odoo IoT long-poll subscribers — push barcode as
        #    iot.device event under identifier `reader.<reader_id>`.
        try:
            from .routes.iot_compat import get_iot_sessions
            await get_iot_sessions().push(
                f"reader.{self.reader_id}",
                {
                    "result": scan.barcode,
                    "value": scan.barcode,  # legacy alias
                    "status": {"status": "success"},
                },
            )
        except Exception:  # noqa: BLE001
            _logger.debug(
                "Reader %r IoT push failed (compat layer not loaded?)",
                self.reader_id, exc_info=True,
            )

    # ─── History access ─────────────────────────────────────

    def last_scan(self) -> Optional[BarcodeScan]:
        return self._history[-1] if self._history else None

    def history(self, limit: int = 10) -> list[BarcodeScan]:
        return list(self._history)[-limit:]

    # ─── Webhook delivery ───────────────────────────────────

    async def _http_client(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=self.WEBHOOK_TIMEOUT)
        return self._http

    async def _deliver_webhooks(self, scan: BarcodeScan) -> None:
        client = await self._http_client()
        payload = scan.to_json()
        for url in self.webhooks:
            for attempt in range(self.WEBHOOK_RETRIES + 1):
                try:
                    resp = await client.post(url, json=payload)
                    resp.raise_for_status()
                    break
                except Exception as exc:  # noqa: BLE001
                    _logger.warning(
                        "Reader %r webhook %s attempt %d failed: %s",
                        self.reader_id, url, attempt + 1, exc,
                    )
                    if attempt < self.WEBHOOK_RETRIES:
                        await asyncio.sleep(0.5 * (attempt + 1))

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None
