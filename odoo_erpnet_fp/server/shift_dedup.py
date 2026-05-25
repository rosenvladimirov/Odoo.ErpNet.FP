"""
SQLite-backed idempotency cache for `POST /devices/<serial>/shift_close`.

The Android client may re-POST the same shift payload after a network
retry, restart, or bridge reconnect. The proxy must absorb that without
double-forwarding to Odoo.

Idempotency key per `anchor_bluecash_shift_sync_contract.md`:
    (device_serial, fiscal_day_number, z_report_number)

Cache stores the FULL successful Odoo response so a replay returns the
exact same `odoo_session_id`, `odoo_orders_created`, etc. that the first
call produced.

Crash safety: SQLite WAL mode + `PRAGMA synchronous=NORMAL`. The DB file
lives on the proxy's persistent volume (same dir as `/app/data/admin_token`).
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
from typing import Optional

_logger = logging.getLogger(__name__)

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS shift_dedup (
    device_serial      TEXT    NOT NULL,
    fiscal_day_number  INTEGER NOT NULL,
    z_report_number    TEXT    NOT NULL,
    response_json      TEXT    NOT NULL,
    http_status        INTEGER NOT NULL DEFAULT 200,
    created_at         TEXT    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (device_serial, fiscal_day_number, z_report_number)
)
"""


class ShiftDedupCache:
    """Thread-safe SQLite-backed dedup cache for shift_close payloads."""

    def __init__(self, db_path: str = "/app/data/shift_dedup.sqlite") -> None:
        self._db_path = db_path
        self._lock = threading.Lock()
        # Гарантира че родителската директория съществува (admin_token
        # обикновено вече я е създал но при чисто initialise не е).
        parent = os.path.dirname(db_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        # `check_same_thread=False` + `self._lock` за worker-pool достъп.
        c = sqlite3.connect(self._db_path, check_same_thread=False, timeout=5.0)
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        return c

    def _init_schema(self) -> None:
        with self._lock:
            with self._conn() as c:
                c.execute(_SCHEMA_SQL)

    # ── Public API ────────────────────────────────────────────────────

    def get(
        self,
        device_serial: str,
        fiscal_day_number: int,
        z_report_number: str,
    ) -> Optional[tuple[int, dict]]:
        """Returns `(http_status, parsed_response)` или None при miss."""
        with self._lock:
            with self._conn() as c:
                row = c.execute(
                    "SELECT http_status, response_json FROM shift_dedup "
                    "WHERE device_serial=? AND fiscal_day_number=? "
                    "AND z_report_number=?",
                    (device_serial, fiscal_day_number, z_report_number),
                ).fetchone()
        if not row:
            return None
        try:
            return int(row[0]), json.loads(row[1])
        except (ValueError, json.JSONDecodeError) as exc:
            _logger.warning("Corrupt dedup row for (%s,%s,%s): %s",
                            device_serial, fiscal_day_number,
                            z_report_number, exc)
            return None

    def put(
        self,
        device_serial: str,
        fiscal_day_number: int,
        z_report_number: str,
        response: dict,
        http_status: int = 200,
    ) -> None:
        """Idempotent upsert. Last-write-wins (но реално второ повикване
        с друг payload е логическа грешка — alert-ваме в логовете)."""
        payload = json.dumps(response, separators=(",", ":"),
                             ensure_ascii=False)
        with self._lock:
            with self._conn() as c:
                # На SQLite INSERT OR REPLACE задейства cascade-и; при тая
                # таблица нямаме FK така че е безопасно. Алтернатива е
                # ON CONFLICT(...) DO UPDATE но няма практическа разлика.
                c.execute(
                    "INSERT OR REPLACE INTO shift_dedup "
                    "(device_serial, fiscal_day_number, z_report_number, "
                    " response_json, http_status) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (device_serial, fiscal_day_number, z_report_number,
                     payload, http_status),
                )

    def purge_older_than(self, days: int = 90) -> int:
        """House-keeping. Returns number of rows deleted."""
        with self._lock:
            with self._conn() as c:
                cur = c.execute(
                    "DELETE FROM shift_dedup "
                    "WHERE created_at < datetime('now', ?)",
                    (f"-{int(days)} days",),
                )
                return cur.rowcount

    def forget(
        self,
        device_serial: str,
        fiscal_day_number: int,
        z_report_number: str,
    ) -> bool:
        """Explicit eviction (admin op). Returns True ако имаше row."""
        with self._lock:
            with self._conn() as c:
                cur = c.execute(
                    "DELETE FROM shift_dedup "
                    "WHERE device_serial=? AND fiscal_day_number=? "
                    "AND z_report_number=?",
                    (device_serial, fiscal_day_number, z_report_number),
                )
                return cur.rowcount > 0


# ── Process-wide singleton (lazy) ─────────────────────────────────────

_singleton: Optional[ShiftDedupCache] = None
_singleton_lock = threading.Lock()


def get_dedup_cache(
    db_path: str = "/app/data/shift_dedup.sqlite",
) -> ShiftDedupCache:
    """Lazily-initialised module-wide cache. Idempotent."""
    global _singleton
    if _singleton is None:
        with _singleton_lock:
            if _singleton is None:
                _singleton = ShiftDedupCache(db_path)
    return _singleton


__all__ = ["ShiftDedupCache", "get_dedup_cache"]
