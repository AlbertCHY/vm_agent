"""VM 建機追蹤子圖節點：背景輪詢 Aria 審核與建機狀態，直到取得 IP 或失敗/取消"""
import time
from typing import Any, Callable, Optional

import requests
from langgraph.graph import END

import config
from clients.aria import get_aria_client
from core.progress import emit
from subgraphs.common.runner import MAX_CONSECUTIVE_POLL_ERRORS, push_notice, run_poll_loop
from subgraphs.vm_build.state import BUILDING, CANCELLED, READY, SUBMITTED, BuildState
from subgraphs.vm_disk.executor import FAIL_STATUSES, locate_machine

APPROVAL_WAIT_STATUSES = (
    "APPROVAL_PENDING",
    "CHECKING_APPROVAL",
    "INITIALIZATION",
    "CREATED",
    "PENDING",
)
BUILDING_STATUSES = ("INPROGRESS", "COMPLETION")


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


def poll_deployment_once(
    state: dict[str, Any],
    aria: Any,
    now: Optional[float] = None,
) -> tuple[str, dict[str, Any], list[str]]:
    """
    對 Aria 查詢一次 deployment 狀態。
    回傳 (outcome, state_updates, notices)：outcome 為 "waiting" | "ready" | "failed"
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

        return (
            "ready",
            {
                "vm_name": info["vm_name"],
                "machine_id": info["machine_id"],
                "ip": info["ip"],
                "sub_stage": READY,
                "stage_since": now,
                "overdue": False,
                "note": f"已建立完成 ({info['ip']})",
                "error": "",
                "last_checked": now,
            },
            notices,
        )

    if r_status in APPROVAL_WAIT_STATUSES or not r_status:
        updates = {"sub_stage": SUBMITTED, "note": "等待 Aria 審核中", "last_checked": now}
    elif r_status in BUILDING_STATUSES:
        updates = {"sub_stage": BUILDING, "note": "Aria 建立 VM 中", "last_checked": now}
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


def init_build(state: BuildState) -> dict[str, Any]:
    now = time.time()
    return {
        "sub_stage": state.get("sub_stage") or SUBMITTED,
        "stage_since": state.get("stage_since") or now,
        "last_checked": state.get("last_checked") or 0.0,
        "overdue": bool(state.get("overdue", False)),
        "error": state.get("error") or "",
        "note": state.get("note") or "",
    }


def wait_vm_ready(state: BuildState, sleep_fn: Optional[Callable[[float], None]] = None) -> dict[str, Any]:
    job_id = state.get("job_id") or ""
    dep_name = state.get("deployment_name") or state.get("deployment_id") or job_id
    last_logged = {"sub": state.get("sub_stage") or "", "note": ""}

    def _poll_once(current: dict[str, Any]) -> Optional[dict[str, Any]]:
        prev_sub = current.get("sub_stage") or SUBMITTED
        prev_overdue = bool(current.get("overdue", False))
        outcome, updates, notices = poll_deployment_once(current, get_aria_client())
        current.update(updates)
        for n in notices:
            push_notice(n)
            emit(n, stage="vm_build")

        cur_sub = current.get("sub_stage") or ""
        cur_note = current.get("note") or ""
        if (cur_sub, cur_note) != (last_logged["sub"], last_logged["note"]) and cur_note:
            emit(f"[VM 建立] {dep_name}：{cur_note}", stage="vm_build")
            last_logged["sub"], last_logged["note"] = cur_sub, cur_note

        stage_changed = cur_sub != prev_sub or bool(current.get("overdue")) != prev_overdue
        if outcome in ("ready", "failed") or stage_changed:
            return {
                k: current[k]
                for k in (
                    "vm_name", "machine_id", "ip", "sub_stage", "stage_since",
                    "overdue", "note", "error", "last_checked",
                )
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


def route_after_build(state: BuildState) -> str:
    if state.get("error") or state.get("sub_stage") == CANCELLED:
        return END
    if state.get("ip") and state.get("sub_stage") == READY:
        return END
    return "wait_vm_ready"
