"""Rich 輸出呈現：訊息文字、工具結果、工單表格、進度事件"""
import json
from typing import Any

from langchain_core.messages import ToolMessage
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

console = Console()


def msg_text(msg: Any) -> str:
    """相容字串或 content blocks (只取一般 text 區塊，排除 thinking 區塊)"""
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        return "".join(parts)
    return str(content)


def msg_thoughts(msg: Any) -> str:
    """擷取 Gemini Thinking 模型回傳的思考過程區塊 (type == 'thinking')"""
    content = getattr(msg, "content", "")
    if not isinstance(content, list):
        return ""
    thoughts: list[str] = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "thinking":
            t = (block.get("thinking") or "").strip()
            if t:
                thoughts.append(t)
    return "\n".join(thoughts)


def print_thoughts(text: str) -> None:
    console.print(Panel(escape(text), title="💭 Agent 思考過程 (Thinking)", border_style="dim yellow", style="dim"))


def print_progress(event: dict[str, Any]) -> None:
    message = event.get("message") if isinstance(event, dict) else str(event)
    if message:
        console.print(f"[dim]  💭 {escape(str(message))}[/dim]", highlight=False)


def display_ticket_results(data: dict[str, Any]) -> None:
    """將 scan_and_parse_tickets 的 artifact 渲染為表格"""
    for t in data.get("tickets", []):
        if "error" in t:
            console.print(f"[red]📄 {escape(str(t.get('file')))}: {escape(str(t['error']))}[/red]")
            continue
        table = Table(title=f"📄 工單解析結果：{escape(str(t.get('file')))}", show_header=False, title_justify="left")
        table.add_column("項目", style="cyan", width=14)
        table.add_column("內容", style="white", width=80)
        table.add_row("工單單號", escape(str(t.get("ticket_id"))))
        table.add_row("工單類型", f"[bold yellow]{escape(str(t.get('ticket_type')))}[/bold yellow]")
        table.add_row("申請人 / 單位", escape(f"{t.get('applicant')} / {t.get('department')}"))
        table.add_row("需求摘要", escape(str(t.get("summary"))))
        table.add_row("簽核意見", escape(str(t.get("approval_notes") or "無")))
        for idx, item in enumerate(t.get("action_items", []), 1):
            table.add_row(f"動作 #{idx}", escape(str(item.get("description", item))))
        console.print(table)


# 依工具名稱指定 artifact 的專屬呈現方式
ARTIFACT_RENDERERS = {
    "scan_and_parse_tickets": display_ticket_results,
}


def print_tool_result(msg: ToolMessage) -> None:
    name = msg.name or "tool"
    renderer = ARTIFACT_RENDERERS.get(name)
    if renderer and isinstance(getattr(msg, "artifact", None), dict):
        renderer(msg.artifact)
        return
    text = msg_text(msg)
    try:
        preview = json.dumps(json.loads(text), ensure_ascii=False, separators=(",", ":"))
    except (json.JSONDecodeError, TypeError):
        preview = " ".join(text.split())
    style = "red" if text.lstrip().startswith("❌") or "[status=failed]" in text else "dim"
    console.print(f"[{style}]  ↳ {name}: {escape(preview[:160])}{'…' if len(preview) > 160 else ''}[/{style}]",
                  highlight=False)
