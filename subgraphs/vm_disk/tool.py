"""adjust_vm_disk：對現有 VM 做硬碟調整 (前景人工審核 → 核准後送入 /jobs 背景執行，可選串接初始設定)"""
from functools import lru_cache
from typing import Annotated, Any, Optional

from langchain_core.tools import InjectedToolCallId, tool

from subgraphs.common.runner import PipelineResult, derive_session_id, last_ai_text, run_pipeline
from subgraphs.vm_disk.graph import build_vm_disk_graph


@lru_cache(maxsize=1)
def get_vm_disk_graph():
    return build_vm_disk_graph()


def _summarize(
    sid: str,
    values: dict[str, Any],
    *,
    background: bool,
    follow_up_bootstrap: bool,
    cs_mode: str,
    os_type: Optional[str],
) -> PipelineResult:
    res = values.get("execution_result") or {}
    if res.get("aborted"):
        return PipelineResult("aborted", sid, last_ai_text(values), data={"ip": values.get("ip")})
    if res.get("no_ops"):
        return PipelineResult("no_change", sid, last_ai_text(values), data={"ip": values.get("ip")})
    if res.get("error"):
        return PipelineResult("failed", sid, last_ai_text(values), data={"error": res.get("error")})

    if background and values.get("ops"):
        from subgraphs.jobs import KIND_BOOTSTRAP, enqueue_disk_job

        next_steps: list[dict[str, Any]] = []
        if follow_up_bootstrap:
            next_steps.append({
                "kind": KIND_BOOTSTRAP,
                "os_type": (os_type or "").strip().lower(),
                "cs_mode": (cs_mode or "NGAV").strip().upper(),
                "skip_disk": False,
            })
        job = enqueue_disk_job(
            values,
            os_type=(os_type or "").strip().lower(),
            next_steps=next_steps,
            needs_review=False,
        )
        ops_desc = "、".join(op.get("label", "") for op in (values.get("ops") or []))
        follow_note = "\n- 硬碟調整完成後，系統將自動產生「初始設定 (Bootstrap)」待審核工作並通知你。" if follow_up_bootstrap else ""
        msg = (
            f"✔ 已核准 `{values.get('vm_name')}` ({values.get('ip')}) 的硬碟調整並送入背景執行（工作編號 `#{job.id}`）：\n"
            f"- 動作：{ops_desc}\n"
            f"- 可隨時輸入 `/jobs` 查看累計輸出與進度，完成後會自動通知。{follow_note}"
        )
        return PipelineResult("submitted", sid, msg, data={
            "job_id": job.id,
            "ops": values.get("ops") or [],
            "machine_id": values.get("machine_id"),
            "vm_name": values.get("vm_name"),
            "ip": values.get("ip"),
        })

    if res.get("state") in ("done", "failed", "timeout"):
        status = {"done": "completed", "failed": "failed", "timeout": "timeout"}[res["state"]]
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
    follow_up_bootstrap: bool = False,
    cs_mode: str = "NGAV",
    os_type: Optional[str] = None,
    tool_call_id: Annotated[Optional[str], InjectedToolCallId] = None,
) -> tuple[str, dict[str, Any]]:
    """
    【硬碟調整 (Day-2)】對 Aria 管理的現有 VM 擴充系統碟 (Resize Boot Disk) 或加掛一顆額外硬碟 (Add Disk)。
    於前景完成人工審核後會立刻送入背景執行 (/jobs)，不阻塞對話，完成後自動通知。
    只有使用者明確要求調整硬碟時才可呼叫。

    參數 ip: 目標 VM 的 IP。
    參數 boot_disk_gb: (選填) 系統碟「擴充後的總大小」(GB)，只能變大。
    參數 extra_disk_gb: (選填) 額外硬碟大小 (GB)；額外硬碟最多一顆，與 VM 同 datastore。
    參數 follow_up_bootstrap: (選填，預設 False) 若使用者要求「掛完硬碟接著做新機基本設定 (Bootstrap)」，
                              設為 True；硬碟背景完成後會自動產生初始設定待審核工作進 /jobs，無須分開呼叫。
    參數 cs_mode: (選填，預設 "NGAV") 當 follow_up_bootstrap=True 時的 CrowdStrike 模式 ("NGAV" / "MDR" / "SKIP")。
    參數 os_type: (選填) "linux" 或 "windows"；不填則由後續流程自動反查。
    """
    sid = derive_session_id("vmdisk", tool_call_id)
    graph_input = {
        "ip": ip.strip(),
        "boot_disk_gb": boot_disk_gb,
        "extra_disk_gb": extra_disk_gb,
        "handoff_to_background": True,
        "errors": [],
        "blocking": [],
        "notes": [],
        "human_feedback": None,
        "execution_result": None,
        "error": "",
        "note": "",
    }
    result = run_pipeline(
        "硬碟調整",
        sid,
        get_vm_disk_graph(),
        graph_input,
        lambda v: _summarize(
            sid, v,
            background=True,
            follow_up_bootstrap=follow_up_bootstrap,
            cs_mode=cs_mode,
            os_type=os_type,
        ),
    )
    return result.to_tool_output()
