"""VM 硬碟調整子圖 (Day-2) 的 State"""
from typing import Annotated, Any, Optional

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict


class DiskState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    deployment_id: Optional[str]
    ip: Optional[str]
    vm_name: str
    machine_id: str
    boot_disk_gb: Optional[int]
    extra_disk_gb: Optional[int]
    notes: list[str]
    errors: list[str]          # 上一輪修改內容的錯誤
    blocking: list[str]        # 機器狀態造成無法執行的問題 (例如動作 valid=false)
    human_feedback: Optional[dict[str, Any]]
    ops: list[dict[str, Any]]
    note: str
    error: str
    last_checked: float
    execution_result: Optional[dict[str, Any]]
