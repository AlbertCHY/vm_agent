"""VM 生命週期子圖 (VMLifecycleGraph) 的 State 與階段定義

所有狀態皆保存在 LangGraph 的 SqliteSaver checkpointer (data/checkpoints.sqlite) 中，
不再額外維護獨立的 vm_jobs 資料表。
"""
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from subgraphs.vm_bootstrap.state import BootstrapState
from subgraphs.vm_disk.state import DiskState

SUBMITTED = "submitted"
BUILDING = "building"
DISK_REVIEW = "disk_review"
DISK_RUNNING = "disk_running"
BOOTSTRAP_REVIEW = "bootstrap_review"
BOOTSTRAP_RUNNING = "bootstrap_running"
DONE = "done"
FAILED = "failed"

WAITING_STAGES = (SUBMITTED, BUILDING, DISK_RUNNING)
REVIEW_STAGES = (DISK_REVIEW, BOOTSTRAP_REVIEW, BOOTSTRAP_RUNNING)
FINAL_STAGES = (DONE, FAILED)

STAGE_LABELS = {
    SUBMITTED: "等待 Aria 審核",
    BUILDING: "Aria 建立中",
    DISK_REVIEW: "等你審核：② 硬碟調整",
    DISK_RUNNING: "硬碟調整執行中",
    BOOTSTRAP_REVIEW: "等你審核：③ 初始設定",
    BOOTSTRAP_RUNNING: "等你續跑：③ 初始設定 (中斷待續)",
    DONE: "完成",
    FAILED: "失敗",
}


class VMLifecycleState(DiskState, BootstrapState, total=False):
    """繼承 DiskState 與 BootstrapState，僅宣告生命週期專屬欄位"""
    # ─── 1. 開機申請送出後的基礎識別資訊 ───
    job_id: str
    deployment_name: str
    project: str
    template: str
    os_image: str
    is_linux: bool

    # ─── 2. 階段追蹤與等待資訊 ───
    sub_stage: str                     # "submitted" | "building" | "ready" | "cancelled"
    stage_since: float
    overdue: bool


@dataclass
class LifecycleJob:
    """由 LangGraph Checkpoint StateSnapshot 即時推導出的 VM 生命週期狀態視圖"""
    id: str
    thread_id: str
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
    disk_ops: list[dict[str, Any]] = field(default_factory=list)
    overdue: bool = False
    error: str = ""
    note: str = ""
    archived: bool = False
    last_checked: float = 0.0

    @property
    def stage_label(self) -> str:
        return STAGE_LABELS.get(self.stage, self.stage)

    @property
    def needs_disk(self) -> bool:
        return bool(self.boot_disk_gb or self.extra_disk_gb)

    def summary(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "deployment_name": self.deployment_name,
            "project": self.project,
            "template": self.template,
            "os_image": self.os_image,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "vm_name": self.vm_name,
            "ip": self.ip,
            "overdue": self.overdue,
            "error": self.error,
            "note": self.note,
            "since": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.stage_since)),
        }
