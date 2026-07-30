# Copyright 2026 Rosen Vladimirov
"""Външна база като източник на събития — нормализиран вид.

Проксито чете таблица в чужда база (типично Test Adapter на MS SQL) и
подава редовете на Odoo по СЪЩИЯ подписан канал, по който минава CFX.
Пликът е нов вид съобщение, а не CFX плик: редът от тестова станция не е
машинно CFX съобщение и преструвката щеше да ни се върне при първия, който
чете лога.

Разделението на отговорностите е нарочно и важно:

* тук се знае САМО за източника — връзка, заявка, воден знак, партиди;
* КОЙ работен ордер поема платките решава Odoo, защото само той вижда
  състоянието на производството (затворен ли е ордерът, какъв е остатъкът,
  кой е следващият в редицата);
* записите обратно в чуждата база (гасене на флага, пренасочване на
  ордера) също минават през проксито, но по НАРЕЖДАНЕ от Odoo — така
  драйвер за MS SQL не влиза в Odoo образа изобщо.

🚨 Известен дефект в източника (MEC, 30.07.2026): Test Adapter НЕ чете
статуса на работния ордер и продължава да пълни в стария, след като той
се е напълнил, при това с различно количество от заявеното в Odoo.
Затова тук се брои отклонението — иначе пренасочването лекува симптома и
разминаването изчезва от погледа.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

# Вид съобщение за Odoo страната. НЕ преизползваме CFX имената — Odoo
# избира extractor по това име и смесването би пратило табличен ред при
# машинния парсер.
MESSAGE_NAME = "DbSourceRows"

# Тип за живата шина (същата азбука като CFX: `<източник>.<събитие>`).
BUS_TYPE = "dbsource.rows"


@dataclass
class DbSourceRow:
    """Един ред от чуждата таблица, вече нормализиран.

    Имената са неутрални нарочно: утре източникът може да е MySQL или
    Postgres с други колони, а мапингът живее в конфигурацията, не в кода.
    """

    # Естественият ключ на реда в източника. ТОВА е идентичността, по
    # която Odoo прави идемпотентност — един и същи ред, подаден два пъти,
    # не бива да произведе две платки.
    row_key: str
    # Работният ордер, както го твърди ИЗТОЧНИКЪТ. Може да е остарял —
    # точно това е дефектът, който Odoo после поправя.
    claimed_wo: str = ""
    product_code: str = ""
    # Партидата на панела и на единичната платка.
    panel_ref: str = ""
    unit_ref: str = ""
    unit_ref_internal: str = ""
    employee_ref: str = ""
    employee_name: str = ""
    # Времето на теста в източника — по него се подрежда и се пази
    # хронологията при частично прехвърляне между ордери.
    source_timestamp: str = ""
    extra: dict = field(default_factory=dict)

    def as_payload(self) -> dict:
        return {
            "row_key": self.row_key,
            "claimed_wo": self.claimed_wo,
            "product_code": self.product_code,
            "panel_ref": self.panel_ref,
            "unit_ref": self.unit_ref,
            "unit_ref_internal": self.unit_ref_internal,
            "employee_ref": self.employee_ref,
            "employee_name": self.employee_name,
            "source_timestamp": self.source_timestamp,
            **({"extra": self.extra} if self.extra else {}),
        }


@dataclass
class DbSourceEvent:
    """Партида редове, готова за подаване към Odoo.

    Партидата НЕ е произволна: тя е ограничена по брой редове, защото
    ingest-ът на Odoo реже тялото на 256 KB. При първо пускане срещу
    натрупана таблица неограниченият прочит опира точно там.
    """

    source_name: str
    rows: list[DbSourceRow] = field(default_factory=list)
    # Докъде е стигнало четенето — за наблюдаемост, не за състояние.
    # Истинският воден знак е флагът в източника, който Odoo гаси.
    high_watermark: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    source: str = "dbsource"

    @property
    def message_name(self) -> str:
        return MESSAGE_NAME

    @property
    def bus_type(self) -> str:
        return BUS_TYPE

    def as_ingest_body(self, proxy_name: str) -> dict:
        """Тялото, което отива към `/erpnet_fp/cfx/ingest`.

        Пликът повтаря формата на CFX (v, proxy, machine_kind,
        message_name, ts, data), за да ползва същата автентикация и
        същото маршрутизиране — но `machine_kind` е `dbsource`, тъй че
        Odoo го праща на своя extractor, а не на машинния.
        """
        return {
            "v": 1,
            "proxy": proxy_name,
            "machine_kind": "dbsource",
            "message_name": self.message_name,
            "cfx_handle": self.source_name,
            "transaction_id": "",
            "ts": self.timestamp.isoformat(timespec="milliseconds"),
            "data": {
                "source": self.source_name,
                "row_count": len(self.rows),
                "high_watermark": self.high_watermark,
                "rows": [r.as_payload() for r in self.rows],
            },
        }


def map_row(raw: Any, mapping: dict[str, str]) -> Optional[DbSourceRow]:
    """Ред от заявката → `DbSourceRow` по конфигурационен мапинг.

    `mapping` свързва нашите неутрални имена с колоните на източника,
    напр. `{"unit_ref": "Odoo_LotRef", "claimed_wo": "Odoo_Workorder"}`.
    Липсващ ключ дава празна стойност, а не изключение: една липсваща
    колона не бива да спира цялата партида.

    Ред БЕЗ `row_key` се отхвърля — без естествен ключ Odoo не може да
    гарантира идемпотентност, а мълчаливото подаване значи риск от двойно
    производство.
    """
    try:
        d = dict(raw._mapping)  # SQLAlchemy Row
    except AttributeError:
        try:
            d = dict(raw)
        except (TypeError, ValueError):
            return None

    def pick(field_name: str) -> str:
        col = mapping.get(field_name)
        if not col:
            return ""
        val = d.get(col)
        if val is None:
            return ""
        return str(val).strip()

    row_key = pick("row_key") or pick("unit_ref")
    if not row_key:
        return None

    return DbSourceRow(
        row_key=row_key,
        claimed_wo=pick("claimed_wo"),
        product_code=pick("product_code"),
        panel_ref=pick("panel_ref"),
        unit_ref=pick("unit_ref"),
        unit_ref_internal=pick("unit_ref_internal"),
        employee_ref=pick("employee_ref"),
        employee_name=pick("employee_name"),
        source_timestamp=pick("source_timestamp"),
    )


__all__ = [
    "MESSAGE_NAME",
    "BUS_TYPE",
    "DbSourceRow",
    "DbSourceEvent",
    "map_row",
]
