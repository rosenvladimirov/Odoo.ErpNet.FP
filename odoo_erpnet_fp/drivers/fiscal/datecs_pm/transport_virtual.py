"""
Виртуален апарат Datecs PM — транспорт без хардуер (`transport: virtual`).

Говори протокола на ниво кадри: заявката се декодира, „апаратът“ я
изпълнява върху своето състояние и връща кадър с ErrorCode и 8 байта
статус. Затова фасадата (`PmDevice`), маршрутите и разборът на отговорите
вървят по истинския си код — симулира се само апаратът.

Предназначение (план o-experts POS, Ф0): всяка фискална промяна да се
доказва без жив апарат — турове в Odoo, pytest на проксито, мутации.

Състоянието живее на ниво модул, по id на принтера: регистърът създава
нов драйвер и нов транспорт за всяка заявка, а бележката, броячите и
дневникът трябва да преживеят това. По желание дневникът се пише и във
файл (JSON), за да се гледа отвън (`extras.virtual_journal`).

Инжектирани грешки (`set_faults` или `extras.virtual_faults`):
  no_paper          — статус „край на хартията“, отварянето на бон отказва
  error_on_open     — отварянето отказва с обща грешка
  error_on_close    — затварянето отказва; бонът остава отворен
  timeout_on_close  — бонът СЕ ЗАПИСВА, но отговор не идва (най-опасният
                      случай: печатът е станал, касата не знае)
  no_status         — апаратът отказва 0x4C (състоянието на бона): сверката
                      не може да реши и прекъснатият бон остава pending
"""

from __future__ import annotations

import json
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from . import codec, commands, frame
from .transport import Transport, TransportClosed, TransportError

# Буквите на групите в отговора на отчетите (както `PmDevice.REPORT_LETTERS`)
REPORT_LETTERS = ("A", "B", "C", "D", "E", "F", "G", "H")
_CYR_TO_LAT = dict(zip("АБВГДЕЖЗ", REPORT_LETTERS))

# Кодовете на апарата (data/error_codes.csv)
ERR_IO = -100001
ERR_FP_INVALID_COMMAND = -112000
ERR_FP_NO_PAPER = -112006
ERR_R_OPEN_BON = -111015
ERR_R_PAY_NOT_STARTED = -111050

KNOWN_FAULTS = {"no_paper", "error_on_open", "error_on_close", "timeout_on_close", "no_status",
                "offline"}


