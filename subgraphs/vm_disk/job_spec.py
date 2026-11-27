"""vm_disk 子圖的背景工作規格宣告 (JobSpec)"""
from typing import Any, Optional

from subgraphs.jobs.models import DONE, FAILED, KIND_DISK, REVIEW, RUNNING
from subgraphs.jobs.registry import CompletionOutcome, JobSpec, register_job_spec
from subgraphs.vm_disk.graph import build_vm_disk_graph


def _derive_stage(
    next_nodes: tuple[str, ...],
    values: dict[str, Any],
    meta: dict[str, Any],
    has_interrupts: bool,
    bg_running: bool,
) -> tuple[str, str]:
    if not next_nodes:
        exec_res = values.get("execution_result") or {}
        if exec_res.get("aborted"):
            return DONE, "已略過硬碟調整"
        if exec_res.get("no_ops"):
            return DONE, "無需硬碟調整"
        if values.get("error") or exec_res.get("error") or exec_res.get("state") in ("failed", "timeout"):
            return FAILED, "硬碟調整失敗"
        return DONE, "硬碟調整完成"
    if any(n in next_nodes for n in ("locate", "review")):
        return REVIEW, "等你審核：硬碟調整"
    if any(n in next_nodes for n in ("finalize", "submit_disk_op", "wait_disk_op", "report")):
        return RUNNING, "硬碟調整執行中"
    return REVIEW, "等你審核：硬碟調整"


def _evaluate_completion(
    job_id: str,
    title: str,
    vals: dict[str, Any],
    meta: dict[str, Any],
) -> CompletionOutcome:
    if vals.get("sub_stage") == "cancelled":
        return CompletionOutcome(status="cancelled")
    exec_res = vals.get("execution_result") or {}
    is_skipped = bool(exec_res.get("aborted") or exec_res.get("no_ops"))
    err = vals.get("error") or exec_res.get("error") or ""
    if not is_skipped and (err or exec_res.get("state") in ("failed", "timeout")):
        reason = err or exec_res.get("message") or "硬碟調整失敗"
        return CompletionOutcome(
            status="failed",
            log_message=f"❌ 硬碟調整失敗：{reason}",
            done_notice=f"❌ {title} 硬碟調整失敗：{reason} — 輸入 /jobs 查看詳細輸出",
            error=reason,
        )

    pending_in = dict(meta.get("pending_input") or {})
    ip = vals.get("ip") or pending_in.get("ip") or ""
    carried = {
        "deployment_id": vals.get("deployment_id") or pending_in.get("deployment_id") or "",
        "deployment_name": vals.get("deployment_name") or vals.get("vm_name") or pending_in.get("deployment_name") or title,
        "vm_name": vals.get("vm_name") or pending_in.get("vm_name") or "",
        "machine_id": vals.get("machine_id") or pending_in.get("machine_id") or "",
        "ip": ip,
        "os_type": meta.get("os_type") or pending_in.get("os_type") or "",
    }
    if is_skipped:
        skip_reason = "已略過硬碟調整" if exec_res.get("aborted") else "無需硬碟調整"
        return CompletionOutcome(
            status="skipped",
            carried=carried,
            log_message=f"⏭️ {skip_reason}",
            chain_notice_prefix=f"{title} {skip_reason}",
        )

    return CompletionOutcome(
        status="succeeded",
        carried=carried,
        log_message=f"✅ 硬碟調整完成 ({ip})",
        chain_notice_prefix=f"{title} 硬碟調整完成",
        done_notice=f"✅ {title} 硬碟調整完成 ({ip}) — 輸入 /jobs 查看結果",
    )


def _enqueue_chained(merged: dict[str, Any], rest: list[dict[str, Any]], checkpointer: Any):
    from subgraphs.jobs.manager import enqueue_disk_job

    return enqueue_disk_job(
        deployment_id=merged.get("deployment_id"),
        deployment_name=merged.get("deployment_name") or merged.get("vm_name") or "",
        ip=merged.get("ip") or "",
        vm_name=merged.get("vm_name") or "",
        machine_id=merged.get("machine_id") or "",
        boot_disk_gb=merged.get("boot_disk_gb"),
        extra_disk_gb=merged.get("extra_disk_gb"),
        os_type=merged.get("os_type") or "",
        next_steps=rest,
        needs_review=True,
        auto_start=False,
        checkpointer=checkpointer,
    )


def _derive_extra_fields(values: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    return {"os_type": str(values.get("os_type") or "")}


def _detail_table(job: Any) -> Optional[tuple[str, tuple[str, ...], list[tuple[str, ...]]]]:
    if not job.disk_ops:
        return None
    rows = [
        (str(op.get("label") or ""), str(op.get("status") or ""), str(op.get("error") or ""))
        for op in job.disk_ops
    ]
    return "💽 硬碟動作明細", ("動作", "狀態", "摘要"), rows


SPEC = register_job_spec(JobSpec(
    kind=KIND_DISK,
    label="硬碟調整",
    build_graph=lambda cp: build_vm_disk_graph(checkpointer=cp, interrupt_after_finalize=True),
    derive_stage=_derive_stage,
    evaluate_completion=_evaluate_completion,
    enqueue_chained=_enqueue_chained,
    review_nodes=("locate", "review"),
    active_stage=RUNNING,
    auto_resume_on_startup=True,
    derive_extra_fields=_derive_extra_fields,
    detail_table=_detail_table,
))
