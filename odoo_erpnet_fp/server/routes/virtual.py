"""
Управлението на виртуалния апарат (`transport: virtual`) — за тестове и
пясъчник: дневникът на „отпечатаните“ документи, инжектирани грешки и
нулиране. Отговаря само за принтер с виртуален транспорт; за истински
апарат е 404 — грешка не може да се инжектира в жива каса.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel

from ...drivers.fiscal.datecs_pm import transport_virtual as tv

router = APIRouter(prefix="/printers/{id}/virtual", tags=["virtual printer"])


class FaultsBody(BaseModel):
    faults: list[str] = []


def _require_virtual(request: Request, id: str):
    registry = request.app.state.registry
    if not registry.has(id) or registry.get(id).config.transport != "virtual":
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Virtual printer {id!r} not found")
    return registry.get(id).config


def _snapshot(state: tv.VirtualPmState) -> dict:
    with state.lock:
        return {
            "faults": sorted(state.faults),
            "slipNumber": state.slip_no,
            "zReportNumber": state.z_no,
            "open": state.open,
            "documents": list(state.journal),
        }


@router.get("")
async def virtual_state(id: str, request: Request) -> dict:
    cfg = _require_virtual(request, id)
    return _snapshot(tv.get_state(id, cfg.extras.get("virtual_journal")))


@router.put("/faults")
async def virtual_faults(id: str, body: FaultsBody, request: Request) -> dict:
    _require_virtual(request, id)
    try:
        faults = tv.set_faults(id, body.faults)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
    return {"faults": sorted(faults), "known": sorted(tv.KNOWN_FAULTS)}


@router.post("/reset")
async def virtual_reset(id: str, request: Request) -> dict:
    cfg = _require_virtual(request, id)
    tv.reset_state(id)
    return _snapshot(tv.get_state(id, cfg.extras.get("virtual_journal"), cfg.extras.get("virtual_faults")))