@dataclass
class VirtualPmState:
    printer_id: str
    model: str = "Virtual PM"
    firmware: str = "1.00"
    serial: str = "VT000001"
    fm_serial: str = "VF000001"
    tax_number: str = "000000000"
    slip_no: int = 0
    z_no: int = 0
    open: dict | None = None
    journal: list[dict] = field(default_factory=list)
    daily: dict[str, float] = field(default_factory=lambda: dict.fromkeys(REPORT_LETTERS, 0.0))
    storno_daily: dict[str, float] = field(default_factory=lambda: dict.fromkeys(REPORT_LETTERS, 0.0))
    cash_in: float = 0.0
    cash_out: float = 0.0
    cash_safe: float = 0.0
    faults: set[str] = field(default_factory=set)
    journal_path: str | None = None
    lock: threading.RLock = field(default_factory=threading.RLock, repr=False)

    # ─── статусът: 8 байта, бит 7 винаги вдигнат ────────────────────

    def status(self, error: bool = False) -> bytes:
        b = bytearray(b"\x80" * 8)
        if error:
            b[0] |= 0x20  # general_error
        if self.open:
            b[2] |= 0x08  # fiscal_open
        if "no_paper" in self.faults:
            b[2] |= 0x01  # end_of_paper
        b[4] |= 0x04 | 0x02  # serial_fm_set, tax_number_set
        b[5] |= 0x10 | 0x08 | 0x02  # vat_set, fiscalized, fm_formatted
        return bytes(b)

    # ─── дневникът ──────────────────────────────────────────────────

    def documents(self, kind: str | None = None) -> list[dict]:
        return [d for d in self.journal if kind is None or d["kind"] == kind]

    def _persist(self) -> None:
        if not self.journal_path:
            return
        path = Path(self.journal_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps({
            "printer_id": self.printer_id,
            "slip_no": self.slip_no,
            "z_no": self.z_no,
            "open": self.open,
            "journal": self.journal,
        }, ensure_ascii=False, indent=2))
        tmp.replace(path)

    # ─── командите ──────────────────────────────────────────────────

    def handle(self, req: frame.Request) -> tuple[bytes, bytes] | None:
        """Изпълнява заявката; връща (DATA, статус) или None = без отговор."""
        with self.lock:
            fields = codec.decode_data(req.data)
            handler = _HANDLERS.get(req.cmd)
            return handler(self, fields) if handler else self._err(ERR_FP_INVALID_COMMAND)

    def _ok(self, *values: Any) -> tuple[bytes, bytes]:
        return codec.encode_data(0, *values), self.status()

    def _err(self, code: int) -> tuple[bytes, bytes]:
        return codec.encode_data(code), self.status(error=True)

    def _now(self) -> str:
        return datetime.now().strftime("%d-%m-%y %H:%M:%S")

    def cmd_status(self, _f):
        return b"", self.status()

    def cmd_device_info(self, _f):
        # <Type>\t<Version>\t<Date>\t<Time>\t<Cert>\t<SerialNumber>\t<FmSerialNumber>
        return codec.encode_data(
            self.model, self.firmware, "24-10-26", "12:00", "VIRTUAL", self.serial, self.fm_serial,
        ), self.status()

    def cmd_tax_number(self, _f):
        return self._ok(self.tax_number)

    def cmd_datetime(self, _f):
        return self._ok(self._now())

    def _open(self, kind: str, nsale: str, extra: dict) -> tuple[bytes, bytes]:
        if "no_paper" in self.faults:
            return self._err(ERR_FP_NO_PAPER)
        if "error_on_open" in self.faults:
            return self._err(ERR_IO)
        if self.open:
            return self._err(ERR_R_OPEN_BON)
        self.open = {
            "kind": kind, "nsale": nsale or "", "slip": self.slip_no + 1,
            "lines": [], "total": 0.0, "paid": 0.0, "payments": [], **extra,
        }
        self._persist()
        return self._ok(self.open["slip"])

    def cmd_open_fiscal(self, f):
        # OpCode, OpPwd, [NSale,] TillNmb, Invoice — NSale има формата LLDDDDDD-CCCC-DDDDDDD
        nsale = f[2] if len(f) > 4 and "-" in (f[2] or "") else ""
        return self._open("sale", nsale, {"invoice": bool(f[-1:] and f[-1] == "I")})

    def cmd_open_storno(self, f):
        # OpCode, OpPwd, TillNmb, Storno, DocNum, DateTime, FMNumber, Invoice, ToInvoice, Reason, NSale
        g = (f + [""] * 11)[:11]
        return self._open("storno", g[10], {
            "storno_type": g[3], "orig_doc": g[4], "orig_datetime": g[5], "orig_fm": g[6],
        })

    def _add_line(self, name: str, letter: str, price: float, qty: float, disc: float | None):
        if not self.open:
            return self._err(ERR_FP_INVALID_COMMAND)
        amount = round(price * qty * (1 - (disc or 0) / 100), 2)
        group = _CYR_TO_LAT.get(letter, letter if letter in REPORT_LETTERS else "B")
        self.open["lines"].append({
            "name": name, "group": group, "price": price, "qty": qty, "discount": disc, "amount": amount,
        })
        self.open["total"] = round(self.open["total"] + amount, 2)
        self._persist()
        return self._ok(f"{self.open['total']:.2f}")

    def cmd_sale(self, f):
        # PluName, TaxCd, Price, Quantity, DiscountType, DiscountValue, Department
        g = (f + [""] * 7)[:7]
        disc = float(g[5]) if g[4] and g[5] else None
        return self._add_line(g[0], g[1], float(g[2] or 0), float(g[3] or 1), disc)

    def cmd_sale_plu(self, f):
        # PluNumber, Quantity, [Price], [DiscountType], [DiscountValue]
        g = (f + [""] * 5)[:5]
        disc = float(g[4]) if g[3] and g[4] else None
        return self._add_line(f"PLU {g[0]}", "Б", float(g[2] or 0), float(g[1] or 1), disc)

    def cmd_subtotal(self, _f):
        if not self.open:
            return self._err(ERR_FP_INVALID_COMMAND)
        return self._ok(f"{self.open['total']:.2f}")

    def cmd_payment(self, f):
        if not self.open:
            return self._err(ERR_FP_INVALID_COMMAND)
        g = (f + ["", ""])[:2]
        remaining = round(self.open["total"] - self.open["paid"], 2)
        amount = float(g[1]) if g[1] else remaining
        self.open["paid"] = round(self.open["paid"] + amount, 2)
        self.open["payments"].append({"type": int(g[0] or 0), "amount": amount})
        self._persist()
        if self.open["paid"] + 0.004 >= self.open["total"]:
            return self._ok("D", f"{max(0.0, self.open['paid'] - self.open['total']):.2f}")
        return self._ok("R", f"{self.open['total'] - self.open['paid']:.2f}")

    def cmd_close(self, _f):
        if not self.open:
            return self._err(ERR_FP_INVALID_COMMAND)
        if "error_on_close" in self.faults:
            return self._err(ERR_IO)
        if self.open["paid"] + 0.004 < self.open["total"]:
            return self._err(ERR_R_PAY_NOT_STARTED)
        doc = dict(self.open)
        self.slip_no = doc["slip"]
        doc.update(no=self.slip_no, datetime=self._now(), fm=self.fm_serial)
        bucket = self.storno_daily if doc["kind"] == "storno" else self.daily
        for line in doc["lines"]:
            bucket[line["group"]] = round(bucket[line["group"]] + line["amount"], 2)
        self.journal.append(doc)
        self.open = None
        self._persist()
        if "timeout_on_close" in self.faults:
            return None  # бонът е отпечатан, отговор няма
        return self._ok(self.slip_no)

    def cmd_cancel(self, _f):
        if not self.open:
            return self._err(ERR_FP_INVALID_COMMAND)
        if self.open["payments"]:
            # след плащане бонът е фискализиран и не може да се анулира
            return self._err(ERR_FP_INVALID_COMMAND)
        self.open = None
        self._persist()
        return self._ok()

    def cmd_report(self, f):
        kind = (f[0] if f else "").upper()
        if kind not in ("X", "Z"):
            return self._ok()
        if self.open:
            return self._err(ERR_R_OPEN_BON)
        totals = [f"{self.daily[g]:.2f}" for g in REPORT_LETTERS]
        storno = [f"{self.storno_daily[g]:.2f}" for g in REPORT_LETTERS]
        n_rep = self.z_no + 1
        if kind == "Z":
            self.z_no = n_rep
            self.journal.append({
                "kind": "Z", "no": n_rep, "datetime": self._now(),
                "totals": dict(self.daily), "storno": dict(self.storno_daily),
            })
            self.daily = dict.fromkeys(REPORT_LETTERS, 0.0)
            self.storno_daily = dict.fromkeys(REPORT_LETTERS, 0.0)
            self._persist()
        return self._ok(n_rep, *totals, *storno)

    def cmd_cash(self, f):
        g = (f + ["0", "0"])[:2]
        op, amount = int(g[0] or 0), float(g[1] or 0)
        if amount:
            if op == 0:
                self.cash_in = round(self.cash_in + amount, 2)
                self.cash_safe = round(self.cash_safe + amount, 2)
            elif op == 1:
                self.cash_out = round(self.cash_out + amount, 2)
                self.cash_safe = round(self.cash_safe - amount, 2)
            self.journal.append({"kind": "cash_in" if op == 0 else "cash_out", "amount": amount,
                                 "datetime": self._now()})
            self._persist()
        return self._ok(f"{self.cash_safe:.2f}", f"{self.cash_in:.2f}", f"{self.cash_out:.2f}")

    def cmd_drawer(self, _f):
        self.journal.append({"kind": "drawer", "datetime": self._now()})
        self._persist()
        return self._ok()

    def cmd_transaction_status(self, _f):
        # ErrorCode, IsOpen, Number, Items, Amount, Payed (протокол FP-700 §4.34)
        if "no_status" in self.faults:
            return self._err(ERR_FP_INVALID_COMMAND)
        if self.open:
            o = self.open
            return self._ok(1, o["slip"], len(o["lines"]), f"{o['total']:.2f}", f"{o['paid']:.2f}")
        return self._ok(0, self.slip_no, 0, "0.00", "0.00")

    def cmd_duplicate(self, _f):
        last = next((d for d in reversed(self.journal) if d["kind"] in ("sale", "storno")), None)
        if not last:
            return self._err(ERR_FP_INVALID_COMMAND)
        self.journal.append({"kind": "duplicate", "of": last["no"], "datetime": self._now()})
        self._persist()
        return self._ok()


