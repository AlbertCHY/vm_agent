"""Aria Automation Cloud Ops Agent — 互動式 CLI 入口

用法：
    python app.py                     # 互動對話模式 (Supervisor ReAct Agent)
"""
import sys
import time
import uuid

from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

import config
from cli.jobs import handle_jobs
from cli.render import console
from cli.session import check_startup_interrupted_session, resolve_dangling_interrupts, run_turn
from core.logging_setup import setup_logging
from core.persistence import acquire_background_lock
from subgraphs.jobs import (
    REVIEW_STAGES,
    count_jobs_in_stages,
    drain_notices,
    resume_active_jobs,
    running_background_task_ids,
)
from supervisor import SUPERVISOR_TOOLS, build_supervisor_agent
from tools.ticket_tools import list_ticket_files


def _help_text() -> str:
    examples = (config.PROMPTS.get("cli") or {}).get("examples") or []
    lines = [
        "[bold]指令[/bold]",
        "  [cyan]/tickets[/cyan]   列出 tickets 資料夾中的工單檔案",
        "  [cyan]/paste[/cyan]     貼上整份工單純文字 (存入 tickets 資料夾後交給 Agent 處理)",
        "  [cyan]/jobs[/cyan]      VM 追蹤工作：查看申請後的建立進度，處理硬碟調整 / 初始設定審核",
        "  [cyan]/new[/cyan]       開啟新的對話 Session",
        "  [cyan]/help[/cyan]      顯示說明",
        "  [cyan]exit[/cyan]       離開程式",
    ]
    if examples:
        lines += ["", "[bold]範例問句[/bold]"] + [f"  [dim]• {escape(e)}[/dim]" for e in examples]
    return "\n".join(lines)


def display_welcome():
    tool_names = ", ".join(t.name for t in SUPERVISOR_TOOLS)
    welcome_text = (
        "[bold cyan]Aria Automation Cloud Ops Agent[/bold cyan]\n"
        "[white]架構：Supervisor (ReAct) + Tools + 獨立 LangGraph 背景工作子圖 (/jobs，原生 interrupt HITL) + Local Ollama 工單解析[/white]\n"
        f"[dim]已載入工具：{tool_names}[/dim]\n"
        f"[dim]工單資料夾：{config.TICKETS_DIR}[/dim]\n"
        f"[dim]Debug 思考過程顯示：{'開啟' if config.SHOW_AGENT_THOUGHTS else '關閉'} (由 .env 的 SHOW_AGENT_THOUGHTS 設定)[/dim]"
    )
    console.print(Panel(welcome_text, title="🤖 AI Agent Ready", border_style="cyan"))


def show_ticket_files():
    files = list_ticket_files()
    if not files:
        console.print(f"[yellow]📭 {config.TICKETS_DIR} 中沒有工單檔案 ({'/'.join(config.TICKET_EXTENSIONS)})。[/yellow]")
        return
    table = Table(title="📂 工單檔案", show_header=True, header_style="bold cyan")
    table.add_column("#", width=4)
    table.add_column("檔名", style="white")
    table.add_column("大小", justify="right")
    table.add_column("修改時間", style="dim")
    for i, p in enumerate(files, 1):
        st = p.stat()
        table.add_row(str(i), p.name, f"{st.st_size:,} B", time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)))
    console.print(table)


def paste_ticket() -> str | None:
    """讀取多行貼上的工單文字並存成 tickets/ 下的檔案，回傳檔名"""
    console.print(Panel(
        "[bold yellow]請貼上整份工單純文字內容。[/bold yellow]\n"
        "[dim]貼上完成後，在新的一行輸入 [bold green]END[/bold green] 送出；輸入 [bold red]CANCEL[/bold red] 取消。[/dim]",
        title="📋 工單貼上", border_style="yellow"
    ))
    lines: list[str] = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip().upper() == "END":
            break
        if line.strip().upper() == "CANCEL":
            console.print("[yellow]已取消。[/yellow]")
            return None
        lines.append(line)

    raw_text = "\n".join(lines).strip()
    if not raw_text:
        console.print("[red]⚠️ 輸入內容為空。[/red]")
        return None

    config.TICKETS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"pasted-{time.strftime('%Y%m%d-%H%M%S')}.txt"
    (config.TICKETS_DIR / filename).write_text(raw_text, encoding="utf-8")
    console.print(f"[green]✔ 已儲存為 {config.TICKETS_DIR / filename}[/green]")
    return filename


