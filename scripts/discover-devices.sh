#!/usr/bin/env bash
# Scans current /dev/* symlinks created by 99-erpnet-fp.rules and prints
# config.yaml-ready entries за всяко открито устройство.
#
# Употреба:  bash scripts/discover-devices.sh
set -euo pipefail

echo "# ────────────────────────────────────────────────────────────"
echo "# Discovered ErpNet.FP devices — копирай в config.yaml"
echo "# ────────────────────────────────────────────────────────────"
echo

# Datecs fiscal printers (datecs_fp_port*)
FPS=$(ls /dev/datecs_fp_port* 2>/dev/null | sort)
if [[ -n "$FPS" ]]; then
    echo "printers:"
    i=1
    for dev in $FPS; do
        port=${dev##*_port}
        echo "  - id: fp$i"
        echo "    driver: datecs.islx    # X variant (FP-700МК / DP-150X / FP-700X)"
        echo "    transport: serial"
        echo "    port: $dev    # USB topology port: $port"
        echo "    baudrate: 115200"
        echo "    operator: '1'"
        echo "    operator_password: '1'"
        echo "    till_number: $i"
        i=$((i+1))
    done
    echo
fi

# FTDI-attached printers (DP-150 thru FTDI cable)
FTDIS=$(ls /dev/ftdi_serial_port* 2>/dev/null | sort)
if [[ -n "$FTDIS" ]]; then
    echo "# DP-150 (C variant, FT232 cable):"
    for dev in $FTDIS; do
        port=${dev##*_port}
        echo "  - id: dp150_$port"
        echo "    driver: datecs.isl    # C variant (DP-150 base, comma-sep)"
        echo "    transport: serial"
        echo "    port: $dev"
        echo "    baudrate: 115200"
        echo "    operator: '1'"
        echo "    operator_password: '1'"
        echo "    till_number: $i"
        i=$((i+1))
    done
    echo
fi

# Pinpads (datecs_pinpad_*)
PINPADS=$(ls /dev/datecs_pinpad_* 2>/dev/null | grep -v "^/dev/datecs_pinpad$" | sort)
if [[ -n "$PINPADS" ]]; then
    echo "pinpads:"
    j=1
    for dev in $PINPADS; do
        serial=${dev##*_}
        echo "  - id: bluepad$j    # SN: $serial"
        echo "    driver: datecs_pay"
        echo "    port: $dev"
        echo "    baudrate: 115200"
        j=$((j+1))
    done
    echo
fi

# Honeywell readers (auto-detected; just info)
READERS=$(ls /dev/honeywell_scanner_* 2>/dev/null | grep -v "^/dev/honeywell_scanner$" | sort)
if [[ -n "$READERS" ]]; then
    echo "# Readers (auto-detect ги хваща без config; за info):"
    for dev in $READERS; do
        serial=${dev##*_}
        echo "#   $dev    # SN: $serial"
    done
fi
