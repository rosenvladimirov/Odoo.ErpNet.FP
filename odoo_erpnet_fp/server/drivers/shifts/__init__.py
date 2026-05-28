"""Shift-sync bridge driver — long-lived TCP client to Android.

Closes the loop в BlueCash PLU + Odoo архитектурата:

    Android (ShiftBridgeService на TCP :9103)
              ▲
              │  NDJSON (line-delimited JSON-RPC w/ correlation IDs)
              │
    Proxy (drivers/shifts/tcp_shift_bridge.py)
              ▲
              │  HTTP (HMAC-signed)
              │
    Odoo  (l10n.bg.erp.net.fp.shift.bridge.client)

Pattern mirrored от `pinpad` registry + `bluecash55_bridges.sh` socat
lifecycle: единственa connection per device, mutex-serialised calls,
auto-reconnect с exponential backoff, async notification stream
(`shift.closed` push от Android когато Z завърши).
"""

from .tcp_shift_bridge import (
    ShiftBridge,
    ShiftBridgeError,
    ShiftBridgeRegistry,
    get_shift_registry,
    set_shift_registry,
)

__all__ = [
    "ShiftBridge",
    "ShiftBridgeError",
    "ShiftBridgeRegistry",
    "get_shift_registry",
    "set_shift_registry",
]
