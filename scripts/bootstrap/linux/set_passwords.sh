#!/bin/bash
# ==============================================================================
# 修改 root 與兩個固定帳號 (user1, user2) 的密碼
# ==============================================================================
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Please use sudo or root to run this script."
    exit 1
fi

echo "=== Changing Passwords ==="
USER1="${USER1:-}"
USER2="${USER2:-}"
PW_ROOT="${PW_ROOT:-}"
PW_USER1="${PW_USER1:-}"
PW_USER2="${PW_USER2:-}"

if [ -t 0 ]; then
    if [ -z "$USER1" ]; then
        read -r -p "Please enter the name of the first user: " USER1
    fi
    if [ -z "$USER2" ]; then
        read -r -p "Please enter the name of the second user: " USER2
    fi
    if [ -z "$PW_ROOT" ]; then
        read -r -s -p "Please enter the new password for root: " PW_ROOT; echo
    fi
    if [ -z "$PW_USER1" ]; then
        read -r -s -p "Please enter the new password for $USER1: " PW_USER1; echo
    fi
    if [ -z "$PW_USER2" ]; then
        read -r -s -p "Please enter the new password for $USER2: " PW_USER2; echo
    fi
fi

if [ -z "$USER1" ] || [ -z "$USER2" ]; then
    echo "[ERROR] USER1 and USER2 cannot be empty."
    exit 2
fi

if [ -z "$PW_ROOT" ] || [ -z "$PW_USER1" ] || [ -z "$PW_USER2" ]; then
    echo "[ERROR] Passwords (PW_ROOT, PW_USER1, PW_USER2) cannot be empty."
    exit 3
fi

modify_password() {
    local target_user="$1"
    local target_pw="$2"
    if ! id "$target_user" &>/dev/null; then
        echo "[WARN] User '$target_user' does not exist, skipping password change."
        return 0
    fi
    echo "Changing ${target_user}'s password..."
    printf '%s:%s\n' "$target_user" "$target_pw" | chpasswd
    echo "[OK] ${target_user}'s password updated."
}

modify_password "root" "$PW_ROOT"
modify_password "$USER1" "$PW_USER1"
modify_password "$USER2" "$PW_USER2"

echo "[SUCCESS] All target user passwords updated."
