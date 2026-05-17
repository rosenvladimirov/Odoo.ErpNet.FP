"""
Access-control actuators (Phase B) — barrier / relay / turnstile.

Command-style output devices (same shape as customer displays). The
access DECISION stays in Odoo (fail-secure); these only EXECUTE an
authorised command, synchronously, with zero queue latency.

Drivers:
  relay_tcp — generic TCP relay board (KMtronic/Numato/USR/ESP)
  onvif     — camera's own ONVIF Device IO relay (reuses Phase A)
  gpio      — Raspberry Pi / SBC GPIO pin (lazy [gpio] extra)
  polimex   — Polimex iCON (BG) via the open WebSDK direct command
  hikvision — Hik DS-K2/K1T/KD via ISAPI RemoteControlDoor (Digest)
  dahua     — Dahua ASC/ASI/VTO via accessControl.cgi (Digest)
  wiegand   — SCAFFOLD (needs MCU bit-banger)
  miv       — MIV Electronics vendor slot (protocol pending)
"""

from .common import AccessActuator, AccessResult
from .dahua import DahuaCgiActuator
from .gpio import GpioActuator
from .hikvision import HikvisionIsapiActuator
from .miv import MivActuator
from .onvif_relay import OnvifRelayActuator
from .polimex import PolimexWebSdkActuator
from .relay_tcp import RelayTcpActuator
from .wiegand import WiegandActuator

__all__ = [
    "AccessActuator",
    "AccessResult",
    "RelayTcpActuator",
    "OnvifRelayActuator",
    "GpioActuator",
    "PolimexWebSdkActuator",
    "HikvisionIsapiActuator",
    "DahuaCgiActuator",
    "WiegandActuator",
    "MivActuator",
]
