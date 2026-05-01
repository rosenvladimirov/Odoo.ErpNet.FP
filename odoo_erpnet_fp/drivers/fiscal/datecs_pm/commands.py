"""
Datecs PM v2.11.4 command opcodes.

Named constants for commands referenced from `pm_v2_11_4.py`. The full
table of 73 commands is loaded on demand from data/command_map.csv via
`all_commands()` / `name()` for diagnostic and logging use.
"""

import csv
from pathlib import Path

_CSV_PATH = Path(__file__).resolve().parent / "data" / "command_map.csv"

# ---- Phase 1 — back-office invoice minimum viable -----------------------
CMD_READ_STATUS = 0x4A  # 74  — health check, called before everything
CMD_OPEN_FISCAL_RECEIPT = 0x30  # 48
CMD_REGISTER_SALE = 0x31  # 49
CMD_SUBTOTAL = 0x33  # 51
CMD_PAYMENT_TOTAL = 0x35  # 53
CMD_CLOSE_FISCAL_RECEIPT = 0x38  # 56
CMD_CANCEL_FISCAL_RECEIPT = 0x3C  # 60

# ---- Phase 2 — back-office complete -------------------------------------
CMD_INVOICE_DATA = 0x39  # 57  — fiscal *invoice* receipt
CMD_FISCAL_TRANSACTION_STATUS = 0x4C  # 76
CMD_PRINT_DUPLICATE = 0x6D  # 109
CMD_CASH_IN_OUT = 0x46  # 70
CMD_REPORTS = 0x45  # 69  — X / Z / D / G / P
CMD_LAST_FISCAL_ENTRY = 0x40  # 64
CMD_DAILY_TAXATION = 0x41  # 65

# ---- Phase 3 — POS specific ---------------------------------------------
CMD_PINPAD = 0x37  # 55  — 15 sub-options
CMD_SALE_PROGRAMMED = 0x3A  # 58  — sale of PLU item
CMD_PLU = 0x6B  # 107  — item DB management

# ---- Phase 4 — admin / maintenance --------------------------------------
CMD_VAT_PROGRAMMING = 0x53  # 83
CMD_TAX_NUMBER_SET = 0x62  # 98
CMD_TAX_NUMBER_READ = 0x63  # 99
CMD_FISCAL_MEMORY_INFO = 0x7E  # 126
CMD_PARAMETERS = 0xFF  # 255  — read/write parameters (header/footer/VAT/...)
CMD_SERVICE_OPS = 0xFD  # 253
CMD_CUSTOMER_LOGO = 0xCA  # 202  — chunked Base64 logo upload [*32]
CMD_STAMP_IMAGE = 0xCB  # 203  — chunked Base64 stamp upload [*32]
                              #   (PDF title typo: lists 0xCAh, correct 0xCB)

# ---- Reports / diagnostics (anytime) ------------------------------------
CMD_DIAGNOSTIC = 0x5A  # 90
CMD_DEVICE_INFO = 0x7B  # 123
CMD_NRA_MODEM_TEST = 0x47  # 71
CMD_READ_DATETIME = 0x3E  # 62
CMD_SET_DATETIME = 0x3D  # 61

# ---- Storno (refund) ----------------------------------------------------
CMD_OPEN_STORNO = 0x2B  # 43

# ---- Non-fiscal receipt -------------------------------------------------
CMD_OPEN_NONFISCAL = 0x26  # 38
CMD_CLOSE_NONFISCAL = 0x27  # 39
CMD_PRINT_NONFISCAL_TEXT = 0x2A  # 42

# ---- Fiscalization ------------------------------------------------------
CMD_FISCALIZATION = 0x48  # 72  — DANGEROUS, audit-logged

_TITLES: dict[int, str] = {}


def _load() -> None:
    if _TITLES:
        return
    with _CSV_PATH.open("r", encoding="utf-8") as fp:
        for row in csv.DictReader(fp):
            _TITLES[int(row["cmd_dec"])] = row["title"]


def all_commands() -> dict[int, str]:
    """{opcode: title} for all 73 commands from command_map.csv."""
    _load()
    return dict(_TITLES)


def name(cmd: int) -> str:
    """Human-readable command name, or 'UNKNOWN_<hex>' if not in map."""
    _load()
    return _TITLES.get(cmd, f"UNKNOWN_0x{cmd:02X}")
