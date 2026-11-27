"""VM Bootstrap 子圖節點 (支援 Linux 與 Windows)

prepare_bootstrap → human_review (HITL) → connect_and_upload → run_step (逐步迴圈) → cleanup → finalize

- 步驟清單依作業系統載入 policies/bootstrap_steps_linux.toml (Linux) 或 policies/bootstrap_steps_windows.toml (Windows)，
  每完成一步即寫入 checkpoint，可從中斷處續跑。
- 密碼只存在 SecretStore (記憶體)，state 只保存 secret_ref；程式重啟後參照失效時會重新產生密碼並再次審核。
- 密碼與 CrowdStrike Token 以 secret_env 傳遞 (暫存檔載入後立即刪除)，不出現在遠端命令列。
"""
import re
from pathlib import Path
from typing import Any, Optional

from langchain_core.messages import AIMessage
from langgraph.graph import END

import config
from clients.aria import get_aria_client
from clients.ssh_client import get_ssh_client
from core.progress import emit
from core.secrets import get_audit_sink, get_credential_sink, get_secret_store
from scripts.passwd_creator import generate_vm_passwords
from subgraphs.common.review import (
    APPROVE, EDIT_REVIEW, REGENERATE, SECRET_REF_PREFIX, SKIP, ReviewField, ask_review, review_request,
)
from subgraphs.common.runner import is_cancelled
from subgraphs.vm_bootstrap.state import BootstrapState
from subgraphs.vm_bootstrap.steps import (
    SUPPORTED_OS_TYPES,
    StepDef,
    get_bootstrap_plan,
    normalize_os_type,
    render,
    render_env,
)

CS_MODES = ("NGAV", "MDR", "SKIP")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_LINUX_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
_WIN_USERNAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9._-]{0,31}$")


def _os_type(state: BootstrapState) -> str:
    return normalize_os_type(state.get("os_type"))


LINUX_CS_SUBDIRS = ("Ubuntu", "RHEL_8", "RHEL_9")
SSHD_RESTART_MARKER = "[SSHD_RESTART_SCHEDULED]"


def _is_windows(state: BootstrapState) -> bool:
    return _os_type(state) == "windows"


def _scripts_dir(os_type: str) -> Path:
    return config.BOOTSTRAP_WINDOWS_SCRIPTS_DIR if os_type == "windows" else config.BOOTSTRAP_LINUX_SCRIPTS_DIR


def _cs_installers_ready(os_type: str) -> bool:
    base = config.CROWDSTRIKE_INSTALLERS_DIR
    if not base.is_dir():
        return False
    if os_type == "windows":
        win_dir = base / "Windows"
        return win_dir.is_dir() and any(win_dir.glob("FalconSensor_Windows*.exe"))
    return any((base / sub).is_dir() for sub in LINUX_CS_SUBDIRS)


