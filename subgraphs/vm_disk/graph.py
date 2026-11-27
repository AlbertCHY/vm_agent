"""VM 硬碟調整子圖：定位機器 → 人工審核 (interrupt) → 依序送出並等待 Day-2 動作 → 報告

每個硬碟動作拆分為 submit_disk_op 與 wait_disk_op 兩個獨立節點，
確保送出取得 request_id 後即刻寫入 Checkpoint，並於 Add Disk 送出前執行冪等檢查。
"""
from langgraph.graph import END, START, StateGraph

from subgraphs.vm_disk.nodes import (
    finalize,
    locate,
    report,
    review,
    route_after_finalize,
    route_after_submit,
    route_after_wait,
    route_locate,
    route_review,
    submit_disk_op,
    wait_disk_op,
)
from subgraphs.vm_disk.state import DiskState


def build_vm_disk_graph(checkpointer=None, interrupt_after_finalize: bool = False):
    b = StateGraph(DiskState)
    b.add_node("locate", locate)
    b.add_node("review", review)
    b.add_node("finalize", finalize)
    b.add_node("submit_disk_op", submit_disk_op)
    b.add_node("wait_disk_op", wait_disk_op)
    b.add_node("report", report)

    b.add_edge(START, "locate")
    b.add_conditional_edges("locate", route_locate, {"review": "review", END: END})
    b.add_conditional_edges("review", route_review, {"review": "review", "finalize": "finalize", END: END})
    b.add_conditional_edges("finalize", route_after_finalize, {"submit_disk_op": "submit_disk_op", END: END})
    b.add_conditional_edges(
        "submit_disk_op", route_after_submit, {"wait_disk_op": "wait_disk_op", "report": "report", END: END}
    )
    b.add_conditional_edges(
        "wait_disk_op",
        route_after_wait,
        {"wait_disk_op": "wait_disk_op", "submit_disk_op": "submit_disk_op", "report": "report", END: END},
    )
    b.add_edge("report", END)
    compile_kwargs: dict = {"checkpointer": checkpointer}
    if interrupt_after_finalize:
        compile_kwargs["interrupt_after"] = ["finalize"]
    return b.compile(**compile_kwargs)
