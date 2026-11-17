"""VM 生命週期子圖節點

核心設計：
1. 「等待輪詢節點 (wait_vm_ready / wait_disk_op)」與「人工審核節點 (disk_review / bootstrap_review)」嚴格分離。
   Resume 時只會從審核節點開頭重跑，絕不會重新執行前面的狀態掃描。
2. 硬碟動作拆分為 submit_disk_op 與 wait_disk_op 兩個獨立節點：
   - submit_disk_op：送出單一硬碟動作取得 request_id 後「立即 return」寫入 Checkpoint，並於送出前檢查資源是否已存在 (冪等保護)。
   - wait_disk_op：只根據 Checkpoint 中已有的 request_id 查詢狀態，重啟後絕不會重複送出硬碟申請。
3. 重要狀態轉換 (如 sub_stage 由 submitted 轉為 building、或 overdue 轉為 True) 會直接 return 寫入 Checkpoint，
   再由條件邊繞回等待節點，兼顧重啟後的狀態準確性與精簡 Checkpoint 數量。
4. 連續查詢失敗達上限 (MAX_CONSECUTIVE_POLL_ERRORS) 或遇 401/403 時，自動推播通知並暫停背景輪詢，不再無限吞錯重試。
"""
import logging
import threading
import time
from collections import deque
from typing import Any, Callable, Optional

import requests
from langgraph.graph import END

import config
from clients.aria import get_aria_client
from subgraphs.vm_bootstrap import nodes as boot_nodes
from subgraphs.vm_disk import graph as disk_nodes
from subgraphs.vm_disk.executor import ADD_DISK, FAIL_STATUSES, OK_STATUSES, build_ops, locate_machine
from subgraphs.vm_lifecycle.state import BUILDING, SUBMITTED, VMLifecycleState

logger = logging.getLogger(__name__)

APPROVAL_WAIT_STATUSES = (
    "APPROVAL_PENDING",
    "CHECKING_APPROVAL",
    "INITIALIZATION",
    "CREATED",
    "PENDING",
)
BUILDING_STATUSES = ("INPROGRESS", "COMPLETION")
FAILED_REQUEST = ("FAILED", "ABORTED", "APPROVAL_REJECTED", "REJECTED", "CANCELLED")
MAX_CONSECUTIVE_POLL_ERRORS = 5

# ─── 執行緒安全的通知佇列、即時進度快取與喚醒事件 ───
_LOCK = threading.Lock()
_NOTICES: deque[str] = deque()
_LIVE_PROGRESS: dict[str, dict[str, Any]] = {}
_CANCELLED_JOBS: set[str] = set()
POLL_WAKE_EVENT = threading.Event()


def push_notice(message: str) -> None:
    with _LOCK:
        _NOTICES.append(message)


def drain_notices() -> list[str]:
    with _LOCK:
        items = list(_NOTICES)
        _NOTICES.clear()
    return items


def set_live_progress(job_id: str, updates: dict[str, Any]) -> None:
    if not job_id:
        return
    with _LOCK:
        _LIVE_PROGRESS[job_id] = dict(updates)


def get_live_progress(job_id: str) -> dict[str, Any]:
    with _LOCK:
        return dict(_LIVE_PROGRESS.get(job_id) or {})


def clear_live_progress(job_id: str) -> None:
    with _LOCK:
        _LIVE_PROGRESS.pop(job_id, None)


def mark_cancelled(job_id: str) -> None:
    with _LOCK:
        _CANCELLED_JOBS.add(job_id)
    POLL_WAKE_EVENT.set()


def clear_cancelled(job_id: str) -> None:
    with _LOCK:
        _CANCELLED_JOBS.discard(job_id)


def is_cancelled(job_id: str) -> bool:
    with _LOCK:
        return job_id in _CANCELLED_JOBS


def notify_poll_now() -> None:
    POLL_WAKE_EVENT.set()


def wait_for_next_poll(timeout: float) -> None:
    POLL_WAKE_EVENT.wait(timeout)
    POLL_WAKE_EVENT.clear()


