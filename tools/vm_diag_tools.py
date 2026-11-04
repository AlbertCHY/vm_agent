"""Linux VM 全方位唯讀診斷工具集 (讀取 scripts/diag/*.sh 透過 SSH Key + 免密 sudo -n 收集系統/認證/傳檔/GUI/網路與細查資訊)

每個診斷工具 = 一支唯讀腳本 + 一份報告版面 (DiagReport)；
腳本以 `=== [SECTION:NAME] ===` 分隔輸出區塊，報告版面宣告各區塊的標題與無資料時的提示。
"""
import posixpath
import re
from dataclasses import dataclass
from typing import Optional, Union

from langchain_core.tools import tool

import config
from clients.ssh_client import SSHClient, get_ssh_client

# 硬性禁止讀取內容的純機密檔案規則 (密碼雜湊與私鑰檔)；
# 同一組規則也會傳給遠端腳本，以 realpath 解析 symlink 後再比對一次
_FORBIDDEN_FILE_PATTERNS = (
    r"(^|/)shadow-?$",
    r"(^|/)gshadow-?$",
    r"(^|/)security/opasswd$",
    r"(^|/)id_(rsa|dsa|ecdsa|ed25519)(\.|$)",
    r"\.(pem|key|p12|pfx)$",
    r"(^|/)\.ssh/id_",
)
# 遠端 grep -E 使用的合併規則 (不分大小寫)
_FORBIDDEN_FILE_ERE = "|".join(_FORBIDDEN_FILE_PATTERNS)

# 遮罩 Proxy URL 內嵌密碼 (例如 http://user:pass@proxy:8080 -> http://user:***@proxy:8080)
_PROXY_PASS_RE = re.compile(r"(https?://[^/\s:@]+:)([^/\s@]+)(@)")
# 遮罩設定檔 / 環境變數中的機密值 (例如 DB_PASSWORD="xxx"、api_key: xxx、token=xxx)
_SECRET_KV_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|private[_-]?key)"
    r"[A-Za-z0-9_.-]*)(\s*[:=]\s*)([\"']?)([^\s\"']+)"
)


def mask_secrets(text: str) -> str:
    """遮罩輸出中的明文機密 (Proxy URL 密碼、key=value 形式的密碼 / Token)"""
    if not text:
        return ""
    text = _PROXY_PASS_RE.sub(r"\1***\3", text)
    return _SECRET_KV_RE.sub(r"\1\2\3***", text)


def _sanitize_text(text: str, max_chars: int = 12000) -> str:
    """遮罩明文機密並控制回傳長度上限，保留完整原始 Log 與設定供 Agent 判讀"""
    if not text:
        return ""
    cleaned = mask_secrets(text)
    if len(cleaned) > max_chars:
        cleaned = cleaned[:max_chars] + f"\n... (輸出過長，已截斷至 {max_chars} 字元)"
    return cleaned


def _load_diag_script(script_name: str) -> str:
    """讀取 scripts/diag/<script_name> 並將 Windows CRLF 正規化為 LF"""
    script_path = config.DIAG_SCRIPTS_DIR / script_name
    if not script_path.is_file():
        raise FileNotFoundError(f"找不到唯讀診斷腳本: {script_path}")
    return script_path.read_text(encoding="utf-8").replace("\r\n", "\n")


def _run_diag_script(
    ip: str,
    script_name: str,
    env: Optional[dict[str, str]] = None,
    timeout: int = 25,
) -> tuple[int, str, str, str]:
    """
    載入 scripts/diag/<script_name> 並透過 SSH 執行唯讀診斷腳本：
    1. 優先以 `sudo -n` (免密提權) 執行，以取得完整 Log 與系統狀態。
    2. 若目標機器因 sudo 被改壞或尚未配置免密 sudo 而失敗，自動降級以一般登入帳號執行，
       並回傳 sudo 失敗原因（這本身常是排障的關鍵線索）。

    Returns:
        (exit_code, stdout, stderr, priv_note)
    """
    try:
        script_content = _load_diag_script(script_name)
    except FileNotFoundError as e:
        return SSHClient.ERR_CONFIG, "", str(e), ""

    ssh = get_ssh_client()
    code, out, err = ssh.exec_command(ip, script_content, sudo=True, env=env, cmd_timeout=timeout)
    if code < 0:
        return code, out, err, ""

    # 若 sudo 本身報錯且未產出結果 (例如 /etc/sudoers 權限被改壞、無免密 sudo)，自動降級為一般權限執行
    if code != 0 and not out.strip() and "sudo" in err.lower():
        sudo_err = err.strip()
        code2, out2, err2 = ssh.exec_command(ip, script_content, sudo=False, env=env, cmd_timeout=timeout)
        note = (
            f"⚠️ 注意：`sudo -n` 提權執行失敗（錯誤：`{sudo_err}`），"
            "已自動降級使用一般 SSH 帳號權限收集資訊（部分需 root 權限的 Log 可能無法讀取）。"
        )
        return code2, out2, err2, note

    return code, out, err, "✅ 已透過免密 `sudo -n` 取得完整系統檢視權限。"


