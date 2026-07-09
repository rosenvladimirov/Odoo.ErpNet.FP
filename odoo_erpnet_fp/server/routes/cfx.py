# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""CFX-IPC ingest status & test-injection routes.

Read-only status (Odoo's operator UI / monitoring scrapes /cfx/status to
see whether each CFX station's embedded CFXPlugin is up), plus a test /
replay injector that feeds a synthetic CFX event through the exact same
forward path (bus_inject live + signed audit POST) as a real machine.

Response shape (multi-station):

    {
      "stations": [
        {"name": "europlacer-1", "enabled": true, "running": true,
         "machine_kind": "europlacer", "cfx_handle": "Europlacer/RC/1",
         "endpoints": [{"transport": "broker", ...}, {"transport": "p2p", ...}],
         "messages_received": 42, "events_live_ok": 42, ...},
        ...
      ],
      "enabled_count": 1, "running_count": 1
    }
"""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(prefix="/cfx", tags=["cfx"])


@router.get("/status")
async def status(request: Request) -> dict:
    reg = getattr(request.app.state, "cfx_ingest_registry", None)
    if reg is None:
        return {"stations": [], "enabled_count": 0, "running_count": 0}
    return reg.status()


@router.get("/status/{name}")
async def status_one(request: Request, name: str) -> dict:
    reg = getattr(request.app.state, "cfx_ingest_registry", None)
    if reg is None or name not in reg.specs:
        raise HTTPException(status_code=404, detail=f"unknown CFX station {name!r}")
    ingest = reg.ingests.get(name)
    if ingest is not None:
        return ingest.status()
    spec = reg.specs[name]
    return {
        "name": name,
        "enabled": spec.enabled,
        "running": False,
        "machine_kind": spec.machine_kind,
        "cfx_handle": spec.cfx_handle,
    }


@router.post("/{name}/start")
async def start_one(request: Request, name: str) -> dict:
    """Runtime START of one CFX station — purely in-process (does NOT
    touch config.d/cfx.yaml). Reverts to `enabled:` on next sync/restart."""
    reg = getattr(request.app.state, "cfx_ingest_registry", None)
    if reg is None or name not in reg.specs:
        raise HTTPException(status_code=404, detail=f"unknown CFX station {name!r}")
    started = reg.start_one(name)
    ingest = reg.ingests.get(name)
    return {
        "ok": True,
        "started": started,
        "status": ingest.status() if ingest is not None else None,
    }


@router.post("/{name}/stop")
async def stop_one(request: Request, name: str) -> dict:
    """Runtime STOP of one CFX station (in-process only)."""
    reg = getattr(request.app.state, "cfx_ingest_registry", None)
    if reg is None or name not in reg.specs:
        raise HTTPException(status_code=404, detail=f"unknown CFX station {name!r}")
    stopped = reg.stop_one(name)
    spec = reg.specs[name]
    return {
        "ok": True,
        "stopped": stopped,
        "status": {
            "name": name,
            "enabled": spec.enabled,
            "running": False,
            "machine_kind": spec.machine_kind,
        },
    }


class CfxInjectReq(BaseModel):
    """Synthetic CFX event for test/replay via /cfx/{name}/inject."""
    message_name: str = "MaterialsInstalled"
    machine_kind: str | None = None       # default: the station's machine_kind
    cfx_handle: str | None = None
    transaction_id: str = ""
    wo_name: str = ""
    data: dict = {}
    counters: dict = {}
    station_state: str | None = None


@router.post("/{name}/inject")
async def cfx_inject(name: str, req: CfxInjectReq, request: Request) -> dict:
    """Feed a synthetic CFX event through the SAME forward path (live +
    audit) a real machine takes — for E2E replay / smoke tests without a
    broker. Requires the station's ingest instance to exist (start it
    first, or configure it enabled)."""
    reg = getattr(request.app.state, "cfx_ingest_registry", None)
    if reg is None or name not in reg.specs:
        raise HTTPException(status_code=404, detail=f"unknown CFX station {name!r}")
    ingest = reg.ingests.get(name)
    if ingest is None:
        # Lazily materialise one so replay works even when the station is
        # disabled / never connected to a broker.
        reg.start_one(name)
        ingest = reg.ingests.get(name)
    if ingest is None:
        raise HTTPException(status_code=409,
                            detail=f"CFX station {name!r} has no ingest instance")

    spec = reg.specs[name]
    from ...drivers.cfx.common import CfxEvent
    event = CfxEvent(
        machine_kind=req.machine_kind or spec.machine_kind,
        message_name=req.message_name,
        cfx_handle=req.cfx_handle or spec.cfx_handle,
        transaction_id=req.transaction_id,
        wo_name=req.wo_name,
        data=req.data,
        counters=req.counters,
        station_state=req.station_state,
        timestamp=datetime.now(),
        source="inject",
    )
    ingest.forward(event)
    return {"ok": True, "station": name, "type": event.bus_type,
            "wo_name": event.wo_name}
