"""VM 生命週期管理服務：Checkpoint 階段推導、Job 查詢/建立/重試/封存/取消與啟動恢復

負責所有 VMLifecycleGraph 的外部管理操作；圖形拓樸組裝見 subgraphs/vm_lifecycle/graph.py，
底層背景執行緒、鎖與通知佇列統一使用 subgraphs/common/runner.py。
"""
import time
import uuid
from typing import Any, Optional

from core.persistence import delete_thread_checkpoints, get_app_store
from subgraphs.common.runner import (
    cleanup_task_resources,
    get_live_progress,
    is_background_running,
    mark_cancelled,
    push_notice,
    spawn_background_graph,
    wait_for_background,
)
from subgraphs.vm_lifecycle import graph as lifecycle_graph
from subgraphs.vm_lifecycle.nodes import lifecycle_prepare_bootstrap
from subgraphs.vm_lifecycle.state import (
    BOOTSTRAP_REVIEW,
    BOOTSTRAP_RUNNING,
    BUILDING,
    DISK_REVIEW,
    DISK_RUNNING,
    DONE,
    FAILED,
    REVIEW_STAGES,
    SUBMITTED,
    WAITING_STAGES,
    LifecycleJob,
)

LIFECYCLE_KIND = "vm_lifecycle"
THREAD_PREFIX = "vmjob-"


def thread_id_for(job_id: str) -> str:
    return job_id if job_id.startswith(THREAD_PREFIX) else f"{THREAD_PREFIX}{job_id}"


def job_id_from(thread_id: str) -> str:
    return thread_id.removeprefix(THREAD_PREFIX)


def lifecycle_config(job_id_or_thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id_for(job_id_or_thread_id)}, "recursion_limit": 60}


def _derive_stage(
    next_nodes: tuple[str, ...],
    values: dict[str, Any],
    has_interrupts: bool = False,
    bg_running: bool = False,
) -> str:
    if not next_nodes:
        if values.get("error"):
            return FAILED
        return DONE
    if "wait_vm_ready" in next_nodes:
        return BUILDING if values.get("sub_stage") == BUILDING else SUBMITTED
    if "disk_locate" in next_nodes:
        return BUILDING if bg_running else DISK_REVIEW
    if "disk_review" in next_nodes:
        return BUILDING if (bg_running and not has_interrupts) else DISK_REVIEW
    if any(n in next_nodes for n in ("disk_finalize", "submit_disk_op", "wait_disk_op")):
        return DISK_RUNNING
    if "prepare_bootstrap" in next_nodes:
        if bg_running:
            return DISK_RUNNING if values.get("ops") else BUILDING
        return BOOTSTRAP_REVIEW
    if "bootstrap_review" in next_nodes:
        if bg_running and not has_interrupts:
            return DISK_RUNNING if values.get("ops") else BUILDING
        return BOOTSTRAP_REVIEW
    if any(n in next_nodes for n in ("connect_and_upload", "run_step", "cleanup", "bootstrap_finalize")):
        return BOOTSTRAP_RUNNING
    return BOOTSTRAP_REVIEW


def _snapshot_to_job(job_id: str, thread_id: str, state: Any, archived: bool = False) -> Optional[LifecycleJob]:
    raw_values = dict(state.values or {}) if state else {}
    if not raw_values.get("deployment_id"):
        return None
    live = get_live_progress(job_id)
    values = {**raw_values, **live}
    next_nodes = tuple(state.next or ()) if state else ()
    has_interrupts = bool(getattr(state, "interrupts", None))
    bg_running = is_background_running(job_id)
    stage = _derive_stage(next_nodes, values, has_interrupts=has_interrupts, bg_running=bg_running)
    note = values.get("note", "")
    if stage == BOOTSTRAP_RUNNING and not note:
        step_idx = values.get("step_index") or 0
        note = f"已完成 {step_idx} 步，可於 /jobs 直接接續執行"
    return LifecycleJob(
        id=job_id,
        thread_id=thread_id,
        deployment_id=values.get("deployment_id", ""),
        deployment_name=values.get("deployment_name", ""),
        project=values.get("project", ""),
        template=values.get("template", ""),
        os_image=values.get("os_image", ""),
        is_linux=bool(values.get("is_linux", True)),
        boot_disk_gb=values.get("boot_disk_gb"),
        extra_disk_gb=values.get("extra_disk_gb"),
        stage=stage,
        stage_since=float(values.get("stage_since") or time.time()),
        vm_name=values.get("vm_name", ""),
        machine_id=values.get("machine_id", ""),
        ip=values.get("ip", ""),
        disk_ops=list(values.get("ops") or []),
        overdue=bool(values.get("overdue", False)),
        error=values.get("error", ""),
        note=note,
        archived=archived,
        last_checked=float(values.get("last_checked") or 0.0),
    )


def get_lifecycle_job(job_id: str, graph=None) -> Optional[LifecycleJob]:
    jid = job_id_from(job_id)
    tid = thread_id_for(jid)
    sess = get_app_store().get_session(tid)
    g = graph or lifecycle_graph.get_vm_lifecycle_graph()
    state = g.get_state(lifecycle_config(tid))
    return _snapshot_to_job(jid, tid, state, archived=bool((sess or {}).get("archived")))


