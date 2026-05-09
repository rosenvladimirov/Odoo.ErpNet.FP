"""
Odoo.ErpNet.FP — Python drop-in replacement for ErpNet.FP HTTP fiscal-printer
server, oriented for the Bulgarian POS market.

Implements the Net.FP HTTP protocol 1:1 (so existing Odoo modules like
`l10n_bg_erp_net_fp` keep working without changes), but the protocol
drivers underneath are pure-Python.

Layout:

    odoo_erpnet_fp/
      ├── drivers/
      │   ├── fiscal/      ← касови апарати (Datecs PM/ISL, Daisy, Tremol, ...)
      │   ├── pinpad/      ← payment terminals (DatecsPay BluePad, ...)
      │   ├── scales/      ← кантари
      │   └── readers/     ← баркод scanners, RFID
      ├── server/          ← FastAPI HTTP server, ErpNet.FP-compatible routes
      └── config/          ← configuration.json loader (ErpNet.FP-compat)
"""

try:
    # Python 3.8+: pull the runtime version from package metadata so
    # __version__ stays in lockstep with pyproject.toml without manual
    # bumps in two places.
    from importlib.metadata import version as _pkg_version
    __version__ = _pkg_version("odoo-erpnet-fp")
except Exception:  # noqa: BLE001
    # Falls back to a known-old value when running from a source tree
    # without the package being installed (e.g. pre-build smoke tests).
    __version__ = "0.0.0+source"
