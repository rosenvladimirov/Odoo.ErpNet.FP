# Copyright 2026 Rosen Vladimirov
"""Регистър на източниците — жизнен цикъл по конвенцията на CFX.

Огледало на `CfxIngestRegistry`: `from_config` / `start_all` / `stop_all`,
изложен на `app.state`. Живее ТУК, а не в `server/service.py`, защото онзи
файл е вече 1500+ реда и нямаше причина да расте за това.

🔑 Ленив импорт по конструкция. `DbSourcePoller` вика `create_engine` вътре
в метода си, а този регистър не прави нищо, когато няма включени източници.
Тоест чисто фискална инсталация не зарежда SQLAlchemy и остава байт за байт
същата — същият договор като при `cfx:`.

Заявката се сглобява от `table`, `mapping` и `batch`, вместо да се пише SQL
на ръка за всеки източник. Двата режима се различават само по това: къде е
водният знак и какво Odoo прави после.
"""
from __future__ import annotations

import asyncio
import logging

from .commands import DbSourceCommands
from .poller import DbSourceConfig, DbSourcePoller

_logger = logging.getLogger(__name__)


def _build_query(spec) -> tuple[str, dict]:
    """SQL за режима на източника + параметри.

    ``production`` — неизядените редове, подредени по време на теста.
    Флагът е в източника, затова няма `WHERE Id > :last`.

    ``lots`` — тесен прочит по колоната `Product` и воден знак от Odoo.
    Двете се подават отвън при всяко четене: артикулът от полето
    `l10n_bg_dbsource_product`, водният знак от `max` на вписаните лотове —
    защото Odoo знае докъде е стигнал, а проксито не.
    """
    m = spec.mapping or {}
    table = spec.table
    key = m.get('row_key') or m.get('unit_ref') or 'Id'
    ts = m.get('source_timestamp') or key

    if spec.mode == 'lots':
        # Съпоставката е по колоната `Product`, НЕ по префикс на серийния
        # номер: артикул с друга схема на номерата не се намира по префикс, а
        # два артикула с общ префикс се привличат взаимно.
        prod = m.get('product_code') or 'Product'
        cols = ', '.join(dict.fromkeys(filter(None, [
            key, m.get('unit_ref'), m.get('customer_ref'), prod,
            m.get('source_timestamp'),
        ])))
        sql = (
            f"SELECT {cols} FROM {table} "
            f"WHERE {prod} = :product_code AND {key} > :last_key "
            f"ORDER BY {key} "
            f"OFFSET 0 ROWS FETCH NEXT :batch ROWS ONLY"
        )
        # `product_code` и `last_key` се подават при ВСЯКО четене: първият от
        # полето на артикула в Odoo, вторият от „докъде сме" — защото Odoo
        # знае докъде е стигнал, а проксито не.
        return sql, {}

    stored = m.get('stored_flag') or 'StoredInOdoo'
    cols = ', '.join(dict.fromkeys(filter(None, [
        key, m.get('claimed_wo'), m.get('product_code'), m.get('panel_ref'),
        m.get('unit_ref'), m.get('unit_ref_internal'),
        m.get('employee_ref'), m.get('employee_name'), ts,
    ])))
    sql = (
        f"SELECT {cols} FROM {table} "
        f"WHERE {stored} = 0 "
        f"ORDER BY {ts} "
        f"OFFSET 0 ROWS FETCH NEXT :batch ROWS ONLY"
    )
    return sql, {'table': table}


class DbSourceRegistry:
    """Всички конфигурирани източници, с общ жизнен цикъл."""

    def __init__(self, forwarder=None, proxy_name: str = ""):
        self.specs: dict = {}
        self.pollers: dict[str, DbSourcePoller] = {}
        self.commands: dict[str, DbSourceCommands] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._forwarder = forwarder
        self._proxy_name = proxy_name

    # ── изграждане ───────────────────────────────────────────────
    @classmethod
    def from_config(cls, config, forwarder=None, app=None) -> "DbSourceRegistry":
        proxy_name = str(getattr(config, 'name', '') or
                         getattr(config, 'proxy_name', '') or '')
        self = cls(forwarder=forwarder, proxy_name=proxy_name)
        for spec in getattr(config, 'dbsource', []) or []:
            if not getattr(spec, 'enabled', True):
                continue
            if not (spec.url_env and spec.table):
                _logger.warning(
                    "dbsource[%s]: без `url_env` или `table` — прескочен",
                    spec.name)
                continue
            sql, params = _build_query(spec)
            params['batch'] = int(spec.batch)
            cfg = DbSourceConfig(
                name=spec.name,
                url_env=spec.url_env,
                query=sql,
                mapping=dict(spec.mapping or {}),
                interval_s=float(spec.interval_s),
                batch=int(spec.batch),
                params=dict(params, table=spec.table, mode=spec.mode),
                enabled=True,
            )
            poller = DbSourcePoller(
                cfg, forwarder=forwarder, proxy_name=proxy_name)
            self.specs[spec.name] = spec
            self.pollers[spec.name] = poller
            self.commands[spec.name] = DbSourceCommands(poller)
        if app is not None:
            app.state.dbsource_registry = self
        return self

    # ── жизнен цикъл ─────────────────────────────────────────────
    def start_all(self) -> None:
        """Вдига по една задача на източник.

        No-op без включени източници — тогава нищо не се импортира и
        нищо не се стартира. Провал на един източник не спира другите:
        цикълът на поллера сам преживява грешките.
        """
        if not self.pollers:
            return
        loop = asyncio.get_event_loop()
        for name, poller in self.pollers.items():
            if name in self._tasks and not self._tasks[name].done():
                continue
            self._tasks[name] = loop.create_task(
                poller.run(), name=f"dbsource:{name}")
        _logger.info("dbsource: вдигнати %d източник(а): %s",
                     len(self._tasks), ', '.join(sorted(self._tasks)))

    def stop_all(self) -> None:
        for poller in self.pollers.values():
            poller.stop()
        for name, task in self._tasks.items():
            if not task.done():
                task.cancel()
        self._tasks.clear()
        if self.pollers:
            _logger.info("dbsource: спрени")

    # ── състояние ────────────────────────────────────────────────
    def snapshot(self) -> dict:
        return {
            'sources': [
                dict(p.snapshot(), mode=self.specs[n].mode,
                     running=bool(self._tasks.get(n)
                                  and not self._tasks[n].done()))
                for n, p in self.pollers.items()
            ],
        }


__all__ = ["DbSourceRegistry", "_build_query"]
