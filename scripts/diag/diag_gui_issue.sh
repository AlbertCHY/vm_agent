#!/usr/bin/env bash
# 唯讀診斷腳本：Linux GUI 桌面環境、VNC / XRDP 狀態、.Xauthority 權限與 Xorg / .xsession-errors 日誌
# 選填環境變數：TARGET_USER (無法登入 GUI 的目標帳號)
export LANG=C LC_ALL=C
TARGET_USER="${TARGET_USER:-}"

echo "=== [SECTION:TARGET_AND_DM] ==="
echo "Default Target: $(systemctl get-default 2>&1)"
echo "--- Display Manager 符號連結與服務狀態 ---"
ls -l /etc/systemd/system/display-manager.service 2>/dev/null || echo "無 display-manager.service 連結"
cat /etc/X11/default-display-manager 2>/dev/null
for svc in display-manager gdm gdm3 lightdm sddm xdm xrdp xrdp-sesman vncserver@:1; do
    if systemctl list-unit-files "$svc.service" >/dev/null 2>&1 || systemctl status "$svc" >/dev/null 2>&1; then
        echo ">>> systemctl status $svc <<<"
        systemctl status "$svc" --no-pager -n 8 2>&1 | head -n 16
    fi
done

echo "=== [SECTION:PORTS_AND_SESSIONS] ==="
echo "--- 遠端桌面與 X11 相關監聽 Port (3389 / 5900-5910 / 6000) ---"
ss -tlnp 2>&1 | grep -E 'State|:3389\b|:3350\b|:590[0-9]\b|:600[0-9]\b|xrdp|Xvnc|Xorg|Xtightvnc'
echo "--- 目前運行中的 GUI / Xorg / Wayland / 桌面程序 ---"
ps aux | grep -iE 'Xorg|Xwayland|Xvnc|xrdp|gdm|lightdm|sddm|gnome-shell|xfce4-session|mate-session|startplasma|dbus-daemon' | grep -v grep | head -n 25
echo "--- loginctl list-sessions ---"
loginctl list-sessions --no-legend 2>&1 | head -n 15

echo "=== [SECTION:XAUTH_AND_TMP_PERMS] ==="
echo "--- /tmp 與 X11 Socket 目錄權限 (正常應為 drwxrwxrwt / 1777) ---"
ls -ld /tmp /tmp/.X11-unix /tmp/.ICE-unix /var/tmp /run/user/* 2>&1
echo "--- 磁碟空間與 Inode (若 / 或 /home 滿 100% 會導致無法建立 X session 而閃退) ---"
df -hP / /home /tmp /var 2>/dev/null | uniq
df -iP / /home /tmp /var 2>/dev/null | uniq

CHECK_USERS=()
if [ -n "$TARGET_USER" ]; then
    CHECK_USERS+=("$TARGET_USER")
else
    for u in $(awk -F: '($3 >= 1000 && $3 < 65534) {print $1}' /etc/passwd); do
        CHECK_USERS+=("$u")
    done
fi

for u in "${CHECK_USERS[@]}"; do
    U_HOME=$(getent passwd "$u" | cut -d: -f6)
    if [ -n "$U_HOME" ] && [ -d "$U_HOME" ]; then
        echo "--- 使用者 $u ($U_HOME) GUI 關鍵檔案權限檢查 ---"
        ls -ld "$U_HOME" "$U_HOME/.Xauthority" "$U_HOME/.ICEauthority" "$U_HOME/.xsession" "$U_HOME/.xinitrc" "$U_HOME/.vnc" "$U_HOME/.config" "$U_HOME/.local" 2>/dev/null
        echo "--- 檢查 $U_HOME 下是否有被 root 誤搶走 Owner 的隱藏檔 ---"
        find "$U_HOME" -maxdepth 2 -user root -ls 2>/dev/null | head -n 15
    fi
done

echo "=== [SECTION:GUI_LOGS] ==="
echo "--- /var/log/Xorg.0.log 錯誤 (EE) 與警告 ---"
grep -E '\(EE\)|Fatal|Failed' /var/log/Xorg.0.log /var/log/Xorg.*.log ~/.local/share/xorg/Xorg.*.log 2>/dev/null | tail -n 25
echo "--- /var/log/xrdp*.log 近期紀錄 ---"
tail -n 20 /var/log/xrdp.log /var/log/xrdp-sesman.log 2>/dev/null
echo "--- 使用者 ~/.xsession-errors 近期錯誤 ---"
for u in "${CHECK_USERS[@]}"; do
    U_HOME=$(getent passwd "$u" | cut -d: -f6)
    if [ -f "$U_HOME/.xsession-errors" ]; then
        echo ">>> $U_HOME/.xsession-errors <<<"
        tail -n 25 "$U_HOME/.xsession-errors" 2>/dev/null
    fi
done
echo "--- journalctl Display Manager / XRDP / GNOME 近期錯誤 ---"
journalctl -p err..alert -n 25 --no-pager 2>/dev/null
