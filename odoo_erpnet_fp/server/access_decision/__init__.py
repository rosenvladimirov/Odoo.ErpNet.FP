"""Offline access decision package — receives ZEN graph from Odoo via
heartbeat, evaluates locally при network loss, drains trace на reconnect.

Mirror на Odoo-side `access_control.access.context.builder` —
`_derive_direction()` ТРЯБВА да остане байт-идентичен с Odoo версията.
Version се pin-ва към ZEN graph version през heartbeat payload.
"""
from .context_builder import derive_direction, build_context, BUILDER_VERSION
from .runner import ZenLocalRunner, GraphStore

__all__ = [
    "derive_direction",
    "build_context",
    "BUILDER_VERSION",
    "ZenLocalRunner",
    "GraphStore",
]