# ─── 逾時判斷 ───
def _check_deployment_overdue(state: dict[str, Any], now: float) -> Optional[str]:
    if state.get("overdue"):
        return None
    since = state.get("stage_since") or now
    hours = (now - since) / 3600
    sub_stage = state.get("sub_stage") or SUBMITTED
    if sub_stage == SUBMITTED and hours > config.JOB_APPROVAL_TIMEOUT_HOURS:
        return f"等待 Aria 審核已超過 {config.JOB_APPROVAL_TIMEOUT_HOURS:g} 小時"
    if sub_stage == BUILDING and hours > config.JOB_BUILD_TIMEOUT_HOURS:
        return f"Aria 建立已超過 {config.JOB_BUILD_TIMEOUT_HOURS:g} 小時"
    return None


def _check_disk_overdue(ops: list[dict[str, Any]], already_overdue: bool, now: float) -> Optional[str]:
    if already_overdue:
        return None
    started = [op.get("submitted_at") for op in ops if op.get("status") == "running" and op.get("submitted_at")]
    if started and (now - min(started)) / 60 > config.JOB_DAY2_TIMEOUT_MINUTES:
        return f"硬碟調整動作已超過 {config.JOB_DAY2_TIMEOUT_MINUTES:g} 分鐘未完成"
    return None


# ─── 單次查詢輔助 ───
def poll_deployment_once(
    state: dict[str, Any],
    aria: Any,
    now: Optional[float] = None,
) -> tuple[str, dict[str, Any], list[str]]:
    """
    對 Aria 查詢一次 deployment 狀態。
    回傳 (outcome, state_updates, notices)：
    - outcome: "waiting" | "ready" | "failed"
    """
    now = now or time.time()
    dep_id = state["deployment_id"]
    dep_name = state.get("deployment_name") or dep_id
    notices: list[str] = []

    try:
        dep = aria.deployments.get(dep_id, expand="lastRequest")
    except requests.HTTPError as e:
        if getattr(e.response, "status_code", None) == 404:
            return (
                "failed",
                {"error": "Deployment 已不存在 (可能被拒絕或刪除)", "last_checked": now},
                [f"❌ {dep_name} 的 Deployment 已不存在"],
            )
        raise

    req = dep.get("lastRequest") or {}
    r_status = str(req.get("status") or "").upper()
    d_status = str(dep.get("status") or "").upper()

    if r_status in FAILED_REQUEST or d_status.endswith("FAILED"):
        detail = req.get("details") or r_status or d_status
        return (
            "failed",
            {"error": f"Aria 申請未成功：{detail}", "last_checked": now},
            [f"❌ {dep_name} 建立失敗：{detail}"],
        )

    if r_status == "SUCCESSFUL" or d_status in ("CREATE_SUCCESSFUL", "SUCCESSFUL"):
        try:
            info = locate_machine(aria, dep_id)
        except ValueError:
            info = {}
        if not info.get("ip"):
            updates: dict[str, Any] = {
                "sub_stage": BUILDING,
                "last_checked": now,
                "note": "建立完成，等待取得 IP",
            }
            if state.get("sub_stage") != BUILDING:
                updates["stage_since"] = now
                updates["overdue"] = False
            merged = {**state, **updates}
            reason = _check_deployment_overdue(merged, now)
            if reason:
                updates["overdue"] = True
                notices.append(f"⏰ {dep_name} {reason}，請確認 Aria 狀態")
            return "waiting", updates, notices

        needs_disk = bool(state.get("boot_disk_gb") or state.get("extra_disk_gb"))
        is_linux = state.get("is_linux", True)
        if needs_disk:
            tail = "等待「② 硬碟調整」審核 — 輸入 /jobs 處理"
        elif is_linux:
            tail = "等待「③ 初始設定」審核 — 輸入 /jobs 處理"
        else:
            tail = "流程完成 (Windows 需手動初始設定)"
        win_note = "Windows 尚未支援自動初始設定，請手動完成" if (not needs_disk and not is_linux) else ""
        notices.append(f"🔔 {dep_name} 已建立完成 ({info['ip']})，{tail}")
        return (
            "ready",
            {
                "vm_name": info["vm_name"],
                "machine_id": info["machine_id"],
                "ip": info["ip"],
                "sub_stage": "ready",
                "stage_since": now,
                "overdue": False,
                "note": win_note,
                "error": "",
                "last_checked": now,
            },
            notices,
        )

    if r_status in APPROVAL_WAIT_STATUSES or not r_status:
        # 包含剛送出申請時的 INITIALIZATION / CHECKING_APPROVAL / PENDING，以及進入 APPROVAL_PENDING，皆屬於等待審核階段
        updates = {"sub_stage": SUBMITTED, "last_checked": now}
    elif r_status in BUILDING_STATUSES:
        # 審核通過後，Request 進入 INPROGRESS / COMPLETION 才代表實際建立 VM 中
        updates = {"sub_stage": BUILDING, "last_checked": now}
        if (state.get("sub_stage") or SUBMITTED) == SUBMITTED:
            updates["stage_since"] = now
            updates["overdue"] = False
    else:
        updates = {"sub_stage": state.get("sub_stage") or SUBMITTED, "last_checked": now}

    merged = {**state, **updates}
    reason = _check_deployment_overdue(merged, now)
    if reason:
        updates["overdue"] = True
        notices.append(f"⏰ {dep_name} {reason}，請確認 Aria 狀態")
    return "waiting", updates, notices


