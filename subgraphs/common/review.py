"""通用人工審核 (HITL) 協定

子圖節點以 ask_review() 發出 ReviewRequest；LangGraph 的 interrupt() 會把它一路冒泡到
Supervisor 的 stream，由介面層 (CLI / Web / Chat) 依 fields / actions 渲染畫面，
再以 Command(resume=<ReviewDecision dict>) 恢復執行。

介面層只負責「顯示」與「收集使用者原始輸入」，不做任何業務解析或驗證；
所有轉型、名稱解析與 Pydantic 驗證都在子圖節點內完成。
"""
from typing import Any, Literal, Optional, TypedDict

from langgraph.types import interrupt
from pydantic import BaseModel, Field

ReviewAction = Literal["approve", "edit", "reject", "skip", "regenerate"]


class ReviewField(TypedDict, total=False):
    key: str
    label: str
    value: Any
    editable: bool
    source: str            # 值的來源 (需求敘述 / 模型選擇 / Aria 查詢 / 預設…)，供審核判斷
    options: str           # 可選項提示 (例如表單下拉選項)
    # 機密欄位：value 不放明文，改放 secret_ref + secret_key，由介面層從 SecretStore 取值顯示
    secret_ref: str
    secret_key: str


class ReviewActionSpec(TypedDict, total=False):
    action: ReviewAction
    hotkey: str
    label: str
    needs_edits: bool      # 介面需收集 key=value 修改內容
    needs_feedback: bool   # 介面需收集文字意見


class ReviewRequest(TypedDict, total=False):
    kind: str                       # 例如 "vm_deployment" / "vm_bootstrap"
    title: str
    message: str
    fields: list[ReviewField]
    actions: list[ReviewActionSpec]
    errors: list[str]               # 上一輪修改的驗證錯誤 (需修正後才能送出)
    notes: list[str]                # 其他提示資訊


class ReviewDecision(BaseModel):
    action: ReviewAction
    edits: dict[str, str] = Field(default_factory=dict, description="使用者輸入的原始 key=value (字串)")
    feedback: str = ""


# ─── 常用動作 ───
APPROVE = ReviewActionSpec(action="approve", hotkey="A", label="確認無誤，核准並執行 (Approve)")
EDIT = ReviewActionSpec(action="edit", hotkey="E", label="修改指定參數後執行 (Edit & Approve)", needs_edits=True)
REJECT = ReviewActionSpec(action="reject", hotkey="R", label="給予修改指示並退回重新推論 (Reject)", needs_feedback=True)
SKIP = ReviewActionSpec(action="skip", hotkey="S", label="略過 / 取消 (Skip)", needs_feedback=True)
REGENERATE = ReviewActionSpec(action="regenerate", hotkey="P", label="重新產生隨機密碼 (Regenerate)")


def review_request(
    kind: str,
    title: str,
    fields: list[ReviewField],
    actions: list[ReviewActionSpec],
    message: str = "",
    errors: Optional[list[str]] = None,
    notes: Optional[list[str]] = None,
) -> ReviewRequest:
    return ReviewRequest(
        kind=kind, title=title, message=message, fields=fields, actions=actions,
        errors=list(errors or []), notes=list(notes or []),
    )


def ask_review(request: ReviewRequest) -> ReviewDecision:
    """暫停圖形等待人工審核；恢復時回傳解析後的 ReviewDecision"""
    raw = interrupt(request)
    if isinstance(raw, ReviewDecision):
        return raw
    if isinstance(raw, dict):
        return ReviewDecision.model_validate(raw)
    raise TypeError(f"不支援的審核回覆格式: {type(raw).__name__}")


def parse_edits(text: str) -> dict[str, str]:
    """解析 `key=value, key2=value2` 形式的修改指令 (只切字串，不轉型)"""
    edits: dict[str, str] = {}
    for item in (text or "").split(","):
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            edits[k] = v
    return edits
