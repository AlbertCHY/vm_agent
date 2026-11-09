"""跨模組共用的領域模型 (工單)

VM 開立欄位不再在此手寫：改由 Aria catalog schema + Custom Form 動態取得
(見 clients/aria/forms.py 的 FormField)。各子圖的 State 定義在 subgraphs/<name>/state.py。
"""
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field

# 工單類型列舉 (為未來彈性預留)
TicketType = Literal[
    "CREATE_VM",          # 建立虛擬機
    "EXPAND_RESOURCE",    # 資源擴容 (未來)
    "DECOMMISSION_VM",    # 註銷/下線虛擬機 (未來)
    "NETWORK_ADJUSTMENT", # 網路變更 (未來)
    "UNKNOWN"             # 未知/人工處理
]


class ParsedTicket(BaseModel):
    """統一工單封裝模型 (Ollama 解析產出)"""
    ticket_id: str = Field(description="工單單號，如 SRA26100500001")
    applicant: str = Field(description="申請人姓名/工號")
    department: str = Field(description="申請人部門單位")
    ticket_type: TicketType = Field(description="識別出的工單類型")
    summary: str = Field(description="工單需求摘要說明")
    approval_notes: Optional[str] = Field(default="", description="簽核審批意見")

    # 根據不同 ticket_type 存放具體的負載陣列
    # 例如 CREATE_VM 時，items 存放各台機器的描述文字或規格字典
    action_items: List[dict[str, Any]] = Field(
        default_factory=list,
        description="拆解出的具體動作清單 (每筆項目可獨立交由對應 Subgraph 處理)"
    )
    raw_content: Optional[str] = Field(default="", description="原始工單文本")
