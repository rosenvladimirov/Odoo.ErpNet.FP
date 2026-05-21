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
import time as _time
from typing import Any, Optional

from fastapi import APIRouter, Request, Response

_logger = logging.getLogger(__name__)
router = APIRouter(tags=["polimex-events"])

# ── Event deduplication ──────────────────────────────────────────────
# Polimex 10.3 firmware on the iCON115 connector has been observed to
# repeatedly POST the SAME "Last Event" every ~15 s (the controller's
# event buffer is never cleared until a NEW card swipe overwrites it).
# `data.time/date/event_n/card` stay byte-identical across the storm.
#
# We dedupe at the receiver: each (ctrl_id, event_n, card, time, date)
# tuple is remembered for a short TTL. Repeats within the TTL emit
# NOTHING (no bus_inject, no per-reader publish) and just answer 200.
#
# TTL is generous (5 minutes) so even slow human swipes (re-presenting
# the same card after a few seconds) DO emit again — only the 15-s
# storm gets swallowed. The cache is small and bounded by ctrl_id
# count, so a process-local dict is enough.
_DEDUP_TTL_SEC = 300
_DEDUP_SEEN: dict[tuple, float] = {}


def _is_duplicate_event(ev: dict, ctrl_id: Optional[int]) -> bool:
    """Return True if `ev` is byte-identical to a recent event from the
    same controller (within `_DEDUP_TTL_SEC`). Side-effect: stamps the
    current event into the cache so subsequent duplicates are caught."""
    if not isinstance(ev, dict):
        return False
    key = (
        ctrl_id,
        ev.get("event_n"),
        ev.get("card"),
        ev.get("time"),
        ev.get("date"),
    )
    now = _time.monotonic()
    prev = _DEDUP_SEEN.get(key)
    _DEDUP_SEEN[key] = now
    # Light-touch GC — prune any entry older than 2*TTL while we're here.
    if len(_DEDUP_SEEN) > 64:
        cutoff = now - 2 * _DEDUP_TTL_SEC
        for k, t in list(_DEDUP_SEEN.items()):
            if t < cutoff:
                _DEDUP_SEEN.pop(k, None)
    return prev is not None and (now - prev) < _DEDUP_TTL_SEC


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


def _polimex_bus_emit(request: Request, event_type: str, data: dict,
                      device: str = "") -> None:
    """Best-effort live signal toward Odoo's bus_inject. Never raises —
    Polimex's HTTP retry window is short (≈3-5 s) and we must answer 200
    fast even if the Fleet receiver is briefly unreachable."""
    try:
        from ...clients.bus_inject import BusInjectClient
        client = BusInjectClient.from_app(request.app)
        if client is None:
            return
        client.emit(event_type, device=device,
                    device_kind="controller", data=data)
        client.close()
    except Exception as exc:  # noqa: BLE001
        _logger.debug("polimex bus_emit suppressed: %s", exc)


# Polimex event_n → our canonical bus_inject event type. Codes from
# github.com/polimex/polimex-rfid hr_rfid_event_system.py. Codes we
# don't map here fall through to the generic `controller.event`.
_EVENT_KIND_BY_N = {
    1:  "controller.event",      # DuressOK
    2:  "controller.event",      # DuressError
    3:  "card.read",             # R1 Card OK
    4:  "door.denied",           # R1 Card Error
    5:  "door.denied",           # R1 T/S Error
    6:  "door.denied",           # R1 APB Error
    7:  "card.read",             # R2 Card OK
    8:  "door.denied",           # R2 Card Error
    11: "card.read",             # R3 Card OK
    12: "door.denied",           # R3 Card Error
    15: "card.read",             # R4 Card OK
    16: "door.denied",           # R4 Card Error
    21: "button.pressed",        # Exit button
    25: "door.sensor",           # Door overtime
    26: "door.sensor",           # Forced door open
    30: "controller.heartbeat",  # Power on
    31: "door.opened",           # Open Door From PC
}