def _extract_sections(raw_output: str) -> dict[str, str]:
    """解析腳本中以 `=== [SECTION:NAME] ===` 分隔的區塊"""
    sections: dict[str, list[str]] = {}
    current = "HEADER"
    sections[current] = []
    marker_re = re.compile(r"^===\s*\[SECTION:([A-Z0-9_]+)\]\s*===$")
    for line in raw_output.splitlines():
        m = marker_re.match(line.strip())
        if m:
            current = m.group(1)
            sections.setdefault(current, [])
        else:
            sections.setdefault(current, []).append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


# ════════════════════════════════════════════════════════════════
# 報告版面宣告
# ════════════════════════════════════════════════════════════════

@dataclass(frozen=True)
class H:
    """小節標題"""
    text: str


@dataclass(frozen=True)
class P:
    """說明文字 (Markdown)"""
    text: str


@dataclass(frozen=True)
class S:
    """腳本輸出區塊 (以 code block 呈現)；區塊不存在或為空時顯示 empty"""
    key: str
    empty: str = "無資料"


ReportItem = Union[H, P, S]


@dataclass(frozen=True)
class DiagReport:
    script: str
    items: tuple[ReportItem, ...]


def _run_report(ip: str, report: DiagReport, title: str, env: Optional[dict[str, str]] = None,
                extra_lines: tuple[str, ...] = ()) -> str:
    code, out, err, priv_note = _run_diag_script(ip, report.script, env=env or None)
    if code < 0:
        return f"❌ 無法連線至 {ip}：{err}"

    sec = _extract_sections(out)
    lines = [title, priv_note, *extra_lines]
    for item in report.items:
        if isinstance(item, H):
            lines += ["", item.text]
        elif isinstance(item, P):
            lines.append(item.text)
        else:
            lines.append(f"```text\n{sec.get(item.key) or item.empty}\n```")
    if err.strip():
        lines.append(f"\n*(stderr: {err.strip()[:400]})*")
    return _sanitize_text("\n".join(lines))


SYSTEM_HEALTH = DiagReport("diag_system_health.sh", (
    H("### 1. 系統基本資訊與時間同步"), S("BASIC"),
    H("### 2. 磁碟空間 (`df -hP`) 與 Inode (`df -iP`) 使用狀況"),
    P("**[磁碟容量 (df -hP)]**"), S("DF_SPACE"),
    P("**[Inode 使用率 (df -iP)]**"), S("DF_INODE"),
    P("**[唯讀 (Read-Only) 實體掛載檢查]**"), S("RO_MOUNTS", "(無異常的 Read-Only 實體掛載)"),
    H("### 3. `/etc/fstab` 掛載設定"), S("FSTAB", "(無內容)"),
    H("### 4. 記憶體與 Swap 使用量 (`free -h`)"), S("MEMORY"),
    H("### 5. Top 10 CPU / Memory 耗用程序 (`ps aux`)"),
    P("**[Top CPU]**"), S("TOP_CPU"),
    P("**[Top Memory]**"), S("TOP_MEM"),
    H("### 6. 殭屍程序與套件管理員 Lock / 核心底層錯誤 (OOM / I/O Error)"), S("ZOMBIE_AND_LOCKS", "(無異常)"),
    P("**[dmesg 近期 OOM / I/O / 檔案系統錯誤]**"), S("KERNEL_ERRORS", "(無近期 OOM 或磁碟 I/O 錯誤紀錄)"),
))

