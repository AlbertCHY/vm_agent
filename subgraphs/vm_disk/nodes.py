"""VM 硬碟調整子圖節點 (Day-2：定位 → 審核 → 送出單一動作 → 等待完成 → 報告)

- submit_disk_op 採單動作送出並立即寫入 Checkpoint，送出 Add Disk 前具備冪等防呆檢查，
  供前景工具 (adjust_vm_disk) 與背景生命週期子圖 (VMLifecycleGraph) 共用。
- wait_disk_op 為前景同步等待節點；背景等待由 VMLifecycleGraph 使用同一套 poll_disk_op_once 驅動。
"""
import time
from typing import Any, Callable, Optional

from langchain_core.messages import AIMessage
from langgraph.graph import END

import config
from clients.aria import get_aria_client
from core.progress import emit
from core.secrets import get_audit_sink
from subgraphs.common.review import APPROVE, EDIT_REVIEW, SKIP, ReviewField, ask_review, review_request
from subgraphs.common.runner import MAX_CONSECUTIVE_POLL_ERRORS
from subgraphs.vm_disk.executor import (
    ADD_DISK,
    RESIZE_BOOT_DISK,
    build_ops,
    extra_disk_name,
    is_extra_disk_already_created,
    locate_machine,
    poll_disk_op_once,
)
from subgraphs.vm_disk.state import DiskState

FOREGROUND_POLL_INTERVAL = 10.0


def locate(state: DiskState) -> dict[str, Any]:
    aria = get_aria_client()
    if state.get("machine_id") and state.get("vm_name"):
        info: dict[str, Any] = {}
    else:
        try:
            info = locate_machine(aria, state.get("deployment_id"), state.get("ip"))
        except Exception as e:
            msg = f"❌ 無法定位機器：{e}"
            return {"execution_result": {"error": str(e)}, "messages": [AIMessage(content=msg)]}
    machine_id = info.get("machine_id") or state.get("machine_id")
    notes: list[str] = []
    errors: list[str] = []
    try:
        actions = {a["id"]: a for a in aria.deployments.resource_actions(machine_id)}
        for aid, label in ((RESIZE_BOOT_DISK, "Resize Boot Disk"), (ADD_DISK, "Add Disk")):
            if aid not in actions:
                errors.append(f"此機器沒有 {label} 動作")
            elif not actions[aid].get("valid", True):
                errors.append(f"{label} 目前不可執行 (Aria 回報 valid=false)")
        resize_form = aria.deployments.resource_action(machine_id, RESIZE_BOOT_DISK)
        default = (((resize_form.get("schema") or {}).get("properties") or {}).get("capacityGb") or {}).get("default")
        if default:
            notes.append(f"Resize Boot Disk 表單預設值為 {default} GB；填寫的是擴充後的總大小，只能變大")
    except Exception as e:
        notes.append(f"無法讀取 Day-2 動作資訊：{e}")
    return {**info, "machine_id": machine_id, "notes": notes, "blocking": errors, "errors": []}


def route_locate(state: DiskState) -> str:
    return END if state.get("execution_result") else "review"


def _to_size(raw: Any) -> tuple[Optional[int], Optional[str]]:
    text = str(raw or "").strip().upper().removesuffix("GB").removesuffix("G").strip()
    if text in ("", "0", "NONE", "-"):
        return None, None
    try:
        n = int(text)
    except ValueError:
        return None, f"硬碟大小必須是整數 (GB)：{raw}"
    return (n, None) if n > 0 else (None, f"硬碟大小必須大於 0：{raw}")


