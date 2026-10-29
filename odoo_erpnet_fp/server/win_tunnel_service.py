"""Windows услуга `ErpNetFP-Tunnel` — пуска и следи cloudflared на касата.

Защо собствена обвивка, а не `cloudflared service install` (ADR
erpnet-kasi-fleet/0003): вградената услуга на cloudflared е винаги с името
`Cloudflared` — на машина, където клиентът вече ползва cloudflared, тя би
подменила ЧУЖДАТА. Нашата е с наше име, наш бинарен файл и наш токен.

    бинарен файл: <INSTDIR>\\tunnel\\cloudflared.exe
    токен:        %PROGRAMDATA%\\Odoo.ErpNet.FP\\tunnel\\token   (само администратори)
    имената:      %PROGRAMDATA%\\Odoo.ErpNet.FP\\tunnel\\names   (tunnel_dns на 127.0.0.1:53535;
                  cloudflared го пита с --dns-resolver-addrs — hosts не участва)
    лог:          %PROGRAMDATA%\\Odoo.ErpNet.FP\\logs\\tunnel.log

CLI (като администратор), както при OdooErpNetFP:
    python -m odoo_erpnet_fp.server.win_tunnel_service --startup auto install
    python -m odoo_erpnet_fp.server.win_tunnel_service remove
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
except ImportError:  # pragma: no cover — не-Windows
    sys.stderr.write("win_tunnel_service requires pywin32; only available on Windows.\n")
    raise

SERVICE_NAME = "ErpNetFP-Tunnel"
SERVICE_DISPLAY = "Odoo ErpNet.FP remote access tunnel"
SERVICE_DESCRIPTION = (
    "Runs the Odoo.ErpNet.FP copy of cloudflared for this machine's own "
    "Cloudflare tunnel (private remote access, nothing public). Does not "
    "use or change any other cloudflared installation."
)
LOG_MAX = 5 * 1024 * 1024
DNS_ADDR = ("127.0.0.1", 53535)


def _data_dir() -> Path:
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Odoo.ErpNet.FP"


def _install_dir() -> Path:
    # Под SCM sys.executable е <INSTDIR>\python\pythonservice.exe
    return Path(sys.executable).resolve().parent.parent


def _log(msg: str) -> None:
    log = _data_dir() / "logs" / "tunnel.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [service] {msg}\n")


class TunnelService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY
    _svc_description_ = SERVICE_DESCRIPTION

    def __init__(self, args):
        super().__init__(args)
        self.stop_event = win32event.CreateEvent(None, 0, 0, None)
        self.proc: subprocess.Popen | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                              servicemanager.PYS_SERVICE_STARTED, (self._svc_name_, ""))
        try:
            self._supervise()
        except Exception as exc:  # noqa: BLE001
            _log(f"crashed: {exc!r}")
            raise

    def _stopping(self, timeout_ms: int) -> bool:
        return win32event.WaitForSingleObject(self.stop_event, timeout_ms) == win32event.WAIT_OBJECT_0

    def _supervise(self) -> None:
        from odoo_erpnet_fp import tunnel_dns  # абсолютен импорт — виж win_service

        exe = _install_dir() / "tunnel" / "cloudflared.exe"
        token = _data_dir() / "tunnel" / "token"
        names = _data_dir() / "tunnel" / "names"
        log = _data_dir() / "logs" / "tunnel.log"
        try:
            tunnel_dns.serve(names, *DNS_ADDR)
            _log(f"names responder on {DNS_ADDR[0]}:{DNS_ADDR[1]} ({names})")
        except OSError as exc:
            _log(f"names responder failed on {DNS_ADDR}: {exc!r} - names will not resolve")
        backoff = 5
        while True:
            if not exe.exists() or not token.exists():
                _log(f"waiting: {exe} exists={exe.exists()}, token exists={token.exists()}")
                if self._stopping(60_000):
                    return
                continue
            if log.exists() and log.stat().st_size > LOG_MAX:
                log.replace(log.with_suffix(".log.1"))
            _log(f"starting {exe}")
            started = time.monotonic()
            with open(log, "a", encoding="utf-8") as fh:
                self.proc = subprocess.Popen(
                    [str(exe), "tunnel", "--no-autoupdate", "run", "--token-file", str(token),
                     "--dns-resolver-addrs", f"{DNS_ADDR[0]}:{DNS_ADDR[1]}"],
                    stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                while self.proc.poll() is None:
                    if self._stopping(2_000):
                        self._terminate()
                        return
            _log(f"cloudflared exited with {self.proc.returncode}")
            # Дълго живял процес ⇒ рестарт веднага; падащ на старта ⇒ все по-рядко
            backoff = 5 if time.monotonic() - started > 300 else min(backoff * 2, 300)
            if self._stopping(backoff * 1000):
                return

    def _terminate(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        _log("stopped")


def main() -> None:
    if len(sys.argv) == 1:
        servicemanager.Initialize()
        servicemanager.PrepareToHostSingle(TunnelService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(TunnelService)


if __name__ == "__main__":
    main()