AUTH_ISSUE = DiagReport("diag_auth_issue.sh", (
    H("### 1. 帳號狀態與密碼期限 (`id` / `passwd -S` / `chage -l`)"), S("USERS_OVERVIEW"),
    H("### 2. PAM 登入失敗鎖定紀錄 (`faillock` / `pam_tally2`)"), S("LOCKOUT_STATUS", "(無鎖定紀錄或未啟用 faillock)"),
    H("### 3. `/etc/passwd`、`/etc/shadow` 權限、SELinux 標籤、`lsattr` 屬性與 Lock 檔"), S("AUTH_FILES_PERM_AND_LOCKS"),
    H("### 4. PAM 規則與密碼複雜度設定 (`pwquality.conf` / `/etc/pam.d/*` / `login.defs`)"), S("PAM_AND_PWQUALITY"),
    H("### 5. `sudo` 權限與 Hostname 解析 (`/etc/hosts` / `sudoers`)"), S("SUDO_AND_HOSTNAME"),
    H("### 6. SSHD 認證設定與家目錄 `.ssh` 權限"), S("SSH_CONFIG_AND_KEYS"),
    H("### 7. 近期認證日誌 (`/var/log/secure` 或 `/var/log/auth.log` 最後 45 行)"), S("AUTH_LOGS"),
))

FILE_TRANSFER = DiagReport("diag_file_transfer.sh", (
    H("### 1. SSHD 與 SFTP Subsystem 設定 (`sshd -T` & `sshd_config`)"), S("SSHD_SFTP_CONFIG"),
    H("### 2. 非互動式 Shell (`.bashrc` / `.profile`) 雜訊輸出檢測"),
    P("> 若載入 `.bashrc` 產生任何文字輸出 (Byte 數 > 0)，SFTP 連線會在握手階段立即斷線 (`Received message too long`)。"),
    S("SHELL_RC_NOISE"),
    H("### 3. 目標路徑權限鏈 (`namei -l`)、不可變屬性與所在磁區容量/Inode"), S("PATH_PERMISSIONS"),
    H("### 4. 磁碟配額 (Quota)、SELinux 與近期 SFTP/SSH 錯誤日誌"), S("QUOTA_AND_SELINUX"),
))

GUI_ISSUE = DiagReport("diag_gui_issue.sh", (
    H("### 1. Systemd Target 與 Display Manager / 遠端桌面服務狀態"), S("TARGET_AND_DM"),
    H("### 2. 遠端桌面監聽 Port (3389/590x)、運行中 GUI 程序與 Sessions"), S("PORTS_AND_SESSIONS"),
    H("### 3. 家目錄 `.Xauthority` 權限、Owner 異常與 `/tmp` / 空間檢查（登入閃退常見根因）"), S("XAUTH_AND_TMP_PERMS"),
    H("### 4. Xorg / XRDP / `.xsession-errors` / Systemd 錯誤日誌"), S("GUI_LOGS"),
))

NETWORK = DiagReport("diag_network.sh", (
    H("### 1. 網卡狀態、路由表與 Default Gateway 連通性"), S("IFACE_AND_ROUTE"),
    H("### 2. DNS 設定 (`/etc/resolv.conf` / `resolvectl` / `hosts`) 與解析測試"), S("DNS_CHECK"),
    H("### 3. 指定目標主機 / Port 連通性測試"), S("TARGET_CONNECTIVITY"),
    H("### 4. 系統與套件管理員 Proxy 設定"), S("PROXY_CONFIG", "(未設定系統級 Proxy)"),
    H("### 5. 本機防火牆規則與監聽服務 (`ss -tulpn`)"), S("FIREWALL_AND_LISTENING"),
))


def _opt_env(**kwargs: Optional[str]) -> dict[str, str]:
    return {k: v.strip() for k, v in kwargs.items() if v and v.strip()}


# ════════════════════════════════════════════════════════════════
# 1. 系統健康度與磁碟空間診斷 (整合原 check_vm_disk_usage)
# ════════════════════════════════════════════════════════════════

