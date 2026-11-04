"""子圖 / 工具的進度事件

節點內不直接 print，而是透過 LangGraph 的 custom stream 發出事件，
由介面層 (CLI / Web) 決定是否顯示；同時寫入日誌檔。
"""
import logging
from typing import Any

logger = logging.getLogger("aria.progress")


def emit(message: str, **data: Any) -> None:
    logger.info(message)
    try:
        from langgraph.config import get_stream_writer
        writer = get_stream_writer()
    except Exception:  # 不在 graph 執行環境中 (例如單元測試直接呼叫)
        return
    try:
        writer({"type": "progress", "message": message, **data})
    except Exception:
        pass
