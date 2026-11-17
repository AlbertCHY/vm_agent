# 純 LangGraph 驅動之 VM 生命週期架構重構說明 (`VMLifecycleGraph`)

本專案已將原本額外維護的外部狀態機（`JobStore`、`JobWatcher`、`core/lifecycle.py`）重構為 **純 LangGraph 原生機制（`StateGraph` + `SqliteSaver` Checkpointer + Node 邊界切分 + `interrupt`）**。

---

## 1. 核心設計決策總覽

1. **統一的非同步 API 背景等待模式（支援未來擴充其他 API 申請）**：
   - 所有耗時的非同步 API（① VM 開機建立、② Day-2 硬碟擴充/加掛，以及未來的其他 API 申請）皆統一採用 **「前景審核與送出 ➔ 釋放前景 ➔ 背景執行緒跑 `wait_*` 節點 ➔ 抵達下一個 `*_review` 的 `interrupt()` 自動停下」** 的標準模式。
2. **輪詢與審核嚴格拆分為不同 Node（100% 避免 Resume 重跑掃描）**：
   - `wait_vm_ready` / `wait_disk_step` 節點只做狀態查詢，完成時 `return` 更新 State 寫入 Checkpoint。
   - `disk_review` / `bootstrap_review` 節點一進入即呼叫 `interrupt()`。Resume 時只從審核節點開頭執行，絕不重跑前面的等待節點。
3. **`wait_disk_step` 採用「單一硬碟動作 Checkpoint 迴圈」**：
   - 每完成一顆硬碟動作就先 `return` 寫入 Checkpoint，再由條件邊 `route_disk_step` 檢查是否還有下一顆硬碟待處理，防止中途斷電重啟時重複送出已完成的硬碟 API。
4. **CLI 指令收斂（保留 `/jobs`、移除 `/pending`）**：
   - **`/jobs`**：專職管理所有已送出申請、正在背景等待或等待後續階段審核（硬碟調整、初始設定）的 `VMLifecycleGraph` 實例。
   - **移除 `/pending`**：若使用者在主對話的審核中途按 `Ctrl+C` 跳出，下次輸入前或重啟 CLI 時自動詢問 `「⏸️ 偵測到上一次有未完成的審核，是否繼續審核？[y/n]」`（選 `n` 則自動以 `skip` 結案該次工具呼叫，避免對話卡在未完成的 `ToolCall`）。

---

## 2. 主要模組結構

- [subgraphs/vm_lifecycle/state.py](file:///c:/Users/Albert/Desktop/vm_agent/subgraphs/vm_lifecycle/state.py)：定義 `VMLifecycleState` 與 `LifecycleJob` 視圖。
- [subgraphs/vm_lifecycle/nodes.py](file:///c:/Users/Albert/Desktop/vm_agent/subgraphs/vm_lifecycle/nodes.py)：定義 `init_lifecycle`、`wait_vm_ready`、`disk_locate`、`disk_review`、`disk_finalize`、`wait_disk_step`、`lifecycle_prepare_bootstrap`、`bootstrap_review`、`bootstrap_finalize` 等節點。
- [subgraphs/vm_lifecycle/graph.py](file:///c:/Users/Albert/Desktop/vm_agent/subgraphs/vm_lifecycle/graph.py)：組裝並編譯 `VMLifecycleGraph`（設定 `interrupt_after=["disk_finalize"]`）。
- [subgraphs/vm_lifecycle/runner.py](file:///c:/Users/Albert/Desktop/vm_agent/subgraphs/vm_lifecycle/runner.py)：管理 `start_lifecycle_job`、`spawn_background_continuation`、從 Checkpoint 推導階段的 `get_lifecycle_job` / `list_lifecycle_jobs`，以及 CLI 啟動恢復 `resume_waiting_lifecycles`。
- [cli/jobs.py](file:///c:/Users/Albert/Desktop/vm_agent/cli/jobs.py)：`/jobs` 介面，直接對 `VMLifecycleGraph` 執行 `Command(resume=...)`。
- [cli/session.py](file:///c:/Users/Albert/Desktop/vm_agent/cli/session.py)：前景對話回合與未完成 `interrupt` 的自動詢問續跑/取消機制。
