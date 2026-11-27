"""VM 背景工作查詢工具 (唯讀，直接讀取統一 JobManager 狀態)"""
import json

from langchain_core.tools import tool

from subgraphs.jobs import FINAL_STAGES, list_jobs


@tool
def track_vm_jobs(include_finished: bool = True) -> str:
    """查詢目前所有 VM 背景工作進度 (唯讀)，涵蓋：建機追蹤 (vm_build)、硬碟調整 (vm_disk)、新機初始設定 (vm_bootstrap)。
    使用者詢問「機器建好了沒 / 硬碟掛完了嗎 / Bootstrap 跑到哪了 / IP 是多少」時呼叫。
    需要人工審核或查看詳細累計輸出的工作，請提醒使用者在 CLI 輸入 /jobs 處理。
    參數 include_finished: 是否包含已完成或失敗的工作 (預設 True)。"""
    jobs = list_jobs()
    if not include_finished:
        jobs = [j for j in jobs if j.stage not in FINAL_STAGES]
    if not jobs:
        return "目前沒有追蹤中的背景工作。"
    return json.dumps({"total": len(jobs), "jobs": [j.summary() for j in jobs]}, ensure_ascii=False)
