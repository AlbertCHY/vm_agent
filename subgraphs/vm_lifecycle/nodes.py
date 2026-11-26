"""VM 生命週期子圖節點

核心設計：
1. 「等待輪詢節點 (wait_vm_ready / wait_disk_op)」與「人工審核節點 (disk_review / bootstrap_review)」嚴格分離。
   Resume 時只會從審核節點開頭重跑，絕不會重新執行前面的狀態掃描。
2. 硬碟動作拆分為 submit_disk_op 與 wait_disk_op 兩個獨立節點：
   - submit_disk_op：送出單一硬碟動作取得 request_id 後「立即 return」寫入 Checkpoint，並於送出前檢查資源是否已存在 (冪等保護)。
   - wait_disk_op：只根據 Checkpoint 中已有的 request_id 查詢狀態，重啟後絕不會重複送出硬碟申請。
3. 背景通知佇列、即時進度快取與喚醒事件統一由 subgraphs.common.runner 提供。
"""
import logging
import time
from typing import Any, Callable, Optional

import requests
from langgraph.graph import END

import config
from clients.aria import get_aria_client
from subgraphs.common.runner import MAX_CONSECUTIVE_POLL_ERRORS, push_notice, run_poll_loop
from subgraphs.vm_bootstrap import nodes as boot_nodes
from subgraphs.vm_disk import nodes as disk_nodes
from subgraphs.vm_disk.executor import FAIL_STATUSES, build_ops, locate_machine, poll_disk_op_once
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

    if r_status in FAIL_STATUSES or d_status.endswith("FAILED"):
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
        if needs_disk:
            tail = "等待「② 硬碟調整」審核 — 輸入 /jobs 處理"
        else:
            tail = "等待「③ 初始設定」審核 — 輸入 /jobs 處理"
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
                "note": "",
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

    def _poll_once(current: dict[str, Any]) -> Optional[dict[str, Any]]:
        prev_sub = current.get("sub_stage") or SUBMITTED
        prev_overdue = bool(current.get("overdue", False))
        outcome, updates, notices = poll_deployment_once(current, get_aria_client())
        current.update(updates)
        for n in notices:
            push_notice(n)

        stage_changed = current.get("sub_stage") != prev_sub or bool(current.get("overdue")) != prev_overdue
        if outcome in ("ready", "failed") or stage_changed:
            return {
                k: current[k]
                for k in ("vm_name", "machine_id", "ip", "sub_stage", "stage_since",
                          "overdue", "note", "error", "last_checked")
                if k in current
            }
        return None

    return run_poll_loop(
        task_id=job_id,
        label=dep_name,
        interval=config.JOB_POLL_INTERVAL,
        max_backoff=600,
        poll_once=_poll_once,
        initial_progress=state,
        wait_fn=sleep_fn,
        max_errors=MAX_CONSECUTIVE_POLL_ERRORS,
        error_label="輪詢",
    )


def route_after_build(state: VMLifecycleState) -> str:
    if state.get("error") or state.get("sub_stage") == "cancelled":
        return END
    if not state.get("ip") or state.get("sub_stage") != "ready":
        return "wait_vm_ready"
    if state.get("boot_disk_gb") or state.get("extra_disk_gb"):
        return "disk_locate"
    return "prepare_bootstrap"


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
        return "prepare_bootstrap"
    if state.get("errors") or state.get("blocking"):
        return "disk_review"
    return "disk_finalize"


def disk_finalize(state: VMLifecycleState) -> dict[str, Any]:
    ops = build_ops(state.get("vm_name") or "", state.get("boot_disk_gb"), state.get("extra_disk_gb"))
    now = time.time()
    if not ops:
        return {
            "ops": [],
            "execution_result": None,
            "note": "沒有需要執行的硬碟動作",
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
    return "prepare_bootstrap"


def submit_disk_op(state: VMLifecycleState) -> dict[str, Any]:
    """委派給 subgraphs.vm_disk.nodes.submit_disk_op 執行單一硬碟送出與冪等保護"""
    return disk_nodes.submit_disk_op(state, aria=get_aria_client())


def route_after_submit_disk_op(state: VMLifecycleState) -> str:
    if state.get("error") or state.get("sub_stage") == "cancelled":
        return END
    ops = state.get("ops") or []
    if all(op.get("status") == "success" for op in ops):
        return "prepare_bootstrap"
    return "wait_disk_op"


def wait_disk_op(state: VMLifecycleState, sleep_fn: Optional[Callable[[float], None]] = None) -> dict[str, Any]:
    """
    專職輪詢「已送出且具備 request_id」的單一硬碟動作狀態 (絕不呼叫 run_resource_action)。
    當該動作完成時立即 return 寫入 Checkpoint，再由 route_disk_op 決定是否還有下一顆硬碟需送出。
    """
    job_id = state.get("job_id") or ""
    dep_name = state.get("deployment_name") or state.get("vm_name") or job_id
    ops = [dict(op) for op in (state.get("ops") or [])]
    target_idx = next((i for i, op in enumerate(ops) if op.get("status") == "running" and op.get("request_id")), None)
    if target_idx is None:
        return {"ops": ops}

    def _poll_once(current: dict[str, Any]) -> Optional[dict[str, Any]]:
        now = time.time()
        outcome, updated_ops, msg = poll_disk_op_once(get_aria_client(), current["ops"], target_idx)
        current["ops"] = updated_ops

        if outcome == "success":
            all_done = all(x.get("status") == "success" for x in updated_ops)
            if all_done:
                push_notice(f"🔔 {dep_name} 硬碟調整完成，等待「③ 初始設定」審核 — 輸入 /jobs 處理")
                return {
                    "ops": updated_ops,
                    "note": "硬碟調整完成",
                    "stage_since": now,
                    "overdue": False,
                    "last_checked": now,
                }
            return {"ops": updated_ops, "note": msg, "last_checked": now}

        if outcome == "failed":
            push_notice(f"❌ {dep_name} {msg}")
            return {"ops": updated_ops, "error": msg, "note": msg, "last_checked": now}

        current.update(note=msg, last_checked=now)
        reason = _check_disk_overdue(updated_ops, bool(current.get("overdue")), now)
        if reason:
            current["overdue"] = True
            push_notice(f"⏰ {dep_name} {reason}，請確認 Aria 狀態")
            return {"ops": updated_ops, "overdue": True, "note": msg, "last_checked": now}
        return None

    return run_poll_loop(
        task_id=job_id,
        label=f"{dep_name} 硬碟狀態",
        interval=config.JOB_FAST_POLL_INTERVAL,
        max_backoff=300,
        poll_once=_poll_once,
        initial_progress={**state, "ops": ops},
        wait_fn=sleep_fn,
        max_errors=MAX_CONSECUTIVE_POLL_ERRORS,
        error_label="硬碟輪詢",
    )


def route_disk_op(state: VMLifecycleState) -> str:
    if state.get("error") or state.get("sub_stage") == "cancelled":
        return END
    ops = state.get("ops") or []
    if any(op.get("status") == "running" and op.get("request_id") for op in ops):
        return "wait_disk_op"
    if any(op.get("status") != "success" for op in ops):
        return "submit_disk_op"
    return "prepare_bootstrap"


# 3. 初始設定 (Bootstrap)：準備 → 人工審核 (interrupt) → 上傳與逐步執行 → 清理 → 報告
def lifecycle_prepare_bootstrap(state: VMLifecycleState) -> dict[str, Any]:
    seeded: VMLifecycleState = {
        **state,
        "os_type": state.get("os_type") or ("linux" if state.get("is_linux", True) else "windows"),
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
