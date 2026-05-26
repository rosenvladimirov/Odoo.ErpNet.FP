"""access_decision.context_builder — proxy-side mirror на Odoo
access.context.builder.

⚠️ ВАЖНО: `derive_direction()` функцията ТРЯБВА да остане байт-идентична
с Odoo-side `access.context.builder._derive_direction()` (виж
~/Проекти/odoo/odoo-19.0/product-design-properties/access_control/
models/access_context_builder.py). Версия се pin-ва към ZEN graph
version през heartbeat payload (`builder_version` field).

Защо отделен Python helper, не ZEN правило:
ZEN/GoRules JDM е stateless decision engine — eval-ва flat context →
output. Patterns които изискват sequence/timing memory (tailgating,
exit_without_entry, denied_but_opened, forced) изискват достъп до
предишно събитие. Това е stateful preprocessing което само Python
може. Виж Open Q#4 на PLAN_base_zen_decision_access_control.md.
"""

from datetime import datetime, timedelta, timezone


# Version pinned through heartbeat. Bump when derive_direction logic
# changes — Odoo сравнява с expected_builder_version и refuse-ва ако
# mismatch (fail-secure deny + push alert).
BUILDER_VERSION = "1.0.0"

# Window constants — SAME values като Odoo версията.
_TAILGATE_WINDOW_SEC = 3
_PREV_EVENT_WINDOW_SEC = 10
_HELD_THRESHOLD_SEC = 30


def derive_direction(signal_matrix, prev_event, credential=None):
    """Pure-Python pattern matching от signal matrix + предходно събитие.

    Returns: (direction: 'in'|'out'|None, anomaly_hint: str|None)

    Patterns (BYTE-IDENTICAL with Odoo
    access.context.builder._derive_direction):
    - ext fires + magnet open + door open    → ('in', None)
    - int fires + magnet open + door open    → ('out', None)
    - magnet break без reader pulse           → (None, 'forced')
    - ext fires + prev_in за същата карта <3s → ('in', 'tailgating')
    - int fires без предходен external        → (None, 'exit_without_entry')
    - ext denied + door opened                → ('in', 'denied_but_opened')
    - magnet open > held threshold            → (None, 'held')
    """
    sm = signal_matrix or {}
    ext = sm.get("external_reader") or sm.get("ext")
    int_r = sm.get("internal_reader") or sm.get("int")
    magnet = sm.get("magnet")
    door = sm.get("door")

    if magnet == "open_too_long":
        return (None, "held")

    if magnet == "break" and not ext and not int_r:
        return (None, "forced")

    if ext == "denied" and door == "opened":
        return ("in", "denied_but_opened")

    if ext in ("fire", "accept") and magnet in ("open", "released") \
            and door in ("opened", None):
        # Tailgating check: prev event е "in" за same credential <3s
        if prev_event and prev_event.get("direction") == "in" \
                and credential is not None \
                and prev_event.get("credential_id") == credential.get("id") \
                and prev_event.get("ts") \
                and (datetime.now(tz=timezone.utc)
                     - _parse_ts(prev_event["ts"])).total_seconds() \
                    < _TAILGATE_WINDOW_SEC:
            return ("in", "tailgating")
        return ("in", None)

    if int_r in ("fire", "accept") and magnet in ("open", "released") \
            and door in ("opened", None):
        if not prev_event or prev_event.get("direction") != "in":
            return (None, "exit_without_entry")
        return ("out", None)

    return (None, None)


def _parse_ts(ts):
    if isinstance(ts, datetime):
        return ts
    if isinstance(ts, str):
        # Tolerant ISO parsing
        return datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return datetime.now(tz=timezone.utc)


def build_context(control_point, credential, signal_matrix,
                   prev_event=None, ts=None,
                   perimeter_chain=None, parent_present=False,
                   within_window=True, tolerance_minutes=15):
    """Mirror на Odoo-side build_context. Caller (декишен endpoint)
    подава всички dimensions като args — proxy не държи DB на perimeters/
    occupancy, разчита на Odoo да push push към него или да query-не
    локален cache.

    Returns dict готов за ZEN.evaluate().
    """
    ts = ts or datetime.now(tz=timezone.utc)
    direction, anomaly_hint = derive_direction(
        signal_matrix, prev_event, credential)
    cred_valid = _credential_valid(credential, ts)
    return {
        "signal": dict(signal_matrix or {}),
        "direction": direction,
        "anomaly_hint": anomaly_hint,
        "perimeter_id": (control_point or {}).get("perimeter_id"),
        "perimeter_code": (control_point or {}).get("perimeter_code"),
        "perimeter_chain_codes": perimeter_chain or [],
        "requires_parent_presence": (control_point or {}).get(
            "requires_parent_presence", False),
        "parent_present": parent_present,
        "enforcement": (control_point or {}).get("enforcement", "soft"),
        "ts": ts.isoformat(),
        "within_window": within_window,
        "tolerance_minutes": tolerance_minutes,
        "credential_id": (credential or {}).get("id"),
        "credential_kind": (credential or {}).get("credential_kind"),
        "credential_active": cred_valid,
        "subject_id": (credential or {}).get("subject_id"),
    }


def _credential_valid(credential, ts):
    if not credential or not credential.get("active", True):
        return False
    valid_from = credential.get("valid_from")
    valid_to = credential.get("valid_to")
    if valid_from:
        if _parse_ts(valid_from) > ts:
            return False
    if valid_to:
        if _parse_ts(valid_to) < ts:
            return False
    return True
