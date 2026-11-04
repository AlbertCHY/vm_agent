"""將 VM Bootstrap 子圖包裝為 Supervisor 可呼叫的標準工具 bootstrap_vm_pipeline

人工審核以 interrupt() 冒泡到 Supervisor，由介面層處理後恢復 (見 subgraphs/common/review.py)。
"""
from functools import lru_cache
from typing import Annotated, Any, Optional

from langchain_core.tools import InjectedToolCallId, tool

from subgraphs.common.runner import PipelineResult, derive_session_id, last_ai_text, run_pipeline
from subgraphs.vm_bootstrap.graph import build_vm_bootstrap_graph


@lru_cache(maxsize=1)
def get_vm_bootstrap_graph():
    return build_vm_bootstrap_graph()


def _summarize(session_id: str, values: dict[str, Any]) -> PipelineResult:
    exec_res = values.get("execution_result") or {}
    if exec_res.get("aborted"):
        status = "aborted"
    elif exec_res.get("success"):
        status = "completed"
    else:
        status = "failed"
    return PipelineResult(status, session_id, last_ai_text(values), data={"execution_result": exec_res})


def run_vm_bootstrap(
    ip: str,
    hostname: Optional[str] = None,
    cs_mode: str = "NGAV",
    skip_disk: bool = False,
    tool_call_id: Optional[str] = None,
) -> PipelineResult:
    """執行一次新機初始化子圖並回傳結構化結果"""
    sid = derive_session_id("vmboot", tool_call_id)
    graph_input = {
        "ip": ip.strip(),
        "hostname": hostname.strip() if hostname else None,
        "cs_mode": cs_mode.strip().upper() if cs_mode else "NGAV",
        "skip_disk": skip_disk,
        "human_feedback": None,
        "validation_errors": [],
        "execution_result": None,
        "step_results": [],
        "step_index": 0,
    }
    return run_pipeline("新機基本設定", sid, get_vm_bootstrap_graph(), graph_input,
                        lambda values: _summarize(sid, values))


@tool(response_format="content_and_artifact")
def bootstrap_vm_pipeline(
    ip: str,
    hostname: Optional[str] = None,
    cs_mode: str = "NGAV",
    skip_disk: bool = False,
    tool_call_id: Annotated[Optional[str], InjectedToolCallId] = None,
) -> tuple[str, dict[str, Any]]:
    """
    【新建立 Linux VM 的基本設定 (Bootstrap)】針對指定 IP 的新虛擬機，產生新密碼並經人工審核後，
    透過 SFTP 上傳標準化腳本並依序完成：SSH 公鑰、免密 sudo、Hostname、帳號密碼、Zabbix、CrowdStrike、
    系統碟 LVM 擴容、額外硬碟掛載 /data，最後清理暫存檔並驗證金鑰登入。
    使用者只是查詢或診斷時絕對不可呼叫。

    參數 ip: 目標 Linux VM 的 IPv4 位址 (必填；若使用者只提供 VM 名稱，請先用查詢工具找出 IP)。
    參數 hostname: (選填) 指定新主機名稱；不填則自動向 Aria 查詢該 IP 所屬的 deployment_name 作為主機名稱。
    參數 cs_mode: (選填) CrowdStrike 安裝模式，預設 "NGAV"；若需完整 MDR 填 "MDR"；若不需安裝填 "SKIP"。
    參數 skip_disk: (選填) 是否略過硬碟擴容與掛載腳本，預設 False (會自動掃描處理)。
    """
    result = run_vm_bootstrap(ip=ip, hostname=hostname, cs_mode=cs_mode, skip_disk=skip_disk,
                              tool_call_id=tool_call_id)
    return result.to_tool_output()
