"""
OHAUS Ranger 3000 / Ranger Count 3000 / Valor 7000 — Ethernet kit driver.

Hardware: OHAUS Ethernet Interface Kit P/N 30037447. The kit is a
TCP-server module fitted to the underside of the scale; one TCP client
at a time. Port is hard-coded to **9761** by the kit's firmware and
cannot be changed. IP comes from DHCP (default) or is set in the
scale's `E.t.h.r.n.t / Ip Adr` menu.

Protocol (from OHAUS Ethernet Interface Instruction Manual P/N 30064478B):

    Output frame (after `P` / `IP` / `CP` / Auto-Print):
        [weight 9ch][space][unit 5ch][space][?|space][G|N][LF]
        e.g.  "    1.234 kg     N\\n"   → 1.234 kg, stable, gross
              "  -12.345 g    ?N\\n"    → -12.345 g, unstable, gross

    Input commands (host → scale):
        IP        — immediate print (stable or unstable)
        P         — print displayed weight
        CP        — continuous print
        SP        — print on next stability
        0S / 1S   — toggle "stable only" filter
        xP        — interval print, x = 1..3600 sec, 0P turns it OFF
        Z         — same as Zero key
        T         — same as Tare key
        xT        — set tare in grams (positive only); 0T clears tare
        PU        — print current unit
        xU        — set unit  (1=g 2=kg 3=lb 4=oz 5=lb:oz 6=t)
        PV        — print version (name + sw rev)
        \\EscR     — global factory reset

The driver opens the socket per request (matches Toledo / CAS pattern);
`read_weight()` sends `P` and parses the next non-empty response line.
"""

from __future__ import annotations

import logging
import re
import socket
import time
from typing import Optional

from .toledo_8217 import WeightReading

_logger = logging.getLogger(__name__)

DEFAULT_TCP_PORT = 9761

