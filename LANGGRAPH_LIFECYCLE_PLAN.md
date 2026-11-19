# 純 LangGraph 驅動之 VM 生命週期架構重構說明 (`VMLifecycleGraph`)

本專案已將原本額外維護的外部狀態機（`JobStore`、`JobWatcher`、`core/lifecycle.py`）重構為 **純 LangGraph 原生機制（`StateGraph` + `SqliteSaver` Checkpointer + Node 邊界切分 + `interrupt`）**，並將前景同步與背景非同步子圖執行器統一整合於 `subgraphs/common/runner.py`。

---

## 1. 核心設計決策總覽

1. **統一的非同步 API 背景等待模式（支援未來擴充其他 API 申請）**：
   - 所有耗時的非同步 API（① VM 開機建立、② Day-2 硬碟擴充/加掛，以及未來的其他 API 申請）皆統一採用 **「前景審核與送出 ➔ 釋放前景 ➔ 背景執行緒跑 `wait_*` 節點 ➔ 抵達下一個 `*_review` 的 `interrupt()` 自動停下」** 的標準模式。
2. **輪詢與審核嚴格拆分為不同 Node（100% 避免 Resume 重跑掃描）**：
   - `wait_vm_ready` / `wait_disk_op` 節點只做狀態查詢，完成時 `return` 更新 State 寫入 Checkpoint。
   - `disk_review` / `bootstrap_review` 節點一進入即呼叫 `interrupt()`。Resume 時只從審核節點開頭執行，絕不重跑前面的等待節點。
3. **硬碟動作拆分為 `submit_disk_op` 與 `wait_disk_op` 雙節點 + 冪等防呆**：
   - `submit_disk_op` 送出單一硬碟申請拿到 `request_id` 後立即 `return` 寫入 Checkpoint，並於送出前檢查磁碟資源是否已存在；`wait_disk_op` 只根據 Checkpoint 中的 `request_id` 輪詢，重啟後絕不重複送出硬碟申請。
4. **初始設定（Bootstrap）中斷後直接從斷點步驟續跑（`BOOTSTRAP_RUNNING`）**：
   - 若在執行步驟中途按 `Ctrl+C` 中斷，下次進 `/jobs` 會直接以 `stream(None)` 從斷點 `step_index` 續跑，不重設也不重跑已完成步驟。
5. **CLI 指令收斂（保留 `/jobs`、移除 `/pending` 與 `/batch`）**：
   - **`/jobs`**：專職管理所有已送出申請、正在背景等待或等待後續階段審核/續跑的 `VMLifecycleGraph` 實例。
   - **移除 `/pending`**：若使用者在主對話的審核中途按 `Ctrl+C` 跳出，下次輸入前或重啟 CLI 時自動詢問 `「⏸️ 偵測到上一次有未完成的審核，是否繼續審核？[y/n]」`。

---

## 2. 主要模組結構

- [subgraphs/common/runner.py](file:///c:/Users/Albert/Desktop/vm_agent/subgraphs/common/runner.py)：統一的子圖執行器，同時提供前景同步執行（`run_pipeline`）與背景非同步執行（`spawn_background_graph`、Per-Task `RLock`、通知佇列、輪詢喚醒與即時進度快取）。
- [subgraphs/vm_lifecycle/state.py](file:///c:/Users/Albert/Desktop/vm_agent/subgraphs/vm_lifecycle/state.py)：定義 `VMLifecycleState` 與 `LifecycleJob` 視圖。
- [subgraphs/vm_lifecycle/nodes.py](file:///c:/Users/Albert/Desktop/vm_agent/subgraphs/vm_lifecycle/nodes.py)：定義 `init_lifecycle`、`wait_vm_ready`、`disk_locate`、`disk_review`、`disk_finalize`、`submit_disk_op`、`wait_disk_op`、`lifecycle_prepare_bootstrap`、`bootstrap_review`、`bootstrap_finalize` 等節點。
- [subgraphs/vm_lifecycle/graph.py](file:///c:/Users/Albert/Desktop/vm_agent/subgraphs/vm_lifecycle/graph.py)：組裝並編譯 `VMLifecycleGraph`（設定 `interrupt_after=["disk_finalize"]`），並提供 `start_lifecycle_job`、`get_lifecycle_job`、`list_lifecycle_jobs` 與 CLI 啟動恢復 `resume_waiting_lifecycles`。
- [cli/jobs.py](file:///c:/Users/Albert/Desktop/vm_agent/cli/jobs.py)：`/jobs` 介面，直接對 `VMLifecycleGraph` 執行 `Command(resume=...)` 或斷點續跑。
- [cli/session.py](file:///c:/Users/Albert/Desktop/vm_agent/cli/session.py)：前景對話回合與未完成 `interrupt` 的自動詢問續跑/取消機制。
