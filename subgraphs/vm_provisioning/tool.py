"""將 VM Provisioning 子圖包裝為 Supervisor 可呼叫的標準工具 deploy_vm_pipeline

HITL 設計：
    子圖內的 human_review 節點以 interrupt() 暫停，中斷會直接冒泡到 Supervisor 的 stream；
    介面層 (CLI / Web) 取得 ReviewRequest 後，以 Command(resume=ReviewDecision) 恢復 Supervisor，
    LangGraph 會重新執行本工具，子圖則從中斷點續跑。工具與子圖都不需要知道介面層的存在。

補問延續：
    子圖回報 needs_clarification 時，把已解析的規格與對話暫存於 AppStore.pipeline_sessions，
    Supervisor 帶相同 session_id 再次呼叫時延續。

後續生命週期追蹤：
    送出申請後，啟動純 LangGraph 的 VMLifecycleGraph (subgraphs.vm_lifecycle)，
    於背景執行 wait_vm_ready 節點，完成後自動停在 disk_review / bootstrap_review 的 interrupt() 節點等待審核。
"""
from functools import lru_cache
from typing import Annotated, Any, Optional

from langchain_core.messages import HumanMessage, messages_from_dict, messages_to_dict
from langchain_core.tools import InjectedToolCallId, tool

from core.persistence import get_app_store
from subgraphs.common.runner import PipelineResult, derive_session_id, last_ai_text, run_pipeline
from subgraphs.vm_lifecycle import start_lifecycle_job
from subgraphs.vm_provisioning.graph import build_vm_provisioning_graph

PIPELINE_KIND = "vm_provisioning"


@lru_cache(maxsize=1)
def get_vm_provisioning_graph():
    """Lazy 編譯子圖 (整個行程共用一份)"""
    return build_vm_provisioning_graph()


def _summarize(session_id: str, values: dict[str, Any]) -> PipelineResult:
    exec_res = values.get("execution_result") or None
    if exec_res and exec_res.get("success"):
        status = "submitted"
    elif exec_res and exec_res.get("aborted"):
        status = "aborted"
    elif exec_res and exec_res.get("error"):
        status = "failed"
    elif values.get("missing") and not exec_res:
        status = "needs_clarification"
    else:
        status = "incomplete"
    return PipelineResult(
        status=status,
        session_id=session_id,
        message=last_ai_text(values),
        data={
            "project": values.get("project"),
            "template": values.get("template"),
            "values": values.get("values") or {},
            "missing": values.get("missing") or [],
            "execution_result": exec_res,
            "_intent": values.get("intent") or {},
            "_messages": values.get("messages", []),
        },
    )


def run_vm_pipeline(requirement_description: str, session_id: Optional[str] = None,
                    tool_call_id: Optional[str] = None) -> PipelineResult:
    """執行一次 VM 開機申請子圖並回傳結構化結果"""
    store = get_app_store()
    sid = session_id or derive_session_id("vmprov", tool_call_id)
    prior = store.load_pipeline(sid) if session_id else None
    prior_messages = messages_from_dict(prior.get("messages", [])) if prior else []

    graph_input = {
        "messages": prior_messages + [HumanMessage(content=requirement_description)],
        "intent": (prior or {}).get("intent") or {},
        "overrides": {},
        "choices": {},
        "notes": [],
        "errors": [],
        "missing": [],
        "reviewed": False,
        "submit_after_validate": False,
        "human_feedback": None,
        "execution_result": None,
    }
    result = run_pipeline("VM 開機申請", sid, get_vm_provisioning_graph(), graph_input,
                          lambda values: _summarize(sid, values))

    messages = result.data.pop("_messages", [])
    intent = result.data.pop("_intent", {})
    result.data["intent"] = intent
    if result.status == "needs_clarification":
        store.save_pipeline(sid, PIPELINE_KIND, {"intent": intent, "messages": messages_to_dict(messages)})
    else:
        store.delete_pipeline(sid)
    return result


def create_tracking_job(result: PipelineResult):
    """申請送出後啟動 VMLifecycleGraph 子圖 (背景等待建立 → 硬碟調整審核 → 初始設定審核)"""
    exec_res = result.data.get("execution_result") or {}
    if not exec_res.get("deployment_id"):
        return None
    intent = result.data.get("intent") or {}
    template = (exec_res.get("template") or {}).get("name", "")
    os_image = str((exec_res.get("inputs") or {}).get("OSimage") or "")
    is_windows = "win" in os_image.lower() or "windows" in template.lower()
    return start_lifecycle_job(
        deployment_id=exec_res["deployment_id"],
        deployment_name=exec_res.get("deployment_name") or "",
        project=(exec_res.get("project") or {}).get("name", ""),
        template=template,
        os_image=os_image,
        is_linux=not is_windows,
        boot_disk_gb=intent.get("boot_disk_gb"),
        extra_disk_gb=intent.get("extra_disk_gb"),
    )


@tool(response_format="content_and_artifact")
def deploy_vm_pipeline(
    requirement_description: str,
    session_id: Optional[str] = None,
    track: bool = True,
    tool_call_id: Annotated[Optional[str], InjectedToolCallId] = None,
) -> tuple[str, dict[str, Any]]:
    """
    【僅限開立 VM】傳入單一 VM 的完整需求描述，依 Aria Service Broker catalog 模板的表單組合申請內容，經人工審查 (HITL) 後送出申請。
    一次只處理一台 VM；多台 VM 請依序逐台呼叫，不要同時平行呼叫。
    回傳 status：submitted (已送出) / needs_clarification (缺欄位，需轉問使用者) / aborted (使用者取消) / failed / incomplete。
    參數 requirement_description: 單一 VM 的自然語言需求 (專案、作業系統、VM 名稱、單號、CPU、記憶體、負責人、單位、用途等)。
    參數 session_id: (選填) 若上一次呼叫回傳 needs_clarification，請帶入當時回傳的 session_id，
                     並在 requirement_description 只填使用者補充的內容，即可延續先前已解析的規格。
    參數 track: (選填，預設 True) 送出後建立追蹤工作：背景等待 VM 建立完成 → 硬碟調整審核 (敘述有硬碟需求時)
                → 初始設定審核。使用者明確表示「只要開機、不要後續流程」時才設為 False。
    """
    result = run_vm_pipeline(requirement_description, session_id=session_id, tool_call_id=tool_call_id)
    if result.status == "needs_clarification":
        result.message = (
            "開機流程缺少關鍵欄位，已暫停。請將以下問題轉達使用者，"
            f"取得答案後再以相同 session_id 呼叫 deploy_vm_pipeline：\n{result.message}"
        )
    elif result.status == "submitted" and track:
        job = create_tracking_job(result)
        if job:
            result.data["job_id"] = job.id
            result.message += (
                f"\n\n已建立追蹤工作 `{job.id}`：背景會等待 Aria 審核與建立完成，"
                + ("之後依序進行「② 硬碟調整」與「③ 初始設定」審核" if job.needs_disk else "之後進行「③ 初始設定」審核")
                + "；需要使用者審核時 CLI 會通知 (/jobs)。"
            )
    elif result.status == "aborted":
        result.message = f"使用者於人工審查時取消了此 VM 的部署。{result.message}"
    elif result.status == "incomplete" and not result.message:
        result.message = "流程未完成，請確認需求後重試。"
    return result.to_tool_output()
