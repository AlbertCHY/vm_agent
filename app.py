"""Aria Automation Cloud Ops Agent — 互動式 CLI 入口

用法：
    python app.py                     # 互動對話模式 (Supervisor ReAct Agent)
    python app.py --batch             # 批次處理 ./tickets/ 下所有工單
    python app.py --file SRA001.txt   # 只處理指定工單檔案
"""
import argparse
import sys
import time
import uuid

from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

import config
from cli.render import console
from cli.session import choose_pending, run_turn
from core.logging_setup import setup_logging
from supervisor import build_supervisor_agent, SUPERVISOR_TOOLS
from tools.ticket_tools import list_ticket_files


def _help_text() -> str:
    examples = (config.PROMPTS.get("cli") or {}).get("examples") or []
    lines = [
        "[bold]指令[/bold]",
        "  [cyan]/tickets[/cyan]   列出 tickets 資料夾中的工單檔案",
        "  [cyan]/batch[/cyan]     批次處理資料夾中所有工單",
        "  [cyan]/paste[/cyan]     貼上整份工單純文字 (存入 tickets 資料夾後交給 Agent 處理)",
        "  [cyan]/pending[/cyan]   列出等待人工審核或中途中斷的流程並繼續執行",
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
        "[white]架構：Supervisor (ReAct) + Tools + 業務子圖 (原生 interrupt HITL) + Local Ollama 工單解析[/white]\n"
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


def process_ticket_file(agent, filename: str, interactive_followup: bool = True):
    """以獨立 Session 交給 Supervisor 處理單一工單，並允許使用者就該工單繼續對話"""
    thread_id = f"ticket-{filename}-{uuid.uuid4().hex[:6]}"
    console.print(f"\n[bold magenta]{'═' * 64}[/bold magenta]")
    console.print(f"[bold cyan]▶ 處理工單：{filename}[/bold cyan]  [dim](session {thread_id})[/dim]")
    run_turn(agent, thread_id, f"請處理工單檔案「{filename}」：先解析，再依工單類型調度後續流程。", kind="ticket")

    if not interactive_followup:
        return
    while True:
        reply = Prompt.ask("[bold blue]回覆 Agent[/bold blue] [dim](直接 Enter 結束此工單)[/dim]", default="").strip()
        if not reply:
            break
        run_turn(agent, thread_id, reply, kind="ticket")


def run_batch(agent, filenames: list[str] | None = None, interactive_followup: bool = True):
    files = filenames or [p.name for p in list_ticket_files()]
    if not files:
        show_ticket_files()
        return
    console.print(f"[bold]📦 批次處理 {len(files)} 張工單：{', '.join(files)}[/bold]")
    for idx, name in enumerate(files, 1):
        console.print(f"[dim]({idx}/{len(files)})[/dim]")
        try:
            process_ticket_file(agent, name, interactive_followup=interactive_followup)
        except KeyboardInterrupt:
            console.print("\n[yellow]已中斷批次處理。[/yellow]")
            return
    console.print("\n[bold green]✔ 批次工單處理結束！[/bold green]")


def run_interactive(agent):
    display_welcome()
    console.print(Panel(_help_text(), title="💬 對話模式", border_style="cyan"))
    thread_id = f"chat-{uuid.uuid4().hex[:6]}"
    console.print(f"[dim]對話 Session ID: {thread_id}[/dim]\n")

    while True:
        try:
            user_input = Prompt.ask("[bold blue]使用者[/bold blue]").strip()
        except (KeyboardInterrupt, EOFError):
            console.print("\n[cyan]感謝使用，再見！[/cyan]")
            break
        if not user_input:
            continue

        cmd = user_input.lower()
        if cmd in ("exit", "quit", "/exit", "/quit", "q"):
            console.print("[cyan]感謝使用，再見！[/cyan]")
            break
        if cmd == "/help":
            console.print(Panel(_help_text(), border_style="cyan"))
        elif cmd == "/tickets":
            show_ticket_files()
        elif cmd == "/batch":
            run_batch(agent)
        elif cmd == "/paste":
            filename = paste_ticket()
            if filename:
                process_ticket_file(agent, filename)
        elif cmd == "/pending":
            resumed = choose_pending(agent)
            if resumed:
                thread_id = resumed
                console.print(f"[dim]已切換至 Session {thread_id}，繼續執行...[/dim]")
                run_turn(agent, thread_id, None)
        elif cmd == "/new":
            thread_id = f"chat-{uuid.uuid4().hex[:6]}"
            console.print(f"[dim]已開啟新對話 Session ID: {thread_id}[/dim]")
        else:
            run_turn(agent, thread_id, user_input)


def main():
    parser = argparse.ArgumentParser(description="Aria Automation Cloud Ops Agent CLI")
    parser.add_argument("--batch", action="store_true", help="批次處理 tickets 資料夾中的所有工單後結束")
    parser.add_argument("--file", action="append", metavar="NAME", help="只處理指定工單檔名 (可重複指定)")
    parser.add_argument("--no-followup", action="store_true", help="批次模式下不進入每張工單的追問對話")
    args = parser.parse_args()

    setup_logging()
    config.TICKETS_DIR.mkdir(parents=True, exist_ok=True)
    agent = build_supervisor_agent()

    if args.batch or args.file:
        display_welcome()
        run_batch(agent, filenames=args.file, interactive_followup=not args.no_followup)
        return

    run_interactive(agent)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        console.print("\n[yellow]程式已中斷[/yellow]")
        sys.exit(130)
