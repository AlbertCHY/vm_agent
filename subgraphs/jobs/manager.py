"""統一背景工作管理器 (JobManager)

透過 subgraphs.jobs.registry (JobSpec) 統一調度任意已註冊的背景子圖 (預設包含 vm_build / vm_disk / vm_bootstrap)，
支援宣告式 next_steps 串接、背景累計輸出查詢 (get_job_logs)、斷點續跑與 DB stage 快取。
"""
import time
import uuid
from functools import lru_cache
from typing import Any, Optional

from langchain_core.messages import AIMessage

import config
from core.persistence import delete_thread_checkpoints, get_app_store, get_checkpointer
from subgraphs.common.runner import (
    append_job_log,
    cleanup_task_resources,
    defer_task_finalize,
    get_job_lock,
    get_job_logs,
    get_live_progress,
    is_background_running,
    is_cancelling,
    mark_cancelled,
    notify_poll_now,
    push_notice,
    running_background_task_ids,
    spawn_background_graph,
    update_live_progress,
    wait_for_background,
)
from subgraphs.jobs.models import (
    ACTIVE_STAGES,
    DONE,
    FAILED,
    KIND_BOOTSTRAP,
    KIND_BUILD,
    KIND_DISK,
    KIND_LABELS,
    RETRY,
    REVIEW,
    REVIEW_STAGES,
    RUNNING,
    WAITING,
    BackgroundJob,
)
from subgraphs.jobs.registry import (
    CompletionOutcome,
    JobSpec,
    all_job_specs,
    get_job_spec,
    kind_label,
    register_job_spec,
)
from subgraphs.vm_build.state import SUBMITTED

THREAD_PREFIX = "job-"


def thread_id_for(job_id: str) -> str:
    return job_id if job_id.startswith(THREAD_PREFIX) else f"{THREAD_PREFIX}{job_id}"


def job_id_from(thread_id: str) -> str:
    return thread_id.removeprefix(THREAD_PREFIX)


def job_config(job_id_or_thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id_for(job_id_or_thread_id)}, "recursion_limit": 60}


def _build_graph_for(kind: str, checkpointer):
    return get_job_spec(kind).build_graph(checkpointer)


@lru_cache(maxsize=8)
def _default_bg_graph(kind: str):
    return _build_graph_for(kind, get_checkpointer())


def get_job_graph(kind: str, checkpointer=None):
    """取得指定工作類型對應的已編譯子圖 (測試可傳入獨立 checkpointer)"""
    if checkpointer is None:
        return _default_bg_graph(kind)
    return _build_graph_for(kind, checkpointer)


def _last_ai_message(values: dict[str, Any]) -> str:
    msgs = [m for m in (values.get("messages") or []) if isinstance(m, AIMessage)]
    if not msgs:
        return ""
    content = msgs[-1].content
    return content if isinstance(content, str) else str(content)


def _derive_stage_and_label(
    kind: str,
    next_nodes: tuple[str, ...],
    values: dict[str, Any],
    meta: dict[str, Any],
    has_interrupts: bool,
    bg_running: bool,
    cancelling: bool = False,
) -> tuple[str, str]:
    spec = get_job_spec(kind)
    # 尚未啟動子圖 (剛由上一步串接長出，等待使用者進 /jobs 開始審核)
    if meta.get("pending_input") is not None and not next_nodes and not values:
        return REVIEW, f"等你審核：{spec.label}"

    if cancelling and next_nodes:
        return RUNNING, "停止中..."

    return spec.derive_stage(next_nodes, values, meta, has_interrupts, bg_running)


