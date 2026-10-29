#!/bin/bash
# Строи odoo-erpnet-fp_<версия>-1_amd64.deb: Docker образът на проксито + собственият тунел.
#   packaging/linux-deb/build-deb.sh [изходна папка]     (по подразбиране build/deb)
# Версията е от pyproject.toml; cloudflared — последният от GitHub с проверен sha256
# (CLOUDFLARED_TAG=… заковава версия), както Windows инсталаторът.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
OUT=${1:-$ROOT/build/deb}
VERSION=$(sed -n 's/^version = "\(.*\)"/\1/p' "$ROOT/pyproject.toml")
STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
PKG=$STAGE/odoo-erpnet-fp

install -d "$PKG/DEBIAN" "$PKG/opt/erpnet-fp/bin" "$PKG/opt/erpnet-fp/lib/odoo_erpnet_fp" \
    "$PKG/lib/systemd/system" "$PKG/usr/bin" "$PKG/etc/odoo-erpnet-fp" "$PKG/etc/default" \
    "$PKG/usr/share/doc/odoo-erpnet-fp"

# Записването и отговарящият за имената — само стандартната библиотека на Python
for f in __init__.py enroll.py tunnel_dns.py; do
    install -m 644 "$ROOT/odoo_erpnet_fp/$f" "$PKG/opt/erpnet-fp/lib/odoo_erpnet_fp/$f"
done

# cloudflared: собствено копие (чужда инсталация не се пипа)
python3 - "$PKG/opt/erpnet-fp/bin" <<'PY'
import hashlib, json, os, sys, urllib.request
out = sys.argv[1]
tag = os.environ.get("CLOUDFLARED_TAG", "").strip()
api = "https://api.github.com/repos/cloudflare/cloudflared/releases/" + ("tags/" + tag if tag else "latest")
hdr = {"Accept": "application/vnd.github+json", "User-Agent": "erpnet-fp-deb-build"}
if os.environ.get("GITHUB_TOKEN"):
    hdr["Authorization"] = "Bearer " + os.environ["GITHUB_TOKEN"]
rel = json.loads(urllib.request.urlopen(urllib.request.Request(api, headers=hdr), timeout=120).read())
if rel.get("prerelease"):
    sys.exit("cloudflared %s is a prerelease — refusing" % rel["tag_name"])
asset = [a for a in rel["assets"] if a["name"] == "cloudflared-linux-amd64"]
if len(asset) != 1:
    sys.exit("no cloudflared-linux-amd64 asset in %s" % rel["tag_name"])
digest = asset[0].get("digest") or ""
if not digest.startswith("sha256:"):
    sys.exit("GitHub gave no sha256 digest for cloudflared-linux-amd64")
data = urllib.request.urlopen(urllib.request.Request(asset[0]["browser_download_url"], headers=hdr), timeout=300).read()
if hashlib.sha256(data).hexdigest() != digest.split(":", 1)[1]:
    sys.exit("cloudflared sha256 mismatch")
path = os.path.join(out, "cloudflared")
open(path, "wb").write(data)
os.chmod(path, 0o755)
open(os.path.join(out, "..", "cloudflared.version"), "w").write(rel["tag_name"] + "\n")
print("cloudflared", rel["tag_name"])
PY

install -m 644 "$HERE"/files/*.service "$PKG/lib/systemd/system/"
install -m 755 "$HERE/files/erpnet-fp-enroll" "$PKG/usr/bin/erpnet-fp-enroll"
install -m 644 "$HERE/files/config.yaml" "$PKG/etc/odoo-erpnet-fp/config.yaml"
sed "s/@VERSION@/$VERSION/" "$HERE/files/default" > "$PKG/etc/default/odoo-erpnet-fp"
chmod 644 "$PKG/etc/default/odoo-erpnet-fp"
install -m 644 "$ROOT/CHANGELOG.md" "$PKG/usr/share/doc/odoo-erpnet-fp/CHANGELOG.md"

for f in conffiles templates; do install -m 644 "$HERE/debian/$f" "$PKG/DEBIAN/$f"; done
for f in config postinst prerm postrm; do install -m 755 "$HERE/debian/$f" "$PKG/DEBIAN/$f"; done
sed "s/@VERSION@/$VERSION/" "$HERE/debian/control" > "$PKG/DEBIAN/control"
echo "Installed-Size: $(du -sk "$PKG" | cut -f1)" >> "$PKG/DEBIAN/control"

# Без право на запис за групата/другите — системните папки и файлове са само на root
chmod -R go-w "$PKG"
chmod 644 "$PKG/opt/erpnet-fp/cloudflared.version"

mkdir -p "$OUT"
DEB="$OUT/odoo-erpnet-fp_${VERSION}-1_amd64.deb"
dpkg-deb --root-owner-group --build "$PKG" "$DEB" >/dev/null
echo "$DEB"