def list_lifecycle_jobs(
    stages: Optional[tuple[str, ...]] = None,
    include_archived: bool = False,
    graph=None,
) -> list[LifecycleJob]:
    g = graph or lifecycle_graph.get_vm_lifecycle_graph()
    sessions = get_app_store().list_sessions(limit=200, kind=LIFECYCLE_KIND, include_archived=include_archived)
    jobs: list[LifecycleJob] = []
    for s in sessions:
        tid = s["thread_id"]
        jid = job_id_from(tid)
        try:
            state = g.get_state(lifecycle_config(tid))
        except Exception:
            continue
        job = _snapshot_to_job(jid, tid, state, archived=bool(s.get("archived")))
        if not job:
            continue
        if stages and job.stage not in stages:
            continue
        jobs.append(job)
    return jobs


def spawn_background_continuation(job_id: str, graph=None) -> None:
    """委派給 subgraphs.common.runner.spawn_background_graph 在背景執行緒推進子圖"""
    jid = job_id_from(job_id)
    tid = thread_id_for(jid)
    g = graph or lifecycle_graph.get_vm_lifecycle_graph()
    spawn_background_graph(jid, g, lifecycle_config(tid), name_prefix="lg-lifecycle")


def start_lifecycle_job(
    deployment_id: str,
    deployment_name: str = "",
    project: str = "",
    template: str = "",
    os_image: str = "",
    is_linux: bool = True,
    boot_disk_gb: Optional[int] = None,
    extra_disk_gb: Optional[int] = None,
    auto_start: bool = True,
    graph=None,
) -> LifecycleJob:
    jid = uuid.uuid4().hex[:8]
    tid = thread_id_for(jid)
    now = time.time()
    g = graph or lifecycle_graph.get_vm_lifecycle_graph()
    cfg = lifecycle_config(tid)

    initial_state: dict[str, Any] = {
        "job_id": jid,
        "deployment_id": deployment_id,
        "deployment_name": deployment_name,
        "project": project,
        "template": template,
        "os_image": os_image,
        "is_linux": is_linux,
        "boot_disk_gb": boot_disk_gb,
        "extra_disk_gb": extra_disk_gb,
        "sub_stage": SUBMITTED,
        "stage_since": now,
        "last_checked": 0.0,
        "overdue": False,
        "error": "",
        "note": "",
        "vm_name": "",
        "machine_id": "",
        "ip": "",
        "ops": [],
        "notes": [],
        "errors": [],
        "blocking": [],
    }
    get_app_store().touch_session(tid, deployment_name or deployment_id, kind=LIFECYCLE_KIND)
    g.update_state(cfg, initial_state, as_node="init_lifecycle")
    if auto_start:
        spawn_background_continuation(jid, graph=g)
    return get_lifecycle_job(jid, graph=g)


def prepare_bootstrap_retry(job_id: str, error: str = "", graph=None) -> None:
    jid = job_id_from(job_id)
    tid = thread_id_for(jid)
    g = graph or lifecycle_graph.get_vm_lifecycle_graph()
    cfg = lifecycle_config(tid)
    state = g.get_state(cfg)
    vals = dict(state.values or {})
    steps = list(vals.get("step_results") or [])

    failed_idx = None
    for i, sr in enumerate(steps):
        if sr.get("status") == "failed" and not sr.get("id"):
            failed_idx = i
            break

    if failed_idx is not None:
        kept_steps = steps[:failed_idx]
        g.update_state(
            cfg,
            {
                "error": "",
                "note": error or f"步驟 {failed_idx + 1} 失敗，可再進 /jobs 從該步續跑",
                "execution_result": None,
                "step_results": kept_steps,
                "step_index": failed_idx,
                "stopped": False,
            },
            as_node="connect_and_upload",
        )
        return

    seeded = {**vals, "error": "", "note": error, "execution_result": None, "step_results": [], "step_index": 0}
    prep = lifecycle_prepare_bootstrap(seeded)
    g.update_state(cfg, {**prep, "error": "", "note": error}, as_node="prepare_bootstrap")
    g.invoke(None, config=cfg)


def archive_lifecycle_job(job_id: str, graph=None) -> None:
    jid = job_id_from(job_id)
    tid = thread_id_for(jid)
    cleanup_task_resources(jid)
    get_app_store().archive_session(tid)
    cp = getattr(graph, "checkpointer", None) if graph else None
    delete_thread_checkpoints(tid, checkpointer=cp)


def cancel_lifecycle_job(job_id: str, graph=None) -> None:
    jid = job_id_from(job_id)
    mark_cancelled(jid)
    wait_for_background(jid, timeout=2.0)
    archive_lifecycle_job(jid, graph=graph)


def resume_waiting_lifecycles(graph=None) -> None:
    g = graph or lifecycle_graph.get_vm_lifecycle_graph()
    jobs = list_lifecycle_jobs(graph=g)
    waiting_reviews = [j for j in jobs if j.stage in REVIEW_STAGES]
    if waiting_reviews:
        push_notice(f"🔔 有 {len(waiting_reviews)} 台 VM 等待你的審核或續跑 — 輸入 /jobs 處理")
    for j in jobs:
        if j.stage in WAITING_STAGES:
            spawn_background_continuation(j.id, graph=g)
