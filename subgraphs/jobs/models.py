"""統一背景工作 (/jobs) 的領域模型與階段常數"""
import time
from dataclasses import dataclass, field
from typing import Any

# 三種內建的可背景執行工作類型
KIND_BUILD = "vm_build"
KIND_DISK = "vm_disk"
KIND_BOOTSTRAP = "vm_bootstrap"

KIND_LABELS = {
    KIND_BUILD: "建機追蹤",
    KIND_DISK: "硬碟調整",
    KIND_BOOTSTRAP: "初始設定",
}

# 統一工作階段
WAITING = "waiting"       # 背景等待外部狀態 (例如 Aria 審核/建機中)
RUNNING = "running"       # 背景執行中 (硬碟動作 / Bootstrap 步驟執行中)
REVIEW = "review"         # 等待人工審核
RETRY = "retry"           # 失敗或中斷，等待從斷點續跑
DONE = "done"             # 完成
FAILED = "failed"         # 失敗

ACTIVE_STAGES = (WAITING, RUNNING)
REVIEW_STAGES = (REVIEW, RETRY)
FINAL_STAGES = (DONE, FAILED)


def _resolve_kind_label(kind: str) -> str:
    from subgraphs.jobs.registry import kind_label

    return kind_label(kind)


@dataclass
class BackgroundJob:
    """由 AppStore 索引 + 各自子圖 Checkpoint 即時推導出的統一背景工作視圖"""
    id: str
    thread_id: str
    kind: str
    title: str
    stage: str = WAITING
    stage_label: str = ""
    stage_since: float = field(default_factory=time.time)
    last_checked: float = 0.0
    deployment_id: str = ""
    deployment_name: str = ""
    vm_name: str = ""
    machine_id: str = ""
    ip: str = ""
    os_type: str = ""
    overdue: bool = False
    error: str = ""
    note: str = ""
    archived: bool = False
    next_steps: list[dict[str, Any]] = field(default_factory=list)
    disk_ops: list[dict[str, Any]] = field(default_factory=list)
    step_results: list[dict[str, Any]] = field(default_factory=list)
    step_index: int = 0
    total_steps: int = 0
    final_message: str = ""

    @property
    def kind_label(self) -> str:
        return _resolve_kind_label(self.kind)

    @property
    def needs_disk(self) -> bool:
        return self.kind == KIND_DISK or any(s.get("kind") == KIND_DISK for s in self.next_steps)

    def summary(self) -> dict[str, Any]:
        return {
            "job_id": self.id,
            "kind": self.kind,
            "kind_label": self.kind_label,
            "title": self.title,
            "stage": self.stage,
            "stage_label": self.stage_label,
            "vm_name": self.vm_name or self.deployment_name,
            "ip": self.ip,
            "os_type": self.os_type,
            "overdue": self.overdue,
            "error": self.error,
            "note": self.note,
            "next_steps": [_resolve_kind_label(s.get("kind", "")) for s in self.next_steps],
            "since": time.strftime("%Y-%m-%d %H:%M", time.localtime(self.stage_since)),
        }
