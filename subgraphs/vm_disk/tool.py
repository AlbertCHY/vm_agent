"""adjust_vm_disk：對現有 VM 做硬碟調整 (完全由 vm_disk 子圖驅動：定位 → 審核 → 送出 → 等待 → 報告)"""
from functools import lru_cache
from typing import Annotated, Any, Optional

from langchain_core.tools import InjectedToolCallId, tool

from subgraphs.common.runner import PipelineResult, derive_session_id, last_ai_text, run_pipeline
from subgraphs.vm_disk.graph import build_vm_disk_graph


@lru_cache(maxsize=1)
def get_vm_disk_graph():
    return build_vm_disk_graph()


def _summarize(sid: str, values: dict[str, Any]) -> PipelineResult:
    res = values.get("execution_result") or {}
    if res.get("aborted"):
        status = "aborted"
    elif res.get("no_ops"):
        status = "no_change"
    elif res.get("state") in ("done", "failed", "timeout"):
        status = {"done": "completed", "failed": "failed", "timeout": "timeout"}[res["state"]]
    elif res.get("error"):
        status = "failed"
    else:
        status = "completed"
    return PipelineResult(status, sid, last_ai_text(values), data={
        "ops": values.get("ops") or [],
        "machine_id": values.get("machine_id"),
        "vm_name": values.get("vm_name"),
        "ip": values.get("ip"),
    })


@tool(response_format="content_and_artifact")
def adjust_vm_disk(
    ip: str,
    boot_disk_gb: Optional[int] = None,
    extra_disk_gb: Optional[int] = None,
    tool_call_id: Annotated[Optional[str], InjectedToolCallId] = None,
) -> tuple[str, dict[str, Any]]:
    """
    【硬碟調整 (Day-2)】對 Aria 管理的現有 VM 擴充系統碟 (Resize Boot Disk) 或加掛一顆額外硬碟 (Add Disk)，
    經人工審查後送出並等待完成。只有使用者明確要求調整硬碟時才可呼叫。
    參數 ip: 目標 VM 的 IP。
    參數 boot_disk_gb: (選填) 系統碟「擴充後的總大小」(GB)，只能變大。
    參數 extra_disk_gb: (選填) 額外硬碟大小 (GB)；額外硬碟最多一顆，與 VM 同 datastore。
    完成後若需掛載到 /data，可再對該 VM 執行 bootstrap_vm_pipeline 或請使用者手動掛載。
    """
    sid = derive_session_id("vmdisk", tool_call_id)
    graph_input = {
        "ip": ip.strip(),
        "boot_disk_gb": boot_disk_gb,
        "extra_disk_gb": extra_disk_gb,
        "errors": [],
        "blocking": [],
        "notes": [],
        "human_feedback": None,
        "execution_result": None,
        "error": "",
        "note": "",
    }
    result = run_pipeline("硬碟調整", sid, get_vm_disk_graph(), graph_input, lambda v: _summarize(sid, v))
    return result.to_tool_output()
