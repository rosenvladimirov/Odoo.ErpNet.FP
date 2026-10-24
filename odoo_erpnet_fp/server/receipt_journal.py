"""
Дневник на бона по УНП — един бон дори при прекъсване (план o-experts POS, Ф2).

Ключът е (принтер, вид, УНП); видът е `sale` или `storno`. Състояния:

  pending — печатът тече или е прекъснат по време на плащането/затварянето:
            резултатът е НЕИЗВЕСТЕН и бонът може вече да е отпечатан
  done    — бонът е издаден; повторната заявка получава пазения резултат
  failed  — грешка ПРЕДИ плащането (нищо не е отпечатано) — нов опит е позволен

Правилото: повторна заявка с УНП в `pending` НЕ печата — получава
E_RECEIPT_UNKNOWN, докато състоянието не се изясни (GET .../receipt/{uns},
сверка с апарата, ръчно решение). Двоен бон е по-лош от изчакан бон.

Маршрутът отбелязва етапа с `mark()`; обвивката `idempotent_print` решава
по етапа какво е станало при неуспех. Заявка, която още тече в този процес,
не се пуска втори път — втората изчаква резултата на първата.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

_logger = logging.getLogger(__name__)

DEFAULT_PATH = os.environ.get(
    "ODOO_ERPNET_FP_RECEIPT_JOURNAL", "/app/data/receipt_journal.sqlite")

# етапите, след които неуспехът значи „може би е отпечатан“
UNKNOWN_STAGES = {"paying", "closing"}

_STAGE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "erpnet_receipt_stage", default=None)


def mark(stage: str) -> None:
    """Отбелязва докъде е стигнал печатът (вика се от маршрута)."""
    holder = _STAGE.get()
    if holder is not None:
        holder["stage"] = stage


class ReceiptJournal:
    def __init__(self, path: str | None = None) -> None:
        self.path = path or DEFAULT_PATH
        try:
            if self.path != ":memory:":
                Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            self._db = sqlite3.connect(self.path, check_same_thread=False)
        except (OSError, sqlite3.Error) as exc:
            # без диск идемпотентността остава в паметта на процеса
            _logger.warning("Receipt journal %s unavailable (%s) — in memory", self.path, exc)
            self.path = ":memory:"
            self._db = sqlite3.connect(":memory:", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        self._inflight: dict[tuple, asyncio.Event] = {}
        with self._lock, self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS receipts ("
                " printer_id TEXT NOT NULL, kind TEXT NOT NULL, uns TEXT NOT NULL,"
                " state TEXT NOT NULL, stage TEXT, result TEXT,"
                " created TEXT NOT NULL, updated TEXT NOT NULL,"
                " PRIMARY KEY (printer_id, kind, uns))")

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    def get(self, printer_id: str, kind: str, uns: str) -> dict | None:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM receipts WHERE printer_id=? AND kind=? AND uns=?",
                (printer_id, kind, uns)).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["result"] = json.loads(out["result"]) if out["result"] else None
        return out

    def put(self, printer_id: str, kind: str, uns: str, state: str,
            stage: str | None = None, result: dict | None = None) -> None:
        now = self._now()
        with self._lock, self._db:
            self._db.execute(
                "INSERT INTO receipts (printer_id, kind, uns, state, stage, result, created, updated)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (printer_id, kind, uns) DO UPDATE SET"
                " state=excluded.state, stage=excluded.stage, result=excluded.result,"
                " updated=excluded.updated",
                (printer_id, kind, uns, state, stage,
                 json.dumps(result, ensure_ascii=False) if result is not None else None, now, now))

    def pending(self, printer_id: str | None = None) -> list[dict]:
        with self._lock:
            rows = self._db.execute(
                "SELECT printer_id, kind, uns, stage, updated FROM receipts WHERE state='pending'"
                + (" AND printer_id=?" if printer_id else ""),
                (printer_id,) if printer_id else ()).fetchall()
        return [dict(r) for r in rows]


def _unknown(uns: str, stage: str | None) -> dict:
    return {
        "ok": False,
        "messages": [{
            "type": "error", "code": "E_RECEIPT_UNKNOWN",
            "text": f"The receipt with UNS {uns} was interrupted while {stage or 'printing'}; "
                    "it may already be printed. Not printing again — check the device "
                    "(GET /printers/{id}/receipt/{uns}) and resolve it.",
        }],
    }


async def idempotent_print(
    journal: ReceiptJournal | None, printer_id: str, kind: str, uns: str | None,
    run: Callable[[], Awaitable], to_dict: Callable[[object], dict],
    from_dict: Callable[[dict], object],
):
    """Изпълнява `run()` най-много веднъж на (принтер, вид, УНП)."""
    if journal is None or not uns:
        return await run()
    key = (printer_id, kind, uns)

    running = journal._inflight.get(key)
    if running is not None:
        # същият бон още се печата в този процес — чакаме го, не печатаме втори
        await running.wait()

    row = journal.get(*key)
    if row is not None:
        if row["state"] == "done":
            result = dict(row["result"] or {})
            result.setdefault("messages", [])
            result["messages"] = list(result["messages"]) + [{
                "type": "info", "code": "I_ALREADY_PRINTED",
                "text": "Already printed with this UNS — returning the stored result.",
            }]
            return from_dict(result)
        if row["state"] == "pending":
            return from_dict(_unknown(uns, row["stage"]))
        # failed — нищо не е отпечатано, нов опит

    event = asyncio.Event()
    journal._inflight[key] = event
    holder = {"stage": "start"}
    token = _STAGE.set(holder)
    journal.put(*key, state="pending", stage="start")
    try:
        result = await run()
        data = to_dict(result)
        if data.get("ok"):
            journal.put(*key, state="done", stage="done", result=data)
        elif holder["stage"] in UNKNOWN_STAGES:
            journal.put(*key, state="pending", stage=holder["stage"], result=data)
            unknown = _unknown(uns, holder["stage"])
            unknown["messages"] += data.get("messages") or []
            return from_dict(unknown)
        else:
            journal.put(*key, state="failed", stage=holder["stage"], result=data)
        return result
    except BaseException:
        state = "pending" if holder["stage"] in UNKNOWN_STAGES else "failed"
        journal.put(*key, state=state, stage=holder["stage"])
        raise
    finally:
        _STAGE.reset(token)
        journal._inflight.pop(key, None)
        event.set()
