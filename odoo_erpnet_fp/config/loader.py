"""
Configuration loader.

Two formats supported on disk, resolved at startup:

1. **ErpNet.FP-compatible** `configuration.json` — same shape as upstream
   so a shop migrating from the C# server only needs to copy its config:

       {
         "AutoDetect": true,
         "Printers": {
           "fp1": {
             "Uri": "bg.dt.pm.com://COM5",
             "BaudRate": 115200
           }
         }
       }

2. **Native** `config.yaml` — adds HTTPS / TLS settings that ErpNet.FP
   doesn't expose (it relies on Traefik reverse-proxy for TLS):

       server:
         host: "0.0.0.0"
         port: 8001
         tls:
           enabled: true
           certfile: /etc/certs/origin.pem    # e.g. Cloudflare Origin Cert
           keyfile:  /etc/certs/origin.key
           # Optional CA bundle (for client-cert auth, mTLS):
           ca_certs: null
           require_client_cert: false
       printers:
         - id: fp1
           driver: datecs.pm
           transport: serial
           port: /dev/ttyUSB0
           baudrate: 115200

YAML is the recommended format for new deployments because TLS, log-level
and per-printer driver hints are easier to express. The `configuration.json`
loader is provided for compatibility only.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml


@dataclass
class TlsConfig:
    """uvicorn-compatible TLS config.

    `certfile` and `keyfile` accept any PEM-encoded cert / key — including
    Cloudflare Origin Certificates (downloaded from the dashboard), Let's
    Encrypt certs, or self-issued ones from Step-CA. The server doesn't
    care about provenance, only that the files are PEM and readable.
    """

    enabled: bool = False
    certfile: Optional[str] = None
    keyfile: Optional[str] = None
    keyfile_password: Optional[str] = None
    ca_certs: Optional[str] = None
    require_client_cert: bool = False

    def validate(self) -> None:
        if not self.enabled:
            return
        for label, path in (("certfile", self.certfile), ("keyfile", self.keyfile)):
            if not path:
                raise ValueError(
                    f"TLS enabled but {label} is not set"
                )
            if not Path(path).is_file():
                raise FileNotFoundError(
                    f"TLS {label} not found at: {path}"
                )
        if self.ca_certs and not Path(self.ca_certs).is_file():
            raise FileNotFoundError(
                f"TLS ca_certs not found at: {self.ca_certs}"
            )


@dataclass
class ServerConfig:
    host: str = "0.0.0.0"
    port: int = 8001
    log_level: str = "info"
    tls: TlsConfig = field(default_factory=TlsConfig)


@dataclass
class PrinterConfig:
    """Single printer entry.

    `driver` is a dotted path into the drivers package, e.g.
    "datecs.pm" → odoo_erpnet_fp.drivers.fiscal.datecs_pm.
    `transport` is one of `serial` / `tcp` / `agent` and selects the
    corresponding Transport implementation in that driver subpackage.
    """

    id: str
    driver: str = "datecs.pm"
    transport: str = "serial"
    port: Optional[str] = None  # /dev/ttyUSB0, COM5, ...
    baudrate: int = 115200
    tcp_host: Optional[str] = None
    tcp_port: Optional[int] = None
    operator: str = "1"
    operator_password: str = "0000"
    till_number: int = 1
    nsale_prefix: Optional[str] = None
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class PinpadConfig:
    """POS payment terminal entry.

    `driver` selects the implementation:
        datecs_pay  — DatecsPay BluePad-50 / BlueCash-50 (C lib via ctypes)
    """

    id: str
    driver: str = "datecs_pay"
    port: Optional[str] = None
    baudrate: int = 115200
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScaleConfig:
    """Weighing scale entry. Filled in Phase 5."""

    id: str
    driver: str = "adam"
    port: Optional[str] = None
    baudrate: int = 9600
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class DisplayConfig:
    """Customer-facing pole display entry.

    `driver` selects the implementation:
        datecs.dpd201    — Datecs DPD-201 + ESC/POS-compatible clones
                           (ICD CD-5220, Birch DSP-V9, Bematech PDX-3000)

    `encoding` controls how text is encoded before sending — pick the
    codec that matches the device's selected character code table:
        cp437     — USA / standard Europe (Latin)
        cp850     — multilingual Latin-1 (Western European)
        cp1251    — Cyrillic; only works on DPD-201 in "DATECS ECR" jumper mode
    """

    id: str
    driver: str = "datecs.dpd201"
    port: Optional[str] = None
    baudrate: int = 9600
    encoding: str = "cp437"
    chars_per_line: int = 20
    lines: int = 2
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class ReaderConfig:
    """Barcode reader entry — push-model (no polling).

    `transport` is one of:
      hid     — USB HID keyboard-emulator; `device_path` = /dev/input/eventN
      serial  — RS232 / USB-CDC line-based; `port` = /dev/ttyUSB?

    `webhooks` is a list of HTTPS URLs that receive a POST with
    `{readerId, barcode, timestamp}` for every scan — typically an Odoo
    `/web/dataset/...` endpoint that records the read.
    """

    id: str
    transport: str = "hid"
    device_path: Optional[str] = None  # /dev/input/eventN — for hid
    port: Optional[str] = None         # /dev/ttyUSB? — for serial
    baudrate: int = 9600                # for serial
    grab: bool = True                   # for hid: exclusive device access
    encoding: str = "ascii"             # for serial: line decode
    # ─── HID auto-discovery (alternative to device_path) ──
    vid: Optional[int] = None           # USB vendor id (decimal or hex literal)
    pid: Optional[int] = None           # USB product id
    name_regex: Optional[str] = None    # regex on device name
    # ─── HID framing ────────────────────────────────────
    terminator: str = "enter"           # enter | tab | lf | comma-separated scancodes
    strip_prefix: str = ""
    strip_suffix: str = ""
    max_length: int = 4096
    caps_lock_strategy: str = "ignore"  # ignore | respect
    webhooks: list[str] = field(default_factory=list)
    extras: dict[str, Any] = field(default_factory=dict)


@dataclass
class AppConfig:
    server: ServerConfig = field(default_factory=ServerConfig)
    printers: list[PrinterConfig] = field(default_factory=list)
    pinpads: list[PinpadConfig] = field(default_factory=list)
    scales: list[ScaleConfig] = field(default_factory=list)
    readers: list[ReaderConfig] = field(default_factory=list)
    displays: list[DisplayConfig] = field(default_factory=list)
    auto_detect: bool = False


# ─── YAML loader (preferred) ─────────────────────────────────────────


def _to_int(v: Any) -> Optional[int]:
    """Accept int, decimal string, or 0xNNNN hex string (handy for VID/PID)."""
    if v is None or v == "":
        return None
    if isinstance(v, int):
        return v
    s = str(v).strip()
    try:
        return int(s, 0)  # base 0 → auto-detect 0x.. / 0o.. / 0b.. / decimal
    except ValueError:
        return None


def _yaml_to_app_config(data: dict) -> AppConfig:
    server_data = data.get("server", {})
    tls_data = server_data.get("tls", {})
    server = ServerConfig(
        host=server_data.get("host", "0.0.0.0"),
        port=int(server_data.get("port", 8001)),
        log_level=server_data.get("log_level", "info"),
        tls=TlsConfig(
            enabled=bool(tls_data.get("enabled", False)),
            certfile=tls_data.get("certfile"),
            keyfile=tls_data.get("keyfile"),
            keyfile_password=tls_data.get("keyfile_password"),
            ca_certs=tls_data.get("ca_certs"),
            require_client_cert=bool(
                tls_data.get("require_client_cert", False)
            ),
        ),
    )
    server.tls.validate()

    printers: list[PrinterConfig] = []
    for entry in data.get("printers", []) or []:
        printers.append(
            PrinterConfig(
                id=str(entry["id"]),
                driver=entry.get("driver", "datecs.pm"),
                transport=entry.get("transport", "serial"),
                port=entry.get("port"),
                baudrate=int(entry.get("baudrate", 115200)),
                tcp_host=entry.get("tcp_host"),
                tcp_port=entry.get("tcp_port") and int(entry["tcp_port"]),
                operator=str(entry.get("operator", "1")),
                # "1" matches C-variant DP-150 factory default; X-variant
                # DP-150X / FP-700X devices need "0000" — set explicitly
                # in config.yaml when needed.
                operator_password=str(entry.get("operator_password", "1")),
                till_number=int(entry.get("till_number", 1)),
                nsale_prefix=entry.get("nsale_prefix"),
                extras=entry.get("extras", {}),
            )
        )

    pinpads: list[PinpadConfig] = []
    for entry in data.get("pinpads", []) or []:
        pinpads.append(
            PinpadConfig(
                id=str(entry["id"]),
                driver=entry.get("driver", "datecs_pay"),
                port=entry.get("port"),
                baudrate=int(entry.get("baudrate", 115200)),
                extras=entry.get("extras", {}),
            )
        )

    scales: list[ScaleConfig] = []
    for entry in data.get("scales", []) or []:
        scales.append(
            ScaleConfig(
                id=str(entry["id"]),
                driver=entry.get("driver", "adam"),
                port=entry.get("port"),
                baudrate=int(entry.get("baudrate", 9600)),
                extras=entry.get("extras", {}),
            )
        )

    readers: list[ReaderConfig] = []
    for entry in data.get("readers", []) or []:
        # Accept VID/PID nested under `match:` (matches the
        # documented hid2serial schema) or flat top-level fields.
        match = entry.get("match") or {}
        readers.append(
            ReaderConfig(
                id=str(entry["id"]),
                transport=entry.get("transport", "hid"),
                device_path=entry.get("device_path"),
                port=entry.get("port"),
                baudrate=int(entry.get("baudrate", 9600)),
                grab=bool(entry.get("grab", True)),
                encoding=entry.get("encoding", "ascii"),
                vid=_to_int(entry.get("vid", match.get("vid"))),
                pid=_to_int(entry.get("pid", match.get("pid"))),
                name_regex=entry.get("name_regex", match.get("name_regex")),
                terminator=entry.get(
                    "terminator", (entry.get("framing") or {}).get("terminator", "enter")
                ),
                strip_prefix=entry.get(
                    "strip_prefix", (entry.get("framing") or {}).get("strip_prefix", "")
                ),
                strip_suffix=entry.get(
                    "strip_suffix", (entry.get("framing") or {}).get("strip_suffix", "")
                ),
                max_length=int(entry.get(
                    "max_length", (entry.get("framing") or {}).get("max_length", 4096)
                )),
                caps_lock_strategy=entry.get(
                    "caps_lock_strategy",
                    (entry.get("keymap") or {}).get("caps_lock_strategy", "ignore"),
                ),
                webhooks=list(entry.get("webhooks", []) or []),
                extras=entry.get("extras", {}),
            )
        )

    displays: list[DisplayConfig] = []
    for entry in data.get("displays", []) or []:
        displays.append(
            DisplayConfig(
                id=str(entry["id"]),
                driver=entry.get("driver", "datecs.dpd201"),
                port=entry.get("port"),
                baudrate=int(entry.get("baudrate", 9600)),
                encoding=entry.get("encoding", "cp437"),
                chars_per_line=int(entry.get("chars_per_line", 20)),
                lines=int(entry.get("lines", 2)),
                extras=entry.get("extras", {}),
            )
        )

    return AppConfig(
        server=server,
        printers=printers,
        pinpads=pinpads,
        scales=scales,
        readers=readers,
        displays=displays,
        auto_detect=bool(data.get("auto_detect", False)),
    )


# ─── ErpNet.FP configuration.json compatibility ──────────────────────


_URI_DRIVER_MAP = {
    # Maps the `bg.<vendor>.<protocol>` part of an ErpNet.FP URI to our
    # dotted driver path. Extend as more drivers are ported.
    "bg.dt.pm": "datecs.pm",
    "bg.dt.c.isl": "datecs.isl",
    "bg.dt.p.isl": "datecs.isl",
    "bg.dt.x.isl": "datecs.isl",
    "bg.dt.fp.isl": "datecs.isl",
    "bg.dy": "daisy",
    "bg.tr.zfp": "tremol.zfp",
    "bg.tr.icp": "tremol.icp",
    "bg.el": "eltrade",
    "bg.is.icp": "incotex",
}


def _parse_erpnet_uri(uri: str) -> tuple[str, str, str]:
    """ErpNet.FP URIs look like `bg.dt.c.isl.com://COM5` or
    `bg.dt.p.isl.tcp://192.168.1.77:9100`. Returns (driver, transport, addr).
    """
    if "://" not in uri:
        raise ValueError(f"Invalid ErpNet.FP URI: {uri!r}")
    scheme, addr = uri.split("://", 1)
    parts = scheme.split(".")
    # The trailing token after the last dot is the transport (com/tcp/http/bt)
    transport_token = parts[-1]
    transport = {"com": "serial", "tcp": "tcp", "http": "http", "bt": "serial"}.get(
        transport_token, "serial"
    )
    driver_key = ".".join(parts[:-1])
    driver = _URI_DRIVER_MAP.get(driver_key, driver_key)
    return driver, transport, addr


def _erpnet_json_to_app_config(data: dict) -> AppConfig:
    server = ServerConfig()  # ErpNet.FP-compat config has no server section
    printers: list[PrinterConfig] = []
    for printer_id, entry in (data.get("Printers") or {}).items():
        uri = entry.get("Uri", "")
        driver, transport, addr = _parse_erpnet_uri(uri)
        if transport == "serial":
            port = addr
            tcp_host = None
            tcp_port = None
        elif transport == "tcp":
            host, _, port_part = addr.partition(":")
            tcp_host = host
            tcp_port = int(port_part) if port_part else 9100
            port = None
        else:
            tcp_host = addr
            tcp_port = None
            port = None
        printers.append(
            PrinterConfig(
                id=printer_id,
                driver=driver,
                transport=transport,
                port=port,
                baudrate=int(entry.get("BaudRate", 115200)),
                tcp_host=tcp_host,
                tcp_port=tcp_port,
                operator=str(entry.get("Operator", "1")),
                operator_password=str(entry.get("OperatorPassword", "1")),
            )
        )
    return AppConfig(
        server=server,
        printers=printers,
        auto_detect=bool(data.get("AutoDetect", False)),
    )


# ─── Top-level dispatch ──────────────────────────────────────────────


def load_config(path: str | Path) -> AppConfig:
    """Load `path` and return AppConfig. Format is detected by extension."""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(p)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        return _yaml_to_app_config(yaml.safe_load(text) or {})
    if p.suffix.lower() == ".json":
        return _erpnet_json_to_app_config(json.loads(text))
    raise ValueError(
        f"Unknown config format {p.suffix}; expected .yaml/.yml or .json"
    )
