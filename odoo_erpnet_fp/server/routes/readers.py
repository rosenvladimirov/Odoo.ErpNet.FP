"""
Extension API for barcode readers.

Push model — clients connect once via WebSocket or SSE and receive
each scan with sub-millisecond latency. Long-poll HTTP fallback is
provided for environments where WS/SSE aren't possible.

Endpoints:
  GET /readers                       — list configured readers
  GET /readers/{id}                  — info (transport, addr, webhooks count)
  GET /readers/{id}/last             — last scanned barcode (history)
  GET /readers/{id}/next?timeout=30  — long-poll next barcode (HTTP)
  GET /readers/{id}/events           — Server-Sent Events stream
  WS  /readers/{id}/ws               — WebSocket push (recommended)
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/readers", tags=["readers"])


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class ReaderInfoResp(_CamelModel):
    id: str
    transport: str
    device_path: Optional[str] = Field(None, alias="devicePath")
    port: Optional[str] = None
    webhooks: int = 0
    running: bool = False
    subscriber_count: int = Field(0, alias="subscriberCount")


class ScanResp(_CamelModel):
    reader_id: str = Field(..., alias="readerId")
    barcode: str
    timestamp: str


def _reader_registry(request: Request):
    return request.app.state.reader_registry


def _require(request: Request, id: str):
    reg = _reader_registry(request)
    if reg is None or not reg.has(id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Reader {id!r} not found")
    return reg


@router.get("", response_model=dict[str, ReaderInfoResp])
@router.get("/", include_in_schema=False)
async def list_readers(request: Request):
    reg = _reader_registry(request)
    if reg is None:
        return {}
    return {
        rid: ReaderInfoResp(
            id=rid,
            transport=entry.config.transport,
            device_path=entry.config.device_path,
            port=entry.config.port,
            webhooks=len(entry.config.webhooks),
            running=entry.driver.is_running if entry.driver else False,
            subscriber_count=entry.bus.subscriber_count,
        )
        for rid, entry in reg.readers.items()
    }


@router.get("/{id}", response_model=ReaderInfoResp)
async def reader_info(id: str, request: Request):
    reg = _require(request, id)
    entry = reg.get(id)
    return ReaderInfoResp(
        id=id,
        transport=entry.config.transport,
        device_path=entry.config.device_path,
        port=entry.config.port,
        webhooks=len(entry.config.webhooks),
        running=entry.driver.is_running if entry.driver else False,
        subscriber_count=entry.bus.subscriber_count,
    )


@router.get("/{id}/last", response_model=Optional[ScanResp])
async def reader_last(id: str, request: Request):
    """Last seen scan (history). Useful for UI bootstraps."""
    reg = _require(request, id)
    last = reg.get_bus(id).last_scan()
    if last is None:
        return None
    return ScanResp(**last.to_json())


@router.get("/{id}/next", response_model=Optional[ScanResp])
async def reader_next(
    id: str,
    request: Request,
    timeout: float = Query(30.0, ge=0.1, le=120.0),
):
    """Long-poll next scan — HTTP fallback for clients that can't use
    WebSocket / SSE. Returns 204 No Content on timeout.
    """
    reg = _require(request, id)
    bus = reg.get_bus(id)
    queue = bus.subscribe()
    try:
        scan = await asyncio.wait_for(queue.get(), timeout=timeout)
        return ScanResp(**scan.to_json())
    except asyncio.TimeoutError:
        from fastapi import Response
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    finally:
        bus.unsubscribe(queue)


@router.get("/{id}/events")
async def reader_events(id: str, request: Request):
    """SSE — `text/event-stream`. Browser EventSource compatible.

        const es = new EventSource('/readers/scan1/events');
        es.addEventListener('scan', e => console.log(JSON.parse(e.data)));
    """
    reg = _require(request, id)
    bus = reg.get_bus(id)

    async def stream():
        queue = bus.subscribe()
        try:
            # Send a hello event so the client knows the stream is live
            yield "event: hello\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    scan = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield (
                        f"event: scan\ndata: {ScanResp(**scan.to_json()).model_dump_json(by_alias=True)}\n\n"
                    )
                except asyncio.TimeoutError:
                    # SSE keepalive comment — keeps the connection open
                    # through HTTP proxies that close idle streams.
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # nginx: don't buffer SSE
        },
    )


@router.websocket("/{id}/ws")
async def reader_ws(websocket: WebSocket, id: str):
    """WebSocket — lowest-latency push. Recommended for POS UI clients.

    On connect, sends `{"type": "hello", "readerId": "..."}` and then
    `{"type": "scan", "readerId": "...", "barcode": "...", "timestamp": "..."}`
    for each scan.
    """
    reg = websocket.app.state.reader_registry
    if reg is None or not reg.has(id):
        await websocket.close(code=4404, reason=f"Reader {id!r} not found")
        return
    bus = reg.get_bus(id)
    await websocket.accept()
    queue = bus.subscribe()
    try:
        await websocket.send_json({"type": "hello", "readerId": id})
        while True:
            scan = await queue.get()
            await websocket.send_json({
                "type": "scan",
                **scan.to_json(),
            })
    except WebSocketDisconnect:
        pass
    except Exception:
        _logger.exception("Reader %r WebSocket loop crashed", id)
    finally:
        bus.unsubscribe(queue)
