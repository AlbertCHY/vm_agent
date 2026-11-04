#!/usr/bin/env bash
# 唯讀診斷腳本：帳號狀態、密碼修改失敗、PAM 鎖定、認證檔權限/SELinux/lsattr、sudo 與 SSH 認證日誌
# 選填環境變數：TARGET_USER (指定發生問題的目標帳號)
export LANG=C LC_ALL=C
TARGET_USER="${TARGET_USER:-}"

echo "=== [SECTION:USERS_OVERVIEW] ==="
echo "--- 一般系統帳號 (UID >= 1000 或 root) ---"
awk -F: '($3 == 0 || ($3 >= 1000 && $3 < 65534)) {print $1, "UID="$3, "GID="$4, "HOME="$6, "SHELL="$7}' /etc/passwd

if [ -n "$TARGET_USER" ]; then
    echo ""
    echo "--- 目標帳號 ($TARGET_USER) 詳細狀態 ---"
    id "$TARGET_USER" 2>&1 || echo "User $TARGET_USER does not exist!"
    getent passwd "$TARGET_USER" 2>&1
    passwd -S "$TARGET_USER" 2>&1
    echo "--- chage -l $TARGET_USER ---"
    chage -l "$TARGET_USER" 2>&1
else
    echo ""
    echo "--- 各帳號密碼狀態 (passwd -S) ---"
    for u in $(awk -F: '($3 == 0 || ($3 >= 1000 && $3 < 65534)) {print $1}' /etc/passwd); do
        passwd -S "$u" 2>&1
    done
fi

echo "=== [SECTION:LOCKOUT_STATUS] ==="
if command -v faillock >/dev/null 2>&1; then
    if [ -n "$TARGET_USER" ]; then
        faillock --user "$TARGET_USER" 2>&1
    else
        faillock 2>&1 | tail -n 30
    fi
fi
if command -v pam_tally2 >/dev/null 2>&1; then
    if [ -n "$TARGET_USER" ]; then
        pam_tally2 --user "$TARGET_USER" 2>&1
    else
        pam_tally2 2>&1 | tail -n 20
    fi
fi

echo "=== [SECTION:AUTH_FILES_PERM_AND_LOCKS] ==="
echo "--- 關鍵認證檔案權限與 SELinux Context (ls -lZ) ---"
ls -lZ /etc/passwd /etc/shadow /etc/group /etc/gshadow /etc/sudoers 2>&1
echo "--- 檢查不可變屬性 (lsattr，若有 'i' 或 'a' 將導致無法改密碼或改帳號) ---"
lsattr /etc/passwd /etc/shadow /etc/group /etc/gshadow 2>&1
echo "--- 檢查殘留 Lock 檔案 (/etc/*.lock) ---"
ls -la /etc/.pwd.lock /etc/passwd.lock /etc/shadow.lock /etc/group.lock /etc/gshadow.lock 2>/dev/null || echo "無殘留 lock 檔"
echo "--- SELinux 狀態 ---"
getenforce 2>/dev/null || sestatus 2>/dev/null || echo "SELinux 未安裝或未啟用"
echo "--- 根目錄掛載模式與可用空間 ---"
df -hP / /var /etc 2>/dev/null | uniq
grep -E '\s/\s' /proc/mounts

echo "=== [SECTION:PAM_AND_PWQUALITY] ==="
echo "--- /etc/security/pwquality.conf ---"
grep -vE '^\s*(#|$)' /etc/security/pwquality.conf /etc/security/pwquality.conf.d/*.conf 2>/dev/null || echo "(預設值，無自訂非註解設定)"
echo "--- PAM password 設定行 (/etc/pam.d/common-password | system-auth | password-auth) ---"
grep -vE '^\s*(#|$)' /etc/pam.d/common-password /etc/pam.d/common-auth /etc/pam.d/system-auth /etc/pam.d/password-auth /etc/pam.d/passwd 2>/dev/null
echo "--- /etc/login.defs 密碼期限設定 ---"
grep -E '^(PASS_MAX_DAYS|PASS_MIN_DAYS|PASS_MIN_LEN|PASS_WARN_AGE|ENCRYPT_METHOD)' /etc/login.defs 2>/dev/null

echo "=== [SECTION:SUDO_AND_HOSTNAME] ==="
echo "Hostname: $(hostname) | FQDN: $(hostname -f 2>&1)"
echo "--- /etc/hosts ---"
grep -vE '^\s*(#|$)' /etc/hosts 2>/dev/null
echo "--- /etc/sudoers 與 /etc/sudoers.d 有效設定 ---"
grep -vE '^\s*(#|$)' /etc/sudoers /etc/sudoers.d/* 2>/dev/null
if [ -n "$TARGET_USER" ]; then
    echo "--- sudo -l -U $TARGET_USER ---"
    sudo -l -U "$TARGET_USER" 2>&1
fi

echo "=== [SECTION:SSH_CONFIG_AND_KEYS] ==="
sshd -T 2>/dev/null | grep -iE '^(permitrootlogin|passwordauthentication|pubkeyauthentication|kbdinteractiveauthentication|challengeResponseAuthentication|usepam|allowusers|denyusers|allowgroups|denygroups|authorizedkeysfile|maxauthtries|pubkeyaccepted)'
if [ -n "$TARGET_USER" ]; then
    U_HOME=$(getent passwd "$TARGET_USER" | cut -d: -f6)
    if [ -n "$U_HOME" ] && [ -d "$U_HOME" ]; then
        echo "--- 家目錄與 .ssh 權限檢查 ($U_HOME) ---"
        ls -ldZ "$U_HOME" "$U_HOME/.ssh" "$U_HOME/.ssh/authorized_keys" 2>&1
        if [ -f "$U_HOME/.ssh/authorized_keys" ]; then
            echo "authorized_keys 金鑰行數: $(grep -cE '^(ssh-|ecdsa-)' "$U_HOME/.ssh/authorized_keys" 2>/dev/null)"
        fi
    fi
fi

echo "=== [SECTION:AUTH_LOGS] ==="
if [ -f /var/log/auth.log ]; then
    tail -n 45 /var/log/auth.log
elif [ -f /var/log/secure ]; then
    tail -n 45 /var/log/secure
else
    journalctl -u ssh -u sshd -u systemd-logind SYSLOG_FACILITY=10 SYSLOG_FACILITY=4 -n 45 --no-pager 2>&1
fi
