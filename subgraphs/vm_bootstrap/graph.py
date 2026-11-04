"""VM Bootstrap 子圖：準備 → 人工審核 (interrupt) → 連線上傳 → 逐步執行 → 清理 → 驗證報告

子圖不自帶 checkpointer，沿用 Supervisor 的持久化 checkpointer；
每個步驟是一次獨立的節點執行，程式中斷後可從下一個步驟續跑。
"""
from langgraph.graph import StateGraph, START, END

from subgraphs.vm_bootstrap.nodes import (
    prepare_bootstrap,
    human_review,
    route_review_result,
    connect_and_upload,
    route_after_upload,
    run_step,
    route_next_step,
    cleanup,
    finalize,
)
from subgraphs.vm_bootstrap.state import BootstrapState


def build_vm_bootstrap_graph():
    builder = StateGraph(BootstrapState)

    builder.add_node("prepare_bootstrap", prepare_bootstrap)
    builder.add_node("human_review", human_review)
    builder.add_node("connect_and_upload", connect_and_upload)
    builder.add_node("run_step", run_step)
    builder.add_node("cleanup", cleanup)
    builder.add_node("finalize", finalize)

    builder.add_edge(START, "prepare_bootstrap")
    builder.add_edge("prepare_bootstrap", "human_review")
    builder.add_conditional_edges(
        "human_review",
        route_review_result,
        {
            "prepare_bootstrap": "prepare_bootstrap",
            "human_review": "human_review",
            "connect_and_upload": "connect_and_upload",
            END: END,
        },
    )
    builder.add_conditional_edges(
        "connect_and_upload", route_after_upload, {"run_step": "run_step", "finalize": "finalize"}
    )
    builder.add_conditional_edges("run_step", route_next_step, {"run_step": "run_step", "cleanup": "cleanup"})
    builder.add_edge("cleanup", "finalize")
    builder.add_edge("finalize", END)

    return builder.compile()
