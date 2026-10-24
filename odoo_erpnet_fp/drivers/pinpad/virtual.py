"""
Виртуален пинпад (`driver: virtual`) — за тестове и пясъчник.

Същото API като `DatecsPayPinpad` (фасадата), без устройство и банка.
Поведението на покупката се избира с режим (`set_mode` или
`extras.virtual_mode`):

  approve            — одобрено: RRN и код за одобрение
  decline            — отказ от банката след въвеждане на картата
  immediate_decline  — отказ веднага, без чакане (както нативният драйвер
                       при отказан четец)
  timeout            — клиентът не довършва: покупката виси, докато
                       касата не поиска /cancel (или до таймаута), после
                       отказ „cancelled“/„timeout“

Всяка заявка (покупка, VOID, отказ, статус, …) влиза в дневник, който
тестовете четат през `GET /pinpads/<id>/virtual`.
"""

from __future__ import annotations

import itertools
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from .datecs_pay._native import PinpadInfo, PinpadStatus
from .datecs_pay.facade import TransactionResult

KNOWN_MODES = {"approve", "decline", "immediate_decline", "timeout"}
TERMINAL_ID = "VT000001"


@dataclass
class VirtualPinpadState:
    mode: str = "approve"
    journal: list = field(default_factory=list)
    approved: dict = field(default_factory=dict)  # rrn → {amount, auth_id, voided}
    lock: threading.Lock = field(default_factory=threading.Lock)
    cancel: threading.Event = field(default_factory=threading.Event)
    seq: itertools.count = field(default_factory=lambda: itertools.count(1))

    def log(self, op: str, **data) -> None:
        with self.lock:
            self.journal.append({"op": op, "at": datetime.now().isoformat(timespec="seconds"), **data})


_STATES: dict[str, VirtualPinpadState] = {}
_STATES_LOCK = threading.Lock()


def get_state(pinpad_id: str, mode: Optional[str] = None) -> VirtualPinpadState:
    with _STATES_LOCK:
        if pinpad_id not in _STATES:
            _STATES[pinpad_id] = VirtualPinpadState(mode=mode or "approve")
        return _STATES[pinpad_id]


def set_mode(pinpad_id: str, mode: str) -> str:
    if mode not in KNOWN_MODES:
        raise ValueError(f"Unknown virtual pinpad mode {mode!r}; known: {', '.join(sorted(KNOWN_MODES))}")
    get_state(pinpad_id).mode = mode
    return mode


def reset_state(pinpad_id: str) -> None:
    with _STATES_LOCK:
        _STATES.pop(pinpad_id, None)


class VirtualPinpad:
    """Фасада с API-то на `DatecsPayPinpad`."""

    def __init__(self, pinpad_id: str, mode: Optional[str] = None, timeout: float = 30.0) -> None:
        self.state = get_state(pinpad_id, mode)
        self.timeout = timeout
        self._open = False

    def open(self) -> None:
        self._open = True

    def close(self) -> None:
        self._open = False

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._open

    def request_cancel(self) -> None:
        self.state.log("cancel")
        self.state.cancel.set()

    def ping(self) -> bool:
        return True

    def get_info(self) -> PinpadInfo:
        return PinpadInfo(model_name="Virtual pinpad", serial_number="VP000001",
                          software_version=(1, 0, 0, 0), terminal_id=TERMINAL_ID, menu_type=0)

    def get_status(self) -> PinpadStatus:
        self.state.log("status")
        return PinpadStatus(reversal_status=0, end_day_required=False)

    def purchase(self, amount_cents: int, tip_cents: Optional[int] = None,
                 cashback_cents: Optional[int] = None, reference: Optional[str] = None,
                 timeout: Optional[float] = None) -> TransactionResult:
        st = self.state
        mode = st.mode
        st.log("purchase", amount=amount_cents, reference=reference, mode=mode)
        if mode == "immediate_decline":
            return TransactionResult(ok=False, error="declined", amount_cents=amount_cents,
                                     terminal_id=TERMINAL_ID)
        if mode == "timeout":
            st.cancel.clear()
            cancelled = st.cancel.wait(timeout or self.timeout)
            return TransactionResult(ok=False, error="cancelled" if cancelled else "timeout",
                                     amount_cents=amount_cents, terminal_id=TERMINAL_ID)
        if mode == "decline":
            return TransactionResult(ok=False, error="05 do not honour", amount_cents=amount_cents,
                                     terminal_id=TERMINAL_ID)
        n = next(st.seq)
        rrn, auth_id = f"{612345678900 + n}", f"A{n:05d}"
        with st.lock:
            st.approved[rrn] = {"amount": amount_cents, "auth_id": auth_id, "voided": False}
        return TransactionResult(ok=True, amount_cents=amount_cents, rrn=rrn, auth_id=auth_id,
                                 terminal_id=TERMINAL_ID)

    def void_purchase(self, amount_cents: int, rrn: str, auth_id: str = "",
                      tip_cents: Optional[int] = None, cashback_cents: Optional[int] = None,
                      timeout: Optional[float] = None) -> TransactionResult:
        st = self.state
        with st.lock:
            tx = st.approved.get(rrn)
            known = bool(tx and tx["amount"] == amount_cents and not tx["voided"]
                         and (not auth_id or auth_id == tx["auth_id"]))
            if known:
                tx["voided"] = True
        st.log("void", amount=amount_cents, rrn=rrn, auth_id=auth_id, ok=known)
        if not known:
            return TransactionResult(ok=False, error="no such transaction", amount_cents=amount_cents,
                                     rrn=rrn, terminal_id=TERMINAL_ID)
        return TransactionResult(ok=True, amount_cents=amount_cents, rrn=rrn, auth_id=auth_id,
                                 terminal_id=TERMINAL_ID)

    def end_of_day(self, timeout: Optional[float] = None) -> TransactionResult:
        self.state.log("end_of_day")
        return TransactionResult(ok=True, terminal_id=TERMINAL_ID)

    def test_connection(self, timeout: Optional[float] = None) -> TransactionResult:
        self.state.log("test_connection")
        return TransactionResult(ok=True, terminal_id=TERMINAL_ID)
