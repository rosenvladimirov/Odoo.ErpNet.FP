"""Controller-liveness watchdog.

Tracks the last heartbeat per Polimex *convertor* (Web Module) and
emits red `controller.offline` / green `controller.online` alerts on
the bus_inject channel when a Web Module goes silent or returns.

Design (per Rosen 2026-05-22): a Polimex heartbeat
(`{convertor, fw, seq}`) means "I'm alive" and NOTHING else. It is
NOT an audit event — `polimex_events.py` calls `record_heartbeat()`
here instead of emitting a bus envelope, so heartbeats never create
`hr.rfid.event` rows nor flood the dashboard. Only state
TRANSITIONS (online↔offline) reach Odoo, as a single alert envelope
that the `live_refresh` hub turns into a (sticky, for offline)
toast for the administrator.

Heartbeats carry only `convertor` (the Web Module serial), not a
controller bus_id — a Web Module hosts N controllers on its RS-485
bus, so liveness is naturally per-convertor. If the Web Module dies,
all its controllers are unreachable at once.
"""
from __future__ import annotations

import asyncio
import logging
import time as _time

_logger = logging.getLogger(__name__)


def _tracker(app) -> dict:
    """Lazily-init the per-convertor heartbeat state on app.state."""
    t = getattr(app.state, "heartbeat_tracker", None)
    if t is None:
        t = {}
        app.state.heartbeat_tracker = t
    return t


def record_heartbeat(app, convertor, *, fw: str = "", seq=None) -> None:
    """Stamp a fresh heartbeat for `convertor`. Called from the
    Polimex `/hr/rfid/event` heartbeat path. Never raises.

    A brand-new convertor is recorded as online immediately (no
    spurious "online" alert on first contact)."""
    if convertor is None:
        return
    key = str(convertor)
    tr = _tracker(app)
    now = _time.monotonic()
    entry = tr.get(key)
    if entry is None:
        entry = {"online": True, "first_seen": now, "transitions": 0}
        tr[key] = entry
        _logger.info(
            "watchdog: first heartbeat from convertor %s (fw=%s)", key, fw)
    entry["last"] = now
    if fw:
        entry["fw"] = fw
    entry["seq"] = seq


async def watchdog_loop(app) -> None:
    """Background task: scan the heartbeat tracker on a fixed
    interval and emit offline/online transitions. Started from the
    FastAPI lifespan only when `server.watchdog.enabled`."""
    cfg = app.state.config.server.watchdog
    timeout = max(0, int(cfg.heartbeat_timeout))
    interval = max(5, int(cfg.check_interval))
    if timeout <= 0:
        _logger.info("watchdog: disabled (heartbeat_timeout=0)")
        return
    _logger.info(
        "watchdog: started (timeout=%ds, interval=%ds)", timeout, interval)
    while True:
        try:
            await asyncio.sleep(interval)
            _scan_once(app, timeout)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            _logger.exception("watchdog scan failed")


def _scan_once(app, timeout: int) -> None:
    tr = _tracker(app)
    now = _time.monotonic()
    for key, entry in list(tr.items()):
        last = entry.get("last")
        if last is None:
            continue
        silent = now - last
        was_online = entry.get("online", True)
        if was_online and silent > timeout:
            entry["online"] = False
            entry["transitions"] = entry.get("transitions", 0) + 1
            _emit_alert(app, key, online=False, silent=silent, entry=entry)
        elif (not was_online) and silent <= timeout:
            entry["online"] = True
            entry["transitions"] = entry.get("transitions", 0) + 1
            _emit_alert(app, key, online=True, silent=silent, entry=entry)


def _emit_alert(app, convertor, *, online: bool, silent: float,
                entry: dict) -> None:
    """Fire-and-forget bus_inject alert for a liveness transition.
    Never raises — a failed alert must not kill the watchdog loop."""
    try:
        from ..clients.bus_inject import BusInjectClient
        client = BusInjectClient.from_app(app)
        if client is None:
            _logger.warning(
                "watchdog: bus_inject unavailable; convertor %s online=%s "
                "not announced", convertor, online)
            return
        event_type = "controller.online" if online else "controller.offline"
        client.emit(
            event_type,
            device=f"polimex-{convertor}",
            device_kind="controller",
            data={
                "convertor": _to_int(convertor),
                "severity": "info" if online else "alert",
                "silent_seconds": round(silent, 1),
                "fw": entry.get("fw", ""),
            },
        )
        client.close()
        _logger.warning(
            "watchdog: convertor %s → %s (silent %.0fs)",
            convertor, event_type, silent)
    except Exception:  # noqa: BLE001
        _logger.exception("watchdog emit failed for convertor %s", convertor)


def _to_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return v
