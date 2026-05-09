"""
Native Odoo IoT Box compatibility layer.

Lets ErpNet.FP appear as an IoT Box to native Odoo modules:
  * pos_iot (POS scale, scanner, customer display, fiscal printer)
  * quality_iot (quality measurements)
  * delivery_iot (package weighing)
  * account_iot (backend fiscal printing)

Endpoints (4 — same handlers, different paths so a single ErpNet.FP
instance serves both Odoo 18 and Odoo 19+ clients on the same network):

  POST /hw_drivers/action   (Odoo 18 EE)
  POST /hw_drivers/event    (Odoo 18 EE — long-poll)
  POST /iot_drivers/action  (Odoo 19+ EE)
  POST /iot_drivers/event   (Odoo 19+ EE — long-poll)

Wire-format differences between v18 and v19 (both supported):

  Odoo 18 request body:  {"params": {"session_id": ..., ...}}
  Odoo 19+ request body: {"session_id": ..., ...}                # no wrapper

  v18 stringifies `data` as JSON: data="{\\"action\\":\\"read_once\\"}"
  v19 sends `data` as object:    data={"action": "read_once"}

Response format is consistent for both: {"result": {...}}.

Device identifier convention (caller picks one of the prefixes):
  printer.<id>   → routes to /printers/<id>/* internal handlers
  scale.<id>     → routes to /scales/<id>/weight
  display.<id>   → routes to /displays/<id>/*
  reader.<id>    → push events through long-poll (no action — see /event)
  pinpad.<id>    → routes to /pinpads/<id>/*

The Odoo iot.device.identifier field stores this exact prefix:dotted
form, so admins create devices like:

  iot.device(name="POS Scale", type="scale", identifier="scale.cas1")
  iot.device(name="Fiscal Printer", type="printer", identifier="printer.fp1")
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import PlainTextResponse

_logger = logging.getLogger(__name__)

# Two routers — one per path family. Same handler functions are
# registered against both, so an ErpNet.FP instance answers to both
# Odoo 18 and Odoo 19 clients without configuration changes.
v18_router = APIRouter(prefix="/hw_drivers", tags=["iot-compat-v18"])
v19_router = APIRouter(prefix="/iot_drivers", tags=["iot-compat-v19"])

# Legacy POS hardware-proxy probe. Pre-IoT Box era, but Odoo POS and
# the fiscal-printer health-check still ping /hw_proxy/hello to decide
# whether the box is "up". A 404 here flips fiscal.printer.device's
# proxy_connected flag to False even when the modern /hw_drivers/*
# handlers are fully functional, surfacing as "не е свързан с iot" in
# the UI. Returning "ping" matches the Odoo built-in handler verbatim.
legacy_router = APIRouter(prefix="/hw_proxy", tags=["iot-compat-legacy"])


@legacy_router.get("/hello", response_class=PlainTextResponse)
@legacy_router.post("/hello", response_class=PlainTextResponse)
async def legacy_hello() -> str:
    return "ping"


@v18_router.get("/download_logs", response_class=PlainTextResponse)
@v19_router.get("/download_logs", response_class=PlainTextResponse)
async def download_logs(tail: Optional[int] = None) -> PlainTextResponse:
    """Stock IoT Box endpoint — Odoo's iot_download_logs.js calls
    `window.location = ip_url + '/hw_drivers/download_logs'` and
    expects a downloadable text file with the box's recent log lines.

    Also reused by the Fleet command queue (`get_logs` kind) so the
    Odoo backend has a single canonical endpoint to hit regardless of
    whether it's a sync browser download or an async Fleet command.

    `tail` query param caps the dump to the last N entries (handy for
    the Fleet get_logs flow where 5000 lines is excessive). When
    omitted, the whole ring buffer is dumped (matching native Odoo's
    iot_download_logs.js expectation).
    """
    from .admin import _LOG_BUFFER
    snapshot = list(_LOG_BUFFER)
    if tail is not None and tail > 0:
        snapshot = snapshot[-tail:]
    body = "\n".join(entry["msg"] for entry in snapshot)
    return PlainTextResponse(
        content=body or "(log buffer empty)\n",
        headers={
            "Content-Disposition":
                'attachment; filename="erpnet_fp_logs.txt"',
        },
    )


# ─── Long-poll session bookkeeping (in-memory) ───────────────────────


class _IotSessionRegistry:
    """Tracks per-session device subscriptions for long-poll fan-out.

    Native Odoo browsers each generate a UUID `session_id` at startup
    and reuse it for the lifetime of the page. We use it to route
    pushed events (e.g. barcode scans) only to subscribers that
    actually want them.
    """

    def __init__(self) -> None:
        # session_id → {device_identifier: last_event_time}
        self._sessions: dict[str, dict[str, float]] = {}
        # device_identifier → list[(session_id, asyncio.Event, payload_holder)]
        self._waiters: dict[str, list[tuple[str, asyncio.Event, list]]] = {}
        # device_identifier → most recent payload pushed while no waiters
        # were registered. Native Odoo long-poll cycles have a ~100ms gap
        # between response and re-poll; a barcode scan landing in that
        # gap would be lost without this buffer. Latest-wins policy: if
        # two scans arrive before the next poll, only the second is
        # delivered (matches operator intent — fast double-scans should
        # not double-fire on the POS).
        self._pending: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def push(self, device_identifier: str, payload: dict) -> None:
        """Wake up every long-poll waiter listening for this device. If
        no waiter is currently registered, buffer the payload so the
        next wait() returns it immediately.
        """
        async with self._lock:
            waiters = self._waiters.pop(device_identifier, [])
            if not waiters:
                self._pending[device_identifier] = payload
        for _sid, evt, holder in waiters:
            holder.append(payload)
            evt.set()

    async def wait(
        self,
        session_id: str,
        device_identifier: str,
        timeout: float,
    ) -> Optional[dict]:
        """Block until an event is pushed for this device, or timeout.
        Returns immediately if there is a buffered payload from a push
        that arrived between long-poll cycles.
        """
        async with self._lock:
            buffered = self._pending.pop(device_identifier, None)
        if buffered is not None:
            return buffered
        evt = asyncio.Event()
        holder: list = []
        async with self._lock:
            self._waiters.setdefault(device_identifier, []).append(
                (session_id, evt, holder)
            )
        try:
            await asyncio.wait_for(evt.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            # Cleanup our waiter so push() doesn't try to wake a dead one
            async with self._lock:
                queue = self._waiters.get(device_identifier, [])
                self._waiters[device_identifier] = [
                    w for w in queue if w[1] is not evt
                ]
            return None
        return holder[0] if holder else None


_sessions = _IotSessionRegistry()


def get_iot_sessions() -> _IotSessionRegistry:
    """Module-level accessor — used by other parts (e.g. reader event bus)
    to push barcode scans to long-poll subscribers."""
    return _sessions


# ─── Body parsing — handles both v18 and v19 wire formats ────────────


async def _parse_body(request: Request) -> dict:
    """Returns the inner message dict regardless of v18/v19 wrapping.

    v18 payload is `{"params": {"session_id": ..., ...}}`;
    v19 payload is `{"session_id": ..., ...}` (no wrapper).
    """
    try:
        raw = await request.json()
    except Exception:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Invalid JSON body")
    if not isinstance(raw, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Body must be an object")
    # v18 wraps everything inside "params"; v19 does not.
    if "params" in raw and isinstance(raw["params"], dict):
        return raw["params"]
    return raw


def _parse_action_data(raw: Any) -> dict:
    """`data` field is JSON-stringified in v18, plain object in v19."""
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {"action": raw}  # fallback: bare string command
    return {}


def _split_identifier(identifier: str) -> tuple[str, str]:
    """`scale.cas1` → ('scale', 'cas1'). `printer.fp1` → ('printer', 'fp1').

    Returns (kind, id). If no prefix found, defaults kind='printer' for
    backwards compat with raw fiscal-printer identifiers.
    """
    if "." not in identifier:
        return "printer", identifier
    kind, dot, rest = identifier.partition(".")
    return kind.lower(), rest


# ─── Action dispatch — maps device_identifier → internal handler ─────


async def _do_action(
    request: Request,
    session_id: str,
    device_identifier: str,
    data: dict,
) -> dict:
    """Execute one action. Returns the inner result dict (caller wraps
    it in {"result": ...} per native Odoo iot_longpolling contract)."""
    kind, dev_id = _split_identifier(device_identifier)
    app_state = request.app.state

    try:
        if kind == "scale":
            return await _scale_action(app_state, dev_id, data)
        if kind == "display":
            return await _display_action(app_state, dev_id, data)
        if kind == "printer":
            return await _printer_action(app_state, dev_id, data)
        if kind == "pinpad":
            return await _pinpad_action(app_state, dev_id, data)
        if kind == "reader":
            # Readers don't support `action` — they're event-only.
            return {
                "status": {"status": "error",
                           "message_body": "Readers are event-only"},
                "device_identifier": device_identifier,
                "session_id": session_id,
            }
        return {
            "status": {"status": "error",
                       "message_body": f"Unknown device kind: {kind!r}"},
            "device_identifier": device_identifier,
            "session_id": session_id,
        }
    except KeyError:
        return {
            "status": {"status": "error",
                       "message_body": f"Device {device_identifier!r} not configured"},
            "device_identifier": device_identifier,
            "session_id": session_id,
        }
    except Exception as exc:
        _logger.exception("IoT action failed: %s", device_identifier)
        return {
            "status": {"status": "error", "message_body": str(exc)},
            "device_identifier": device_identifier,
            "session_id": session_id,
        }


async def _scale_action(state, scale_id: str, data: dict) -> dict:
    """Native pos_iot.scale_screen calls `{action: "read_once"}`."""
    reg = state.scale_registry
    if not reg.has(scale_id):
        raise KeyError(scale_id)
    action = data.get("action", "read_once")
    if action == "read_once":
        async with reg.with_scale(scale_id) as sc:
            reading = await asyncio.to_thread(sc.read_weight)
        # Native pos_iot expects: {result: <weight in kg>, status: {status: "success"}}
        if reading.ok:
            return {
                "result": reading.weight_kg,
                "status": {"status": "success"},
            }
        return {
            "result": 0,
            "status": {
                "status": "error",
                "message_body": "; ".join(reading.status) or "Scale read failed",
            },
        }
    return {"status": {"status": "error",
                       "message_body": f"Unknown scale action: {action!r}"}}


async def _display_action(state, display_id: str, data: dict) -> dict:
    """Native pos_iot customer-display protocol.

    Action shapes seen in the wild:
      `{action: "customer_display_update", lines: [...]}` — Odoo POS
      `{action: "show", top: "...", bottom: "..."}` — generic
      `{action: "clear"}`
    """
    reg = state.display_registry
    if not reg.has(display_id):
        raise KeyError(display_id)
    action = data.get("action", "show")
    async with reg.with_display(display_id) as d:
        def _run():
            if action == "clear":
                d.clear()
            elif action in ("show", "two_lines"):
                d.display_two_lines(data.get("top", ""), data.get("bottom", ""))
            elif action == "customer_display_update":
                # POS sends a 'lines' array — adapt to two-line display
                lines = data.get("lines") or []
                top = lines[0] if len(lines) > 0 else ""
                bottom = lines[1] if len(lines) > 1 else ""
                d.display_two_lines(top, bottom)
            elif action == "total":
                d.display_total(
                    data.get("label", ""),
                    data.get("amount", 0),
                    data.get("currency", ""),
                )
            elif action == "brightness":
                d.set_brightness(int(data.get("level", 4)))
            else:
                raise ValueError(f"Unknown display action: {action!r}")
        await asyncio.to_thread(_run)
    return {"result": True, "status": {"status": "success"}}


async def _printer_action(state, printer_id: str, data: dict) -> dict:
    """Map native iot printer actions → existing /printers/{id}/* handlers."""
    reg = state.registry
    if not reg.has(printer_id):
        raise KeyError(printer_id)
    action = data.get("action") or data.get("operation") or "status"

    async with reg.with_driver(printer_id) as drv:
        def _run():
            # Method names differ between PM and ISL drivers; try a
            # short list of synonyms before declaring unsupported.
            candidates = {
                "status": ["get_status", "status"],
                "read_status": ["get_status", "status"],
                "zreport": ["print_z_report", "print_zreport"],
                "z_report": ["print_z_report", "print_zreport"],
                "print_zreport": ["print_z_report", "print_zreport"],
                "xreport": ["print_x_report", "print_xreport"],
                "x_report": ["print_x_report", "print_xreport"],
                "print_xreport": ["print_x_report", "print_xreport"],
                "drawer": ["open_drawer", "drawer"],
                "open_drawer": ["open_drawer", "drawer"],
                "duplicate": ["print_duplicate", "duplicate"],
            }
            method_names = candidates.get(action, [])
            for name in method_names:
                fn = getattr(drv, name, None)
                if callable(fn):
                    return fn()
            raise ValueError(f"Unknown printer action: {action!r}")
        try:
            result = await asyncio.to_thread(_run)
        except AttributeError as exc:
            return {"status": {"status": "error",
                               "message_body": f"Action not supported: {exc}"}}
    # DeviceStatus dataclass / dict / etc — wrap into JSON-friendly shape.
    if hasattr(result, "__dict__") and not isinstance(result, dict):
        result_payload = {k: v for k, v in vars(result).items()
                          if not k.startswith("_")}
    else:
        result_payload = result if result is not None else True
    return {"result": result_payload, "status": {"status": "success"}}


async def _pinpad_action(state, pinpad_id: str, data: dict) -> dict:
    """Map native iot payment terminal actions to pinpad registry."""
    reg = state.pinpad_registry
    if not reg.has(pinpad_id):
        raise KeyError(pinpad_id)
    action = data.get("action") or "status"
    # Pinpads are NDA-locked Datecs; we expose a thin compatibility surface.
    if action == "status":
        return {"result": True, "status": {"status": "connected"}}
    return {"status": {"status": "error",
                       "message_body": "Pinpad actions over IoT compat not yet wired"}}


# ─── Endpoints ──────────────────────────────────────────────────────


_POLL_TIMEOUT_SECS = 50.0  # Odoo browser uses 60s; we time out earlier


async def _action_handler(request: Request):
    body = await _parse_body(request)
    session_id = str(body.get("session_id") or "")
    device_identifier = str(body.get("device_identifier") or "")
    if not device_identifier:
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "device_identifier is required")
    data = _parse_action_data(body.get("data"))
    inner = await _do_action(request, session_id, device_identifier, data)
    inner.setdefault("session_id", session_id)
    inner.setdefault("device_identifier", device_identifier)
    inner.setdefault("time", time.time())
    return {"result": inner}


async def _event_handler(request: Request):
    body = await _parse_body(request)
    listener = body.get("listener") or {}
    session_id = str(listener.get("session_id") or body.get("session_id") or "")
    devices = listener.get("devices") or {}
    if not isinstance(devices, dict):
        raise HTTPException(status.HTTP_400_BAD_REQUEST,
                            "listener.devices must be an object")
    if not devices:
        # Nothing to wait for — return immediately so the client polls again
        return {"result": False}

    # Wait on ALL the listed device identifiers in parallel; first one
    # that produces an event wins. Native Odoo client expects one event
    # per response, then re-polls.
    async def _wait_one(device_identifier: str):
        return device_identifier, await _sessions.wait(
            session_id, device_identifier, _POLL_TIMEOUT_SECS
        )

    tasks = [asyncio.create_task(_wait_one(d)) for d in devices.keys()]
    try:
        done, pending = await asyncio.wait(
            tasks, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
    for t in done:
        try:
            device_id, payload = t.result()
        except Exception:
            continue
        if payload is None:
            continue
        return {"result": {
            "session_id": session_id,
            "device_identifier": device_id,
            "time": time.time(),
            **payload,
        }}
    # All timed out — return False so client immediately re-polls
    return {"result": False}


# Bind both handlers to both v18 and v19 paths.
v18_router.post("/action")(_action_handler)
v18_router.post("/event")(_event_handler)
v19_router.post("/action")(_action_handler)
v19_router.post("/event")(_event_handler)
