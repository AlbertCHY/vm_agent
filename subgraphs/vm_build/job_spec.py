"""vm_build 子圖的背景工作規格宣告 (JobSpec)"""
from typing import Any

from subgraphs.jobs.models import DONE, FAILED, KIND_BUILD, WAITING
from subgraphs.jobs.registry import CompletionOutcome, JobSpec, register_job_spec
from subgraphs.vm_build.graph import build_vm_build_graph
from subgraphs.vm_build.state import BUILDING, CANCELLED, READY, SUBMITTED


def _derive_stage(
    next_nodes: tuple[str, ...],
    values: dict[str, Any],
    meta: dict[str, Any],
    has_interrupts: bool,
    bg_running: bool,
) -> tuple[str, str]:
    if not next_nodes:
        if values.get("error"):
            return FAILED, "建立失敗"
        return DONE, "建立完成"
    sub = values.get("sub_stage") or SUBMITTED
    if sub == BUILDING:
        return WAITING, "Aria 建立中"
    return WAITING, "等待 Aria 審核"


def _evaluate_completion(
    job_id: str,
    title: str,
    vals: dict[str, Any],
    meta: dict[str, Any],
) -> CompletionOutcome:
    if vals.get("sub_stage") == CANCELLED:
        return CompletionOutcome(status="cancelled")
    if vals.get("error"):
        return CompletionOutcome(status="failed", error=str(vals.get("error") or ""))
    if vals.get("sub_stage") == READY and vals.get("ip"):
        os_type = "linux" if vals.get("is_linux", True) else "windows"
        carried = {
            "deployment_id": vals.get("deployment_id") or "",
            "deployment_name": vals.get("deployment_name") or title,
            "vm_name": vals.get("vm_name") or "",
            "machine_id": vals.get("machine_id") or "",
            "ip": vals.get("ip") or "",
            "os_type": os_type,
        }
        return CompletionOutcome(
            status="succeeded",
            carried=carried,
            chain_notice_prefix=f"{title} 已建立完成 ({carried['ip']})",
            done_notice=f"✅ {title} 已建立完成 ({carried['ip']})",
        )
    return CompletionOutcome(status="cancelled")


def _derive_extra_fields(values: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    os_type = values.get("os_type") or ("linux" if values.get("is_linux", True) else "windows")
    return {"os_type": str(os_type)}


SPEC = register_job_spec(JobSpec(
    kind=KIND_BUILD,
    label="建機追蹤",
    build_graph=lambda cp: build_vm_build_graph(checkpointer=cp),
    derive_stage=_derive_stage,
    evaluate_completion=_evaluate_completion,
    review_nodes=(),
    active_stage=WAITING,
    auto_resume_on_startup=True,
    derive_extra_fields=_derive_extra_fields,
))
