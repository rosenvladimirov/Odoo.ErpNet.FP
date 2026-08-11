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
#
# Both trailing groups are optional and so is the whitespace before
# them: a Ranger Count 3000 with the Ethernet kit emits
# `"    0.00000    kg      "` — no G/N letter at all, just padding.
# Requiring whitespace after the unit would reject a frame that ends
# on the unit itself.
_LINE_RE = re.compile(
    rb"""
    ^\s*
    (?P<sign>[+\-])?\s*
    (?P<num>\d+(?:\.\d+)?)\s*
    (?P<unit>kg|g|lb:oz|lb|oz|pcs|t)
    \s*
    (?P<unstable>\?)?
    \s*
    (?P<gn>[GN])?
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


# ─── MT-SICS dialect ──────────────────────────────────────────────
# 🚨 Не всеки OHAUS с Ethernet кит говори SCP протокола от мануала.
# Измерено на MEC (`192.168.3.162:9761`, R31P1502 Abacus, 06.08.2026):
#
#     P   → b'ES\r\n'                      ← SCP командата се ОТХВЪРЛЯ
#     SI  → b'S S       2.15 g\r\n'        ← MT-SICS работи
#     S   → b'S S       2.15 g\r\n'
#     I2  → b'I2 A "R31P1502 Abacus 1504.50 g"'
#
# Тоест уредът е на MT-SICS, където `P` е невалидна команда и връща `ES`
# (syntax error). Симптомът в Odoo е `Unparseable: 'ES'` при ВСЯКО четене
# и празен екран в Shop Floor.
#
# Дialektът се РАЗПОЗНАВА, не се конфигурира: първото четене пробва SCP
# (заварено поведение), а при `ES`/`ET`/`EL` минава на MT-SICS и помни
# кое е сработило. Така везните, които говорят SCP, не се пипат.
# 🚨 Коментарите вътре в израза са на английски по НЕОБХОДИМОСТ: това е
# байтов литерал (`rb"""`), а той не приема не-ASCII знаци — кирилица там
# дава `SyntaxError: bytes can only contain ASCII literal characters`.
# Ехото е `S` (и на `SI` уредът отговаря `S`), после статус S/D.
_MTSICS_RE = re.compile(
    rb"""
    ^\s*S\s*I?\s+                 # command echo: `S`
    (?P<st>[SD])\s+               # S = stable, D = dynamic (unstable)
    (?P<sign>[+\-])?\s*
    (?P<num>\d+(?:\.\d+)?)\s*
    (?P<unit>kg|g|lb:oz|lb|oz|pcs|t)
    \s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)

# Отговори на MT-SICS, които НЕ са тегло.
_MTSICS_ERRORS = {
    "ES": "Scale rejected the command (MT-SICS syntax error)",
    "ET": "Transmission error",
    "EL": "Logical error",
}
_MTSICS_NOT_A_WEIGHT = {
    "S I": "Scale busy — no stable value yet",
    "S +": "Overload",
    "S -": "Underload",
}


def _is_mtsics_error(line: bytes) -> bool:
    """Отговорът ли е от вида, който казва „не разбрах командата"?"""
    return line.decode("ascii", "replace").strip().upper() in _MTSICS_ERRORS


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
    DRAIN_TIMEOUT = 0.15

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
        # Кой диалект говори ТОЗИ уред: None = още не знаем, "scp" =
        # протоколът от мануала на кита, "mtsics" = MT-SICS. Пази се на
        # инстанцията, за да не плащаме пробата при всяко четене.
        self._dialect: Optional[str] = None

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
        # Китът буферира отговори и ги доставя в СЛЕДВАЩАТА сесия, ако
        # предишният четец е затворил, преди те да пристигнат. Без това
        # източване `P` получава отговора на предишната команда.
        self._drain()

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
        """Read the weight, in whichever dialect this device speaks.

        Behaviour matches Toledo8217Scale: stable readings return
        ok=True; unstable returns ok=False with status=["Scale unstable"].

        🔑 Два диалекта, разпознати автоматично. Първо се пробва `P`
        (SCP, от мануала на Ethernet кита — заварено поведение). Ако
        уредът отговори `ES`/`ET`/`EL`, значи не разбира командата и е на
        MT-SICS: минаваме на `SI` и помним това за следващите четения.

        Обратното НЕ се прави — щом веднъж сме на MT-SICS, не се връщаме
        към SCP. Иначе всяко четене би плащало по един провален обмен.
        """
        if self._sock is None:
            raise RuntimeError("Scale not open")

        if self._dialect == "mtsics":
            return self._read_mtsics()

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
        if _is_mtsics_error(raw):
            _logger.info(
                "ohaus %s:%s rejected SCP `P` with %r — switching to MT-SICS",
                self.host, self.tcp_port, raw.strip())
            self._dialect = "mtsics"
            # Застоялото от отхвърлената команда не бива да влезе в
            # следващото четене (виж `_drain`).
            self._drain()
            return self._read_mtsics()

        reading = self._parse_line(raw)
        if reading.ok:
            self._dialect = "scp"
        return reading

    def _read_mtsics(self) -> WeightReading:
        """Прочети теглото по MT-SICS: `SI` → `S S <тегло> <единица>`.

        Ползва се `SI` (send immediately), не `S`: `S` чака стабилност и
        при движеща се везна може да не отговори в прозореца, а Shop Floor
        предпочита нестабилна стойност пред празен екран — нестабилността
        се вижда от флага, не от липсата на число.
        """
        try:
            self._sock.sendall(b"SI\r\n")
        except Exception as exc:  # noqa: BLE001
            return WeightReading(
                ok=False, weight_kg=None,
                status=[f"Send failed: {exc}"], raw=b"",
            )
        raw = self._read_first_line(timeout=self.read_timeout)
        return self._parse_mtsics(raw)

    @staticmethod
    def _parse_mtsics(line: bytes) -> WeightReading:
        """MT-SICS отговор → `WeightReading`."""
        if not line.strip():
            return WeightReading(
                ok=False, weight_kg=None,
                status=["No data from scale"], raw=line,
            )
        text = line.decode("ascii", errors="replace").strip()
        upper = text.upper()

        if upper in _MTSICS_ERRORS:
            return WeightReading(
                ok=False, weight_kg=None,
                status=[_MTSICS_ERRORS[upper]], raw=line,
            )
        # `S I` / `S +` / `S -` — уредът отговаря, но не с тегло.
        for prefix, msg in _MTSICS_NOT_A_WEIGHT.items():
            if upper.startswith(prefix) and not upper[len(prefix):].strip():
                return WeightReading(
                    ok=False, weight_kg=None, status=[msg], raw=line,
                )

        m = _MTSICS_RE.match(line)
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
                status=[f"Unparseable: {text!r}"], raw=line,
            )
        if m.group("sign") == b"-":
            num = -num
        kg = _convert_to_kg(num, m.group("unit").decode("ascii", "replace"))
        # `D` = dynamic ⇒ везната още не е стабилна.
        unstable = m.group("st").upper() == b"D"
        return _make_reading(kg, unstable, line)

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
        """Send `PV` and check for any non-empty response.

        Drains afterwards no matter what: `PV` answers with the software
        revision (`SR 1.21`), and a slow reply that arrives after the
        1-second read window would otherwise be delivered into the next
        session — where `read_weight()` reads it instead of the weight
        and fails with `Unparseable: 'SR 1.21'`.
        """
        if self._sock is None:
            return False
        try:
            self._sock.sendall(b"PV\r\n")
            raw = self._read_first_line(timeout=1.0)
            return bool(raw.strip())
        except Exception:  # noqa: BLE001
            return False
        finally:
            self._drain()

    # ─── internals ────────────────────────────────────────────

    @staticmethod
    def _split_endpoint(port) -> tuple[str, int]:
        """Parse `"host"` or `"host:port"`. Default port 9761.

        Coerces to `str` first: a config that carries the port as a
        number must not blow up with `TypeError` deep inside `in`.
        A non-numeric part after `:` falls back to the fixed 9761
        rather than raising — the kit has no other port anyway.
        """
        text = str(port).strip()
        if ":" in text:
            host, _, p = text.rpartition(":")
            try:
                return host, int(p)
            except ValueError:
                return host, DEFAULT_TCP_PORT
        return text, DEFAULT_TCP_PORT

    def _drain(self, timeout: float = DRAIN_TIMEOUT) -> int:
        """Discard bytes left over from an earlier command or session.

        The Ethernet kit buffers its output. When a reader closes before
        a slow reply lands — `probe()` waits 1 s for `PV`, the kit can be
        slower — that reply is delivered into the *next* TCP session. The
        following `read_weight()` then parses `SR 1.21` as a weight frame
        and fails with `Unparseable`, which looks like a flaky cable but
        is a one-command offset in the stream.

        Cheap insurance: a short non-blocking sweep on open, and after
        `probe()`. Returns how many bytes were thrown away so a caller
        can log an unexpectedly noisy device. Never raises — a device we
        could not drain is still worth talking to.
        """
        if self._sock is None:
            return 0
        dropped = 0
        deadline = time.monotonic() + timeout
        try:
            self._sock.settimeout(0.05)
            while time.monotonic() < deadline:
                try:
                    chunk = self._sock.recv(256)
                except socket.timeout:
                    break  # тихо е — нищо не е останало
                except Exception:  # noqa: BLE001
                    break
                if not chunk:
                    break
                dropped += len(chunk)
        finally:
            try:
                self._sock.settimeout(self.read_timeout)
            except Exception:  # noqa: BLE001
                pass
        if dropped:
            _logger.debug(
                "OHAUS %s:%s — изхвърлени %d изостанали байта",
                self.host, self.tcp_port, dropped,
            )
        return dropped

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
        unstable = m.group("unstable") == b"?"
        if unit == "pcs":
            # Броячен режим — отчита БРОЙКИ, не маса. Няма тегло за
            # връщане и `weight_kg` остава None: празна стойност е
            # по-честна от подхлъзващо число в килограми.
            return _make_count_reading(num, unstable, line)
        kg = _convert_to_kg(num, unit)
        return _make_reading(kg, unstable, line)


def _make_reading(kg: float, unstable: bool, raw: bytes) -> WeightReading:
    if unstable:
        return WeightReading(
            ok=False, weight_kg=None,
            status=["Scale unstable"], raw=raw,
        )
    return WeightReading(ok=True, weight_kg=kg, status=[], raw=raw)


def _make_count_reading(num: float, unstable: bool,
                        raw: bytes) -> WeightReading:
    """Четене в броячен режим — бройки, без маса."""
    if unstable:
        return WeightReading(
            ok=False, weight_kg=None, status=["Scale unstable"],
            raw=raw, count=None, mode="count",
        )
    # Бройките са цели по определение; везната ги дава без дробна част,
    # но закръгляме, за да не пропълзи 11.999999 при друг фърмуер.
    return WeightReading(
        ok=True, weight_kg=None, status=[], raw=raw,
        count=int(round(num)), mode="count",
    )
