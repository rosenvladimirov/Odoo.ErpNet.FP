"""ZEN graph sync endpoints — handles inbound graph push от Odoo + provides
local evaluate API за callers който искат offline решение.

Routes:
- POST /zen/graphs/sync  — Odoo push на ZEN graph (HMAC-signed body);
                            persists to GraphStore.
- GET  /zen/graphs       — list loaded graphs (debug).
- POST /access/<point_id>/evaluate — offline decision endpoint (Phase 3
                                      hook; не активен ако zen-engine
                                      няма).
"""

from __future__ import annotations

import hmac
import hashlib
import json
import logging
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

from ..access_decision import (
    BUILDER_VERSION, GraphStore, ZenLocalRunner, build_context,
)

_logger = logging.getLogger(__name__)


_graph_store: Optional[GraphStore] = None


def _store(request: Request) -> GraphStore:
    """Singleton GraphStore per app."""
    global _graph_store
    if _graph_store is None:
        _graph_store = GraphStore()
    return _graph_store


def _verify_hmac(body: bytes, signature: str, secret: str) -> bool:
    if not signature or not secret:
        return False
    expected = hmac.new(
        secret.encode("utf-8"), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature)


class GraphSyncReq(BaseModel):
    code: str
    domain: str
    version: int
    graph: dict
    company_id: int | None = None


router = APIRouter(prefix="/zen", tags=["zen"])


@router.post("/graphs/sync")
async def sync_graph(
    payload: GraphSyncReq,
    request: Request,
    x_signature: str | None = Header(default=None,
                                       alias="X-Signature"),
):
    """Odoo push ZEN graph. HMAC verified против registry_secret.
    Idempotent (overwrites existing version)."""
    # Locate registry secret (from registry module — already manages
    # the proxy.registry_secret stored at /app/data/registry_secret)
    secret = None
    try:
        from .. import registry
        secret = registry._load_persistent_secret()  # type: ignore[attr-defined]
    except Exception as e:  # noqa: BLE001
        _logger.warning("zen_sync: registry secret unavailable: %s", e)
    if secret:
        raw = await request.body()
        if not _verify_hmac(raw, x_signature or "", secret):
            raise HTTPException(status_code=403, detail="bad HMAC")
    sha = _store(request).save(payload.code, payload.version, payload.graph)
    _logger.info(
        "ZEN graph synced: code=%s version=%d sha256=%s",
        payload.code, payload.version, sha[:12])
    return {
        "ok": True,
        "code": payload.code,
        "version": payload.version,
        "sha256": sha,
        "builder_version": BUILDER_VERSION,
    }


@router.get("/graphs")
def list_graphs(request: Request):
    """List loaded graphs (debug). Returns [{code, version, sha256}, ...]."""
    return {
        "graphs": _store(request).list_loaded(),
        "builder_version": BUILDER_VERSION,
        "zen_engine_available": ZenLocalRunner.is_available(),
    }


class EvaluateReq(BaseModel):
    """Offline access decision request от downstream caller (camera,
    Polimex pump, etc.). Caller passes everything builder needs since
    proxy doesn't hold perimeter/occupancy DB."""
    code: str  # ZEN graph code (e.g. 'access_default')
    control_point: dict
    credential: dict | None = None
    signal_matrix: dict | None = None
    prev_event: dict | None = None
    perimeter_chain: list[str] | None = None
    parent_present: bool = False
    within_window: bool = True
    tolerance_minutes: int = 15


@router.post("/access/evaluate")
def evaluate_access(req: EvaluateReq, request: Request):
    """Evaluate access decision OFFLINE. Returns:
      {ok, result, version, builder_version, fail_secure: bool}

    fail_secure=True ако зен евал не може (zen-engine missing, graph
    not loaded, eval exception) — caller MUST treat as DENY.
    """
    store = _store(request)
    graph = store.load(req.code)
    meta = store.load_meta(req.code) or {}
    if not graph:
        return {
            "ok": False,
            "result": None,
            "version": None,
            "builder_version": BUILDER_VERSION,
            "fail_secure": True,
            "reason": f"graph '{req.code}' not loaded",
        }
    if not ZenLocalRunner.is_available():
        return {
            "ok": False,
            "result": None,
            "version": meta.get("version"),
            "builder_version": BUILDER_VERSION,
            "fail_secure": True,
            "reason": "zen-engine not installed",
        }
    context = build_context(
        control_point=req.control_point,
        credential=req.credential,
        signal_matrix=req.signal_matrix,
        prev_event=req.prev_event,
        perimeter_chain=req.perimeter_chain,
        parent_present=req.parent_present,
        within_window=req.within_window,
        tolerance_minutes=req.tolerance_minutes,
    )
    result = ZenLocalRunner.evaluate(graph, context)
    if result is None:
        return {
            "ok": False,
            "result": None,
            "version": meta.get("version"),
            "builder_version": BUILDER_VERSION,
            "fail_secure": True,
            "reason": "evaluation failed",
        }
    return {
        "ok": True,
        "result": result,
        "context": context,
        "version": meta.get("version"),
        "builder_version": BUILDER_VERSION,
        "fail_secure": False,
    }
