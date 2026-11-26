# Aria Automation Cloud Ops Agent

基於 **LangGraph + LangChain + Gemini + Local Ollama + Paramiko SSH** 打造的企業私有雲維運助手，專注於 **VMware Aria Automation** 查詢、Linux VM 故障診斷、IT 工單解析，以及具備人工審核（HITL）與斷點續跑能力的 VM 全生命週期自動化作業。

---

## 核心功能

| 模組 | 能力說明 |
|---|---|
| **Aria 唯讀查詢** | 依 IP 反查 VM 規格與同 Deployment 關聯資源（`query_vm_by_ip`）、列出專案下所有 Deployments（`list_project_deployments`） |
| **Linux VM 故障診斷** | 透過 SSH + 免密 `sudo -n`（失敗自動降級）執行唯讀腳本，涵蓋系統健康與磁碟、帳號認證、SFTP 傳檔、GUI/XRDP、網路/DNS/防火牆與細部設定複查，內建機密遮罩與敏感檔案攔截 |
| **本地 Ollama 工單解析** | 掃描 `tickets/` 或以 `/paste` 貼上工單純文字，經表單預處理器擷取欄位提示後由本地 Ollama 判斷工單類型（`CREATE_VM` 等）並拆解 `action_items` |
| **VM 開立申請（Provisioning）** | 動態讀取 Aria Service Broker Catalog Schema 與 Custom Form，結合 LLM 意圖抽取與確定性規則（`policies/vm_defaults.toml`），經人工審核後送出申請 |
| **Day-2 硬碟調整（Disk）** | 支援擴充系統碟（Resize Boot Disk）與加掛額外硬碟（Add Disk），具備人工審核、單動作 Checkpoint 保護與 Add Disk 冪等防呆 |
| **新機初始化（Bootstrap）** | 支援 Linux（`policies/bootstrap_steps_linux.toml`）與 Windows OpenSSH（`policies/bootstrap_steps_windows.toml`）：SSH 公鑰、Hostname、強密碼輪替、Zabbix、CrowdStrike、系統碟擴容與額外硬碟掛載；Linux 含免密 sudo 與清理，Windows 含 DNS、排程清理、SCCM、系統檢查與完成後重開機 |
| **VM 生命週期背景追蹤（`/jobs`）** | VM 申請送出後自動啟動 `VMLifecycleGraph`：背景等待 Aria 審核與建機 ➔ 抵達「② 硬碟調整」審核 ➔ 背景執行硬碟調整 ➔ 抵達「③ 初始設定」審核與斷點續跑 |

---

## 系統架構

```text
[CLI 介面層] app.py / cli/{session,jobs,review,render}.py
     │
     ├── 前景對話 (run_turn) ────────► [頂層調度層] supervisor/agent.py (Gemini ReAct Agent)
     │                                      │
     │                                      ├── 唯讀查詢與診斷 Tools (tools/aria_query_tools.py, vm_diag_tools.py)
     │                                      ├── 工單解析 Tool (tools/ticket_tools.py ──► parsers/ + Ollama)
     │                                      ├── 追蹤進度查詢 Tool (tools/job_tools.py)
     │                                      └── 變更流程子圖 Tools (透過 subgraphs/common/runner.py 呼叫)
     │                                             ├─► vm_provisioning (開機申請 ──► 送出後啟動 vm_lifecycle)
     │                                             ├─► vm_disk         (現有機器單獨調整硬碟)
     │                                             └─► vm_bootstrap    (現有機器單獨跑初始設定)
     │
     └── 背景追蹤 (/jobs) ───────────► [生命週期子圖] subgraphs/vm_lifecycle/ (VMLifecycleGraph)
                                            └── wait_vm_ready (背景) ➔ disk_review (HITL)
                                                ➔ submit_disk_op / wait_disk_op (背景)
                                                ➔ bootstrap_review (HITL) ➔ connect/run_step/cleanup (前景逐步)
```

### 專案目錄結構

