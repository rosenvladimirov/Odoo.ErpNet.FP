# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.

"""MQTT subscriber status & health route.

Read-only — Odoo's operator UI / monitoring scrapes /mqtt/status to see
whether each broker subscription is up. Each subscriber runs in its own
background thread; this route just reports per-broker telemetry.

Response shape (R5+, multi-broker):

    {
      "brokers": [
        {"name": "parking", "enabled": true, "running": true,
         "connected": true, "host": "p.local", "port": 1883,
         "topics": ["lpr", "lpr/+"], "messages_received": 42, ...},
        {"name": "warehouse", ...}
      ],
      "enabled_count": 2, "running_count": 2
    }

When no brokers are configured, returns
`{"brokers": [], "enabled_count": 0, "running_count": 0}`.
"""

from fastapi import APIRouter, HTTPException, Request

router = APIRouter(prefix="/mqtt", tags=["mqtt"])


@router.get("/status")
async def status(request: Request) -> dict:
    reg = getattr(request.app.state, "mqtt_ingest_registry", None)
    if reg is None:
        return {"brokers": [], "enabled_count": 0, "running_count": 0}
    return reg.status()


@router.get("/status/{name}")
async def status_one(request: Request, name: str) -> dict:
    """Per-broker status — handy when an operator wants to drill into a
    specific subscriber without grep-ing the list."""
    reg = getattr(request.app.state, "mqtt_ingest_registry", None)
    if reg is None or name not in reg.specs:
        raise HTTPException(status_code=404, detail=f"unknown broker {name!r}")
    ingest = reg.ingests.get(name)
    if ingest is not None:
        return ingest.status()
    spec = reg.specs[name]
    return {
        "name": name,
        "enabled": spec.enabled,
        "running": False,
        "connected": False,
        "host": spec.host,
        "port": spec.port,
        "topics": list(spec.topics),
    }


@router.post("/{name}/start")
async def start_one(request: Request, name: str) -> dict:
    """Runtime START of one MQTT broker.

    Does NOT touch `config.d/mqtt.yaml` — purely in-process. At the
    next sync from Odoo (or proxy restart) the broker reverts to
    whatever `enabled:` says in the YAML. Debug convenience only.
    """
    reg = getattr(request.app.state, "mqtt_ingest_registry", None)
    if reg is None or name not in reg.specs:
        raise HTTPException(status_code=404, detail=f"unknown broker {name!r}")
    started = reg.start_one(name)
    ingest = reg.ingests.get(name)
    return {
        "ok": True,
        "started": started,
        "status": ingest.status() if ingest is not None else None,
    }


@router.post("/{name}/stop")
async def stop_one(request: Request, name: str) -> dict:
    """Runtime STOP of one MQTT broker (does NOT touch config on disk —
    see /mqtt/{name}/start docstring for rationale)."""
    reg = getattr(request.app.state, "mqtt_ingest_registry", None)
    if reg is None or name not in reg.specs:
        raise HTTPException(status_code=404, detail=f"unknown broker {name!r}")
    stopped = reg.stop_one(name)
    spec = reg.specs[name]
    return {
        "ok": True,
        "stopped": stopped,
        "status": {
            "name": name,
            "enabled": spec.enabled,
            "running": False,
            "connected": False,
            "host": spec.host,
            "port": spec.port,
            "topics": list(spec.topics),
        },
    }
