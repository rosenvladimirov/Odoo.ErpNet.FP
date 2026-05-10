"""
Datecs ISL Fiscal Printer Driver — pure-Python port of Odoo IoT box.

Covers all current ISL Datecs models:
  Datecs P/C    — DP-25, DP-05, WP-50, DP-35
  Datecs X      — FP-700X, WP-500X, **DP-150X**, FMP-350X, FMP-55X
  Datecs FP     — FP-800, FP-2000, FP-650
  Datecs FMP v2 — FMP-350X / FP-700X v2 (Programmer's Manual v2.02)

ISL is the older Datecs protocol — distinct from PM v2.11.4 used on
the new MX series (FP-700 MX). Both protocols share the same physical
layer (RS232/TCP) and similar frame envelope (PRE/PST/BCC) but use
different command opcodes, DATA encoding (text-CSV vs TAB-separated),
and tax-group letters.

High-level usage:

    from odoo_erpnet_fp.drivers.fiscal.datecs_isl import IslDevice
    from odoo_erpnet_fp.drivers.fiscal.datecs_isl.transport_serial import SerialTransport

    transport = SerialTransport(port="/dev/ttyUSB0", baudrate=115200)
    isl = IslDevice(transport)
    isl.open()
    info = isl.detect()  # auto-probes model, sets isl.model
    isl.print_x_report()
    isl.close()
"""

from .protocol import IslDevice, ReversalReason
from .status import DeviceStatus, StatusMessage, StatusMessageType
from .vendors import (
    DaisyIslDevice,
    DatecsIslDevice,
    DatecsIslXDevice,
    EltradeIslDevice,
    IncotexIslDevice,
    TremolIslDevice,
)

__all__ = [
    "IslDevice",
    "DatecsIslDevice",
    "DatecsIslXDevice",
    "DaisyIslDevice",
    "EltradeIslDevice",
    "IncotexIslDevice",
    "TremolIslDevice",
    "DeviceStatus",
    "StatusMessage",
    "StatusMessageType",
    "ReversalReason",
]
