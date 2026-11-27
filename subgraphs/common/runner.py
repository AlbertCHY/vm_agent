"""子圖共用的執行輔助 (PipelineRunner & BackgroundGraphRunner)

同時支援兩種子圖執行模式：
1. 前景同步模式 (run_pipeline)：
   - 把子圖包成 Supervisor 工具時的共同邏輯；子圖在工具內以 graph.invoke() 執行，
     interrupt() 會以 GraphBubbleUp 例外冒泡到 Supervisor，由介面層處理審核後恢復。
2. 背景非同步模式 (spawn_background_graph / run_poll_loop)：
   - 供具有長時等待節點 (如 wait_vm_ready、wait_disk_op 或未來其他非同步 API 等待) 的子圖使用。
   - 以獨立 thread_id 在背景執行緒呼叫 graph.invoke(None, config)，直到撞到下一個審核節點的 interrupt() 或 END 自動停下。
   - 統一管理跨子圖的 Per-Task 執行鎖 (RLock)、Per-Task 喚醒事件、背景執行緒生命週期、通知佇列與即時進度快取。
"""
import hashlib
import logging
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from langchain_core.messages import AIMessage
from langgraph.errors import GraphBubbleUp

from core.progress import bind_background_task, register_background_sink

logger = logging.getLogger(__name__)

MAX_CONSECUTIVE_POLL_ERRORS = 5
MAX_JOB_LOG_LINES = 300


# ════════════════════════════════════════════════════════════════
# 1. 前景同步執行器 (Supervisor 工具內嵌子圖)
# ════════════════════════════════════════════════════════════════

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


# ════════════════════════════════════════════════════════════════
# 2. 背景非同步執行器 (獨立 thread_id 背景等待與推進)
# ════════════════════════════════════════════════════════════════

_STATE_LOCK = threading.Lock()
_NOTICES: deque[str] = deque()
_LIVE_PROGRESS: dict[str, dict[str, Any]] = {}
_JOB_LOGS: dict[str, deque[str]] = {}
_CANCELLED_TASKS: set[str] = set()
_ACTIVE_THREADS: dict[str, threading.Thread] = {}
_TASK_LOCKS: dict[str, threading.RLock] = {}
_WAKE_EVENTS: dict[str, threading.Event] = {}
_PENDING_FINALIZE: dict[str, Callable[[], None]] = {}
_RELEASE_LOCK_ON_EXIT: set[str] = set()


def _get_wake_event(task_id: str) -> threading.Event:
    with _STATE_LOCK:
        ev = _WAKE_EVENTS.get(task_id)
        if ev is None:
            ev = threading.Event()
            _WAKE_EVENTS[task_id] = ev
        return ev


def push_notice(message: str) -> None:
    """將背景子圖的通知推入佇列，供 CLI 於下一次輸入提示前顯示"""
    with _STATE_LOCK:
        _NOTICES.append(message)


def drain_notices() -> list[str]:
    """取出並清空目前所有背景通知"""
    with _STATE_LOCK:
        items = list(_NOTICES)
        _NOTICES.clear()
    return items


def append_job_log(task_id: str, message: str) -> None:
    """追加一筆帶時間戳的累計輸出給指定背景工作，供 /jobs 選中該任務時查閱"""
    if not task_id or not message:
        return
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {message}"
    with _STATE_LOCK:
        buf = _JOB_LOGS.get(task_id)
        if buf is None:
            buf = deque(maxlen=MAX_JOB_LOG_LINES)
            _JOB_LOGS[task_id] = buf
        buf.append(line)


def get_job_logs(task_id: str) -> list[str]:
    """取得指定工作到目前為止累計的輸出紀錄"""
    with _STATE_LOCK:
        buf = _JOB_LOGS.get(task_id)
        return list(buf) if buf else []


def set_live_progress(task_id: str, updates: dict[str, Any]) -> None:
    if not task_id:
        return
    with _STATE_LOCK:
        _LIVE_PROGRESS[task_id] = dict(updates)


def update_live_progress(task_id: str, updates: dict[str, Any]) -> None:
    """合併更新即時進度快取 (不覆蓋其他已存在欄位)"""
    if not task_id:
        return
    with _STATE_LOCK:
        cur = dict(_LIVE_PROGRESS.get(task_id) or {})
        cur.update(updates)
        _LIVE_PROGRESS[task_id] = cur


def _on_bg_emit(task_id: str, message: str) -> None:
    append_job_log(task_id, message)
    update_live_progress(task_id, {"note": message, "last_checked": time.time()})


register_background_sink(_on_bg_emit)


