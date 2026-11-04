#!/usr/bin/env bash
# 唯讀診斷腳本：網路介面、Default Gateway、DNS 設定與解析、目標 Host/Port 連通性、Proxy 與防火牆
# 選填環境變數：TARGET_HOST (目標網域或 IP), TARGET_PORT (目標 TCP Port)
export LANG=C LC_ALL=C
TARGET_HOST="${TARGET_HOST:-}"
TARGET_PORT="${TARGET_PORT:-}"

echo "=== [SECTION:IFACE_AND_ROUTE] ==="
echo "--- 網卡狀態 (ip -br link / addr) ---"
ip -br link 2>&1
ip -br addr 2>&1
echo "--- 路由表 (ip route) ---"
ip route 2>&1
GW=$(ip route | awk '/^default/ {print $3; exit}')
if [ -n "$GW" ]; then
    echo "--- Default Gateway ($GW) Ping 測試 ---"
    ping -c 2 -W 2 "$GW" 2>&1
else
    echo "⚠️ 未設定 Default Gateway!"
fi

echo "=== [SECTION:DNS_CHECK] ==="
echo "--- /etc/resolv.conf ---"
grep -vE '^\s*(#|$)' /etc/resolv.conf 2>/dev/null
if command -v resolvectl >/dev/null 2>&1; then
    echo "--- resolvectl status ---"
    resolvectl status 2>/dev/null | head -n 35
fi
echo "--- /etc/nsswitch.conf (hosts 行) ---"
grep -E '^\s*hosts:' /etc/nsswitch.conf 2>/dev/null
echo "--- /etc/hosts ---"
grep -vE '^\s*(#|$)' /etc/hosts 2>/dev/null

echo "--- 測試各 Nameserver 連通性與解析 ---"
for ns in $(awk '/^\s*nameserver/ {print $2}' /etc/resolv.conf 2>/dev/null); do
    echo "Nameserver $ns:"
    timeout 2 bash -c "</dev/tcp/$ns/53" 2>/dev/null && echo "  TCP/53 可連通" || ping -c 1 -W 2 "$ns" 2>&1 | tail -n 2
done

TEST_DOMAIN="${TARGET_HOST:-$(hostname -f 2>/dev/null || echo 'localhost')}"
echo "--- getent ahosts $TEST_DOMAIN ---"
getent ahosts "$TEST_DOMAIN" 2>&1 || nslookup "$TEST_DOMAIN" 2>&1 || host "$TEST_DOMAIN" 2>&1

echo "=== [SECTION:TARGET_CONNECTIVITY] ==="
if [ -n "$TARGET_HOST" ]; then
    echo "--- Ping 目標 ($TARGET_HOST) ---"
    ping -c 2 -W 2 "$TARGET_HOST" 2>&1
    if [ -n "$TARGET_PORT" ]; then
        echo "--- TCP 連線測試 ($TARGET_HOST:$TARGET_PORT) ---"
        if timeout 3 bash -c "</dev/tcp/$TARGET_HOST/$TARGET_PORT" 2>/dev/null; then
            echo "✅ TCP 連線成功: $TARGET_HOST:$TARGET_PORT"
        else
            echo "❌ TCP 連線失敗或逾時 (3s): $TARGET_HOST:$TARGET_PORT"
        fi
    fi
else
    echo "(未指定特定 target_host，略過外部單點連線測試)"
fi

echo "=== [SECTION:PROXY_CONFIG] ==="
echo "--- /etc/environment & /etc/profile.d Proxy 設定 ---"
grep -iE 'proxy' /etc/environment /etc/profile /etc/profile.d/*.sh /etc/bash.bashrc 2>/dev/null
echo "--- Package Manager (apt / yum / dnf) Proxy 設定 ---"
grep -iE 'proxy' /etc/apt/apt.conf /etc/apt/apt.conf.d/* /etc/yum.conf /etc/dnf/dnf.conf 2>/dev/null

echo "=== [SECTION:FIREWALL_AND_LISTENING] ==="
echo "--- Firewalld / UFW / iptables 狀態 ---"
if systemctl is-active firewalld >/dev/null 2>&1; then
    echo "firewalld: active"
    firewall-cmd --list-all 2>&1
elif command -v ufw >/dev/null 2>&1; then
    ufw status verbose 2>&1
fi
iptables -L -n -v --line-numbers 2>/dev/null | head -n 40
echo "--- 本機監聽服務 (ss -tulpn) ---"
ss -tulpn 2>&1 | head -n 40
