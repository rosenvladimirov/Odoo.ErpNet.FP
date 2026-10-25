"""
Customer display through the proxy (no Odoo needed on the screen side).

  POST /customer-display/{channel}         — the POS posts the order state
                                             body: {order, shop?, labels?, pole?, currency?}
  GET  /customer-display/{channel}/events  — SSE stream for the screens
  GET  /customer-display/{channel}/state   — the last state (polling fallback)
  GET  /customer-display/{channel}         — the screen page (HTML)
  GET  /customer-display                   — the channels and their listeners

`pole` names a display from `/displays` (VFD 2×20). The proxy writes
`pole_lines()` to it; a failing pole never fails the update.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict

from ..customer_display_bus import CustomerDisplayBus, pole_lines

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/customer-display", tags=["customer-display"])

_PAGE = Path(__file__).resolve().parent.parent / "static" / "customer_display.html"
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


class UpdateReq(BaseModel):
    model_config = ConfigDict(extra="allow")

    order: dict = {}
    shop: str = ""
    labels: dict = {}
    pole: Optional[str] = None
    currency: str = ""


def _bus(request: Request) -> CustomerDisplayBus:
    bus = getattr(request.app.state, "customer_display_bus", None)
    if bus is None:
        bus = request.app.state.customer_display_bus = CustomerDisplayBus()
    return bus


def _check(channel: str) -> str:
    if not _CHANNEL_RE.match(channel):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Channel: letters, digits, . _ - (max 64)")
    return channel


async def _write_pole(request: Request, pole: str, payload: dict) -> Optional[str]:
    """Двата реда на полюсния дисплей; грешката се връща, не се хвърля."""
    reg = getattr(request.app.state, "display_registry", None)
    if reg is None or not reg.has(pole):
        return f"Display {pole!r} not found"
    cfg = reg.get(pole).config
    width = getattr(cfg, "chars_per_line", 20) or 20
    encoding = getattr(cfg, "encoding", "cp437") or "cp437"
    top, bottom = pole_lines(payload, width, encoding)
    try:
        async with reg.with_display(pole) as d:
            await asyncio.to_thread(d.display_two_lines, top, bottom)
        return None
    except Exception as exc:  # дисплеят не спира продажбата
        _logger.warning("customer display: pole %s failed: %s", pole, exc)
        return str(exc)


@router.get("")
@router.get("/", include_in_schema=False)
async def list_channels(request: Request):
    return {
        name: {"version": ch.version, "updatedAt": ch.updated_at, "listeners": ch.listeners}
        for name, ch in _bus(request).channels().items()
    }


@router.post("/{channel}")
async def update(channel: str, body: UpdateReq, request: Request):
    _check(channel)
    payload = body.model_dump()
    pole = payload.pop("pole", None)
    snap = _bus(request).channel(channel).publish(payload)
    result = {"ok": True, "version": snap["version"], "listeners": _bus(request).channel(channel).listeners}
    if pole:
        error = await _write_pole(request, pole, payload)
        result["pole"] = {"ok": error is None, **({"error": error} if error else {})}
    return result


@router.get("/{channel}/state")
async def state(channel: str, request: Request):
    _check(channel)
    return _bus(request).channel(channel).snapshot()


@router.get("/{channel}/events")
async def events(channel: str, request: Request):
    """SSE: първо текущото състояние, после всяко обновяване; ping на 15 s."""
    _check(channel)
    ch = _bus(request).channel(channel)

    async def stream():
        import json

        queue = ch.subscribe()
        try:
            yield f"event: update\ndata: {json.dumps(ch.snapshot(), ensure_ascii=False)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    message = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield f"event: update\ndata: {message}\n\n"
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            ch.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/{channel}")
async def page(channel: str):
    _check(channel)
    return FileResponse(_PAGE, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})