# OHAUS line: optional weight (digits + optional decimal), space, unit
# (g/kg/lb/oz/t/lb:oz), spaces, optional '?' (unstable), 'G' or 'N'.
# Real frames use space-padding inside the 9-char weight field, so we
# allow leading whitespace before the sign too.
_LINE_RE = re.compile(
    rb"""
    ^\s*
    (?P<sign>[+\-])?\s*
    (?P<num>\d+(?:\.\d+)?)\s*
    (?P<unit>kg|g|lb:oz|lb|oz|t)
    \s+
    (?P<unstable>\?)?
    \s*
    (?P<gn>[GN])?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


def _convert_to_kg(num: float, unit: str) -> float:
    u = unit.lower()
    if u in ("kg", "t"):
        return num * 1000.0 if u == "t" else num
    if u == "g":
        return num / 1000.0
    if u == "lb":
        return num * 0.45359237
    if u == "oz":
        return num * 0.028349523125
    # lb:oz handled separately by caller
    return num


class OhausRangerScale:
    """OHAUS Ranger 3000 / Count 3000 / Valor 7000 over TCP/IP.

    Aliases registered in ScaleRegistry: ``ohaus_ranger``, ``ohaus``,
    ``ranger3000``, ``valor7000``.

    Construction matches the existing scale-driver contract used by
    `ScaleRegistry.make_scale()`:

        OhausRangerScale(port="192.168.3.162", baudrate=9600)
        OhausRangerScale(port="192.168.3.162:9761")

    `port` is the TCP endpoint — either a bare hostname/IP (port
    defaults to 9761) or `"host:port"`. `baudrate` is accepted for
    interface compatibility but ignored (the kit is Ethernet-only).
    """

    DEFAULT_PORT = DEFAULT_TCP_PORT
    READ_TIMEOUT = 2.0
    CONNECT_TIMEOUT = 1.5

    def __init__(
        self,
        port: str,
        baudrate: int = 9600,  # accepted for factory compat; ignored
        connect_timeout: float = CONNECT_TIMEOUT,
        read_timeout: float = READ_TIMEOUT,
    ) -> None:
        if not port:
            raise ValueError("OhausRangerScale: 'port' must be host or host:port")
        self.host, self.tcp_port = self._split_endpoint(port)
        self.baudrate = baudrate  # unused; preserved for symmetry
        self.connect_timeout = connect_timeout
        self.read_timeout = read_timeout
        self._sock: Optional[socket.socket] = None

    # ─── lifecycle ─────────────────────────────────────────────

    def open(self) -> None:
        if self._sock is not None:
            return
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(self.connect_timeout)
        try:
            s.connect((self.host, self.tcp_port))
        except Exception:
            s.close()
            raise
        s.settimeout(self.read_timeout)
        self._sock = s

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            finally:
                self._sock = None

    def __enter__(self) -> "OhausRangerScale":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @property
    def is_open(self) -> bool:
        return self._sock is not None

    # ─── public API ────────────────────────────────────────────

    def read_weight(self) -> WeightReading:
        """Send `P`, return first complete frame as a `WeightReading`.

        Behaviour matches Toledo8217Scale: stable readings return
        ok=True; unstable returns ok=False with status=["Scale unstable"].
        """
        if self._sock is None:
            raise RuntimeError("Scale not open")

        # `P` triggers a single print. We collect bytes for up to
        # `read_timeout` seconds, take the first non-empty line.
        try:
            self._sock.sendall(b"P\r\n")
        except Exception as exc:  # noqa: BLE001
            return WeightReading(
                ok=False, weight_kg=None,
                status=[f"Send failed: {exc}"], raw=b"",
            )

        raw = self._read_first_line(timeout=self.read_timeout)
        return self._parse_line(raw)

    def zero(self) -> None:
        """Same as pressing the Zero key on the scale."""
        if self._sock is None:
            raise RuntimeError("Scale not open")
        self._sock.sendall(b"Z\r\n")

    def tare(self, grams: Optional[float] = None) -> None:
        """Tare. With no argument = press Tare key.
        With `grams` = download a pre-set tare (positive grams only).
        Pass 0 to clear an existing tare.
        """
        if self._sock is None:
            raise RuntimeError("Scale not open")
        if grams is None:
            self._sock.sendall(b"T\r\n")
        else:
            if grams < 0:
                raise ValueError("Tare grams must be >= 0 (OHAUS limitation)")
            cmd = f"{int(grams)}T\r\n".encode("ascii")
            self._sock.sendall(cmd)

    def probe(self) -> bool:
        """Send `PV` and check for any non-empty response."""
        if self._sock is None:
            return False
        try:
            self._sock.sendall(b"PV\r\n")
            raw = self._read_first_line(timeout=1.0)
            return bool(raw.strip())
        except Exception:  # noqa: BLE001
            return False

    # ─── internals ────────────────────────────────────────────

    @staticmethod
    def _split_endpoint(port: str) -> tuple[str, int]:
        """Parse `"host"` or `"host:port"`. Default port 9761."""
        if ":" in port:
            host, _, p = port.rpartition(":")
            return host, int(p)
        return port, DEFAULT_TCP_PORT

    def _read_first_line(self, timeout: float) -> bytes:
        """Read until \\n or timeout. Returns the line (without trailing
        \\r\\n) or b"" on timeout/empty stream."""
        assert self._sock is not None
        deadline = time.monotonic() + timeout
        buf = bytearray()
        self._sock.settimeout(0.2)
        while time.monotonic() < deadline:
            try:
                chunk = self._sock.recv(128)
            except socket.timeout:
                if buf:
                    break
                continue
            except Exception:  # noqa: BLE001
                break
            if not chunk:
                break
            buf.extend(chunk)
            if b"\n" in buf:
                break
        # Restore the per-call read timeout
        self._sock.settimeout(self.read_timeout)
        # Take only the first line; ignore trailing junk
        nl = buf.find(b"\n")
        if nl >= 0:
            return bytes(buf[:nl]).rstrip(b"\r")
        return bytes(buf).rstrip(b"\r")

    @staticmethod
    def _parse_line(line: bytes) -> WeightReading:
        if not line.strip():
            return WeightReading(
                ok=False, weight_kg=None,
                status=["No data from scale"], raw=line,
            )
        text = line.decode("ascii", errors="replace")

        # Handle lb:oz separately — format is "X lb:Y.Z oz" or
        # "  3 lb:5.6 oz" depending on display layout.
        loz = re.match(
            r"^\s*(?P<lb>\d+)\s*lb\s*[:]\s*(?P<oz>\d+(?:\.\d+)?)\s*oz\s*"
            r"(?P<unstable>\?)?\s*(?P<gn>[GN])?\s*$",
            text, re.IGNORECASE,
        )
        if loz:
            kg = (
                int(loz.group("lb")) * 0.45359237
                + float(loz.group("oz")) * 0.028349523125
            )
            unstable = loz.group("unstable") == "?"
            return _make_reading(kg, unstable, line)

        m = _LINE_RE.match(line)
        if m is None:
            return WeightReading(
                ok=False, weight_kg=None,
                status=[f"Unparseable: {text!r}"], raw=line,
            )
        try:
            num = float(m.group("num"))
        except (TypeError, ValueError):
            return WeightReading(
                ok=False, weight_kg=None,
                status=[f"Bad number: {m.group('num')!r}"], raw=line,
            )
        if m.group("sign") == b"-":
            num = -num
        unit = m.group("unit").decode("ascii").lower()
        kg = _convert_to_kg(num, unit)
        unstable = m.group("unstable") == b"?"
        return _make_reading(kg, unstable, line)


def _make_reading(kg: float, unstable: bool, raw: bytes) -> WeightReading:
    if unstable:
        return WeightReading(
            ok=False, weight_kg=None,
            status=["Scale unstable"], raw=raw,
        )
    return WeightReading(ok=True, weight_kg=kg, status=[], raw=raw)
