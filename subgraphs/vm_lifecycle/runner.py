"""VM 生命週期子圖執行管理 (純 LangGraph Checkpointer 驅動)

負責：
- 建立並初始化 VM 生命週期子圖的 Checkpoint (start_lifecycle_job)
- 啟動背景執行緒推進等待節點 (spawn_background_continuation)，並提供 Per-Job 執行鎖避免前景與背景競態
- 從 LangGraph Checkpointer 直接推導目前各 VM 的階段與狀態 (get_lifecycle_job / list_lifecycle_jobs)
- CLI 啟動時自動恢復上次未完成的背景等待節點 (resume_waiting_lifecycles)
"""
import logging
import threading
import time
import uuid
from functools import lru_cache
from typing import Any, Optional

from core.persistence import delete_thread_checkpoints, get_app_store
from subgraphs.vm_lifecycle.graph import build_vm_lifecycle_graph
from subgraphs.vm_lifecycle.nodes import (
    clear_cancelled,
    clear_live_progress,
    drain_notices,
    get_live_progress,
    mark_cancelled,
    notify_poll_now,
    push_notice,
    set_live_progress,
)
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

logger = logging.getLogger(__name__)

LIFECYCLE_KIND = "vm_lifecycle"
THREAD_PREFIX = "vmjob-"

_BG_LOCK = threading.Lock()
_ACTIVE_THREADS: dict[str, threading.Thread] = {}
_JOB_LOCKS: dict[str, threading.RLock] = {}


@lru_cache(maxsize=1)
def get_vm_lifecycle_graph():
    return build_vm_lifecycle_graph()


def thread_id_for(job_id: str) -> str:
    return job_id if job_id.startswith(THREAD_PREFIX) else f"{THREAD_PREFIX}{job_id}"


def job_id_from(thread_id: str) -> str:
    return thread_id.removeprefix(THREAD_PREFIX)


def lifecycle_config(job_id_or_thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id_for(job_id_or_thread_id)}, "recursion_limit": 60}


def get_job_lock(job_id: str) -> threading.RLock:
    jid = job_id_from(job_id)
    with _BG_LOCK:
        lock = _JOB_LOCKS.get(jid)
        if lock is None:
            lock = threading.RLock()
            _JOB_LOCKS[jid] = lock
        return lock


def is_background_running(job_id: str) -> bool:
    jid = job_id_from(job_id)
    with _BG_LOCK:
        t = _ACTIVE_THREADS.get(jid)
        return bool(t and t.is_alive())


def wait_for_background(job_id: str, timeout: float = 5.0) -> bool:
    """等待該 job 的背景執行緒收尾結束；回傳 True 代表背景已完全停止"""
    jid = job_id_from(job_id)
    with _BG_LOCK:
        t = _ACTIVE_THREADS.get(jid)
    if t and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=timeout)
    return not is_background_running(jid)


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
        # 若背景執行緒剛結束 wait_vm_ready 正在跑 disk_locate，尚未停在 disk_review 的 interrupt，仍視為建立收尾中
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
    g = graph or get_vm_lifecycle_graph()
    state = g.get_state(lifecycle_config(tid))
    return _snapshot_to_job(jid, tid, state, archived=bool((sess or {}).get("archived")))


