"""
Datecs ISL command opcodes.

All `CMD_*` constants are 1-byte values (0x00..0xFF). The frame layer
encodes them as a single byte after `<SEQ>` in the ISL frame.
"""

# ─── Display / paper ─────────────────────────────────────────────
CMD_DIAGNOSTIC = 0x22
CMD_CLEAR_DISPLAY = 0x24
CMD_DISPLAY_TEXT_LINE1 = 0x25
CMD_DISPLAY_TEXT_LINE2 = 0x26
CMD_DISPLAY_DATETIME = 0x28
CMD_CUT_PAPER = 0x29
CMD_OPEN_DRAWER = 0x2A
CMD_PAPER_FEED = 0x2B

# ─── Fiscal receipt lifecycle ────────────────────────────────────
CMD_OPEN_FISCAL_RECEIPT = 0x30  # 48
CMD_FISCAL_RECEIPT_SALE = 0x31  # 49
CMD_FISCAL_RECEIPT_COMMENT = 0x36  # 54
CMD_FISCAL_RECEIPT_TOTAL = 0x35  # 53
CMD_CLOSE_FISCAL_RECEIPT = 0x38  # 56
CMD_ABORT_FISCAL_RECEIPT = 0x3C  # 60
CMD_SUBTOTAL = 0x33  # 51

# ─── Date / time ─────────────────────────────────────────────────
CMD_SET_DATE_TIME = 0x3D
CMD_GET_DATE_TIME = 0x3E

# ─── Status / info ───────────────────────────────────────────────
CMD_GET_STATUS = 0x4A  # 74
CMD_GET_DEVICE_INFO = 0x5A  # 90
CMD_GET_TAX_ID_NUMBER = 0x63  # 99
CMD_GET_RECEIPT_STATUS = 0x4C  # 76
CMD_GET_LAST_DOCUMENT_NUMBER = 0x71  # 113

# ─── Programming ─────────────────────────────────────────────────
CMD_PROGRAM_PAYMENT = 0x44
CMD_PROGRAM_PARAMETERS = 0x45
CMD_PROGRAM_DEPARTMENT = 0x47
CMD_PROGRAM_OPERATOR = 0x4A  # alias of CMD_GET_STATUS in some firmware
CMD_PROGRAM_PLU = 0x4B
CMD_PROGRAM_LOGO = 0x4C  # alias of CMD_GET_RECEIPT_STATUS
CMD_MONEY_TRANSFER = 0x46  # 70 — служебно внасяне/изплащане

# ─── Reads ───────────────────────────────────────────────────────
CMD_READ_SERIAL_NUMBERS = 0x60
CMD_READ_VAT_RATES = 0x62
CMD_READ_PAYMENTS = 0x64
CMD_READ_PARAMETERS = 0x65
CMD_READ_DEPARTMENT = 0x67
CMD_READ_OPERATOR = 0x6A
CMD_READ_PLU = 0x6B

# ─── Reports ─────────────────────────────────────────────────────
CMD_PRINT_DAILY_REPORT = 0x45  # 69 — X / Z (param "2" = X-no-zero)
CMD_PRINT_DEPARTMENT_REPORT = 0x76
CMD_PRINT_OPERATOR_REPORT = 0x79
CMD_PRINT_PLU_REPORT = 0x77
CMD_PRINT_FM_REPORT_BY_DATE = 0x78
CMD_PRINT_FM_REPORT_BY_NUMBER = 0x79
CMD_PRINT_LAST_RECEIPT_DUPLICATE = 0x6D  # 109

# ─── Electronic Journal (ЕД / EJ) ────────────────────────────────
CMD_READ_EJ_BY_DATE = 0x7C
CMD_READ_EJ_BY_NUMBER = 0x7D
CMD_READ_LAST_RECEIPT_QR_DATA = 0x74

# ─── Misc / pinpad ───────────────────────────────────────────────
CMD_GET_DEVICE_CONSTANTS = 0x80
CMD_TO_PINPAD = 0x37  # Datecs X pinpad pass-through
CMD_BEEP = 0x50
