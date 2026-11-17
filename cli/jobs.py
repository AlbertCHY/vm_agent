"""/jobs：VM 生命週期追蹤清單與審核入口 (純 LangGraph Checkpointer 驅動)

所有等待階段與審核階段都在同一個 VMLifecycleGraph 的 thread_id 上推進：
- 遇到 disk_review / bootstrap_review 的 interrupt() 時，顯示審核畫面並以 Command(resume=...) 恢復。
- 硬碟調整核准後，由 disk_finalize (設定於 interrupt_after) 產生 ops 清單並立刻交給背景執行緒跑 submit_disk_op → wait_disk_op。
- 初始設定若在步驟中途被 Ctrl+C 中斷 (BOOTSTRAP_RUNNING)，再次進入 /jobs 時會直接以 stream(None) 從斷點步驟續跑，不重設也不重跑已完成步驟。
"""
import time
from typing import Any, Optional

from langgraph.types import Command
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table

from cli.render import console, print_progress
from cli.review import handle_review
from core.secrets import get_secret_store
from subgraphs.vm_lifecycle import (
    BOOTSTRAP_REVIEW,
    BOOTSTRAP_RUNNING,
    DISK_REVIEW,
    DISK_RUNNING,
    DONE,
    FAILED,
    FINAL_STAGES,
    REVIEW_STAGES,
    LifecycleJob,
    archive_lifecycle_job,
    cancel_lifecycle_job,
    get_job_lock,
    get_lifecycle_job,
    get_vm_lifecycle_graph,
    is_background_running,
    lifecycle_config,
    list_lifecycle_jobs,
    notify_poll_now,
    prepare_bootstrap_retry,
    spawn_background_continuation,
    wait_for_background,
)


def _stream_graph(graph, payload: Any, cfg: dict[str, Any]) -> None:
    for _ns, mode, chunk in graph.stream(payload, cfg, stream_mode=["custom", "updates"], subgraphs=True):
        if mode == "custom":
            print_progress(chunk)


def run_lifecycle_reviews(graph, thread_id: str, expected_kind: Optional[str] = None) -> dict[str, Any]:
    """
    恢復處於 interrupt() 的 VMLifecycleGraph，直到完成當前階段審核 (或遇到 interrupt_after / 下一個階段的 interrupt)。
    若設定 expected_kind (如 'vm_disk')，當推進到不同 kind 的中斷 (如略過硬碟直接進到 'vm_bootstrap') 時即停下返回。
    """
    cfg = lifecycle_config(thread_id)
    while True:
        state = graph.get_state(cfg)
        pending = list(state.interrupts or ())
        if not pending:
            return state.values or {}
        if expected_kind and (pending[0].value or {}).get("kind") != expected_kind:
            return state.values or {}

        payload = Command(resume={i.id: handle_review(i.value) for i in pending})
        _stream_graph(graph, payload, cfg)

        state = graph.get_state(cfg)
        if not state.interrupts:
            return state.values or {}


def _final_message(values: dict[str, Any]) -> str:
    msgs = values.get("messages") or []
    return str(msgs[-1].content) if msgs else ""


def run_disk_review(job_id: str, graph=None) -> None:
    g = graph or get_vm_lifecycle_graph()
    if not wait_for_background(job_id, timeout=5.0):
        console.print("[yellow]⏳ 背景執行緒正在收尾中，請稍候再試。[/yellow]")
        return

    with get_job_lock(job_id):
        job = get_lifecycle_job(job_id, graph=g)
        if not job:
            return
        cfg = lifecycle_config(job.thread_id)
        state = g.get_state(cfg)
        # 若停在 disk_locate (尚未進到 disk_review 的 interrupt)，先推進到 disk_review
        if state.next and "disk_locate" in state.next and not state.interrupts:
            g.invoke(None, config=cfg)

        try:
            run_lifecycle_reviews(g, job.thread_id, expected_kind="vm_disk")
        except KeyboardInterrupt:
            console.print("\n[yellow]已中斷硬碟審核；稍後可再由 /jobs 繼續。[/yellow]")
            return

        state = g.get_state(cfg)
        # 若在 disk_finalize 因 interrupt_after 暫停，但其實沒有任何硬碟動作需執行 (例如改為 0)，直接推進到 bootstrap_review
        if state.next and "prepare_bootstrap" in state.next and not state.interrupts:
            g.invoke(None, config=cfg)

        updated = get_lifecycle_job(job.id, graph=g)
        if not updated:
            return
        if updated.error:
            console.print(f"[red]❌ {escape(updated.error)}[/red]")
        elif updated.stage == DISK_RUNNING and updated.disk_ops:
            spawn_background_continuation(updated.id, graph=g)
            console.print("[green]✔ 已核准，硬碟動作會在背景依序執行，完成後通知你。[/green]")
        else:
            note = updated.note or "已略過硬碟調整"
            console.print(f"[yellow]{escape(note)}[/yellow]")