@router.post("/polimex/event")
@router.post("/hr/rfid/event", include_in_schema=False)  # legacy WebSDK path
async def polimex_event(request: Request):
    reg = getattr(request.app.state, "reader_registry", None)
    try:
        payload = await request.json()
    except Exception:  # noqa: BLE001
        # Невалиден JSON — пак отговаряме 200 (без resend buря); логваме.
        _logger.warning("Polimex event: unparseable body")
        return Response(content=b"", media_type="application/json", status_code=200)

    # Unwrap JSON-RPC envelope (sent when "RPC JSON format (Odoo)" toggle
    # is enabled in Polimex Web UI).
    if isinstance(payload, dict) and "jsonrpc" in payload \
            and isinstance(payload.get("params"), dict):
        payload = payload["params"]

    convertor = payload.get("convertor") if isinstance(payload, dict) else None
    src_label = f"polimex-{convertor}" if convertor else "polimex"

    # ─── Heartbeat ──────────────────────────────────────────────
    # Keep-alive ping. Polimex sends one every `HeartBeat Time` seconds
    # (60s default). Useful to track controller liveness on the Odoo
    # Fleet form.
    if isinstance(payload, dict) and "heartbeat" in payload:
        fw = payload.get("FW") or payload.get("fw") or ""
        _polimex_bus_emit(request, "controller.heartbeat", {
            "convertor": convertor,
            "fw": fw,
            "seq": payload.get("heartbeat"),
        }, device=src_label)
        return Response(content=b"", media_type="application/json", status_code=200)

    # ─── Response (controller's answer to an embedded command) ───
    # Phase A: log + bus_inject only. Phase B will tie this to the
    # inverted-RPC command queue (Wait → Process → here) so Odoo can
    # parse F0 responses and auto-create controller records.
    if isinstance(payload, dict) and "response" in payload:
        resp = payload.get("response") or {}
        ctrl_id = resp.get("id")
        _polimex_bus_emit(request, "controller.response", {
            "convertor": convertor,
            "ctrl_id": ctrl_id,
            "cmd": resp.get("c"),
            "err": resp.get("e"),
            "data": resp.get("d"),
        }, device=f"polimex-{convertor}-ctrl{ctrl_id}" if ctrl_id else src_label)
        return Response(content=b"", media_type="application/json", status_code=200)

    # ─── Event (the canonical hot path) ─────────────────────────
    # Two side-effects: (1) the existing per-reader bus pattern below
    # for cards mapped to external readers; (2) a bus_inject envelope
    # so Odoo's dashboards/toasts see EVERY event, not only mapped ones.
    ev = payload.get("event") if isinstance(payload, dict) else None
    if isinstance(ev, dict):
        ctrl_id = ev.get("id")
        event_n = ev.get("event_n")
        # Dedup the Polimex 10.3 "repeating Last Event" bug — see notes
        # at _is_duplicate_event for context. Duplicates answer 200
        # immediately, without emit or downstream processing.
        if _is_duplicate_event(ev, ctrl_id):
            _logger.debug(
                "Polimex dup-event swallowed: ctrl=%s n=%s card=%s "
                "time=%s/%s", ctrl_id, event_n, ev.get("card"),
                ev.get("date"), ev.get("time"))
            return Response(content=b"", media_type="application/json",
                            status_code=200)
        kind = _EVENT_KIND_BY_N.get(event_n, "controller.event")
        _polimex_bus_emit(request, kind, {
            "convertor": convertor,
            "ctrl_id": ctrl_id,
            "event_n": event_n,
            "card": ev.get("card"),
            "reader": ev.get("reader"),
            "time": ev.get("time"),
            "date": ev.get("date"),
            "dt": ev.get("dt"),
            "err": ev.get("err"),
        }, device=f"polimex-{convertor}-ctrl{ctrl_id}" if ctrl_id else src_label)

    rid, card = resolve_reader(reg, payload)
    if rid is None:
        ev = (payload or {}).get("event") or {}
        _logger.info(
            "Polimex event unmatched (convertor=%s ctrl=%s reader=%s "
            "card=%r) — no external reader maps it",
            payload.get("convertor"), ev.get("id"), ev.get("reader"),
            card,
        )
        return Response(content=b"", media_type="application/json", status_code=200)  # 200 mandatory — never trigger resend

    from ...drivers.readers.common import BarcodeScan
    scan = BarcodeScan(reader_id=rid, barcode=card)
    reg.get(rid).bus.publish_threadsafe(scan)
    _logger.info("Polimex card %s → reader %r bus", card, rid)
    return {"status": "ok", "reader": rid}
