"""VM Bootstrap 子圖節點

prepare_bootstrap → human_review (HITL) → connect_and_upload → run_step (逐步迴圈) → cleanup → finalize

- 步驟清單來自 policies/bootstrap_steps.toml，每完成一步即寫入 checkpoint，可從中斷處續跑。
- 密碼只存在 SecretStore (記憶體)，state 只保存 secret_ref；程式重啟後參照失效時會重新產生密碼並再次審核。
- 密碼與 CrowdStrike Token 以 secret_env 傳遞 (權限 600 暫存檔，載入後立即刪除)，不出現在遠端命令列。
"""
import re
from pathlib import Path
from typing import Any, Optional

from langchain_core.messages import AIMessage
from langgraph.graph import END

import config
from clients.aria_client import get_aria_client
from clients.ssh_client import get_ssh_client
from core.progress import emit
from core.secrets import get_audit_sink, get_credential_sink, get_secret_store
from scripts.passwd_creator import generate_vm_passwords
from subgraphs.common.review import (
    APPROVE, EDIT, REGENERATE, SKIP, ReviewField, ask_review, review_request,
)
from subgraphs.vm_bootstrap.state import BootstrapState
from subgraphs.vm_bootstrap.steps import StepDef, get_bootstrap_plan, render, render_env

