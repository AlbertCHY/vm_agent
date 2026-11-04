"""VM Provisioning 子圖的 State 與結構化抽取模型"""
from typing import Annotated, Any, Literal, Optional

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages
from pydantic import BaseModel, Field
from typing_extensions import TypedDict

from schemas import PartialVMDeploymentParams


class SpecExtractionResult(BaseModel):
    """LLM 結構化抽取輸出"""
    action_type: Literal["create_deployment", "expand_vm", "delete_vm", "query_status", "unknown"] = Field(
        default="create_deployment",
        description="使用者的意圖動作類型"
    )
    extracted_spec: PartialVMDeploymentParams = Field(  # type: ignore[valid-type]
        description="從使用者對話中抽取的規格參數"
    )
    project_name_query: Optional[str] = Field(
        default=None,
        description="使用者提及但需要工具查詢 project_id 的專案名稱"
    )
    blueprint_os_query: Optional[str] = Field(
        default=None,
        description="使用者提及但需要工具查詢 blueprint_id 的作業系統關鍵字"
    )
    storage_type: Optional[Literal["HDD", "SSD"]] = Field(
        default=None,
        description="使用者提及的存儲類型 (HDD 或 SSD)，用於自動選擇 datastore"
    )
    needs_user_input: bool = Field(
        default=False,
        description="是否仍缺少關鍵資訊需要使用者補充"
    )
    clarification_message: Optional[str] = Field(
        default=None,
        description="若缺少關鍵資訊，向使用者詢問補充的訊息"
    )


class ProvisioningState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    action_type: str
    current_spec: Optional[dict[str, Any]]
    missing_fields: list[str]
    # 最近一次人工審核的 ReviewDecision (model_dump)
    human_feedback: Optional[dict[str, Any]]
    # 送出前 Pydantic 驗證或修改內容的錯誤；非空時會回到人工審核要求修正
    validation_errors: list[str]
    execution_result: Optional[dict[str, Any]]