@tool
def diagnose_vm_system_health(ip: str, partition: Optional[str] = None) -> str:
    """全面檢查目標 Linux VM 的系統健康度與磁碟空間（取代原磁碟檢查工具）。
    適用情境：
    1. 檢查磁碟容量 (`df -h`) 與 Inode 使用率 (`df -i`)，可指定特定 partition。
    2. 使用者反應系統卡頓、很慢、負載高、記憶體不足、開機或掛載異常。
    3. 任何其他故障（GUI 進不去、改不了密碼、傳不進檔案）的底層根因排查（如磁碟滿載、磁區變 Read-Only、OOM Killer、套件鎖定）。

    參數:
    - ip: 目標主機 IP。
    - partition: (選填) 只檢查特定掛載點或路徑，例如 `/` 或 `/data`；不填則檢查全部實體磁區與完整系統健康度。"""
    scope = f"指定路徑: {partition}" if partition else "全部實體磁區"
    return _run_report(
        ip, SYSTEM_HEALTH, f"🖥️ **主機 `{ip}` 系統健康度與磁碟空間診斷報告**",
        env=_opt_env(PARTITION=partition), extra_lines=(f"- 磁碟檢查範圍：{scope}",),
    )


# ════════════════════════════════════════════════════════════════
# 2. 帳號、登入認證、密碼無法修改與 sudo 異常診斷
# ════════════════════════════════════════════════════════════════

@tool
def diagnose_vm_auth_issue(ip: str, target_user: Optional[str] = None) -> str:
    """診斷目標 Linux VM 的「帳號無法登入、密碼無法修改 (passwd 失敗)、帳號被鎖定、SSH 金鑰失效或 sudo 權限異常」問題。
    會一次檢查：帳號狀態 (`passwd -S`, `chage -l`)、PAM 失敗鎖定 (`faillock`/`pam_tally2`)、
    `/etc/passwd` 與 `/etc/shadow` 的權限/SELinux標籤/`lsattr` 屬性（不讀取密碼雜湊本身）、
    `.pwd.lock` 殘留、PAM 密碼複雜度規則 (`pwquality.conf`, `/etc/pam.d/*`)、
    Hostname 與 `/etc/hosts` 一致性、`sudo` 設定、SSH 設定 (`sshd -T`) 與 `~/.ssh` 權限，以及近期認證 Log。

    參數:
    - ip: 目標主機 IP。
    - target_user: (選填) 發生問題的目標帳號名稱（如 `user`、`admin`、`root`）。若不填則列出系統上所有可登入的一般帳號並檢查整體認證環境。"""
    target_title = f" (目標帳號: `{target_user}`)" if target_user else " (全機認證環境)"
    return _run_report(
        ip, AUTH_ISSUE, f"🔐 **主機 `{ip}` 帳號、密碼修改與登入認證診斷報告{target_title}**",
        env=_opt_env(TARGET_USER=target_user),
    )


# ════════════════════════════════════════════════════════════════
# 3. 檔案傳輸異常診斷 (SFTP / SCP 傳不進去、連線中斷、權限拒絕)
# ════════════════════════════════════════════════════════════════

@tool
def diagnose_vm_file_transfer(
    ip: str,
    target_user: Optional[str] = None,
    target_path: Optional[str] = None,
) -> str:
    """診斷目標 Linux VM 的「檔案傳不進去 (SFTP / SCP / WinSCP / FileZilla 失敗或斷線)」問題。
    會一次檢查：
    1. SSHD 的 `Subsystem sftp`、`ChrootDirectory`、`AllowUsers`/`DenyUsers` 設定，以及 `sftp-server` 執行檔是否存在。
    2. 非互動式 Shell (`.bashrc` / `.bash_profile` / `/etc/profile`) 是否有印出雜訊文字（會直接破壞 SFTP 封包協定導致傳檔秒斷），並抓出實際印出的文字內容。
    3. 目標上傳路徑（及家目錄、`/tmp`）每一層目錄的完整權限鏈 (`namei -l`)、Owner、`lsattr` 不可變屬性與 ACL。
    4. 目標路徑所在磁區的剩餘空間、Inode 使用率、Read-Only 狀態與磁碟配額 (`quota`)。
    5. SELinux 狀態與近期 AVC denied 攔截紀錄。

    參數:
    - ip: 目標主機 IP。
    - target_user: (選填) 使用者用來傳檔的登入帳號。
    - target_path: (選填) 使用者嘗試上傳檔案的目標目錄或檔案路徑（例如 `/data/upload` 或 `/home/user/app`）。"""
    return _run_report(
        ip, FILE_TRANSFER, f"📂 **主機 `{ip}` 檔案傳輸 (SFTP / SCP) 異常診斷報告**",
        env=_opt_env(TARGET_USER=target_user, TARGET_PATH=target_path),
        extra_lines=(
            f"- 檢查帳號：`{target_user or '(未指定，檢查全機一般帳號)'}` | "
            f"目標路徑：`{target_path or '(未指定，預設檢查家目錄與 /tmp)'}`",
        ),
    )