# ─── Graph Nodes ───

# 0. 初始化節點 (讓啟動時即刻落地第一筆 Checkpoint)
def init_lifecycle(state: VMLifecycleState) -> dict[str, Any]:
    now = time.time()
    return {
        "sub_stage": state.get("sub_stage") or SUBMITTED,
        "stage_since": state.get("stage_since") or now,
        "last_checked": state.get("last_checked") or 0.0,
        "overdue": bool(state.get("overdue", False)),
        "error": state.get("error") or "",
        "note": state.get("note") or "",
        "ops": state.get("ops") or [],
        "notes": [],
        "errors": [],
        "blocking": [],
    }


# 1. 背景等待 VM 建立完成 (不含 interrupt，狀態轉變或完成時直接 return 寫入 Checkpoint)
def wait_vm_ready(state: VMLifecycleState, sleep_fn: Optional[Callable[[float], None]] = None) -> dict[str, Any]:
    job_id = state.get("job_id") or ""
    dep_name = state.get("deployment_name") or state.get("deployment_id") or job_id
    wait = sleep_fn or wait_for_next_poll
    current: dict[str, Any] = dict(state)
    consecutive_errors = 0

    while not is_cancelled(job_id):
        try:
            aria = get_aria_client()
            prev_sub = current.get("sub_stage") or SUBMITTED
            prev_overdue = bool(current.get("overdue", False))
            outcome, updates, notices = poll_deployment_once(current, aria)
            consecutive_errors = 0
            current.update(updates)
            for n in notices:
                push_notice(n)

            # 若已完成 / 失敗，或發生關鍵狀態轉變 (submitted → building、或首次標記 overdue)，
            # 立即 return 寫入 Checkpoint！(若尚未取得 IP，route_after_build 會再繞回 wait_vm_ready)
            stage_changed = current.get("sub_stage") != prev_sub or bool(current.get("overdue")) != prev_overdue
            if outcome in ("ready", "failed") or stage_changed:
                clear_live_progress(job_id)
                return {
                    k: current[k]
                    for k in ("vm_name", "machine_id", "ip", "sub_stage", "stage_since",
                              "overdue", "note", "error", "last_checked")
                    if k in current
                }
            set_live_progress(job_id, current)
        except Exception as e:
            consecutive_errors += 1
            logger.exception("追蹤工作 %s 輪詢失敗 (%d/%d)", job_id, consecutive_errors, MAX_CONSECUTIVE_POLL_ERRORS)
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            err_note = f"上次查詢失敗 ({consecutive_errors}/{MAX_CONSECUTIVE_POLL_ERRORS})：{type(e).__name__}: {e}"
            current["note"] = err_note
            set_live_progress(job_id, current)
            if status_code in (401, 403) or consecutive_errors >= MAX_CONSECUTIVE_POLL_ERRORS:
                push_notice(f"⚠️ {dep_name} 連續查詢失敗 ({type(e).__name__}: {e})，已暫停背景輪詢 — 請於 /jobs 按 [C] 重試")
                raise
            backoff = min(config.JOB_POLL_INTERVAL * (2 ** (consecutive_errors - 1)), 600)
            wait(backoff)
            continue

        wait(config.JOB_POLL_INTERVAL)

    clear_live_progress(job_id)
    clear_cancelled(job_id)
    return {"sub_stage": "cancelled", "note": "使用者停止追蹤"}


