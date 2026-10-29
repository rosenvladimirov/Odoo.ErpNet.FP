#!/usr/bin/env python3
"""Сваля зависимостите, които инсталаторът носи със себе си: MSI-то на
Win32-OpenSSH (сървърът) и самостоятелния cloudflared.exe (конекторът на
собствения тунел — не MSI, за да не пипа чужда инсталация на cloudflared).

По подразбиране взима последното издание (releases/latest) от GitHub;
за повторяем билд версията се заковава с OPENSSH_TAG / CLOUDFLARED_TAG.
Всеки файл се сверява със sha256, който GitHub дава в полето `digest`
на актива — несъвпадение спира билда.

Употреба:  fetch_bundled_msi.py <изходна-директория>
Резултат:  <dir>/OpenSSH-Win64.msi, <dir>/cloudflared.exe,
           <dir>/versions.env (OPENSSH_VERSION=..., CLOUDFLARED_VERSION=...)
"""

import hashlib
import json
import os
import pathlib
import re
import sys
import urllib.request

# (ключ, репо, променлива за заковаване, шаблон на актива, име на диска)
BUNDLES = [
    ("OPENSSH", "PowerShell/Win32-OpenSSH", "OPENSSH_TAG",
     re.compile(r"^OpenSSH-Win64-v[\d.]+\.msi$"), "OpenSSH-Win64.msi"),
    ("CLOUDFLARED", "cloudflare/cloudflared", "CLOUDFLARED_TAG",
     re.compile(r"^cloudflared-windows-amd64\.exe$"), "cloudflared.exe"),
]


def _get(url):
    headers = {"Accept": "application/vnd.github+json",
               "User-Agent": "erpnet-fp-installer-build"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = "Bearer " + token
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=120) as r:
        return r.read()


def _release(repo, tag):
    path = "tags/" + tag if tag else "latest"
    return json.loads(_get("https://api.github.com/repos/%s/releases/%s" % (repo, path)))


def _msi_version(asset_name, tag):
    # Win32-OpenSSH: таговете са „10.0.0.0p2-Preview“, а версията в името
    # на MSI-то е чистата (10.0.0.0) — нея сравнява скриптът на целта.
    m = re.search(r"v([\d.]+)\.msi$", asset_name)
    return m.group(1) if m else tag


def main():
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    out = pathlib.Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    env_lines = []
    for key, repo, pin_var, pattern, disk_name in BUNDLES:
        rel = _release(repo, os.environ.get(pin_var, "").strip())
        if rel.get("prerelease"):
            sys.exit("%s %s is a prerelease — refusing" % (repo, rel["tag_name"]))
        assets = [a for a in rel["assets"] if pattern.match(a["name"])]
        if len(assets) != 1:
            sys.exit("%s %s: expected exactly one asset matching %s, got %s"
                     % (repo, rel["tag_name"], pattern.pattern, [a["name"] for a in assets]))
        asset = assets[0]
        digest = asset.get("digest") or ""
        if not digest.startswith("sha256:"):
            sys.exit("%s %s: GitHub gave no sha256 digest for %s" % (repo, rel["tag_name"], asset["name"]))
        data = _get(asset["browser_download_url"])
        got = hashlib.sha256(data).hexdigest()
        if got != digest.split(":", 1)[1]:
            sys.exit("%s: sha256 mismatch (%s != %s)" % (asset["name"], got, digest))
        (out / disk_name).write_bytes(data)
        version = _msi_version(asset["name"], rel["tag_name"])
        env_lines.append("%s_VERSION=%s" % (key, version))
        env_lines.append("%s_TAG=%s" % (key, rel["tag_name"]))
        env_lines.append("%s_SHA256=%s" % (key, got))
        print("  ok %-32s %-22s sha256 %s…" % (asset["name"], rel["tag_name"], got[:16]))
    (out / "versions.env").write_text("\n".join(env_lines) + "\n", encoding="ascii")


if __name__ == "__main__":
    main()
