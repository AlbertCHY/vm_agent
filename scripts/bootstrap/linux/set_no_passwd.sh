#!/bin/bash
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "Please use sudo or root to run this script."
    exit 1
fi

TARGET_USER="${TARGET_USER:-}"
if [ -z "$TARGET_USER" ] && [ -t 0 ]; then
    read -r -p "Please enter target user name: " TARGET_USER
fi

if [ -z "$TARGET_USER" ]; then
    echo "[ERROR] User name cannot be empty."
    exit 3
fi

if ! id "$TARGET_USER" &>/dev/null; then
    echo "[ERROR] User '$TARGET_USER' does not exist on this system."
    exit 4
fi

SUDO_FILE="/etc/sudoers.d/${TARGET_USER}_nopasswd"

if [ ! -d "/etc/sudoers.d" ]; then
    echo "/etc/sudoers.d does not exist"
    exit 2
fi

if [ -f "$SUDO_FILE" ]; then
    echo "[OK] $SUDO_FILE already exists, verifying syntax..."
    if visudo -cf "$SUDO_FILE"; then
        exit 0
    fi
fi

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT

echo "${TARGET_USER} ALL=(ALL) NOPASSWD:ALL" > "$TMP"
chmod 440 "$TMP"

visudo -cf "$TMP"
mv "$TMP" "$SUDO_FILE"
echo "[SUCCESS] Configured NOPASSWD sudo for ${TARGET_USER} ($SUDO_FILE)"

exit 0