def _record_to_job(rec: dict[str, Any], checkpointer=None) -> BackgroundJob:
    jid = rec["job_id"]
    tid = rec["thread_id"]
    kind = rec["kind"]
    meta = dict(rec.get("meta") or {})
    spec = get_job_spec(kind)
    g = get_job_graph(kind, checkpointer=checkpointer)
    try:
        snap = g.get_state(job_config(tid))
    except Exception:
        snap = None

    raw_values = dict(snap.values or {}) if snap and snap.values else {}
    pending_in = dict(meta.get("pending_input") or {})
    live = get_live_progress(jid)
    values = {**pending_in, **raw_values, **live}
    next_nodes = tuple(snap.next or ()) if snap else ()
    has_interrupts = bool(getattr(snap, "interrupts", None)) if snap else False
    bg_running = is_background_running(jid)
    cancelling = is_cancelling(jid)

    stage, stage_label = _derive_stage_and_label(
        kind, next_nodes, raw_values, meta,
        has_interrupts=has_interrupts, bg_running=bg_running, cancelling=cancelling,
    )
    if not rec.get("archived") and not cancelling and rec.get("stage") != stage:
        get_app_store().update_job_stage(jid, stage)

    extra = spec.derive_extra_fields(values, meta) if spec.derive_extra_fields else {}
    step_idx = int(extra.get("step_index") if "step_index" in extra else (values.get("step_index") or 0))
    total_steps = int(extra.get("total_steps") or 0)
    os_type = str(extra.get("os_type") or "")

    note = values.get("note") or ""
    if stage == RETRY and not note:
        note = f"已完成 {step_idx} 步，可於 /jobs 直接從斷點續跑"

    exec_res = values.get("execution_result") or {}
    error = values.get("error") or exec_res.get("error") or ""
    title = (
        rec.get("title")
        or values.get("deployment_name")
        or values.get("vm_name")
        or values.get("hostname")
        or values.get("ip")
        or jid
    )
    return BackgroundJob(
        id=jid,
        thread_id=tid,
        kind=kind,
        title=title,
        stage=stage,
        stage_label=stage_label,
        stage_since=float(values.get("stage_since") or rec.get("created_at") or time.time()),
        last_checked=float(values.get("last_checked") or 0.0),
        deployment_id=str(values.get("deployment_id") or ""),
        deployment_name=str(values.get("deployment_name") or ""),
        vm_name=str(values.get("vm_name") or values.get("hostname") or ""),
        machine_id=str(values.get("machine_id") or ""),
        ip=str(values.get("ip") or ""),
        os_type=os_type,
        overdue=bool(values.get("overdue", False)),
        error=str(error),
        note=str(note),
        archived=bool(rec.get("archived")),
        next_steps=list(meta.get("next_steps") or []),
        disk_ops=list(values.get("ops") or []),
        step_results=list(values.get("step_results") or []),
        step_index=step_idx,
        total_steps=total_steps,
        final_message=_last_ai_message(raw_values),
    )


def get_job(job_id: str, checkpointer=None) -> Optional[BackgroundJob]:
    rec = get_app_store().get_job(job_id_from(job_id))
    if not rec:
        return None
    return _record_to_job(rec, checkpointer=checkpointer)


def list_jobs(
    stages: Optional[tuple[str, ...]] = None,
    kinds: Optional[tuple[str, ...]] = None,
    include_archived: bool = False,
    checkpointer=None,
) -> list[BackgroundJob]:
    recs = get_app_store().list_jobs(limit=200, include_archived=include_archived)
    jobs: list[BackgroundJob] = []
    for rec in recs:
        if kinds and rec["kind"] not in kinds:
            continue
        job = _record_to_job(rec, checkpointer=checkpointer)
        if stages and job.stage not in stages:
            continue
        jobs.append(job)
    return jobs


def count_jobs_in_stages(stages: tuple[str, ...], checkpointer=None) -> int:
    """快速統計指定階段的未封存工作數量 (優先使用 DB stage 快取，免重讀所有 Checkpoint)"""
    if not stages:
        return 0
    store = get_app_store()
    if store.has_uncached_job_stages():
        list_jobs(checkpointer=checkpointer)
    return store.count_jobs_by_stages(stages)


# ════════════════════════════════════════════════════════════════
# 啟動與串接邏輯
# ════════════════════════════════════════════════════════════════

def _spawn_chained_step(
    prev_job_id: str,
    next_steps: list[dict[str, Any]],
    carried: dict[str, Any],
    checkpointer=None,
) -> Optional[BackgroundJob]:
    """消耗 next_steps 的第一筆建立下一個待審核工作，並自動封存已完成的前置工作"""
    if not next_steps:
        return None
    first = dict(next_steps[0])
    rest = [dict(s) for s in next_steps[1:]]
    kind = first.pop("kind")

    spec = get_job_spec(kind)
    if spec.enqueue_chained is None:
        return None

    merged = {**carried, **{k: v for k, v in first.items() if v not in (None, "")}}
    nxt = spec.enqueue_chained(merged, rest, checkpointer)
    if nxt is None:
        return None

    # 前置工作已順利移交給下一步，封存舊工作保持 /jobs 列表乾淨
    archive_job(prev_job_id, checkpointer=checkpointer)
    return nxt


