# Copyright 2026 Rosen Vladimirov
"""Периодично четене от чужда база и подаване към Odoo.

## Защо синхронен драйвер в нишка, а не async

Проксито е async (FastAPI/uvloop), но еднакъв async път през трите бази
НЕ е възможен: `asyncpg` и `aiomysql` съществуват, а за MS SQL остава
`aioodbc`, който сам е обвивка около `pyodbc` в нишка. Тоест нишка има или
скрита, или явна. Избираме явната — един и същ код за трите, без да се бием
с цикъла на събитията.

Блокиращото четене минава през `anyio.to_thread.run_sync`; `anyio` вече е
налична като зависимост на FastAPI.

## Воден знак

Няма локален воден знак. Източникът сам държи флага (`StoredInOdoo` при
Test Adapter) и Odoo го гаси, след като реално е произвел. Затова тук четем
винаги „неизядените", подредени по време на теста.

🚨 Следствие: между четенето и гасенето един и същи ред може да бъде подаден
пак. Затова идемпотентността е ЗАДЪЛЖИТЕЛНА от страната на Odoo, по
`row_key`. Драйверът не се опитва да я гарантира — не може.

## Какво НЕ прави този модул

Не решава кой работен ордер поема платките и не пипа количества. Само чете
и подава. Решенията са в Odoo, защото само там се вижда състоянието на
производството.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import anyio

from .common import DbSourceEvent, map_row

_logger = logging.getLogger(__name__)

# Таван на партидата. Ingest-ът на Odoo реже тялото на 256 KB; един ред е
# порядъка на 200-300 байта в JSON, тъй че 300 реда стоят с голям запас.
DEFAULT_BATCH = 300

# Долна граница на интервала. Под нея се пита по-често, отколкото Odoo
# успява да произведе, и партидите се застъпват.
MIN_INTERVAL_S = 5.0


@dataclass
class DbSourceConfig:
    """Конфигурация на един източник.

    🔑 Кредитите НЕ живеят тук. `url` се сглобява от променливи на средата,
    за да не влиза парола нито в кода, нито в конфигурационен файл, нито в
    лога. Пример за Test Adapter:

        DBSRC_TA_URL=mssql+pymssql://tester1:***@mssql01.mecproduction.eu/TestAdapter

    Форматът е стандартният SQLAlchemy URL, тъй че същият код обслужва и
    `mysql+pymysql://`, и `postgresql+psycopg://`.
    """

    name: str
    url_env: str
    query: str
    mapping: dict[str, str] = field(default_factory=dict)
    interval_s: float = 30.0
    batch: int = DEFAULT_BATCH
    # Незадължителен филтър, който се подава като свързан параметър.
    params: dict = field(default_factory=dict)
    enabled: bool = True

    @property
    def url(self) -> str:
        return os.environ.get(self.url_env, "")

    def effective_interval(self) -> float:
        return max(MIN_INTERVAL_S, float(self.interval_s or 0))


class DbSourcePoller:
    """Един източник — един engine, един цикъл."""

    def __init__(self, cfg: DbSourceConfig, forwarder, proxy_name: str,
                 metrics=None):
        self.cfg = cfg
        self._forwarder = forwarder
        self._proxy_name = proxy_name
        self._metrics = metrics
        self._engine = None
        self._running = False
        # Само за наблюдаемост — НЕ е състояние, от което зависи четенето.
        self.last_read_at: Optional[datetime] = None
        self.last_row_count = 0
        self.last_error: str = ""

    # ── връзка ───────────────────────────────────────────────────
    def _get_engine(self):
        """Кеширан engine.

        🚨 Engine НА ПОВИКВАНЕ е класическата грешка — вдига нова връзка
        всеки път и изчерпва сървъра. Engine-ът е thread-safe и сам пули,
        затова се създава веднъж.

        `pool_pre_ping` е задължителен: работниците живеят дълго, отсрещната
        база къса неактивни връзки и без ping първата заявка след прекъсване
        гърми. `pool_recycle` е под типичния таймаут на MySQL/прокси.
        """
        if self._engine is not None:
            return self._engine
        url = self.cfg.url
        if not url:
            raise RuntimeError(
                f"dbsource[{self.cfg.name}]: липсва {self.cfg.url_env} "
                f"в средата — без него няма как да се свържем")
        from sqlalchemy import create_engine  # локален импорт: по избор
        self._engine = create_engine(
            url,
            pool_pre_ping=True,
            pool_recycle=1800,
            pool_size=2,
            max_overflow=0,
            future=True,
        )
        return self._engine

    # ── четене (блокиращо, върви в нишка) ────────────────────────
    def _read_batch_blocking(self) -> list:
        from sqlalchemy import text
        engine = self._get_engine()
        params = dict(self.cfg.params)
        params.setdefault("batch", int(self.cfg.batch))
        with engine.connect() as conn:
            # Само свързани параметри. Слепването на стойности в низа е
            # входната точка за SQL инжекция, а част от тези стойности
            # (артикулен код) се редактират от потребител.
            result = conn.execute(text(self.cfg.query), params)
            return list(result)

    async def read_batch(self) -> DbSourceEvent:
        raw_rows = await anyio.to_thread.run_sync(self._read_batch_blocking)
        rows = []
        skipped = 0
        for raw in raw_rows:
            row = map_row(raw, self.cfg.mapping)
            if row is None:
                skipped += 1
                continue
            rows.append(row)
        if skipped:
            _logger.warning(
                "dbsource[%s]: %d ред(а) без естествен ключ — пропуснати, "
                "защото Odoo не може да им гарантира идемпотентност",
                self.cfg.name, skipped)
        watermark = rows[-1].source_timestamp if rows else ""
        self.last_read_at = datetime.now(timezone.utc)
        self.last_row_count = len(rows)
        return DbSourceEvent(
            source_name=self.cfg.name,
            rows=rows,
            high_watermark=watermark,
        )

    # ── подаване ─────────────────────────────────────────────────
    async def push(self, event: DbSourceEvent) -> bool:
        if not event.rows:
            return True
        body = event.as_ingest_body(self._proxy_name)
        ok = await self._forwarder(body)
        if self._metrics:
            self._metrics.observe(self.cfg.name, len(event.rows), ok)
        return bool(ok)

    # ── цикъл ────────────────────────────────────────────────────
    async def run(self) -> None:
        """Върти до отмяна. Никога не умира от грешка в един кръг.

        Грешка при четене или подаване се логва и цикълът изчаква — падналата
        база или спрян Odoo не бива да убиват драйвера, защото после никой
        няма да го вдигне.
        """
        self._running = True
        interval = self.cfg.effective_interval()
        _logger.info(
            "dbsource[%s]: старт, интервал %.0fs, партида %d",
            self.cfg.name, interval, self.cfg.batch)
        while self._running:
            try:
                event = await self.read_batch()
                if event.rows:
                    sent = await self.push(event)
                    _logger.info(
                        "dbsource[%s]: %d ред(а) %s",
                        self.cfg.name, len(event.rows),
                        "подадени" if sent else "НЕ подадени")
                self.last_error = ""
            except anyio.get_cancelled_exc_class():
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                _logger.exception("dbsource[%s]: кръгът пропадна",
                                  self.cfg.name)
            await anyio.sleep(interval)

    def stop(self) -> None:
        self._running = False

    # ── състояние за /status ─────────────────────────────────────
    def snapshot(self) -> dict:
        return {
            "name": self.cfg.name,
            "enabled": self.cfg.enabled,
            "interval_s": self.cfg.effective_interval(),
            "batch": self.cfg.batch,
            "configured": bool(self.cfg.url),
            "last_read_at": (self.last_read_at.isoformat()
                             if self.last_read_at else None),
            "last_row_count": self.last_row_count,
            "last_error": self.last_error,
        }


__all__ = ["DbSourceConfig", "DbSourcePoller", "DEFAULT_BATCH"]
