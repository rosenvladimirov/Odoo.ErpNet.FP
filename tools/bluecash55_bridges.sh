#!/usr/bin/env bash
# BlueCash-55 socat bridges — wires the Android PinpadBridgeService
# (channel 2, port 9101) into a PTY that the existing datecs_pay
# driver discovers via its `/dev/datecs_pinpad/*` pattern.
#
# The scanner (channel 3, port 9102) and fiscal printer (channel 1,
# port 9100) are handled natively by the proxy (TcpBarcodeReader and
# datecs.pm transport=tcp respectively) — no socat needed.
#
# Modes:
#   ./tools/bluecash55_bridges.sh              # foreground, single socat
#   ./tools/bluecash55_bridges.sh --daemon     # background, single socat
#   ./tools/bluecash55_bridges.sh --watchdog   # foreground watchdog loop
#                                              # (auto-restart on socat exit)
#   ./tools/bluecash55_bridges.sh --watchdog-daemon
#                                              # background watchdog loop
#
# Why a watchdog? socat exits cleanly when the Android side closes
# the TCP socket (idle timeout, service restart, BLE blip, APK
# redeploy). A one-shot socat leaves the proxy with a dangling PTY
# symlink and an unreachable pinpad until manual intervention. The
# watchdog re-spawns socat after 2 s and recreates the PTY symlink.
#
# Container-friendly variant — run from inside the proxy container:
#   docker exec -d odoo-erpnet-fp \
#     /bin/sh -c "/app/tools/bluecash55_bridges.sh --watchdog-daemon"
#
# To make this persistent across container restarts, either:
#   * Add a startup hook under /docker-entrypoint.d/ that calls
#     this script with --watchdog-daemon; or
#   * docker-compose sidecar service that runs the watchdog
#     attached to the same network namespace.

set -euo pipefail

BLUECASH_HOST="${BLUECASH_HOST:-192.168.1.70}"
PINPAD_PORT="${PINPAD_PORT:-9101}"
# PTY basename = serial of the BlueCash-55. Endpoint id derives from
# this basename via the `pinpad_<basename>` pattern in config.yaml
# (so the final endpoint becomes `/pinpads/pinpad_DA054852`).
PTY_PATH="${PTY_PATH:-/dev/datecs_pinpad/DA054852}"
LOG_PATH="${LOG_PATH:-/tmp/socat_DA054852.log}"
WATCHDOG_LOG="${WATCHDOG_LOG:-/tmp/socat_watchdog.log}"
RESTART_DELAY_S="${RESTART_DELAY_S:-2}"

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

run_watchdog() {
    while true; do
        # Clean any dangling PTY symlink from the previous socat
        # (otherwise next socat sees EEXIST and exits immediately).
        rm -f "$PTY_PATH"
        echo "[$(date '+%H:%M:%S')] starting socat → ${BLUECASH_HOST}:${PINPAD_PORT}" \
            >> "$WATCHDOG_LOG"
        # Run socat in foreground; capture exit code.
        socat "${SOCAT_ARGS[@]}" >> "$WATCHDOG_LOG" 2>&1
        rc=$?
        echo "[$(date '+%H:%M:%S')] socat exited rc=$rc — restart in ${RESTART_DELAY_S}s" \
            >> "$WATCHDOG_LOG"
        sleep "$RESTART_DELAY_S"
    done
}

case "${1:-}" in
    --daemon)
        echo "Starting socat pinpad bridge in background → $LOG_PATH"
        nohup socat "${SOCAT_ARGS[@]}" > "$LOG_PATH" 2>&1 &
        echo "  pid: $!"
        echo "  PTY: $PTY_PATH"
        ;;
    --watchdog)
        echo "Starting socat pinpad watchdog (foreground)"
        echo "  PTY: $PTY_PATH"
        echo "  TCP: $BLUECASH_HOST:$PINPAD_PORT"
        echo "  log: $WATCHDOG_LOG"
        run_watchdog
        ;;
    --watchdog-daemon)
        echo "Starting socat pinpad watchdog in background → $WATCHDOG_LOG"
        nohup "$0" --watchdog > /dev/null 2>&1 &
        echo "  pid: $!"
        echo "  PTY: $PTY_PATH"
        ;;
    *)
        echo "Starting socat pinpad bridge (foreground)"
        echo "  PTY: $PTY_PATH"
        echo "  TCP: $BLUECASH_HOST:$PINPAD_PORT"
        exec socat "${SOCAT_ARGS[@]}"
        ;;
esac