def complete_job(job_id: str, checkpointer=None) -> Optional[BackgroundJob]:
    """工作完成交接：透過 JobSpec 評估結果，處理完成通知、串接 next_steps 或失敗斷點準備。
    若有串接產生下一筆待審核工作則回傳該 BackgroundJob，否則回傳 None。
    """
    rec = get_app_store().get_job(job_id)
    if not rec or rec.get("archived"):
        return None
    kind = rec["kind"]
    meta = dict(rec.get("meta") or {})
    next_steps = list(meta.get("next_steps") or [])
    spec = get_job_spec(kind)
    g = get_job_graph(kind, checkpointer=checkpointer)
    snap = g.get_state(job_config(rec["thread_id"]))
    if snap.next:
        return None  # 尚未結束 (例如撞到 interrupt)

    vals = dict(snap.values or {})
    title = rec.get("title") or vals.get("deployment_name") or vals.get("vm_name") or vals.get("ip") or job_id
    outcome = spec.evaluate_completion(job_id, title, vals, meta)

    if outcome.log_message:
        append_job_log(job_id, outcome.log_message)

    if outcome.status in ("cancelled", "aborted"):
        get_job(job_id, checkpointer=checkpointer)
        return None

    if outcome.status == "failed":
        if outcome.on_failure is not None:
            outcome.on_failure(job_id, checkpointer)
        get_job(job_id, checkpointer=checkpointer)
        if outcome.done_notice:
            push_notice(outcome.done_notice)
        return None

    if outcome.status in ("succeeded", "skipped"):
        if next_steps:
            nxt = _spawn_chained_step(job_id, next_steps, outcome.carried, checkpointer=checkpointer)
            if nxt is not None:
                prefix = outcome.chain_notice_prefix or title
                push_notice(f"🔔 {prefix}，等待「{kind_label(nxt.kind)}」審核 — 輸入 /jobs 處理")
                return nxt
        get_app_store().update_job_stage(job_id, DONE)
        if outcome.done_notice:
            push_notice(outcome.done_notice)
        return None

    return None


def spawn_job_background(job_id: str, checkpointer=None) -> None:
    """啟動指定工作的背景執行緒"""
    jid = job_id_from(job_id)
    rec = get_app_store().get_job(jid)
    if not rec:
        return
    spec = get_job_spec(rec["kind"])
    get_app_store().update_job_stage(jid, spec.active_stage)
    g = get_job_graph(rec["kind"], checkpointer=checkpointer)
    spawn_background_graph(
        jid,
        g,
        job_config(rec["thread_id"]),
        name_prefix=f"lg-{rec['kind']}",
        on_complete=lambda tid: complete_job(tid, checkpointer=checkpointer),
    )


def start_build_job(
    deployment_id: str,
    deployment_name: str = "",
    project: str = "",
    template: str = "",
    os_image: str = "",
    is_linux: bool = True,
    boot_disk_gb: Optional[int] = None,
    extra_disk_gb: Optional[int] = None,
    next_steps: Optional[list[dict[str, Any]]] = None,
    auto_start: bool = True,
    checkpointer=None,
) -> BackgroundJob:
    """建立「VM 建機追蹤」背景工作；建機完成後依 next_steps 自動串接硬碟調整與初始設定"""
    jid = uuid.uuid4().hex[:8]
    tid = thread_id_for(jid)
    now = time.time()
    os_type = "linux" if is_linux else "windows"
    if next_steps is None:
        steps: list[dict[str, Any]] = []
        if boot_disk_gb or extra_disk_gb:
            steps.append({
                "kind": KIND_DISK,
                "boot_disk_gb": boot_disk_gb,
                "extra_disk_gb": extra_disk_gb,
                "os_type": os_type,
            })
        steps.append({"kind": KIND_BOOTSTRAP, "os_type": os_type})
        next_steps = steps

    initial_state: dict[str, Any] = {
        "job_id": jid,
        "deployment_id": deployment_id,
        "deployment_name": deployment_name,
        "project": project,
        "template": template,
        "os_image": os_image,
        "is_linux": is_linux,
        "vm_name": "",
        "machine_id": "",
        "ip": "",
        "sub_stage": SUBMITTED,
        "stage_since": now,
        "last_checked": 0.0,
        "overdue": False,
        "note": "",
        "error": "",
    }
    title = deployment_name or deployment_id
    get_app_store().save_job(jid, tid, KIND_BUILD, title, meta={"next_steps": next_steps, "os_type": os_type}, stage=WAITING)
    g = get_job_graph(KIND_BUILD, checkpointer=checkpointer)
    g.update_state(job_config(tid), initial_state, as_node="init_build")
    if auto_start:
        spawn_job_background(jid, checkpointer=checkpointer)
    return get_job(jid, checkpointer=checkpointer)  # type: ignore[return-value]


