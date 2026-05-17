"""
Polimex WebSDK event-stream ingestion.

Polimex iCON webstacks PUSH every reader/card/door event as an HTTP
POST to their configured "server URL" and expect `200 OK` within
10 s (else the event is re-sent after 10 s). This adapter receives
those events and funnels the **card number into the existing reader
bus** — a Polimex card swipe then behaves exactly like a barcode
scan: it flows out over the same WS / SSE / webhook / native-IoT
path every scanner already uses (no new Odoo plumbing).

Canonical payload (from the Polimex reference `simulate_event.py`):

    {"convertor": 414468, "key": "7411",
     "event": {"card": "1786802811", "id": 40, "reader": 1,
               "cmd": "FA", "event_n": 3, "err": 0,
               "date": "05.06.25", "time": "15:40:55", ...}}

Mapping: define an `external`-transport reader and point it at a
Polimex source via `extras.polimex` — any subset of
`{convertor, controller_id, reader, key}` (omitted keys = wildcard):

    readers:
      - id: gate_card
        transport: external
        extras:
          polimex: { convertor: 414468, controller_id: 40, reader: 1 }

The endpoint ALWAYS answers 200 quickly (publish is fire-and-forget)
so the webstack never enters its 10 s resend loop — even on an
unmatched event (logged, not errored).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Request

_logger = logging.getLogger(__name__)
router = APIRouter(tags=["polimex-events"])


def _to_int(v: Any) -> Optional[int]:
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _match(extras_polimex: dict, convertor, ctrl_id, reader_no,
           key) -> bool:
    """All configured keys must match; omitted keys are wildcards."""
    m = extras_polimex
    if "convertor" in m and _to_int(m["convertor"]) != _to_int(convertor):
        return False
    if "controller_id" in m and _to_int(m["controller_id"]) != _to_int(ctrl_id):
        return False
    if "reader" in m and _to_int(m["reader"]) != _to_int(reader_no):
        return False
    if m.get("key") and str(m["key"]) != str(key or ""):
        return False
    return True


def resolve_reader(reader_registry, payload: dict):
    """Return (reader_id, card) for a Polimex event, or (None, card).

    Pure — unit-testable without the FastAPI app."""
    ev = (payload or {}).get("event") or {}
    card = str(ev.get("card") or "").strip()
    # Полимекс праща "0"/нулева карта при не-картови събития — пропускаме
    if not card or set(card) == {"0"}:
        return None, card
    convertor = payload.get("convertor")
    key = payload.get("key")
    ctrl_id = ev.get("id")
    reader_no = ev.get("reader")
    if reader_registry is None:
        return None, card
    for rid, entry in reader_registry.readers.items():
        if entry.config.transport != "external":
            continue
        pol = (entry.config.extras or {}).get("polimex")
        if not isinstance(pol, dict):
            continue
        if _match(pol, convertor, ctrl_id, reader_no, key):
            return rid, card
    return None, card


@router.post("/polimex/event")
@router.post("/hr/rfid/event", include_in_schema=False)  # legacy WebSDK path
async def polimex_event(request: Request):
    reg = getattr(request.app.state, "reader_registry", None)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        # Невалиден JSON — пак отговаряме 200 (без resend buря); логваме.
        _logger.warning("Polimex event: unparseable body")
        return {"status": "ok"}

    rid, card = resolve_reader(reg, payload)
    if rid is None:
        ev = (payload or {}).get("event") or {}
        _logger.info(
            "Polimex event unmatched (convertor=%s ctrl=%s reader=%s "
            "card=%r) — no external reader maps it",
            payload.get("convertor"), ev.get("id"), ev.get("reader"),
            card,
        )
        return {"status": "ok"}  # 200 mandatory — never trigger resend

    from ...drivers.readers.common import BarcodeScan
    scan = BarcodeScan(reader_id=rid, barcode=card)
    reg.get(rid).bus.publish_threadsafe(scan)
    _logger.info("Polimex card %s → reader %r bus", card, rid)
    return {"status": "ok", "reader": rid}
