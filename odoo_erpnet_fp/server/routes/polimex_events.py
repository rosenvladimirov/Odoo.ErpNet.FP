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


def _ack(is_jsonrpc: bool, jsonrpc_id, *, result: dict | None = None) -> Response:
    """Return the controller-side ACK reply.

    Polimex 10.3 legacy mode (no `jsonrpc` key in request): empty
    body, 200. (Same as their own `hr_rfid/controllers/main.py:687`.)

    Polimex 100.1+ JSON-RPC mode (request has `"jsonrpc":"2.0"`):
    Polimex's own receiver ALWAYS wraps the reply in
    `{jsonrpc:2.0, id, result}` — even for notification requests
    (where top-level `id` is missing → we echo `null`). The result
    body MUST contain `{"status": 200}` (or a `{"cmd": …}` next
    command) — otherwise the iCON115 controller treats the push as
    "not ACK'd", does NOT advance the `tos` event buffer pointer,
    and re-sends the SAME cached event forever. See
    [[feedback_polimex_icon115_push_log_stuck]].

    `result` defaults to `{"status": 200}` for the JSON-RPC path
    (Polimex's check_for_unsent_cmd-without-command shape), and `{}`
    is rejected — Polimex specifically dispatches on presence of
    `cmd` vs `status` keys.
    """
    if not is_jsonrpc:
        return Response(content=b"", media_type="application/json",
                        status_code=200)
    import json as _json
    body = _json.dumps({
        "jsonrpc": "2.0",
        "id": jsonrpc_id,  # may be None — that's the notification case
        "result": result if result is not None else {"status": 200},
    })
    return Response(content=body.encode("utf-8"),
                    media_type="application/json; charset=utf-8",
                    status_code=200)


@router.post("/polimex/event")
@router.post("/hr/rfid/event", include_in_schema=False)  # legacy WebSDK path
async def polimex_event(request: Request):
    reg = getattr(request.app.state, "reader_registry", None)
    # Diagnostic: log the raw body bytes BEFORE any parsing so we see
    # exactly what Polimex sends (firmware 1.66 changed payload shape;
    # the legacy "event"/"heartbeat" top-level keys may have moved).
    # Drop back to debug once the firmware-1.66 protocol is mapped.
    raw_body = await request.body()
    _logger.info("Polimex POST body (%d bytes): %s",
                 len(raw_body), raw_body[:1500].decode("utf-8", "replace"))
    try:
        import json as _json_parse
        payload = _json_parse.loads(raw_body or b"{}")
    except Exception:  # noqa: BLE001
        # Невалиден JSON — пак отговаряме 200 (без resend buря); логваме.
        _logger.warning("Polimex event: unparseable body")
        return Response(content=b"", media_type="application/json", status_code=200)

    # Detect JSON-RPC envelope (firmware 1.66+ sends ALL pushes as
    # `webstack.notification` JSON-RPC; legacy 10.3 sends raw dict).
    # Capture both the protocol flag AND the id (may be missing for
    # notification requests — we echo `null` to match Polimex's own
    # _make_response which never strips the id even when None).
    is_jsonrpc = False
    jsonrpc_id = None
    if isinstance(payload, dict) and "jsonrpc" in payload \
            and isinstance(payload.get("params"), dict):
        is_jsonrpc = True
        jsonrpc_id = payload.get("id")  # may be None for notifications
        payload = payload["params"]

    convertor = payload.get("convertor") if isinstance(payload, dict) else None
    src_label = f"polimex-{convertor}" if convertor else "polimex"

    # ─── Heartbeat ──────────────────────────────────────────────
    # Keep-alive ping. Polimex sends one every `HeartBeat Time` seconds
    # (60s default). A heartbeat means "alive" and NOTHING else — it
    # is NOT an audit event. We record it in the watchdog tracker and
    # do NOT emit a bus envelope (would create spurious hr.rfid.event
    # rows + dashboard noise). Only liveness TRANSITIONS reach Odoo,
    # via server/watchdog.py. (Rosen 2026-05-22.)
    if isinstance(payload, dict) and "heartbeat" in payload:
        fw = payload.get("FW") or payload.get("fw") or ""
        from ..watchdog import record_heartbeat
        record_heartbeat(request.app, convertor, fw=fw,
                         seq=payload.get("heartbeat"))
        return _ack(is_jsonrpc, jsonrpc_id)

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
        return _ack(is_jsonrpc, jsonrpc_id)

    # ─── Event (the canonical hot path) ─────────────────────────
    # Two side-effects: (1) the existing per-reader bus pattern below
    # for cards mapped to external readers; (2) a bus_inject envelope
    # so Odoo's dashboards/toasts see EVERY event, not only mapped ones.
    ev = payload.get("event") if isinstance(payload, dict) else None
    if isinstance(ev, dict):
        ctrl_id = ev.get("id")
        event_n = ev.get("event_n")
        # Diagnostic: log EVERY event payload (dup or not) so we can see
        # whether the Polimex storm carries the same time/date forever or
        # whether new physical swipes update the buffer. Drop this back to
        # debug once the buffer-update behaviour is confirmed.
        _logger.info(
            "Polimex event RAW: ctrl=%s n=%s card=%s reader=%s "
            "time=%s/%s dt=%s err=%s",
            ctrl_id, event_n, ev.get("card"), ev.get("reader"),
            ev.get("date"), ev.get("time"), ev.get("dt"), ev.get("err"))
        # Dedup the Polimex 10.3 "repeating Last Event" bug — see notes
        # at _is_duplicate_event for context. Duplicates answer 200
        # immediately, without emit or downstream processing.
        if _is_duplicate_event(ev, ctrl_id):
            _logger.debug(
                "Polimex dup-event swallowed: ctrl=%s n=%s card=%s "
                "time=%s/%s", ctrl_id, event_n, ev.get("card"),
                ev.get("date"), ev.get("time"))
            return _ack(is_jsonrpc, jsonrpc_id)
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
        return _ack(is_jsonrpc, jsonrpc_id)  # 200 mandatory — never trigger resend

    from ...drivers.readers.common import BarcodeScan
    scan = BarcodeScan(reader_id=rid, barcode=card)
    reg.get(rid).bus.publish_threadsafe(scan)
    _logger.info("Polimex card %s → reader %r bus", card, rid)
    return _ack(is_jsonrpc, jsonrpc_id, result={"status": "ok", "reader": rid})
