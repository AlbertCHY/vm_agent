#!/bin/bash
# ==============================================================================
# 修改 Zabbix Agent 設定檔並重啟服務
# ==============================================================================
set -euo pipefail

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Please use sudo or root to run this script."
    exit 1
fi

echo "=== Zabbix Agent Configuration Update ==="
LOCAL_IP="${TARGET_IP:-$(hostname -I | awk '{print $1}')}"
ZABBIX_SERVERS="${ZABBIX_SERVERS:-}"
ZABBIX_CONF="${ZABBIX_CONF:-/etc/zabbix/zabbix_agentd.conf}"

if [ -z "$ZABBIX_SERVERS" ]; then
    echo "[ERROR] ZABBIX_SERVERS is not set (please configure ZABBIX_SERVERS in .env)."
    exit 2
fi

if [ ! -f "$ZABBIX_CONF" ]; then
    echo "[WARN] Cannot find Zabbix configuration file at $ZABBIX_CONF"
    exit 0
fi

sed -i "s|^Server=.*|Server=${ZABBIX_SERVERS}|" "$ZABBIX_CONF"
sed -i "s|^ServerActive=.*|ServerActive=${ZABBIX_SERVERS}|" "$ZABBIX_CONF"
sed -i "s|^Hostname=.*|Hostname=${LOCAL_IP}|" "$ZABBIX_CONF"

systemctl restart zabbix-agent

if systemctl is-active --quiet zabbix-agent; then
    echo "[OK] Zabbix config ($ZABBIX_CONF) updated (Hostname=${LOCAL_IP}) and zabbix-agent is running."
else
    echo "[ERROR] Zabbix Agent failed to start after config update."
    exit 3
fi
