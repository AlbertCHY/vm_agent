"""VM Provisioning 子圖的 State 與 LLM 抽取模型"""
from typing import Annotated, Any, Literal, Optional

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from typing_extensions import TypedDict


class VMIntent(BaseModel):
    """從自然語言抽取的「意圖」—— 只放使用者能表達的內容，不含任何 Aria ID 或需查詢的值"""
    project: Optional[str] = Field(None, description="Aria 專案名稱，例如 proj-demo")
    os_family: Optional[str] = Field(None, description="作業系統家族，例如 Ubuntu、Windows、Oracle、Rocky")
    os_version: Optional[str] = Field(None, description="作業系統版本描述，例如 24.04、2022 英文版、9.6")
    function: Optional[str] = Field(
        None, description="這台 VM 的功能縮寫，用於自動命名；英數字、簡短 (最多 8 字元)，例如 AP、DB、Web、Console、Worker")
    cpu: Optional[int] = Field(None, description="CPU 核心數")
    memory_gb: Optional[int] = Field(None, description="記憶體大小 (GB)")
    storage_type: Optional[Literal["HDD", "SSD"]] = Field(None, description="存儲類型，使用者有提到才填")
    sg: Optional[Literal["ap", "db", "web"]] = Field(None, description="Application Type，使用者明確提到 DB 或 Web 用途時才填")
    static_ip: Optional[str] = Field(None, description="使用者指定的固定 IP 位址，沒有指定則為 null")
    cluster: Optional[str] = Field(None, description="使用者明確指定的運算集群")
    deployment_name: Optional[str] = Field(None, description="使用者明確指定的部署名稱，沒有指定則為 null")
    case_no: Optional[str] = Field(None, description="需求單號 / 工單號")
    ap_owner: Optional[str] = Field(None, description="AP 應用負責人")
    os_owner: Optional[str] = Field(None, description="OS 系統負責人")
    ap_unit: Optional[str] = Field(None, description="應用單位 / 申請單位")
    db_owner: Optional[str] = Field(None, description="DB 負責人")
    description: Optional[str] = Field(None, description="用途說明 / 專案背景")
    boot_disk_gb: Optional[int] = Field(None, description="系統碟擴充後的總大小 (GB)，使用者有提到才填")
    extra_disk_gb: Optional[int] = Field(None, description="額外資料硬碟大小 (GB)，使用者有提到才填")


class ProvisioningState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    intent: dict[str, Any]                 # VMIntent (累積合併)
    project: Optional[dict[str, str]]      # {"id", "name"}
    template: Optional[dict[str, str]]     # {"id", "name", "version"}
    fields: list[dict[str, Any]]           # FormField (model_dump)
    values: dict[str, Any]                 # 欄位 ID → 值 (含 deploymentName)
    sources: dict[str, str]                # 欄位 ID → 值的來源
    notes: list[str]                       # 審核時的提示 (清單外的值、IP 已被使用…)
    overrides: dict[str, str]              # 使用者在審核中修改的原始值
    choices: dict[str, dict[str, Any]]     # LLM 選擇結果快取 (候選不變時不重新呼叫)
    missing: list[str]
    errors: list[str]
    reviewed: bool                         # 是否已進入過人工審核
    submit_after_validate: bool            # Edit 後若驗證通過直接送出
    human_feedback: Optional[dict[str, Any]]
    execution_result: Optional[dict[str, Any]]
