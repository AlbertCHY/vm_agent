"""VM Provisioning 子圖：模仿 Service Broker > Catalog 的手動申請流程

parse_intent → select_template → load_form → resolve_fields → validate
validate → ask_clarification (首次缺漏) | human_review | submit_request (Edit 後驗證通過)
human_review → submit_request | select_template (Edit：重新解析) | parse_intent (Reject) | END (Skip)

子圖不自帶 checkpointer：由 Supervisor 的工具呼叫時沿用 Supervisor 的持久化 checkpointer，
interrupt() 直接冒泡到 Supervisor，由介面層處理審核並恢復。
"""
from langgraph.graph import StateGraph, START, END

from subgraphs.vm_provisioning.nodes import (
    parse_intent,
    select_template,
    load_form,
    resolve_fields,
    validate,
    route_validate,
    ask_clarification,
    human_review,
    route_review,
    submit_request,
)
from subgraphs.vm_provisioning.state import ProvisioningState


def build_vm_provisioning_graph():
    builder = StateGraph(ProvisioningState)

    builder.add_node("parse_intent", parse_intent)
    builder.add_node("select_template", select_template)
    builder.add_node("load_form", load_form)
    builder.add_node("resolve_fields", resolve_fields)
    builder.add_node("validate", validate)
    builder.add_node("ask_clarification", ask_clarification)
    builder.add_node("human_review", human_review)
    builder.add_node("submit_request", submit_request)

    builder.add_edge(START, "parse_intent")
    builder.add_edge("parse_intent", "select_template")
    builder.add_edge("select_template", "load_form")
    builder.add_edge("load_form", "resolve_fields")
    builder.add_edge("resolve_fields", "validate")
    builder.add_conditional_edges("validate", route_validate, {
        "ask_clarification": "ask_clarification",
        "human_review": "human_review",
        "submit_request": "submit_request",
    })
    builder.add_edge("ask_clarification", END)
    builder.add_conditional_edges("human_review", route_review, {
        "submit_request": "submit_request",
        "human_review": "human_review",
        "select_template": "select_template",
        "parse_intent": "parse_intent",
        END: END,
    })
    builder.add_edge("submit_request", END)
    return builder.compile()
