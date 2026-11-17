"""VM 硬碟調整子圖：定位機器 → 人工審核 (interrupt) → 輸出已核准的 Day-2 動作清單

本子圖只負責「組合與審核」，不執行動作：
- 追蹤工作：核准的動作交給背景監看器依序執行
- adjust_vm_disk 工具：核准後由工具同步執行 (executor.run_until_done)
"""
from typing import Annotated, Any, Optional

from langchain_core.messages import AIMessage, BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from clients.aria import get_aria_client
from subgraphs.common.review import APPROVE, EDIT, SKIP, ReviewField, ask_review, review_request
from subgraphs.vm_disk.executor import ADD_DISK, RESIZE_BOOT_DISK, build_ops, extra_disk_name, locate_machine


class DiskState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    deployment_id: Optional[str]
    ip: Optional[str]
    vm_name: str
    machine_id: str
    boot_disk_gb: Optional[int]
    extra_disk_gb: Optional[int]
    notes: list[str]
    errors: list[str]          # 上一輪修改內容的錯誤
    blocking: list[str]        # 機器狀態造成無法執行的問題 (例如動作 valid=false)
    human_feedback: Optional[dict[str, Any]]
    ops: list[dict[str, Any]]
    execution_result: Optional[dict[str, Any]]


def locate(state: DiskState) -> dict[str, Any]:
    aria = get_aria_client()
    if state.get("machine_id") and state.get("vm_name"):
        info = {}
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
    decision = ask_review(review_request(
        kind="vm_disk",
        title="💽 待審核的硬碟調整 (Day-2)",
        message=f"VM `{vm}` 的硬碟調整計畫，請人工審查確認。留空代表不執行該項。",
        fields=[
            ReviewField(key="vm_name", label="VM 名稱", value=vm, editable=False),
            ReviewField(key="ip", label="IP", value=state.get("ip"), editable=False),
            ReviewField(key="boot_disk_gb", label="系統碟擴充後的總大小 (GB)", value=boot or "(不調整)", editable=True),
            ReviewField(key="extra_disk_gb", label="額外硬碟大小 (GB)", value=extra or "(不加掛)", editable=True),
            ReviewField(key="extra_disk_name", label="額外硬碟名稱 (Aria 會補 6 位流水號)",
                        value=extra_disk_name(vm) if extra else "", editable=False),
        ],
        actions=[APPROVE, EDIT, SKIP],
        errors=(state.get("blocking") or []) + (state.get("errors") or []),
        notes=state.get("notes") or [],
    ))
    feedback = decision.model_dump()
    if decision.action == "skip":
        return {"human_feedback": feedback,
                "execution_result": {"aborted": True, "reason": decision.feedback or "使用者略過硬碟調整"},
                "messages": [AIMessage(content=f"⏭ 已略過 `{vm}` 的硬碟調整。")]}

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
    return "review" if state.get("errors") or state.get("blocking") else "finalize"


def finalize(state: DiskState) -> dict[str, Any]:
    ops = build_ops(state.get("vm_name") or "", state.get("boot_disk_gb"), state.get("extra_disk_gb"))
    if not ops:
        return {"ops": [], "execution_result": {"approved": True, "no_ops": True},
                "messages": [AIMessage(content="沒有需要執行的硬碟動作。")]}
    return {"ops": ops, "execution_result": {"approved": True},
            "messages": [AIMessage(content="已核准硬碟調整：" + "；".join(op["label"] for op in ops))]}


def route_locate(state: DiskState) -> str:
    return END if state.get("execution_result") else "review"


def build_vm_disk_graph(checkpointer=None):
    b = StateGraph(DiskState)
    b.add_node("locate", locate)
    b.add_node("review", review)
    b.add_node("finalize", finalize)
    b.add_edge(START, "locate")
    b.add_conditional_edges("locate", route_locate, {"review": "review", END: END})
    b.add_conditional_edges("review", route_review, {"review": "review", "finalize": "finalize", END: END})
    b.add_edge("finalize", END)
    return b.compile(checkpointer=checkpointer)
