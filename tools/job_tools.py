"""VM 追蹤工作查詢工具 (唯讀，直接讀取 VMLifecycleGraph Checkpoint 狀態)"""
import json

from langchain_core.tools import tool

from subgraphs.vm_lifecycle import list_lifecycle_jobs


@tool
def track_vm_jobs(include_finished: bool = True) -> str:
    """查詢由 deploy_vm_pipeline 建立的 VM 追蹤工作進度 (唯讀)。
    每台已送出申請的 VM 會依序經過：等待 Aria 審核 → Aria 建立中 → 等使用者審核「② 硬碟調整」(有需要時)
    → 硬碟調整執行中 → 等使用者審核「③ 初始設定」→ 完成 / 失敗。
    使用者詢問「申請的機器好了沒 / 建到哪了 / IP 是多少」時呼叫。需要人工審核的階段請提醒使用者在 CLI 輸入 /jobs 處理。
    參數 include_finished: 是否包含已完成或失敗的工作 (預設 True)。"""
    jobs = list_lifecycle_jobs()
    if not include_finished:
        jobs = [j for j in jobs if j.stage not in ("done", "failed")]
    if not jobs:
        return "目前沒有追蹤中的 VM。"
    return json.dumps({"total": len(jobs), "jobs": [j.summary() for j in jobs]}, ensure_ascii=False)
