"""Windows service wrapper for Odoo.ErpNet.FP.

Registers the proxy as a regular Windows service that starts at boot,
runs in the background, and is controllable via Services.msc / sc.exe.

Service identity:
    Name:         OdooErpNetFP
    DisplayName:  Odoo ErpNet.FP fiscal printer proxy
    StartType:    Automatic
    Account:      LocalSystem (has access to COM ports + USB)

Lifecycle:
    SvcDoRun  → loads config from %PROGRAMDATA%\\Odoo.ErpNet.FP\\config.yaml,
                builds the FastAPI app, runs uvicorn in the same process.
    SvcStop   → flips a stop flag; uvicorn's signal handler (or our
                explicit shutdown) terminates the worker loop and we
                exit cleanly within the SCM stop timeout (30 s default).

Logging:
    Service redirects stdout/stderr to %PROGRAMDATA%\\Odoo.ErpNet.FP\\logs\\service.log
    so anything uvicorn / fastapi / our drivers print is captured even
    when the service has no console.

CLI usage (run as Administrator):
    python -m odoo_erpnet_fp.server.win_service install
    python -m odoo_erpnet_fp.server.win_service start
    python -m odoo_erpnet_fp.server.win_service stop
    python -m odoo_erpnet_fp.server.win_service remove
    python -m odoo_erpnet_fp.server.win_service debug   :: foreground for dev

The NSIS installer calls `install + start` automatically; the
uninstaller calls `stop + remove`. End users normally never run this
module directly.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional

# pywin32 — only available on Windows
try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
except ImportError:  # pragma: no cover — non-Windows host
    sys.stderr.write(
        "win_service requires pywin32; only available on Windows.\n"
    )
    raise


SERVICE_NAME = "OdooErpNetFP"
SERVICE_DISPLAY = "Odoo ErpNet.FP fiscal printer proxy"
SERVICE_DESCRIPTION = (
    "Python drop-in replacement for the C# ErpNet.FP HTTP fiscal-printer "
    "server. Drives Datecs / Tremol / Eltrade fiscal printers, customer "
    "displays, scales, and HID barcode readers from POS / industrial "
    "applications via REST + WebSocket. Bulgarian-market focused."
)


def _config_dir() -> Path:
    """%PROGRAMDATA%\\Odoo.ErpNet.FP\\ — created at install time, NOT
    deleted on uninstall (admin's data)."""
    pd = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
    return Path(pd) / "Odoo.ErpNet.FP"


def _config_path() -> Path:
    return _config_dir() / "config.yaml"


def _log_path() -> Path:
    return _config_dir() / "logs" / "service.log"


def _redirect_to_logfile() -> None:
    """The service has no console — pywin32 closes stdin/stdout/stderr
    by default. We point them at a file so uvicorn / loggers don't
    silently swallow output."""
    log = _log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "a", buffering=1, encoding="utf-8")
    sys.stdout = fh
    sys.stderr = fh
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=fh,
    )


class ErpNetFpService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY
    _svc_description_ = SERVICE_DESCRIPTION

    def __init__(self, args):
        super().__init__(args)
        # Auto-reset event the SCM signals via SvcStop().
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self._uvicorn_thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._server = None  # uvicorn.Server instance once started

    # ─── SCM control hooks ──────────────────────────────────────────

    def SvcStop(self):
        """Called by SCM when 'stop' is requested. Returns quickly; the
        actual shutdown completes in the worker thread."""
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        # Tell uvicorn to begin graceful shutdown.
        if self._server is not None:
            self._server.should_exit = True
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        """SCM 'start' handler — must block until shutdown."""
        servicemanager.LogMsg(
            servicemanager.EVENTLOG_INFORMATION_TYPE,
            servicemanager.PYS_SERVICE_STARTED,
            (self._svc_name_, ""),
        )
        try:
            _redirect_to_logfile()
            self._serve_forever()
        except Exception:  # noqa: BLE001
            logging.exception("Service run failed")
            servicemanager.LogErrorMsg(
                f"Service {self._svc_name_} crashed — see {_log_path()}"
            )
            raise

    # ─── Actual work ────────────────────────────────────────────────

    def _serve_forever(self) -> None:
        """Load config, build app, run uvicorn in this same thread."""
        from ..config.loader import load_config
        from .main import create_app

        cfg_path = _config_path()
        if not cfg_path.exists():
            raise FileNotFoundError(
                f"Config not found at {cfg_path}. Reinstall or copy a "
                f"sample from config-examples/config-windows.example.yaml"
            )
        config = load_config(cfg_path)
        app = create_app(config)

        import uvicorn
        uvicorn_cfg = uvicorn.Config(
            app,
            host=config.server.host,
            port=config.server.port,
            log_level=config.server.log_level.lower(),
            # Service has no console — disable colored output etc.
            access_log=True,
            use_colors=False,
            # Let SvcStop set should_exit; uvicorn handles its own loop.
            ssl_certfile=config.server.tls.certfile if config.server.tls.enabled else None,
            ssl_keyfile=config.server.tls.keyfile if config.server.tls.enabled else None,
        )
        self._server = uvicorn.Server(uvicorn_cfg)

        logging.info(
            "ErpNet.FP service starting on %s:%s (TLS=%s, config=%s)",
            config.server.host, config.server.port,
            config.server.tls.enabled, cfg_path,
        )
        # Run uvicorn synchronously in this thread — SvcDoRun blocks
        # until this returns. SvcStop flips should_exit which causes
        # uvicorn.serve() to unwind cleanly.
        self._server.run()
        logging.info("ErpNet.FP service exited cleanly")


def main() -> None:
    """Entry point for `python -m odoo_erpnet_fp.server.win_service`.

    Without args → SCM dispatcher (used when SCM starts the service).
    With args (install / start / stop / remove / debug) →
    HandleCommandLine, the standard pywin32 service-control CLI.
    """
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(ErpNetFpService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(ErpNetFpService)


if __name__ == "__main__":
    main()
