"""VM 追蹤工作 (job)：申請送出後的生命週期狀態，存於 data/app.sqlite

階段 (stage)：
    submitted         已送出，等待 Aria 審核              (背景監看器輪詢)
    building          Aria 建立中                         (背景監看器輪詢)
    disk_review       等待「② 硬碟調整」人工審核          (/jobs 處理)
    disk_running      Day-2 硬碟動作執行中                (背景監看器輪詢 / 依序送出)
    bootstrap_review  等待「③ 初始設定」人工審核與執行    (/jobs 處理)
    done / failed     結束 (略過的步驟記在 note)
"""
import json
import sqlite3
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import config

SUBMITTED = "submitted"
BUILDING = "building"
DISK_REVIEW = "disk_review"
DISK_RUNNING = "disk_running"
BOOTSTRAP_REVIEW = "bootstrap_review"
DONE = "done"
FAILED = "failed"

WAITING_STAGES = (SUBMITTED, BUILDING, DISK_RUNNING)      # 由背景監看器推進
REVIEW_STAGES = (DISK_REVIEW, BOOTSTRAP_REVIEW)           # 等待使用者在 /jobs 處理
FINAL_STAGES = (DONE, FAILED)

STAGE_LABELS = {
    SUBMITTED: "等待 Aria 審核",
    BUILDING: "Aria 建立中",
    DISK_REVIEW: "等你審核：② 硬碟調整",
    DISK_RUNNING: "硬碟調整執行中",
    BOOTSTRAP_REVIEW: "等你審核：③ 初始設定",
    DONE: "完成",
    FAILED: "失敗",
}


@dataclass
class Job:
    id: str
    deployment_id: str
    deployment_name: str
    project: str = ""
    template: str = ""
    os_image: str = ""
    is_linux: bool = True
    boot_disk_gb: Optional[int] = None
    extra_disk_gb: Optional[int] = None
    stage: str = SUBMITTED
    stage_since: float = field(default_factory=time.time)
    vm_name: str = ""
    machine_id: str = ""
    ip: str = ""
    disk_ops: list[dict[str, Any]] = field(default_factory=list)   # 已核准、依序執行的 Day-2 動作
    threads: dict[str, str] = field(default_factory=dict)          # 審核階段 → 目前使用的 LangGraph thread_id
    overdue: bool = False
    error: str = ""
    note: str = ""
    archived: bool = False
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    last_checked: float = 0.0

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.stage, self.stage)

    @property
    def needs_disk(self) -> bool:
        return bool(self.boot_disk_gb or self.extra_disk_gb)

    def summary(self) -> dict[str, Any]:
        return {
            "job_id": self.id, "deployment_name": self.deployment_name, "project": self.project,
            "stage": self.stage, "stage_label": self.stage_label, "vm_name": self.vm_name, "ip": self.ip,
            "overdue": self.overdue, "error": self.error, "note": self.note,
            "since": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.stage_since)),
        }


class JobStore:
    """執行緒安全的 job 資料表；所有階段轉換都以「預期的舊階段」做條件更新，避免背景監看器與 CLI 互相覆蓋"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.execute(
                "CREATE TABLE IF NOT EXISTS vm_jobs (id TEXT PRIMARY KEY, stage TEXT, archived INTEGER, "
                "updated_at REAL, data TEXT)"
            )
            self._conn.commit()

    @staticmethod
    def _load(row) -> Job:
        return Job(**json.loads(row[0]))

    def create(self, **kwargs: Any) -> Job:
        job = Job(id=uuid.uuid4().hex[:8], **kwargs)
        self._write(job)
        return job

    def _write(self, job: Job) -> None:
        job.updated_at = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO vm_jobs(id, stage, archived, updated_at, data) VALUES (?,?,?,?,?) "
                "ON CONFLICT(id) DO UPDATE SET stage=excluded.stage, archived=excluded.archived, "
                "updated_at=excluded.updated_at, data=excluded.data",
                (job.id, job.stage, int(job.archived), job.updated_at, json.dumps(asdict(job), ensure_ascii=False)),
            )
            self._conn.commit()

    def get(self, job_id: str) -> Optional[Job]:
        with self._lock:
            row = self._conn.execute("SELECT data FROM vm_jobs WHERE id=?", (job_id,)).fetchone()
        return self._load(row) if row else None

    def list(self, stages: Optional[tuple[str, ...]] = None, include_archived: bool = False) -> list[Job]:
        sql, args = "SELECT data FROM vm_jobs WHERE 1=1", []
        if not include_archived:
            sql += " AND archived=0"
        if stages:
            sql += f" AND stage IN ({','.join('?' * len(stages))})"
            args += list(stages)
        with self._lock:
            rows = self._conn.execute(sql + " ORDER BY rowid", args).fetchall()
        return [self._load(r) for r in rows]

    def update(self, job: Job, expected_stage: Optional[str] = None, **changes: Any) -> Optional[Job]:
        """套用變更；expected_stage 不符 (已被他處推進) 時不寫入並回傳 None"""
        with self._lock:
            row = self._conn.execute("SELECT data FROM vm_jobs WHERE id=?", (job.id,)).fetchone()
        if not row:
            return None
        current = self._load(row)
        if expected_stage and current.stage != expected_stage:
            return None
        if "stage" in changes and changes["stage"] != current.stage:
            changes.setdefault("stage_since", time.time())
            changes.setdefault("overdue", False)
        for k, v in changes.items():
            setattr(current, k, v)
        self._write(current)
        return current


@lru_cache(maxsize=1)
def get_job_store() -> JobStore:
    return JobStore(config.DATA_DIR / "app.sqlite")


# 有新工作或狀態需要立即處理時 set()，背景監看器會提前醒來輪詢
JOBS_CHANGED = threading.Event()


def notify_jobs_changed() -> None:
    JOBS_CHANGED.set()
