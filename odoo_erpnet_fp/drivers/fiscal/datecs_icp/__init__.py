"""
Datecs ICP Fiscal Printer Driver — pure-Python port of Odoo IoT box.

Covers all current ICP Datecs models:
  Datecs P/C    — DP-25, DP-05, WP-50, DP-35
  Datecs X      — FP-700X, WP-500X, **DP-150X**, FMP-350X, FMP-55X
  Datecs FP     — FP-800, FP-2000, FP-650
  Datecs FMP v2 — FMP-350X / FP-700X v2 (Programmer's Manual v2.02)

ICP is the older Datecs protocol — distinct from PM v2.11.4 used on
the new MX series (FP-700 MX). Both protocols share the same physical
layer (RS232/TCP) and similar frame envelope (PRE/PST/BCC) but use
different command opcodes, DATA encoding (text-CSV vs TAB-separated),
and tax-group letters.

High-level usage:

    from odoo_erpnet_fp.drivers.fiscal.datecs_icp import IcpDevice
    from odoo_erpnet_fp.drivers.fiscal.datecs_icp.transport_serial import SerialTransport

    transport = SerialTransport(port="/dev/ttyUSB0", baudrate=115200)
    icp = IcpDevice(transport)
    icp.open()
    info = icp.detect()  # auto-probes model, sets icp.model
    icp.print_x_report()
    icp.close()
"""

from .protocol import IcpDevice, ReversalReason
from .status import DeviceStatus, StatusMessage, StatusMessageType
from .vendors import (
    DaisyIcpDevice,
    DatecsIcpDevice,
    DatecsIcpXDevice,
    EltradeIcpDevice,
    IncotexIcpDevice,
)

__all__ = [
    "IcpDevice",
    "DatecsIcpDevice",
    "DatecsIcpXDevice",
    "DaisyIcpDevice",
    "EltradeIcpDevice",
    "IncotexIcpDevice",
    "DeviceStatus",
    "StatusMessage",
    "StatusMessageType",
    "ReversalReason",
]