def list_lifecycle_jobs(
    stages: Optional[tuple[str, ...]] = None,
    include_archived: bool = False,
    graph=None,
) -> list[LifecycleJob]:
    g = graph or get_vm_lifecycle_graph()
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
    """在背景執行緒呼叫 graph.invoke(None, cfg)，推進 wait_* 節點直到遇到下一個 interrupt() 或 END 自動結束"""
    jid = job_id_from(job_id)
    tid = thread_id_for(jid)
    g = graph or get_vm_lifecycle_graph()
    job_lock = get_job_lock(jid)

    with _BG_LOCK:
        existing = _ACTIVE_THREADS.get(jid)
        if existing and existing.is_alive():
            return

        def _runner():
            try:
                with job_lock:
                    g.invoke(None, config=lifecycle_config(tid))
            except Exception as e:
                logger.exception("VM 生命週期子圖背景推進失敗 (job=%s)", jid)
                set_live_progress(jid, {"note": f"背景推進暫停 ({type(e).__name__}: {e}) — 可於 /jobs 按 [C] 重試"})
            finally:
                clear_cancelled(jid)
                with _BG_LOCK:
                    _ACTIVE_THREADS.pop(jid, None)

        t = threading.Thread(target=_runner, name=f"lg-lifecycle-{jid}", daemon=True)
        _ACTIVE_THREADS[jid] = t
        t.start()


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
    """
    建立新的 VM 生命週期子圖執行個體：
    1. 以 update_state(..., as_node='init_lifecycle') 同步寫入初始 Checkpoint (state.next == ('wait_vm_ready',))
    2. 若 auto_start=True，立即在背景執行緒啟動 graph.invoke(None, cfg) 執行 wait_vm_ready
    """
    jid = uuid.uuid4().hex[:8]
    tid = thread_id_for(jid)
    now = time.time()
    g = graph or get_vm_lifecycle_graph()
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
    """
    當初始設定執行失敗 (例如 SSH 尚未就緒，或某一步驟失敗) 時：
    - 保留既有的 secret_ref、login_mode、credentials_saved 與已成功的步驟進度
    - 若尚未進入步驟 (connect_and_upload 失敗)，退回 bootstrap_review 讓使用者可稍後重試
    - 若在 run_step 某一步驟失敗，將 step_index 退回該失敗步驟並停在 run_step，下次進 /jobs 可直接從該步驟續跑
    """
    jid = job_id_from(job_id)
    tid = thread_id_for(jid)
    g = graph or get_vm_lifecycle_graph()
    cfg = lifecycle_config(tid)
    state = g.get_state(cfg)
    vals = dict(state.values or {})
    steps = list(vals.get("step_results") or [])

    # 檢查是否有失敗的一般步驟 (不含 cleanup)
    failed_idx = None
    for i, sr in enumerate(steps):
        if sr.get("status") == "failed" and not sr.get("id"):
            failed_idx = i
            break

    if failed_idx is not None:
        # 保留前面已成功的步驟與 secret_ref，將狀態停在 run_step 供下次直接續跑
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

    # 連線或上傳階段就失敗：保留既有 secret_ref 退回 prepare_bootstrap → bootstrap_review
    from subgraphs.vm_lifecycle.nodes import lifecycle_prepare_bootstrap
    seeded = {**vals, "error": "", "note": error, "execution_result": None, "step_results": [], "step_index": 0}
    prep = lifecycle_prepare_bootstrap(seeded)
    g.update_state(cfg, {**prep, "error": "", "note": error}, as_node="prepare_bootstrap")
    g.invoke(None, config=cfg)


def archive_lifecycle_job(job_id: str, graph=None) -> None:
    jid = job_id_from(job_id)
    tid = thread_id_for(jid)
    clear_live_progress(jid)
    clear_cancelled(jid)
    get_app_store().archive_session(tid)
    cp = getattr(graph, "checkpointer", None) if graph else None
    delete_thread_checkpoints(tid, checkpointer=cp)


def cancel_lifecycle_job(job_id: str, graph=None) -> None:
    jid = job_id_from(job_id)
    tid = thread_id_for(jid)
    mark_cancelled(jid)
    wait_for_background(jid, timeout=2.0)
    clear_live_progress(jid)
    clear_cancelled(jid)
    get_app_store().archive_session(tid)
    cp = getattr(graph, "checkpointer", None) if graph else None
    delete_thread_checkpoints(tid, checkpointer=cp)


def resume_waiting_lifecycles(graph=None) -> None:
    """CLI 啟動時：自動為停在 wait_* / submit_disk_op 節點的生命週期子圖重新啟動背景執行緒，並提示待審核工作數量"""
    g = graph or get_vm_lifecycle_graph()
    jobs = list_lifecycle_jobs(graph=g)
    waiting_reviews = [j for j in jobs if j.stage in REVIEW_STAGES]
    if waiting_reviews:
        push_notice(f"🔔 有 {len(waiting_reviews)} 台 VM 等待你的審核或續跑 — 輸入 /jobs 處理")
    for j in jobs:
        if j.stage in WAITING_STAGES:
            spawn_background_continuation(j.id, graph=g)
