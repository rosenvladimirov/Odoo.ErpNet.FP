"""
Extension API for cameras (live stream + LPR events).

Push model identical to `/readers/*`: clients connect once via
WebSocket or SSE and receive each recognised plate; long-poll HTTP is
the fallback. Live video is delegated to the go2rtc sibling — this API
only exposes its URLs + a JPEG snapshot passthrough.

Endpoints:
  GET  /cameras                       — list configured cameras
  GET  /cameras/{id}                  — info + go2rtc stream URLs
  GET  /cameras/{id}/last             — last recognised plate (history)
  GET  /cameras/{id}/next?timeout=30  — long-poll next plate (HTTP)
  GET  /cameras/{id}/events           — Server-Sent Events stream
  WS   /cameras/{id}/ws               — WebSocket push (recommended)
  GET  /cameras/{id}/snapshot         — current JPEG still (via go2rtc)
  GET  /cameras/{id}/stream           — redirect to the go2rtc viewer
  POST /cameras/{id}/inject           — external/test plate injection
  POST /cameras/{id}/reset            — re-push stream def to go2rtc
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

import httpx
from fastapi import (
    APIRouter,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/cameras", tags=["cameras"])


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class CameraInfoResp(_CamelModel):
    id: str
    driver: str
    lpr_engine: str = Field("none", alias="lprEngine")
    running: bool = False
    subscriber_count: int = Field(0, alias="subscriberCount")
    webhooks: int = 0
    stream_urls: dict[str, str] = Field(default_factory=dict, alias="streamUrls")


class PlateResp(_CamelModel):
    camera_id: str = Field(..., alias="cameraId")
    plate: str
    confidence: float
    timestamp: str
    source: str


def _camera_registry(request: Request):
    return getattr(request.app.state, "camera_registry", None)


def _require(request: Request, id: str):
    reg = _camera_registry(request)
    if reg is None or not reg.has(id):
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Camera {id!r} not found")
    return reg


def _is_running(entry) -> bool:
    if entry.config.driver == "external":
        return True
    return entry.driver.is_running if entry.driver else False


def _info(entry) -> CameraInfoResp:
    drv = entry.driver
    return CameraInfoResp(
        id=entry.config.id,
        driver=entry.config.driver,
        lpr_engine=(entry.config.lpr_engine if entry.config.lpr_enabled else "none"),
        running=_is_running(entry),
        subscriber_count=entry.bus.subscriber_count,
        webhooks=len(entry.config.webhooks),
        stream_urls=drv.stream_urls() if drv else {},
    )


@router.get("", response_model=dict[str, CameraInfoResp])
@router.get("/", include_in_schema=False)
async def list_cameras(request: Request):
    reg = _camera_registry(request)
    if reg is None:
        return {}
    return {cid: _info(entry) for cid, entry in reg.cameras.items()}


@router.get("/{id}", response_model=CameraInfoResp)
async def camera_info(id: str, request: Request):
    reg = _require(request, id)
    return _info(reg.get(id))


@router.get("/{id}/last", response_model=Optional[PlateResp])
async def camera_last(id: str, request: Request):
    """Last recognised plate (history). Useful for UI bootstraps."""
    reg = _require(request, id)
    last = reg.get_bus(id).last_event()
    if last is None:
        return None
    return PlateResp(**last.to_json())


@router.get("/{id}/snapshot")
async def camera_snapshot(id: str, request: Request):
    """Current JPEG still — proxied from go2rtc `/api/frame.jpeg`."""
    reg = _require(request, id)
    entry = reg.get(id)
    if entry.driver is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Camera {id!r} has no live driver",
        )
    try:
        jpeg = await asyncio.to_thread(entry.driver.snapshot)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Snapshot failed for camera {id!r}: {exc}",
        ) from exc
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@router.get("/{id}/stream")
async def camera_stream(id: str, request: Request):
    """Redirect to the go2rtc viewer page for this camera."""
    reg = _require(request, id)
    entry = reg.get(id)
    urls = entry.driver.stream_urls() if entry.driver else {}
    page = urls.get("page")
    if not page:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Camera {id!r} has no stream URL",
        )
    return RedirectResponse(page)


@router.get("/{id}/stream.mjpeg")
async def camera_stream_mjpeg(id: str, request: Request):
    """Same-origin MJPEG relay — pipes go2rtc's `/api/stream.mjpeg`
    through the proxy.

    `streamUrls` point at the proxy-internal `go2rtc_url` (e.g.
    `http://go2rtc:1984`), which the operator's browser can't reach.
    The proxy CAN reach it (same Docker network), so we relay the
    multipart JPEG stream here. Result: a real live `<img>` feed that
    works behind Traefik / Cloudflare with zero extra config and
    without exposing go2rtc publicly — same principle the rest of the
    dashboard already follows (everything same-origin).
    """
    reg = _require(request, id)
    entry = reg.get(id)
    # Релеят НАТОВАРВА фискалния процес с целия видео-поток → opt-in
    # само за LAN-only без публичен go2rtc. По подразбиране браузърът
    # тегли видеото ДИРЕКТНО от публичния go2rtc (stream_urls()).
    if not getattr(entry.config, "mjpeg_relay", False):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"MJPEG relay disabled for camera {id!r} (keeps proxy load "
            f"minimal). Stream directly from go2rtc: "
            f"{(entry.driver.stream_urls() if entry.driver else {}).get('page', '(set go2rtc_public_url)')}",
        )
    up = entry.driver.internal_mjpeg_url() if entry.driver else None
    if not up:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            f"Camera {id!r} has no MJPEG source",
        )
    client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=None))
    try:
        upstream = await client.send(
            client.build_request("GET", up), stream=True
        )
        upstream.raise_for_status()
    except Exception as exc:  # noqa: BLE001
        await client.aclose()
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"go2rtc MJPEG unreachable for camera {id!r}: {exc}",
        ) from exc

    ctype = upstream.headers.get(
        "content-type", "multipart/x-mixed-replace; boundary=frame"
    )

    async def body():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
            await client.aclose()

    return StreamingResponse(
        body(),
        media_type=ctype,
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )


class InjectReq(BaseModel):
    plate: str
    confidence: float = 1.0


@router.post("/{id}/inject", response_model=PlateResp)
async def camera_inject(id: str, req: InjectReq, request: Request):
    """External/test plate injection — publishes onto the same bus the
    LPR sampling loop uses, so all subscribers receive it identically.
    Handy for an external LPR pipeline or for QA without a real camera.
    """
    reg = _require(request, id)
    entry = reg.get(id)
    plate = (req.plate or "").strip().upper()
    if not plate:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Empty plate")
    from ...drivers.cameras.common import PlateEvent
    evt = PlateEvent(
        camera_id=id, plate=plate,
        confidence=float(req.confidence), source="inject",
    )
    entry.bus.publish_threadsafe(evt)
    return PlateResp(**evt.to_json())


import re as _re

from ...drivers.cameras.common import normalize_plate

_PLATE_KEY = _re.compile(r"plate|licens", _re.I)
_CONF_KEY = _re.compile(r"conf|score|reliab", _re.I)


def _scan(obj, found: dict) -> None:
    """Recursively pull the first plate-ish + confidence-ish leaf from a
    decoded JSON/dict tree (vendor-agnostic)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, (str, int, float)):
                ks = str(k)
                if "plate" not in found and _PLATE_KEY.search(ks) and str(v).strip():
                    found["plate"] = normalize_plate(str(v))
                elif "conf" not in found and _CONF_KEY.search(ks):
                    try:
                        c = float(v)
                        found["conf"] = c / 100.0 if c > 1.0 else c
                    except (TypeError, ValueError):
                        pass
            else:
                _scan(v, found)
    elif isinstance(obj, (list, tuple)):
        for it in obj:
            _scan(it, found)


