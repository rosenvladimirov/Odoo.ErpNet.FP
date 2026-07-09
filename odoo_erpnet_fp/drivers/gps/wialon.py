# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""
Wialon Remote API GPS tracker — polling driver.

The proxy logs in with a token, then polls ``core/search_items``
(avl_unit) on an interval and emits a ``PositionEvent`` for every unit
whose fix moved since the last poll. This mirrors the field-proven Odoo
``wialon.client`` semantics (CAST white-label): **params travel in the
URL query string, not the POST body** — CAST reads params from the query;
params in the body return error 4 "Invalid input".

Wialon ``pos`` dict: y=lat, x=lon, s=speed, c=course, z=alt, t=utc, sc=sat.
The currently-logged driver (avl_driver) and CAN mileage live in the last
message params (``lmsg.p``) when the unit exposes them.

Failure is non-fatal — the poll loop reconnects with backoff and never
gives up (a fleet server hiccup should self-heal without a proxy restart).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None

from .common import GpsTracker, PositionEvent

_logger = logging.getLogger(__name__)

# Wialon приема токена като URL query параметър (не в тялото) → httpx на INFO
# ниво логва пълния URL, вкл. токена. Заглушаваме httpx до WARNING, за да НЕ
# изтича READ токенът в docker/файлови логове (secret hygiene).
logging.getLogger("httpx").setLevel(logging.WARNING)

# Битова маска по подразбиране — base + last position + last message.
# Същата стойност, доказана срещу CAST в Odoo wialon.config.search_flags.
_DEFAULT_FLAGS = 5251073

# Wialon error кодове (подмножество) → четим текст за лога.
_WIALON_ERR = {
    1: "Invalid session",
    4: "Invalid input",
    5: "Error performing request",
    7: "Access denied",
    8: "Invalid service name",
    1001: "No message for selected interval",
}

_RECONNECT_BACKOFF_MAX_S = 30.0
_HTTP_TIMEOUT_S = 30.0


