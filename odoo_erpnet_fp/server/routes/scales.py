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
    port: Optional[str] = None


class WeightReadResp(_CamelModel):
    ok: bool = True
    weight_kg: Optional[float] = Field(None, alias="weightKg")
    status: list[str] = []
    error: Optional[str] = None


def _scale_registry(request: Request):
    return request.app.state.scale_registry


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
        sid: ScaleInfoResp(
            id=sid, driver=entry.config.driver, port=entry.config.port
        )
        for sid, entry in reg.scales.items()
    }


@router.get("/{id}", response_model=ScaleInfoResp)
async def scale_info(id: str, request: Request):
    reg = _require(request, id)
    cfg = reg.get(id).config
    return ScaleInfoResp(id=id, driver=cfg.driver, port=cfg.port)


@router.get("/{id}/weight", response_model=WeightReadResp)
async def scale_weight(id: str, request: Request):
    reg = _require(request, id)
    try:
        async with reg.with_scale(id) as sc:
            reading = await asyncio.to_thread(sc.read_weight)
        return WeightReadResp(
            ok=reading.ok,
            weight_kg=reading.weight_kg,
            status=reading.status,
        )
    except Exception as exc:
        _logger.exception("scale weight read failed for %s", id)
        return WeightReadResp(ok=False, error=str(exc))


@router.get("/{id}/probe")
async def scale_probe(id: str, request: Request):
    reg = _require(request, id)
    try:
        async with reg.with_scale(id) as sc:
            ok = await asyncio.to_thread(sc.probe)
        return {"ok": bool(ok)}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
