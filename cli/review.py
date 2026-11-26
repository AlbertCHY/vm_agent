"""通用人工審核畫面：依子圖送出的 ReviewRequest (fields / actions) 渲染，回傳 ReviewDecision dict

本模組不認識任何特定子圖，也不做業務解析 (例如專案名稱解析、型別轉換)，
只把使用者的原始輸入交回子圖處理。
"""
from typing import Any

from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from cli.render import console
from core.secrets import get_secret_store
from subgraphs.common.review import SECRET_REF_PREFIX, ReviewDecision, parse_edits


def _display_value(field: dict[str, Any]) -> str:
    if field.get("secret_ref"):
        secrets = get_secret_store().get(field["secret_ref"]) or {}
        val = secrets.get(field.get("secret_key", ""))
        return escape(val) if val else "[red]<已遺失，核准後將重新產生>[/red]"
    val = field.get("value")
    if val is None or val == "":
        return "[red]<未填寫>[/red]"
    return escape(str(val))


def render_request(request: dict[str, Any]) -> None:
    fields = request.get("fields") or []
    has_source = any(f.get("source") for f in fields)
    table = Table(title=request.get("title") or "待審核項目", show_header=True, header_style="bold magenta")
    table.add_column("參數名稱 (Key)", style="cyan", width=18)
    table.add_column("說明", style="dim", width=26)
    table.add_column("設定值 (Value)", style="bold green", width=40)
    if has_source:
        table.add_column("來源", style="yellow", width=12)
    for f in fields:
        key = f.get("key", "")
        label = escape(str(f.get("label", "")))
        if f.get("options"):
            label += f"\n[dim italic]可選：{escape(str(f['options']))}[/dim italic]"
        row = [key + ("" if f.get("editable", True) else " 🔒"), label, _display_value(f)]
        if has_source:
            row.append(escape(str(f.get("source") or "")))
        table.add_row(*row)
    console.print(table)

    for note in request.get("notes") or []:
        console.print(f"[dim]ℹ️  {escape(note)}[/dim]")
    errors = request.get("errors") or []
    if errors:
        console.print(Panel("\n".join(f"• {escape(e)}" for e in errors),
                            title="❗ 驗證錯誤 (請修正後再送出)", border_style="red"))


def _protect_secret_edits(fields: list[dict[str, Any]], edits: dict[str, str]) -> dict[str, str]:
    """將機密欄位的修改值轉存本機記憶體 SecretStore，避免明文進入 Command(resume) 與 Checkpoint"""
    secret_keys = {f.get("key"): f.get("secret_key") or f.get("key") for f in fields if f.get("secret_ref")}
    if not secret_keys or not edits:
        return edits
    store = get_secret_store()
    protected = dict(edits)
    for key, raw in list(edits.items()):
        if key not in secret_keys:
            continue
        val = raw
        if val == "?":
            val = Prompt.ask(f"請輸入 {key} 的新值 (支援含逗號與特殊字元)").strip()
        ref = store.put({secret_keys[key]: val})
        protected[key] = f"{SECRET_REF_PREFIX}{ref}"
    return protected


def handle_review(request: dict[str, Any]) -> dict[str, Any]:
    """顯示審核畫面並收集決策 (KeyboardInterrupt 會往外拋，流程保持暫停並由 CLI 自動詢問續跑或由 /jobs 繼續)"""
    console.print("\n[bold yellow]⚠️  觸發人工審查機制 (Human-In-The-Loop)[/bold yellow]")
    if request.get("message"):
        console.print(f"[yellow]{escape(request['message'])}[/yellow]")
    render_request(request)

    actions = request.get("actions") or []
    by_key = {a["hotkey"].upper(): a for a in actions}
    console.print("\n[bold]請選擇審核操作：[/bold]")
    for a in actions:
        console.print(f"[cyan][{a['hotkey']}][/cyan] {escape(a['label'])}")

    choices = [k for hk in by_key for k in (hk.lower(), hk)]
    default = "A" if "A" in by_key else actions[0]["hotkey"]
    choice = Prompt.ask("\n請輸入操作選項", choices=choices, default=default).upper()
    action = by_key[choice]

    edits: dict[str, str] = {}
    feedback = ""
    if action.get("needs_edits"):
        fields = request.get("fields") or []
        editable = [f["key"] for f in fields if f.get("editable", True)]
        console.print("[cyan]請輸入欲修改的參數與數值 (格式: key=value，多個以逗號分隔)[/cyan]")
        console.print(f"[dim]可修改欄位：{', '.join(editable)}[/dim]")
        edits = _protect_secret_edits(fields, parse_edits(Prompt.ask("修改內容")))
    if action.get("needs_feedback"):
        prompt = "請輸入退回原因或調整需求" if action["action"] == "reject" else "請輸入原因 (可留空)"
        feedback = Prompt.ask(prompt, default="")

    return ReviewDecision(action=action["action"], edits=edits, feedback=feedback).model_dump()
