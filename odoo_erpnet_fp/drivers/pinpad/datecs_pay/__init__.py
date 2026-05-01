"""
DatecsPay pinpad driver — pure-Python wrapper over the native C
library (`libdatecs_pinpad.so`) for BluePad-50, BlueCash-50 and
similar Datecs payment terminals.

The native library is bundled at `lib/libdatecs_pinpad.so`. Library
loading is deferred to first use — importing this package does not
require the .so to be present (so CI runs without it).

High-level usage:

    from odoo_erpnet_fp.drivers.pinpad.datecs_pay import DatecsPayPinpad

    pp = DatecsPayPinpad(port='/dev/ttyUSB0', baudrate=115200)
    pp.open()
    info = pp.get_info()         # model, serial, terminal_id
    status = pp.get_status()     # has_reversal / has_hang_transaction
    result = pp.purchase(2050)   # 20.50 BGN — returns auth code, RRN, ...
    pp.close()
"""

from ._native import (
    DatecsPinpadDriver,
    PinpadInfo,
    PinpadStatus,
    TRANS_PURCHASE,
    TRANS_VOID_PURCHASE,
    TRANS_END_OF_DAY,
    TRANS_TEST_CONNECTION,
    TAG_AMOUNT,
    TAG_RRN,
    TAG_AUTH_ID,
    TAG_HOST_RRN,
    TAG_HOST_AUTH_ID,
    TAG_TRANS_RESULT,
    TAG_TRANS_ERROR,
    TAG_TERMINAL_ID,
    create_purchase_params,
    create_void_params,
)
from .facade import DatecsPayPinpad, TransactionResult

__all__ = [
    "DatecsPayPinpad",
    "TransactionResult",
    "DatecsPinpadDriver",
    "PinpadInfo",
    "PinpadStatus",
    "TRANS_PURCHASE",
    "TRANS_VOID_PURCHASE",
    "TRANS_END_OF_DAY",
    "TRANS_TEST_CONNECTION",
    "TAG_AMOUNT",
    "TAG_RRN",
    "TAG_AUTH_ID",
    "TAG_HOST_RRN",
    "TAG_HOST_AUTH_ID",
    "TAG_TRANS_RESULT",
    "TAG_TRANS_ERROR",
    "TAG_TERMINAL_ID",
    "create_purchase_params",
    "create_void_params",
]