_HANDLERS = {
    commands.CMD_READ_STATUS: VirtualPmState.cmd_status,
    commands.CMD_DEVICE_INFO: VirtualPmState.cmd_device_info,
    commands.CMD_TAX_NUMBER_READ: VirtualPmState.cmd_tax_number,
    commands.CMD_READ_DATETIME: VirtualPmState.cmd_datetime,
    commands.CMD_OPEN_FISCAL_RECEIPT: VirtualPmState.cmd_open_fiscal,
    commands.CMD_OPEN_STORNO: VirtualPmState.cmd_open_storno,
    commands.CMD_REGISTER_SALE: VirtualPmState.cmd_sale,
    commands.CMD_SALE_PROGRAMMED: VirtualPmState.cmd_sale_plu,
    commands.CMD_SUBTOTAL: VirtualPmState.cmd_subtotal,
    commands.CMD_PAYMENT_TOTAL: VirtualPmState.cmd_payment,
    commands.CMD_CLOSE_FISCAL_RECEIPT: VirtualPmState.cmd_close,
    commands.CMD_CANCEL_FISCAL_RECEIPT: VirtualPmState.cmd_cancel,
    commands.CMD_REPORTS: VirtualPmState.cmd_report,
    commands.CMD_CASH_IN_OUT: VirtualPmState.cmd_cash,
    commands.CMD_PRINT_DUPLICATE: VirtualPmState.cmd_duplicate,
    commands.CMD_FISCAL_TRANSACTION_STATUS: VirtualPmState.cmd_transaction_status,
    commands.CMD_OPEN_DRAWER: VirtualPmState.cmd_drawer,
}

