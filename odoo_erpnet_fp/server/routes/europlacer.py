# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""Europlacer material-order producer routes.

* ``POST /europlacer/{name}/order`` — Odoo submits a READY material-order
  XML; the proxy writes it into the machine's shared folder and (on a
  worker thread) waits for the ``.ans`` answer, retrying per the
  ``<Error>`` code table. Returns a fast **202** ack — the ``.ans`` wait
  can reach the machine's answer timeout (param 290), far beyond any HTTP
  timeout, so the terminal outcome is delivered out-of-band (bus_inject
  live ping + optional signed ``result_url`` POST) and is pollable via
  ``GET /europlacer/{name}/order/{order_id}``.
* Read-only status endpoints mirror ``/cfx/status``.

Auth: the ``/order`` submit is HMAC-guarded exactly like the other
Odoo→proxy business routes (``shifts`` / ``pos_order_storno``) —
``X-Registry-Signature`` = HMAC-SHA256(body, ``server.iot_setup.token``).
The read-only status/poll endpoints are open (monitoring), like
``/cfx/status``.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Optional

from fastapi import APIRouter, Header, HTTPException, Query, Request, Response

from ..odoo_forwarder import verify_signature

router = APIRouter(prefix="/europlacer", tags=["europlacer"])


# ─── auth (Odoo → proxy) — mirrors shifts.py ────────────────────────────


def _shared_secret(cfg) -> Optional[str]:
    return getattr(getattr(cfg, "iot_setup", None), "token", None) or None


async def _verify_request(request: Request, signature: Optional[str],
                          raw_body: bytes) -> None:
    cfg = request.app.state.config.server
    secret = _shared_secret(cfg)
    if not secret:
        raise HTTPException(
            status_code=503, detail="Proxy not paired (iot_setup.token unset)")
    if not raw_body:
        raw_body = request.url.path.encode("utf-8")
    if not verify_signature(raw_body, secret, signature or ""):
        raise HTTPException(status_code=401, detail="X-Registry-Signature mismatch")


# ─── status (read-only, open) ───────────────────────────────────────────


@router.get("/status")
async def status(request: Request) -> dict:
    reg = getattr(request.app.state, "europlacer_registry", None)
    if reg is None:
        return {"stations": [], "enabled_count": 0, "running_count": 0}
    return reg.status()


@router.get("/status/{name}")
async def status_one(request: Request, name: str) -> dict:
    reg = getattr(request.app.state, "europlacer_registry", None)
    if reg is None or name not in reg.specs:
        raise HTTPException(status_code=404,
                            detail=f"unknown Europlacer station {name!r}")
    ingest = reg.ingests.get(name)
    if ingest is not None:
        return ingest.status()
    spec = reg.specs[name]
    return {
        "name": name,
        "enabled": spec.enabled,
        "running": False,
        "machine_kind": spec.machine_kind,
        "order_dir": spec.order_dir,
    }


# ─── live file-system view (read-only, open) ────────────────────────────
#
# What the producer WRITES (order XML) and what the machine RETURNS (.ans),
# straight off the shared folder, for a live dashboard tab. Top-level only
# (order_dir is a machine OUT tree with huge Outputs/ subfolders — never
# recurse), scandir off the event loop with a hard timeout so a hung CIFS
# mount can't wedge the proxy, and a path-traversal-safe content preview.

_FILES_TIMEOUT = 8.0        # seconds — a hung mount aborts, never blocks
_FILES_MAX = 200            # hard cap on rows returned
_PREVIEW_MAX = 256 * 1024   # bytes — content preview ceiling


def _classify_file(fname: str) -> str:
    low = fname.lower()
    if low.endswith(".ans"):
        return "answer"       # machine → us
    if low.endswith(".xml"):
        return "order"        # us → machine
    return "other"


