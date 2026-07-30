# Copyright 2026 Rosen Vladimirov
"""Нареждания от Odoo към източника.

Odoo решава, проксито изпълнява. Разделението не е стилово: връзката към
чуждата база живее в проксито, значи **никакъв драйвер за MS SQL не влиза в
Odoo образа** и пиновете му остават непокътнати. Обратното — Odoo да пипа
чуждата база директно — щеше да значи MS SQL драйвер в Odoo и още едно
място, където версиите могат да се разфазират.

Две нареждания, точно колкото уговорихме, и нито едно повече:

* `mark_stored`  — гаси флага за изядените редове (`StoredInOdoo = 1`);
* `retag`        — пренасочва редове към друг работен ордер.

🚨 Схемата на чуждата таблица НЕ се пипа: не добавяме колонки. Затова
одит следата „кой ред накъде отиде" живее в Odoo, а тук се изпълнява само
самото пренасочване.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

import anyio

_logger = logging.getLogger(__name__)

# Таван на един пакет ключове в едно нареждане. Държи `IN (...)` списъка в
# разумни граници — MS SQL има таван на параметрите в заявка, а и един
# гигантски UPDATE държи ключалки върху таблица, в която ТА пише.
MAX_KEYS = 500


@dataclass
class CommandResult:
    ok: bool
    affected: int = 0
    error: str = ""

    def as_dict(self) -> dict:
        return {"ok": self.ok, "affected": self.affected,
                "error": self.error}


class DbSourceCommands:
    """Изпълнява нареждания срещу конфигуриран източник."""

    def __init__(self, poller):
        # Преизползваме engine-а на четеца — един източник, една връзка.
        self._poller = poller

    # ── публични ─────────────────────────────────────────────────
    async def mark_stored(self, keys: list[str]) -> CommandResult:
        """Гаси флага за подадените редове.

        Идемпотентно по конструкция: филтърът иска флагът да е още вдигнат,
        тъй че повторно нареждане не пипа нищо и връща нула засегнати.
        """
        cfg = self._poller.cfg
        col_key = cfg.mapping.get("row_key") or cfg.mapping.get("unit_ref")
        stored_col = cfg.mapping.get("stored_flag")
        table = cfg.params.get("table") or cfg.params.get("source_table")
        if not (col_key and stored_col and table):
            return CommandResult(
                False, error="mapping/table непълни за mark_stored")
        return await self._run_batched(
            keys,
            f"UPDATE {table} SET {stored_col} = 1 "
            f"WHERE {stored_col} = 0 AND {col_key} IN :keys",
        )

    async def retag(self, keys: list[str], new_wo: str) -> CommandResult:
        """Пренасочва редове към друг работен ордер.

        Само неизядени редове — вече произведен ред не се пренасочва,
        защото платката му е влязла някъде и промяната би скрила следата.
        """
        cfg = self._poller.cfg
        col_key = cfg.mapping.get("row_key") or cfg.mapping.get("unit_ref")
        wo_col = cfg.mapping.get("claimed_wo")
        stored_col = cfg.mapping.get("stored_flag")
        table = cfg.params.get("table") or cfg.params.get("source_table")
        if not (col_key and wo_col and stored_col and table):
            return CommandResult(
                False, error="mapping/table непълни за retag")
        if not new_wo:
            return CommandResult(False, error="липсва целеви ордер")
        return await self._run_batched(
            keys,
            f"UPDATE {table} SET {wo_col} = :new_wo "
            f"WHERE {stored_col} = 0 AND {col_key} IN :keys",
            extra={"new_wo": new_wo},
        )

    # ── партидиране ──────────────────────────────────────────────
    async def _run_batched(self, keys, sql, extra=None) -> CommandResult:
        clean = [str(k).strip() for k in (keys or []) if str(k).strip()]
        if not clean:
            return CommandResult(True, 0)
        from sqlalchemy import bindparam, text  # noqa: F401
        total = 0
        try:
            for i in range(0, len(clean), MAX_KEYS):
                chunk = clean[i:i + MAX_KEYS]
                params = dict(extra or {})
                params["keys"] = tuple(chunk)
                # `expanding` разгъва кортежа в свързани параметри — така
                # ключовете НЕ се слепват в низа и инжекция не е възможна.
                stmt = text(sql).bindparams(
                    bindparam("keys", expanding=True))
                total += await anyio.to_thread.run_sync(
                    lambda s=stmt, p=params: self._exec_stmt_blocking(s, p))
            return CommandResult(True, total)
        except Exception as exc:  # noqa: BLE001
            _logger.exception("dbsource[%s]: нареждането пропадна",
                              self._poller.cfg.name)
            return CommandResult(False, total, str(exc))

    def _exec_stmt_blocking(self, stmt, params) -> int:
        engine = self._poller._get_engine()
        with engine.begin() as conn:
            res = conn.execute(stmt, params)
            return int(res.rowcount or 0)


__all__ = ["DbSourceCommands", "CommandResult", "MAX_KEYS"]
