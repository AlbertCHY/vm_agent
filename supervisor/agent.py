"""頂層 ReAct Supervisor：整合唯讀工具、工單解析工具與業務子圖工具"""
from typing import Optional

from langchain.agents import create_agent
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver

import config
from clients.llm import get_llm
from core.persistence import get_checkpointer
from subgraphs.vm_bootstrap import bootstrap_vm_pipeline
from subgraphs.vm_provisioning import deploy_vm_pipeline
from tools import (
    query_vm_by_ip,
    list_project_deployments,
    diagnose_vm_system_health,
    diagnose_vm_auth_issue,
    diagnose_vm_file_transfer,
    diagnose_vm_gui_issue,
    diagnose_vm_network,
    inspect_vm_detail,
    scan_and_parse_tickets,
)

# 新增業務子圖 (例如 vm_decommission_flow) 時，只要在這裡註冊其工具即可
SUPERVISOR_TOOLS: list[BaseTool] = [
    # 維運查詢與 Linux VM 診斷工具組 (Ops & Diag)
    query_vm_by_ip,
    list_project_deployments,
    diagnose_vm_system_health,
    diagnose_vm_auth_issue,
    diagnose_vm_file_transfer,
    diagnose_vm_gui_issue,
    diagnose_vm_network,
    inspect_vm_detail,
    # 工單處理工具組 (Ticket Subsystem)
    scan_and_parse_tickets,
    # 業務執行子圖 (Workflows)
    deploy_vm_pipeline,
    bootstrap_vm_pipeline,
]

SUPERVISOR_SYSTEM_PROMPT = config.PROMPTS["supervisor"]["system"]


def build_supervisor_agent(checkpointer: Optional[BaseCheckpointSaver] = None):
    """
    建立 Supervisor ReAct Agent (已編譯的 LangGraph)。
    預設使用 SQLite 持久化 checkpointer (data/checkpoints.sqlite)，子圖於工具中執行時也沿用它，
    因此等待人工審核或程式中斷的流程可在重啟後以相同 thread_id 繼續。
    """
    llm = get_llm()
    checkpointer = checkpointer or get_checkpointer()

    return create_agent(
        model=llm,
        tools=SUPERVISOR_TOOLS,
        system_prompt=SUPERVISOR_SYSTEM_PROMPT,
        checkpointer=checkpointer,
        name="cloud_ops_supervisor",
    )
