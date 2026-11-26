#!/bin/bash
set -euo pipefail

USER_NAME="${TARGET_USER:-$USER}"
USER_HOME=$(eval echo "~$USER_NAME")
MY_PUB_KEY="${SSH_PUB_KEY:-}"

if [ -z "$MY_PUB_KEY" ]; then
    echo "[ERROR] SSH_PUB_KEY is empty."
    exit 1
fi

echo "[INFO] Checking $USER_HOME/.ssh for user $USER_NAME ..."
if [ ! -d "$USER_HOME/.ssh" ]; then
    mkdir -p "$USER_HOME/.ssh"
    chmod 700 "$USER_HOME/.ssh"
fi

echo "[INFO] Checking $USER_HOME/.ssh/authorized_keys ..."
if [ ! -f "$USER_HOME/.ssh/authorized_keys" ]; then
    touch "$USER_HOME/.ssh/authorized_keys"
    chmod 600 "$USER_HOME/.ssh/authorized_keys"
fi

# 抽取公鑰核心欄位 (第 2 欄 base64 部分) 避免只比對結尾註解
KEY_BODY=$(echo "$MY_PUB_KEY" | awk '{print $2}')

echo "[INFO] Checking pub keys ..."
if [ -n "$KEY_BODY" ] && grep -qF "$KEY_BODY" "$USER_HOME/.ssh/authorized_keys"; then
    echo "[OK] Pubkey already exists"
else
    echo "$MY_PUB_KEY" >> "$USER_HOME/.ssh/authorized_keys"
    echo "[SUCCESS] Pubkey insert success!"
fi

# 若以 root 身分代建，確保目錄與檔案擁有者正確
if [ "$EUID" -eq 0 ] && id "$USER_NAME" &>/dev/null; then
    chown -R "$USER_NAME:$USER_NAME" "$USER_HOME/.ssh"
fi
