"""
PrinterRegistry — multi-driver registry with per-printer asyncio.Lock.

A single device can only be talked to by one writer at a time, so each
registry entry has its own lock. The HTTP layer always acquires through
`with_driver(printer_id)` which yields the appropriate driver instance
(PmDevice or IslDevice) under the lock.

Drivers supported:
  datecs.pm     — Datecs FP-700 MX and other PM v2.11.4 devices
  datecs.isl    — Datecs ISL family:
                    P/C  (DP-25, DP-05, WP-50, DP-35)
                    X    (FP-700X, WP-500X, DP-150X, FMP-350X)
                    FP   (FP-800, FP-2000, FP-650)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

# Persistent on-disk cache за IslDeviceInfo (FW/serial/FM/TIN).
# Без него — restart на proxy → info=празно докато device-ът не
# отговори (което не може ако paper-out / cable disconnected).
# С cache → последно successful detect остава видим.
_ISL_INFO_CACHE_FILE = Path(os.environ.get(
    "ODOO_ERPNET_FP_INFO_CACHE",
    "/app/data/.isl_info_cache.json"))
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Optional, Union

from ..config.loader import (
    AccessConfig,
    AppConfig,
    BiometricConfig,
    CameraConfig,
    DisplayConfig,
    MqttIngestConfig,
    PinpadConfig,
    PrinterConfig,
    ReaderConfig,
    ScaleConfig,
)
from ..drivers.cameras import (
    CameraStream,
    GenericRtspCameraStream,
    Go2RtcCameraStream,
    OnvifAnprCameraStream,
    OnvifCameraStream,
    make_lpr_engine,
)
from ..drivers.access import (
    AccessActuator,
    DahuaCgiActuator,
    GpioActuator,
    HikvisionIsapiActuator,
    MivActuator,
    OnvifRelayActuator,
    PolimexWebSdkActuator,
    RelayTcpActuator,
    WiegandActuator,
)
from ..drivers.biometric import (
    BiometricVerifier,
    FaceAuthVerifier,
)
from ..drivers.customer_displays import (
    CustomerDisplay,
    DatecsDpd201,
)
from ..drivers.pinpad.datecs_pay import DatecsPayPinpad
from ..drivers.readers import (
    BarcodeReader,
    BarcodeScan,
    HidBarcodeReader,
    SerialBarcodeReader,
)
from ..drivers.scales import (
    AsciiContinuousScale,
    CasPrIIScale,
    OhausRangerScale,
    Toledo8217Scale,
)

# Map driver name → CustomerDisplay subclass.
_DISPLAY_DRIVERS: dict[str, type[CustomerDisplay]] = {
    "datecs.dpd201": DatecsDpd201,
}
from .reader_bus import ReaderEventBus
from .camera_bus import CameraEventBus
from ..drivers.fiscal.datecs_isl import (
    DaisyIslDevice,
    DatecsIslDevice,
    DatecsIslXDevice,
    EltradeIslDevice,
    IncotexIslDevice,
    IslDevice,
    TremolIslDevice,
)
from ..drivers.fiscal.datecs_isl.transport_serial import (
    SerialTransport as IslSerialTransport,
)
from ..drivers.fiscal.datecs_isl.transport_tcp import (
    TcpTransport as IslTcpTransport,
)
from ..drivers.fiscal.datecs_pm import PmDevice
from ..drivers.fiscal.datecs_pm.transport_serial import SerialTransport
from ..drivers.fiscal.datecs_pm.transport_tcp import TcpTransport
from .schemas import DeviceInfo

# Map driver name → IslDevice subclass.
# `datecs.isl`  = C variant (DP-150 base, comma-sep, admin pw "9999")
# `datecs.islx` = X variant (DP-150X / FP-700X / FMP-350X, TAB-sep, pw "0000")
_ISL_DRIVERS: dict[str, type[IslDevice]] = {
    "datecs.isl": DatecsIslDevice,
    "datecs.islx": DatecsIslXDevice,
    "daisy.isl": DaisyIslDevice,
    "eltrade.isl": EltradeIslDevice,
    "incotex.isl": IncotexIslDevice,
    "tremol.isl": TremolIslDevice,
}

_logger = logging.getLogger(__name__)


# Anything we can hand to the routes layer
DriverInstance = Union[PmDevice, IslDevice]


@dataclass
class PrinterEntry:
    config: PrinterConfig
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    info: Optional[DeviceInfo] = None  # cached on first probe


SUPPORTED_DRIVERS = {"datecs.pm"} | set(_ISL_DRIVERS.keys())


class PrinterRegistry:
    def __init__(self) -> None:
        self.printers: dict[str, PrinterEntry] = {}

    @classmethod
    def from_config(cls, config: AppConfig) -> "PrinterRegistry":
        registry = cls()
        cached_info = registry._load_isl_info_cache()
        for cfg in config.printers:
            if cfg.id in registry.printers:
                raise ValueError(f"Duplicate printer id: {cfg.id!r}")
            if cfg.driver not in SUPPORTED_DRIVERS:
                raise ValueError(
                    f"Unsupported driver {cfg.driver!r} for printer {cfg.id!r}; "
                    f"known: {', '.join(sorted(SUPPORTED_DRIVERS))}"
                )
            entry = PrinterEntry(config=cfg)
            # Restore cached IslDeviceInfo if we have it from a
            # previous run (paper-out / cable-disconnected → still
            # show the last-known FW/serial/FM/TIN in the UI).
            if cfg.id in cached_info:
                entry._isl_info_cache = cached_info[cfg.id]
            registry.printers[cfg.id] = entry
            _logger.info(
                "Registered printer %r — driver=%s transport=%s addr=%s%s",
                cfg.id,
                cfg.driver,
                cfg.transport,
                cfg.port or f"{cfg.tcp_host}:{cfg.tcp_port}",
                " (info restored from cache)" if cfg.id in cached_info else "",
            )
        return registry

    # ─── Persistent ISL info cache ─────────────────────────────
    @staticmethod
    def _load_isl_info_cache():
        """Read cached IslDeviceInfo dict from disk, if file exists.
        Returns dict[printer_id → IslDeviceInfo]. Empty on first run
        or unreadable file (corrupted, permission, etc.).
        """
        try:
            from ..drivers.fiscal.datecs_isl.protocol import IslDeviceInfo
            if not _ISL_INFO_CACHE_FILE.exists():
                return {}
            raw = json.loads(_ISL_INFO_CACHE_FILE.read_text())
            out = {}
            for pid, d in (raw or {}).items():
                try:
                    out[pid] = IslDeviceInfo(**d)
                except Exception:
                    pass
            _logger.info("Loaded ISL info cache for %d printer(s) from %s",
                         len(out), _ISL_INFO_CACHE_FILE)
            return out
        except Exception as exc:
            _logger.warning("ISL info cache load failed: %s", exc)
            return {}

    def persist_isl_info_cache(self):
        """Write current cached IslDeviceInfo entries back to disk."""
        try:
            from dataclasses import asdict
            payload = {}
            for pid, entry in self.printers.items():
                info = getattr(entry, "_isl_info_cache", None)
                if info is not None:
                    payload[pid] = asdict(info)
            _ISL_INFO_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
            _ISL_INFO_CACHE_FILE.write_text(json.dumps(payload, indent=2))
        except Exception as exc:
            _logger.warning("ISL info cache persist failed: %s", exc)

    # ─── Driver factories ─────────────────────────────────────

    def _make_pm(self, cfg: PrinterConfig) -> PmDevice:
        if cfg.transport == "serial":
            if not cfg.port:
                raise ValueError(f"Serial printer {cfg.id} has no port")
            transport = SerialTransport(port=cfg.port, baudrate=cfg.baudrate)
        elif cfg.transport == "tcp":
            if not cfg.tcp_host or not cfg.tcp_port:
                raise ValueError(f"TCP printer {cfg.id} needs tcp_host + tcp_port")
            transport = TcpTransport(host=cfg.tcp_host, port=cfg.tcp_port)
        else:
            raise NotImplementedError(f"Transport {cfg.transport!r} not supported for PM")
        return PmDevice(
            transport=transport,
            op_code=int(cfg.operator),
            op_password=cfg.operator_password,
            till_number=cfg.till_number,
        )

    def _make_isl(self, cfg: PrinterConfig) -> IslDevice:
        if cfg.transport == "serial":
            if not cfg.port:
                raise ValueError(f"Serial printer {cfg.id} has no port")
            transport = IslSerialTransport(port=cfg.port, baudrate=cfg.baudrate)
        elif cfg.transport == "tcp":
            if not cfg.tcp_host or not cfg.tcp_port:
                raise ValueError(f"TCP printer {cfg.id} needs tcp_host + tcp_port")
            transport = IslTcpTransport(host=cfg.tcp_host, port=cfg.tcp_port)
        else:
            raise NotImplementedError(f"Transport {cfg.transport!r} not supported for ISL")
        device_cls = _ISL_DRIVERS.get(cfg.driver, DatecsIslDevice)
        return device_cls(
            transport=transport,
            operator_id=cfg.operator,
            operator_password=cfg.operator_password,
        )

    def make_driver(self, printer_id: str) -> DriverInstance:
        entry = self.get(printer_id)
        if entry.config.driver == "datecs.pm":
            return self._make_pm(entry.config)
        if entry.config.driver in _ISL_DRIVERS:
            return self._make_isl(entry.config)
        raise ValueError(f"Unknown driver: {entry.config.driver!r}")

    # ─── Public access ────────────────────────────────────────

    def get(self, printer_id: str) -> PrinterEntry:
        if printer_id not in self.printers:
            raise KeyError(printer_id)
        return self.printers[printer_id]

    def has(self, printer_id: str) -> bool:
        return printer_id in self.printers

    def driver_kind(self, printer_id: str) -> str:
        """Return the configured driver string ('datecs.pm' / 'datecs.isl' / ...)."""
        return self.get(printer_id).config.driver

    def is_isl(self, printer_id: str) -> bool:
        """True if the printer uses any ISL-family driver."""
        return self.driver_kind(printer_id) in _ISL_DRIVERS

    def is_pm(self, printer_id: str) -> bool:
        return self.driver_kind(printer_id) == "datecs.pm"

    @asynccontextmanager
    async def with_driver(self, printer_id: str):
        """Serialised, opened driver context for a printer.

        Yields PmDevice or IslDevice depending on configured driver,
        always inside the entry's asyncio.Lock. For ISL drivers, lazy-
        runs `detect()` on the entry's first use so `driver.info`
        (firmware, model, capability flags) is populated for every
        subsequent caller.
        """
        entry = self.get(printer_id)
        async with entry.lock:
            driver = self.make_driver(printer_id)
            driver.open()
            try:
                # Restore previously cached IslDeviceInfo if we have it.
                # We do NOT auto-run detect() here — that would fire 2+
                # ISL commands that take up to 5s × retries on an
                # unresponsive device, dragging /status checks to 30+s
                # and freezing the calling browser. Routes that genuinely
                # need capability info (e.g. invoice) call ensure_detect()
                # explicitly with their own timeout budget.
                cached = getattr(entry, "_isl_info_cache", None)
                if cached is not None and hasattr(driver, "info"):
                    driver.info = cached
                yield driver
            finally:
                try:
                    driver.close()
                except Exception:
                    _logger.exception("Failed to close driver for %s", printer_id)

    # Backwards-compat alias for the existing PM-only routes
    @asynccontextmanager
    async def with_pm(self, printer_id: str):
        """Legacy alias used by routes that only know PmDevice.
        Raises if the printer is configured for a different driver.
        """
        if self.driver_kind(printer_id) != "datecs.pm":
            raise RuntimeError(
                f"Printer {printer_id!r} is not a PM driver — routes that "
                f"require PM should branch on registry.driver_kind() first."
            )
        async with self.with_driver(printer_id) as drv:
            yield drv  # PmDevice


# ─── Pinpad registry (parallel to PrinterRegistry) ───────────────


@dataclass
class PinpadEntry:
    config: PinpadConfig
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Активният facade по време на операция — за /cancel да достигне
    # текущата транзакция БЕЗ да чака lock-а (който purchase държи).
    active_pinpad: object = None


_PINPAD_DRIVERS = {
    "datecs_pay": DatecsPayPinpad,
}


class PinpadRegistry:
    """Independent registry for POS payment terminals.

    Pinpads have their own URL prefix (`/pinpads/...`) since their API
    is not part of ErpNet.FP — it's an extension.
    """

    def __init__(self) -> None:
        self.pinpads: dict[str, PinpadEntry] = {}

    @classmethod
    def from_config(cls, config: AppConfig) -> "PinpadRegistry":
        registry = cls()
        for cfg in config.pinpads:
            if cfg.id in registry.pinpads:
                raise ValueError(f"Duplicate pinpad id: {cfg.id!r}")
            if cfg.driver not in _PINPAD_DRIVERS:
                raise ValueError(
                    f"Unsupported pinpad driver {cfg.driver!r}; "
                    f"known: {', '.join(_PINPAD_DRIVERS)}"
                )
            registry.pinpads[cfg.id] = PinpadEntry(config=cfg)
            _logger.info(
                "Registered pinpad %r — driver=%s port=%s",
                cfg.id, cfg.driver, cfg.port,
            )
        return registry

    def get(self, pinpad_id: str) -> PinpadEntry:
        if pinpad_id not in self.pinpads:
            raise KeyError(pinpad_id)
        return self.pinpads[pinpad_id]

    def has(self, pinpad_id: str) -> bool:
        return pinpad_id in self.pinpads

    def make_pinpad(self, pinpad_id: str) -> DatecsPayPinpad:
        entry = self.get(pinpad_id)
        cls = _PINPAD_DRIVERS[entry.config.driver]
        return cls(port=entry.config.port, baudrate=entry.config.baudrate)

    @asynccontextmanager
    async def with_pinpad(self, pinpad_id: str):
        entry = self.get(pinpad_id)
        async with entry.lock:
            pp = self.make_pinpad(pinpad_id)
            pp.open()
            entry.active_pinpad = pp  # за /cancel
            try:
                yield pp
            finally:
                entry.active_pinpad = None
                try:
                    pp.close()
                except Exception:
                    _logger.exception("Failed to close pinpad %s", pinpad_id)


# ─── Scale registry ──────────────────────────────────────────────


@dataclass
class ScaleEntry:
    config: ScaleConfig
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


_SCALE_DRIVERS = {
    # Mettler-Toledo 8217 — industrial scales (Ariva-S, Viva, etc.)
    "toledo_8217": Toledo8217Scale,
    "mettler.toledo.8217": Toledo8217Scale,
    "ariva-s": Toledo8217Scale,

    # CAS PR-II + all CAS-compatible BG market scales (~75% coverage):
    # native CAS, Elicom EVL in CASH47 jumper mode, Datecs in CAS mode.
    "cas": CasPrIIScale,
    "cas.pr2": CasPrIIScale,
    "cas.pd2": CasPrIIScale,
    "cas.psd": CasPrIIScale,
    "cas.pds": CasPrIIScale,
    "cas.pr_c": CasPrIIScale,
    "elicom.cash47": CasPrIIScale,
    "elicom.evl": CasPrIIScale,
    "datecs.cas": CasPrIIScale,

    # Generic ASCII continuous-stream scales — passive listener.
    # Covers ACS 6/15, ACS 15/30, JCS, no-name OEM Chinese scales.
    "adam": AsciiContinuousScale,
    "ascii": AsciiContinuousScale,
    "ascii_continuous": AsciiContinuousScale,
    "acs": AsciiContinuousScale,
    "jcs": AsciiContinuousScale,
    "generic": AsciiContinuousScale,

    # OHAUS Ranger 3000 / Count 3000 / Valor 7000 with Ethernet kit
    # 30037447 — TCP port 9761 fixed by firmware. `port` config field
    # is the host or "host:9761" string, baudrate is ignored.
    "ohaus_ranger": OhausRangerScale,
    "ohaus": OhausRangerScale,
    "ranger3000": OhausRangerScale,
    "ranger.count3000": OhausRangerScale,
    "valor7000": OhausRangerScale,
}


class ScaleRegistry:
    def __init__(self) -> None:
        self.scales: dict[str, ScaleEntry] = {}

    @classmethod
    def from_config(cls, config: AppConfig) -> "ScaleRegistry":
        registry = cls()
        for cfg in config.scales:
            if cfg.id in registry.scales:
                raise ValueError(f"Duplicate scale id: {cfg.id!r}")
            if cfg.driver not in _SCALE_DRIVERS:
                raise ValueError(
                    f"Unsupported scale driver {cfg.driver!r}; "
                    f"known: {', '.join(sorted(_SCALE_DRIVERS))}"
                )
            registry.scales[cfg.id] = ScaleEntry(config=cfg)
            _logger.info(
                "Registered scale %r — driver=%s port=%s",
                cfg.id, cfg.driver, cfg.port,
            )
        return registry

    def get(self, scale_id: str) -> ScaleEntry:
        if scale_id not in self.scales:
            raise KeyError(scale_id)
        return self.scales[scale_id]

    def has(self, scale_id: str) -> bool:
        return scale_id in self.scales

    def make_scale(self, scale_id: str):
        """Returns one of: Toledo8217Scale, CasPrIIScale, AsciiContinuousScale."""
        entry = self.get(scale_id)
        cls = _SCALE_DRIVERS[entry.config.driver]
        return cls(port=entry.config.port, baudrate=entry.config.baudrate)

    @asynccontextmanager
    async def with_scale(self, scale_id: str):
        entry = self.get(scale_id)
        async with entry.lock:
            sc = self.make_scale(scale_id)
            sc.open()
            try:
                yield sc
            finally:
                try:
                    sc.close()
                except Exception:
                    _logger.exception("Failed to close scale %s", scale_id)


# ─── Reader registry — push model with background threads ────────


@dataclass
class ReaderEntry:
    config: ReaderConfig
    bus: ReaderEventBus
    driver: Optional[BarcodeReader] = None  # populated by start_all()


class ReaderRegistry:
    """Long-lived reader registry.

    Unlike printers/pinpads/scales (which open/close per request), readers
    keep their device open for the lifetime of the server. A background
    thread per reader continuously decodes incoming bytes; each completed
    barcode is `publish_threadsafe()`-ed into a `ReaderEventBus` that
    fans out to WebSocket / SSE / webhook subscribers.
    """

    def __init__(self) -> None:
        self.readers: dict[str, ReaderEntry] = {}

    @classmethod
    def from_config(cls, config: AppConfig) -> "ReaderRegistry":
        registry = cls()
        for cfg in config.readers:
            if cfg.id in registry.readers:
                raise ValueError(f"Duplicate reader id: {cfg.id!r}")
            if cfg.transport not in ("hid", "serial", "external", "tcp"):
                raise ValueError(
                    f"Unknown reader transport {cfg.transport!r} on {cfg.id!r}; "
                    f"expected 'hid', 'serial', 'tcp', or 'external'"
                )
            bus = ReaderEventBus(reader_id=cfg.id, webhooks=cfg.webhooks,
                              access_control=cfg.access_control)
            registry.readers[cfg.id] = ReaderEntry(config=cfg, bus=bus)
            _logger.info(
                "Registered reader %r — transport=%s addr=%s webhooks=%d",
                cfg.id, cfg.transport,
                cfg.device_path or cfg.port or "?", len(cfg.webhooks),
            )
        return registry

    # ─── Public access ────────────────────────────────────────

    def get(self, reader_id: str) -> ReaderEntry:
        if reader_id not in self.readers:
            raise KeyError(reader_id)
        return self.readers[reader_id]

    def has(self, reader_id: str) -> bool:
        return reader_id in self.readers

    def get_bus(self, reader_id: str) -> ReaderEventBus:
        return self.get(reader_id).bus

    # ─── Lifecycle (called from FastAPI startup/shutdown) ──────

    async def start_all(self, loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """Open all configured readers + start their background threads.

        Failures are logged per-reader and do not block other readers.
        """
        loop = loop or asyncio.get_running_loop()
        for entry in self.readers.values():
            entry.bus._loop = loop  # bind bus to running loop so publish works
            # external transport — bus only, no in-proc driver. Scans arrive
            # via POST /readers/{id}/inject from a host-side listener.
            if entry.config.transport == "external":
                _logger.info(
                    "Reader %r is external — listening on /readers/%s/inject",
                    entry.config.id, entry.config.id,
                )
                continue
            try:
                driver = self._make_driver(entry.config)
                driver.set_listener(entry.bus.publish_threadsafe)
                driver.start()
                entry.driver = driver
            except Exception:
                _logger.exception(
                    "Failed to start reader %r", entry.config.id
                )

    async def stop_all(self) -> None:
        for entry in self.readers.values():
            if entry.driver is not None:
                try:
                    entry.driver.stop()
                except Exception:
                    _logger.exception(
                        "Failed to stop reader %r", entry.config.id
                    )
            try:
                await entry.bus.close()
            except Exception:
                _logger.exception(
                    "Failed to close bus for reader %r", entry.config.id
                )

    # ─── Hot-plug add/remove (called from reader_autodetect.py) ──

    async def add_dynamic(
        self,
        cfg: ReaderConfig,
        loop: Optional[asyncio.AbstractEventLoop] = None,
    ) -> bool:
        """Register + start a reader at runtime (from udev hot-plug).

        Returns True on success, False on duplicate id or driver
        failure. Idempotent — silently no-ops if `cfg.id` is already
        registered.
        """
        if cfg.id in self.readers:
            return False
        loop = loop or asyncio.get_running_loop()
        bus = ReaderEventBus(reader_id=cfg.id, webhooks=cfg.webhooks,
                              access_control=cfg.access_control)
        bus._loop = loop
        entry = ReaderEntry(config=cfg, bus=bus)
        try:
            driver = self._make_driver(cfg)
            driver.set_listener(bus.publish_threadsafe)
            driver.start()
            entry.driver = driver
        except Exception:
            _logger.exception(
                "add_dynamic: failed to start reader %r", cfg.id
            )
            return False
        self.readers[cfg.id] = entry
        _logger.info(
            "Hot-plug registered reader %r — transport=%s addr=%s",
            cfg.id, cfg.transport, cfg.port or cfg.device_path or "?",
        )
        return True

    async def remove_dynamic(self, reader_id: str) -> bool:
        """Stop + unregister a reader at runtime (from udev unplug).

        Returns True if the reader was removed, False if it wasn't
        registered. Idempotent.
        """
        entry = self.readers.pop(reader_id, None)
        if entry is None:
            return False
        if entry.driver is not None:
            try:
                entry.driver.stop()
            except Exception:
                _logger.exception(
                    "remove_dynamic: stop failed for %r", reader_id
                )
        try:
            await entry.bus.close()
        except Exception:
            _logger.exception(
                "remove_dynamic: bus close failed for %r", reader_id
            )
        _logger.info("Hot-plug unregistered reader %r", reader_id)
        return True

    @staticmethod
    def _make_driver(cfg: ReaderConfig) -> BarcodeReader:
        if cfg.transport == "hid":
            if not (cfg.device_path or cfg.vid or cfg.pid or cfg.name_regex):
                raise ValueError(
                    f"HID reader {cfg.id!r} needs at least one of: "
                    "device_path, vid+pid, or name_regex"
                )
            return HidBarcodeReader(
                reader_id=cfg.id,
                device_path=cfg.device_path,
                vid=cfg.vid,
                pid=cfg.pid,
                name_regex=cfg.name_regex,
                grab=cfg.grab,
                terminator=cfg.terminator,
                strip_prefix=cfg.strip_prefix,
                strip_suffix=cfg.strip_suffix,
                max_length=cfg.max_length,
                caps_lock_strategy=cfg.caps_lock_strategy,
            )
        if cfg.transport == "tcp":
            # BlueCash-50 ScannerBridge (channel 3, port 9102) и подобни
            # push-only event sources: newline-delimited barcodes по TCP.
            host = (cfg.tcp_host or cfg.device_path or "").strip()
            port = int(cfg.tcp_port or 0)
            if not host or not port:
                raise ValueError(
                    f"TCP reader {cfg.id!r} needs `tcp_host` and `tcp_port` "
                    f"(e.g. 192.168.1.70:9102)"
                )
            from ..drivers.readers.tcp_reader import TcpBarcodeReader
            return TcpBarcodeReader(
                reader_id=cfg.id, host=host, port=port,
                encoding=cfg.encoding,
            )
        # serial
        if not cfg.port:
            raise ValueError(
                f"Serial reader {cfg.id!r} needs `port` (e.g. /dev/ttyUSB?)"
            )
        return SerialBarcodeReader(
            reader_id=cfg.id,
            port=cfg.port,
            baudrate=cfg.baudrate,
            encoding=cfg.encoding,
        )


# ─── Customer-display registry — opened on demand, per-id lock ───


@dataclass
class DisplayEntry:
    config: DisplayConfig
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    driver: Optional[CustomerDisplay] = None


class DisplayRegistry:
    """Customer-facing pole displays (VFD/LCD).

    Unlike printers, displays have no readback channel — they're write-
    only. The registry holds an open serial port per display for the
    lifetime of the process to avoid the open/close jitter that kills
    the first byte of each command on cheap clones.
    """

    def __init__(self) -> None:
        self.displays: dict[str, DisplayEntry] = {}

    @classmethod
    def from_config(cls, config: AppConfig) -> "DisplayRegistry":
        registry = cls()
        for cfg in config.displays:
            if cfg.id in registry.displays:
                raise ValueError(f"Duplicate display id: {cfg.id!r}")
            if cfg.driver not in _DISPLAY_DRIVERS:
                raise ValueError(
                    f"Unsupported display driver {cfg.driver!r}; "
                    f"known: {', '.join(sorted(_DISPLAY_DRIVERS))}"
                )
            registry.displays[cfg.id] = DisplayEntry(config=cfg)
            _logger.info(
                "Registered display %r — driver=%s port=%s encoding=%s",
                cfg.id, cfg.driver, cfg.port, cfg.encoding,
            )
        return registry

    def get(self, display_id: str) -> DisplayEntry:
        if display_id not in self.displays:
            raise KeyError(display_id)
        return self.displays[display_id]

    def has(self, display_id: str) -> bool:
        return display_id in self.displays

    def _make_driver(self, cfg: DisplayConfig) -> CustomerDisplay:
        cls = _DISPLAY_DRIVERS[cfg.driver]
        return cls(
            display_id=cfg.id,
            port=cfg.port,
            baudrate=cfg.baudrate,
            encoding=cfg.encoding,
            chars_per_line=cfg.chars_per_line,
            lines=cfg.lines,
        )

    async def start_all(self) -> None:
        """Open every configured display once at startup. Failures are
        logged per-display; the proxy keeps running with the rest."""
        for entry in self.displays.values():
            try:
                drv = self._make_driver(entry.config)
                drv.open()
                entry.driver = drv
            except Exception:
                _logger.exception(
                    "Failed to open display %r", entry.config.id
                )

    async def stop_all(self) -> None:
        for entry in self.displays.values():
            if entry.driver is None:
                continue
            try:
                entry.driver.close()
            except Exception:
                _logger.exception(
                    "Failed to close display %r", entry.config.id
                )
            entry.driver = None

    @asynccontextmanager
    async def with_display(self, display_id: str):
        entry = self.get(display_id)
        async with entry.lock:
            if entry.driver is None:
                # Lazy reopen if start_all() failed (e.g. cable was
                # unplugged at boot, plugged in later).
                drv = self._make_driver(entry.config)
                drv.open()
                entry.driver = drv
            yield entry.driver


# ─── Camera registry — push model, go2rtc-backed, LPR events ─────


@dataclass
class CameraEntry:
    config: CameraConfig
    bus: CameraEventBus
    driver: Optional[CameraStream] = None  # populated by start_all()


class CameraRegistry:
    """Long-lived camera registry — same lifecycle as ReaderRegistry.

    Each camera keeps a background sampling thread that pulls JPEG
    frames from the go2rtc sibling, runs them through the pluggable
    LPR engine, and `publish_threadsafe()`-es every recognised plate
    onto a `CameraEventBus` (WS / SSE / webhook / native-IoT fanout).
    """

    _VALID_DRIVERS = ("rtsp", "onvif", "go2rtc", "external")

    def __init__(self) -> None:
        self.cameras: dict[str, CameraEntry] = {}

    @classmethod
    def from_config(cls, config: AppConfig) -> "CameraRegistry":
        registry = cls()
        for cfg in config.cameras:
            if cfg.id in registry.cameras:
                raise ValueError(f"Duplicate camera id: {cfg.id!r}")
            if cfg.driver not in cls._VALID_DRIVERS:
                raise ValueError(
                    f"Unknown camera driver {cfg.driver!r} on {cfg.id!r}; "
                    f"expected one of {', '.join(cls._VALID_DRIVERS)}"
                )
            bus = CameraEventBus(camera_id=cfg.id, webhooks=cfg.webhooks)
            registry.cameras[cfg.id] = CameraEntry(config=cfg, bus=bus)
            _logger.info(
                "Registered camera %r — driver=%s lpr=%s webhooks=%d",
                cfg.id, cfg.driver,
                cfg.lpr_engine if cfg.lpr_enabled else "off",
                len(cfg.webhooks),
            )
        return registry

    # ─── Public access ────────────────────────────────────────

    def get(self, camera_id: str) -> CameraEntry:
        if camera_id not in self.cameras:
            raise KeyError(camera_id)
        return self.cameras[camera_id]

    def has(self, camera_id: str) -> bool:
        return camera_id in self.cameras

    def get_bus(self, camera_id: str) -> CameraEventBus:
        return self.get(camera_id).bus

    # ─── Driver factory ───────────────────────────────────────

    @staticmethod
    def _make_driver(cfg: CameraConfig) -> CameraStream:
        lpr = make_lpr_engine(
            enabled=cfg.lpr_enabled,
            engine=cfg.lpr_engine,
            url=cfg.lpr_url,
            min_confidence=cfg.lpr_min_confidence,
            region=cfg.lpr_region,
        )
        common = dict(
            go2rtc_url=cfg.go2rtc_url,
            go2rtc_public_url=cfg.go2rtc_public_url,
            stream_name=cfg.stream_name or cfg.id,
            lpr_engine=lpr,
            interval_seconds=cfg.lpr_interval_seconds,
            dedupe_cooldown_seconds=cfg.dedupe_cooldown_seconds,
            include_image=cfg.include_image,
        )
        if cfg.driver == "rtsp":
            if not cfg.source:
                raise ValueError(
                    f"Camera {cfg.id!r}: rtsp driver needs `source` "
                    f"(rtsp:// URL)"
                )
            return GenericRtspCameraStream(
                camera_id=cfg.id, rtsp_url=cfg.source, **common
            )
        if cfg.driver == "onvif":
            onvif_kw = dict(
                camera_id=cfg.id,
                host=cfg.onvif_host or "",
                port=cfg.onvif_port,
                user=cfg.onvif_user,
                password=cfg.onvif_password,
                subtype=cfg.onvif_subtype,
                control=cfg.onvif_control,
                relay_output=cfg.onvif_relay_output,
                **common,
            )
            if cfg.onvif_anpr:
                # Камерата сама прави ANPR → events, без sidecar.
                onvif_kw["events_topic"] = cfg.onvif_events_topic
                return OnvifAnprCameraStream(**onvif_kw)
            return OnvifCameraStream(**onvif_kw)
        # "go2rtc" — стриймът е дефиниран server-side в go2rtc.yaml;
        # `source` може да липсва (Go2RtcCameraStream го толерира).
        return Go2RtcCameraStream(
            camera_id=cfg.id, source=cfg.source, **common
        )

    # ─── Lifecycle (called from FastAPI startup/shutdown) ──────

    async def start_all(
        self, loop: Optional[asyncio.AbstractEventLoop] = None
    ) -> None:
        loop = loop or asyncio.get_running_loop()
        for entry in self.cameras.values():
            entry.bus._loop = loop  # bind bus to running loop
            if entry.config.driver == "external":
                _logger.info(
                    "Camera %r is external — listening on "
                    "/cameras/%s/inject",
                    entry.config.id, entry.config.id,
                )
                continue
            try:
                driver = self._make_driver(entry.config)
                driver.set_listener(entry.bus.publish_threadsafe)
                driver.start()
                entry.driver = driver
            except Exception:
                _logger.exception(
                    "Failed to start camera %r", entry.config.id
                )

    async def stop_all(self) -> None:
        for entry in self.cameras.values():
            if entry.driver is not None:
                try:
                    entry.driver.stop()
                except Exception:
                    _logger.exception(
                        "Failed to stop camera %r", entry.config.id
                    )
            try:
                await entry.bus.close()
            except Exception:
                _logger.exception(
                    "Failed to close bus for camera %r", entry.config.id
                )


# ─── Access-control registry (Phase B) — command-style, per-id lock ──


@dataclass
class AccessEntry:
    config: AccessConfig
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    actuator: Optional[AccessActuator] = None


class AccessRegistry:
    """Barrier / relay / turnstile controllers.

    Same shape as DisplayRegistry — discrete synchronous commands
    under a per-id lock. The proxy only EXECUTES an Odoo-authorised
    open/deny; it never decides and never auto-opens (fail-secure).
    """

    _VALID = ("relay_tcp", "onvif", "gpio", "polimex", "hikvision",
              "dahua", "wiegand", "miv")

    def __init__(self) -> None:
        self.access: dict[str, AccessEntry] = {}

    @classmethod
    def from_config(cls, config: AppConfig) -> "AccessRegistry":
        registry = cls()
        for cfg in config.access:
            if cfg.id in registry.access:
                raise ValueError(f"Duplicate access id: {cfg.id!r}")
            if cfg.driver not in cls._VALID:
                raise ValueError(
                    f"Unknown access driver {cfg.driver!r} on {cfg.id!r}; "
                    f"expected one of {', '.join(cls._VALID)}"
                )
            registry.access[cfg.id] = AccessEntry(config=cfg)
            _logger.info(
                "Registered access %r — driver=%s fail_secure=%s",
                cfg.id, cfg.driver, cfg.fail_secure,
            )
        return registry

    def get(self, access_id: str) -> AccessEntry:
        if access_id not in self.access:
            raise KeyError(access_id)
        return self.access[access_id]

    def has(self, access_id: str) -> bool:
        return access_id in self.access

    @staticmethod
    def _make(cfg: AccessConfig) -> AccessActuator:
        if cfg.driver == "relay_tcp":
            return RelayTcpActuator(
                cfg.id, host=cfg.host or "", port=cfg.port,
                on_cmd=cfg.on_cmd, off_cmd=cfg.off_cmd,
                pulse_seconds=cfg.pulse_seconds,
                fail_secure=cfg.fail_secure,
            )
        if cfg.driver == "onvif":
            return OnvifRelayActuator(
                cfg.id, host=cfg.host or "", port=cfg.port or 80,
                user=cfg.user, password=cfg.password,
                relay_output=cfg.relay_output,
                pulse_seconds=cfg.pulse_seconds,
                fail_secure=cfg.fail_secure,
            )
        if cfg.driver == "gpio":
            return GpioActuator(
                cfg.id, pin=cfg.pin, active_high=cfg.active_high,
                pulse_seconds=cfg.pulse_seconds,
                fail_secure=cfg.fail_secure,
            )
        # HTTP-API драйверите ползват порт 80 по подразбиране;
        # AccessConfig.port default (23) е relay_tcp/telnet — не
        # бива да изтича към ISAPI/CGI/WebSDK.
        http_port = cfg.port if cfg.port not in (0, 23) else 80
        if cfg.driver == "polimex":
            return PolimexWebSdkActuator(
                cfg.id, host=cfg.host or "", port=http_port,
                user=cfg.user or "SDK", password=cfg.password or "0000",
                bus_id=cfg.bus_id, output=cfg.output,
                relay_ctrl=cfg.relay_ctrl, mode=cfg.mode,
                pulse_seconds=cfg.pulse_seconds,
                fail_secure=cfg.fail_secure,
            )
        if cfg.driver == "hikvision":
            return HikvisionIsapiActuator(
                cfg.id, host=cfg.host or "", port=http_port,
                user=cfg.user or "admin", password=cfg.password or "",
                door_no=cfg.output,
                fail_secure=cfg.fail_secure,
            )
        if cfg.driver == "dahua":
            return DahuaCgiActuator(
                cfg.id, host=cfg.host or "", port=http_port,
                user=cfg.user or "admin", password=cfg.password or "",
                channel=cfg.output, user_id=cfg.user_id,
                fail_secure=cfg.fail_secure,
            )
        if cfg.driver == "wiegand":
            return WiegandActuator(cfg.id, fail_secure=cfg.fail_secure)
        return MivActuator(
            cfg.id, host=cfg.host or "", port=cfg.port,
            extras=cfg.extras, fail_secure=cfg.fail_secure,
        )

    async def start_all(self) -> None:
        for entry in self.access.values():
            try:
                act = self._make(entry.config)
                # Само персистентните транспорти (gpio) реално отварят
                # тук; relay_tcp/onvif connect() е no-op (lazy per cmd).
                act.connect()
                entry.actuator = act
            except Exception:
                # Стартът не блокира — командите ще пробват пак чрез
                # with_access() (важно за wiegand/miv stub-овете).
                _logger.warning(
                    "Access %r not ready at boot (%s) — lazy on command",
                    entry.config.id, entry.config.driver,
                )

    async def stop_all(self) -> None:
        for entry in self.access.values():
            if entry.actuator is not None:
                try:
                    entry.actuator.disconnect()
                except Exception:
                    _logger.exception(
                        "Failed to disconnect access %r", entry.config.id
                    )
            entry.actuator = None

    @asynccontextmanager
    async def with_access(self, access_id: str):
        entry = self.get(access_id)
        async with entry.lock:
            if entry.actuator is None:
                act = self._make(entry.config)
                act.connect()
                entry.actuator = act
            yield entry.actuator


# ─── Biometric registry (Phase C / #6) — request/response, per-id lock ─


@dataclass
class BiometricEntry:
    config: BiometricConfig
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    verifier: Optional[BiometricVerifier] = None


class BiometricRegistry:
    """Face-identity verifiers (Channel-1 transport).

    Same shape as AccessRegistry — discrete synchronous ops under a
    per-id lock. The proxy is a THIN CLIENT to the external face-auth
    Node μsvc; it never reimplements matching and never decides
    attendance/access (Odoo enforces `x_bio_consent`, fail-secure).
    """

    _VALID = ("faceauth",)

    def __init__(self) -> None:
        self.biometric: dict[str, BiometricEntry] = {}

    @classmethod
    def from_config(cls, config: AppConfig) -> "BiometricRegistry":
        registry = cls()
        for cfg in config.biometric:
            if cfg.id in registry.biometric:
                raise ValueError(f"Duplicate biometric id: {cfg.id!r}")
            if cfg.driver not in cls._VALID:
                raise ValueError(
                    f"Unknown biometric driver {cfg.driver!r} on "
                    f"{cfg.id!r}; expected one of "
                    f"{', '.join(cls._VALID)}"
                )
            registry.biometric[cfg.id] = BiometricEntry(config=cfg)
            _logger.info(
                "Registered biometric %r — driver=%s base_url=%s",
                cfg.id, cfg.driver, cfg.base_url or "(unset)",
            )
        return registry

    def get(self, biometric_id: str) -> BiometricEntry:
        if biometric_id not in self.biometric:
            raise KeyError(biometric_id)
        return self.biometric[biometric_id]

    def has(self, biometric_id: str) -> bool:
        return biometric_id in self.biometric

    @staticmethod
    def _make(cfg: BiometricConfig) -> BiometricVerifier:
        # Само `faceauth` засега (вендор-агностичен ABC за бъдещи).
        return FaceAuthVerifier(
            cfg.id, base_url=cfg.base_url, timeout=cfg.timeout,
            fail_secure=cfg.fail_secure,
        )

    async def start_all(self) -> None:
        for entry in self.biometric.values():
            try:
                v = self._make(entry.config)
                v.connect()
                entry.verifier = v
            except Exception:
                # Стартът не блокира — lazy on command (with_biometric).
                _logger.warning(
                    "Biometric %r not ready at boot — lazy on command",
                    entry.config.id,
                )

    async def stop_all(self) -> None:
        for entry in self.biometric.values():
            if entry.verifier is not None:
                try:
                    entry.verifier.disconnect()
                except Exception:
                    _logger.exception(
                        "Failed to disconnect biometric %r",
                        entry.config.id,
                    )
            entry.verifier = None

    @asynccontextmanager
    async def with_biometric(self, biometric_id: str):
        entry = self.get(biometric_id)
        async with entry.lock:
            if entry.verifier is None:
                v = self._make(entry.config)
                v.connect()
                entry.verifier = v
            yield entry.verifier


# ─── MQTT Ingest (multi-broker fan-out, R5) ──────────────────────────


class MqttIngestRegistry:
    """Multi-broker MQTT subscriber registry.

    One MqttCameraIngest instance per `MqttBrokerSpec`, keyed by
    `spec.name`. Each broker runs its OWN paho-mqtt connection +
    subscriber thread; they don't share state, so a flaky broker can't
    take down the others.

    Lifecycle parity with CameraRegistry / AccessRegistry:
      `from_config` / `start_all` / `stop_all` / `status`.

    Multi-broker hot-reload (`reload_from_config(new_cfg)`) does a
    granular diff: brokers removed in the new config get stopped,
    new ones get started, and brokers whose spec changed get
    restarted in-place. Untouched brokers keep running — this is the
    "form button push-ва САМО този broker" semantic R5 promises.

    When `mqtt_brokers` is empty (default), every method is a no-op
    and paho-mqtt is never imported.
    """

    def __init__(self):
        # spec.name → MqttBrokerSpec (last config we built from)
        self.specs: dict[str, "MqttBrokerSpec"] = {}
        # spec.name → MqttCameraIngest (lazy; only enabled brokers
        # ever get an instance)
        self.ingests: dict[str, object] = {}
        self._camera_registry: Optional["CameraRegistry"] = None

    @classmethod
    def from_config(cls, config: AppConfig) -> "MqttIngestRegistry":
        r = cls()
        for spec in getattr(config, "mqtt_brokers", []) or []:
            r.specs[spec.name] = spec
        return r

    # Back-compat shim for code that still expects `.config`/`.ingest`
    # (`hot_reload_ac_fragment("cameras")` updates `mqtt_reg.ingest.
    # camera_registry`). Returns the first enabled instance, or None.
    @property
    def ingest(self):
        for name, spec in self.specs.items():
            if spec.enabled and name in self.ingests:
                return self.ingests[name]
        return None

    def bind(self, camera_registry: "CameraRegistry") -> None:
        """Inject the CameraRegistry so subscribers can resolve
        camera ids → CameraEventBus. Propagates to already-built
        ingest instances too — used by hot_reload_ac_fragment("cameras")
        to swap the registry without restarting brokers.
        """
        self._camera_registry = camera_registry
        for ingest in self.ingests.values():
            ingest.camera_registry = camera_registry

    def start_all(self) -> None:
        if self._camera_registry is None:
            if any(s.enabled for s in self.specs.values()):
                _logger.warning(
                    "MqttIngestRegistry.start_all called without bind() — "
                    "skipping; camera_registry is required")
            return
        # Lazy import — only paid if we actually have an enabled broker.
        from ..drivers.cameras.mqtt_listener import MqttCameraIngest
        for name, spec in self.specs.items():
            if not spec.enabled:
                continue
            if name not in self.ingests:
                self.ingests[name] = MqttCameraIngest(spec, self._camera_registry)
            self.ingests[name].start()

    def stop_all(self) -> None:
        for ingest in list(self.ingests.values()):
            try:
                ingest.stop()
            except Exception as exc:  # noqa: BLE001
                _logger.warning("MQTT ingest[%s] stop failed: %s",
                                getattr(ingest, "name", "?"), exc)
        self.ingests.clear()

    # ─── Runtime per-broker toggle (landing UI helpers) ─────────
    # NB: these do NOT touch config.d/mqtt.yaml. The toggle is purely
    # in-process — at the next sync from Odoo (or proxy restart) the
    # broker reverts to whatever `enabled:` says in the YAML. This is
    # intentional: Odoo stays the source of truth; the landing button
    # is a debug convenience, not a config edit.

    def start_one(self, name: str) -> bool:
        """Start one named broker. Returns True if newly started."""
        spec = self.specs.get(name)
        if spec is None:
            return False
        if self._camera_registry is None:
            _logger.warning(
                "start_one(%s) before bind() — camera_registry required", name)
            return False
        from ..drivers.cameras.mqtt_listener import MqttCameraIngest
        ingest = self.ingests.get(name)
        if ingest is None:
            ingest = MqttCameraIngest(spec, self._camera_registry)
            self.ingests[name] = ingest
        return bool(ingest.start())

    def stop_one(self, name: str) -> bool:
        """Stop one named broker but keep its spec — the operator can
        Start it again from the same UI button. Returns True if it
        was running and is now stopped.
        """
        ingest = self.ingests.get(name)
        if ingest is None:
            return False
        result = bool(ingest.stop())
        # Drop the instance so the next start_one() rebuilds it
        # cleanly with current spec (covers the rare edge case where
        # the spec mutated since the original construction).
        self.ingests.pop(name, None)
        return result

    def status(self) -> dict:
        """Multi-broker status — one entry per known broker.

        Top-level shape:
            {"brokers": [<per-broker status>, ...],
             "enabled_count": N, "running_count": M}
        """
        brokers: list[dict] = []
        running = 0
        enabled = 0
        for name, spec in self.specs.items():
            if spec.enabled:
                enabled += 1
            ingest = self.ingests.get(name)
            if ingest is not None:
                s = ingest.status()
                brokers.append(s)
                if s.get("running"):
                    running += 1
            else:
                brokers.append({
                    "name": name,
                    "enabled": spec.enabled,
                    "running": False,
                    "connected": False,
                    "host": spec.host,
                    "port": spec.port,
                    "topics": list(spec.topics),
                })
        return {
            "brokers": brokers,
            "enabled_count": enabled,
            "running_count": running,
        }

    # ─── Granular hot-reload (R5) ───────────────────────────────

    def reload_from_config(self, new_config: AppConfig) -> dict:
        """Diff old vs new brokers; restart only what changed.

        Returns a dict with `added/removed/restarted/unchanged` lists
        (broker names) — useful for the push_config response payload
        so Odoo / the operator can see exactly what happened.
        """
        # Lazy import — only if we have at least one broker to start.
        new_specs = {s.name: s for s in (getattr(new_config, "mqtt_brokers", []) or [])}
        old_specs = dict(self.specs)

        added: list[str] = []
        removed: list[str] = []
        restarted: list[str] = []
        unchanged: list[str] = []

        # 1. Stop & drop brokers that no longer exist OR became disabled.
        for name, old in old_specs.items():
            new = new_specs.get(name)
            if new is None:
                ingest = self.ingests.pop(name, None)
                if ingest is not None:
                    try:
                        ingest.stop()
                    except Exception:  # noqa: BLE001
                        pass
                removed.append(name)
            elif old.enabled and not new.enabled:
                ingest = self.ingests.pop(name, None)
                if ingest is not None:
                    try:
                        ingest.stop()
                    except Exception:  # noqa: BLE001
                        pass
                restarted.append(name)  # treat enable→disable as a restart event

        # 2. For brokers present in BOTH, check whether the spec changed.
        for name, new in new_specs.items():
            old = old_specs.get(name)
            if old is None or (old.enabled and not new.enabled):
                continue  # handled below / above
            if _broker_spec_dirty(old, new):
                ingest = self.ingests.pop(name, None)
                if ingest is not None:
                    try:
                        ingest.stop()
                    except Exception:  # noqa: BLE001
                        pass
                if new.enabled:
                    from ..drivers.cameras.mqtt_listener import MqttCameraIngest
                    fresh = MqttCameraIngest(new, self._camera_registry)
                    fresh.start()
                    self.ingests[name] = fresh
                restarted.append(name)
            else:
                unchanged.append(name)

        # 3. Brand-new brokers — instantiate & start if enabled.
        for name, new in new_specs.items():
            if name in old_specs:
                continue
            if new.enabled and self._camera_registry is not None:
                from ..drivers.cameras.mqtt_listener import MqttCameraIngest
                fresh = MqttCameraIngest(new, self._camera_registry)
                fresh.start()
                self.ingests[name] = fresh
            added.append(name)

        # 4. Sync stored specs to the new config.
        self.specs = new_specs

        return {
            "added": added,
            "removed": removed,
            "restarted": restarted,
            "unchanged": unchanged,
        }


def _broker_spec_dirty(old: "MqttBrokerSpec", new: "MqttBrokerSpec") -> bool:
    """True iff old/new differ on any field that demands a reconnect.

    `name` is the registry key so it can never differ here. `debug`
    and `max_log_payload` are runtime-only and don't justify a
    reconnect, but we restart on them anyway for simplicity — the
    rare flip is cheap and avoids surprise behaviour.
    """
    keys = ("enabled", "host", "port", "tls", "user", "password",
            "qos", "keepalive", "reconnect_attempts", "reconnect_delay",
            "debug", "max_log_payload")
    if tuple(old.topics) != tuple(new.topics):
        return True
    return any(getattr(old, k) != getattr(new, k) for k in keys)


# ─── Per-hardware online reload (push_config target) ────────────────


# Whitelist of AC sections the Fleet push_config command is allowed to
# rewrite. Fiscal sections (printers/pinpads/scales/displays/readers)
# stay under customer-IT manual control — never rewritten from Odoo.
PUSH_CONFIG_AC_KINDS = ("cameras", "access", "biometric", "mqtt")


def _write_fragment_atomic(fragment_path: "Path", section: str, payload: Any) -> str:
    """Atomically write a single AC section fragment.

    payload format is uniformly `list[dict]` (one entry per device or
    broker) for all four AC kinds:
      cameras / access / biometric / mqtt → list[dict]

    The loader also accepts a legacy single-broker `mqtt:` dict for
    backwards compat, but Odoo always writes the list form.

    Returns SHA-256 hex of the file bytes (used as runtime_config_version
    so Odoo can detect in-sync / out-of-sync per record).
    """
    import hashlib
    import json as _json
    import os
    import tempfile
    import yaml

    fragment_path.parent.mkdir(parents=True, exist_ok=True)
    body = yaml.safe_dump({section: payload}, sort_keys=False, allow_unicode=True)
    # Atomic write: tmp in same dir → fsync → rename
    with tempfile.NamedTemporaryFile(
            mode="w",
            dir=str(fragment_path.parent),
            prefix=f".{fragment_path.name}.",
            suffix=".tmp",
            delete=False,
            encoding="utf-8") as tf:
        tf.write(body)
        tf.flush()
        os.fsync(tf.fileno())
        tmp_path = tf.name
    os.rename(tmp_path, str(fragment_path))
    # Hash the JSON-canonical form of the SECTION payload — same format
    # Odoo uses for `last_pushed_version`, so the two hashes match on a
    # successful sync. Hashing the YAML bytes instead would give a
    # different SHA than Odoo (yaml.safe_dump emits keys in
    # insertion-order; Odoo dumps JSON with sort_keys=True).
    canon = _json.dumps(payload, sort_keys=True,
                        separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canon).hexdigest()


async def hot_reload_ac_fragment(app, kind: str) -> dict:
    """Re-read config + restart ONLY the registry owning `kind`.

    Fiscal/POS stack (printers/pinpads/scales/displays/readers/server/
    registry/iot_setup) is NEVER touched — even though we re-load the
    whole AppConfig from disk, only the AC registries get stop_all/
    from_config/start_all'd.

    Used by the `push_config` Fleet command after a fragment file has
    been rewritten on disk.
    """
    from ..config.loader import load_config

    if kind not in PUSH_CONFIG_AC_KINDS:
        raise ValueError(
            f"hot_reload_ac_fragment: unsupported kind {kind!r} "
            f"(allowed: {PUSH_CONFIG_AC_KINDS})")

    base_path = getattr(app.state.config, "_base_path", None) or "config.yaml"
    new_cfg = load_config(base_path)
    # Preserve _fragment_map / _base_path on the new object (load_config
    # already sets them, so just swap into app.state).
    app.state.config = new_cfg

    detail: dict = {}
    if kind == "cameras":
        await app.state.camera_registry.stop_all()
        new = CameraRegistry.from_config(new_cfg)
        app.state.camera_registry = new
        # MQTT ingests hold a reference to the camera registry for
        # resolving cameraId → bus. Propagate to ALL ingests
        # (multi-broker) before camera start_all so any incoming
        # message lands on the new bus.
        mqtt_reg = getattr(app.state, "mqtt_ingest_registry", None)
        if mqtt_reg is not None:
            mqtt_reg.bind(new)
        import asyncio
        await new.start_all(loop=asyncio.get_running_loop())

    elif kind == "access":
        await app.state.access_registry.stop_all()
        new = AccessRegistry.from_config(new_cfg)
        app.state.access_registry = new
        await new.start_all()

    elif kind == "biometric":
        await app.state.biometric_registry.stop_all()
        new = BiometricRegistry.from_config(new_cfg)
        app.state.biometric_registry = new
        await new.start_all()

    elif kind == "mqtt":
        # Granular per-broker reload — untouched brokers keep running.
        reg = app.state.mqtt_ingest_registry
        # Make sure camera_registry is bound (no-op if it already is).
        reg.bind(app.state.camera_registry)
        detail = reg.reload_from_config(new_cfg)

    return {"reloaded": kind, "ok": True, **({"detail": detail} if detail else {})}