# ─── регистърът на състоянията (по id на принтера) ──────────────────

_STATES: dict[str, VirtualPmState] = {}
_STATES_LOCK = threading.Lock()


def get_state(printer_id: str, journal_path: str | None = None,
              faults: list[str] | None = None) -> VirtualPmState:
    with _STATES_LOCK:
        state = _STATES.get(printer_id)
        if state is None:
            state = VirtualPmState(printer_id=printer_id, journal_path=journal_path,
                                   faults=set(faults or ()))
            _STATES[printer_id] = state
        return state


def reset_state(printer_id: str) -> None:
    with _STATES_LOCK:
        _STATES.pop(printer_id, None)


def set_faults(printer_id: str, faults: list[str] | set[str]) -> set[str]:
    unknown = set(faults) - KNOWN_FAULTS
    if unknown:
        raise ValueError(f"Unknown virtual faults: {sorted(unknown)}")
    state = get_state(printer_id)
    with state.lock:
        state.faults = set(faults)
        return set(state.faults)


class VirtualPmTransport(Transport):
    """Transport към виртуалния апарат на даден принтер."""

    def __init__(self, printer_id: str, journal_path: str | None = None,
                 faults: list[str] | None = None) -> None:
        self.state = get_state(printer_id, journal_path, faults)
        self._opened = False
        self._inbox: deque[bytes] = deque()
        # повторен SEQ в същата връзка (хостът не е получил отговор) получава
        # СЪЩИЯ отговор, без командата да се изпълни отново — както апаратът;
        # при timeout_on_close и повторението остава без отговор. Само в
        # рамките на връзката: всеки нов драйвер почва от SEQ 0x20.
        self._last: tuple | None = None

    def open(self) -> None:
        # offline: апаратът не отговаря изобщо (изключен, без мрежа) — като
        # неуспешна TCP връзка, преди да е изпратена каквато и да е команда
        if "offline" in self.state.faults:
            raise TransportError("Virtual PM offline (fault)")
        self._opened = True

    def close(self) -> None:
        self._opened = False
        self._inbox.clear()

    def is_open(self) -> bool:
        return self._opened

    def write(self, data: bytes) -> None:
        if not self._opened:
            raise TransportClosed("Virtual PM not opened")
        req = frame.decode_request(data)
        key = (req.seq, req.cmd, req.data)
        if self._last is not None and self._last[0] == key:
            result = self._last[1]
        else:
            result = self.state.handle(req)
            self._last = (key, result)
        if result is None:
            return  # без отговор — хостът ще изчака и ще получи таймаут
        resp_data, resp_status = result
        self._inbox.append(frame.encode_response(req.seq, req.cmd, resp_data, resp_status))

    def read(self, n: int, timeout: float) -> bytes:
        if not self._opened:
            raise TransportClosed("Virtual PM not opened")
        if not self._inbox:
            return b""
        head = self._inbox[0]
        if len(head) <= n:
            self._inbox.popleft()
            return head
        self._inbox[0] = head[n:]
        return head[:n]

    def read_until(self, terminator: bytes, max_bytes: int, timeout: float) -> bytes:
        if not self._opened:
            raise TransportClosed("Virtual PM not opened")
        if not self._inbox:
            return b""
        head = self._inbox.popleft()
        if terminator in head:
            idx = head.index(terminator) + len(terminator)
            if idx < len(head):
                self._inbox.appendleft(head[idx:])
            return head[:idx]
        return head
