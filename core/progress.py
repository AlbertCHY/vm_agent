"""子圖 / 工具的進度事件

節點內不直接 print：
- 前景執行時透過 LangGraph 的 custom stream 發出事件，由介面層決定顯示；
- 背景執行緒綁定 task_id 時，透過 contextvars.ContextVar 傳遞至子執行緒／平行節點，
  寫入該任務專屬的累計輸出緩衝區 (供 /jobs 查看，不搶主畫面輸出)；
- 無論前後景一律寫入日誌檔。
"""
import logging
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger("aria.progress")

_BG_TASK_ID: ContextVar[Optional[str]] = ContextVar("aria_bg_task_id", default=None)
_BG_SINKS: list[Callable[[str, str], None]] = []


def register_background_sink(sink: Callable[[str, str], None]) -> None:
    """註冊背景進度接收函式 (task_id, message)，避免 core 反向相依 subgraphs"""
    if sink not in _BG_SINKS:
        _BG_SINKS.append(sink)


@contextmanager
def bind_background_task(task_id: str) -> Iterator[None]:
    """在背景執行中綁定目前執行的 task_id，使 emit() 轉入該任務累計輸出而非前景串流"""
    token = _BG_TASK_ID.set(task_id)
    try:
        yield
    finally:
        _BG_TASK_ID.reset(token)


def current_background_task() -> Optional[str]:
    return _BG_TASK_ID.get()


def emit(message: str, **data: Any) -> None:
    logger.info(message)
    bg_task_id = current_background_task()
    if bg_task_id:
        for sink in list(_BG_SINKS):
            try:
                sink(bg_task_id, message)
            except Exception:
                pass
        return
    try:
        from langgraph.config import get_stream_writer
        writer = get_stream_writer()
    except Exception:  # 不在 graph 執行環境中 (例如單元測試直接呼叫)
        return
    try:
        writer({"type": "progress", "message": message, **data})
    except Exception:
        pass
