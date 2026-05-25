"""
BlueCash shift-signal endpoint (Odoo → Android push).

Conjugate of `shift_sync.py` (Android → Odoo). Implements the contract
in `anchor_bluecash_shift_signal_contract.md`:

    Endpoints
    ─────────
    POST  /devices/<serial>/events/push   — Odoo emits an event for
                                            this device (HMAC-signed)
    WS    /devices/<serial>/events/ws     — Android subscribes here
    GET   /devices/<serial>/events/sse    — Server-Sent Events fallback
    GET   /devices/<serial>/events/last   — last event poll

    Event shapes (sent verbatim over WS)
    ────────────────────────────────────
    {"type": "shift.open",
     "pos_session_id": 1234,
     "operator_code": "1",
     "fiscal_day_number": 47,
     "issued_at": "2026-05-25T07:31:12Z"}

    {"type": "shift.close.request",
     "pos_session_id": 1234,
     "reason": "operator"}

Auth: same HMAC scheme as shift_sync.
    Odoo → Proxy: X-Registry-Signature(raw_body, iot_setup.token)
    Proxy → Android (WS): no auth (already inside trusted LAN +
        per-device subscription; trust boundary is proxy ingress)

In-memory pub/sub — events are NOT persisted across proxy restarts.
The contract assumes Odoo will re-emit on Android reconnect (via
`/devices/<serial>/events/last` poll) or via the next session
state change.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from typing import Optional

from fastapi import (
    APIRouter, Header, HTTPException, Query, Request,
    WebSocket, WebSocketDisconnect, status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from ..odoo_forwarder import verify_signature

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/devices", tags=["shift_signal"])


# ── In-memory event hub ──────────────────────────────────────────────


class _DeviceChannel:
    """Per-device fan-out + last-event memory."""

    def __init__(self, serial: str) -> None:
        self.serial = serial
        self._subscribers: list[asyncio.Queue] = []
        self._last_event: dict | None = None
        self._last_at: float = 0.0
        # Кратък ring-buffer за late subscribers (catch-up без storage).
        self._history: deque = deque(maxlen=32)

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    def publish(self, event: dict) -> int:
        """Fan-out event към всички subscribers. Връща брой получили."""
        self._last_event = event
        self._last_at = time.time()
        self._history.append({"at": self._last_at, "event": event})
        delivered = 0
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                _logger.warning(
                    "device %s: subscriber queue full — dropped event %s",
                    self.serial, event.get("type"))
        return delivered

    @property
    def last_event(self) -> dict | None:
        return self._last_event

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def history_since(self, since_ts: float) -> list[dict]:
        return [h["event"] for h in self._history if h["at"] > since_ts]


class _EventHub:
    """Process-wide in-memory channel registry."""

    def __init__(self) -> None:
        self._channels: dict[str, _DeviceChannel] = {}

    def channel(self, serial: str) -> _DeviceChannel:
        c = self._channels.get(serial)
        if c is None:
            c = _DeviceChannel(serial)
            self._channels[serial] = c
        return c

    def stats(self) -> dict:
        return {
            serial: {
                "subscribers": ch.subscriber_count,
                "last_event_type": (ch.last_event or {}).get("type"),
                "last_event_at": ch._last_at,
            }
            for serial, ch in self._channels.items()
        }


_hub = _EventHub()


# ── Schemas ──────────────────────────────────────────────────────────


class _Cml(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class EventPushBody(_Cml):
    """Wrapper за Odoo→proxy push.

    `event` е свободно (валидира Android клиентът). Минимални gates:
    `type` трябва да е present; всеки event се stamp-ва с
    `_received_at` от страна на проксито.
    """
    event: dict


class EventPushResp(_Cml):
    ok: bool
    delivered_to: int = Field(0, alias="deliveredTo")
    subscribers: int = 0


class LastEventResp(_Cml):
    has_event: bool = Field(False, alias="hasEvent")
    event: dict | None = None


# ── Auth helper ──────────────────────────────────────────────────────


def _shared_secret(cfg) -> Optional[str]:
    """Same shared secret като shift_sync — `iot_setup.token`."""
    return getattr(getattr(cfg, "iot_setup", None), "token", None) or None


# ── Endpoints ────────────────────────────────────────────────────────


@router.post("/{serial}/events/push", response_model=EventPushResp)
async def event_push(
    serial: str,
    body: EventPushBody,
    request: Request,
    x_registry_signature: Optional[str] = Header(
        None, alias="X-Registry-Signature"),
) -> EventPushResp:
    """Odoo emits an event for `serial`. Fan-out to WS subscribers."""
    cfg = request.app.state.config.server
    secret = _shared_secret(cfg)
    if not secret:
        raise HTTPException(503, "Proxy not configured (iot_setup.token)")
    raw = await request.body()
    if not verify_signature(raw, secret, x_registry_signature or ""):
        raise HTTPException(401, "X-Registry-Signature mismatch")
    event = body.event or {}
    if not event.get("type"):
        raise HTTPException(400, "Event missing 'type' field")
    event.setdefault("_received_at", time.time())
    channel = _hub.channel(serial)
    delivered = channel.publish(event)
    return EventPushResp(
        ok=True,
        delivered_to=delivered,
        subscribers=channel.subscriber_count,
    )


@router.get("/{serial}/events/last", response_model=LastEventResp)
async def event_last(serial: str) -> LastEventResp:
    """Връща последното published event за device-а (или празно)."""
    channel = _hub.channel(serial)
    last = channel.last_event
    return LastEventResp(has_event=last is not None, event=last)


@router.get("/{serial}/events/sse")
async def event_sse(
    serial: str,
    since: float = Query(0.0, ge=0.0),
) -> StreamingResponse:
    """Server-Sent Events stream. `?since=<ts>` replays history."""
    channel = _hub.channel(serial)

    async def gen():
        # Replay missed events from history.
        for ev in channel.history_since(since):
            yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
        q = channel.subscribe()
        try:
            while True:
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=25.0)
                    yield f"data: {json.dumps(ev, ensure_ascii=False)}\n\n"
                except asyncio.TimeoutError:
                    # Keep-alive heart-beat — много proxy/middlebox
                    # пускат idle SSE след ~30s.
                    yield ": ka\n\n"
        finally:
            channel.unsubscribe(q)

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx friendly
        },
    )


@router.websocket("/{serial}/events/ws")
async def event_ws(ws: WebSocket, serial: str) -> None:
    """WebSocket subscription. Preferred Android transport."""
    await ws.accept()
    channel = _hub.channel(serial)
    q = channel.subscribe()
    # On-connect replay: изпращаме последното event ако има (catch-up
    # за reconnecting клиенти, които не помнят `since`).
    if channel.last_event:
        try:
            await ws.send_text(
                json.dumps(channel.last_event, ensure_ascii=False))
        except Exception:  # noqa: BLE001
            channel.unsubscribe(q)
            return
    try:
        while True:
            try:
                ev = await asyncio.wait_for(q.get(), timeout=25.0)
                await ws.send_text(json.dumps(ev, ensure_ascii=False))
            except asyncio.TimeoutError:
                # Ping ot proxy → client.
                try:
                    await ws.send_text(json.dumps({"type": "ping"}))
                except Exception:  # noqa: BLE001
                    break
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        _logger.exception("ws handler failed for %s", serial)
    finally:
        channel.unsubscribe(q)


@router.get("/{serial}/events/stats")
async def event_stats(serial: str) -> dict:
    """Admin / debug endpoint — counts."""
    channel = _hub.channel(serial)
    return {
        "serial": serial,
        "subscribers": channel.subscriber_count,
        "last_event": channel.last_event,
        "last_event_at": channel._last_at,
        "history_size": len(channel._history),
    }


__all__ = ["router"]
