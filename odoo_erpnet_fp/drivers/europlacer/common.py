# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""Europlacer order / answer data model + ``.ans`` ``<Error>`` code table.

The proxy does NOT build the order XML — Odoo hands it a ready payload
(ADR-ERPNET-0005: "Odoo дава ГОТОВИЯ XML — проксито само го доставя и
чете отговора"). This module only carries the order/outcome dataclasses
and the reverse-engineered ``<Error>N</Error>`` semantics.

Source of the code table: the Odoo 11 ``dxf`` module
``europlacer/data/europlacer_mapper_error_data.xml`` — the ONLY source
(the "Europlacer communication" manual is not available). The proxy
reads ``<Error>`` exactly as Odoo 11 did
(``europlacer_trac.py:934`` — ``getElementsByTagName('Error')[0]``), but
adds the WAIT + RETRY the Odoo 11 code never had (it did a single
immediate check and only warned on mismatch, :935-939).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional
from xml.etree import ElementTree as ET

# ─── <Error>N</Error> code table (Odoo 11 europlacer_mapper_error_data) ──

# Пълната таблица код → значение (реверс-инженерна, единствен източник).
ERROR_MEANINGS: dict[int, str] = {
    0: "OK",
    1: "action unknown",
    2: "object unknown",
    3: "empty object name",
    4: "object locked",
    5: "object already existing",
    6: "object does not exist",
    7: "impossible to delete the article",
    8: "impossible to delete the carrier",
    9: "bad value",
    10: "memory error",
    11: "XML file error",
    12: "stock database can not be opened",
    13: "stock database or record locked",
    14: "file management error",
    1000: "error in stock management database",
}

# 0 = OK, 5 = "object already existing" → идемпотентен успех (заявката вече
# е приложена, повторното подаване е безвредно).
SUCCESS_CODES: frozenset[int] = frozenset({0, 5})

# Временни (locking / IO / memory / DB недостъпна) — заслужават retry.
TRANSIENT_CODES: frozenset[int] = frozenset({4, 10, 12, 13, 14})

# Всичко останало (1,2,3,6,7,8,9,11,1000) = фатално — грешна заявка/данни,
# retry няма да помогне.

# ─── outcome states (returned to Odoo) ──────────────────────────────────

STATE_SUCCESS = "success"            # Error ∈ {0,5}
STATE_FATAL = "failed:fatal"         # фатален Error код или лош отговор
STATE_TRANSIENT = "failed:transient"  # transient Error код, retry-ите изчерпани
STATE_TIMEOUT = "failed:timeout"     # никакъв .ans до timeout, retry-ите изчерпани


def classify(code: int) -> str:
    """Класифицирай Error код → ``success`` | ``transient`` | ``fatal``."""
    if code in SUCCESS_CODES:
        return "success"
    if code in TRANSIENT_CODES:
        return "transient"
    return "fatal"


def meaning(code: Optional[int]) -> str:
    """Човешко значение на Error код (за outcome/лог)."""
    if code is None:
        return ""
    return ERROR_MEANINGS.get(code, f"unknown error {code}")


# ─── .ans parsing ───────────────────────────────────────────────────────


class AnswerNotReady(Exception):
    """``.ans`` файлът съществува, но още не е добре оформен XML.

    Машината (често CIFS mount) може да пише файла частично — продължаваме
    да четем, докато стане parse-ваем (има затварящ таг).
    """


class AnswerMalformed(Exception):
    """``.ans`` е добре оформен XML, но няма валиден ``<Error>`` елемент.

    Това е фатален протоколен дефект (не се retry-ва).
    """


def parse_answer(raw: bytes) -> int:
    """Извади int-а от ``<Error>N</Error>`` в ``.ans`` тялото.

    * празно / non-well-formed → :class:`AnswerNotReady` (пиши се още);
    * well-formed без ``<Error>`` или non-int стойност → :class:`AnswerMalformed`;
    * иначе → int кода.

    Търси ``<Error>`` на всяко ниво (root или дете), както Odoo 11
    ``getElementsByTagName('Error')[0]``.
    """
    if not raw or not raw.strip():
        raise AnswerNotReady("empty .ans")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise AnswerNotReady(f"partial .ans: {exc}") from exc
    elem = root if _localname(root.tag) == "Error" else _find_error(root)
    if elem is None or elem.text is None or not elem.text.strip():
        raise AnswerMalformed("no <Error> element in .ans")
    try:
        return int(elem.text.strip())
    except ValueError as exc:
        raise AnswerMalformed(
            f"non-integer <Error> value {elem.text!r}") from exc


def _localname(tag: str) -> str:
    """XML tag без namespace префикс (``{ns}Error`` → ``Error``)."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find_error(root: "ET.Element") -> Optional["ET.Element"]:
    """Първият ``<Error>`` елемент на произволна дълбочина (ns-агностично)."""
    for el in root.iter():
        if _localname(el.tag) == "Error":
            return el
    return None


# ─── dataclasses ────────────────────────────────────────────────────────


@dataclass
class EuroplacerOrder:
    """Един material-order: готовият XML от Odoo + корелационен id."""

    order_id: str
    xml: bytes
    received_at: float = field(default_factory=time.time)
    source: str = ""            # напр. "odoo" / "inject"


@dataclass
class EuroplacerOutcome:
    """Крайният изход на един order (върнат към Odoo + пазен за poll)."""

    order_id: str
    state: str                  # един от STATE_*
    error_code: Optional[int] = None
    error_meaning: str = ""
    attempts: int = 0
    filename: str = ""          # последното записано име на файла
    detail: str = ""
    finished_at: float = field(default_factory=time.time)

    @property
    def ok(self) -> bool:
        return self.state == STATE_SUCCESS

    def to_dict(self) -> dict:
        return {
            "order_id": self.order_id,
            "state": self.state,
            "ok": self.ok,
            "error_code": self.error_code,
            "error_meaning": self.error_meaning,
            "attempts": self.attempts,
            "filename": self.filename,
            "detail": self.detail,
            "finished_at": self.finished_at,
        }
