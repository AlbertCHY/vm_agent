"""Supervisor 對話回合執行：串流顯示、人工審核中斷處理、未完成審核自動詢問續跑"""
import json
import logging
from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt

import config
from cli.render import console, msg_text, msg_thoughts, print_progress, print_thoughts, print_tool_result
from cli.review import handle_review
from core.persistence import get_app_store
from subgraphs.vm_lifecycle import LIFECYCLE_KIND

logger = logging.getLogger(__name__)

RECURSION_LIMIT = 60


def _run_config(thread_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id}, "recursion_limit": RECURSION_LIMIT}


def _render_update(update: dict[str, Any], final_text: str) -> str:
    for node_output in update.values():
        if not isinstance(node_output, dict):
            continue
        for msg in node_output.get("messages", []) or []:
            if isinstance(msg, AIMessage):
                if config.SHOW_AGENT_THOUGHTS:
                    thoughts = msg_thoughts(msg)
                    if thoughts:
                        print_thoughts(thoughts)
                text = msg_text(msg).strip()
                if text and msg.tool_calls and config.SHOW_AGENT_THOUGHTS:
                    console.print(f"[yellow]💭 {escape(text)}[/yellow]", highlight=False)
                for tc in msg.tool_calls or []:
                    args = json.dumps(tc.get("args", {}), ensure_ascii=False)
                    console.print(f"[cyan]🔧 {tc.get('name')}[/cyan] [dim]{escape(args[:200])}[/dim]", highlight=False)
                if text and not msg.tool_calls:
                    final_text = text
            elif isinstance(msg, ToolMessage):
                print_tool_result(msg)
    return final_text


def _stream(agent, payload: Any, run_config: dict[str, Any]) -> str:
    final_text = ""
    for ns, mode, chunk in agent.stream(
        payload, config=run_config, stream_mode=["updates", "custom"], subgraphs=True
    ):
        if mode == "custom":
            if config.SHOW_AGENT_THOUGHTS:
                print_progress(chunk)
        elif mode == "updates" and not ns and isinstance(chunk, dict):
            # 只呈現 Supervisor 本身的訊息；子圖內部訊息會由工具整理後回傳
            final_text = _render_update(chunk, final_text)
    return final_text


def _collect_decisions(interrupts) -> Command:
    # 一律以 interrupt id 對應恢復值：若子圖改走其他路徑 (例如密碼遺失需重新產生) 而發出新的中斷，
    # 舊的審核結果不會被誤套用到新的中斷上
    return Command(resume={itr.id: handle_review(itr.value) for itr in interrupts})


def _skip_decisions(interrupts, reason: str = "使用者放棄未完成的審核") -> Command:
    skip_val = {"action": "skip", "edits": {}, "feedback": reason}
    return Command(resume={itr.id: skip_val for itr in interrupts})


def _cancel_pending_interrupts(agent, run_config: dict[str, Any], pending) -> None:
    while pending:
        _stream(agent, _skip_decisions(pending), run_config)
        pending = list(agent.get_state(run_config).interrupts or ())
    console.print("[dim]已取消上次未完成的審核。[/dim]")


def resolve_dangling_interrupts(agent, thread_id: str) -> Optional[str]:
    """
    若指定對話 Session 仍有上次按 Ctrl+C 中斷而未完成的審核 (state.interrupts)，
    自動詢問使用者是否繼續審核：
    - 選 y：繼續完成該審核並回傳 Supervisor 的回覆文字
    - 選 n：自動以 skip 取消該工具呼叫 (補齊 ToolMessage)，避免後續傳入 HumanMessage 時報錯
    """
    run_config = _run_config(thread_id)
    pending = list(agent.get_state(run_config).interrupts or ())
    if not pending:
        return None

    ans = Prompt.ask(
        "⏸️ 偵測到上一次有未完成的審核，是否繼續審核？",
        choices=["y", "n", "Y", "N"],
        default="y",
    ).lower()
    if ans == "y":
        return run_turn(agent, thread_id, user_text=None)

    _cancel_pending_interrupts(agent, run_config, pending)
    return None


def check_startup_interrupted_session(agent) -> Optional[str]:
    """CLI 啟動時檢查是否有上次未完成審核的前景對話 Session，若有則詢問是否續跑"""
    for s in get_app_store().list_sessions(limit=20, exclude_kind=LIFECYCLE_KIND):
        tid = s["thread_id"]
        run_config = _run_config(tid)
        try:
            state = agent.get_state(run_config)
        except Exception:
            continue
        pending = list(state.interrupts or ())
        if not pending:
            continue
        title = escape((s.get("title") or tid)[:60])
        ans = Prompt.ask(
            f"⏸️ 偵測到上次對話「{title}」({tid}) 有未完成的審核，是否繼續？",
            choices=["y", "n", "Y", "N"],
            default="y",
        ).lower()
        if ans == "y":
            run_turn(agent, tid, user_text=None)
            return tid
        _cancel_pending_interrupts(agent, run_config, pending)
        break
    return None


def run_turn(agent, thread_id: str, user_text: Optional[str] = None, kind: str = "chat") -> str:
    """
    執行一輪對話 (user_text 為 None 時代表繼續先前暫停的流程)。
    遇到子圖人工審核中斷時，顯示審核畫面並以 Command(resume=...) 恢復，直到該輪結束。
    """
    run_config = _run_config(thread_id)
    store = get_app_store()
    final_text = ""

    try:
        pending = list(agent.get_state(run_config).interrupts or ())

        if user_text is not None:
            if pending:
                # 同一對話上一輪在審核中被 Ctrl+C 中斷：先詢問要繼續審核或放棄結案
                resolve_dangling_interrupts(agent, thread_id)
            store.touch_session(thread_id, user_text, kind)
            payload: Any = {"messages": [HumanMessage(content=user_text)]}
        else:
            payload = _collect_decisions(pending) if pending else None

        while True:
            final_text = _stream(agent, payload, run_config) or final_text
            pending = list(agent.get_state(run_config).interrupts or ())
            if not pending:
                break
            payload = _collect_decisions(pending)
        store.touch_session(thread_id, user_text or "", kind)
    except KeyboardInterrupt:
        console.print("\n[yellow]已中斷本輪執行；下次輸入時將自動詢問是否繼續未完成的審核。[/yellow]")
        return final_text
    except Exception as e:
        logger.exception("Supervisor 執行錯誤 (thread=%s)", thread_id)
        console.print(f"[bold red]Supervisor 執行發生錯誤: {type(e).__name__}: {escape(str(e))}[/bold red]")
        return final_text

    if final_text:
        console.print(Panel(Markdown(final_text), title="🤖 Supervisor", border_style="green"))
    return final_text
