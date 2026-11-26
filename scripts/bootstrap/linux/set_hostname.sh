#!/bin/bash
# ==============================================================================
# 變更主機名稱 (/etc/hostname & hostnamectl) 並同步更新 /etc/hosts
# ==============================================================================
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Please use sudo or root to run this script."
    exit 1
fi

echo "=== Changing Hostname and /etc/hosts ==="
if [ -z "${NEW_HOSTNAME:-}" ] && [ -t 0 ]; then
    read -r -p "Please enter the new hostname: " NEW_HOSTNAME
fi

if [ -z "${NEW_HOSTNAME:-}" ]; then
    echo "[ERROR] NEW_HOSTNAME cannot be empty."
    exit 2
fi

OLD_HOSTNAME=$(hostname)
LOCAL_IP="${TARGET_IP:-$(hostname -I | awk '{print $1}')}"

# 1. 修改 /etc/hostname 並立即套用
echo "$NEW_HOSTNAME" > /etc/hostname
if command -v hostnamectl &>/dev/null; then
    hostnamectl set-hostname "$NEW_HOSTNAME" || hostname "$NEW_HOSTNAME"
else
    hostname "$NEW_HOSTNAME"
fi
echo "[OK] Hostname updated to: $(hostname)"

# 2. 修改 /etc/hosts (同步替換舊 hostname 避免 sudo: unable to resolve host)
if [ -n "$OLD_HOSTNAME" ] && [ "$OLD_HOSTNAME" != "$NEW_HOSTNAME" ] && [ "$OLD_HOSTNAME" != "localhost" ]; then
    sed -i "s/\b$OLD_HOSTNAME\b/$NEW_HOSTNAME/g" /etc/hosts || true
fi

if grep -q "$LOCAL_IP" /etc/hosts; then
    if ! grep "$LOCAL_IP" /etc/hosts | grep -qw "$NEW_HOSTNAME"; then
        sed -i "/$LOCAL_IP/s/$/ $NEW_HOSTNAME/" /etc/hosts
    fi
else
    echo "$LOCAL_IP $NEW_HOSTNAME" >> /etc/hosts
fi

echo "[OK] /etc/hosts updated: $(grep "$LOCAL_IP" /etc/hosts || true)"
