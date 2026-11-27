"""vm_bootstrap 子圖的背景工作規格宣告 (JobSpec)"""
from typing import Any, Callable, Optional

from core.secrets import get_secret_store
from subgraphs.jobs.models import DONE, FAILED, KIND_BOOTSTRAP, RETRY, REVIEW, RUNNING
from subgraphs.jobs.registry import CompletionOutcome, JobSpec, register_job_spec
from subgraphs.vm_bootstrap import nodes as boot_nodes
from subgraphs.vm_bootstrap.graph import build_vm_bootstrap_graph
from subgraphs.vm_bootstrap.steps import get_bootstrap_plan, normalize_os_type


def _derive_stage(
    next_nodes: tuple[str, ...],
    values: dict[str, Any],
    meta: dict[str, Any],
    has_interrupts: bool,
    bg_running: bool,
) -> tuple[str, str]:
    os_type = normalize_os_type(values.get("os_type") or (meta.get("pending_input") or {}).get("os_type"))
    total_steps = len(get_bootstrap_plan(os_type).steps)
    step_idx = int(values.get("step_index") or 0)
    if not next_nodes:
        exec_res = values.get("execution_result") or {}
        if exec_res.get("aborted"):
            return DONE, "已略過初始設定"
        if exec_res.get("success"):
            return DONE, "初始設定完成"
        return FAILED, "初始設定失敗"
    if any(n in next_nodes for n in ("prepare_bootstrap", "human_review")) or has_interrupts:
        return REVIEW, "等你審核：初始設定"
    if any(n in next_nodes for n in ("connect_and_upload", "run_step", "cleanup", "finalize")):
        if bg_running:
            shown_step = min(step_idx + 1, total_steps) if total_steps else step_idx
            return RUNNING, f"初始設定執行中 ({shown_step}/{total_steps})"
        return RETRY, f"初始設定待續 (第 {min(step_idx + 1, max(total_steps, 1))} 步)"
    return REVIEW, "等你審核：初始設定"


def _evaluate_completion(
    job_id: str,
    title: str,
    vals: dict[str, Any],
    meta: dict[str, Any],
) -> CompletionOutcome:
    if vals.get("sub_stage") == "cancelled":
        return CompletionOutcome(status="cancelled")
    exec_res = vals.get("execution_result") or {}
    if exec_res.get("aborted"):
        return CompletionOutcome(status="aborted")
    ip = vals.get("ip") or ""
    if exec_res.get("success"):
        carried = {
            "deployment_id": vals.get("deployment_id") or "",
            "deployment_name": vals.get("deployment_name") or vals.get("hostname") or title,
            "ip": ip,
            "os_type": vals.get("os_type") or "",
        }
        return CompletionOutcome(
            status="succeeded",
            carried=carried,
            log_message=f"🎉 初始設定全部完成 ({ip})",
            chain_notice_prefix=f"{title} 初始設定完成",
            done_notice=f"🎉 {title} ({ip}) 初始設定完成！— 輸入 /jobs 查看完整報告",
        )

    err = exec_res.get("error") or "初始設定未全部成功，可從斷點續跑"

    def _on_failure(jid: str, cp: Any) -> None:
        from subgraphs.jobs.manager import prepare_bootstrap_retry

        prepare_bootstrap_retry(jid, error=err, checkpointer=cp)

    return CompletionOutcome(
        status="failed",
        error=err,
        done_notice=f"❌ {title} ({ip}) 初始設定未完成：{err} — 輸入 /jobs 查看累計輸出或從斷點續跑",
        on_failure=_on_failure,
    )


def _enqueue_chained(merged: dict[str, Any], rest: list[dict[str, Any]], checkpointer: Any):
    import config
    from subgraphs.jobs.manager import enqueue_bootstrap_job

    return enqueue_bootstrap_job(
        ip=merged.get("ip") or "",
        hostname=merged.get("hostname") or merged.get("deployment_name") or merged.get("vm_name") or "",
        os_type=merged.get("os_type") or "",
        cs_mode=merged.get("cs_mode") or config.CROWDSTRIKE_DEFAULT_MODE,
        skip_disk=bool(merged.get("skip_disk", False)),
        deployment_id=merged.get("deployment_id"),
        deployment_name=merged.get("deployment_name") or merged.get("vm_name") or "",
        next_steps=rest,
        needs_review=True,
        auto_start=False,
        checkpointer=checkpointer,
    )


def _derive_extra_fields(values: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
    os_type = values.get("os_type") or ("linux" if values.get("is_linux", True) else "windows")
    total_steps = len(get_bootstrap_plan(normalize_os_type(os_type)).steps)
    step_idx = int(values.get("step_index") or 0)
    return {"os_type": str(os_type), "total_steps": total_steps, "step_index": step_idx}


def _detail_table(job: Any) -> Optional[tuple[str, tuple[str, ...], list[tuple[str, ...]]]]:
    if not job.step_results:
        return None
    rows: list[tuple[str, ...]] = []
    for sr in job.step_results:
        st = sr.get("status")
        icon = "✅ 成功" if st == "success" else ("⏭️ 略過" if st == "skipped" else "❌ 失敗")
        tail = " ".join((sr.get("detail") or "").splitlines()[-1:])[:90]
        rows.append((str(sr.get("step") or ""), icon, str(sr.get("exit_code", "")), tail))
    return "🛠️ 初始設定步驟進度", ("步驟", "狀態", "Exit", "摘要"), rows


def _prepare_retry_before_resume(
    job_id: str,
    graph: Any,
    cfg: dict[str, Any],
    run_review_loop: Callable[[], dict[str, Any]],
) -> bool:
    """若程式曾重啟導致記憶體密碼遺失且尚未改密碼：重產密碼並重跑審核，保留已完成 step_index"""
    snap = graph.get_state(cfg)
    vals = dict(snap.values or {})
    has_secrets = get_secret_store().has(vals.get("secret_ref"))
    if not has_secrets and not vals.get("credentials_saved"):
        prep = boot_nodes.prepare_bootstrap(vals)
        graph.update_state(cfg, prep, as_node="prepare_bootstrap")
        graph.invoke(None, config=cfg)
        run_review_loop()
        return bool(graph.get_state(cfg).next)
    return True


SPEC = register_job_spec(JobSpec(
    kind=KIND_BOOTSTRAP,
    label="初始設定",
    build_graph=lambda cp: build_vm_bootstrap_graph(checkpointer=cp, interrupt_before_exec=True),
    derive_stage=_derive_stage,
    evaluate_completion=_evaluate_completion,
    enqueue_chained=_enqueue_chained,
    review_nodes=("prepare_bootstrap", "human_review"),
    active_stage=RUNNING,
    auto_resume_on_startup=False,
    derive_extra_fields=_derive_extra_fields,
    detail_table=_detail_table,
    prepare_retry_before_resume=_prepare_retry_before_resume,
))