def review(state: DiskState) -> dict[str, Any]:
    vm = state.get("vm_name") or ""
    boot, extra = state.get("boot_disk_gb"), state.get("extra_disk_gb")
    notes = list(state.get("notes") or [])
    notes.append("輸入 0 或 none 可取消該項調整 (例如：boot_disk_gb=0, extra_disk_gb=200)；修改後會先回到此畫面確認，再按 [A] 執行。")
    decision = ask_review(review_request(
        kind="vm_disk",
        title="💽 待審核的硬碟調整 (Day-2)",
        message=f"VM `{vm}` 的硬碟調整計畫，請人工審查確認。留空或填 0 代表不執行該項。",
        fields=[
            ReviewField(key="vm_name", label="VM 名稱", value=vm, editable=False),
            ReviewField(key="ip", label="IP", value=state.get("ip"), editable=False),
            ReviewField(
                key="boot_disk_gb",
                label="系統碟擴充後的總大小 (GB)",
                value=boot or "(不調整)",
                editable=True,
                options="整數 GB；填 0 或 none 代表不調整",
            ),
            ReviewField(
                key="extra_disk_gb",
                label="額外硬碟大小 (GB)",
                value=extra or "(不加掛)",
                editable=True,
                options="整數 GB；填 0 或 none 代表不加掛",
            ),
            ReviewField(key="extra_disk_name", label="額外硬碟名稱 (Aria 會補 6 位流水號)",
                        value=extra_disk_name(vm) if extra else "", editable=False),
        ],
        actions=[APPROVE, EDIT_REVIEW, SKIP],
        errors=(state.get("blocking") or []) + (state.get("errors") or []),
        notes=notes,
    ))
    feedback = decision.model_dump()
    if decision.action == "skip":
        return {
            "human_feedback": feedback,
            "execution_result": {"aborted": True, "reason": decision.feedback or "使用者略過硬碟調整"},
            "messages": [AIMessage(content=f"⏭ 已略過 `{vm}` 的硬碟調整。")],
        }

    updates: dict[str, Any] = {"human_feedback": feedback}
    errors: list[str] = []
    for key, raw in decision.edits.items():
        if key not in ("boot_disk_gb", "extra_disk_gb"):
            errors.append(f"不可修改或未知的欄位：{key}")
            continue
        size, err = _to_size(raw)
        if err:
            errors.append(err)
        else:
            updates[key] = size
    updates["errors"] = errors
    return updates


def route_review(state: DiskState) -> str:
    action = (state.get("human_feedback") or {}).get("action")
    if action == "skip":
        return END
    if action == "edit":
        return "review"
    return "review" if state.get("errors") or state.get("blocking") else "finalize"


def finalize(state: DiskState) -> dict[str, Any]:
    ops = build_ops(state.get("vm_name") or "", state.get("boot_disk_gb"), state.get("extra_disk_gb"))
    if not ops:
        return {
            "ops": [],
            "execution_result": {"approved": True, "no_ops": True},
            "messages": [AIMessage(content="沒有需要執行的硬碟動作。")],
        }
    return {
        "ops": ops,
        "error": "",
        "note": "",
        "execution_result": None,
    }


def route_after_finalize(state: DiskState) -> str:
    return "submit_disk_op" if state.get("ops") else END


def submit_disk_op(state: DiskState, aria: Any = None) -> dict[str, Any]:
    """
    專職送出「單一」待執行的硬碟動作，取得 request_id 後立即 return 寫入 Checkpoint。
    送出 Add Disk 前會檢查 deployment 是否已存在同名磁碟 (冪等保護)。
    """
    client = aria or get_aria_client()
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
            return {"ops": ops}

        if op.get("action_id") == ADD_DISK:
            prefix = str((op.get("inputs") or {}).get("name") or "")
            if is_extra_disk_already_created(client, dep_id, prefix):
                op.update(status="success", error="")
                msg = f"{op['label']} (已存在，略過重複送出)"
                emit(f"[硬碟調整] {msg}", stage="vm_disk")
                return {"ops": ops, "note": msg, "last_checked": now}

        resp = client.deployments.run_resource_action(
            machine_id, op["action_id"], op["inputs"], reason="Aria Agent 硬碟調整"
        )
        op.update(status="running", request_id=resp.get("id"), submitted_at=now)
        msg = f"已送出：{op['label']}"
        emit(f"[硬碟調整] {msg}", stage="vm_disk")
        return {"ops": ops, "note": msg, "last_checked": now}

    return {"ops": ops}


