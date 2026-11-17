"""背景監看器：CLI 開啟期間定時推進 VM 追蹤工作，並把通知放進佇列 (下一次輸入提示前顯示)

只做查詢與推進，不跳出任何輸入提示；需要人工審核的階段停下來等 /jobs。
"""
import logging
import threading
from collections import deque
from typing import Any, Callable, Optional

import config
from core.jobs import DISK_RUNNING, JOBS_CHANGED, REVIEW_STAGES, WAITING_STAGES, JobStore, get_job_store
from core.lifecycle import poll_job

logger = logging.getLogger(__name__)


class JobWatcher:
    def __init__(self, store: Optional[JobStore] = None, aria_factory: Optional[Callable[[], Any]] = None):
        self.store = store or get_job_store()
        self._aria_factory = aria_factory
        self._notices: deque[str] = deque()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _aria(self):
        if self._aria_factory:
            return self._aria_factory()
        from clients.aria import get_aria_client
        return get_aria_client()

    # ─── 通知佇列 ───
    def push(self, message: str) -> None:
        with self._lock:
            self._notices.append(message)

    def drain(self) -> list[str]:
        with self._lock:
            items = list(self._notices)
            self._notices.clear()
        return items

    # ─── 輪詢 ───
    def poll_once(self) -> None:
        jobs = self.store.list(WAITING_STAGES)
        if not jobs:
            return
        aria = self._aria()
        for job in jobs:
            try:
                for notice in poll_job(job, aria, self.store):
                    self.push(notice)
            except Exception as e:  # 單一工作失敗不影響其他工作；下次輪詢再試
                logger.exception("追蹤工作 %s 輪詢失敗", job.id)
                self.store.update(job, job.stage, note=f"上次查詢失敗：{type(e).__name__}: {e}")

    def interval(self) -> float:
        fast = self.store.list((DISK_RUNNING,))
        return config.JOB_FAST_POLL_INTERVAL if fast else config.JOB_POLL_INTERVAL

    def _run(self) -> None:
        waiting = self.store.list(REVIEW_STAGES)
        if waiting:
            self.push(f"🔔 有 {len(waiting)} 台 VM 等待你的審核 — 輸入 /jobs 處理")
        while not self._stop.is_set():
            self.poll_once()
            JOBS_CHANGED.wait(self.interval())
            JOBS_CHANGED.clear()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="job-watcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        JOBS_CHANGED.set()