@router.post("/{id}/plate")
async def camera_plate_listener(id: str, request: Request):
    """Vendor HTTP-listener ingestion — the most portable ANPR pattern.

    Most BG/EU cameras (Hikvision ISAPI, Dahua, Uniview, Vivotek,
    Provision) do NOT carry plate text over plain ONVIF — they POST it
    to an "alarm host / HTTP listener". Point that at this endpoint and
    the tolerant parser extracts plate+confidence from JSON, XML
    (Hikvision `EventNotificationAlert`) or multipart, then publishes
    to the SAME bus as everything else. Zero per-vendor config — only
    Profile-M cameras (Axis/Milesight/Hanwha) use the onvif_anpr path.
    """
    reg = _require(request, id)
    entry = reg.get(id)
    ctype = request.headers.get("content-type", "")
    found: dict = {}
    try:
        if ctype.startswith("multipart/"):
            form = await request.form()
            for v in form.values():
                if hasattr(v, "read"):  # file part
                    continue
                txt = str(v)
                try:
                    _scan(__import__("json").loads(txt), found)
                except Exception:  # noqa: BLE001
                    _scan_xml(txt, found)
                if found.get("plate"):
                    break
        else:
            raw = (await request.body()).decode("utf-8", "ignore")
            stripped = raw.lstrip()
            if stripped[:1] in ("{", "["):
                _scan(__import__("json").loads(raw), found)
            else:
                _scan_xml(raw, found)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"Unparseable plate payload: {exc}",
        ) from exc

    plate = (found.get("plate") or "").strip()
    if not plate:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "No plate field found in payload",
        )
    from ...drivers.cameras.common import PlateEvent
    evt = PlateEvent(
        camera_id=id, plate=plate,
        confidence=float(found.get("conf", 1.0)), source="camera-http",
    )
    entry.bus.publish_threadsafe(evt)
    return PlateResp(**evt.to_json())


