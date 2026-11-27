"""VM 建機追蹤子圖 (vm_build) 的 State"""
from typing_extensions import TypedDict

SUBMITTED = "submitted"
BUILDING = "building"
READY = "ready"
CANCELLED = "cancelled"


class BuildState(TypedDict, total=False):
    job_id: str
    deployment_id: str
    deployment_name: str
    project: str
    template: str
    os_image: str
    is_linux: bool
    vm_name: str
    machine_id: str
    ip: str
    sub_stage: str      # "submitted" | "building" | "ready" | "cancelled"
    stage_since: float
    overdue: bool
    note: str
    error: str
    last_checked: float
