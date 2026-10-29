"""Тегли сертификата на машината от cert-issuer и го слага пред устройствата (ADR erpnet-kasi-fleet/0005).

Сървърът (cert-issuer) държи един `*.erpnet.mcpworks.net` и сам го подновява;
машината само го дърпа: `GET <url>/v1/cert` с тайната си (Bearer) и — когато
името е зад Cloudflare Access — с ключа за услуга (CF-Access-Client-Id/Secret).
Нов сериен номер ⇒ сертификатът и ключът се записват атомарно в папката, която
Traefik гледа (file provider с watch), и той ги поема без рестарт.

Само стандартната библиотека — върви и на хоста (Linux .deb), и в услугата на
Windows, и в контейнер.

    python -m odoo_erpnet_fp.certpull --env /etc/odoo-erpnet-fp/certpull.env [--once]

Env (или файлът от --env, ред `KEY=VALUE`):
    CERTPULL_URL            https://<сървърът>        (задължително)
    CERTPULL_SECRET_FILE    файл с тайната на машината (0600)
    CERTPULL_CF_ID / CERTPULL_CF_SECRET_FILE   ключът за услуга на Cloudflare Access (по избор)
    CERTPULL_DIR            /var/lib/odoo-erpnet-fp/certs
    CERTPULL_EVERY_S        21600 (6 ч.); при грешка — след 300 s
    CERTPULL_TRAEFIK_TLS    динамичната TLS настройка на Traefik, която certpull пише след
                            смяна (той презарежда сертификатите само при нейна промяна);
                            докато сертификат няма, там е празна и Traefik върви със своя
    CERTPULL_TRAEFIK_CERTS  пътят до сертификатите, както ги вижда Traefik (/certs)
    CERTPULL_TRAEFIK_MODE   default (сертификатът по подразбиране — наш Traefik) или
                            certificates (един ред в tls.certificates — чужд Traefik със
                            свои сертификати; файлът е само наш, неговите не се пипат)
    CERTPULL_TRAEFIK_ALLOW  middleware-ът „only-local“ на Traefik: пуска 127.0.0.1 и
                            собствените адреси на машината (оттам идва cloudflared на
                            тунела ѝ); LAN-ът — 403. Обновява се при смяна на адреса (DHCP).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

log = logging.getLogger("erpnet.certpull")

DEFAULT_DIR = "/var/lib/odoo-erpnet-fp/certs"
RETRY_S = 300


def load_env(path: str | None) -> dict:
    env = dict(os.environ)
    if path:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def _read_secret(env: dict, file_key: str, value_key: str) -> str:
    if env.get(file_key):
        return Path(env[file_key]).read_text().strip()
    return env.get(value_key, "").strip()


def fetch(env: dict, timeout: float = 30) -> tuple[int, dict]:
    url = env["CERTPULL_URL"].rstrip("/") + "/v1/cert"
    headers = {"Authorization": "Bearer " + _read_secret(env, "CERTPULL_SECRET_FILE", "CERTPULL_SECRET"),
               "User-Agent": "odoo-erpnet-fp-certpull/1"}
    cf_id = env.get("CERTPULL_CF_ID", "").strip()
    if cf_id:
        headers["CF-Access-Client-Id"] = cf_id
        headers["CF-Access-Client-Secret"] = _read_secret(env, "CERTPULL_CF_SECRET_FILE", "CERTPULL_CF_SECRET")
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read())
        except Exception:  # noqa: BLE001
            body = {"detail": f"HTTP {e.code}"}
        return e.code, body


def _write(path: Path, data: str, mode: int) -> None:
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
    with os.fdopen(fd, "w") as f:
        f.write(data)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def install(cert: dict, directory: Path) -> bool:
    """Записва при нов сериен номер. True — сменен."""
    directory.mkdir(parents=True, exist_ok=True)
    state_p = directory / "state.json"
    try:
        state = json.loads(state_p.read_text())
    except (FileNotFoundError, ValueError):
        state = {}
    extra = cert.get("extra")  # допълнителните (публични) имена — само на тази машина
    if (state.get("serial") == cert["serial"] and (directory / "wildcard.crt").exists()
            and state.get("extra_serial") == (extra or {}).get("serial")):
        return False
    for c in [cert] + ([extra] if extra else []):
        if not c.get("chain", "").startswith("-----BEGIN CERTIFICATE-----") or "PRIVATE KEY-----" not in c.get("key", ""):
            raise ValueError("the server returned something that is not a certificate and a key")
    # Ключът първо: Traefik гледа папката и не бива да види нов сертификат със стар ключ.
    _write(directory / "wildcard.key", cert["key"], 0o600)
    _write(directory / "wildcard.crt", cert["chain"], 0o644)
    if extra:
        _write(directory / "extra.key", extra["key"], 0o600)
        _write(directory / "extra.crt", extra["chain"], 0o644)
    _write(directory / "state.json", json.dumps({
        "serial": cert["serial"], "notAfter": cert["notAfter"], "names": cert.get("names", []),
        "extra_serial": (extra or {}).get("serial"), "extra_names": (extra or {}).get("names", []),
        "at": int(time.time())}), 0o644)
    return True


def write_traefik_tls(path: Path, certs_dir: str, mode: str = "default", extra: bool = False) -> None:
    head = "# managed by certpull — сменя се при нов сертификат\n"
    extra_item = (f"    - certFile: {certs_dir}/extra.crt\n      keyFile: {certs_dir}/extra.key\n" if extra else "")
    if mode == "certificates":
        body = (f"tls:\n  certificates:\n    - certFile: {certs_dir}/wildcard.crt\n"
                f"      keyFile: {certs_dir}/wildcard.key\n" + extra_item)
    else:
        body = ("tls:\n" + ("  certificates:\n" + extra_item if extra else "")
                + "  stores:\n    default:\n      defaultCertificate:\n"
                f"        certFile: {certs_dir}/wildcard.crt\n        keyFile: {certs_dir}/wildcard.key\n")
    _write(path, head + body, 0o644)


def own_ipv4() -> list[str]:
    """Глобалните IPv4 адреси на машината (без docker/loopback)."""
    import socket
    ips = set()
    try:
        for fam, _, _, _, addr in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(addr[0])
    except OSError:
        pass
    try:  # адресът, с който машината излиза навън — без пакет
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            ips.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith(("127.", "172.17.")))


def write_traefik_allow(path: Path, ips: list[str]) -> bool:
    """True — сменен."""
    ranges = ["127.0.0.1/32"] + [f"{ip}/32" for ip in ips]
    body = ("# managed by certpull — само тази машина и тунелът ѝ; LAN-ът получава 403\n"
            "http:\n  middlewares:\n    only-local:\n      ipAllowList:\n"
            f"        sourceRange: [{', '.join(repr(x).replace(chr(39), chr(34)) for x in ranges)}]\n")
    try:
        if path.read_text() == body:
            return False
    except FileNotFoundError:
        pass
    _write(path, body, 0o644)
    return True


def once(env: dict) -> int:
    status, body = fetch(env)
    if status != 200:
        log.warning("cert-issuer: %s %s", status, body.get("detail", ""))
        return 1
    changed = install(body, Path(env.get("CERTPULL_DIR", DEFAULT_DIR)))
    if changed and env.get("CERTPULL_TRAEFIK_TLS"):
        write_traefik_tls(Path(env["CERTPULL_TRAEFIK_TLS"]), env.get("CERTPULL_TRAEFIK_CERTS", "/certs"),
                          env.get("CERTPULL_TRAEFIK_MODE", "default"), extra=bool(body.get("extra")))
    log.info("%s %s serial=%s until=%s", "installed" if changed else "unchanged",
             ",".join(body.get("names", [])), body["serial"],
             time.strftime("%Y-%m-%d", time.gmtime(body["notAfter"])))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--env", help="file with KEY=VALUE lines")
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    env = load_env(a.env)
    if not env.get("CERTPULL_URL"):
        log.error("CERTPULL_URL is not set")
        return 2
    allow = env.get("CERTPULL_TRAEFIK_ALLOW")
    if a.once:
        if allow:
            write_traefik_allow(Path(allow), own_ipv4())
        return once(env)
    every = int(env.get("CERTPULL_EVERY_S", "21600"))
    next_pull = 0.0
    while True:
        if allow and write_traefik_allow(Path(allow), own_ipv4()):
            log.info("only-local: %s", ", ".join(["127.0.0.1"] + own_ipv4()))
        if time.time() >= next_pull:
            try:
                rc = once(env)
            except Exception:  # noqa: BLE001 — следващият опит е след RETRY_S
                log.exception("certpull failed")
                rc = 1
            next_pull = time.time() + (every if rc == 0 else RETRY_S)
        time.sleep(60)  # адресът (DHCP) се гледа всяка минута, сертификатът — по график


if __name__ == "__main__":
    sys.exit(main())