def _scan_dirs(dirs: list[str], limit: int) -> dict:
    """Top-level file listing across `dirs` (deduped by path). Sync — run
    via asyncio.to_thread with a timeout. Never raises: a per-dir OSError
    (hung/absent mount) is recorded and skipped."""
    seen: set[str] = set()
    rows: list[dict] = []
    errors: dict[str, str] = {}
    for d in dirs:
        if not d or d in seen:
            continue
        seen.add(d)
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        if not e.is_file(follow_symlinks=False):
                            continue
                        st = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    rows.append({
                        "name": e.name,
                        "dir": d,
                        "kind": _classify_file(e.name),
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    })
        except OSError as exc:
            errors[d] = f"{exc.__class__.__name__}: {exc}"
    rows.sort(key=lambda r: r["mtime"], reverse=True)
    return {"files": rows[:limit], "total": len(rows),
            "truncated": len(rows) > limit, "errors": errors}


def _producer_dirs(request: Request, name: str):
    """(producer-or-None, order_dir, answer_dir) for station `name`, or
    raise 404 if the station is unknown."""
    reg = getattr(request.app.state, "europlacer_registry", None)
    if reg is None or name not in reg.specs:
        raise HTTPException(status_code=404,
                            detail=f"unknown Europlacer station {name!r}")
    spec = reg.specs[name]
    order_dir = getattr(spec, "order_dir", "") or ""
    answer_dir = getattr(spec, "answer_dir", "") or order_dir
    return reg.ingests.get(name), order_dir, answer_dir


@router.get("/{name}/files")
async def files(request: Request, name: str,
                limit: int = Query(80, ge=1, le=_FILES_MAX)) -> dict:
    """Live top-level listing of order_dir + answer_dir: order XML we write
    and .ans answers the machine returns, newest first. Open (monitoring)."""
    _prod, order_dir, answer_dir = _producer_dirs(request, name)
    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(_scan_dirs, [order_dir, answer_dir], limit),
            timeout=_FILES_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=f"listing timed out after {_FILES_TIMEOUT}s "
                   "(mount may be hung)")
    result.update({"name": name, "order_dir": order_dir,
                   "answer_dir": answer_dir})
    return result


