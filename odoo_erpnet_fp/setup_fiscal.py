"""Първа настройка на касата от кода за записване: фискалният апарат и CORS.

Кодът `EFP1.` (enroll) може да носи и:
    "fp":   {"id": "daisy", "driver": "daisy.icp", "usb": "0483:5740", "op": "1", "pw": "1"}
    "cors": ["https://<база>.odoo.com"]
enroll ги оставя в `<edge>/edge.json`; тук:
  - COM портът се намира по USB идентификатора (pyserial) — човекът не търси
    номера в Device Manager; ако апаратът не е включен, не се пише нищо и
    това се казва;
  - в config.yaml се вписва апаратът (ако такъв `id` още няма) и Odoo-то в
    `server.cors_origins` — като ТЕКСТ, коментарите и чуждото остават; YAML-ът
    се проверява преди запис, а старият файл остава до него (.bak-…).

    python -m odoo_erpnet_fp.setup_fiscal --from-enroll [--config <път>] [--edge <папка>]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import yaml

IS_WINDOWS = os.name == "nt"


def default_paths() -> tuple[Path, Path]:
    if IS_WINDOWS:
        root = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / "Odoo.ErpNet.FP"
        return root / "config.yaml", root / "edge"
    return Path("/etc/odoo-erpnet-fp/config.yaml"), Path(os.environ.get("ERPNET_FP_ETC", "/etc/odoo-erpnet-fp"))


def find_port(usb: str, ports=None) -> str | None:
    """`VID:PID` (hex) → COMx / /dev/tty… на първия такъв апарат."""
    vid, pid = (int(x, 16) for x in usb.split(":"))
    if ports is None:
        from serial.tools import list_ports
        ports = list_ports.comports()
    for p in sorted(ports, key=lambda p: p.device):
        if p.vid == vid and p.pid == pid:
            return p.device
    return None


def add_printer(text: str, fp: dict, port: str) -> str:
    """Вписва апарата под `printers:`, ако такъв id още няма."""
    data = yaml.safe_load(text) or {}
    if any(isinstance(p, dict) and p.get("id") == fp["id"] for p in (data.get("printers") or [])):
        return text
    entry = (f"  - id: {fp['id']}\n"
             f"    driver: {fp['driver']}\n"
             f"    transport: serial\n"
             f"    port: \"{port}\"\n"
             f"    baudrate: {int(fp.get('baud', 115200))}\n"
             f"    operator: \"{fp.get('op', '1')}\"\n"
             f"    operator_password: \"{fp.get('pw', '1')}\"\n"
             f"    till_number: 1\n")
    m = re.search(r"(?m)^printers:[ \t]*(\[\])?[ \t]*$", text)
    if m:
        return text[:m.start()] + "printers:\n" + entry + text[m.end() + 1:]
    return text.rstrip("\n") + "\n\nprinters:\n" + entry


def add_cors(text: str, origins: list[str]) -> str:
    """`server.cors_origins` — добавя липсващите, не пипа останалото."""
    if not origins:
        return text
    data = yaml.safe_load(text) or {}
    have = list((data.get("server") or {}).get("cors_origins") or [])
    missing = [o for o in origins if o not in have]
    if not missing:
        return text
    lines = "".join(f"    - \"{o}\"\n" for o in missing)
    m = re.search(r"(?m)^  cors_origins:[ \t]*\n", text)
    if m:
        return text[:m.end()] + lines + text[m.end():]
    m = re.search(r"(?m)^server:[ \t]*\n", text)
    if not m:
        return text.rstrip("\n") + "\n\nserver:\n  cors_origins:\n" + lines
    return text[:m.end()] + "  cors_origins:\n" + lines + text[m.end():]


def configure(config: Path, edge: dict, ports=None) -> list[str]:
    done: list[str] = []
    text = config.read_text(encoding="utf-8") if config.exists() else ""
    new = add_cors(text, edge.get("cors") or [])
    fp = edge.get("fiscal")
    if fp:
        port = find_port(fp["usb"], ports)
        if port:
            new = add_printer(new, fp, port)
            done.append(f"fiscal device {fp['id']} ({fp['driver']}) on {port}")
        else:
            done.append(f"fiscal device {fp['id']}: USB {fp['usb']} not found - plug it in and rerun")
    if new == text:
        return done + ["config.yaml unchanged"]
    yaml.safe_load(new)  # счупен YAML не се записва
    if config.exists():
        config.with_name(config.name + time.strftime(".bak-%Y%m%d-%H%M%S")).write_text(text, encoding="utf-8")
    config.write_text(new, encoding="utf-8")
    return done + [f"config.yaml updated ({config})"]


def main(argv=None) -> int:
    cfg, edge_dir = default_paths()
    ap = argparse.ArgumentParser(prog="python -m odoo_erpnet_fp.setup_fiscal")
    ap.add_argument("--from-enroll", action="store_true", required=True)
    ap.add_argument("--config", type=Path, default=cfg)
    ap.add_argument("--edge", type=Path, default=edge_dir)
    a = ap.parse_args(argv)
    f = a.edge / "edge.json"
    if not f.exists():
        print(f"[setup] no {f} - nothing to do")
        return 0
    try:
        for line in configure(a.config, json.loads(f.read_text(encoding="utf-8"))):
            print(f"[setup] {line}")
    except Exception as exc:  # noqa: BLE001
        print(f"[setup] ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