def enqueue_disk_job(
    approved_state: Optional[dict[str, Any]] = None,
    *,
    deployment_id: Optional[str] = None,
    deployment_name: str = "",
    ip: str = "",
    vm_name: str = "",
    machine_id: str = "",
    boot_disk_gb: Optional[int] = None,
    extra_disk_gb: Optional[int] = None,
    os_type: str = "",
    next_steps: Optional[list[dict[str, Any]]] = None,
    needs_review: bool = False,
    auto_start: bool = True,
    checkpointer=None,
) -> BackgroundJob:
    """建立「硬碟調整」背景工作：
    - needs_review=False (前景已完成審核)：直接寫入 finalize 後狀態並送入背景執行 submit → wait → report。
    - needs_review=True (由上一步串接長出)：停在待審核，使用者進 /jobs 審核通過後再送背景。
    """
    jid = uuid.uuid4().hex[:8]
    tid = thread_id_for(jid)
    now = time.time()
    src = dict(approved_state or {})
    resolved_ip = src.get("ip") or ip
    resolved_vm = src.get("vm_name") or vm_name or deployment_name or resolved_ip
    title = deployment_name or src.get("deployment_name") or resolved_vm or jid
    meta: dict[str, Any] = {"next_steps": list(next_steps or []), "os_type": os_type}

    if needs_review:
        meta["pending_input"] = {
            "job_id": jid,
            "deployment_id": src.get("deployment_id") or deployment_id,
            "deployment_name": title,
            "ip": resolved_ip,
            "vm_name": resolved_vm,
            "machine_id": src.get("machine_id") or machine_id,
            "boot_disk_gb": src.get("boot_disk_gb", boot_disk_gb),
            "extra_disk_gb": src.get("extra_disk_gb", extra_disk_gb),
            "notes": [],
            "errors": [],
            "blocking": [],
            "human_feedback": None,
            "handoff_to_background": False,
            "ops": [],
            "sub_stage": "",
            "stage_since": now,
            "overdue": False,
            "note": "",
            "error": "",
            "execution_result": None,
        }
        get_app_store().save_job(jid, tid, KIND_DISK, title, meta=meta, stage=REVIEW)
        return get_job(jid, checkpointer=checkpointer)  # type: ignore[return-value]

    seeded = {
        **src,
        "job_id": jid,
        "deployment_name": title,
        "handoff_to_background": False,
        "sub_stage": "",
        "stage_since": now,
        "overdue": False,
        "error": "",
        "execution_result": None,
    }
    get_app_store().save_job(jid, tid, KIND_DISK, title, meta=meta, stage=RUNNING)
    g = get_job_graph(KIND_DISK, checkpointer=checkpointer)
    g.update_state(job_config(tid), seeded, as_node="finalize")
    append_job_log(jid, f"已核准硬碟調整計畫 ({len(seeded.get('ops') or [])} 項動作)，進入背景執行")
    if auto_start:
        spawn_job_background(jid, checkpointer=checkpointer)
    return get_job(jid, checkpointer=checkpointer)  # type: ignore[return-value]


