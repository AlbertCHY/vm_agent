#!/usr/bin/env bash
# 唯讀診斷腳本：系統健康度、磁碟空間與 Inode、Read-Only 掛載、記憶體、Top 程序、Lock 與 Kernel 錯誤
# 選填環境變數：PARTITION (指定只檢查特定掛載點或路徑)
export LANG=C LC_ALL=C

echo "=== [SECTION:BASIC] ==="
echo "Hostname: $(hostname -f 2>/dev/null || hostname)"
echo "Kernel: $(uname -srm)"
grep -E '^(PRETTY_NAME|NAME|VERSION)=' /etc/os-release 2>/dev/null | head -n 3
echo "Uptime: $(uptime)"
timedatectl status 2>/dev/null | grep -E 'Local time|System clock synchronized|NTP service' || date

echo "=== [SECTION:DF_SPACE] ==="
if [ -n "${PARTITION:-}" ]; then
    df -hP -- "$PARTITION" 2>&1
else
    df -hP -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs 2>&1
fi

echo "=== [SECTION:DF_INODE] ==="
if [ -n "${PARTITION:-}" ]; then
    df -iP -- "$PARTITION" 2>&1
else
    df -iP -x tmpfs -x devtmpfs -x squashfs -x overlay -x efivarfs 2>&1
fi

echo "=== [SECTION:RO_MOUNTS] ==="
awk '$4 ~ /(^|,)ro($|,)/ && $1 !~ /^(tmpfs|squashfs|proc|sysfs|cgroup|devtmpfs|iso9660|overlay)/ {print $1, "on", $2, "type", $3, "("$4")"}' /proc/mounts

echo "=== [SECTION:FSTAB] ==="
grep -vE '^\s*(#|$)' /etc/fstab 2>/dev/null

echo "=== [SECTION:MEMORY] ==="
free -h 2>&1

echo "=== [SECTION:TOP_CPU] ==="
ps aux --sort=-%cpu | head -n 11

echo "=== [SECTION:TOP_MEM] ==="
ps aux --sort=-%mem | head -n 11

echo "=== [SECTION:ZOMBIE_AND_LOCKS] ==="
echo "--- Zombie Processes ---"
ps -eo pid,ppid,stat,user,args | awk '$3 ~ /^Z/ {print}' | head -n 10
echo "--- Package Manager Locks ---"
find /var/lib/dpkg /var/lib/apt/lists /var/cache/apt/archives /var/run/yum.pid /var/run/dnf.pid -maxdepth 1 -name '*lock*' -o -name '*.pid' 2>/dev/null | xargs -r fuser -v 2>&1 | head -n 15

echo "=== [SECTION:KERNEL_ERRORS] ==="
dmesg -T 2>/dev/null | grep -iE 'out of memory|killed process|i/o error|ext4-fs error|xfs_force_shutdown|read-only file system|buffer i/o error' | tail -n 20