# ════════════════════════════════════════════════════════════════
# 4. Linux GUI / 桌面環境 / VNC / XRDP 異常診斷
# ════════════════════════════════════════════════════════════════

@tool
def diagnose_vm_gui_issue(ip: str, target_user: Optional[str] = None) -> str:
    """診斷目標 Linux VM 的「GUI 桌面異常、黑畫面、卡在登入畫面、登入後瞬間閃退、或 XRDP / VNC 連不上」問題。
    會一次檢查：
    1. Systemd 預設 Target (`systemctl get-default` 是否為 `graphical.target`) 與已安裝的桌面環境套件。
    2. Display Manager (`gdm`/`gdm3`/`lightdm`/`sddm`) 與遠端桌面服務 (`xrdp`/`xrdp-sesman`/`vncserver`) 狀態及監聽 Port (`3389`, `590x`)。
    3. 登入閃退頭號元凶檢查：使用者家目錄下 `.Xauthority`、`.ICEauthority`、`.config`、`.vnc` 的 Owner 是否變成 `root` 或權限錯誤，以及 `/tmp/.X11-unix` 權限與磁碟/Inode 是否 100% 滿。
    4. 目前活動中的 GUI Session (`loginctl`) 與 Xorg / Wayland 程序。
    5. `/var/log/Xorg*.log`、`/var/log/xrdp*.log`、`journalctl` 與 `~/.xsession-errors` 的最新錯誤訊息。

    參數:
    - ip: 目標主機 IP。
    - target_user: (選填) 無法登入 GUI 的目標使用者帳號。"""
    return _run_report(
        ip, GUI_ISSUE, f"🖼️ **主機 `{ip}` GUI 桌面環境 / VNC / XRDP 異常診斷報告**",
        env=_opt_env(TARGET_USER=target_user),
    )


# ════════════════════════════════════════════════════════════════
# 5. 網路、DNS、Proxy、防火牆與連外異常診斷
# ════════════════════════════════════════════════════════════════

@tool
def diagnose_vm_network(
    ip: str,
    target_host: Optional[str] = None,
    target_port: Optional[int] = None,
) -> str:
    """診斷目標 Linux VM 的「網路連外異常、DNS 無法解析、Proxy 設定問題、防火牆攔截、或連不上特定目標 Host/Port」問題。
    會一次檢查：
    1. 網卡狀態 (`ip -br link`, `ip -br addr`)、路由表 (`ip route`)，並自動 Ping Default Gateway 確認內網閘道連通。
    2. DNS 設定 (`/etc/resolv.conf`, `resolvectl`, `/etc/nsswitch.conf`, `/etc/hosts`) 並測試 DNS 解析與連通每個設定的 DNS Server (`53` port)。
    3. 系統與套件管理器的 Proxy 設定 (`/etc/environment`, `/etc/profile.d/*`, `apt`/`yum`/`dnf` proxy)。
    4. 本機防火牆 (`firewalld`, `ufw`, `iptables`) 規則與本機監聽 Port (`ss -tulpn`)。
    5. 若有提供 `target_host`（與選填 `target_port`），會從該 VM 測試對目標的 DNS 解析、Ping 與 TCP Port 連線。

    參數:
    - ip: 目標主機 IP。
    - target_host: (選填) 想從該 VM 測試連線的目標網域或 IP（例如 `www.google.com`、`10.50.1.10` 或內部 Repo 主機）。
    - target_port: (選填) 想測試的目標 TCP Port（例如 `443`、`80`、`53`、`3306`）。"""
    if target_host and not re.match(r"^[a-zA-Z0-9._-]+$", target_host.strip()):
        return f"❌ `target_host` 格式不合法：{target_host}"
    if target_port is not None and not (1 <= int(target_port) <= 65535):
        return f"❌ `target_port` 必須介於 1 ~ 65535：{target_port}"

    env = _opt_env(TARGET_HOST=target_host)
    if target_port is not None:
        env["TARGET_PORT"] = str(int(target_port))
    return _run_report(ip, NETWORK, f"🌐 **主機 `{ip}` 網路、DNS、Proxy 與防火牆診斷報告**", env=env)


