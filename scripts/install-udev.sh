#!/usr/bin/env bash
# Инсталира udev правилата на хоста + reload + trigger.
# Изисква sudo (записва в /etc/udev/rules.d/).
#
# Употреба:  sudo bash scripts/install-udev.sh
set -euo pipefail

RULE_SRC="$(cd "$(dirname "$0")/.." && pwd)/udev/99-erpnet-fp.rules"
RULE_DST="/etc/udev/rules.d/99-erpnet-fp.rules"

if [[ $EUID -ne 0 ]]; then
    echo "❌ Изисква root. Изпълни:  sudo bash $0" >&2
    exit 1
fi
if [[ ! -f "$RULE_SRC" ]]; then
    echo "❌ Не намерих $RULE_SRC" >&2
    exit 1
fi

echo "→ Копирам $RULE_SRC → $RULE_DST"
install -m 644 "$RULE_SRC" "$RULE_DST"

echo "→ udevadm control --reload-rules"
udevadm control --reload-rules

echo "→ udevadm trigger --subsystem-match=tty (re-enumerate)"
udevadm trigger --subsystem-match=tty

sleep 1
echo ""
echo "=== резултат ==="
ls -la /dev/datecs_pinpad /dev/honeywell_scanner 2>/dev/null || \
    echo "⚠ символни линкове още не се появили — провери дали устройствата са включени"
