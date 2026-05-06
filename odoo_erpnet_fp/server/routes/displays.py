"""
Extension API for customer-facing pole displays (VFD/LCD).

Not part of ErpNet.FP. URL prefix is `/displays`. JSON-only.

Endpoints:
  GET  /displays                    — list configured displays
  GET  /displays/{id}               — info + capabilities
  POST /displays/{id}/clear         — clear screen, cursor → home
  POST /displays/{id}/initialize    — full reset (ESC @)
  POST /displays/{id}/text          — body: {line, text}
  POST /displays/{id}/two-lines     — body: {top, bottom}
  POST /displays/{id}/total         — body: {label, amount, currency?}
  POST /displays/{id}/change        — body: {amount, currency?, label?}
  POST /displays/{id}/cursor        — body: {col, row}
  POST /displays/{id}/brightness    — body: {level: 0..4}
  POST /displays/{id}/blink         — body: {n: 0..255}
  POST /displays/{id}/self-test     — trigger on-device self-test (~20 s)
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/displays", tags=["displays"])


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class DisplayInfoResp(_CamelModel):
    id: str
    driver: str
    port: Optional[str] = None
    encoding: str
    chars_per_line: int = Field(alias="charsPerLine")
    lines: int


class TextLineReq(_CamelModel):
    line: int = Field(..., ge=1, le=4)
    text: str


class TwoLinesReq(_CamelModel):
    top: str = ""
    bottom: str = ""


class TotalReq(_CamelModel):
    label: str
    amount: Decimal
    currency: str = ""


class ChangeReq(_CamelModel):
    amount: Decimal
    currency: str = ""
    label: str = "ЗА ВРЪЩАНЕ"


class CursorReq(_CamelModel):
    col: int = Field(..., ge=1, le=40)
    row: int = Field(..., ge=1, le=4)


class BrightnessReq(_CamelModel):
    level: int = Field(..., ge=0, le=4)


class BlinkReq(_CamelModel):
    n: int = Field(..., ge=0, le=255)


class OkResp(_CamelModel):
    ok: bool = True
    error: Optional[str] = None


def _registry(request: Request):
    return request.app.state.display_registry


def _require(request: Request, id: str):
    reg = _registry(request)
    if reg is None or not reg.has(id):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, f"Display {id!r} not found"
        )
    return reg


@router.get("", response_model=dict[str, DisplayInfoResp])
@router.get("/", include_in_schema=False)
async def list_displays(request: Request):
    reg = _registry(request)
    if reg is None:
        return {}
    return {
        did: DisplayInfoResp(
            id=did,
            driver=entry.config.driver,
            port=entry.config.port,
            encoding=entry.config.encoding,
            chars_per_line=entry.config.chars_per_line,
            lines=entry.config.lines,
        )
        for did, entry in reg.displays.items()
    }


@router.get("/{id}", response_model=DisplayInfoResp)
async def display_info(id: str, request: Request):
    reg = _require(request, id)
    cfg = reg.get(id).config
    return DisplayInfoResp(
        id=id,
        driver=cfg.driver,
        port=cfg.port,
        encoding=cfg.encoding,
        chars_per_line=cfg.chars_per_line,
        lines=cfg.lines,
    )


async def _run(reg, id: str, fn) -> OkResp:
    try:
        async with reg.with_display(id) as d:
            await asyncio.to_thread(fn, d)
        return OkResp(ok=True)
    except Exception as exc:
        _logger.exception("display op failed for %s", id)
        return OkResp(ok=False, error=str(exc))


@router.post("/{id}/clear", response_model=OkResp)
async def clear(id: str, request: Request):
    reg = _require(request, id)
    return await _run(reg, id, lambda d: d.clear())


@router.post("/{id}/initialize", response_model=OkResp)
async def initialize(id: str, request: Request):
    reg = _require(request, id)
    return await _run(reg, id, lambda d: d.initialize())


@router.post("/{id}/text", response_model=OkResp)
async def text(id: str, body: TextLineReq, request: Request):
    reg = _require(request, id)
    return await _run(reg, id, lambda d: d.display_line(body.line, body.text))


@router.post("/{id}/two-lines", response_model=OkResp)
async def two_lines(id: str, body: TwoLinesReq, request: Request):
    reg = _require(request, id)
    return await _run(reg, id, lambda d: d.display_two_lines(body.top, body.bottom))


@router.post("/{id}/total", response_model=OkResp)
async def total(id: str, body: TotalReq, request: Request):
    reg = _require(request, id)
    return await _run(
        reg, id, lambda d: d.display_total(body.label, body.amount, body.currency)
    )


@router.post("/{id}/change", response_model=OkResp)
async def change(id: str, body: ChangeReq, request: Request):
    reg = _require(request, id)
    return await _run(
        reg,
        id,
        lambda d: d.display_change(body.amount, body.currency, body.label),
    )


@router.post("/{id}/cursor", response_model=OkResp)
async def cursor(id: str, body: CursorReq, request: Request):
    reg = _require(request, id)
    return await _run(reg, id, lambda d: d.set_cursor(body.col, body.row))


@router.post("/{id}/brightness", response_model=OkResp)
async def brightness(id: str, body: BrightnessReq, request: Request):
    reg = _require(request, id)
    return await _run(reg, id, lambda d: d.set_brightness(body.level))


@router.post("/{id}/blink", response_model=OkResp)
async def blink(id: str, body: BlinkReq, request: Request):
    reg = _require(request, id)
    return await _run(reg, id, lambda d: d.set_blink(body.n))


@router.post("/{id}/self-test", response_model=OkResp)
async def self_test(id: str, request: Request):
    reg = _require(request, id)
    return await _run(reg, id, lambda d: d.self_test())
