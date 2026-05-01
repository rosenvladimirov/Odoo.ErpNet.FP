"""
Adapters between vendor-specific driver APIs and the ErpNet.FP wire
protocol. Each module is a one-way translator (or pair) deliberately
kept isolated — adding a new fiscal vendor means adding its mapping
here without touching either drivers/ or routes/.
"""

from . import messages, payment_type, tax_group

__all__ = ["messages", "payment_type", "tax_group"]