def enqueue_bootstrap_job(
    approved_state: Optional[dict[str, Any]] = None,
    *,
    ip: str = "",
    hostname: str = "",
    os_type: str = "",
    cs_mode: str = "",
    skip_disk: bool = False,
    deployment_id: Optional[str] = None,
    deployment_name: str = "",
    next_steps: Optional[list[dict[str, Any]]] = None,
    needs_review: bool = False,
    auto_start: bool = True,
    checkpointer=None,
) -> BackgroundJob:
    """建立「新機基本設定 (Bootstrap)」背景工作：
    - needs_review=False (前景已完成審核)：直接寫入 human_review 後狀態並送入背景執行。
    - needs_review=True (由上一步串接長出)：停在待審核，使用者進 /jobs 審核通過後再送背景。
    """
    jid = uuid.uuid4().hex[:8]
    tid = thread_id_for(jid)
    now = time.time()
    src = dict(approved_state or {})
    resolved_ip = src.get("ip") or ip
    resolved_host = src.get("hostname") or hostname or deployment_name
    title = deployment_name or src.get("deployment_name") or resolved_host or resolved_ip or jid
    meta: dict[str, Any] = {"next_steps": list(next_steps or [])}

    if needs_review:
        meta["pending_input"] = {
            "job_id": jid,
            "deployment_id": src.get("deployment_id") or deployment_id,
            "deployment_name": title,
            "ip": resolved_ip,
            "os_type": src.get("os_type") or os_type,
            "hostname": resolved_host or None,
            "cs_mode": (src.get("cs_mode") or cs_mode or config.CROWDSTRIKE_DEFAULT_MODE).upper(),
            "skip_disk": bool(src.get("skip_disk", skip_disk)),
            "human_feedback": None,
            "handoff_to_background": False,
            "validation_errors": [],
            "execution_result": None,
            "step_results": [],
            "step_index": 0,
            "stopped": False,
            "sub_stage": "",
            "stage_since": now,
            "note": "",
            "error": "",
        }
        get_app_store().save_job(jid, tid, KIND_BOOTSTRAP, title, meta=meta, stage=REVIEW)
        return get_job(jid, checkpointer=checkpointer)  # type: ignore[return-value]

    seeded = {
        **src,
        "job_id": jid,
        "deployment_name": title,
        "handoff_to_background": False,
        "sub_stage": "",
        "stage_since": now,
        "step_results": list(src.get("step_results") or []),
        "step_index": int(src.get("step_index") or 0),
        "stopped": False,
        "error": "",
        "execution_result": None,
    }
    get_app_store().save_job(jid, tid, KIND_BOOTSTRAP, title, meta=meta, stage=RUNNING)
    g = get_job_graph(KIND_BOOTSTRAP, checkpointer=checkpointer)
    g.update_state(job_config(tid), seeded, as_node="human_review")
    append_job_log(jid, f"已核准 {seeded.get('os_type', 'linux')} 初始設定 ({resolved_ip})，進入背景執行")
    if auto_start:
        spawn_job_background(jid, checkpointer=checkpointer)
    return get_job(jid, checkpointer=checkpointer)  # type: ignore[return-value]


def clear_pending_input(job_id: str) -> Optional[dict[str, Any]]:
    """取出並清除 pending_input (供 /jobs 首次啟動待審核子圖時呼叫)"""
    jid = job_id_from(job_id)
    rec = get_app_store().get_job(jid)
    if not rec:
        return None
    meta = dict(rec.get("meta") or {})
    pending = meta.pop("pending_input", None)
    if pending is not None:
        get_app_store().update_job_meta(jid, meta)
    return pending


def prepare_bootstrap_retry(job_id: str, error: str = "", checkpointer=None) -> None:
    """Bootstrap 失敗後保留已完成步驟，將工作切換回可從失敗步驟續跑的狀態"""
    jid = job_id_from(job_id)
    rec = get_app_store().get_job(jid)
    if not rec or rec["kind"] != KIND_BOOTSTRAP:
        return
    g = get_job_graph(KIND_BOOTSTRAP, checkpointer=checkpointer)
    cfg = job_config(rec["thread_id"])
    snap = g.get_state(cfg)
    vals = dict(snap.values or {})
    steps = list(vals.get("step_results") or [])

    failed_idx = None
    for i, sr in enumerate(steps):
        if sr.get("status") == "failed" and not sr.get("id"):
            failed_idx = i
            break

    if failed_idx is not None:
        kept_steps = steps[:failed_idx]
        target_step_idx = steps[failed_idx].get("step_idx")
        if target_step_idx is None:
            target_step_idx = sum(1 for sr in kept_steps if not str(sr.get("step", "")).startswith("0"))
        g.update_state(
            cfg,
            {
                "error": "",
                "note": error or f"步驟 {target_step_idx + 1} 失敗，可再進 /jobs 從該步續跑",
                "execution_result": None,
                "step_results": kept_steps,
                "step_index": target_step_idx,
                "stopped": False,
                "sub_stage": "",
            },
            as_node="connect_and_upload",
        )
        get_app_store().update_job_stage(jid, RETRY)
        return

    # 連線或上傳階段失敗：回到 prepare_bootstrap 重新審核
    seeded = {
        **vals,
        "error": "",
        "note": error,
        "execution_result": None,
        "step_results": [],
        "step_index": 0,
        "stopped": False,
        "sub_stage": "",
    }
    from subgraphs.vm_bootstrap import nodes as boot_nodes

    prep = boot_nodes.prepare_bootstrap(seeded)
    g.update_state(cfg, {**prep, "error": "", "note": error}, as_node="prepare_bootstrap")
    g.invoke(None, config=cfg)
    get_app_store().update_job_stage(jid, REVIEW)


