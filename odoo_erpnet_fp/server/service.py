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
    AppConfig,
    PinpadConfig,
    PrinterConfig,
    ReaderConfig,
    ScaleConfig,
)
from ..drivers.pinpad.datecs_pay import DatecsPayPinpad
from ..drivers.readers import (
    BarcodeReader,
    BarcodeScan,
    HidBarcodeReader,
    SerialBarcodeReader,
)
from ..drivers.scales import Toledo8217Scale
from .reader_bus import ReaderEventBus
from ..drivers.fiscal.datecs_isl import (
    DaisyIslDevice,
    DatecsIslDevice,
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
_ISL_DRIVERS: dict[str, type[IslDevice]] = {
    "datecs.isl": DatecsIslDevice,
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
            try:
                yield pp
            finally:
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
    "toledo_8217": Toledo8217Scale,
    # Aliases — Mettler scales using Toledo 8217 protocol
    "mettler.toledo.8217": Toledo8217Scale,
    "ariva-s": Toledo8217Scale,
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

    def make_scale(self, scale_id: str) -> Toledo8217Scale:
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
            if cfg.transport not in ("hid", "serial", "external"):
                raise ValueError(
                    f"Unknown reader transport {cfg.transport!r} on {cfg.id!r}; "
                    f"expected 'hid', 'serial', or 'external'"
                )
            bus = ReaderEventBus(reader_id=cfg.id, webhooks=cfg.webhooks)
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

    @staticmethod
    def _make_driver(cfg: ReaderConfig) -> BarcodeReader:
        if cfg.transport == "hid":
            if not cfg.device_path:
                raise ValueError(
                    f"HID reader {cfg.id!r} needs `device_path` (e.g. /dev/input/eventN)"
                )
            return HidBarcodeReader(
                reader_id=cfg.id,
                device_path=cfg.device_path,
                grab=cfg.grab,
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