def route_after_build(state: VMLifecycleState) -> str:
    if state.get("error") or state.get("sub_stage") == "cancelled":
        return END
    # 若因為 sub_stage 轉換為 building 或標記 overdue 而中途存檔 Checkpoint，但尚未拿到 IP，繞回 wait_vm_ready 繼續等待
    if not state.get("ip") or state.get("sub_stage") != "ready":
        return "wait_vm_ready"
    if state.get("boot_disk_gb") or state.get("extra_disk_gb"):
        return "disk_locate"
    if state.get("is_linux", True):
        return "prepare_bootstrap"
    return END


# 2. 硬碟調整：定位 → 人工審核 (interrupt) → 產出 ops 清單 (interrupt_after) → 送出單一動作 (submit_disk_op) → 等待單一動作 (wait_disk_op)
def disk_locate(state: VMLifecycleState) -> dict[str, Any]:
    res = disk_nodes.locate(state)
    exec_err = (res.get("execution_result") or {}).get("error")
    if exec_err:
        return {**res, "error": exec_err}
    return res


def route_disk_locate(state: VMLifecycleState) -> str:
    if (state.get("execution_result") or {}).get("error"):
        return END
    return "disk_review"


def disk_review(state: VMLifecycleState) -> dict[str, Any]:
    res = disk_nodes.review(state)
    if (res.get("execution_result") or {}).get("aborted"):
        # 使用者選擇略過硬碟調整：清除 execution_result 避免影響後續 bootstrap，記錄於 note
        return {
            **res,
            "execution_result": None,
            "ops": [],
            "note": "已略過硬碟調整",
            "error": "",
            "stage_since": time.time(),
            "overdue": False,
        }
    return res


def route_disk_review(state: VMLifecycleState) -> str:
    action = (state.get("human_feedback") or {}).get("action")
    if action == "skip":
        return "prepare_bootstrap" if state.get("is_linux", True) else END
    if state.get("errors") or state.get("blocking"):
        return "disk_review"
    return "disk_finalize"


def disk_finalize(state: VMLifecycleState) -> dict[str, Any]:
    ops = build_ops(state.get("vm_name") or "", state.get("boot_disk_gb"), state.get("extra_disk_gb"))
    now = time.time()
    if not ops:
        win_note = "Windows 尚未支援自動初始設定，請手動完成" if not state.get("is_linux", True) else "沒有需要執行的硬碟動作"
        return {
            "ops": [],
            "execution_result": None,
            "note": win_note,
            "error": "",
            "stage_since": now,
            "overdue": False,
        }
    return {
        "ops": ops,
        "execution_result": None,
        "note": "",
        "error": "",
        "stage_since": now,
        "overdue": False,
    }


def route_after_disk_finalize(state: VMLifecycleState) -> str:
    if state.get("ops"):
        return "submit_disk_op"
    if state.get("is_linux", True):
        return "prepare_bootstrap"
    return END


def _is_extra_disk_already_created(aria: Any, deployment_id: str, disk_prefix: str) -> bool:
    """冪等保險：送出 Add Disk 前，檢查 deployment 中是否已存在同名磁碟資源"""
    if not deployment_id or not disk_prefix:
        return False
    try:
        resources = aria.deployments.resources(deployment_id)
    except Exception:
        return False
    for r in resources or ():
        rtype = (r.get("type") or "").lower()
        if "disk" not in rtype and "volume" not in rtype:
            continue
        props = r.get("properties") or {}
        rname = str(props.get("resourceName") or r.get("name") or "")
        if rname.startswith(disk_prefix):
            return True
    return False


