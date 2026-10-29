#!/bin/bash
# Пред касата: Traefik с *.erpnet.mcpworks.net + certpull (ADR erpnet-kasi-fleet/0005).
#
#   браузърът на касата ── https://$EDGE_NAME (→ 127.0.0.1, наш блок в /etc/hosts)
#   ние през WARP ── маршрут по име → тунелът на машината → tunnel_dns: `self`
#        ▼  Traefik на :443 — пуска само 127.0.0.1 и собствените адреси на машината
#           (оттам идва cloudflared); всеки друг в LAN-а получава 403
#        ├─ /pinpads/…  → $PINPAD_UPSTREAM   (APK-то на BlueCash, канал „Pinpad HTTPS“)
#        └─ останалото  → $PROXY_UPSTREAM    (проксито, фискалният бон)
#
# Всичко за конкретния клиент е в /etc/odoo-erpnet-fp/edge.env (само на машината):
#   EDGE_NAME=<каса>.<клиент>.erpnet.mcpworks.net
#   PINPAD_UPSTREAM=https://<IP на BlueCash>:9443
#   PROXY_UPSTREAM=http://127.0.0.1:8001
#   CERTPULL_URL=https://<cert-issuer>
#   CERTPULL_CF_ID=<ключ за услуга на Access>          (по избор)
# Тайните са във файлове 0600 до него: certpull.secret, certpull.cf_secret.
#
#   sudo ./install-edge.sh            # слага/обновява (идемпотентно)
#   sudo ./install-edge.sh remove     # маха само нашето
set -euo pipefail

TRAEFIK_IMAGE=${TRAEFIK_IMAGE:-traefik:v3.7.14}
ETC=/etc/odoo-erpnet-fp
ENV_FILE=$ETC/edge.env
DYN=$ETC/edge/dynamic
CERTS=/var/lib/odoo-erpnet-fp/certs
LIB=/usr/lib/odoo-erpnet-fp
HERE=$(cd "$(dirname "$0")" && pwd)
BEGIN="# BEGIN erpnet-fp-edge (managed by Odoo.ErpNet.FP - do not edit inside)"
END="# END erpnet-fp-edge"

hosts_block() {  # $1 = име или празно (маха блока)
    local tmp; tmp=$(mktemp)
    awk -v b="$BEGIN" -v e="$END" '$0==b{skip=1;next} $0==e{skip=0;next} !skip' /etc/hosts > "$tmp"
    if [ -n "${1:-}" ]; then printf '%s\n127.0.0.1 %s\n%s\n' "$BEGIN" "$1" "$END" >> "$tmp"; fi
    if ! cmp -s "$tmp" /etc/hosts; then cat "$tmp" > /etc/hosts; fi   # cat: пази inode-а и правата
    rm -f "$tmp"
}

if [ "${1:-}" = remove ]; then
    systemctl disable --now odoo-erpnet-fp-edge.service odoo-erpnet-fp-certpull.service 2>/dev/null || true
    rm -f /etc/systemd/system/odoo-erpnet-fp-edge.service /etc/systemd/system/odoo-erpnet-fp-certpull.service
    systemctl daemon-reload
    hosts_block ""
    rm -rf "$ETC/edge" "$LIB/certpull.py"
    echo "edge: махнато (сертификатите в $CERTS и $ENV_FILE остават)"
    exit 0
fi

[ -r "$ENV_FILE" ] || { echo "няма $ENV_FILE"; exit 1; }
# shellcheck disable=SC1090
. "$ENV_FILE"
for v in EDGE_NAME PINPAD_UPSTREAM PROXY_UPSTREAM CERTPULL_URL; do
    [ -n "${!v:-}" ] || { echo "$ENV_FILE: липсва $v"; exit 1; }
done
# <каса>.<клиент>.erpnet.mcpworks.net (ADR-0002) — сертификатът носи *.<клиент>.erpnet… (ADR-0005)
if ! [[ "$EDGE_NAME" =~ ^[a-z0-9-]+(\.[a-z0-9-]+)?\.erpnet\.mcpworks\.net$ ]]; then
    echo "EDGE_NAME трябва да е <каса>.<клиент>.erpnet.mcpworks.net"; exit 1
fi
[ -r "$ETC/certpull.secret" ] || { echo "няма $ETC/certpull.secret"; exit 1; }
chmod 600 "$ETC/certpull.secret" "$ENV_FILE"; [ -e "$ETC/certpull.cf_secret" ] && chmod 600 "$ETC/certpull.cf_secret"

