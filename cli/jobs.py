"""/jobs：統一背景工作清單、累計輸出檢視與審核入口

由 subgraphs.jobs.registry (JobSpec) 驅動，CLI 本身不寫死任何工作類型、節點名稱或明細欄位。
- 選中任何階段的工作皆可檢視背景累計輸出 (get_job_logs) 與已完成明細。
- 審核通過後一律切入背景執行 (不搶主畫面輸出)，完成或失敗時透過 push_notice 跳行通知。
- 執行中的工作可按 [R] 刷新最新累計輸出，[C] 立即重新檢查，[X] 停止工作。
"""
import time
from typing import Any

from langgraph.types import Command
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from cli.render import console
from cli.review import handle_review
from subgraphs.jobs import (
    DONE,
    FAILED,
    FINAL_STAGES,
    RETRY,
    REVIEW,
    REVIEW_STAGES,
    BackgroundJob,
    archive_job,
    cancel_job,
    clear_pending_input,
    complete_job,
    get_job,
    get_job_graph,
    get_job_lock,
    get_job_logs,
    get_job_spec,
    is_background_running,
    job_config,
    kind_label,
    list_jobs,
    notify_poll_now,
    spawn_job_background,
    wait_for_background,
)


def _run_review_loop(graph, thread_id: str) -> dict[str, Any]:
    """在前景處理待審核的 interrupt()，直到離開審核 (核准停在背景邊界、或使用者取消)"""
    cfg = job_config(thread_id)
    while True:
        snap = graph.get_state(cfg)
        pending = list(snap.interrupts or ())
        if not pending:
            return dict(snap.values or {})
        payload = Command(resume={itr.id: handle_review(itr.value) for itr in pending})
        graph.invoke(payload, config=cfg)


def render_job_detail(job: BackgroundJob) -> None:
    """顯示單一工作的累計輸出紀錄、JobSpec 明細表與最終報告"""
    header_lines = [
        f"[bold]工作編號[/bold]：#{job.id}   [bold]類型[/bold]：{job.kind_label}   [bold]狀態[/bold]：{escape(job.stage_label)}",
        f"[bold]目標[/bold]：{escape(job.title)}   [bold]IP[/bold]：{job.ip or '-'}   [bold]OS[/bold]：{job.os_type or '-'}",
    ]
    if job.next_steps:
        chain = " → ".join(kind_label(s.get("kind", "")) for s in job.next_steps)
        header_lines.append(f"[bold]後續串接[/bold]：{chain}")
    if job.error:
        header_lines.append(f"[bold red]錯誤[/bold red]：{escape(job.error)}")
    elif job.note:
        header_lines.append(f"[bold]目前進度[/bold]：{escape(job.note)}")
    console.print(Panel("\n".join(header_lines), title=f"📋 {escape(job.title)} ({job.kind_label})", border_style="cyan"))

    # 1. 背景累計輸出紀錄 (不搶主畫面，選中工作時一次呈現)
    logs = get_job_logs(job.id)
    if logs:
        log_text = "\n".join(escape(line) for line in logs[-60:])
        console.print(Panel(log_text, title=f"📜 累計執行輸出 (共 {len(logs)} 筆)", border_style="dim"))

    # 2. 由 JobSpec 宣告的工作進度明細表 (持久化於 Checkpoint，重啟後仍看得到)
    spec = get_job_spec(job.kind)
    tbl = spec.detail_table(job) if spec.detail_table else None
    if tbl:
        tbl_title, columns, rows = tbl
        t = Table(title=tbl_title, show_header=True, header_style="bold cyan")
        for col in columns:
            t.add_column(col)
        for row in rows:
            t.add_row(*(escape(str(cell)) for cell in row))
        console.print(t)

    # 3. 完成／失敗時的完整最終報告
    if job.stage in FINAL_STAGES and job.final_message:
        border = "green" if job.stage == DONE else "red"
        console.print(Panel(escape(job.final_message), title="📄 執行報告", border_style=border))


def run_job_review(job_id: str, checkpointer=None) -> None:
    """處理待審核 (REVIEW) 的工作：前景審核通過後立刻送入背景執行"""
    job = get_job(job_id, checkpointer=checkpointer)
    if not job:
        return
    if not wait_for_background(job.id, timeout=5.0):
        console.print("[yellow]⏳ 背景執行緒正在收尾中，請稍候再試。[/yellow]")
        return

    spec = get_job_spec(job.kind)
    with get_job_lock(job.id):
        g = get_job_graph(job.kind, checkpointer=checkpointer)
        cfg = job_config(job.thread_id)
        pending_in = clear_pending_input(job.id)
        try:
            if pending_in is not None:
                # 首次進入審核：啟動子圖直到撞上 review 的 interrupt()
                g.invoke(pending_in, config=cfg)
            else:
                snap = g.get_state(cfg)
                if snap.next and not snap.interrupts and any(n in snap.next for n in spec.review_nodes):
                    g.invoke(None, config=cfg)
            values = _run_review_loop(g, job.thread_id)
        except KeyboardInterrupt:
            console.print("\n[yellow]已中斷審核；稍後可再由 /jobs 繼續。[/yellow]")
            return

        snap = g.get_state(cfg)
        exec_res = (values or {}).get("execution_result") or {}
        if exec_res.get("aborted"):
            console.print(f"[yellow]{escape(exec_res.get('reason') or '已略過此工作。')}[/yellow]")
            chained = complete_job(job.id, checkpointer=checkpointer)
            if chained:
                console.print(
                    f"[cyan]➔ 已自動產生下一步「{chained.kind_label}」待審核工作（#{chained.id}），請於 /jobs 處理。[/cyan]"
                )
            return
        if exec_res.get("no_ops"):
            console.print("[yellow]沒有需要執行的硬碟動作。[/yellow]")
            chained = complete_job(job.id, checkpointer=checkpointer)
            if chained:
                console.print(
                    f"[cyan]➔ 已自動產生下一步「{chained.kind_label}」待審核工作（#{chained.id}），請於 /jobs 處理。[/cyan]"
                )
            return
        if exec_res.get("error"):
            console.print(f"[red]❌ {escape(str(exec_res['error']))}[/red]")
            return

        if snap.next:
            spawn_job_background(job.id, checkpointer=checkpointer)
            console.print(
                f"[green]✔ 已核准並送入背景執行（工作 #{job.id}）；可隨時進 /jobs 查看累計輸出，完成後會自動通知。[/green]"
            )