class WialonTracker(GpsTracker):
    """Polls a Wialon (CAST) account and emits moved-unit fixes.

    Args:
        tracker_id: short id used in API URLs / envelopes
        base_url: Wialon ajax endpoint, e.g. https://host/wialon/ajax.html
        token: Wialon API token (read scope is enough)
        poll_interval: seconds between polls (default 15)
        flags: search_items bit mask (default 5251073 = base+pos+lmsg)
        verify_ssl: TLS verification (CAST self-signed → set False)
        min_move_m: skip emitting if the unit moved less than this many
            meters AND the fix timestamp is unchanged (anti-noise)
        plate_from_name: use the Wialon unit name as the plate hint
        session_ttl: seconds before a pre-emptive re-login
    """

    def __init__(
        self,
        tracker_id: str,
        base_url: str,
        token: str,
        poll_interval: float = 15.0,
        flags: int = _DEFAULT_FLAGS,
        verify_ssl: bool = True,
        min_move_m: float = 15.0,
        plate_from_name: bool = True,
        session_ttl: float = 240.0,
    ) -> None:
        super().__init__(tracker_id)
        if httpx is None:
            raise RuntimeError("httpx is not installed")
        if not base_url or not token:
            raise ValueError(
                f"WialonTracker {tracker_id!r} needs base_url and token"
            )
        self.base_url = base_url
        self.token = (token or "").strip()
        self.poll_interval = max(float(poll_interval), 2.0)
        self.flags = int(flags)
        self.verify_ssl = verify_ssl
        self.min_move_m = float(min_move_m)
        self.plate_from_name = plate_from_name
        self.session_ttl = float(session_ttl)

        self._client: Optional["httpx.Client"] = None
        self._sid: Optional[str] = None
        self._sid_expires = 0.0
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        # Последна изпратена позиция per unit: {unit_id: (lat, lon, utc)}.
        self._last: dict[str, tuple] = {}

    # ─── Lifecycle ──────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        # follow_redirects=True: CAST white-label хостове често 301-редиректват
        # (напр. api.cast-bg.net → my.kitin.at); requests (Odoo cron) следва по
        # подразбиране, httpx — не, затова го включваме изрично.
        self._client = httpx.Client(
            timeout=_HTTP_TIMEOUT_S, verify=self.verify_ssl,
            follow_redirects=True,
        )
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name=f"WialonTracker[{self.tracker_id}]",
            daemon=True,
        )
        self._running = True
        self._thread.start()
        _logger.info(
            "WialonTracker %r started — base=%s interval=%ss",
            self.tracker_id, self.base_url, self.poll_interval,
        )

    def stop(self) -> None:
        self._stop_evt.set()
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        # best-effort logout + close
        try:
            if self._sid:
                self._call("core/logout", {})
        except Exception:  # noqa: BLE001
            pass
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # noqa: BLE001
                pass
            self._client = None
        self._sid = None

    # ─── Poll loop ──────────────────────────────────────────

    def _loop(self) -> None:
        backoff = 1.0
        while not self._stop_evt.is_set():
            try:
                self._ensure_session()
                self._poll_once()
                backoff = 1.0  # успешен цикъл → нулиране на backoff
                self._stop_evt.wait(self.poll_interval)
            except Exception as exc:  # noqa: BLE001 — reconnect, не спираме
                _logger.warning(
                    "WialonTracker %r poll error: %s (retry in %.0fs)",
                    self.tracker_id, exc, backoff,
                )
                self._sid = None  # форсирай re-login
                self._stop_evt.wait(backoff)
                backoff = min(backoff * 2.0, _RECONNECT_BACKOFF_MAX_S)

    def _poll_once(self) -> None:
        units = self._get_units()
        for unit in units:
            evt = self._unit_to_event(unit)
            if evt is None:
                continue
            self._emit(evt)

    # ─── Wialon transport ───────────────────────────────────

    def _request(self, svc: str, params: dict, sid: Optional[str] = None) -> dict:
        # Params в URL query (не body) — CAST изискване. httpx.params
        # URL-кодира; JSON стрингът за `params` носи самите аргументи.
        query = {"svc": svc, "params": json.dumps(params or {})}
        if sid:
            query["sid"] = sid
        resp = self._client.get(self.base_url, params=query)
        resp.raise_for_status()
        result = resp.json()
        if isinstance(result, dict) and result.get("error"):
            code = result["error"]
            raise RuntimeError(
                "Wialon error %s (%s) on svc=%s"
                % (code, _WIALON_ERR.get(code, "unknown"), svc)
            )
        return result

    def _call(self, svc: str, params: dict) -> dict:
        return self._request(svc, params, sid=self._sid)

    def _ensure_session(self) -> None:
        if self._sid and time.time() < self._sid_expires:
            return
        result = self._request("token/login", {"token": self.token})
        eid = result.get("eid")
        if not eid:
            raise RuntimeError("Wialon login returned no session id (eid)")
        self._sid = eid
        self._sid_expires = time.time() + self.session_ttl
        _logger.info(
            "WialonTracker %r login OK (eid=%s)", self.tracker_id, eid
        )

    def _get_units(self) -> list:
        params = {
            "spec": {
                "itemsType": "avl_unit",
                "propName": "sys_name",
                "propValueMask": "*",
                "sortType": "sys_name",
                "propType": "property",
                "or_logic": 0,
            },
            "force": 1,
            "flags": self.flags,
            "from": 0,
            "to": 0,
        }
        result = self._call("core/search_items", params)
        return (result or {}).get("items") or []

    # ─── Fix extraction ─────────────────────────────────────

    def _unit_to_event(self, unit: dict) -> Optional[PositionEvent]:
        if not isinstance(unit, dict):
            return None
        pos = unit.get("pos") or {}
        if not pos:
            lmsg = unit.get("lmsg") or {}
            pos = lmsg.get("pos") if isinstance(lmsg, dict) else {}
        if not pos:
            return None
        lat = _f(pos.get("y"))
        lon = _f(pos.get("x"))
        if lat is None or lon is None:
            return None
        utc = pos.get("t")
        unit_id = str(unit.get("id"))

        # Dedup / анти-шум: пропусни, ако не се е мръднал забележимо И
        # времето на fix-а е същото като последно изпратеното.
        prev = self._last.get(unit_id)
        if prev is not None:
            plat, plon, putc = prev
            if putc == utc and _haversine_m(plat, plon, lat, lon) < self.min_move_m:
                return None
        self._last[unit_id] = (lat, lon, utc)

        p = self._lmsg_params(unit)
        driver_code = str(p.get("avl_driver") or "").strip()
        if driver_code in ("0", ""):
            driver_code = ""
        can_mileage = _f(p.get("can_mileage"))
        odo_km = can_mileage / 1000.0 if can_mileage else None

        return PositionEvent(
            tracker_id=self.tracker_id,
            unit_id=unit_id,
            plate=(unit.get("nm") or "").strip() if self.plate_from_name else "",
            lat=lat,
            lon=lon,
            speed=_f(pos.get("s")) or 0.0,
            heading=_f(pos.get("c")) or 0.0,
            altitude=_f(pos.get("z")) or 0.0,
            sat_count=int(pos.get("sc") or 0),
            driver_code=driver_code,
            odometer_km=odo_km,
            fix_utc=int(utc) if utc else None,
        )

    @staticmethod
    def _lmsg_params(unit: dict) -> dict:
        lmsg = unit.get("lmsg")
        if isinstance(lmsg, dict) and isinstance(lmsg.get("p"), dict):
            return lmsg["p"]
        return {}


# ─── helpers ────────────────────────────────────────────────

def _f(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in meters (anti-noise gate; approx is fine)."""
    import math
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = (math.sin(dp / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2)
    return r * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
