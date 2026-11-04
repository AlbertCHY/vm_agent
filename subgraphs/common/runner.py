"""子圖工具共用的執行輔助 (PipelineRunner)

把子圖包成 Supervisor 工具時的共同邏輯集中於此：
- 子圖在工具內以 graph.invoke() 執行，interrupt() 會以 GraphBubbleUp 例外冒泡到 Supervisor，
  因此工具的錯誤處理「必須」放行 GraphBubbleUp。
- 審核恢復時，LangGraph 會重新執行整個工具函式，子圖則從中斷點續跑；
  所以工具在呼叫子圖之前的程式碼必須是無副作用、可重複執行的 (例如只讀取資料)。
"""
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from langchain_core.messages import AIMessage
from langgraph.errors import GraphBubbleUp

logger = logging.getLogger(__name__)


def derive_session_id(prefix: str, tool_call_id: Optional[str]) -> str:
    """由 tool_call_id 推導穩定的 session_id (審核恢復、重新執行工具時保持一致)"""
    if tool_call_id:
        return f"{prefix}-{hashlib.sha1(tool_call_id.encode()).hexdigest()[:8]}"
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def last_ai_text(values: dict[str, Any]) -> str:
    ai_msgs = [m for m in values.get("messages", []) if isinstance(m, AIMessage)]
    if not ai_msgs:
        return ""
    content = ai_msgs[-1].content
    return content if isinstance(content, str) else str(content)


@dataclass
class PipelineResult:
    status: str
    session_id: str
    message: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_tool_output(self) -> tuple[str, dict[str, Any]]:
        """(給 LLM 的精簡文字, 給介面層的結構化 artifact)"""
        content = f"[status={self.status}][session_id={self.session_id}]\n{self.message}".rstrip()
        artifact = {"status": self.status, "session_id": self.session_id, **self.data}
        return content, artifact


def run_pipeline(
    name: str,
    session_id: str,
    graph: Any,
    graph_input: dict[str, Any],
    summarize: Callable[[dict[str, Any]], PipelineResult],
) -> PipelineResult:
    """執行子圖；interrupt 一律放行給 Supervisor，其餘例外轉為 failed 結果"""
    try:
        values = graph.invoke(graph_input) or {}
    except GraphBubbleUp:
        raise
    except Exception as e:
        logger.exception("%s 子圖執行失敗 (session=%s)", name, session_id)
        return PipelineResult("failed", session_id, f"❌ {name} 子圖執行失敗：{type(e).__name__}: {e}")
    return summarize(values)