def get_live_progress(task_id: str) -> dict[str, Any]:
    with _STATE_LOCK:
        return dict(_LIVE_PROGRESS.get(task_id) or {})


def clear_live_progress(task_id: str) -> None:
    with _STATE_LOCK:
        _LIVE_PROGRESS.pop(task_id, None)


def mark_cancelled(task_id: str) -> None:
    with _STATE_LOCK:
        _CANCELLED_TASKS.add(task_id)
        ev = _WAKE_EVENTS.get(task_id)
    if ev is not None:
        ev.set()


def clear_cancelled(task_id: str) -> None:
    with _STATE_LOCK:
        _CANCELLED_TASKS.discard(task_id)


def is_cancelled(task_id: str) -> bool:
    with _STATE_LOCK:
        return task_id in _CANCELLED_TASKS


def notify_poll_now(task_id: Optional[str] = None) -> None:
    """喚醒指定背景任務 (不指定時喚醒全部) 立即執行下一次檢查"""
    with _STATE_LOCK:
        if task_id:
            ev = _WAKE_EVENTS.get(task_id)
            events = [ev] if ev is not None else []
        else:
            events = list(_WAKE_EVENTS.values())
    for ev in events:
        ev.set()


def wait_for_next_poll(timeout: float, task_id: Optional[str] = None) -> None:
    """背景等待節點專用的可中斷睡眠 (Per-Task Event，避免多任務競爭與誤喚醒)"""
    ev = _get_wake_event(task_id or "__global__")
    ev.wait(timeout)
    ev.clear()


def get_job_lock(task_id: str) -> threading.RLock:
    """取得指定背景任務的專屬鎖，避免前景審核與背景輪詢同時操作同一個 thread_id"""
    with _STATE_LOCK:
        lock = _TASK_LOCKS.get(task_id)
        if lock is None:
            lock = threading.RLock()
            _TASK_LOCKS[task_id] = lock
        return lock


def cleanup_task_resources(task_id: str) -> None:
    """清理已封存或取消任務的記憶體快取、累計輸出、喚醒事件與執行鎖。
    若對應背景執行緒仍在執行中，先保留執行鎖並標記於執行緒退出時回收，避免鎖洩漏或競態。
    """
    with _STATE_LOCK:
        _LIVE_PROGRESS.pop(task_id, None)
        _JOB_LOGS.pop(task_id, None)
        _CANCELLED_TASKS.discard(task_id)
        _WAKE_EVENTS.pop(task_id, None)
        _PENDING_FINALIZE.pop(task_id, None)
        t = _ACTIVE_THREADS.get(task_id)
        if t and t.is_alive():
            _RELEASE_LOCK_ON_EXIT.add(task_id)
        else:
            _TASK_LOCKS.pop(task_id, None)
            _RELEASE_LOCK_ON_EXIT.discard(task_id)


def defer_task_finalize(task_id: str, finalize_fn: Callable[[], None]) -> bool:
    """若背景執行緒仍在執行中，保留取消旗標並登記待執行緒結束後再執行 finalize_fn (回傳 False)；
    若執行緒已結束則立即執行 finalize_fn 並回傳 True。
    """
    with _STATE_LOCK:
        t = _ACTIVE_THREADS.get(task_id)
        if t and t.is_alive():
            _PENDING_FINALIZE[task_id] = finalize_fn
            _RELEASE_LOCK_ON_EXIT.add(task_id)
            return False
    finalize_fn()
    return True


def is_cancelling(task_id: str) -> bool:
    """任務已被要求停止，但背景執行緒仍在等待目前遠端操作收尾"""
    with _STATE_LOCK:
        t = _ACTIVE_THREADS.get(task_id)
        return (task_id in _CANCELLED_TASKS or task_id in _PENDING_FINALIZE) and bool(t and t.is_alive())


def is_background_running(task_id: str) -> bool:
    with _STATE_LOCK:
        t = _ACTIVE_THREADS.get(task_id)
        return bool(t and t.is_alive())


def running_background_task_ids() -> list[str]:
    """回傳目前仍在執行中的背景任務 ID 清單"""
    with _STATE_LOCK:
        return [tid for tid, t in _ACTIVE_THREADS.items() if t and t.is_alive()]


def wait_for_background(task_id: str, timeout: float = 5.0) -> bool:
    """等待指定任務的背景執行緒收尾結束；回傳 True 代表背景已完全停止"""
    with _STATE_LOCK:
        t = _ACTIVE_THREADS.get(task_id)
    if t and t.is_alive() and t is not threading.current_thread():
        t.join(timeout=timeout)
    return not is_background_running(task_id)


