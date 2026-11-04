#!/bin/bash
set -uo pipefail

# ==========================================
# 1. target array
# ==========================================

readonly FILES_TO_REMOVE=(
    "/tmp/add_pub_key.sh"
    "/tmp/set_no_passwd.sh"
    "/tmp/set_hostname.sh"
    "/tmp/set_passwords.sh"
    "/tmp/set_zabbix.sh"
    "/tmp/set_crowdstrike.sh"
    "/tmp/expand_root_disk.sh"
    "/tmp/mount_extra_disk.sh"
    "/tmp/sop.sh"
    "/tmp/add_disk.sh"
    "/tmp/remover.sh"
)

readonly DIRS_TO_REMOVE=(
    "/tmp/CrowdStrike"
)

# ==========================================
# 2. remove logic
# ==========================================

echo "Cleanup start"
echo "----------------------------------------"

for file in "${FILES_TO_REMOVE[@]}"; do
    if [[ -f "$file" ]]; then
        if rm -f "$file"; then
            echo "Deleted: $file"
        else
            echo "Failed: $file"
        fi
    else
        echo "File not found, skipping: $file"
    fi
done

for dir in "${DIRS_TO_REMOVE[@]}"; do
    if [[ -d "$dir" ]]; then
        if rm -rf "$dir"; then
            echo "Deleted: $dir"
        else
            echo "Failed: $dir"
        fi
    else
        echo "Directory not found, skipping: $dir"
    fi
done

echo "----------------------------------------"
echo "Cleanup completed."