def process_ticket_file(agent, filename: str):
    """以獨立 Session 交給 Supervisor 處理單一工單，並允許使用者就該工單繼續對話"""
    thread_id = f"ticket-{filename}-{uuid.uuid4().hex[:6]}"
    console.print(f"\n[bold magenta]{'═' * 64}[/bold magenta]")
    console.print(f"[bold cyan]▶ 處理工單：{filename}[/bold cyan]  [dim](session {thread_id})[/dim]")
    run_turn(agent, thread_id, f"請處理工單檔案「{filename}」：先解析，再依工單類型調度後續流程。", kind="ticket")

    while True:
        reply = Prompt.ask("[bold blue]回覆 Agent[/bold blue] [dim](直接 Enter 結束此工單)[/dim]", default="").strip()
        if not reply:
            break
        run_turn(agent, thread_id, reply, kind="ticket")


def _show_notices() -> str:
    """印出背景工作的完成/待審核通知，並回傳輸入提示 (有待審核工作時顯示數量)"""
    for notice in drain_notices():
        console.print(f"[bold yellow]{escape(notice)}[/bold yellow]")
    waiting = count_jobs_in_stages(REVIEW_STAGES)
    running = len(running_background_task_ids())
    badges: list[str] = []
    if waiting:
        badges.append(f"🔔 {waiting} 項待審核")
    if running:
        badges.append(f"⏳ {running} 項背景執行中")
    suffix = f" [yellow]({' / '.join(badges)}，/jobs)[/yellow]" if badges else ""
    return "[bold blue]使用者[/bold blue]" + suffix


def run_interactive(agent):
    display_welcome()
    console.print(Panel(_help_text(), title="💬 對話模式", border_style="cyan"))
    resumed_tid = check_startup_interrupted_session(agent)
    thread_id = resumed_tid or f"chat-{uuid.uuid4().hex[:6]}"
    console.print(f"[dim]對話 Session ID: {thread_id}[/dim]\n")

    while True:
        try:
            resolve_dangling_interrupts(agent, thread_id)
            user_input = Prompt.ask(_show_notices()).strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[cyan]感謝使用，再見！[/cyan]")
            break
        if not user_input:
            continue

        cmd = user_input.lower()
        if cmd in ("exit", "quit", "/exit", "/quit", "q"):
            active_bg = running_background_task_ids()
            if active_bg:
                ans = Prompt.ask(
                    f"[yellow]⚠️ 目前尚有 {len(active_bg)} 項背景工作執行中，確定中斷離開？(已完成步驟已保存於 Checkpoint)[/yellow]",
                    choices=["y", "n"],
                    default="n",
                )
                if ans != "y":
                    continue
            console.print("[cyan]感謝使用，再見！[/cyan]")
            break
        if cmd == "/help":
            console.print(Panel(_help_text(), border_style="cyan"))
        elif cmd == "/tickets":
            show_ticket_files()
        elif cmd == "/paste":
            filename = paste_ticket()
            if filename:
                process_ticket_file(agent, filename)
        elif cmd == "/jobs":
            handle_jobs()
        elif cmd == "/new":
            thread_id = f"chat-{uuid.uuid4().hex[:6]}"
            console.print(f"[dim]已開啟新對話 Session ID: {thread_id}[/dim]")
        else:
            run_turn(agent, thread_id, user_input)


def main():
    setup_logging()
    config.TICKETS_DIR.mkdir(parents=True, exist_ok=True)
    agent = build_supervisor_agent()
    if acquire_background_lock():
        # 恢復上次未完成的背景工作 (vm_build / vm_disk) 並推播待審核/待續提醒
        resume_active_jobs()
    else:
        console.print("[yellow]⚠️ 偵測到另一個 CLI 實例正在執行背景追蹤；本視窗將不重複啟動背景輪詢。[/yellow]")

    run_interactive(agent)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]程式已中斷[/yellow]")
        sys.exit(130)