def run_bootstrap(job_id: str, graph=None) -> None:
    g = graph or get_vm_lifecycle_graph()
    if not wait_for_background(job_id, timeout=5.0):
        console.print("[yellow]⏳ 背景執行緒正在收尾中，請稍候再試。[/yellow]")
        return

    with get_job_lock(job_id):
        job = get_lifecycle_job(job_id, graph=g)
        if not job:
            return
        cfg = lifecycle_config(job.thread_id)
        state = g.get_state(cfg)
        next_nodes = tuple(state.next or ())

        try:
            # 情況 A：上次在執行步驟中途被中斷 (BOOTSTRAP_RUNNING，停在 connect_and_upload / run_step / cleanup / bootstrap_finalize)
            if any(n in next_nodes for n in ("connect_and_upload", "run_step", "cleanup", "bootstrap_finalize")) and not state.interrupts:
                vals = state.values or {}
                has_secrets = get_secret_store().has(vals.get("secret_ref"))
                if not has_secrets and not vals.get("credentials_saved"):
                    # 程式曾重啟導致記憶體密碼遺失，且尚未執行過改密碼步驟：重新產生密碼並審核，但保留已完成的 step_index
                    console.print("[yellow]ℹ️ 偵測到程式曾重啟，重新產生初始密碼供審核後從中斷步驟續跑...[/yellow]")
                    from subgraphs.vm_lifecycle.nodes import lifecycle_prepare_bootstrap
                    prep = lifecycle_prepare_bootstrap(vals)
                    g.update_state(cfg, prep, as_node="prepare_bootstrap")
                    g.invoke(None, config=cfg)
                    values = run_lifecycle_reviews(g, job.thread_id, expected_kind="vm_bootstrap")
                else:
                    step_idx = (vals.get("step_index") or 0) + 1
                    console.print(f"[cyan]▶ 從中斷處繼續執行初始設定 (步驟 {step_idx})...[/cyan]")
                    _stream_graph(g, None, cfg)
                    values = g.get_state(cfg).values or {}
            else:
                # 情況 B：正常於 bootstrap_review 審核後執行
                if "prepare_bootstrap" in next_nodes and not state.interrupts:
                    g.invoke(None, config=cfg)
                values = run_lifecycle_reviews(g, job.thread_id, expected_kind="vm_bootstrap")
        except KeyboardInterrupt:
            console.print("\n[yellow]已中斷初始設定；已完成的步驟已保存在 Checkpoint，稍後可由 /jobs 直接接續執行。[/yellow]")
            return

        res = values.get("execution_result") or {}
        msg = _final_message(values)
        if msg:
            console.print(Panel(escape(msg), title=f"🛠️ {job.deployment_name} 初始設定", border_style="green"))
        if not res.get("aborted") and not res.get("success"):
            # 初始設定失敗 (例如 VM 尚未開機完成、SSH 未就緒或某一步失敗)：保留已成功步驟與密碼供重試
            err = res.get("error") or "初始設定未全部成功，可重新執行"
            prepare_bootstrap_retry(job.id, error=err, graph=g)


def render_jobs(jobs: list[LifecycleJob]) -> None:
    table = Table(title="📦 VM 追蹤工作", show_header=True, header_style="bold cyan")
    for col in ("#", "部署名稱", "階段", "IP", "階段開始", "備註"):
        table.add_column(col)
    for i, j in enumerate(jobs, 1):
        style = (
            "yellow"
            if j.stage in REVIEW_STAGES
            else ("red" if j.stage == FAILED else ("green" if j.stage == DONE else "white"))
        )
        stage = f"[{style}]{j.stage_label}[/{style}]" + (" ⏰逾時" if j.overdue else "")
        note = j.error or j.note
        table.add_row(
            str(i),
            escape(j.deployment_name),
            stage,
            j.ip or "",
            time.strftime("%m-%d %H:%M", time.localtime(j.stage_since)),
            escape(note[:60]),
        )
    console.print(table)


def handle_jobs(graph=None) -> None:
    g = graph or get_vm_lifecycle_graph()
    jobs = list_lifecycle_jobs(graph=g)
    if not jobs:
        console.print("[green]目前沒有追蹤中的 VM。[/green]")
        return
    render_jobs(jobs)
    choice = Prompt.ask("選擇工作編號 (Enter 返回)", default="").strip()
    if not choice.isdigit() or not 1 <= int(choice) <= len(jobs):
        return
    selected_id = jobs[int(choice) - 1].id
    if is_background_running(selected_id):
        wait_for_background(selected_id, timeout=1.0)
    job = get_lifecycle_job(selected_id, graph=g)
    if not job:
        return
    if job.stage == DISK_REVIEW:
        run_disk_review(job.id, graph=g)
    elif job.stage in (BOOTSTRAP_REVIEW, BOOTSTRAP_RUNNING):
        run_bootstrap(job.id, graph=g)
    elif job.stage in FINAL_STAGES:
        if Prompt.ask("從清單移除此工作？", choices=["y", "n"], default="y") == "y":
            archive_lifecycle_job(job.id, graph=g)
    else:
        act = Prompt.ask(
            "此工作在背景等待中：[C] 立即重新檢查 / [X] 停止追蹤 / Enter 返回",
            choices=["c", "C", "x", "X", ""],
            default="",
        ).upper()
        if act == "C":
            notify_poll_now()
            spawn_background_continuation(job.id, graph=g)
            console.print("[dim]已要求背景節點立即重新檢查。[/dim]")
        elif act == "X":
            cancel_lifecycle_job(job.id, graph=g)