@router.get("/{name}/file")
async def file_content(request: Request, name: str,
                       filename: str = Query(..., min_length=1, max_length=255),
                       ) -> dict:
    """Path-traversal-safe content preview of ONE file (order XML or .ans)
    living directly in order_dir/answer_dir. Capped at 256 KiB. Open."""
    _prod, order_dir, answer_dir = _producer_dirs(request, name)
    # basename only — never let the query escape the station's folders.
    base = os.path.basename(filename)
    if base != filename or base in ("", ".", ".."):
        raise HTTPException(status_code=400, detail="invalid filename")
    target = None
    for d in (order_dir, answer_dir):
        if not d:
            continue
        cand = os.path.realpath(os.path.join(d, base))
        # realpath of the file must stay inside the (realpath'd) dir.
        if os.path.dirname(cand) == os.path.realpath(d) and os.path.isfile(cand):
            target = cand
            break
    if target is None:
        raise HTTPException(status_code=404, detail="file not found")

    def _read(path: str) -> dict:
        st = os.stat(path)
        with open(path, "rb") as fh:
            raw = fh.read(_PREVIEW_MAX + 1)
        clipped = len(raw) > _PREVIEW_MAX
        text = raw[:_PREVIEW_MAX].decode("utf-8", errors="replace")
        return {"size": st.st_size, "mtime": st.st_mtime,
                "clipped": clipped, "content": text}

    try:
        data = await asyncio.wait_for(
            asyncio.to_thread(_read, target), timeout=_FILES_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(status_code=504, detail="read timed out")
    except OSError as exc:
        raise HTTPException(status_code=502, detail=f"read failed: {exc}")
    data.update({"name": base, "kind": _classify_file(base)})
    return data


@router.post("/{name}/start")
async def start_one(
    request: Request,
    name: str,
    x_registry_signature: Optional[str] = Header(None, alias="X-Registry-Signature"),
) -> dict:
    """Runtime START of one station (in-process only — does NOT touch
    config.d/europlacer.yaml; reverts to `enabled:` on next sync/restart).

    HMAC-guarded like /order: a state-changing control op. Empty body ⇒
    the signature is over the URL path bytes (shifts.py convention)."""
    await _verify_request(request, x_registry_signature, await request.body())
    reg = getattr(request.app.state, "europlacer_registry", None)
    if reg is None or name not in reg.specs:
        raise HTTPException(status_code=404,
                            detail=f"unknown Europlacer station {name!r}")
    started = reg.start_one(name)
    ingest = reg.ingests.get(name)
    return {"ok": True, "started": started,
            "status": ingest.status() if ingest is not None else None}


@router.post("/{name}/stop")
async def stop_one(
    request: Request,
    name: str,
    x_registry_signature: Optional[str] = Header(None, alias="X-Registry-Signature"),
) -> dict:
    """Runtime STOP of one station (in-process only).

    HMAC-guarded: stop cancels queued orders + drops the instance, so an
    unauthenticated caller could otherwise silently discard already-acked
    material orders (a producer, unlike the CFX consumer, has no replay)."""
    await _verify_request(request, x_registry_signature, await request.body())
    reg = getattr(request.app.state, "europlacer_registry", None)
    if reg is None or name not in reg.specs:
        raise HTTPException(status_code=404,
                            detail=f"unknown Europlacer station {name!r}")
    stopped = reg.stop_one(name)
    spec = reg.specs[name]
    return {"ok": True, "stopped": stopped,
            "status": {"name": name, "enabled": spec.enabled,
                       "running": False, "machine_kind": spec.machine_kind}}


# ─── order submit (Odoo → proxy, HMAC-guarded) ──────────────────────────


@router.post("/{name}/order")
async def submit_order(
    name: str,
    request: Request,
    response: Response,
    order_id: Optional[str] = None,
    x_registry_signature: Optional[str] = Header(None, alias="X-Registry-Signature"),
) -> dict:
    """Accept a ready material-order XML, queue the write→wait-.ans→retry
    cycle, and return a fast 202 ack. The terminal outcome arrives via
    bus_inject + optional result_url POST, and is pollable at
    ``GET /europlacer/{name}/order/{order_id}``.

    The signed bytes are the EXACT XML body (no canonicalisation) — Odoo
    signs the serialized XML it transmits and the proxy verifies over
    ``await request.body()``.
    """
    raw = await request.body()
    await _verify_request(request, x_registry_signature, raw)

    reg = getattr(request.app.state, "europlacer_registry", None)
    if reg is None or name not in reg.specs:
        raise HTTPException(status_code=404,
                            detail=f"unknown Europlacer station {name!r}")

    if not raw or not raw.strip():
        raise HTTPException(status_code=400, detail="empty order body")
    # Well-formedness gate — never drop a malformed file into the machine
    # folder (the machine would choke or return Error 11). Odoo sends the
    # READY XML, so this only catches transport corruption.
    from xml.etree import ElementTree as ET
    try:
        ET.fromstring(raw)
    except ET.ParseError as exc:
        raise HTTPException(status_code=400,
                            detail=f"order body is not well-formed XML: {exc}")

    ingest = reg.ingests.get(name)
    if ingest is None:
        # Materialise + start the station on first order (parity with the
        # cfx inject route). Writing only ever happens on this signed path.
        reg.start_one(name)
        ingest = reg.ingests.get(name)
    if ingest is None:
        raise HTTPException(
            status_code=409,
            detail=f"Europlacer station {name!r} is disabled / has no instance")

    from ...drivers.europlacer.common import EuroplacerOrder
    oid = (order_id or "").strip() or str(uuid.uuid4())
    order = EuroplacerOrder(order_id=oid, xml=raw, source="odoo")
    try:
        ingest.submit(order)
    except RuntimeError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    response.status_code = 202
    return {"ok": True, "station": name, "order_id": oid, "state": "queued"}


@router.get("/{name}/order/{order_id}")
async def order_status(request: Request, name: str, order_id: str) -> dict:
    """Poll the terminal outcome of a submitted order (``pending`` while
    in-flight, 404 if the id is unknown to this station)."""
    reg = getattr(request.app.state, "europlacer_registry", None)
    if reg is None or name not in reg.specs:
        raise HTTPException(status_code=404,
                            detail=f"unknown Europlacer station {name!r}")
    ingest = reg.ingests.get(name)
    outcome = ingest.get_outcome(order_id) if ingest is not None else None
    if outcome is None:
        raise HTTPException(status_code=404,
                            detail=f"unknown order {order_id!r} on {name!r}")
    return outcome
