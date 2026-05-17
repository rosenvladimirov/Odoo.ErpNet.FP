"""
Access-control API (Phase B) — barrier / relay / turnstile.

SYNCHRONOUS request→response, zero queue latency — Odoo POSTs the
already-authorised decision and gets a definitive result, the same
channel pattern a barcode read uses (NOT the 60 s Fleet
command-queue; that exists only as a secondary remote-management
path in registry._execute_command kind=access_open).

The access DECISION is taken in Odoo (Channel-1 ⊕ Channel-2,
fail-secure). This endpoint only EXECUTES it. No call → barrier stays
shut.

Endpoints:
  GET  /access                  — list controllers
  GET  /access/{id}             — controller info
  POST /access/{id}/open        — grant (body: {"seconds": 3} → pulse)
  POST /access/{id}/deny        — explicit close / deny
  GET  /access/{id}/status      — best-effort state
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

_logger = logging.getLogger(__name__)
router = APIRouter(prefix="/access", tags=["access"])


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="allow")


class AccessInfoResp(_CamelModel):
    id: str
    driver: str
    fail_secure: bool = Field(True, alias="failSecure")


class OpenReq(BaseModel):
    # Празно/0 → latched open; иначе momentary pulse за толкова секунди.
    seconds: Optional[float] = None


def _registry(request: Request):
    return getattr(request.app.state, "access_registry", None)


def _require(request: Request, id: str):
    reg = _registry(request)
    if reg is None or not reg.has(id):
        raise HTTPException(status.HTTP_404_NOT_FOUND,
                            f"Access controller {id!r} not found")
    return reg


@router.get("", response_model=dict[str, AccessInfoResp])
@router.get("/", include_in_schema=False)
async def list_access(request: Request):
    reg = _registry(request)
    if reg is None:
        return {}
    return {
        aid: AccessInfoResp(id=aid, driver=e.config.driver,
                            fail_secure=e.config.fail_secure)
        for aid, e in reg.access.items()
    }


@router.get("/{id}", response_model=AccessInfoResp)
async def access_info(id: str, request: Request):
    reg = _require(request, id)
    e = reg.get(id)
    return AccessInfoResp(id=id, driver=e.config.driver,
                          fail_secure=e.config.fail_secure)


@router.post("/{id}/open")
async def access_open(id: str, request: Request,
                      req: OpenReq | None = None):
    """Execute an Odoo-authorised OPEN. Synchronous — returns the
    actuator result. `{"seconds": N}` → momentary pulse."""
    reg = _require(request, id)
    secs = req.seconds if req else None
    try:
        async with reg.with_access(id) as act:
            res = await asyncio.to_thread(act.open, secs)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Access {id!r} open failed: {exc}",
        ) from exc
    return res.to_json()


@router.post("/{id}/deny")
async def access_deny(id: str, request: Request):
    """Explicit close / deny (safe no-op-ish on a plain barrier)."""
    reg = _require(request, id)
    try:
        async with reg.with_access(id) as act:
            res = await asyncio.to_thread(act.deny)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Access {id!r} deny failed: {exc}",
        ) from exc
    return res.to_json()


@router.get("/{id}/status")
async def access_status(id: str, request: Request):
    reg = _require(request, id)
    try:
        async with reg.with_access(id) as act:
            res = await asyncio.to_thread(act.status)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(
            status.HTTP_502_BAD_GATEWAY,
            f"Access {id!r} status failed: {exc}",
        ) from exc
    return res.to_json()
