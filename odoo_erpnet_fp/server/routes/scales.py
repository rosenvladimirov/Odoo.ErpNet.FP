"""
Extension API for weighing scales.

Not part of ErpNet.FP. URL prefix is `/scales`. JSON-only.

Endpoints:
  GET  /scales                  — list configured scales
  GET  /scales/{id}             — info (driver, port)
  GET  /scales/{id}/weight      — single weight read (kg)
  GET  /scales/{id}/probe       — quick echo health-check
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/scales", tags=["scales"])


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class ScaleInfoResp(_CamelModel):
    id: str
    driver: str
    # 🚨 Крайната точка, НЕ суровият `port`. При мрежова везна Odoo подава
    # порта като число и обявеният тук `str` го отхвърляше — списъкът и
    # информацията връщаха 500, докато четенето си работеше.
    port: Optional[str] = None
    transport: Optional[str] = None
    host: Optional[str] = None


class WeightReadResp(_CamelModel):
    ok: bool = True
    weight_kg: Optional[float] = Field(None, alias="weightKg")
    status: list[str] = []
    error: Optional[str] = None
    # 🔑 Кой е отговорил. Адресът е идентичността на станцията — той е
    # закачен за желязото, докато `id` на везната е произволно име в
    # конфигурацията. Слушалката филтрира по него, затова пътува с
    # всяко четене, не само в `/scales`.
    scale_id: Optional[str] = Field(None, alias="scaleId")
    host: Optional[str] = None


def _scale_registry(request: Request):
    return request.app.state.scale_registry


# 🔑 Типът НЕ е свободен избор. `l10n_bg_live_refresh` вече го разпознава и
# го префирва като `SCALE_READ` на `env.bus` (live_refresh_service.js), а
# `scale_handler.js` чака `data` във вида `{weight, unit, stable}`. Всеки
# друг низ би минал по канала и не би задействал НИЩО — мълчаливо, точно
# като липсващ вид в `PUSH_CONFIG_AC_KINDS`.
WEIGHT_EVENT_TYPE = "scale.weighed"


def _bus_client(request: Request):
    """Кешираният bus-inject клиент, или None ако не е конфигуриран.

    Строи се лениво и се пази на `app.state`: `from_app` чете тайната от
    диска, а това не бива да става на всяко мерене.
    """
    app = request.app
    client = getattr(app.state, "scale_bus_client", None)
    if client is not None:
        return client or None
    from ...clients.bus_inject import BusInjectClient
    client = BusInjectClient.from_app(app)
    # Пази и отрицателния отговор (False), за да не се опитва пак.
    app.state.scale_bus_client = client or False
    return client


def _weight_event_data(scale_id: str, cfg, reading) -> dict:
    """Тялото на събитието, във вида, който Odoo вече чака.

    `weight`/`unit`/`stable` идват от договора на `scale_handler.js`.
    Драйверите връщат винаги килограми, затова `unit` е фиксирана.

    `host` е добавката: адресът е идентичността на станцията, тъй че
    слушалката на работната карта приема само своето. `id` в конфига е
    произволно име и не става за това.
    """
    return {
        "weight": reading.weight_kg,
        "unit": "kg",
        # Драйверът връща `ok=True` САМО за стабилно четене — нестабилното
        # идва като `ok=False` със статус „Scale unstable".
        "stable": bool(reading.ok),
        "scale_id": scale_id,
        "host": cfg.host or "",
        "driver": cfg.driver,
        "status": list(reading.status),
    }


def _schedule_emit(request: Request, scale_id: str, cfg, data: dict):
    """Пуска събитието НАСТРАНИ и се връща веднага.

    🚨 `BusInjectClient.emit` е синхронен httpx. Извикан направо в async
    маршрут, той блокира event loop-а на ЦЯЛОТО прокси, докато трае
    заявката към Odoo — а тя минава през Cloudflare и мери в секунди.
    Измерено на живо: мерене от ~200 ms стана 3158 ms, при това с
    достъпа и CFX-а спрели зад него.

    Теглото е отговорът към оператора; известяването на Odoo е странична
    последица и няма право да го бави.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # Извън цикъл (тест) — върши го направо.
        _emit_weight(request, scale_id, cfg, data)
        return
    task = loop.create_task(
        asyncio.to_thread(_emit_weight, request, scale_id, cfg, data))
    # Задача без държана референция може да бъде събрана от GC преди да
    # е свършила. Пазим я, докато приключи.
    pending = getattr(request.app.state, "scale_emit_tasks", None)
    if pending is None:
        pending = set()
        request.app.state.scale_emit_tasks = pending
    pending.add(task)
    task.add_done_callback(pending.discard)