CS_MODES = ("NGAV", "MDR", "SKIP")
_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$")
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")


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
    return {
        "ip": state.get("ip", ""),
        "hostname": state.get("hostname", ""),
        "init_user": state.get("init_user", ""),
        "user1": state.get("user1", ""),
        "user2": state.get("user2", ""),
        "pub_key": _resolve_pub_key(),
        "cs_mode": state.get("cs_mode", ""),
        "cs_proxy_host": config.CS_PROXY_HOST,
        "cs_proxy_port": config.CS_PROXY_PORT,
        "zabbix_servers": config.ZABBIX_SERVERS,
        "zabbix_conf": config.ZABBIX_CONF_PATH,
        "cs_skip": (state.get("cs_mode") or "").upper() == "SKIP",
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
    location = get_credential_sink().record(
        state.get("ip", ""), state.get("hostname", ""),
        {f"root / {user1}": pw.get("admin_password", ""), user2: pw.get("user2_password", "")},
    )
    get_audit_sink().record("vm_bootstrap_credentials_rotated", ip=state.get("ip"), hostname=state.get("hostname"))
    return location


def _fail(msg: str, state: BootstrapState) -> dict[str, Any]:
    return {
        "execution_result": {"success": False, "error": msg, "steps": state.get("step_results") or []},
        "messages": [AIMessage(content=msg)],
    }


# 1. 準備階段：由 IP 查詢 Aria Deployment 名稱作為 Hostname，並於本機產生兩組強密碼
def prepare_bootstrap(state: BootstrapState) -> dict[str, Any]:
    ip = (state.get("ip") or "").strip()
    hostname = (state.get("hostname") or "").strip() or None
    cs_mode = (state.get("cs_mode") or config.CROWDSTRIKE_DEFAULT_MODE or "NGAV").upper()
    if cs_mode not in CS_MODES:
        cs_mode = "NGAV"

    # 若未指定 hostname，自動透過 Aria API 以 IP 反查 deployment_name
    if not hostname and ip:
        try:
            vm_info = get_aria_client().query_vm_by_ip(ip)
            if vm_info.get("found"):
                hostname = (vm_info.get("deployment") or {}).get("name") or (vm_info.get("machine") or {}).get("name")
        except Exception:
            pass
    if not hostname:
        hostname = f"vm-{ip.replace('.', '-')}"

    secret_ref = state.get("secret_ref")
    if not get_secret_store().has(secret_ref):
        secret_ref = _new_secrets(secret_ref)

    return {
        "ip": ip,
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
def apply_edits(state: BootstrapState, edits: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    updates: dict[str, Any] = {}
    errors: list[str] = []
    for key, raw in (edits or {}).items():
        val = (raw or "").strip()
        if key == "hostname":
            if _HOSTNAME_RE.match(val):
                updates["hostname"] = val
            else:
                errors.append(f"hostname 格式不合法：{val}")
        elif key == "cs_mode":
            if val.upper() in CS_MODES:
                updates["cs_mode"] = val.upper()
            else:
                errors.append(f"cs_mode 必須是 {'/'.join(CS_MODES)}：{val}")
        elif key == "skip_disk":
            updates["skip_disk"] = val.lower() in ("true", "1", "yes", "y")
        elif key in ("user1", "user2", "init_user"):
            if _USERNAME_RE.match(val):
                updates[key] = val
            else:
                errors.append(f"{key} 帳號名稱格式不合法：{val}")
        else:
            errors.append(f"不可修改或未知的欄位：{key}")
    return updates, errors


def build_review_fields(state: BootstrapState) -> list[ReviewField]:
    plan = get_bootstrap_plan()
    ref = state.get("secret_ref") or ""
    user1, user2 = state.get("user1"), state.get("user2")
    cs_mode = state.get("cs_mode", "NGAV")
    cs_ready = "✔ 已就緒" if config.CROWDSTRIKE_LOCAL_DIR.is_dir() else "⚠️ 本機未找到 CrowdStrike 資料夾"
    return [
        ReviewField(key="ip", label="目標主機 IP (Zabbix Hostname)", value=state.get("ip"), editable=False),
        ReviewField(key="hostname", label="新主機名稱 (Deployment Name)", value=state.get("hostname"), editable=True),
        ReviewField(key="init_user", label="初始 SSH 登入帳號", value=state.get("init_user"), editable=True),
        ReviewField(key="user1", label="管理帳號 (設免密 sudo + 公鑰)", value=user1, editable=True),
        ReviewField(key="admin_password", label=f"root 與 {user1} 共用新密碼",
                    secret_ref=ref, secret_key="admin_password", editable=False),
        ReviewField(key="user2", label="第二個固定帳號", value=user2, editable=True),
        ReviewField(key="user2_password", label=f"{user2} 新密碼",
                    secret_ref=ref, secret_key="user2_password", editable=False),
        ReviewField(key="cs_mode", label="CrowdStrike 安裝模式 (NGAV/MDR/SKIP)",
                    value="略過 (SKIP)" if cs_mode == "SKIP" else f"{cs_mode} ({cs_ready})", editable=True),
        ReviewField(key="skip_disk", label="略過 LVM 磁碟擴容與掛載 (true/false)",
                    value="略過 (SKIP)" if state.get("skip_disk") else "自動掃描 (系統碟擴容 / + 額外碟掛載 /data)",
                    editable=True),
        ReviewField(key="scripts", label=f"依序上傳執行腳本 ({plan.remote_dir})",
                    value=", ".join(plan.script_files), editable=False),
    ]


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
        actions=[APPROVE, EDIT, REGENERATE, SKIP],
        errors=errors,
        notes=["密碼只保存在本機記憶體，執行成功後才會寫入密碼紀錄檔。"],
    ))
    feedback = decision.model_dump()

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
    if action == "regenerate" or state.get("validation_errors"):
        return "human_review"
    if action in ("approve", "edit"):
        return "connect_and_upload"
    return END


# 3. 連線就緒檢查與 SFTP 上傳
def connect_and_upload(state: BootstrapState) -> dict[str, Any]:
    ssh = get_ssh_client()
    plan = get_bootstrap_plan()
    ip, init_user = state["ip"], state.get("init_user") or config.BOOTSTRAP_INIT_USER
    pw = _secrets(state)
    if pw is None:
        return _fail("❌ 本機記憶體中的密碼已遺失 (程式可能已重啟)，請重新執行基本設定。", state)
    admin_password = pw["admin_password"]
    step_results: list[dict[str, Any]] = []

    # 確保初始連線就緒，若遇模板首次登入 PAM 強制要求改密碼，自動變更為 admin_password
    emit(f"[Bootstrap] 正在檢測 {init_user}@{ip} SSH 連線與密碼效期...", stage="vm_bootstrap")
    ec_login, auth_password, pam_changed, login_msg = ssh.ensure_initial_login(
        ip=ip, username=init_user,
        init_password=config.BOOTSTRAP_INIT_PASSWORD or None,
        new_password=admin_password,
    )
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

    # SFTP 上傳腳本與 CrowdStrike 安裝檔資料夾
    emit(f"[Bootstrap] 正在透過 SFTP 上傳腳本與安裝檔至 {ip}:{plan.remote_dir} ...", stage="vm_bootstrap")
    local_scripts = [config.BOOTSTRAP_SCRIPTS_DIR / name for name in plan.script_files]
    extra_dirs: list[tuple[Path, str]] = []
    if (state.get("cs_mode") or "").upper() != "SKIP" and config.CROWDSTRIKE_LOCAL_DIR.is_dir():
        extra_dirs.append((config.CROWDSTRIKE_LOCAL_DIR, plan.crowdstrike_remote_dir))

    up_code, uploaded_files, up_err = ssh.upload_files(
        ip=ip, files=local_scripts, remote_dir=plan.remote_dir, extra_dirs=extra_dirs,
        username=init_user, password=auth_password,
    )
    if up_code < 0:
        return {**updates, **_fail(f"❌ SFTP 上傳腳本至 {ip} 失敗：{up_err}", {**state, "step_results": step_results})}

    step_results.append({"step": "0. SFTP 上傳腳本與安裝檔", "status": "success", "exit_code": 0,
                         "detail": f"已上傳 {len(uploaded_files)} 個檔案至 {plan.remote_dir}"})
    return {**updates, "step_results": step_results, "step_index": 0, "stopped": False}


def route_after_upload(state: BootstrapState) -> str:
    return "finalize" if state.get("execution_result") else "run_step"


def _exec_step(state: BootstrapState, step: StepDef) -> tuple[int, str]:
    """執行單一步驟；若免密 sudo 尚未生效而需要密碼，自動以目前登入密碼重試一次"""
    ssh = get_ssh_client()
    plan = get_bootstrap_plan()
    login_pw = _login_password(state)
    env = render_env(step.env, _public_context(state))
    secret_env = render_env(step.secret_env, _secret_context(state)) if step.secret_env else None
    kwargs = dict(
        ip=state["ip"], cmd=f"{plan.remote_dir.rstrip('/')}/{step.script}",
        username=state.get("init_user"), password=login_pw, sudo=step.sudo,
        env=env, secret_env=secret_env, cmd_timeout=step.timeout,
    )
    sudo_pw = login_pw if step.sudo_password else None
    ec, out, err = ssh.exec_command(sudo_password=sudo_pw, **kwargs)
    if ec != 0 and step.sudo and not sudo_pw and login_pw:
        ec, out, err = ssh.exec_command(sudo_password=login_pw, **kwargs)
    combined = (out.strip() + ("\n" + err.strip() if err.strip() else "")).strip()
    return ec, combined


# 4. 逐步執行 (每次執行一個步驟，完成後寫入 checkpoint)
def run_step(state: BootstrapState) -> dict[str, Any]:
    plan = get_bootstrap_plan()
    idx = state.get("step_index", 0)
    step = plan.steps[idx]
    ctx = _public_context(state)
    name = f"{idx + 1}. {render(step.name, ctx)}"
    results = list(state.get("step_results") or [])
    updates: dict[str, Any] = {"step_index": idx + 1}

    if step.skip_if and ctx.get(step.skip_if):
        results.append({"step": name, "status": "skipped", "exit_code": 0, "detail": "依設定略過"})
        return {**updates, "step_results": results}

    if step.secret_env and _secrets(state) is None:
        results.append({"step": name, "status": "failed", "exit_code": -1,
                        "detail": "本機記憶體中的密碼已遺失 (程式可能已重啟)，無法續跑"})
        return {**updates, "step_results": results, "stopped": True}

    emit(f"[Bootstrap] ▶ 執行 {name} ...", stage="vm_bootstrap", step=step.id)
    ec, combined = _exec_step(state, step)
    if ec == 0:
        results.append({"step": name, "status": "success", "exit_code": 0, "detail": combined[-400:]})
        if step.rotates_password:
            # 密碼修改成功：留存密碼紀錄 (若尚未於 PAM 階段留存)，後續改以新密碼登入
            if not state.get("credentials_saved"):
                updates["credentials_location"] = _record_credentials(state)
                updates["credentials_saved"] = True
            if state.get("login_mode") != "key":
                updates["login_mode"] = "admin"
    else:
        results.append({"step": name, "status": "failed", "exit_code": ec,
                        "detail": combined[-500:] or f"exit code {ec}"})
        if step.stop_on_fail:
            updates["stopped"] = True
    return {**updates, "step_results": results}


def route_next_step(state: BootstrapState) -> str:
    if state.get("stopped") or state.get("step_index", 0) >= len(get_bootstrap_plan().steps):
        return "cleanup"
    return "run_step"


# 5. 清理 (無論前面步驟是否失敗)
def cleanup(state: BootstrapState) -> dict[str, Any]:
    step = get_bootstrap_plan().cleanup
    emit(f"[Bootstrap] ▶ 執行 {step.name} ...", stage="vm_bootstrap", step=step.id)
    ec, combined = _exec_step(state, step)
    results = list(state.get("step_results") or [])
    results.append({"id": step.id, "step": f"{len(get_bootstrap_plan().steps) + 1}. {step.name}",
                    "status": "success" if ec == 0 else "failed", "exit_code": ec, "detail": combined[-300:]})
    return {"step_results": results}


# 6. 最終驗證與報告
def finalize(state: BootstrapState) -> dict[str, Any]:
    ip, hostname, user1 = state.get("ip"), state.get("hostname"), state.get("user1")
    step_results = state.get("step_results") or []

    if state.get("execution_result"):  # 連線或上傳階段已失敗
        get_secret_store().delete(state.get("secret_ref"))
        return {}

    # 測試以「SSH 私鑰 + 免密碼 sudo」連入管理帳號
    key_verify_msg = "未測試 (本機未配置私鑰檔案)"
    if config.SSH_PRIVATE_KEY_PATH.exists():
        v_ec, v_out, v_err = get_ssh_client().exec_command(
            ip=ip, cmd="hostname && sudo -n whoami", username=user1, password=None,
        )
        if v_ec == 0 and "root" in v_out:
            key_verify_msg = f"✅ 驗證成功 ({user1} 可使用 SSH 私鑰登入且免密碼 sudo 為 root)"
        else:
            key_verify_msg = f"⚠️ 私鑰或免密 sudo 驗證未通過 (exit={v_ec}): {v_err.strip() or v_out.strip()}"

    cleanup_id = get_bootstrap_plan().cleanup.id
    overall_ok = all(sr["status"] != "failed" for sr in step_results if sr.get("id") != cleanup_id)
    location = state.get("credentials_location") or "(尚未留存，密碼未變更)"

    # 組合回傳訊息 (注意：不將明文密碼放入 AIMessage，避免傳送至雲端 Gemini API)
    lines = [
        f"{'🎉' if overall_ok else '⚠️'} **VM `{ip}` ({hostname}) 基本設定執行{'完成' if overall_ok else '結束 (部分步驟異常)'}**",
        "",
        "| 步驟 | 狀態 | Exit Code | 摘要 |",
        "|---|---|---|---|",
    ]
    for sr in step_results:
        icon = "✅ 成功" if sr["status"] == "success" else ("⏭️ 略過" if sr["status"] == "skipped" else "❌ 失敗")
        short_detail = " ".join((sr.get("detail") or "").splitlines()[-2:])[:100].replace("|", "\\|")
        lines.append(f"| {sr['step']} | {icon} | `{sr['exit_code']}` | {short_detail} |")
    lines.extend([
        "",
        f"- **SSH 私鑰與免密 sudo 驗證**：{key_verify_msg}",
        f"- **帳號密碼紀錄**：`root` / `{user1}` 與 `{state.get('user2')}` 之新密碼已顯示於審核畫面並留存於 `{location}`"
        "（為保護資安，不在雲端對話中重複顯示明文密碼）。",
    ])

    get_secret_store().delete(state.get("secret_ref"))
    get_audit_sink().record("vm_bootstrap_finished", ip=ip, hostname=hostname, success=overall_ok)
    return {
        "execution_result": {
            "success": overall_ok, "ip": ip, "hostname": hostname,
            "key_verify": key_verify_msg, "credentials_log": location, "steps": step_results,
        },
        "messages": [AIMessage(content="\n".join(lines))],
    }
