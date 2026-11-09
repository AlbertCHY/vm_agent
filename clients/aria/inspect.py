"""Aria 檢查工具：列出專案可用模板、模板欄位與選項，並可試跑 vRO action

用法：
    python -m clients.aria.inspect --project proj-demo
    python -m clients.aria.inspect --project proj-demo --item "Oracle"
    python -m clients.aria.inspect --vro com.example.module/Project_vLAN_List
"""
import argparse
import json
import sys

from rich.console import Console
from rich.table import Table

from clients.aria import get_aria_client

console = Console()


def show_items(project_name: str, item_filter: str | None) -> None:
    aria = get_aria_client()
    project = aria.iaas.resolve_project(project_name)
    if not project:
        console.print(f"[red]找不到專案 {project_name}[/red]")
        sys.exit(1)
    console.print(f"專案：[bold]{project['name']}[/bold] ({project['id']})")

    items = aria.catalog.list_items(project["id"])
    table = Table(title="可申請的 catalog items")
    table.add_column("名稱")
    table.add_column("ID", style="dim")
    for it in items:
        table.add_row(it.name, it.id)
    console.print(table)

    if not item_filter:
        return
    matched = [it for it in items if item_filter.lower() in it.name.lower()]
    if not matched:
        console.print(f"[red]沒有名稱包含「{item_filter}」的模板[/red]")
        return
    item = matched[0]
    version = aria.catalog.get_version(item.id)
    fields = aria.forms.fields_for(version)
    console.print(f"\n模板：[bold]{item.name}[/bold] 版本 {version.version}，formId={version.form_id}")
    ft = Table(title="欄位定義 (catalog schema + Custom Form)")
    for col in ("ID", "Label", "型別", "必填", "選項 / 來源", "預設", "顯示條件"):
        ft.add_column(col, overflow="fold")
    for f in fields:
        if f.options:
            opts = ", ".join(f"{o.label}={o.value}" if o.label != str(o.value) else str(o.value) for o in f.options)
        else:
            opts = f"(動態) {f.options_source}" if f.options_source else ""
        default = f"(動態) {f.default_source}" if f.default_source else (repr(f.default) if f.default is not None else "")
        ft.add_row(f.id, f.label, f.data_type, "✔" if f.required else "", opts, default,
                   json.dumps(f.visible_when, ensure_ascii=False) if f.visible_when else "")
    console.print(ft)


def try_vro(fqn: str) -> None:
    aria = get_aria_client()
    try:
        values = aria.vro.run_action_values(fqn)
    except Exception as e:
        console.print(f"[red]vRO action 執行失敗 (將繼續使用 policy)：{type(e).__name__}: {e}[/red]")
        return
    console.print(f"[green]vRO action 執行成功，取得 {len(values)} 個值：[/green]")
    console.print(values)


def main() -> None:
    parser = argparse.ArgumentParser(description="Aria catalog / form 檢查工具")
    parser.add_argument("--project", help="專案名稱或 ID")
    parser.add_argument("--item", help="模板名稱關鍵字 (顯示欄位定義)")
    parser.add_argument("--vro", help="試跑 vRO action，格式 module/ActionName")
    args = parser.parse_args()
    if args.project:
        show_items(args.project, args.item)
    if args.vro:
        try_vro(args.vro)
    if not (args.project or args.vro):
        parser.print_help()


if __name__ == "__main__":
    main()