def run_poll_loop(
    task_id: str,
    label: str,
    interval: float,
    max_backoff: float,
    poll_once: Callable[[dict[str, Any]], Optional[dict[str, Any]]],
    initial_progress: dict[str, Any],
    wait_fn: Optional[Callable[[float], None]] = None,
    max_errors: int = MAX_CONSECUTIVE_POLL_ERRORS,
    error_label: str = "輪詢",
    cancelled_result: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """
    通用的背景輪詢迴圈骨架：
    - poll_once(current) 回傳非 None dict 時代表狀態轉變或完成，清除即時進度並回傳寫入 Checkpoint；
      回傳 None 代表繼續等待 (更新即時進度後進入可中斷睡眠)。
    - 遇例外時記錄連續錯誤、更新即時進度、採指數退避；遇 401/403 或達 max_errors 時推播通知並重新拋出。
    - 偵測到取消旗標時清理快取並回傳 cancelled_result。
    """
    wait = wait_fn or (lambda timeout: wait_for_next_poll(timeout, task_id=task_id))
    current: dict[str, Any] = dict(initial_progress)
    consecutive_errors = 0

    while not is_cancelled(task_id):
        try:
            result = poll_once(current)
            consecutive_errors = 0
            if result is not None:
                clear_live_progress(task_id)
                return result
            set_live_progress(task_id, current)
        except Exception as e:
            consecutive_errors += 1
            logger.exception("追蹤工作 %s %s失敗 (%d/%d)", task_id, error_label, consecutive_errors, max_errors)
            status_code = getattr(getattr(e, "response", None), "status_code", None)
            current["note"] = f"上次查詢失敗 ({consecutive_errors}/{max_errors})：{type(e).__name__}: {e}"
            set_live_progress(task_id, current)
            if status_code in (401, 403) or consecutive_errors >= max_errors:
                push_notice(
                    f"⚠️ {label} 連續查詢失敗 ({type(e).__name__}: {e})，已暫停背景輪詢 — 請於 /jobs 按 [C] 重試"
                )
                raise
            backoff = min(interval * (2 ** (consecutive_errors - 1)), max_backoff)
            wait(backoff)
            continue

        wait(interval)

    clear_live_progress(task_id)
    clear_cancelled(task_id)
    return dict(cancelled_result or {"sub_stage": "cancelled", "note": "使用者停止追蹤"})


def spawn_background_graph(
    task_id: str,
    graph: Any,
    run_config: dict[str, Any],
    name_prefix: str = "lg-bg",
    on_complete: Optional[Callable[[str], None]] = None,
) -> None:
    """
    在背景 Daemon 執行緒中綁定 task_id 並呼叫 graph.invoke(None, config=run_config)，
    推進等待／執行節點直到遇到下一個中斷點或 END 自動結束並釋放執行緒；
    執行完畢無例外時呼叫 on_complete(task_id) 處理完成通知或串接下一步。
    """
    task_lock = get_job_lock(task_id)

    with _STATE_LOCK:
        existing = _ACTIVE_THREADS.get(task_id)
        if existing and existing.is_alive():
            return

        def _runner():
            succeeded = False
            try:
                with task_lock:
                    with bind_background_task(task_id):
                        graph.invoke(None, config=run_config)
                    succeeded = True
                    if on_complete is not None:
                        on_complete(task_id)
            except Exception as e:
                logger.exception("背景子圖推進失敗 (task=%s)", task_id)
                append_job_log(task_id, f"❌ 背景執行發生例外：{type(e).__name__}: {e}")
                update_live_progress(
                    task_id,
                    {"note": f"背景推進暫停 ({type(e).__name__}: {e}) — 可於 /jobs 按 [C] 重試"},
                )
            finally:
                if succeeded:
                    clear_live_progress(task_id)
                clear_cancelled(task_id)
                with _STATE_LOCK:
                    _ACTIVE_THREADS.pop(task_id, None)
                    pending_fn = _PENDING_FINALIZE.pop(task_id, None)
                    release_lock = task_id in _RELEASE_LOCK_ON_EXIT
                    _RELEASE_LOCK_ON_EXIT.discard(task_id)
                if pending_fn is not None:
                    try:
                        pending_fn()
                    except Exception:
                        logger.exception("背景任務延遲收尾失敗 (task=%s)", task_id)
                if release_lock or pending_fn is not None:
                    with _STATE_LOCK:
                        t_now = _ACTIVE_THREADS.get(task_id)
                        if not (t_now and t_now.is_alive()):
                            _TASK_LOCKS.pop(task_id, None)

        t = threading.Thread(target=_runner, name=f"{name_prefix}-{task_id}", daemon=True)
        _ACTIVE_THREADS[task_id] = t
        t.start()
