"""Supervisor 對話回合執行：串流顯示、人工審核中斷處理、待續流程 (/pending)"""
import json
import logging
import time
from typing import Any, Optional

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.types import Command
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

import config
from cli.render import console, msg_text, msg_thoughts, print_progress, print_thoughts, print_tool_result
from cli.review import handle_review
from core.persistence import get_app_store

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
    for ns, mode, chunk in agent.stream(payload, config=run_config,
                                        stream_mode=["updates", "custom"], subgraphs=True):
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


def run_turn(agent, thread_id: str, user_text: Optional[str] = None, kind: str = "chat") -> str:
    """
    執行一輪對話 (user_text 為 None 時代表繼續先前暫停的流程)。
    遇到子圖人工審核中斷時，顯示審核畫面並以 Command(resume=...) 恢復，直到該輪結束。
    """
    run_config = _run_config(thread_id)
    store = get_app_store()
    final_text = ""

    if user_text is not None:
        store.touch_session(thread_id, user_text, kind)
        payload: Any = {"messages": [HumanMessage(content=user_text)]}
    else:
        payload = None

    try:
        while True:
            state = agent.get_state(run_config)
            pending = list(state.interrupts or ())
            if payload is None and pending:
                payload = _collect_decisions(pending)
                continue
            final_text = _stream(agent, payload, run_config) or final_text
            store.touch_session(thread_id, user_text or "", kind)
            pending = list(agent.get_state(run_config).interrupts or ())
            if not pending:
                break
            payload = _collect_decisions(pending)
    except KeyboardInterrupt:
        console.print("\n[yellow]已中斷本輪執行；若流程停在人工審核，可稍後以 /pending 繼續。[/yellow]")
        return final_text
    except Exception as e:
        logger.exception("Supervisor 執行錯誤 (thread=%s)", thread_id)
        console.print(f"[bold red]Supervisor 執行發生錯誤: {type(e).__name__}: {escape(str(e))}[/bold red]")
        return final_text

    if final_text:
        console.print(Panel(Markdown(final_text), title="🤖 Supervisor", border_style="green"))
    return final_text


def list_pending(agent) -> list[dict[str, Any]]:
    """列出仍停在人工審核或中途中斷的對話 Session"""
    pending = []
    for s in get_app_store().list_sessions():
        try:
            state = agent.get_state(_run_config(s["thread_id"]))
        except Exception:
            continue
        if state.interrupts or state.next:
            pending.append({**s, "waiting_review": bool(state.interrupts)})
    return pending


def choose_pending(agent) -> Optional[str]:
    items = list_pending(agent)
    if not items:
        console.print("[green]✔ 目前沒有等待審核或中斷的流程。[/green]")
        return None
    table = Table(title="⏸️ 待續流程", show_header=True, header_style="bold cyan")
    table.add_column("#", width=4)
    table.add_column("Session", style="dim")
    table.add_column("狀態")
    table.add_column("最初的需求", style="white")
    table.add_column("最後更新", style="dim")
    for i, it in enumerate(items, 1):
        status = "[yellow]等待人工審核[/yellow]" if it["waiting_review"] else "[red]中途中斷[/red]"
        table.add_row(str(i), it["thread_id"], status, escape(it["title"] or ""),
                      time.strftime("%m-%d %H:%M", time.localtime(it["updated_at"])))
    console.print(table)
    choice = Prompt.ask("選擇要繼續的流程編號 (Enter 取消)", default="").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(items):
        return None
    return items[int(choice) - 1]["thread_id"]
