"""VM 生命週期統一子圖 (VMLifecycleGraph)

串聯：
    init_lifecycle → wait_vm_ready
      → (disk_locate → disk_review [interrupt] → disk_finalize [interrupt_after] → submit_disk_op → wait_disk_op)
      → (prepare_bootstrap → bootstrap_review [interrupt] → connect_and_upload → run_step [單步迴圈] → cleanup → bootstrap_finalize)
      → END

設計重點：
1. 等待節點 (wait_vm_ready / wait_disk_op) 與審核節點 (disk_review / bootstrap_review) 嚴格分離，
   確保 Resume 時只重跑審核節點，不會重跑前面的狀態掃描。
2. 硬碟動作拆分為 submit_disk_op (送出後立即寫入 request_id 至 Checkpoint) 與 wait_disk_op (只輪詢狀態)，
   徹底防止在動作執行中重啟導致重複送出硬碟申請。
3. disk_finalize 設定於 interrupt_after：使用者於 /jobs 核准硬碟調整後，
   disk_finalize 組完 ops 清單即暫停並釋放前景 CLI，交由背景執行緒跑 submit_disk_op → wait_disk_op。
"""
from langgraph.graph import END, START, StateGraph

from core.persistence import get_checkpointer
from subgraphs.vm_bootstrap import nodes as boot_nodes
from subgraphs.vm_lifecycle.nodes import (
    bootstrap_finalize,
    bootstrap_review,
    disk_finalize,
    disk_locate,
    disk_review,
    init_lifecycle,
    lifecycle_prepare_bootstrap,
    route_after_build,
    route_after_disk_finalize,
    route_after_submit_disk_op,
    route_bootstrap_review,
    route_disk_locate,
    route_disk_op,
    route_disk_review,
    submit_disk_op,
    wait_disk_op,
    wait_vm_ready,
)
from subgraphs.vm_lifecycle.state import VMLifecycleState


def build_vm_lifecycle_graph(checkpointer=None):
    builder = StateGraph(VMLifecycleState)

    # 1. 初始化與等待 VM 建立
    builder.add_node("init_lifecycle", init_lifecycle)
    builder.add_node("wait_vm_ready", wait_vm_ready)

    # 2. Day-2 硬碟調整：定位 → 審核 → 組裝 ops → 送出單一動作 → 等待單一動作
    builder.add_node("disk_locate", disk_locate)
    builder.add_node("disk_review", disk_review)
    builder.add_node("disk_finalize", disk_finalize)
    builder.add_node("submit_disk_op", submit_disk_op)
    builder.add_node("wait_disk_op", wait_disk_op)

    # 3. Bootstrap 初始設定
    builder.add_node("prepare_bootstrap", lifecycle_prepare_bootstrap)
    builder.add_node("bootstrap_review", bootstrap_review)
    builder.add_node("connect_and_upload", boot_nodes.connect_and_upload)
    builder.add_node("run_step", boot_nodes.run_step)
    builder.add_node("cleanup", boot_nodes.cleanup)
    builder.add_node("bootstrap_finalize", bootstrap_finalize)

    # ─── Edges ───
    builder.add_edge(START, "init_lifecycle")
    builder.add_edge("init_lifecycle", "wait_vm_ready")
    builder.add_conditional_edges(
        "wait_vm_ready",
        route_after_build,
        {
            "wait_vm_ready": "wait_vm_ready",
            "disk_locate": "disk_locate",
            "prepare_bootstrap": "prepare_bootstrap",
            END: END,
        },
    )

    builder.add_conditional_edges(
        "disk_locate",
        route_disk_locate,
        {"disk_review": "disk_review", END: END},
    )
    builder.add_conditional_edges(
        "disk_review",
        route_disk_review,
        {
            "disk_review": "disk_review",
            "disk_finalize": "disk_finalize",
            "prepare_bootstrap": "prepare_bootstrap",
            END: END,
        },
    )
    builder.add_conditional_edges(
        "disk_finalize",
        route_after_disk_finalize,
        {
            "submit_disk_op": "submit_disk_op",
            "prepare_bootstrap": "prepare_bootstrap",
            END: END,
        },
    )
    builder.add_conditional_edges(
        "submit_disk_op",
        route_after_submit_disk_op,
        {
            "wait_disk_op": "wait_disk_op",
            "prepare_bootstrap": "prepare_bootstrap",
            END: END,
        },
    )
    builder.add_conditional_edges(
        "wait_disk_op",
        route_disk_op,
        {
            "wait_disk_op": "wait_disk_op",
            "submit_disk_op": "submit_disk_op",
            "prepare_bootstrap": "prepare_bootstrap",
            END: END,
        },
    )

    builder.add_edge("prepare_bootstrap", "bootstrap_review")
    builder.add_conditional_edges(
        "bootstrap_review",
        route_bootstrap_review,
        {
            "prepare_bootstrap": "prepare_bootstrap",
            "bootstrap_review": "bootstrap_review",
            "connect_and_upload": "connect_and_upload",
            END: END,
        },
    )
    builder.add_conditional_edges(
        "connect_and_upload",
        lambda s: "bootstrap_finalize" if s.get("execution_result") else "run_step",
        {"run_step": "run_step", "bootstrap_finalize": "bootstrap_finalize"},
    )
    builder.add_conditional_edges(
        "run_step",
        boot_nodes.route_next_step,
        {"run_step": "run_step", "cleanup": "cleanup"},
    )
    builder.add_edge("cleanup", "bootstrap_finalize")
    builder.add_edge("bootstrap_finalize", END)

    return builder.compile(
        checkpointer=checkpointer or get_checkpointer(),
        interrupt_after=["disk_finalize"],
    )
