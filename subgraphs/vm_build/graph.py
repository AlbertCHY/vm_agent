"""VM 建機追蹤子圖 (init_build → wait_vm_ready → END)"""
from langgraph.graph import END, START, StateGraph

from subgraphs.vm_build.nodes import init_build, route_after_build, wait_vm_ready
from subgraphs.vm_build.state import BuildState


def build_vm_build_graph(checkpointer=None):
    b = StateGraph(BuildState)
    b.add_node("init_build", init_build)
    b.add_node("wait_vm_ready", wait_vm_ready)
    b.add_edge(START, "init_build")
    b.add_edge("init_build", "wait_vm_ready")
    b.add_conditional_edges(
        "wait_vm_ready",
        route_after_build,
        {"wait_vm_ready": "wait_vm_ready", END: END},
    )
    return b.compile(checkpointer=checkpointer)