install -d -m 755 "$DYN" "$LIB"
install -d -m 700 "$CERTS"
install -m 644 "$HERE/../../odoo_erpnet_fp/certpull.py" "$LIB/certpull.py"

# TLS настройката я пише certpull, когато сертификатът дойде; дотогава е празна
# и Traefik върви със своя временен (с липсващ файл не би отговарял по TLS изобщо).
if [ -s "$CERTS/wildcard.crt" ] && [ -s "$CERTS/wildcard.key" ]; then
    printf '%s\n' "# managed by certpull — сменя се при нов сертификат" "tls:" "  stores:" "    default:" \
        "      defaultCertificate:" "        certFile: /certs/wildcard.crt" "        keyFile: /certs/wildcard.key" > "$DYN/tls.yml"
else
    echo "# waiting for certpull" > "$DYN/tls.yml"
fi
python3 -c "import sys; sys.path.insert(0, '$LIB'); import certpull, pathlib; certpull.write_traefik_allow(pathlib.Path('$DYN/allow.yml'), certpull.own_ipv4())"
cat > "$DYN/routes.yml" <<EOF
# managed by install-edge.sh
http:
  routers:
    pinpad:
      rule: "Host(\`$EDGE_NAME\`) && PathPrefix(\`/pinpads\`)"
      entryPoints: [websecure]
      middlewares: [only-local]
      service: pinpad
      tls: {}
      priority: 100
    proxy:
      rule: "Host(\`$EDGE_NAME\`)"
      entryPoints: [websecure]
      middlewares: [only-local]
      service: proxy
      tls: {}
      priority: 10
  services:
    pinpad:
      loadBalancer:
        serversTransport: device
        servers: [{url: "$PINPAD_UPSTREAM"}]
    proxy:
      loadBalancer:
        servers: [{url: "$PROXY_UPSTREAM"}]
  serversTransports:
    device:
      # Устройството в LAN е със самоподписан сертификат (APK-то); веригата до
      # браузъра е тази на Traefik.
      insecureSkipVerify: true
EOF

cat > "$ETC/certpull.env" <<EOF
CERTPULL_URL=$CERTPULL_URL
CERTPULL_SECRET_FILE=$ETC/certpull.secret
CERTPULL_CF_ID=${CERTPULL_CF_ID:-}
CERTPULL_CF_SECRET_FILE=$( [ -e "$ETC/certpull.cf_secret" ] && echo "$ETC/certpull.cf_secret" )
CERTPULL_DIR=$CERTS
CERTPULL_TRAEFIK_TLS=$DYN/tls.yml
CERTPULL_TRAEFIK_CERTS=/certs
CERTPULL_TRAEFIK_ALLOW=$DYN/allow.yml
EOF
chmod 600 "$ETC/certpull.env"

cat > /etc/systemd/system/odoo-erpnet-fp-certpull.service <<EOF
[Unit]
Description=Odoo.ErpNet.FP — certificate pull (*.erpnet.mcpworks.net)
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/python3 $LIB/certpull.py --env $ETC/certpull.env
Restart=always
RestartSec=60

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/odoo-erpnet-fp-edge.service <<EOF
[Unit]
Description=Odoo.ErpNet.FP — Traefik in front of the till (https on 127.0.0.1:443)
After=docker.service odoo-erpnet-fp-certpull.service
Requires=docker.service

[Service]
ExecStartPre=-/usr/bin/docker rm -f odoo-erpnet-fp-edge
ExecStart=/usr/bin/docker run --rm --name odoo-erpnet-fp-edge --network host \\
  -v $DYN:/dynamic:ro -v $CERTS:/certs:ro $TRAEFIK_IMAGE \\
  --entrypoints.websecure.address=:443 \\
  --providers.file.directory=/dynamic --providers.file.watch=true \\
  --log.level=INFO
ExecStop=/usr/bin/docker stop odoo-erpnet-fp-edge
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

hosts_block "$EDGE_NAME"
systemctl daemon-reload
systemctl enable --now odoo-erpnet-fp-certpull.service
systemctl enable odoo-erpnet-fp-edge.service
systemctl restart odoo-erpnet-fp-edge.service
echo "edge: https://$EDGE_NAME → /pinpads: $PINPAD_UPSTREAM · останалото: $PROXY_UPSTREAM"