def submit_disk_op(state: VMLifecycleState) -> dict[str, Any]:
    """
    專職送出「單一」待執行的硬碟動作，取得 request_id 後「立即 return」寫入 Checkpoint！
    不在本節點內輪詢等待，確保即使在送出後瞬間重啟，request_id 也已安全存於 Checkpoint 中，絕不重複送出。
    """
    aria = get_aria_client()
    machine_id = state["machine_id"]
    dep_id = state.get("deployment_id") or ""
    ops = [dict(op) for op in (state.get("ops") or [])]
    now = time.time()

    for op in ops:
        if op.get("status") == "success":
            continue
        if op.get("status") == "failed":
            return {"ops": ops, "error": f"{op['label']} 失敗：{op.get('error')}"}
        if op.get("request_id"):
            # 已經有 request_id (例如重啟後進入)，直接交給 wait_disk_op 查詢
            return {"ops": ops}

        # 送出前冪等檢查：若為加掛額外硬碟且該磁碟資源已存在，直接標為完成
        if op.get("action_id") == ADD_DISK:
            prefix = str((op.get("inputs") or {}).get("name") or "")
            if _is_extra_disk_already_created(aria, dep_id, prefix):
                op.update(status="success", error="")
                return {"ops": ops, "note": f"{op['label']} (已存在，略過重複送出)", "last_checked": now}

        resp = aria.deployments.run_resource_action(
            machine_id, op["action_id"], op["inputs"], reason="Aria Agent 硬碟調整"
        )
        op.update(status="running", request_id=resp.get("id"), submitted_at=now)
        return {"ops": ops, "note": f"已送出：{op['label']}", "last_checked": now}

    return {"ops": ops}


def route_after_submit_disk_op(state: VMLifecycleState) -> str:
    if state.get("error") or state.get("sub_stage") == "cancelled":
        return END
    ops = state.get("ops") or []
    if all(op.get("status") == "success" for op in ops):
        return "prepare_bootstrap" if state.get("is_linux", True) else END
    return "wait_disk_op"


def wait_disk_op(state: VMLifecycleState, sleep_fn: Optional[Callable[[float], None]] = None) -> dict[str, Any]:
    """
    專職輪詢「已送出且具備 request_id」的單一硬碟動作狀態 (絕不呼叫 run_resource_action)。
    當該動作完成時立即 return 寫入 Checkpoint，再由 route_disk_op 決定是否還有下一顆硬碟需送出。
    """
    job_id = state.get("job_id") or ""
    dep_name = state.get("deployment_name") or state.get("vm_name") or job_id
    wait = sleep_fn or wait_for_next_poll
    current: dict[str, Any] = dict(state)
    ops = [dict(op) for op in (state.get("ops") or [])]
    target_idx = next((i for i, op in enumerate(ops) if op.get("status") == "running" and op.get("request_id")), None)
    if target_idx is None:
        return {"ops": ops}

    consecutive_errors = 0
    while not is_cancelled(job_id):
        try:
            aria = get_aria_client()
            op = dict(ops[target_idx])
            req = aria.deployments.get_request(op["request_id"])
            consecutive_errors = 0
            status = str(req.get("status") or "").upper()
            now = time.time()

            if status in OK_STATUSES:
                op["status"] = "success"
                ops[target_idx] = op
                all_done = all(x.get("status") == "success" for x in ops)
                clear_live_progress(job_id)
                if all_done:
                    is_linux = state.get("is_linux", True)
                    tail = "等待「③ 初始設定」審核 — 輸入 /jobs 處理" if is_linux else "流程完成"
                    win_note = "Windows 尚未支援自動初始設定，請手動完成" if not is_linux else "硬碟調整完成"
                    push_notice(f"🔔 {dep_name} 硬碟調整完成，{tail}")
                    return {
                        "ops": ops,
                        "note": win_note,
                        "stage_since": now,
                        "overdue": False,
                        "last_checked": now,
                    }
                return {"ops": ops, "note": f"已完成：{op['label']}", "last_checked": now}

            if status in FAIL_STATUSES:
                detail = req.get("details") or status
                op.update(status="failed", error=detail)
                ops[target_idx] = op
                msg = f"{op['label']} 失敗：{detail}"
                push_notice(f"❌ {dep_name} {msg}")
                clear_live_progress(job_id)
                return {"ops": ops, "error": msg, "note": msg, "last_checked": now}

            msg = f"{op['label']} 執行中 ({status or '未知狀態'})"
            current.update(ops=ops, note=msg, last_checked=now)
            reason = _check_disk_overdue(ops, bool(current.get("overdue")), now)
            if reason:
                current["overdue"] = True
                push_notice(f"⏰ {dep_name} {reason}，請確認 Aria 狀態")
                clear_live_progress(job_id)
                # 將 overdue=True 寫入 Checkpoint，再由 route_disk_op 繞回 wait_disk_op 繼續等
                return {"ops": ops, "overdue": True, "note": msg, "last_checked": now}

            set_live_progress(job_id, current)
        except Exception as e:
            consecutive_errors += 1
            logger.exception("追蹤工作 %s 硬碟輪詢失敗 (%d/%d)", job_id, consecutive_errors, MAX_CONSECUTIVE_POLL_ERRORS)
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            err_note = f"上次查詢失敗 ({consecutive_errors}/{MAX_CONSECUTIVE_POLL_ERRORS})：{type(e).__name__}: {e}"
            current["note"] = err_note
            set_live_progress(job_id, current)
            if status_code in (401, 403) or consecutive_errors >= MAX_CONSECUTIVE_POLL_ERRORS:
                push_notice(f"⚠️ {dep_name} 硬碟狀態連續查詢失敗 ({type(e).__name__}: {e})，已暫停背景輪詢 — 請於 /jobs 按 [C] 重試")
                raise
            backoff = min(config.JOB_FAST_POLL_INTERVAL * (2 ** (consecutive_errors - 1)), 300)
            wait(backoff)
            continue

        wait(config.JOB_FAST_POLL_INTERVAL)

    clear_live_progress(job_id)
    clear_cancelled(job_id)
    return {"sub_stage": "cancelled", "note": "使用者停止追蹤"}