```text
vm_agent/
├── app.py                        # 互動式 CLI 主程式入口
├── config.py                     # 全域環境變數與 prompts 載入 (啟動時唯讀)
├── schemas.py                    # 跨模組領域模型 (ParsedTicket)
├── prompts.toml                  # 公開版 Prompt 設定 (可用 prompts.local.toml 覆蓋)
├── cli/                          # Rich CLI 介面層 (不含業務邏輯)
│   ├── session.py                #   前景對話串流、interrupt 恢復、中斷自動續跑
│   ├── jobs.py                   #   /jobs 生命週期追蹤管理介面
│   ├── review.py                 #   通用人工審核 (HITL) 畫面
│   └── render.py                 #   訊息、思考過程、工具與工單表格渲染
├── supervisor/
│   └── agent.py                  # ReAct Supervisor Agent 與工具註冊
├── tools/                        # Supervisor 標準工具集
│   ├── aria_query_tools.py       #   Aria 唯讀查詢
│   ├── vm_diag_tools.py          #   Linux VM 6 項唯讀診斷工具
│   ├── ticket_tools.py           #   工單掃描與快取呼叫
│   └── job_tools.py              #   track_vm_jobs 進度查詢
├── parsers/                      # 工單解析模組
│   ├── ticket_form.py            #   申請單純文字正規表示式預處理器
│   └── ticket_parser.py          #   結合預處理與 Ollama 的通用工單解析引擎
├── subgraphs/                    # LangGraph 狀態子圖
│   ├── common/
│   │   ├── review.py             #   通用 HITL 協定 (ReviewRequest / ask_review)
│   │   └── runner.py             #   前景同步與背景非同步子圖執行器、通用輪詢骨架
│   ├── vm_provisioning/          #   VM 開立申請子圖 (state/reasoning/resolver/nodes/graph/tool)
│   ├── vm_disk/                  #   Day-2 硬碟調整子圖 (state/executor/nodes/graph/tool)
│   ├── vm_bootstrap/             #   Linux / Windows 新機初始化子圖 (state/steps/nodes/graph/tool)
│   └── vm_lifecycle/             #   VM 全生命週期子圖 (state/nodes/graph/service)
├── clients/                      # 無狀態底層連線 SDK
│   ├── aria/                     #   Aria REST API (session/catalog/forms/iaas/deployment/vro/inspect)
│   ├── ssh_client.py             #   Paramiko SSH/SFTP (Linux/Windows OpenSSH、PAM 改密碼、機密暫存檔注入)
│   ├── ollama_client.py          #   本地 Ollama REST API Client
│   └── llm.py                    #   Gemini Chat Model 工廠
├── core/                         # 基礎設施層
│   ├── persistence.py            #   SqliteSaver Checkpointer、AppStore、單一背景鎖 (watcher.lock)
│   ├── policy.py                 #   業務規則 TOML 載入器 (VMPolicy)
│   ├── secrets.py                #   InMemorySecretStore、密碼留存 Sink、LoggingAuditSink
│   ├── progress.py               #   LangGraph custom stream 進度事件
│   └── logging_setup.py          #   輪替日誌設定 (logs/app.log)
├── policies/                     # 業務規則設定 (TOML)
│   ├── vm_defaults.example.toml  #   VM 預設值、命名與網段規則範本 (複製為 vm_defaults.toml 使用)
│   ├── bootstrap_steps_linux.toml   # Linux 新機初始化步驟宣告
│   └── bootstrap_steps_windows.toml # Windows 新機初始化步驟宣告
├── scripts/                      # 遠端執行腳本與輔助工具
│   ├── bootstrap/                #   新機初始化腳本 (linux/*.sh 與 windows/*.ps1)
│   ├── installers/               #   大型安裝檔目錄 (不進版控：CrowdStrike/{Ubuntu,RHEL_8,RHEL_9,Windows}、SCCM/)
│   ├── diag/                     #   Linux 唯讀診斷 Shell 腳本
│   └── passwd_creator.py         #   強密碼產生器
└── tests/                        # Pytest 單元與整合測試套件
```

---

## 核心設計與安全機制

1. **原生 LangGraph `interrupt()` HITL**：所有變更操作（開機、硬碟調整、初始設定）皆於子圖內透過 `ask_review()` 觸發 `interrupt()`，介面層只負責渲染與收集原始輸入，所有轉型與驗證均在子圖節點完成。
2. **純 LangGraph Checkpoint 與斷點續跑**：等待輪詢節點與審核節點嚴格分離，Resume 絕不重跑前置掃描；硬碟送出採單動作 `submit_disk_op → wait_disk_op` 分離設計並具 Add Disk 冪等防呆；Bootstrap 每步寫入 Checkpoint，中斷後可從斷點直接續跑。
3. **機密零外洩設計**：
   - 新機密碼僅存放於本機記憶體 `InMemorySecretStore`，LangGraph State、SQLite Checkpoint 與雲端 LLM 對話只保存參照 ID（`secret_ref`）。
   - 遠端執行時，密碼與 CrowdStrike Token 透過 SFTP 寫入權限 `0600` 的暫存檔載入後立即刪除，不出現在遠端 `ps` / `sudo` 命令列。
   - 診斷工具禁止讀取 `/etc/shadow` 與私鑰檔（本機與遠端 `realpath` 雙重攔截），並自動遮罩輸出中的明文密碼與 Token。
   - 關鍵變更操作由 `LoggingAuditSink` 寫入 `logs/app.log`（`aria.audit`）。

