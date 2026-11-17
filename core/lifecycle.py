"""VM 追蹤工作的狀態推進 (由背景監看器呼叫，不含任何人工互動)

submitted / building：查詢 deployment 與最後一次 request 的狀態 → 建立完成時取得 VM 名稱、machine resource、IP
disk_running：依序送出 / 查詢已核准的 Day-2 硬碟動作
完成後停在下一個審核階段 (disk_review / bootstrap_review)，等使用者在 /jobs 處理。
"""
import logging
import time
from typing import Any, Optional

import requests

import config
from core.jobs import (
    BOOTSTRAP_REVIEW, BUILDING, DISK_REVIEW, DISK_RUNNING, DONE, FAILED, SUBMITTED, Job, JobStore,
)
from subgraphs.vm_disk.executor import locate_machine, step

logger = logging.getLogger(__name__)

APPROVAL_PENDING = ("APPROVAL_PENDING",)
FAILED_REQUEST = ("FAILED", "ABORTED", "APPROVAL_REJECTED", "REJECTED", "CANCELLED")


def stage_after_build(job: Job) -> str:
    if job.needs_disk:
        return DISK_REVIEW
    return BOOTSTRAP_REVIEW if job.is_linux else DONE


def stage_after_disk(job: Job) -> str:
    return BOOTSTRAP_REVIEW if job.is_linux else DONE


def _windows_note(job: Job, stage: str) -> str:
    return "Windows 尚未支援自動初始設定，請手動完成" if stage == DONE and not job.is_linux else ""


def _overdue(job: Job, now: float) -> Optional[str]:
    """回傳逾時說明 (尚未標示過時才回傳)"""
    if job.overdue:
        return None
    hours = (now - job.stage_since) / 3600
    if job.stage == SUBMITTED and hours > config.JOB_APPROVAL_TIMEOUT_HOURS:
        return f"等待 Aria 審核已超過 {config.JOB_APPROVAL_TIMEOUT_HOURS:g} 小時"
    if job.stage == BUILDING and hours > config.JOB_BUILD_TIMEOUT_HOURS:
        return f"Aria 建立已超過 {config.JOB_BUILD_TIMEOUT_HOURS:g} 小時"
    if job.stage == DISK_RUNNING:
        started = [op.get("submitted_at") for op in job.disk_ops if op.get("status") == "running" and op.get("submitted_at")]
        if started and (now - min(started)) / 60 > config.JOB_DAY2_TIMEOUT_MINUTES:
            return f"硬碟調整動作已超過 {config.JOB_DAY2_TIMEOUT_MINUTES:g} 分鐘未完成"
    return None


def _poll_deployment(job: Job, aria: Any, store: JobStore) -> tuple[Optional[Job], Optional[str]]:
    try:
        dep = aria.deployments.get(job.deployment_id, expand="lastRequest")
    except requests.HTTPError as e:
        if getattr(e.response, "status_code", None) == 404:
            j = store.update(job, job.stage, stage=FAILED, error="Deployment 已不存在 (可能被拒絕或刪除)")
            return j, f"❌ {job.deployment_name} 的 Deployment 已不存在"
        raise
    req = dep.get("lastRequest") or {}
    r_status = str(req.get("status") or "").upper()
    d_status = str(dep.get("status") or "").upper()

    if r_status in FAILED_REQUEST or d_status.endswith("FAILED"):
        detail = req.get("details") or r_status or d_status
        j = store.update(job, job.stage, stage=FAILED, error=f"Aria 申請未成功：{detail}")
        return j, f"❌ {job.deployment_name} 建立失敗：{detail}"

    if r_status in APPROVAL_PENDING:
        return store.update(job, job.stage, last_checked=time.time()), None

    if r_status == "SUCCESSFUL" or d_status in ("CREATE_SUCCESSFUL", "SUCCESSFUL"):
        try:
            info = locate_machine(aria, job.deployment_id)
        except ValueError:
            info = {}
        if not info.get("ip"):
            j = store.update(job, job.stage, stage=BUILDING, last_checked=time.time(),
                             note="建立完成，等待取得 IP")
            return j, None
        nxt = stage_after_build(job)
        j = store.update(job, job.stage, stage=nxt, vm_name=info["vm_name"], machine_id=info["machine_id"],
                         ip=info["ip"], note=_windows_note(job, nxt), last_checked=time.time())
        tail = {DISK_REVIEW: "等待「② 硬碟調整」審核", BOOTSTRAP_REVIEW: "等待「③ 初始設定」審核",
                DONE: "流程完成 (Windows 需手動初始設定)"}[nxt]
        return j, f"🔔 {job.deployment_name} 已建立完成 ({info['ip']})，{tail} — 輸入 /jobs 處理"

    # 其餘狀態 (PENDING / INPROGRESS / CREATE_INPROGRESS…) 視為建立中
    if job.stage == SUBMITTED:
        return store.update(job, SUBMITTED, stage=BUILDING, last_checked=time.time()), None
    return store.update(job, job.stage, last_checked=time.time()), None


def _poll_disk(job: Job, aria: Any, store: JobStore) -> tuple[Optional[Job], Optional[str]]:
    ops, state, msg = step(aria, job.machine_id, job.disk_ops)
    if state == "running":
        return store.update(job, DISK_RUNNING, disk_ops=ops, note=msg, last_checked=time.time()), None
    if state == "failed":
        j = store.update(job, DISK_RUNNING, disk_ops=ops, stage=FAILED, error=msg)
        return j, f"❌ {job.deployment_name} {msg}"
    nxt = stage_after_disk(job)
    j = store.update(job, DISK_RUNNING, disk_ops=ops, stage=nxt, note=_windows_note(job, nxt) or msg,
                     last_checked=time.time())
    tail = "等待「③ 初始設定」審核 — 輸入 /jobs 處理" if nxt == BOOTSTRAP_REVIEW else "流程完成"
    return j, f"🔔 {job.deployment_name} 硬碟調整完成，{tail}"


def poll_job(job: Job, aria: Any, store: JobStore, now: Optional[float] = None) -> list[str]:
    """推進一個等待中的 job，回傳要顯示給使用者的通知"""
    now = now or time.time()
    notices: list[str] = []
    if job.stage in (SUBMITTED, BUILDING):
        updated, notice = _poll_deployment(job, aria, store)
    elif job.stage == DISK_RUNNING:
        updated, notice = _poll_disk(job, aria, store)
    else:
        return notices
    if notice:
        notices.append(notice)
    if updated and updated.stage in (SUBMITTED, BUILDING, DISK_RUNNING):
        reason = _overdue(updated, now)
        if reason:
            store.update(updated, updated.stage, overdue=True)
            notices.append(f"⏰ {updated.deployment_name} {reason}，請確認 Aria 狀態")
    return notices