def route_disk_op(state: VMLifecycleState) -> str:
    if state.get("error") or state.get("sub_stage") == "cancelled":
        return END
    ops = state.get("ops") or []
    # 若當前動作仍在 running (例如剛因標記 overdue=True 寫入 Checkpoint)，繞回 wait_disk_op 繼續等待
    if any(op.get("status") == "running" and op.get("request_id") for op in ops):
        return "wait_disk_op"
    # 若還有下一顆 pending 的硬碟待送出，進入 submit_disk_op
    if any(op.get("status") != "success" for op in ops):
        return "submit_disk_op"
    if state.get("is_linux", True):
        return "prepare_bootstrap"
    return END


# 3. 初始設定 (Bootstrap)：準備 → 人工審核 (interrupt) → 上傳與逐步執行 → 清理 → 報告
def lifecycle_prepare_bootstrap(state: VMLifecycleState) -> dict[str, Any]:
    seeded: VMLifecycleState = {
        **state,
        "hostname": state.get("hostname") or state.get("deployment_name") or state.get("vm_name"),
        "cs_mode": state.get("cs_mode") or config.CROWDSTRIKE_DEFAULT_MODE,
        "skip_disk": bool(state.get("skip_disk", False)),
    }
    prep = boot_nodes.prepare_bootstrap(seeded)
    return {
        **prep,
        "human_feedback": None,
        "execution_result": None,
        "step_results": state.get("step_results") or [],
        "step_index": state.get("step_index") or 0,
        "stopped": False,
        "stage_since": time.time(),
        "overdue": False,
    }


def bootstrap_review(state: VMLifecycleState) -> dict[str, Any]:
    res = boot_nodes.human_review(state)
    if (res.get("execution_result") or {}).get("aborted"):
        return {**res, "note": "已略過初始設定", "error": ""}
    return res


def route_bootstrap_review(state: VMLifecycleState) -> str:
    nxt = boot_nodes.route_review_result(state)
    if nxt == "human_review":
        return "bootstrap_review"
    return nxt


def bootstrap_finalize(state: VMLifecycleState) -> dict[str, Any]:
    res = boot_nodes.finalize(state)
    exec_res = res.get("execution_result") or state.get("execution_result") or {}
    if exec_res.get("success"):
        return {**res, "note": "全部完成", "error": ""}
    err = exec_res.get("error") or "初始設定未全部成功，可重新執行"
    return {**res, "error": err}
