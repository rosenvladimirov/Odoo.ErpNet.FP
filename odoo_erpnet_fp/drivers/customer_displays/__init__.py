"""
Customer-facing pole displays — VFD / LCD with serial transport.

Drivers:
  datecs.dpd201   — Datecs DPD-201 (and ESC/POS-compatible 2×20 VFD
                    pole displays: ICD CD-5220, Birch DSP-V9 in default
                    mode, Bematech PDX-3000 in DSP800 emulation mode)

Coming later:
  posiflex.pd2x00 — Posiflex PD-2600/2800 native protocol
  escpos.generic  — Universal ESC/POS subset for OEM clones
"""

from .common import CustomerDisplay, DisplayCapabilities
from .datecs_dpd201 import DatecsDpd201

__all__ = [
    "CustomerDisplay",
    "DisplayCapabilities",
    "DatecsDpd201",
]
