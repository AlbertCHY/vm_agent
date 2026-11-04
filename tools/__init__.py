"""給 ReAct Supervisor 使用的標準 Tools"""
from tools.aria_query_tools import query_vm_by_ip, list_project_deployments
from tools.vm_diag_tools import (
    diagnose_vm_system_health,
    diagnose_vm_auth_issue,
    diagnose_vm_file_transfer,
    diagnose_vm_gui_issue,
    diagnose_vm_network,
    inspect_vm_detail,
)
from tools.ticket_tools import scan_and_parse_tickets

__all__ = [
    "query_vm_by_ip",
    "list_project_deployments",
    "diagnose_vm_system_health",
    "diagnose_vm_auth_issue",
    "diagnose_vm_file_transfer",
    "diagnose_vm_gui_issue",
    "diagnose_vm_network",
    "inspect_vm_detail",
    "scan_and_parse_tickets",
]