---

## 環境需求與快速開始

### 1. 環境需求
- **Python 3.12+**（專案使用標準庫 `tomllib`）
- **Google Gemini API Key**（Supervisor 與 VM Provisioning 使用）
- **VMware Aria Automation Refresh Token**
- **本地 Ollama**（選用，工單解析時使用）
- **SSH 私鑰 / 模板初始密碼**（Linux VM 診斷與新機初始化使用）

### 2. 安裝相依套件

```bash
# 建立虛擬環境
python -m venv .venv

# Windows (PowerShell)
.\.venv\Scripts\Activate.ps1
# Linux / macOS
# source .venv/bin/activate

# 安裝執行期相依
pip install -r requirements.txt

# 若需執行測試，一併安裝開發相依
pip install -r requirements-dev.txt
```

### 3. 設定環境變數與業務規則

1. 複製 `.env.example` 為 `.env` 並填入實際憑證與設定：
   ```bash
   cp .env.example .env
   ```
2. 複製 `policies/vm_defaults.example.toml` 為 `policies/vm_defaults.toml`（不進版控），填入貴單位的專案網段對應與預設值。
3. （選用）若需自訂內部 Prompt 或範例問句，建立 `prompts.local.toml`（不進版控），以相同 `[section].key` 覆蓋 `prompts.toml`。
4. （選用）若需自動安裝 CrowdStrike / SCCM，將安裝檔放入 `scripts/installers/CrowdStrike/`（`Ubuntu/`、`RHEL_8/`、`RHEL_9/`、`Windows/`）與 `scripts/installers/SCCM/`。

### 4. 啟動程式

```bash
python app.py
```

---

## CLI 指令與常用操作

| 指令 | 說明 |
|---|---|
| `/tickets` | 列出 `tickets/` 資料夾中的工單檔案（`.txt` / `.md`） |
| `/paste` | 多行貼上工單純文字（輸入 `END` 送出、`CANCEL` 取消），存檔後自動交由 Agent 解析與調度 |
| `/jobs` | 開啟 VM 追蹤工作清單，處理「② 硬碟調整」、「③ 初始設定」審核、斷點續跑、立即重查 `[C]` 或停止追蹤 `[X]` |
| `/new` | 開啟新的對話 Session |
| `/help` | 顯示指令說明與範例問句 |
| `exit` | 離開程式 |

### Aria Catalog / Form 檢查工具

開發或除錯表單欄位與 vRO Action 時，可直接執行內建 CLI 檢查工具：

```bash
# 列出指定專案可申請的 catalog templates
python -m clients.aria.inspect --project proj-demo

# 檢視特定模板的 catalog schema + Custom Form 合併欄位定義
python -m clients.aria.inspect --project proj-demo --item "Oracle"

# 測試內嵌 vRO action (實驗性)
python -m clients.aria.inspect --vro com.example.module/Project_vLAN_List
```

---

## 測試

測試設定於 `tests/conftest.py` 會自動將 `POLICIES_DIR`、`DATA_DIR`、`LOG_DIR` 導向暫存目錄，不會汙染本機資料庫或受內部 policy 影響。

```bash
python -m pytest -v
```

### 測試模組一覽
- `tests/test_aria_client.py`：Aria JWT 效期解析、401 自動重登、分頁防呆、Catalog 送出與 Custom Form 欄位合併
- `tests/test_provisioning_flow.py`：VM 開機申請完整流程（補問續跑、審核修改、快取、生命週期工作建立）
- `tests/test_bootstrap_flow.py`：新機初始化審核、密碼重產與重啟遺失恢復、機密零外洩驗證、斷點續跑
- `tests/test_jobs.py`：VM 生命週期背景輪詢、不重跑掃描、Add Disk 冪等與崩潰防重送、Bootstrap 中斷續跑、前景 `adjust_vm_disk`、Per-Task 喚醒與資源釋放
- `tests/test_cli.py`：通用審核畫面渲染、對話回合與中斷自動詢問續跑
- `tests/test_policy.py`：Policy fallback 載入、網段與命名規則、`bootstrap_steps_{linux,windows}.toml` 機密隔離檢查
- `tests/test_ticket_form.py`：申請單正規表示式預處理、`parsers.ticket_parser.parse_ticket` 補值、路徑遍歷防呆與快取驗證
- `tests/test_security_helpers.py`：審核輸入解析、SSH `secret_env` 與 PAM 登入降級、診斷 `sudo` 降級、機密遮罩與敏感檔案攔截
- `tests/test_persistence.py`：`AppStore` Session / Pipeline 存取篩選與 `delete_thread_checkpoints` 清理驗證