def run_job_retry(job_id: str, checkpointer=None) -> None:
    """從斷點繼續執行失敗或中斷的背景工作"""
    job = get_job(job_id, checkpointer=checkpointer)
    if not job:
        return
    if not wait_for_background(job.id, timeout=5.0):
        console.print("[yellow]⏳ 背景執行緒正在收尾中，請稍候再試。[/yellow]")
        return

    spec = get_job_spec(job.kind)
    with get_job_lock(job.id):
        g = get_job_graph(job.kind, checkpointer=checkpointer)
        cfg = job_config(job.thread_id)

        if spec.prepare_retry_before_resume is not None:
            try:
                proceed = spec.prepare_retry_before_resume(
                    job.id, g, cfg, lambda: _run_review_loop(g, job.thread_id),
                )
            except KeyboardInterrupt:
                console.print("\n[yellow]已中斷審核；稍後可再由 /jobs 繼續。[/yellow]")
                return
            if not proceed:
                return

        spawn_job_background(job.id, checkpointer=checkpointer)
        console.print(f"[green]▶ 已於背景從斷點續跑（工作 #{job.id}），可隨時刷新檢視輸出。[/green]")


def render_jobs(jobs: list[BackgroundJob]) -> None:
    table = Table(title="📦 VM 背景工作 (/jobs)", show_header=True, header_style="bold cyan")
    for col in ("#", "類型", "目標", "狀態", "IP", "開始時間", "備註"):
        table.add_column(col)
    for i, j in enumerate(jobs, 1):
        style = (
            "yellow"
            if j.stage in REVIEW_STAGES
            else ("red" if j.stage == FAILED else ("green" if j.stage == DONE else "cyan"))
        )
        stage = f"[{style}]{escape(j.stage_label)}[/{style}]" + (" ⏰逾時" if j.overdue else "")
        note = j.error or j.note
        if j.next_steps and not j.error:
            chain = "→".join(kind_label(s.get("kind", "")) for s in j.next_steps)
            note = f"{note} [接續:{chain}]".strip() if note else f"完成後接續：{chain}"
        table.add_row(
            str(i),
            j.kind_label,
            escape(j.title),
            stage,
            j.ip or "",
            time.strftime("%m-%d %H:%M", time.localtime(j.stage_since)),
            escape(note[:60]),
        )
    console.print(table)


def handle_jobs(checkpointer=None) -> None:
    jobs = list_jobs(checkpointer=checkpointer)
    if not jobs:
        console.print("[green]目前沒有追蹤或執行中的背景工作。[/green]")
        return
    render_jobs(jobs)
    choice = Prompt.ask("選擇工作編號 (Enter 返回)", default="").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(jobs):
        return
    selected_id = jobs[int(choice) - 1].id

    while True:
        job = get_job(selected_id, checkpointer=checkpointer)
        if not job:
            return
        render_job_detail(job)

        if job.stage == REVIEW:
            run_job_review(job.id, checkpointer=checkpointer)
            return

        if job.stage == RETRY:
            act = Prompt.ask(
                "此工作有未完成步驟：[R] 從斷點續跑 (背景) / [X] 移除工作 / Enter 返回",
                choices=["r", "R", "x", "X", ""],
                default="",
            ).upper()
            if act == "R":
                run_job_retry(job.id, checkpointer=checkpointer)
            elif act == "X":
                cancel_job(job.id, checkpointer=checkpointer)
            return

        if job.stage in FINAL_STAGES:
            if Prompt.ask("從清單移除此工作？", choices=["y", "n"], default="y") == "y":
                archive_job(job.id, checkpointer=checkpointer)
            return

        # WAITING / RUNNING：背景執行中，允許原地刷新累計輸出、立即重查或停止
        act = Prompt.ask(
            "背景執行中：[R] 重新整理最新輸出 / [C] 立即重新檢查 / [X] 停止工作 / Enter 返回",
            choices=["r", "R", "c", "C", "x", "X", ""],
            default="",
        ).upper()
        if act == "R":
            continue
        if act == "C":
            notify_poll_now(job.id)
            if not is_background_running(job.id):
                spawn_job_background(job.id, checkpointer=checkpointer)
            console.print("[dim]已要求背景節點立即重新檢查。[/dim]")
            continue
        if act == "X":
            stopped = cancel_job(job.id, checkpointer=checkpointer)
            if stopped:
                console.print("[yellow]已停止並移除此工作。[/yellow]")
            else:
                console.print("[yellow]已送出停止訊號；目前遠端操作結束後將自動停止並移除此工作。[/yellow]")
            return
        return
