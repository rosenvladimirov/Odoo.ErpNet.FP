"""Записване на касата във флота (ADR erpnet-kasi-fleet/0001–0004).

Кодът за записване се прави на лаптопа и носи САМО тайната на собствения
тунел на касата — никакъв API токен на акаунта:

    EFP1.<base64url(zlib(JSON))>
    {"v":1, "c":клиент, "k":каса, "h":име, "t":tunnel_id, "tok":токен на тунела,
     "d":{устройство: LAN IP}, "ssh":[публични ключове],
     "crt":тайната за cert-issuer, "cu":адресът му, "e":{"n":име пред касата, "p":пинпадът},
     "fp":{"id","driver","usb":"VID:PID","op","pw"} — апаратът, "cors":[Odoo],
     "reg":"<Odoo с флота>"}  (setup_fiscal; ADR erpnet-kasi-fleet/0006)
    (последните три — по избор, ADR erpnet-kasi-fleet/0005: пред касата е Traefik)

`apply` прави на касата само НАШИТЕ неща и е идемпотентно (ADR-0003):
  - токенът → файл, четим само от администратора/root;
  - имената → НАШ файл tunnel/names, който чете tunnel_dns (cloudflared пита
    него с --dns-resolver-addrs). Касата е `self` — текущият ѝ LAN адрес.
    hosts на машината НЕ се пипа: cloudflared не го чете по този път
    (измерено 05.10); блок, оставен от по-ранна версия, се маха;
  - ключовете → редове с белег `erpnet-fp:` в authorized_keys;
  - следа без тайни → enroll.json.
`remove` маха точно тях и нищо друго.

CLI:
    python -m odoo_erpnet_fp.enroll apply      # кодът от ERPNET_ENROLL_CODE или --code
    python -m odoo_erpnet_fp.enroll remove
    python -m odoo_erpnet_fp.enroll show       # декодира без тайните
    python -m odoo_erpnet_fp.enroll encode ... # на лаптопа — виж --help
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import zlib
from datetime import datetime, timezone
from pathlib import Path

PREFIX = "EFP1."
BEGIN = "# BEGIN erpnet-fp (managed by Odoo.ErpNet.FP - do not edit inside)"
END = "# END erpnet-fp"
KEY_MARK = "erpnet-fp:"
# Над NSIS_MAX_STRLEN (1024) кодът не минава през полето на инсталатора
MAX_CODE_LEN = 1000

IS_WINDOWS = os.name == "nt"


class EnrollError(Exception):
    pass


# ─── Кодът ────────────────────────────────────────────────────────────

def encode(data: dict) -> str:
    raw = json.dumps(data, separators=(",", ":"), ensure_ascii=True).encode()
    code = PREFIX + base64.urlsafe_b64encode(zlib.compress(raw, 9)).decode().rstrip("=")
    if len(code) > MAX_CODE_LEN:
        raise EnrollError(f"enrollment code is {len(code)} chars (max {MAX_CODE_LEN}); use fewer/shorter SSH keys")
    return code


def decode(code: str) -> dict:
    code = "".join(code.split())  # пренесен на няколко реда при копиране
    if not code.startswith(PREFIX):
        raise EnrollError("not an Odoo.ErpNet.FP enrollment code (must start with EFP1.)")
    body = code[len(PREFIX):]
    try:
        raw = zlib.decompress(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        raise EnrollError(f"enrollment code is damaged: {exc}") from exc
    if data.get("v") != 1:
        raise EnrollError(f"unsupported enrollment code version {data.get('v')!r}")
    for key in ("h", "t", "tok"):
        if not data.get(key):
            raise EnrollError(f"enrollment code has no {key!r}")
    if not re.fullmatch(r"[a-z0-9]([a-z0-9.-]{0,251}[a-z0-9])?", data["h"]):
        raise EnrollError(f"bad hostname {data['h']!r}")
    return data


def host_entries(data: dict) -> list[tuple[str, str]]:
    """Имената, които cloudflared на касата трябва да резолвне. Касата е
    `self` (не 127.0.0.1 — loopback Gateway връща на клиента непроменен)."""
    entries = [(data["h"], "self")]
    for name, ip in sorted((data.get("d") or {}).items()):
        if not re.fullmatch(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", name):
            raise EnrollError(f"bad device name {name!r}")
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", ip):
            raise EnrollError(f"bad device IP {ip!r} for {name!r}")
        entries.append((f"{name}.{data['h']}", ip))
    return entries


# ─── hosts: само нашият блок ───────────────────────────────────────────

def _read_text(path: Path) -> tuple[str, str]:
    """Съдържание + разделител на редовете (CRLF на Windows се пази)."""
    if not path.exists():
        return "", ("\r\n" if IS_WINDOWS else "\n")
    raw = path.read_bytes()
    text = raw.decode("utf-8", errors="surrogateescape")
    nl = "\r\n" if "\r\n" in text else "\n"
    return text, nl


def _write_text(path: Path, text: str) -> None:
    path.write_bytes(text.encode("utf-8", errors="surrogateescape"))


def _strip_block(lines: list[str]) -> list[str]:
    out, inside = [], False
    for line in lines:
        s = line.strip()
        # Само НАШИЯТ блок — не и „# BEGIN erpnet-fp-edge“ на Traefik пред касата.
        if s == BEGIN or s == "# BEGIN erpnet-fp":
            inside = True
            continue
        if inside and s == END:
            inside = False
            continue
        if not inside:
            out.append(line)
    return out


def set_hosts_block(path: Path, entries: list[tuple[str, str]]) -> bool:
    """Подменя (или добавя) нашия блок; всичко извън него остава байт по байт.
    Не се ползва от apply (виж горе) — остава за ръчни случаи и тестове."""
    text, nl = _read_text(path)
    lines = text.splitlines()
    kept = _strip_block(lines)
    while kept and not kept[-1].strip():
        kept.pop()
    block = [BEGIN] + [f"{ip}\t{name}" for name, ip in entries] + [END]
    new = nl.join(kept + ([""] if kept else []) + block) + nl
    if new == text:
        return False
    backup = path.with_name(path.name + ".erpnet-fp.bak")
    if path.exists() and not backup.exists():
        _write_text(backup, text)
    _write_text(path, new)
    return True


def write_names(path: Path, entries: list[tuple[str, str]]) -> bool:
    """Нашият файл с имената (целият е наш). True, ако е променен."""
    new = "# Odoo.ErpNet.FP tunnel names (managed by enroll)\n" + "".join(
        f"{ip} {name}\n" for name, ip in entries)
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if new == old:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new, encoding="utf-8")
    return True


def remove_hosts_block(path: Path) -> bool:
    text, nl = _read_text(path)
    if BEGIN.split(" (")[0] not in text:
        return False
    lines = text.splitlines()
    kept = _strip_block(lines)
    if kept == lines:
        return False  # нашият блок го няма (напр. само „erpnet-fp-edge“) — файлът не се пипа
    while kept and not kept[-1].strip():
        kept.pop()
    _write_text(path, nl.join(kept) + (nl if kept else ""))
    return True


# ─── SSH ключове: само нашите редове ───────────────────────────────────

def _key_line(key: str, label: str) -> str:
    parts = key.split()
    if len(parts) < 2 or not re.fullmatch(r"(ssh-(ed25519|rsa)|ecdsa-sha2-nistp(256|384|521)|sk-\S+)", parts[0]):
        raise EnrollError(f"not an OpenSSH public key: {key[:30]!r}…")
    return f"{parts[0]} {parts[1]} {KEY_MARK}{label}"


def set_keys(path: Path, keys: list[str], label: str) -> bool:
    text, nl = _read_text(path)
    lines = [ln for ln in text.splitlines() if KEY_MARK not in ln]
    while lines and not lines[-1].strip():
        lines.pop()
    lines += [_key_line(k, label) for k in keys]
    new = nl.join(lines) + nl if lines else ""
    if new == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_text(path, new)
    return True


def remove_keys(path: Path) -> bool:
    text, nl = _read_text(path)
    if KEY_MARK not in text:
        return False
    lines = [ln for ln in text.splitlines() if KEY_MARK not in ln]
    _write_text(path, nl.join(lines) + nl if lines else "")
    return True


# ─── Пътищата по ОС ───────────────────────────────────────────────────

def paths() -> dict[str, Path]:
    if IS_WINDOWS:
        pd = Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData"))
        root = pd / "Odoo.ErpNet.FP"
        return {
            "state": root / "enroll.json",
            "token": root / "tunnel" / "token",
            "names": root / "tunnel" / "names",
            # само за махане на блок от по-ранна версия
            "hosts": Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "drivers" / "etc" / "hosts",
            "keys": pd / "ssh" / "administrators_authorized_keys",
            "edge": root / "edge",
        }
    root = Path(os.environ.get("ERPNET_FP_HOME", "/opt/erpnet-fp"))
    return {
        "state": root / "enroll.json",
        "token": root / "tunnel" / "token",
        "names": root / "tunnel" / "names",
        "hosts": Path("/etc/hosts"),  # само за махане на стар блок; нов не се пише
        "keys": Path(os.environ.get("ERPNET_FP_KEYS", "/root/.ssh/authorized_keys")),
        "edge": Path(os.environ.get("ERPNET_FP_ETC", "/etc/odoo-erpnet-fp")),
    }


def _restrict(path: Path) -> None:
    """Само администраторите/SYSTEM (Windows) или собственикът (Linux)."""
    if IS_WINDOWS:
        subprocess.run(["icacls", str(path), "/inheritance:r",
                        "/grant", "*S-1-5-32-544:F", "/grant", "*S-1-5-18:F"],
                       check=True, capture_output=True)
    else:
        os.chmod(path, 0o600)


def _write_secret(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        path.touch()
    _restrict(path)
    path.write_text(value, encoding="ascii")


# ─── apply / remove ───────────────────────────────────────────────────

def apply(code: str, p: dict[str, Path] | None = None) -> dict:
    p = p or paths()
    data = decode(code)
    entries = host_entries(data)
    _write_secret(p["token"], data["tok"])
    hosts_changed = write_names(p["names"], entries)
    remove_hosts_block(p["hosts"])  # блок от по-ранна версия, ако има
    keys = data.get("ssh") or []
    keys_changed = False
    if keys:
        keys_changed = set_keys(p["keys"], keys, data.get("c") or "rosen")
        if IS_WINDOWS:
            _restrict(p["keys"])  # иначе sshd на Windows отказва файла
    edge = None
    if data.get("crt"):
        # Сертификатът пред касата (Traefik + certpull) — тайната отделно, 0600/админи;
        # в edge.json само каквото не е тайна. Инсталаторът (install-edge) чете оттам.
        _write_secret(p["edge"] / "certpull.secret", data["crt"])
        e = data.get("e") or {}
        edge = {"name": e.get("n") or data["h"], "pinpad": e.get("p") or "", "cert_url": data.get("cu") or ""}
    if data.get("fp") or data.get("cors") or data.get("reg"):
        edge = edge or {}
        edge["fiscal"] = data.get("fp") or None
        edge["cors"] = data.get("cors") or []
        if data.get("reg"):
            # флотът: оттам идват и CORS адресите (ADR erpnet-kasi-fleet/0006)
            edge["registry"] = {"url": data["reg"], "name": data["h"]}
    if edge is not None:
        p["edge"].mkdir(parents=True, exist_ok=True)
        (p["edge"] / "edge.json").write_text(json.dumps(edge, indent=2), encoding="utf-8")
    state = {
        "enrolled_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "client": data.get("c"), "kasa": data.get("k"),
        "hostname": data["h"], "tunnel_id": data["t"],
        "names": [f"{ip} {name}" for name, ip in entries],
        "ssh_keys": len(keys),
        "edge": edge,
    }
    p["state"].parent.mkdir(parents=True, exist_ok=True)
    p["state"].write_text(json.dumps(state, indent=2), encoding="utf-8")
    state["hosts_changed"], state["keys_changed"] = hosts_changed, keys_changed
    return state


def remove(p: dict[str, Path] | None = None) -> list[str]:
    p = p or paths()
    done = []
    if remove_hosts_block(p["hosts"]):
        done.append(f"names removed from {p['hosts']}")
    if remove_keys(p["keys"]):
        done.append(f"erpnet-fp keys removed from {p['keys']}")
    for key in ("token", "names", "state"):
        if p[key].exists():
            p[key].unlink()
            done.append(f"deleted {p[key]}")
    return done


# ─── CLI ──────────────────────────────────────────────────────────────

def _cli(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m odoo_erpnet_fp.enroll")
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("apply", help="enroll this machine (code from --code or ERPNET_ENROLL_CODE)")
    a.add_argument("--code")
    sub.add_parser("remove", help="remove everything enroll added")
    s = sub.add_parser("show", help="decode a code without printing secrets")
    s.add_argument("--code")
    e = sub.add_parser("encode", help="build a code (laptop side)")
    e.add_argument("--client", required=True)
    e.add_argument("--kasa", required=True)
    e.add_argument("--hostname", required=True)
    e.add_argument("--tunnel-id", required=True)
    e.add_argument("--token-file", required=True, type=Path)
    e.add_argument("--device", action="append", default=[], metavar="NAME=IP")
    e.add_argument("--ssh-key-file", action="append", default=[], type=Path)
    e.add_argument("--cert-secret-file", type=Path, help="тайната на машината в cert-issuer")
    e.add_argument("--cert-url", default="https://erpnet-certs.mcpworks.net")
    e.add_argument("--edge-name", help="името пред касата (по подразбиране --hostname)")
    e.add_argument("--edge-pinpad", default="", help="накъде отиват /pinpads (празно = проксито)")
    e.add_argument("--fiscal", metavar="ID:DRIVER:VID:PID:OP:PW", help="апаратът, напр. daisy:daisy.icp:0483:5740:1:1")
    e.add_argument("--cors", action="append", default=[], metavar="ORIGIN", help="Odoo-то, напр. https://x.odoo.com")
    e.add_argument("--registry", metavar="URL", help="Odoo с флота (l10n_bg_erp_net_fp_fleet), напр. https://x.odoo.com")
    args = ap.parse_args(argv)

    try:
        if args.cmd == "apply":
            code = args.code or os.environ.get("ERPNET_ENROLL_CODE", "")
            if not code:
                raise EnrollError("no enrollment code (use --code or ERPNET_ENROLL_CODE)")
            st = apply(code)
            print(f"[enroll] {st['hostname']} (tunnel {st['tunnel_id']})")
            for n in st["names"]:
                print(f"[enroll]   name {n}")
            print(f"[enroll] hosts {'updated' if st['hosts_changed'] else 'unchanged'}, "
                  f"ssh keys {st['ssh_keys']} ({'updated' if st['keys_changed'] else 'unchanged'})")
        elif args.cmd == "remove":
            for line in remove() or ["nothing to remove"]:
                print(f"[enroll] {line}")
        elif args.cmd == "show":
            d = decode(args.code or os.environ.get("ERPNET_ENROLL_CODE", ""))
            d["tok"] = d["tok"][:6] + "…"
            if d.get("crt"):
                d["crt"] = d["crt"][:4] + "…"
            print(json.dumps(d, indent=2))
        elif args.cmd == "encode":
            devices = dict(x.split("=", 1) for x in args.device)
            keys = [f.read_text().strip() for f in args.ssh_key_file]
            data = {"v": 1, "c": args.client, "k": args.kasa, "h": args.hostname,
                    "t": args.tunnel_id, "tok": args.token_file.read_text().strip(),
                    "d": devices, "ssh": keys}
            if args.cert_secret_file:
                data["crt"] = args.cert_secret_file.read_text().strip()
                data["cu"] = args.cert_url
                data["e"] = {"n": args.edge_name or args.hostname, "p": args.edge_pinpad}
            if args.fiscal:
                fid, drv, vid, pid, op, pw = args.fiscal.split(":")
                data["fp"] = {"id": fid, "driver": drv, "usb": f"{vid}:{pid}", "op": op, "pw": pw}
            if args.cors:
                data["cors"] = args.cors
            if args.registry:
                data["reg"] = args.registry.rstrip("/")
            host_entries(data)
            print(encode(data))
    except EnrollError as exc:
        print(f"[enroll] ERROR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