def _emit_weight(request: Request, scale_id: str, cfg, data: dict):
    """Пуска събитие към Odoo. Никога не вдига.

    Меренето е първичното — ако каналът към Odoo е паднал, операторът
    пак трябва да получи теглото си. Затова провалът само се логва.
    """
    try:
        client = _bus_client(request)
        if client is None:
            return
        client.emit(
            WEIGHT_EVENT_TYPE,
            device=scale_id,
            device_kind="scale",
            data=data,
        )
    except Exception:  # noqa: BLE001
        _logger.warning("scale %s: bus emit failed", scale_id, exc_info=True)


def _require(request: Request, id: str):
    reg = _scale_registry(request)
    if reg is None or not reg.has(id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Scale {id!r} not found"
        )
    return reg


@router.get("", response_model=dict[str, ScaleInfoResp])
@router.get("/", include_in_schema=False)
async def list_scales(request: Request):
    reg = _scale_registry(request)
    if reg is None:
        return {}
    return {
        sid: _info(sid, entry.config) for sid, entry in reg.scales.items()
    }


def _info(sid: str, cfg) -> "ScaleInfoResp":
    endpoint = cfg.endpoint()
    return ScaleInfoResp(
        id=sid,
        driver=cfg.driver,
        port=None if endpoint is None else str(endpoint),
        transport=cfg.transport,
        host=cfg.host,
    )


@router.get("/{id}", response_model=ScaleInfoResp)
async def scale_info(id: str, request: Request):
    reg = _require(request, id)
    return _info(id, reg.get(id).config)


@router.get("/{id}/weight", response_model=WeightReadResp)
async def scale_weight(id: str, request: Request):
    reg = _require(request, id)
    # Адресът пътува и при провал — иначе консуматор, който подрежда
    # четенията по станция, няма къде да сложи неуспешното.
    cfg = reg.get(id).config
    host = cfg.host
    from .. import metrics as _m
    try:
        async with reg.with_scale(id) as sc:
            reading = await asyncio.to_thread(sc.read_weight)
    except Exception as exc:
        _logger.exception("scale weight read failed for %s", id)
        try:
            _m.scale_reads_total.labels(scale_id=id, outcome="unreachable").inc()
        except Exception:
            pass
        _schedule_emit(request, id, cfg, {
            "weight": None, "unit": "kg", "stable": False,
            "scale_id": id, "host": cfg.host or "", "driver": cfg.driver,
            "status": [], "error": str(exc),
        })
        return WeightReadResp(
            ok=False, error=str(exc), scale_id=id, host=host)

    if reading.ok:
        try:
            _m.scale_reads_total.labels(scale_id=id, outcome="success").inc()
            if reading.weight_kg is not None:
                _m.scale_last_weight_kg.labels(scale_id=id).set(reading.weight_kg)
        except Exception:
            pass
    else:
        outcome = "unstable" if "unstable" in " ".join(reading.status).lower() else "error"
        try:
            _m.scale_reads_total.labels(scale_id=id, outcome=outcome).inc()
        except Exception:
            pass
    _schedule_emit(request, id, cfg, _weight_event_data(id, cfg, reading))
    return WeightReadResp(
        ok=reading.ok,
        weight_kg=reading.weight_kg,
        status=reading.status,
        scale_id=id,
        host=host,
    )


@router.get("/{id}/probe")
async def scale_probe(id: str, request: Request):
    reg = _require(request, id)
    try:
        async with reg.with_scale(id) as sc:
            ok = await asyncio.to_thread(sc.probe)
        return {"ok": bool(ok)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
