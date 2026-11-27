"""將 VM Bootstrap 子圖包裝為 Supervisor 可呼叫的標準工具 bootstrap_vm_pipeline

前景完成人工審核 (interrupt() 冒泡到 Supervisor) 後，核准即送入 /jobs 背景執行，不阻塞主對話。
"""
from functools import lru_cache
from typing import Annotated, Any, Optional

from langchain_core.tools import InjectedToolCallId, tool

from subgraphs.common.runner import PipelineResult, derive_session_id, last_ai_text, run_pipeline
from subgraphs.vm_bootstrap.graph import build_vm_bootstrap_graph


@lru_cache(maxsize=1)
def get_vm_bootstrap_graph():
    return build_vm_bootstrap_graph()


def _summarize(session_id: str, values: dict[str, Any], *, background: bool) -> PipelineResult:
    exec_res = values.get("execution_result") or {}
    if exec_res.get("aborted"):
        return PipelineResult("aborted", session_id, last_ai_text(values), data={"execution_result": exec_res})

    action = (values.get("human_feedback") or {}).get("action")
    if background and action == "approve" and not exec_res:
        from subgraphs.jobs import enqueue_bootstrap_job

        job = enqueue_bootstrap_job(values, needs_review=False)
        os_badge = "Windows" if values.get("os_type") == "windows" else "Linux"
        msg = (
            f"✔ 已核准 {os_badge} VM `{values.get('ip')}` ({values.get('hostname')}) 的基本設定並送入背景執行"
            f"（工作編號 `#{job.id}`）。\n"
            f"- 可隨時輸入 `/jobs` 選中此工作查看累計執行輸出，完成或失敗時會自動通知。"
        )
        return PipelineResult("submitted", session_id, msg, data={"job_id": job.id, "ip": values.get("ip")})

    status = "completed" if exec_res.get("success") else "failed"
    return PipelineResult(status, session_id, last_ai_text(values), data={"execution_result": exec_res})


def run_vm_bootstrap(
    ip: str,
    hostname: Optional[str] = None,
    cs_mode: str = "NGAV",
    skip_disk: bool = False,
    os_type: Optional[str] = None,
    background: bool = True,
    tool_call_id: Optional[str] = None,
) -> PipelineResult:
    """執行新機初始化子圖：background=True 時審核通過立即送入 /jobs 背景執行"""
    sid = derive_session_id("vmboot", tool_call_id)
    graph_input = {
        "ip": ip.strip(),
        "os_type": os_type.strip().lower() if os_type else "",
        "hostname": hostname.strip() if hostname else None,
        "cs_mode": cs_mode.strip().upper() if cs_mode else "NGAV",
        "skip_disk": skip_disk,
        "handoff_to_background": background,
        "human_feedback": None,
        "validation_errors": [],
        "execution_result": None,
        "step_results": [],
        "step_index": 0,
    }
    return run_pipeline(
        "新機基本設定",
        sid,
        get_vm_bootstrap_graph(),
        graph_input,
        lambda values: _summarize(sid, values, background=background),
    )


@tool(response_format="content_and_artifact")
def bootstrap_vm_pipeline(
    ip: str,
    hostname: Optional[str] = None,
    cs_mode: str = "NGAV",
    skip_disk: bool = False,
    os_type: Optional[str] = None,
    tool_call_id: Annotated[Optional[str], InjectedToolCallId] = None,
) -> tuple[str, dict[str, Any]]:
    """
    【新建立 Linux / Windows VM 的基本設定 (Bootstrap)】針對指定 IP 的新虛擬機，產生新密碼並於前景經人工審核後，
    立即送入背景執行 (/jobs)，不阻塞對話，透過 SSH/SFTP 上傳對應作業系統的標準化腳本並依序完成初始化：
    - Linux：SSH 公鑰、免密 sudo、Hostname、帳號密碼、Zabbix、CrowdStrike、LVM 系統碟擴容、額外硬碟掛載 /data、清理與金鑰驗證。
    - Windows (OpenSSH)：SSH 公鑰、DNS、電腦名稱、帳號密碼、移除預設排程、Zabbix、SCCM、CrowdStrike、C 槽擴容、額外硬碟初始化 (GPT/NTFS)、系統檢查、金鑰驗證與重開機。
    使用者只是查詢或診斷時絕對不可呼叫；若使用者要求「先掛硬碟再做開機設置」，請直接呼叫 adjust_vm_disk(follow_up_bootstrap=True)。

    參數 ip: 目標 VM 的 IPv4 位址 (必填；若使用者只提供 VM 名稱，請先用查詢工具找出 IP)。
    參數 hostname: (選填) 指定新主機名稱；不填則自動向 Aria 查詢該 IP 所屬的 deployment_name 作為主機名稱。
    參數 cs_mode: (選填) CrowdStrike 安裝模式，預設 "NGAV"；若需完整 MDR 填 "MDR"；若不需安裝填 "SKIP"。
    參數 skip_disk: (選填) 是否略過硬碟擴容與掛載腳本，預設 False (會自動掃描處理)。
    參數 os_type: (選填) 作業系統類型，"linux" 或 "windows"；不填則自動向 Aria 反查判定 (查無時預設 linux，並可於審核畫面修改)。
    """
    result = run_vm_bootstrap(
        ip=ip, hostname=hostname, cs_mode=cs_mode, skip_disk=skip_disk,
        os_type=os_type, background=True, tool_call_id=tool_call_id,
    )
    return result.to_tool_output()