def detect_linux_cs_subdir(os_release_text: str) -> Optional[str]:
    """依 /etc/os-release 內容判斷對應的 Linux CrowdStrike 安裝檔子目錄 (Ubuntu / RHEL_8 / RHEL_9)"""
    kv: dict[str, str] = {}
    for line in (os_release_text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        kv[k.strip()] = v.strip().strip('"').strip("'").lower()

    os_id = kv.get("ID", "")
    os_like = kv.get("ID_LIKE", "")
    os_ver = kv.get("VERSION_ID", "")
    if not os_id and not os_like:
        return None

    if os_id in ("ubuntu", "debian") or "debian" in os_like or "ubuntu" in os_like:
        return "Ubuntu"
    if os_id in ("centos", "rhel", "rocky", "oracle", "ol", "almalinux") or "rhel" in os_like or "fedora" in os_like:
        if os_ver.startswith("8"):
            return "RHEL_8"
        if os_ver.startswith("9"):
            return "RHEL_9"
    return None


def _resolve_remote_dirs(state: BootstrapState) -> tuple[str, str, str, str]:
    """回傳 (remote_dir, crowdstrike_remote_dir, sccm_remote_dir, secret_temp_dir)"""
    os_type = _os_type(state)
    plan = get_bootstrap_plan(os_type)
    base_ctx = {
        "init_user": state.get("init_user") or config.BOOTSTRAP_INIT_USER,
        "user1": state.get("user1") or config.BOOTSTRAP_USER1,
    }
    remote_dir = (state.get("remote_dir") or render(plan.remote_dir, base_ctx)).rstrip("/")
    secret_temp_dir = (state.get("secret_temp_dir") or render(plan.secret_temp_dir, base_ctx)).rstrip("/")
    if state.get("remote_dir"):
        cs_remote = f"{remote_dir}/CrowdStrike"
        sccm_remote = f"{remote_dir}/SCCM" if plan.sccm_remote_dir else ""
    else:
        cs_remote = render(plan.crowdstrike_remote_dir, base_ctx).rstrip("/")
        sccm_remote = render(plan.sccm_remote_dir, base_ctx).rstrip("/") if plan.sccm_remote_dir else ""
    return remote_dir, cs_remote, sccm_remote, secret_temp_dir


def _detect_os_from_aria(vm_info: dict[str, Any]) -> Optional[str]:
    machine = vm_info.get("machine") or {}
    raw = str(machine.get("os_type") or machine.get("osFamily") or machine.get("image") or "").strip().lower()
    if not raw:
        return None
    return "windows" if "win" in raw else "linux"


def _resolve_pub_key() -> str:
    """優先讀取 .env 的 SSH_PUBLIC_KEY，其次讀取 SSH_PUBLIC_KEY_PATH 檔案內容"""
    if config.SSH_PUBLIC_KEY:
        return config.SSH_PUBLIC_KEY
    if config.SSH_PUBLIC_KEY_PATH.is_file():
        try:
            return config.SSH_PUBLIC_KEY_PATH.read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return ""


def _new_secrets(old_ref: Optional[str] = None) -> str:
    store = get_secret_store()
    store.delete(old_ref)
    return store.put(generate_vm_passwords(config.BOOTSTRAP_PASSWORD_LENGTH))


def _secrets(state: BootstrapState) -> Optional[dict[str, str]]:
    return get_secret_store().get(state.get("secret_ref"))


def _login_password(state: BootstrapState) -> Optional[str]:
    mode = state.get("login_mode") or "init"
    if mode == "key":
        return None
    if mode == "admin":
        return (_secrets(state) or {}).get("admin_password")
    return config.BOOTSTRAP_INIT_PASSWORD or None


def _public_context(state: BootstrapState) -> dict[str, Any]:
    os_type = _os_type(state)
    _remote_dir, cs_remote_dir, sccm_remote_dir, _secret_temp_dir = _resolve_remote_dirs(state)
    cs_skip = (state.get("cs_mode") or "").upper() == "SKIP"
    return {
        "ip": state.get("ip", ""),
        "os_type": os_type,
        "hostname": state.get("hostname", ""),
        "init_user": state.get("init_user", ""),
        "user1": state.get("user1", ""),
        "user2": state.get("user2", ""),
        "pub_key": _resolve_pub_key(),
        "cs_mode": state.get("cs_mode", ""),
        "cs_proxy_host": config.CS_PROXY_HOST,
        "cs_proxy_port": config.CS_PROXY_PORT,
        "zabbix_servers": config.ZABBIX_SERVERS,
        "zabbix_conf": config.WINDOWS_ZABBIX_CONF_PATH if os_type == "windows" else config.LINUX_ZABBIX_CONF_PATH,
        "windows_dns_servers": config.WINDOWS_DNS_SERVERS,
        "sccm_site_code": config.WINDOWS_SCCM_SITE_CODE,
        "sccm_fsp": config.WINDOWS_SCCM_FSP,
        "sccm_smsmp": config.WINDOWS_SCCM_SMSMP,
        "sccm_remote_dir": sccm_remote_dir,
        "crowdstrike_remote_dir": cs_remote_dir,
        "extra_disk_start_letter": config.WINDOWS_EXTRA_DISK_START_LETTER,
        "cs_skip": cs_skip,
        "cs_skip_str": "true" if cs_skip else "false",
        "disk_skip": bool(state.get("skip_disk", False)),
    }


def _secret_context(state: BootstrapState) -> dict[str, Any]:
    pw = _secrets(state) or {}
    return {
        **_public_context(state),
        "admin_password": pw.get("admin_password", ""),
        "user2_password": pw.get("user2_password", ""),
        "mdr_cid": config.MDR_CID,
        "mdr_token": config.MDR_TOKEN,
        "ngav_cid": config.NGAV_CID,
        "ngav_token": config.NGAV_TOKEN,
    }


def _record_credentials(state: BootstrapState) -> str:
    pw = _secrets(state) or {}
    user1, user2 = state.get("user1", ""), state.get("user2", "")
    admin_label = user1 if _is_windows(state) else f"root / {user1}"
    location = get_credential_sink().record(
        state.get("ip", ""), state.get("hostname", ""),
        {admin_label: pw.get("admin_password", ""), user2: pw.get("user2_password", "")},
    )
    get_audit_sink().record("vm_bootstrap_credentials_rotated", ip=state.get("ip"), hostname=state.get("hostname"))
    return location


def _fail(msg: str, state: BootstrapState) -> dict[str, Any]:
    return {
        "execution_result": {"success": False, "error": msg, "steps": state.get("step_results") or []},
        "messages": [AIMessage(content=msg)],
    }


# 1. 準備階段：由 IP 查詢 Aria Deployment 名稱與作業系統類型，並於本機產生兩組強密碼
def prepare_bootstrap(state: BootstrapState) -> dict[str, Any]:
    ip = (state.get("ip") or "").strip()
    hostname = (state.get("hostname") or "").strip() or None
    raw_os = (state.get("os_type") or "").strip().lower()
    os_type = raw_os if raw_os in SUPPORTED_OS_TYPES else None
    cs_mode = (state.get("cs_mode") or config.CROWDSTRIKE_DEFAULT_MODE or "NGAV").upper()
    if cs_mode not in CS_MODES:
        cs_mode = "NGAV"

    # 若未指定 hostname 或 os_type，自動透過 Aria API 以 IP 反查
    if ip and (not hostname or not os_type):
        try:
            vm_info = get_aria_client().query_vm_by_ip(ip)
            if vm_info.get("found"):
                if not hostname:
                    hostname = (vm_info.get("deployment") or {}).get("name") or (vm_info.get("machine") or {}).get("name")
                if not os_type:
                    os_type = _detect_os_from_aria(vm_info)
        except Exception:
            pass
    if not hostname:
        hostname = f"vm-{ip.replace('.', '-')}"
    if not os_type:
        os_type = "linux"

    secret_ref = state.get("secret_ref")
    if not get_secret_store().has(secret_ref):
        secret_ref = _new_secrets(secret_ref)

    return {
        "ip": ip,
        "os_type": os_type,
        "hostname": hostname,
        "cs_mode": cs_mode,
        "skip_disk": bool(state.get("skip_disk", False)),
        "init_user": state.get("init_user") or config.BOOTSTRAP_INIT_USER,
        "user1": state.get("user1") or config.BOOTSTRAP_USER1,
        "user2": state.get("user2") or config.BOOTSTRAP_USER2,
        "secret_ref": secret_ref,
        "validation_errors": [],
    }


# ─── 人工審核 ───
_SECRET_EDIT_KEYS = ("admin_password", "user2_password")


def _extract_secret_edit(key: str, raw: str) -> Optional[str]:
    """取出機密欄位修改值；若為 CLI 轉存的 secret_ref:<id>，取回後立即刪除暫存參照"""
    if raw.startswith(SECRET_REF_PREFIX):
        temp_ref = raw[len(SECRET_REF_PREFIX):]
        store = get_secret_store()
        data = store.get(temp_ref)
        store.delete(temp_ref)
        return (data or {}).get(key)
    return raw


def apply_edits(state: BootstrapState, edits: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    updates: dict[str, Any] = {}
    errors: list[str] = []
    secret_updates: dict[str, str] = {}
    # 若本次同時修改 os_type，先決定有效作業系統供帳號格式驗證使用
    effective_os = _os_type(state)
    raw_os_edit = (edits or {}).get("os_type")
    if raw_os_edit and raw_os_edit.strip().lower() in SUPPORTED_OS_TYPES:
        effective_os = raw_os_edit.strip().lower()
    user_re = _WIN_USERNAME_RE if effective_os == "windows" else _LINUX_USERNAME_RE

    for key, raw in (edits or {}).items():
        if key in _SECRET_EDIT_KEYS:
            pw = _extract_secret_edit(key, (raw or "").strip())
            if not pw:
                errors.append(f"{key} 不可為空")
            else:
                secret_updates[key] = pw
            continue
        val = (raw or "").strip()
        if key == "hostname":
            if _HOSTNAME_RE.match(val):
                updates["hostname"] = val
            else:
                errors.append(f"hostname 格式不合法：{val}")
        elif key == "os_type":
            if val.lower() in SUPPORTED_OS_TYPES:
                updates["os_type"] = val.lower()
            else:
                errors.append(f"os_type 必須是 {'/'.join(SUPPORTED_OS_TYPES)}：{val}")
        elif key == "cs_mode":
            if val.upper() in CS_MODES:
                updates["cs_mode"] = val.upper()
            else:
                errors.append(f"cs_mode 必須是 {'/'.join(CS_MODES)}：{val}")
        elif key == "skip_disk":
            updates["skip_disk"] = val.lower() in ("true", "1", "yes", "y")
        elif key in ("user1", "user2", "init_user"):
            if user_re.match(val):
                updates[key] = val
            else:
                errors.append(f"{key} 帳號名稱格式不合法：{val}")
        else:
            errors.append(f"不可修改或未知的欄位：{key}")

    if secret_updates:
        store = get_secret_store()
        ref = state.get("secret_ref")
        current = store.get(ref) or {}
        current.update(secret_updates)
        updates["secret_ref"] = store.put(current, ref=ref)
    return updates, errors


def build_review_fields(state: BootstrapState) -> list[ReviewField]:
    os_type = _os_type(state)
    is_win = os_type == "windows"
    plan = get_bootstrap_plan(os_type)
    remote_dir, _, _, _ = _resolve_remote_dirs(state)
    ref = state.get("secret_ref") or ""
    user1, user2 = state.get("user1"), state.get("user2")
    cs_mode = state.get("cs_mode", "NGAV")
    cs_ready = "✔ 已就緒" if _cs_installers_ready(os_type) else "⚠️ 本機未找到對應 CrowdStrike 安裝檔"
    fields = [
        ReviewField(key="ip", label="目標主機 IP (Zabbix Hostname)", value=state.get("ip"), editable=False),
        ReviewField(key="hostname", label="新主機名稱 (Deployment Name)", value=state.get("hostname"), editable=True),
        ReviewField(key="init_user", label="初始 SSH 登入帳號", value=state.get("init_user"), editable=True),
        ReviewField(
            key="user1",
            label="管理帳號 (Administrators + 公鑰)" if is_win else "管理帳號 (設免密 sudo + 公鑰)",
            value=user1,
            editable=True,
        ),
        ReviewField(
            key="admin_password",
            label=f"{user1} 新密碼" if is_win else f"root 與 {user1} 共用新密碼",
            secret_ref=ref, secret_key="admin_password", editable=True,
            options="可直接輸入，或填 ? 以獨立輸入框輸入 (密碼含逗號時適用)",
        ),
        ReviewField(key="user2", label="第二個固定帳號", value=user2, editable=True),
        ReviewField(
            key="user2_password", label=f"{user2} 新密碼",
            secret_ref=ref, secret_key="user2_password", editable=True,
            options="可直接輸入，或填 ? 以獨立輸入框輸入 (密碼含逗號時適用)",
        ),
        ReviewField(
            key="cs_mode", label="CrowdStrike 安裝模式 (NGAV/MDR/SKIP)",
            value="略過 (SKIP)" if cs_mode == "SKIP" else f"{cs_mode} ({cs_ready})", editable=True,
        ),
        ReviewField(
            key="skip_disk",
            label="略過磁碟擴容與掛載 (true/false)" if is_win else "略過 LVM 磁碟擴容與掛載 (true/false)",
            value="略過 (SKIP)" if state.get("skip_disk") else (
                f"自動掃描 (C 槽擴容 + 額外碟 GPT/NTFS 從 {config.WINDOWS_EXTRA_DISK_START_LETTER}: 掛載)"
                if is_win else "自動掃描 (系統碟擴容 / + 額外碟掛載 /data)"
            ),
            editable=True,
        ),
        ReviewField(key="os_type", label="目標作業系統 (linux/windows)", value=os_type, editable=True),
    ]
    if is_win:
        sccm_ok = config.SCCM_INSTALLERS_DIR.is_dir() and bool(config.WINDOWS_SCCM_SITE_CODE)
        sccm_status = (
            f"✔ 已就緒 (Site={config.WINDOWS_SCCM_SITE_CODE})"
            if sccm_ok else "⚠️ 請確認本機 scripts/installers/SCCM 與 .env 的 WINDOWS_SCCM_* 設定"
        )
        fields.append(ReviewField(key="sccm", label="SCCM Client 安裝 (必裝)", value=sccm_status, editable=False))
    fields.append(
        ReviewField(
            key="scripts",
            label=f"依序上傳執行腳本 ({remote_dir})",
            value=", ".join(plan.script_files),
            editable=False,
        )
    )
    return fields


# 2. 人工審查中斷節點 (HITL)
def human_review(state: BootstrapState) -> dict[str, Any]:
    # 程式重啟後記憶體中的密碼會遺失：回到準備階段重新產生並再次審核
    if not get_secret_store().has(state.get("secret_ref")):
        return {"secret_ref": None, "human_feedback": {"action": "secrets_lost"}, "validation_errors": []}

    errors = state.get("validation_errors") or []
    decision = ask_review(review_request(
        kind="vm_bootstrap",
        title="🛠️ 待審核的新機基本設定計畫 (VM Bootstrap)",
        message="新 VM 基本設定參數與密碼已準備完成，請人工審查確認。",
        fields=build_review_fields(state),
        actions=[APPROVE, EDIT_REVIEW, REGENERATE, SKIP],
        errors=errors,
        notes=[
            "密碼只保存在本機記憶體，執行成功後才會寫入密碼紀錄檔。",
            "選 [E] 修改參數 (含密碼) 後會先回到此畫面重新檢視，確認無誤再按 [A] 執行。",
        ],
    ))
    feedback = decision.model_dump()
    # 防呆：即使呼叫端未經 CLI 遮蔽，也不把明文密碼寫入 state["human_feedback"]
    if feedback.get("edits"):
        feedback["edits"] = {
            k: ("<redacted>" if k in _SECRET_EDIT_KEYS else v) for k, v in feedback["edits"].items()
        }

    if decision.action == "regenerate":
        return {"secret_ref": _new_secrets(state.get("secret_ref")), "human_feedback": feedback, "validation_errors": []}

    if decision.action in ("approve", "edit"):
        updates, edit_errors = apply_edits(state, decision.edits)
        return {**updates, "human_feedback": feedback, "validation_errors": edit_errors}

    reason = decision.feedback or "使用者於人工審查取消"
    get_secret_store().delete(state.get("secret_ref"))
    return {
        "human_feedback": feedback,
        "validation_errors": [],
        "execution_result": {"aborted": True, "reason": reason},
        "messages": [AIMessage(content=f"🚫 已取消對 {state.get('ip')} 的基本設定。原因：{reason}")],
    }


def route_review_result(state: BootstrapState) -> str:
    action = (state.get("human_feedback") or {}).get("action")
    if action == "secrets_lost":
        return "prepare_bootstrap"
    if action in ("regenerate", "edit") or state.get("validation_errors"):
        return "human_review"
    if action == "approve":
        return END if state.get("handoff_to_background") else "connect_and_upload"
    return END


def _probe_windows_paths(ssh: Any, ip: str, username: str, password: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """透過 PowerShell 動態取得 Windows 登入帳號的實際桌面與 Temp 路徑 (處理桌面重導向等情境)"""
    probe_cmd = (
        'powershell.exe -NoProfile -NonInteractive -Command '
        '"[Environment]::GetFolderPath(\'Desktop\'); [System.IO.Path]::GetTempPath()"'
    )
    ec, out, _ = ssh.exec_command(
        ip=ip, cmd=probe_cmd, username=username, password=password, cmd_timeout=15, os_type="windows",
    )
    if ec != 0:
        return None, None
    lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
    desktop_bootstrap: Optional[str] = None
    temp_dir: Optional[str] = None
    if len(lines) >= 1 and re.match(r"^[A-Za-z]:[\\/]", lines[0]):
        desktop_bootstrap = lines[0].replace("\\", "/").rstrip("/") + "/bootstrap"
    if len(lines) >= 2 and re.match(r"^[A-Za-z]:[\\/]", lines[1]):
        temp_dir = lines[1].replace("\\", "/").rstrip("/")
    return desktop_bootstrap, temp_dir


# 3. 連線就緒檢查與 SFTP 上傳
def connect_and_upload(state: BootstrapState) -> dict[str, Any]:
    ssh = get_ssh_client()
    os_type = _os_type(state)
    is_win = os_type == "windows"
    plan = get_bootstrap_plan(os_type)
    ip, init_user = state["ip"], state.get("init_user") or config.BOOTSTRAP_INIT_USER
    pw = _secrets(state)
    if pw is None:
        return _fail("❌ 本機記憶體中的密碼已遺失 (程式可能已重啟)，請重新執行基本設定。", state)
    admin_password = pw["admin_password"]
    step_results: list[dict[str, Any]] = []

    # 確保初始連線就緒，若遇 Linux 模板首次登入 PAM 強制要求改密碼，自動變更為 admin_password
    emit(f"[Bootstrap] 正在檢測 {init_user}@{ip} ({os_type}) SSH 連線...", stage="vm_bootstrap")
    login_kwargs: dict[str, Any] = dict(
        ip=ip, username=init_user,
        init_password=config.BOOTSTRAP_INIT_PASSWORD or None,
        new_password=admin_password,
    )
    if is_win:
        login_kwargs["os_type"] = "windows"
    ec_login, auth_password, pam_changed, login_msg = ssh.ensure_initial_login(**login_kwargs)
    if ec_login < 0:
        return _fail(f"❌ 無法連線至 {ip} (帳號: {init_user})：{login_msg}", state)

    login_mode = "key" if auth_password is None else ("admin" if auth_password == admin_password else "init")
    updates: dict[str, Any] = {"login_mode": login_mode, "credentials_saved": False}
    if pam_changed:
        # 首次登入已由 PAM 強制改為 admin_password，立即先留存避免後續步驟中斷而遺失密碼
        updates["credentials_location"] = _record_credentials(state)
        updates["credentials_saved"] = True
        step_results.append({"step": "0a. 首次登入 PAM 密碼過期自動更新", "status": "success",
                             "exit_code": 0, "detail": login_msg})
        emit(f"[Bootstrap] ✔ {login_msg}", stage="vm_bootstrap")

    # 解析遠端上傳與機密暫存目錄 (Windows 動態取得實際桌面與 Temp 路徑並寫入 Checkpoint)
    default_remote, _, _, default_secret_tmp = _resolve_remote_dirs(state)
    if is_win:
        probed_remote, probed_tmp = _probe_windows_paths(ssh, ip, init_user, auth_password)
        remote_dir = probed_remote or default_remote
        secret_temp_dir = probed_tmp or default_secret_tmp
    else:
        remote_dir = default_remote
        secret_temp_dir = default_secret_tmp
    updates["remote_dir"] = remote_dir
    updates["secret_temp_dir"] = secret_temp_dir
    cs_remote_dir = f"{remote_dir}/CrowdStrike"
    sccm_remote_dir = f"{remote_dir}/SCCM" if plan.sccm_remote_dir else ""

    # 依作業系統與發行版精準挑選要上傳的安裝檔
    scripts_dir = _scripts_dir(os_type)
    local_scripts = [scripts_dir / name for name in plan.script_files]
    extra_dirs: list[tuple[Path, str]] = []
    extra_files: list[tuple[Path, str]] = []

    cs_enabled = (state.get("cs_mode") or "").upper() != "SKIP"
    if cs_enabled and config.CROWDSTRIKE_INSTALLERS_DIR.is_dir():
        if is_win:
            win_cs_dir = config.CROWDSTRIKE_INSTALLERS_DIR / "Windows"
            if win_cs_dir.is_dir():
                for exe in sorted(win_cs_dir.glob("FalconSensor_Windows*.exe")):
                    extra_files.append((exe, cs_remote_dir))
        else:
            ec_os, os_out, _ = ssh.exec_command(
                ip=ip, cmd="cat /etc/os-release", username=init_user, password=auth_password, cmd_timeout=10,
            )
            matched_sub = detect_linux_cs_subdir(os_out) if ec_os == 0 else None
            if matched_sub and (config.CROWDSTRIKE_INSTALLERS_DIR / matched_sub).is_dir():
                emit(f"[Bootstrap] 偵測到 Linux 發行版 ({matched_sub})，僅上傳對應 CrowdStrike 安裝檔", stage="vm_bootstrap")
                extra_dirs.append((config.CROWDSTRIKE_INSTALLERS_DIR / matched_sub, f"{cs_remote_dir}/{matched_sub}"))
            else:
                for sub in LINUX_CS_SUBDIRS:
                    sub_dir = config.CROWDSTRIKE_INSTALLERS_DIR / sub
                    if sub_dir.is_dir():
                        extra_dirs.append((sub_dir, f"{cs_remote_dir}/{sub}"))

    if is_win and sccm_remote_dir and config.SCCM_INSTALLERS_DIR.is_dir():
        extra_dirs.append((config.SCCM_INSTALLERS_DIR, sccm_remote_dir))

    upload_hint = " (含 SCCM x64 / CrowdStrike 安裝檔，首次傳輸可能需數分鐘)" if is_win else ""
    emit(f"[Bootstrap] 正在透過 SFTP 上傳腳本與安裝檔至 {ip}:{remote_dir}{upload_hint} ...", stage="vm_bootstrap")

    upload_kwargs: dict[str, Any] = dict(
        ip=ip, files=local_scripts, remote_dir=remote_dir, extra_dirs=extra_dirs,
        username=init_user, password=auth_password,
    )
    if extra_files:
        upload_kwargs["extra_files"] = extra_files
    if is_win:
        upload_kwargs["os_type"] = "windows"
        upload_kwargs["transfer_timeout"] = 300

    up_code, uploaded_files, up_err = ssh.upload_files(**upload_kwargs)
    if up_code < 0:
        return {**updates, **_fail(f"❌ SFTP 上傳腳本至 {ip} 失敗：{up_err}", {**state, "step_results": step_results})}

    step_results.append({"step": "0. SFTP 上傳腳本與安裝檔", "status": "success", "exit_code": 0,
                         "detail": f"已上傳 {len(uploaded_files)} 個檔案至 {remote_dir}"})
    job_id = state.get("job_id") or ""
    if job_id and is_cancelled(job_id):
        return {
            **updates,
            "step_results": step_results,
            "sub_stage": "cancelled",
            "stopped": True,
            "note": "使用者停止工作",
            "execution_result": {"aborted": True, "reason": "使用者停止工作", "steps": step_results},
        }
    return {**updates, "step_results": step_results, "step_index": 0, "stopped": False}


def route_after_upload(state: BootstrapState) -> str:
    return "finalize" if state.get("execution_result") else "run_step"


def _exec_step(state: BootstrapState, step: StepDef) -> tuple[int, str]:
    """執行單一步驟；Linux 若免密 sudo 尚未生效而需要密碼，自動以目前登入密碼重試一次"""
    ssh = get_ssh_client()
    is_win = _is_windows(state)
    remote_dir, _, _, secret_temp_dir = _resolve_remote_dirs(state)
    login_pw = _login_password(state)
    env = render_env(step.env, _public_context(state))
    secret_env = render_env(step.secret_env, _secret_context(state)) if step.secret_env else None
    kwargs: dict[str, Any] = dict(
        ip=state["ip"], cmd=f"{remote_dir}/{step.script}",
        username=state.get("init_user"), password=login_pw, sudo=(False if is_win else step.sudo),
        env=env, secret_env=secret_env, cmd_timeout=step.timeout,
    )
    if is_win:
        kwargs["os_type"] = "windows"
        kwargs["remote_temp_dir"] = secret_temp_dir
        ec, out, err = ssh.exec_command(**kwargs)
    else:
        sudo_pw = login_pw if step.sudo_password else None
        ec, out, err = ssh.exec_command(sudo_password=sudo_pw, **kwargs)
        if ec != 0 and step.sudo and not sudo_pw and login_pw:
            ec, out, err = ssh.exec_command(sudo_password=login_pw, **kwargs)
    combined = (out.strip() + ("\n" + err.strip() if err.strip() else "")).strip()
    return ec, combined


# 4. 逐步執行 (每次執行一個步驟，完成後寫入 checkpoint)
def run_step(state: BootstrapState) -> dict[str, Any]:
    plan = get_bootstrap_plan(_os_type(state))
    idx = state.get("step_index", 0)
    step = plan.steps[idx]
    ctx = _public_context(state)
    name = f"{idx + 1}. {render(step.name, ctx)}"
    results = list(state.get("step_results") or [])
    updates: dict[str, Any] = {"step_index": idx + 1}

    if step.skip_if and ctx.get(step.skip_if):
        results.append({"step": name, "step_idx": idx, "status": "skipped", "exit_code": 0, "detail": "依設定略過"})
        return {**updates, "step_results": results}

    if step.rotates_password and _secrets(state) is None:
        results.append({"step": name, "step_idx": idx, "status": "failed", "exit_code": -1,
                        "detail": "本機記憶體中的密碼已遺失 (程式可能已重啟)，無法執行改密碼步驟"})
        return {**updates, "step_results": results, "stopped": True}

    job_id = state.get("job_id") or ""
    if job_id and is_cancelled(job_id):
        return {"sub_stage": "cancelled", "stopped": True, "note": "使用者停止工作"}

    emit(f"[Bootstrap] ▶ 執行 {name} ...", stage="vm_bootstrap", step=step.id)
    ec, combined = _exec_step(state, step)
    if ec == 0:
        results.append({"step": name, "step_idx": idx, "status": "success", "exit_code": 0, "detail": combined[-400:]})
        emit(f"[Bootstrap] ✅ 完成 {name}", stage="vm_bootstrap", step=step.id)
        if SSHD_RESTART_MARKER in combined:
            emit(f"[Bootstrap] ⏳ 已更新 sshd_config，等待 {state['ip']} 的 sshd 重啟就緒...", stage="vm_bootstrap")
            wait_fn = getattr(get_ssh_client(), "wait_port_ready", None)
            if callable(wait_fn):
                wait_fn(state["ip"])
        if step.rotates_password:
            # 密碼修改成功：留存密碼紀錄 (若尚未於 PAM 階段留存)，後續改以新密碼登入
            if not state.get("credentials_saved"):
                updates["credentials_location"] = _record_credentials(state)
                updates["credentials_saved"] = True
            if state.get("login_mode") != "key":
                updates["login_mode"] = "admin"
    else:
        detail = combined[-500:] or f"exit code {ec}"
        results.append({"step": name, "step_idx": idx, "status": "failed", "exit_code": ec, "detail": detail})
        emit(f"[Bootstrap] ❌ 失敗 {name} (exit={ec})：{detail.splitlines()[-1][:120]}", stage="vm_bootstrap", step=step.id)
        if step.stop_on_fail:
            updates["stopped"] = True
    return {**updates, "step_results": results}


def route_next_step(state: BootstrapState) -> str:
    if state.get("stopped") or state.get("step_index", 0) >= len(get_bootstrap_plan(_os_type(state)).steps):
        return "cleanup"
    return "run_step"


# 5. 清理 (Linux 無論前面步驟是否失敗一律清理；Windows 未設定 cleanup 則直接略過)
def cleanup(state: BootstrapState) -> dict[str, Any]:
    if state.get("sub_stage") == "cancelled":
        return {}
    plan = get_bootstrap_plan(_os_type(state))
    step = plan.cleanup
    if step is None:
        return {}
    emit(f"[Bootstrap] ▶ 執行 {step.name} ...", stage="vm_bootstrap", step=step.id)
    ec, combined = _exec_step(state, step)
    results = list(state.get("step_results") or [])
    results.append({"id": step.id, "step": f"{len(plan.steps) + 1}. {step.name}",
                    "status": "success" if ec == 0 else "failed", "exit_code": ec, "detail": combined[-300:]})
    return {"step_results": results}


# 6. 最終驗證與報告
def finalize(state: BootstrapState) -> dict[str, Any]:
    os_type = _os_type(state)
    is_win = os_type == "windows"
    plan = get_bootstrap_plan(os_type)
    ip, hostname, user1 = state.get("ip"), state.get("hostname"), state.get("user1")
    step_results = state.get("step_results") or []

    if state.get("sub_stage") == "cancelled":
        get_secret_store().delete(state.get("secret_ref"))
        return {
            "execution_result": {"aborted": True, "reason": "使用者停止工作", "steps": step_results},
            "messages": [AIMessage(content=f"🛑 已停止對 {ip} ({hostname}) 的基本設定。")],
        }

    if state.get("execution_result"):  # 連線或上傳階段已失敗
        get_secret_store().delete(state.get("secret_ref"))
        return {}

    # 測試以 SSH 私鑰連入管理帳號 (Linux 一併驗證免密碼 sudo)
    key_verify_msg = "未測試 (本機未配置私鑰檔案)"
    key_verified = False
    if config.SSH_PRIVATE_KEY_PATH.exists():
        if is_win:
            v_ec, v_out, v_err = get_ssh_client().exec_command(
                ip=ip, cmd="whoami", username=user1, password=None, os_type="windows",
            )
            if v_ec == 0 and (user1 or "").lower() in v_out.lower():
                key_verified = True
                key_verify_msg = f"✅ 驗證成功 ({user1} 可使用 SSH 私鑰登入 Windows：{v_out.strip()})"
            else:
                key_verify_msg = f"⚠️ SSH 私鑰驗證未通過 (exit={v_ec}): {v_err.strip() or v_out.strip()}"
        else:
            v_ec, v_out, v_err = get_ssh_client().exec_command(
                ip=ip, cmd="hostname && sudo -n whoami", username=user1, password=None,
            )
            if v_ec == 0 and "root" in v_out:
                key_verified = True
                key_verify_msg = f"✅ 驗證成功 ({user1} 可使用 SSH 私鑰登入且免密碼 sudo 為 root)"
            else:
                key_verify_msg = f"⚠️ 私鑰或免密 sudo 驗證未通過 (exit={v_ec}): {v_err.strip() or v_out.strip()}"

    cleanup_id = plan.cleanup.id if plan.cleanup else None
    overall_ok = all(sr["status"] != "failed" for sr in step_results if sr.get("id") != cleanup_id)
    location = state.get("credentials_location") or "(尚未留存，密碼未變更)"

    # 全部步驟與驗證通過且策略啟用 reboot_on_success 時，排程自動重開機
    reboot_msg = ""
    if overall_ok and plan.reboot_on_success:
        emit(f"[Bootstrap] 🔄 所有檢查皆已通過，正在排程 {ip} 重開機...", stage="vm_bootstrap")
        reboot_pw = None if key_verified else _login_password(state)
        reboot_cmd = 'shutdown /r /t 5 /c "VM Bootstrap completed"' if is_win else "shutdown -r +0"
        reboot_kw: dict[str, Any] = dict(
            ip=ip, cmd=reboot_cmd, username=state.get("init_user") or user1, password=reboot_pw,
        )
        if is_win:
            reboot_kw["os_type"] = "windows"
        else:
            reboot_kw["sudo"] = True
        r_ec, r_out, r_err = get_ssh_client().exec_command(**reboot_kw)
        if r_ec == 0:
            reboot_msg = "🔄 已排程於 5 秒後自動重開機 (使新電腦名稱與代理程式完整生效)"
        else:
            reboot_msg = f"⚠️ 自動重開機指令回傳異常 (exit={r_ec})，請手動重開機：{r_err.strip() or r_out.strip()}"

    # 組合回傳訊息 (注意：不將明文密碼放入 AIMessage，避免傳送至雲端 Gemini API)
    os_badge = "Windows" if is_win else "Linux"
    lines = [
        f"{'🎉' if overall_ok else '⚠️'} **{os_badge} VM `{ip}` ({hostname}) 基本設定執行{'完成' if overall_ok else '結束 (部分步驟異常)'}**",
        "",
        "| 步驟 | 狀態 | Exit Code | 摘要 |",
        "|---|---|---|---|",
    ]
    for sr in step_results:
        icon = "✅ 成功" if sr["status"] == "success" else ("⏭️ 略過" if sr["status"] == "skipped" else "❌ 失敗")
        short_detail = " ".join((sr.get("detail") or "").splitlines()[-2:])[:100].replace("|", "\\|")
        lines.append(f"| {sr['step']} | {icon} | `{sr['exit_code']}` | {short_detail} |")

    verify_label = "SSH 私鑰登入驗證" if is_win else "SSH 私鑰與免密 sudo 驗證"
    acct_desc = f"`{user1}` 與 `{state.get('user2')}`" if is_win else f"`root` / `{user1}` 與 `{state.get('user2')}`"
    lines.extend([
        "",
        f"- **{verify_label}**：{key_verify_msg}",
        f"- **帳號密碼紀錄**：{acct_desc} 之新密碼已顯示於審核畫面並留存於 `{location}`"
        "（為保護資安，不在雲端對話中重複顯示明文密碼）。",
    ])
    if reboot_msg:
        lines.append(f"- **自動重開機**：{reboot_msg}")

    if overall_ok:
        get_secret_store().delete(state.get("secret_ref"))
    get_audit_sink().record(
        "vm_bootstrap_finished", ip=ip, hostname=hostname, os_type=os_type, success=overall_ok,
    )
    exec_result: dict[str, Any] = {
        "success": overall_ok, "ip": ip, "hostname": hostname, "os_type": os_type,
        "key_verify": key_verify_msg, "credentials_log": location, "steps": step_results,
    }
    if reboot_msg:
        exec_result["reboot"] = reboot_msg
    return {
        "execution_result": exec_result,
        "messages": [AIMessage(content="\n".join(lines))],
    }
