#!/usr/bin/env bash
# BlueCash-55 socat bridges — wires the Android PinpadBridgeService
# (channel 2, port 9101) into a PTY that the existing datecs_pay
# driver discovers via its `/dev/datecs_pinpad/*` pattern.
#
# The scanner (channel 3, port 9102) and fiscal printer (channel 1,
# port 9100) are handled natively by the proxy (TcpBarcodeReader and
# datecs.pm transport=tcp respectively) — no socat needed.
#
# Usage:
#   ./tools/bluecash50_bridges.sh             # foreground (Ctrl-C to stop)
#   ./tools/bluecash50_bridges.sh --daemon    # detach as background process
#
# Container-friendly variant — run from inside the proxy container:
#   docker exec -d odoo-erpnet-fp /bin/sh -c \
#     "/app/tools/bluecash50_bridges.sh --daemon"
#
# To make this persistent across container restarts, either:
#   * Mount this script into the container and call it from a
#     docker-compose `command:` directive after the proxy starts; or
#   * Bake `socat` into the container image and add a startup hook
#     under /docker-entrypoint.d/.

set -euo pipefail

BLUECASH_HOST="${BLUECASH_HOST:-192.168.1.70}"
PINPAD_PORT="${PINPAD_PORT:-9101}"
# PTY basename = serial of the BlueCash-55. Endpoint id derives from
# this basename via the `pinpad_<basename>` pattern in config.yaml
# (so the final endpoint becomes `/pinpads/pinpad_DA054852`).
PTY_PATH="${PTY_PATH:-/dev/datecs_pinpad/DA054852}"
LOG_PATH="${LOG_PATH:-/tmp/socat_DA054852.log}"

if [ ! -d "$(dirname "$PTY_PATH")" ]; then
    mkdir -p "$(dirname "$PTY_PATH")"
fi

# socat keep-alive tuning — Android may sleep wifi; auto-reconnect
# after 30 s of unacked TCP probes (3 probes × 5 s + 15 s idle).
SOCAT_ARGS=(
    -d
    "PTY,link=${PTY_PATH},raw,echo=0,perm=0666"
    "TCP:${BLUECASH_HOST}:${PINPAD_PORT},reuseaddr,connect-timeout=5,keepalive,keepidle=10,keepintvl=5,keepcnt=3"
)

if [ "${1:-}" = "--daemon" ]; then
    echo "Starting socat pinpad bridge in background → $LOG_PATH"
    nohup socat "${SOCAT_ARGS[@]}" > "$LOG_PATH" 2>&1 &
    echo "  pid: $!"
    echo "  PTY: $PTY_PATH"
    exit 0
fi

echo "Starting socat pinpad bridge (foreground)"
echo "  PTY: $PTY_PATH"
echo "  TCP: $BLUECASH_HOST:$PINPAD_PORT"
exec socat "${SOCAT_ARGS[@]}"
