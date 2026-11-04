#!/usr/bin/env bash
# 唯讀診斷腳本：SFTP / SCP 檔案傳不進去、連線中斷、.bashrc 雜訊輸出、路徑權限鏈 (namei) 與 Quota
# 選填環境變數：TARGET_USER (傳檔帳號), TARGET_PATH (目標上傳路徑)
export LANG=C LC_ALL=C
TARGET_USER="${TARGET_USER:-}"
TARGET_PATH="${TARGET_PATH:-}"

echo "=== [SECTION:SSHD_SFTP_CONFIG] ==="
echo "--- sshd -T (SFTP / Chroot / Access 相關設定) ---"
sshd -T 2>/dev/null | grep -iE '^(subsystem|chrootdirectory|forcecommand|allowusers|denyusers|allowgroups|denygroups|allowtcpforwarding|permitopen|passwordauthentication|pubkeyauthentication)'
echo "--- /etc/ssh/sshd_config 中的 Subsystem 與 Match 區塊 ---"
grep -iE '^\s*(Subsystem|Match|ChrootDirectory|ForceCommand)' /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*.conf 2>/dev/null
SFTP_BIN=$(awk '/^\s*Subsystem\s+sftp/ {print $3}' /etc/ssh/sshd_config 2>/dev/null | head -n 1)
if [ -n "$SFTP_BIN" ] && [ "$SFTP_BIN" != "internal-sftp" ]; then
    echo "--- sftp-server 執行檔檢查 ($SFTP_BIN) ---"
    ls -l "$SFTP_BIN" 2>&1
fi

echo "=== [SECTION:SHELL_RC_NOISE] ==="
CHECK_USERS=()
if [ -n "$TARGET_USER" ]; then
    CHECK_USERS+=("$TARGET_USER")
else
    for u in $(awk -F: '($3 == 0 || ($3 >= 1000 && $3 < 65534)) {print $1}' /etc/passwd); do
        CHECK_USERS+=("$u")
    done
fi

for u in "${CHECK_USERS[@]}"; do
    U_SHELL=$(getent passwd "$u" | cut -d: -f7)
    U_HOME=$(getent passwd "$u" | cut -d: -f6)
    echo "--- 帳號 $u (Shell: $U_SHELL, Home: $U_HOME) ---"
    if [ -d "$U_HOME" ]; then
        # 測試非互動 bash 載入 .bashrc 時是否會產生 stdout 輸出 (這會破壞 SFTP 協定!)
        RC_OUT=$(sudo -n -u "$u" bash -c 'source "$HOME/.bashrc" 2>/dev/null; true' 2>&1)
        RC_BYTES=$(printf "%s" "$RC_OUT" | wc -c)
        echo "載入 ~/.bashrc 產生的輸出 Byte 數: $RC_BYTES"
        if [ "$RC_BYTES" -gt 0 ]; then
            echo "⚠️ 偵測到非互動輸出內容 (前 500 字元):"
            printf "%s\n" "$RC_OUT" | head -c 500
            echo ""
        fi
        grep -nE '^\s*(echo|printf|cat|fortune|neofetch|fastfetch|motd)' "$U_HOME/.bashrc" "$U_HOME/.bash_profile" "$U_HOME/.profile" /etc/bash.bashrc /etc/profile 2>/dev/null | head -n 20
    fi
done

echo "=== [SECTION:PATH_PERMISSIONS] ==="
PATHS_TO_CHECK=("/tmp")
if [ -n "$TARGET_PATH" ]; then
    PATHS_TO_CHECK+=("$TARGET_PATH")
fi
if [ -n "$TARGET_USER" ]; then
    UH=$(getent passwd "$TARGET_USER" | cut -d: -f6)
    [ -n "$UH" ] && PATHS_TO_CHECK+=("$UH")
fi

for p in "${PATHS_TO_CHECK[@]}"; do
    echo ">>> 檢查路徑: $p <<<"
    CUR="$p"
    while [ ! -e "$CUR" ] && [ "$CUR" != "/" ] && [ -n "$CUR" ]; do
        echo "  (路徑 $CUR 尚不存在，向上檢查父目錄)"
        CUR=$(dirname "$CUR")
    done
    echo "--- namei -l $p (展開每一層目錄的 Owner 與 rwx 權限) ---"
    namei -l "$p" 2>&1 || ls -ld "$CUR" 2>&1
    echo "--- stat & lsattr & SELinux context ($CUR) ---"
    ls -ldZ "$CUR" 2>&1
    lsattr -d "$CUR" 2>&1
    getfacl -p "$CUR" 2>/dev/null | grep -vE '^#|^$' | head -n 15
    echo "--- 該路徑所在磁區空間與 Inode ---"
    df -hP "$CUR" 2>&1
    df -iP "$CUR" 2>&1
    echo ""
done

echo "=== [SECTION:QUOTA_AND_SELINUX] ==="
echo "SELinux: $(getenforce 2>/dev/null || echo 'N/A')"
quota -v "$TARGET_USER" 2>/dev/null || repquota -a 2>/dev/null | head -n 20 || echo "未啟用磁碟 Quota"
echo "--- 近期 SELinux AVC Denied 與 SFTP/SSH 相關日誌 ---"
grep -i 'avc:.*denied' /var/log/audit/audit.log 2>/dev/null | tail -n 15
if [ -f /var/log/auth.log ]; then
    grep -iE 'sftp|scp|subsystem|chroot|error|denied|fatal' /var/log/auth.log | tail -n 25
elif [ -f /var/log/secure ]; then
    grep -iE 'sftp|scp|subsystem|chroot|error|denied|fatal' /var/log/secure | tail -n 25
else
    journalctl -u ssh -u sshd -n 30 --no-pager 2>&1
fi
