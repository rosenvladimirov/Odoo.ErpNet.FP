# -*- coding: utf-8 -*-
# Part of Odoo.ErpNet.FP. License: LGPL-3.
"""Common types for the CFX-IPC ingest driver.

A ``CfxEvent`` is the proxy-internal normalised form of one CFX-IPC
message, produced from the ipc-cfx SDK ``(cfx_payload, properties)``
callback. It mirrors ``PlateEvent`` / ``AccessResult`` (a plain dataclass
with ``to_json()``) and knows how to shape itself for both proxy→Odoo
legs:

  * ``to_bus_data()`` — the **LIVE** bus_inject ``data`` payload
    (machine_kind + wo_name + counters + a ``_refresh`` hint so an open
    ``mrp.workorder`` form live-flashes with zero frontend code).
  * ``audit_body()``  — the **AUDIT** signed-POST body
    (``/erpnet_fp/cfx/ingest``; machine_kind-dispatched on the Odoo side).

The proxy stays a thin relay: NO MO/WO resolution here — Odoo matches on
``wo_name`` / ``transaction_id`` in one authoritative place.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

# CamelCase CFX message name (e.g. `MaterialsInstalled`, `WorkStarted`) →
# snake_case bus event suffix (`materials_installed`, `work_started`).
# Двустъпков regex: разделя ABBRWord и wordWord границите.
_CAMEL_BOUND1 = re.compile(r"(.)([A-Z][a-z]+)")
_CAMEL_BOUND2 = re.compile(r"([a-z0-9])([A-Z])")

# Ключове, под които различните CFX съобщения носят WorkOrder идентификатор.
# CFX WorkOrders topic + Work* съобщенията носят WorkOrderIdentifier; пазим
# permissive списък, защото SDK-то може да ги нормализира различно.
_WO_KEYS = (
    "wo_name", "workorder", "work_order", "WorkOrderId", "WorkOrderIdentifier",
    "UnitIdentifier", "workOrderId", "workorder_name",
)
_TXN_KEYS = ("transaction_id", "TransactionId", "transactionId", "cfx-transaction")
_HANDLE_KEYS = ("cfx_handle", "CFXHandle", "Source", "source", "cfx-handle")


def to_snake(name: str) -> str:
    """`MaterialsInstalled` → `materials_installed` (permissive; keeps
    already-snake input intact)."""
    s = _CAMEL_BOUND1.sub(r"\1_\2", name or "")
    s = _CAMEL_BOUND2.sub(r"\1_\2", s)
    return s.replace("-", "_").replace(".", "_").lower().strip("_")


# CFX message class → bus event `type` (PLAN §3 vocabulary). Трябва да е
# ИДЕНТИЧЕН на Odoo AUDIT-страната (cfx_ingest.py `_EVENT_TYPE_MAP`), за да
# emit-ва proxy-live пътят и Odoo-audit-then-live пътят СЪЩИЯ `type` за едно
# и също CFX съобщение — Phase-3 (mrp_display_cfx_patch.js) вижда един
# вокабулар (напр. StationStateChanged → cfx.station_state, НЕ
# cfx.station_state_changed; UnitsInspected → cfx.wo_progress). Непокрити
# съобщения падат обратно на `cfx.<snake(message_name)>`.
_EVENT_TYPE_MAP = {
    "WorkStarted": "cfx.work_started",
    "MaterialsInstalled": "cfx.materials_installed",
    "WorkCompleted": "cfx.work_completed",
    "StationStateChanged": "cfx.station_state",
    "FaultOccurred": "cfx.fault",
    "UnitsInspected": "cfx.wo_progress",
    "UnitsProcessed": "cfx.wo_progress",
}


@dataclass
class CfxEvent:
    """A single normalised CFX-IPC message.

    ``message_name`` is the CFX message type without its module path
    (e.g. ``MaterialsInstalled``); ``machine_kind`` is the proxy-config
    classification (``europlacer`` / ``parmi`` / ``oven`` / ``laser``)
    used by the Odoo AUDIT dispatcher. ``counters`` carries the small
    machine-agnostic KPI numbers the shop-floor cares about (placed
    count, inspected count, station state), extracted best-effort by the
    listener from the CFX body.
    """

    machine_kind: str
    message_name: str
    cfx_handle: str = ""
    transaction_id: str = ""
    wo_name: str = ""
    data: dict = field(default_factory=dict)
    counters: dict = field(default_factory=dict)
    station_state: Optional[str] = None
    timestamp: datetime = field(default_factory=datetime.now)
    # Времето на СЪБИТИЕТО от CFX плика (raw ISO, машинно). Различно от
    # `timestamp`, който е кога проксито е получило съобщението.
    cfx_timestamp: str = ""
    source: str = "cfx"

    # ─── derived ────────────────────────────────────────────────

    @property
    def message_name_snake(self) -> str:
        return to_snake(self.message_name)

    @property
    def bus_type(self) -> str:
        """bus_inject event `type` (vocabulary in PLAN §3: cfx.work_started,
        cfx.materials_installed, cfx.work_completed, cfx.station_state,
        cfx.fault, cfx.wo_progress, …). Ползва `_EVENT_TYPE_MAP` (същия като
        Odoo-страната), за да не се разминат snake-имената (напр.
        StationStateChanged → cfx.station_state, не …_changed); при непознато
        съобщение пада на `cfx.<snake>`."""
        return _EVENT_TYPE_MAP.get(
            self.message_name, f"cfx.{self.message_name_snake}")

    # ─── serialisers ────────────────────────────────────────────

    def to_json(self) -> dict:
        """Full snapshot — used by /cfx/status replay + logging."""
        return {
            "machineKind": self.machine_kind,
            "messageName": self.message_name,
            "cfxHandle": self.cfx_handle,
            "transactionId": self.transaction_id,
            "woName": self.wo_name,
            "counters": dict(self.counters),
            "stationState": self.station_state,
            "timestamp": self.timestamp.strftime("%Y-%m-%dT%H:%M:%S.%f"),
            "cfxTimestamp": self.cfx_timestamp,
            "source": self.source,
            "data": self.data,
        }

    def _refresh_hint(self) -> list[dict]:
        """`data._refresh` for the LIVE leg — makes an open mrp.workorder
        form match-and-flash (live_refresh_controllers.js:81) with zero
        frontend code. Only emitted when we actually carry a WO key."""
        if not self.wo_name:
            return []
        # `mode` е "field" — същото като Odoo-страната (cfx_ingest.py
        # `_emit_wo_refresh`). live_refresh_service.js:174 различава само
        # "list" от всичко останало (→ LIVE_REFRESH_FIELD), така че всяка
        # не-"list" стойност работи; държим я идентична за консистентност.
        return [{
            "model": "mrp.workorder",
            "match_field": "name",
            "match_value": self.wo_name,
            "field": "qty_produced",
            "mode": "field",
        }]

    def to_bus_data(self) -> dict:
        """LIVE bus_inject `data` payload.

        Имената на ключовете са ПОДРАВНЕНИ с Odoo-страната
        (cfx_ingest.py `_emit_wo_refresh`) и с четенето на Shop Floor patch-а
        (mrp_display_cfx_patch.js), за да изглеждат proxy-live пътят и
        Odoo-audit-then-live пътят ИДЕНТИЧНИ на Phase-3:
        `workorder`/`wo_name` (WO ключ), `placed` (overlay брояч),
        `state` (station state) + `_refresh` hint. `counters` +
        `transaction_id` остават като безобидни допълнителни полета.
        """
        out: dict[str, Any] = {
            "machine_kind": self.machine_kind,
            "message_name": self.message_name,
            # Phase-3 чете `data.workorder ?? data.wo_name ?? workorder_id`;
            # даваме и двата алиаса (както Odoo-страната).
            "workorder": self.wo_name,
            "wo_name": self.wo_name,
            "transaction_id": self.transaction_id,
            "counters": dict(self.counters),
        }
        # `placed` на топ ниво (overlay значката го чете) — идва от counters.
        placed = self.counters.get("placed")
        if placed is not None:
            out["placed"] = placed
        # Shop Floor overlay-ът чете `data.state` за station state.
        if self.station_state is not None:
            out["state"] = self.station_state
        refresh = self._refresh_hint()
        if refresh:
            out["_refresh"] = refresh
        return out

    def audit_body(self) -> dict:
        """AUDIT signed-POST body — persisted by the Odoo machine_kind
        dispatcher (POST /erpnet_fp/cfx/ingest).

        Носи `workorder` (когато е известен), защото Odoo-страната
        (`_wo_key` в cfx_ingest.py) чете първо `event["workorder"]`, за да
        emit-не WO live-refresh и от AUDIT пътя — иначе WO контекстът, който
        proxy-то вече е извлякло, се губи.
        """
        body: dict[str, Any] = {
            "machine_kind": self.machine_kind,
            "message_name": self.message_name,
            "transaction_id": self.transaction_id,
            "cfx_handle": self.cfx_handle,
            "data": self.data,
        }
        if self.wo_name:
            body["workorder"] = self.wo_name
        # `ts` = времето на събитието от CFX плика. Odoo-страната
        # (cfx_ingest.py `_write_generic_stat`) чете първо него за
        # `event_time`; без него пада на body-производно време, което
        # повечето съобщения нямат → cfx.machine.stat.event_time остава
        # празен и записите не изплуват в сортираните по време изгледи.
        if self.cfx_timestamp:
            body["ts"] = self.cfx_timestamp
        return body


# ─── extraction helpers (permissive; SDK schema not pinned) ─────────

def _first(d: dict, keys) -> Optional[Any]:
    for k in keys:
        if isinstance(d, dict) and d.get(k):
            return d[k]
    return None


def extract_message_name(payload: dict, properties: dict) -> str:
    """Message type name (no module path).

    Preference order: AMQP property `cfx-message` (the CFX message path,
    e.g. `CFX.Production.Assembly.MaterialsInstalled`) → payload
    `MessageName` → payload top-level single key. Falls back to `Unknown`.
    """
    raw = (properties or {}).get("cfx-message") or (properties or {}).get("cfx_message")
    if raw:
        return str(raw).rsplit(".", 1)[-1]
    mn = (payload or {}).get("MessageName") or (payload or {}).get("messageName")
    if mn:
        return str(mn).rsplit(".", 1)[-1]
    return "Unknown"


def extract_body(payload: dict) -> dict:
    """The CFX MessageBody, if the SDK nests it; else the whole payload."""
    if not isinstance(payload, dict):
        return {}
    for k in ("MessageBody", "messageBody", "Body", "body"):
        v = payload.get(k)
        if isinstance(v, dict):
            return v
    return payload


def extract_timestamp(payload: dict, properties: dict) -> str:
    """The CFX **envelope** `TimeStamp`, raw, as the machine emitted it.

    CFX carries the event time on the envelope, not in `MessageBody` — so
    for most message types (`UnitsInspected`, `UnitsArrived`, …) it is the
    only event time there is. Odoo's `_parse_dt` feeds it straight to
    `datetime.fromisoformat`, which on py3.11+ accepts the .NET 7-digit
    fractional second (`…12.0702018+03:00`), so no reformatting here.

    Returns "" when absent — the caller then leaves `ts` out entirely and
    Odoo falls back to its own body-derived time.
    """
    for k in ("TimeStamp", "timeStamp", "Timestamp", "timestamp"):
        v = (payload or {}).get(k)
        if v:
            return str(v)
    for k in ("cfx-timestamp", "cfx_timestamp"):
        v = (properties or {}).get(k)
        if v:
            return str(v)
    return ""


def extract_wo_name(payload: dict, body: dict) -> str:
    v = _first(body, _WO_KEYS) or _first(payload, _WO_KEYS)
    return str(v) if v else ""


def extract_transaction_id(payload: dict, body: dict, properties: dict) -> str:
    v = (_first(body, _TXN_KEYS) or _first(payload, _TXN_KEYS)
         or _first(properties or {}, _TXN_KEYS))
    return str(v) if v else ""


def extract_handle(payload: dict, properties: dict) -> str:
    v = _first(properties or {}, _HANDLE_KEYS) or _first(payload, _HANDLE_KEYS)
    return str(v) if v else ""


def extract_counters(message_name: str, body: dict) -> dict:
    """Small machine-agnostic KPI numbers for the shop-floor overlay.

    Best-effort, keyed on the CFX message name. CFX field sets differ per
    machine (PLAN §6 open item), so this stays permissive — a missing
    field just yields no counter rather than an error.
    """
    counters: dict[str, Any] = {}
    if not isinstance(body, dict):
        return counters
    name = (message_name or "").lower()
    if "materialsinstalled" in name:
        # CFX MaterialsInstalled → InstalledMaterials[] (one per placed part).
        installed = (body.get("InstalledMaterials") or body.get("installedMaterials")
                     or body.get("Materials"))
        if isinstance(installed, list):
            counters["placed"] = len(installed)
    elif "unitsinspected" in name or "inspected" in name:
        inspected = (body.get("Inspections") or body.get("inspections")
                     or body.get("Units"))
        if isinstance(inspected, list):
            counters["inspected"] = len(inspected)
    return counters
