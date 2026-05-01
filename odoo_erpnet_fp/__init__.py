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

__version__ = "0.1.0"