def archive_job(job_id: str, checkpointer=None) -> None:
    jid = job_id_from(job_id)
    rec = get_app_store().get_job(jid)
    tid = rec["thread_id"] if rec else thread_id_for(jid)
    cleanup_task_resources(jid)
    get_app_store().archive_job(jid)
    delete_thread_checkpoints(tid, checkpointer=checkpointer)


def cancel_job(job_id: str, checkpointer=None, wait_timeout: float = 2.0) -> bool:
    """停止背景工作並移除；若背景執行緒正在執行耗時遠端步驟尚未退出，
    則保留取消旗標並登記待執行緒結束後才自動封存與清理資源 (回傳 False)。
    """
    jid = job_id_from(job_id)
    rec = get_app_store().get_job(jid)
    title = (rec or {}).get("title") or jid
    mark_cancelled(jid)
    if wait_for_background(jid, timeout=wait_timeout):
        archive_job(jid, checkpointer=checkpointer)
        return True

    update_live_progress(jid, {"note": "已送出停止訊號，待目前遠端操作結束後自動移除"})

    def _finalize() -> None:
        archive_job(jid, checkpointer=checkpointer)
        push_notice(f"🛑 {title} 已停止並移除")

    return defer_task_finalize(jid, _finalize)


def resume_active_jobs(checkpointer=None) -> None:
    """CLI 啟動時恢復未完成的背景工作：
    - 依各工作類型的 JobSpec.auto_resume_on_startup 自動恢復背景執行／輪詢
    - 待審核 / 中斷待續工作推播一行通知提醒進 /jobs
    """
    jobs = list_jobs(checkpointer=checkpointer)
    waiting_reviews = [j for j in jobs if j.stage in REVIEW_STAGES]
    if waiting_reviews:
        push_notice(f"🔔 有 {len(waiting_reviews)} 項工作等待你的審核或續跑 — 輸入 /jobs 處理")
    for j in jobs:
        if j.stage in ACTIVE_STAGES and get_job_spec(j.kind).auto_resume_on_startup:
            spawn_job_background(j.id, checkpointer=checkpointer)


__all__ = [
    "ACTIVE_STAGES",
    "DONE",
    "FAILED",
    "KIND_BOOTSTRAP",
    "KIND_BUILD",
    "KIND_DISK",
    "KIND_LABELS",
    "RETRY",
    "REVIEW",
    "REVIEW_STAGES",
    "RUNNING",
    "WAITING",
    "BackgroundJob",
    "CompletionOutcome",
    "JobSpec",
    "all_job_specs",
    "append_job_log",
    "archive_job",
    "cancel_job",
    "clear_pending_input",
    "complete_job",
    "count_jobs_in_stages",
    "enqueue_bootstrap_job",
    "enqueue_disk_job",
    "get_job",
    "get_job_graph",
    "get_job_lock",
    "get_job_logs",
    "get_job_spec",
    "is_background_running",
    "is_cancelling",
    "job_config",
    "job_id_from",
    "kind_label",
    "list_jobs",
    "notify_poll_now",
    "prepare_bootstrap_retry",
    "push_notice",
    "register_job_spec",
    "resume_active_jobs",
    "running_background_task_ids",
    "spawn_job_background",
    "start_build_job",
    "thread_id_for",
    "wait_for_background",
]