def _scan_xml(text: str, found: dict) -> None:
    """Best-effort XML walk (Hikvision EventNotificationAlert etc.)."""
    try:
        import xml.etree.ElementTree as ET
        root = ET.fromstring(text)
    except Exception:  # noqa: BLE001
        return
    for el in root.iter():
        tag = el.tag.rsplit("}", 1)[-1]  # strip namespace
        val = (el.text or "").strip()
        if not val:
            continue
        if "plate" not in found and _PLATE_KEY.search(tag):
            found["plate"] = normalize_plate(val)
        elif "conf" not in found and _CONF_KEY.search(tag):
            try:
                c = float(val)
                found["conf"] = c / 100.0 if c > 1.0 else c
            except ValueError:
                pass


@router.post("/{id}/reset")
async def camera_reset(id: str, request: Request):
    """Re-push the stream definition to go2rtc (fallback button)."""
    reg = _require(request, id)
    entry = reg.get(id)
    reset_fn = getattr(entry.driver, "reset", None) if entry.driver else None
    if not callable(reset_fn):
        return {"ok": True, "id": id, "message": "no driver — nothing to reset"}
    await asyncio.to_thread(reset_fn)
    return {"ok": True, "id": id, "message": "go2rtc stream re-registered"}


class ControlReq(BaseModel):
    # action: relay | open | close | pulse | ptz
    action: str
    state: Optional[str] = None          # relay: active|inactive
    seconds: float = 2.0                 # pulse / open duration
    ptz_action: Optional[str] = None     # ptz: goto_preset|stop
    preset: Optional[str] = None


@router.post("/{id}/control")
async def camera_control(id: str, req: ControlReq, request: Request):
    """SYNCHRONOUS ONVIF control — zero queue latency.

    Single SOAP call to the camera's Device IO / PTZ, awaited and
    returned. This is the barrier/relay path Odoo drives the same way
    it reads a barcode (request→response), NOT the 60 s Fleet
    command-queue. The access *decision* is taken in Odoo
    (fail-secure); this only executes it.
    """
    reg = _require(request, id)
    entry = reg.get(id)
    drv = entry.driver
    if drv is None or not getattr(drv, "has_control", False):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"Camera {id!r} has no ONVIF control (set onvif_control: true)",
        )
    a = (req.action or "").lower()
    try:
        if a in ("open", "pulse"):
            res = await asyncio.to_thread(drv.pulse, req.seconds)
        elif a == "relay":
            res = await asyncio.to_thread(
                drv.relay, req.state or "active"
            )
        elif a == "close":
            res = await asyncio.to_thread(drv.relay, "inactive")
        elif a == "ptz":
            res = await asyncio.to_thread(
                drv.ptz, req.ptz_action or "stop", preset=req.preset
            )
        else:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"Unknown control action: {a!r}",
            )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"ONVIF control failed for camera {id!r}: {exc}",
        ) from exc
    return {"ok": True, "id": id, **(res or {})}


@router.get("/{id}/next", response_model=Optional[PlateResp])
async def camera_next(
    id: str,
    request: Request,
    timeout: float = Query(30.0, ge=0.1, le=120.0),
):
    """Long-poll next plate — HTTP fallback. 204 on timeout."""
    reg = _require(request, id)
    bus = reg.get_bus(id)
    queue = bus.subscribe()
    try:
        evt = await asyncio.wait_for(queue.get(), timeout=timeout)
        return PlateResp(**evt.to_json())
    except asyncio.TimeoutError:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    finally:
        bus.unsubscribe(queue)


@router.get("/{id}/events")
async def camera_events(id: str, request: Request):
    """SSE — `text/event-stream`. Browser EventSource compatible.

        const es = new EventSource('/cameras/gate1/events');
        es.addEventListener('plate', e => console.log(JSON.parse(e.data)));
    """
    reg = _require(request, id)
    bus = reg.get_bus(id)

    async def stream():
        queue = bus.subscribe()
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield (
                        "event: plate\ndata: "
                        f"{PlateResp(**evt.to_json()).model_dump_json(by_alias=True)}"
                        "\n\n"
                    )
                except asyncio.TimeoutError:
                    yield ": ping\n\n"
        finally:
            bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.websocket("/{id}/ws")
async def camera_ws(websocket: WebSocket, id: str):
    """WebSocket — lowest-latency push. Recommended for the access UI.

    Sends `{"type": "hello", "cameraId": "..."}` then
    `{"type": "plate", "cameraId": "...", "plate": "...", ...}` per
    recognition.
    """
    reg = getattr(websocket.app.state, "camera_registry", None)
    if reg is None or not reg.has(id):
        await websocket.close(code=4404, reason=f"Camera {id!r} not found")
        return
    bus = reg.get_bus(id)
    await websocket.accept()
    queue = bus.subscribe()
    try:
        await websocket.send_json({"type": "hello", "cameraId": id})
        while True:
            evt = await queue.get()
            await websocket.send_json({"type": "plate", **evt.to_json()})
    except WebSocketDisconnect:
        pass
    except Exception:
        _logger.exception("Camera %r WebSocket loop crashed", id)
    finally:
        bus.unsubscribe(queue)
