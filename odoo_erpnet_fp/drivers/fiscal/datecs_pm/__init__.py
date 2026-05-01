"""
Datecs PM Communication Protocol v2.11.4 — pure-Python driver.

Source of truth for the wire protocol:
  data/command_map.csv     — opcode table (73 commands)
  data/error_codes.csv     — 457 error codes with names + categories
  data/status_bits.csv     — 8-byte status decode table

High-level usage:

    from odoo_erpnet_fp.drivers.fiscal.datecs_pm import PmDevice
    from odoo_erpnet_fp.drivers.fiscal.datecs_pm.transport_serial import SerialTransport

    transport = SerialTransport(port="/dev/ttyUSB0", baudrate=115200)
    pm = PmDevice(transport, op_code=1, op_password="1", till_number=24)
    pm.open()
    slip = pm.open_fiscal_receipt(nsale="DT636533-0020-0010110")
    pm.register_sale("Хляб", price=1.50, vat_group="А")
    pm.payment_total(payment_type=0, amount=1.50)
    pm.close_fiscal_receipt()
    pm.close()

Compatible devices: Datecs FP-700 MX and other PM-protocol fiscal devices
(new MX series; the older ISL series — DP-25, FP-700X, FP-2000, etc. — is
covered by `drivers/fiscal/datecs_isl/` once that port lands).
"""

from . import codec, commands, errors, frame, status
from .pm_v2_11_4 import PmDevice

__all__ = [
    "PmDevice",
    "codec",
    "commands",
    "errors",
    "frame",
    "status",
]