def route_after_submit(state: DiskState) -> str:
    if state.get("error"):
        return "report"
    ops = state.get("ops") or []
    if all(op.get("status") == "success" for op in ops):
        return "report"
    return "wait_disk_op"


def wait_disk_op(
    state: DiskState,
    sleep_fn: Optional[Callable[[float], None]] = None,
    poll_s: float = FOREGROUND_POLL_INTERVAL,
) -> dict[str, Any]:
    """前景同步等待單一已送出的硬碟動作完成 (含逾時保護)"""
    aria = get_aria_client()
    wait = sleep_fn or time.sleep
    ops = [dict(op) for op in (state.get("ops") or [])]
    target_idx = next((i for i, op in enumerate(ops) if op.get("status") == "running" and op.get("request_id")), None)
    if target_idx is None:
        return {"ops": ops}

    started = ops[target_idx].get("submitted_at") or time.time()
    deadline = started + config.JOB_DAY2_TIMEOUT_MINUTES * 60
    last_msg = ""
    consecutive_errors = 0

    while True:
        try:
            outcome, ops, msg = poll_disk_op_once(aria, ops, target_idx)
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            now = time.time()
            if consecutive_errors >= MAX_CONSECUTIVE_POLL_ERRORS:
                err = f"{ops[target_idx]['label']} 狀態查詢連續失敗 ({type(e).__name__}: {e})；request_id={ops[target_idx].get('request_id')}"
                return {"ops": ops, "error": err, "note": err, "last_checked": now}
            emit(f"[硬碟調整] 查詢失敗 ({consecutive_errors}/{MAX_CONSECUTIVE_POLL_ERRORS})：{e}，稍後重試", stage="vm_disk")
            wait(min(poll_s * (2 ** (consecutive_errors - 1)), 300))
            continue
        now = time.time()
        if msg != last_msg:
            emit(f"[硬碟調整] {msg}", stage="vm_disk")
            last_msg = msg

        if outcome == "success":
            return {"ops": ops, "note": msg, "last_checked": now}
        if outcome == "failed":
            return {"ops": ops, "error": msg, "note": msg, "last_checked": now}
        if now > deadline:
            timeout_msg = f"等待逾時：{msg}"
            return {
                "ops": ops,
                "note": timeout_msg,
                "last_checked": now,
                "execution_result": {"state": "timeout", "message": timeout_msg, "ops": ops},
            }
        wait(poll_s)


def route_after_wait(state: DiskState) -> str:
    if state.get("error") or state.get("execution_result"):
        return "report"
    ops = state.get("ops") or []
    if any(op.get("status") == "running" and op.get("request_id") for op in ops):
        return "wait_disk_op"
    if any(op.get("status") != "success" for op in ops):
        return "submit_disk_op"
    return "report"


def report(state: DiskState) -> dict[str, Any]:
    ops = state.get("ops") or []
    ip = state.get("ip") or ""
    vm_name = state.get("vm_name") or ""
    existing = state.get("execution_result") or {}

    if existing.get("state") == "timeout":
        exec_state = "timeout"
        msg = existing.get("message") or "等待逾時"
    elif state.get("error"):
        exec_state = "failed"
        msg = state["error"]
    else:
        exec_state = "done"
        msg = "硬碟調整完成"

    get_audit_sink().record("vm_disk_adjusted", ip=ip, ops=ops, state=exec_state)
    lines = [f"- {op['label']}：{op['status']}" + (f" ({op['error']})" if op.get("error") else "") for op in ops]
    text = f"VM `{vm_name}` ({ip})：{msg}\n" + "\n".join(lines)
    return {
        "execution_result": {"state": exec_state, "ops": ops, "error": state.get("error") or ""},
        "messages": [AIMessage(content=text)],
    }
