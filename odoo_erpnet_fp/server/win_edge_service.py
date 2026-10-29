"""Windows услуга `ErpNetFP-Edge` — Traefik пред касата + certpull (ADR erpnet-kasi-fleet/0005).

Същото като `packaging/edge/install-edge.sh` на Linux:
    браузърът на касата ── https://<каса>.<клиент>.erpnet.mcpworks.net (→ 127.0.0.1, наш блок в hosts)
    ние през WARP ── маршрут по име → тунелът на машината (ErpNetFP-Tunnel) → tunnel_dns: `self`
        ▼ Traefik на :443 — само 127.0.0.1 и собствените адреси (middleware only-local)
        ├─ /pinpads/…  → устройството (APK-то на BlueCash)
        └─ останалото  → проксито

    бинарен файл: <INSTDIR>\\edge\\traefik.exe
    настройката:  %PROGRAMDATA%\\Odoo.ErpNet.FP\\edge\\dynamic\\{routes,tls,allow}.yml
    сертификатът: %PROGRAMDATA%\\Odoo.ErpNet.FP\\edge\\certs\\wildcard.{crt,key}
    certpull:     %PROGRAMDATA%\\Odoo.ErpNet.FP\\edge\\certpull.env (+ тайните до него)
    лог:          %PROGRAMDATA%\\Odoo.ErpNet.FP\\logs\\edge.log

Наша услуга с наше име (ADR-0003): чужд Traefik/IIS на машината не се пипа.

    python -m odoo_erpnet_fp.server.win_edge_service --startup auto install
    python -m odoo_erpnet_fp.server.win_edge_service remove
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

try:
    import servicemanager
    import win32event
    import win32service
    import win32serviceutil
except ImportError:  # pragma: no cover — не-Windows
    sys.stderr.write("win_edge_service requires pywin32; only available on Windows.\n")
    raise

SERVICE_NAME = "ErpNetFP-Edge"
SERVICE_DISPLAY = "Odoo ErpNet.FP edge (https in front of the till)"
SERVICE_DESCRIPTION = (
    "Runs the Odoo.ErpNet.FP copy of Traefik on :443 for this machine's private "
    "name and keeps its certificate fresh from the certificate server. Accepts "
    "only this machine and its own tunnel; the shop LAN gets 403."
)
LOG_MAX = 5 * 1024 * 1024


def _data_dir() -> Path:
    return Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Odoo.ErpNet.FP"


def _install_dir() -> Path:
    # Под SCM sys.executable е <INSTDIR>\python\pythonservice.exe
    return Path(sys.executable).resolve().parent.parent


def _log(msg: str) -> None:
    log = _data_dir() / "logs" / "edge.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a", encoding="utf-8") as fh:
        fh.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [service] {msg}\n")


class EdgeService(win32serviceutil.ServiceFramework):
    _svc_name_ = SERVICE_NAME
    _svc_display_name_ = SERVICE_DISPLAY
    _svc_description_ = SERVICE_DESCRIPTION

    def __init__(self, args):
        super().__init__(args)
        # Ръчно нулиране: чакат го ДВЕ нишки (Traefik и certpull). С авто-нулиране
        # го взима едната и другата не разбира — услугата засядаше в StopPending.
        self.stop_event = win32event.CreateEvent(None, 1, 0, None)
        self.proc: subprocess.Popen | None = None

    def SvcStop(self):
        self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
        win32event.SetEvent(self.stop_event)

    def SvcDoRun(self):
        servicemanager.LogMsg(servicemanager.EVENTLOG_INFORMATION_TYPE,
                              servicemanager.PYS_SERVICE_STARTED, (self._svc_name_, ""))
        threading.Thread(target=self._certpull, daemon=True, name="certpull").start()
        try:
            self._supervise()
        except Exception as exc:  # noqa: BLE001
            _log(f"crashed: {exc!r}")
            raise

    def _stopping(self, timeout_ms: int) -> bool:
        return win32event.WaitForSingleObject(self.stop_event, timeout_ms) == win32event.WAIT_OBJECT_0

    def _certpull(self) -> None:
        """Сертификатът на всеки 6 ч., адресът (DHCP) — всяка минута; виж certpull.main."""
        from odoo_erpnet_fp import certpull  # абсолютен импорт — виж win_service

        env_file = _data_dir() / "edge" / "certpull.env"
        next_pull = 0.0
        while not self._stopping(0):
            try:
                env = certpull.load_env(str(env_file))
                allow = env.get("CERTPULL_TRAEFIK_ALLOW")
                if allow and certpull.write_traefik_allow(Path(allow), certpull.own_ipv4()):
                    _log("only-local: " + ", ".join(["127.0.0.1"] + certpull.own_ipv4()))
                if time.time() >= next_pull:
                    rc = certpull.once(env)
                    next_pull = time.time() + (int(env.get("CERTPULL_EVERY_S", "21600")) if rc == 0 else certpull.RETRY_S)
            except Exception as exc:  # noqa: BLE001
                _log(f"certpull: {exc!r}")
                next_pull = time.time() + certpull.RETRY_S
            if self._stopping(60_000):
                return

    def _supervise(self) -> None:
        exe = _install_dir() / "edge" / "traefik.exe"
        dyn = _data_dir() / "edge" / "dynamic"
        log = _data_dir() / "logs" / "edge.log"
        backoff = 5
        while True:
            if not exe.exists() or not (dyn / "routes.yml").exists():
                _log(f"waiting: {exe} exists={exe.exists()}, routes exists={(dyn / 'routes.yml').exists()}")
                if self._stopping(60_000):
                    return
                continue
            if log.exists() and log.stat().st_size > LOG_MAX:
                log.replace(log.with_suffix(".log.1"))
            _log(f"starting {exe}")
            started = time.monotonic()
            with open(log, "a", encoding="utf-8") as fh:
                self.proc = subprocess.Popen(
                    [str(exe), "--entrypoints.websecure.address=:443",
                     f"--providers.file.directory={dyn}", "--providers.file.watch=true",
                     "--log.level=INFO"],
                    stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
                while self.proc.poll() is None:
                    if self._stopping(2_000):
                        self._terminate()
                        return
            _log(f"traefik exited with {self.proc.returncode}")
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
        servicemanager.PrepareToHostSingle(EdgeService)
        servicemanager.StartServiceCtrlDispatcher()
    else:
        win32serviceutil.HandleCommandLine(EdgeService)


if __name__ == "__main__":
    main()
