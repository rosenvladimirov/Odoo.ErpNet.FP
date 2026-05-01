"""
HTTP+JWT transport (Topology C — cloud Odoo + remote IoT agent).

The IoT agent is a separate Python service installed on a shop machine
where the device is connected by serial/USB. It exposes a thin JSON-RPC
endpoint that this transport calls; the agent in turn drives the device
through `transport_serial`. Auth is a per-agent JWT.

NOTE: this is a stub for Phase 6. The agent itself
(`l10n_bg_fp_iot_agent`) is a separate PyPI package (not an Odoo addon).
"""

from __future__ import annotations

from .transport import Transport, TransportError


class AgentTransport(Transport):
    """HTTP+JWT transport. Phase 6 — not yet implemented."""

    def __init__(self, base_url: str, jwt: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.jwt = jwt
        self.timeout = timeout
        self._open = False

    def open(self) -> None:
        # Health-check the agent endpoint — not implemented yet.
        raise NotImplementedError(
            "AgentTransport is Phase 6 — IoT agent package "
            "`l10n_bg_fp_iot_agent` is not yet available."
        )

    def close(self) -> None:
        self._open = False

    def is_open(self) -> bool:
        return self._open

    def write(self, data: bytes) -> None:
        raise NotImplementedError

    def read(self, n: int, timeout: float) -> bytes:
        raise NotImplementedError

    def read_until(self, terminator: bytes, max_bytes: int, timeout: float) -> bytes:
        raise NotImplementedError
