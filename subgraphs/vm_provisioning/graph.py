"""VM Provisioning 子圖：編譯包含 interrupt() (HITL) 的 StateGraph

子圖不自帶 checkpointer：由 Supervisor 的工具在執行期間呼叫時，會沿用 Supervisor 的
持久化 checkpointer，interrupt() 也會直接冒泡到 Supervisor，由介面層處理審核並恢復。
"""
from langgraph.graph import StateGraph, START, END

from subgraphs.vm_provisioning.nodes import (
    analyze_and_extract,
    check_completeness,
    route_completeness,
    ask_clarification,
    human_review,
    route_review_result,
    submit_request,
)
from subgraphs.vm_provisioning.state import ProvisioningState


def build_vm_provisioning_graph():
    """建立 LangGraph 工作流 (規格抽取 → 完整性檢查 → 人工中斷審核 → 送出請求)"""
    builder = StateGraph(ProvisioningState)

    # 加入節點
    builder.add_node("analyze_and_extract", analyze_and_extract)
    builder.add_node("check_completeness", check_completeness)
    builder.add_node("ask_clarification", ask_clarification)
    builder.add_node("human_review", human_review)
    builder.add_node("submit_request", submit_request)

    # 加入邊
    builder.add_edge(START, "analyze_and_extract")
    builder.add_edge("analyze_and_extract", "check_completeness")

    # 條件邊：檢查參數完整性
    builder.add_conditional_edges(
        "check_completeness",
        route_completeness,
        {
            "ask_clarification": "ask_clarification",
            "human_review": "human_review"
        }
    )

    # 若缺少參數發問後即結束本輪，等待使用者補充
    builder.add_edge("ask_clarification", END)

    # 條件邊：人工審核結果 (核准 → 送出；修改有誤 → 重新審核；退回 → 重新推論；取消 → 結束)
    builder.add_conditional_edges(
        "human_review",
        route_review_result,
        {
            "submit_request": "submit_request",
            "human_review": "human_review",
            "analyze_and_extract": "analyze_and_extract",
            END: END
        }
    )

    # 提交完成結束
    builder.add_edge("submit_request", END)

    return builder.compile()