# ════════════════════════════════════════════════════════════════
# 6. 細部唯讀追蹤與設定複查工具 (支援邊修邊聊、即時驗證)
# ════════════════════════════════════════════════════════════════

def is_forbidden_path(path: str) -> bool:
    norm = posixpath.normpath(path)
    return any(re.search(pat, norm, re.IGNORECASE) for pat in _FORBIDDEN_FILE_PATTERNS)


@tool
def inspect_vm_detail(
    ip: str,
    check_type: str,
    target: str,
    lines: int = 80,
) -> str:
    """當需要深入檢查某個特定服務、特定 Log 檔、特定設定檔或目錄，或是使用者在終端機修改設定後要求「幫我再看一次最新狀態」時呼叫。
    此工具為完全唯讀；禁止讀取密碼雜湊 (`/etc/shadow`) 與私鑰檔 (`id_rsa`, `*.pem`, `*.key`)，
    且會自動遮罩輸出中的密碼 / Token 等機密值。

    參數:
    - ip: 目標主機 IP。
    - check_type: 查詢類型，必須為以下四種之一：
        * `"service"`：查看指定 systemd 服務的狀態 (`systemctl status <target>`)。
        * `"journal"`：查看指定服務或關鍵字的 `journalctl` 日誌 (`journalctl -u <target> -n <lines>`)。
        * `"file"`：查看指定設定檔、Log 檔或目錄內容（例如 `/etc/ssh/sshd_config`、`/var/log/messages`、`/home/user/.bashrc`）。
        * `"process_or_port"`：查看特定程序名稱或 Port 號目前的運行與監聽狀況（例如 `xrdp`、`3389`、`python`）。
    - target: 目標名稱或路徑（例如服務名 `sshd`、檔案路徑 `/etc/pam.d/system-auth`、或 Port `8080`）。
    - lines: (選填) 讀取日誌或檔案的最大行數，預設 80 行（上限 250 行）。"""
    ctype = (check_type or "").strip().lower()
    tgt = (target or "").strip()
    if not tgt:
        return "❌ 請提供 `target`（服務名稱、檔案路徑或關鍵字）。"

    max_lines = max(10, min(int(lines or 80), 250))

    if ctype in ("service", "journal"):
        if not re.match(r"^[a-zA-Z0-9_.@-]+$", tgt):
            return f"❌ 無效的服務名稱格式：{tgt}"
    elif ctype == "file":
        if is_forbidden_path(tgt):
            return f"🚫 基於安全底線，禁止讀取密碼雜湊或私鑰檔案內容 (`{tgt}`)。若需確認其權限，請使用 `diagnose_vm_auth_issue`。"
        tgt = posixpath.normpath(tgt)
    elif ctype == "process_or_port":
        if not re.match(r"^[a-zA-Z0-9_.:-]+$", tgt):
            return f"❌ 無效的程序或 Port 關鍵字：{tgt}"
    else:
        return (
            f"❌ 不支援的 `check_type`: `{check_type}`。"
            "請使用 `service`、`journal`、`file` 或 `process_or_port`。"
        )

    env = {
        "CHECK_TYPE": ctype,
        "TARGET": tgt,
        "MAX_LINES": str(max_lines),
        # 遠端以 realpath 解析 symlink 後再比對一次，防止透過連結繞過
        "FORBIDDEN_REGEX": _FORBIDDEN_FILE_ERE,
    }
    code, out, err, priv_note = _run_diag_script(ip, "diag_inspect_detail.sh", env=env)
    if code < 0:
        return f"❌ 無法連線至 {ip}：{err}"

    body = out.strip() or err.strip() or "(無輸出)"
    return _sanitize_text(
        f"🔍 **主機 `{ip}` 細部檢查 (`{ctype}`: `{tgt}`)**\n{priv_note}\n\n```text\n{body}\n```"
    )
