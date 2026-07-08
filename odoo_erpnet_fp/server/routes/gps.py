# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""
Extension API for GPS / vehicle trackers.

Push model — clients connect once via WebSocket or SSE and receive each
position fix with low latency. The primary consumer is Odoo (via the
bus_inject envelope, emitted by the bus); these endpoints let LAN clients
(dashboards, diagnostics) tap the same stream.

Endpoints:
  GET /gps                     — list configured trackers
  GET /gps/{id}                — info (source, running, subscribers)
  GET /gps/{id}/last           — latest fix per unit (snapshot)
  GET /gps/{id}/events         — Server-Sent Events stream
  WS  /gps/{id}/ws             — WebSocket push
  POST /gps/{id}/inject        — inject a fix (external source / testing)
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import (
    APIRouter,
    HTTPException,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict

from ..drivers.gps.common import PositionEvent

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/gps", tags=["gps"])


class _Model(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class TrackerInfoResp(_Model):
    id: str
    source: str
    running: bool = False
    subscriber_count: int = 0
    unit_count: int = 0


class InjectReq(_Model):
    unit_id: str = ""
    plate: str = ""
    lat: float
    lon: float
    speed: float = 0.0
    heading: float = 0.0
    driver_code: str = ""


def _registry(request: Request):
    return request.app.state.tracker_registry


def _require(request: Request, id: str):
    reg = _registry(request)
    if not reg.has(id):
        raise HTTPException(status_code=404, detail=f"Unknown tracker {id!r}")
    return reg.get(id)


@router.get("", response_model=dict)
@router.get("/", include_in_schema=False)
async def list_trackers(request: Request):
    reg = _registry(request)
    out = {}
    for tid, entry in reg.trackers.items():
        out[tid] = TrackerInfoResp(
            id=tid,
            source=entry.config.source,
            running=bool(entry.driver and entry.driver.is_running),
            subscriber_count=entry.bus.subscriber_count,
            unit_count=len(entry.bus.latest()),
        ).model_dump(by_alias=True)
    return out


@router.get("/{id}", response_model=TrackerInfoResp)
async def tracker_info(id: str, request: Request):
    entry = _require(request, id)
    return TrackerInfoResp(
        id=id,
        source=entry.config.source,
        running=bool(entry.driver and entry.driver.is_running),
        subscriber_count=entry.bus.subscriber_count,
        unit_count=len(entry.bus.latest()),
    )


@router.get("/{id}/last")
async def tracker_last(id: str, request: Request, unit_id: Optional[str] = None):
    entry = _require(request, id)
    data = entry.bus.latest(unit_id)
    if data is None:
        return None
    if isinstance(data, list):
        return [e.to_json() for e in data]
    return data.to_json()


@router.get("/{id}/events")
async def tracker_events(id: str, request: Request):
    entry = _require(request, id)
    bus = entry.bus
    q = bus.subscribe()

    async def stream():
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt: PositionEvent = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"  # SSE comment ping
                    continue
                yield f"data: {json.dumps(evt.to_json())}\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.websocket("/{id}/ws")
async def tracker_ws(websocket: WebSocket, id: str):
    reg = websocket.app.state.tracker_registry
    if not reg.has(id):
        await websocket.close(code=1008)
        return
    bus = reg.get(id).bus
    await websocket.accept()
    q = bus.subscribe()
    try:
        while True:
            evt: PositionEvent = await q.get()
            await websocket.send_json(evt.to_json())
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        _logger.exception("Tracker %r WebSocket loop crashed", id)
    finally:
        bus.unsubscribe(q)


@router.post("/{id}/inject")
async def tracker_inject(id: str, req: InjectReq, request: Request):
    """Inject a fix — for external sources or manual testing. Fans out
    to WS/SSE subscribers + bus_inject identically to a polled fix."""
    entry = _require(request, id)
    evt = PositionEvent(
        tracker_id=id,
        unit_id=req.unit_id,
        plate=req.plate,
        lat=req.lat,
        lon=req.lon,
        speed=req.speed,
        heading=req.heading,
        driver_code=req.driver_code,
    )
    entry.bus.publish_threadsafe(evt)
    return {"ok": True, "id": id, "unit_id": req.unit_id}
