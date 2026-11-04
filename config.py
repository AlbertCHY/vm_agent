"""全域設定模組

集中管理所有環境變數 (Aria 憑證、Gemini、Ollama、SSH Key、Tickets 路徑) 與 prompts 載入。

設計原則：本模組的值只在程式啟動時從 .env 讀取一次，執行期間「不得修改」。
需要依單次呼叫而變化的參數 (例如 thread_id) 請改走 LangGraph 的 RunnableConfig。
"""
import os
import tomllib
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# 專案根目錄
BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("true", "1", "yes", "y", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_path(name: str, default: Path) -> Path:
    raw = os.getenv(name)
    if not raw:
        return default
    p = Path(os.path.expandvars(os.path.expanduser(raw)))
    return p if p.is_absolute() else (BASE_DIR / p)


# ─── Gemini (Supervisor 與 VM Provisioning 子圖使用) ───
GOOGLE_API_KEY: str = os.getenv("GOOGLE_API_KEY", "")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")
# Debug 用：顯示 Agent 思考過程與子圖步驟進度 (啟動時決定，執行中不切換)
SHOW_AGENT_THOUGHTS: bool = _env_bool("SHOW_AGENT_THOUGHTS", False)

# ─── VMware Aria Automation ───
AUTOMATION_URL: str = os.getenv("AUTOMATION_URL", "").rstrip("/")
AUTOMATION_API_TOKEN: str = os.getenv("AUTOMATION_API_TOKEN", "")
AUTOMATION_VERIFY_SSL: bool = _env_bool("AUTOMATION_VERIFY_SSL", False)
AUTOMATION_HTTP_TIMEOUT: int = _env_int("AUTOMATION_HTTP_TIMEOUT", 15)
AUTOMATION_HTTP_RETRIES: int = _env_int("AUTOMATION_HTTP_RETRIES", 3)

# ─── 本地 Ollama (工單解析) ───
OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "gemma3:1b")
OLLAMA_TIMEOUT: int = _env_int("OLLAMA_TIMEOUT", 300)
# 工單原文 + 系統提示可能超過 Ollama 預設 context (2048/4096)，需放大避免被截斷
OLLAMA_NUM_CTX: int = _env_int("OLLAMA_NUM_CTX", 8192)
OLLAMA_MAX_RAW_CHARS: int = _env_int("OLLAMA_MAX_RAW_CHARS", 6000)

# ─── SSH (VM 診斷與新機初始化設定) ───
SSH_PRIVATE_KEY_PATH: Path = _env_path("SSH_PRIVATE_KEY_PATH", Path.home() / ".ssh" / "id_rsa")
SSH_PUBLIC_KEY_PATH: Path = _env_path("SSH_PUBLIC_KEY_PATH", Path.home() / ".ssh" / "id_rsa.pub")
SSH_PUBLIC_KEY: str = os.getenv("SSH_PUBLIC_KEY", "").strip()
SSH_KEY_PASSPHRASE: str | None = os.getenv("SSH_KEY_PASSPHRASE") or None
DEFAULT_SSH_USER: str = os.getenv("DEFAULT_SSH_USER", "root")
SSH_PORT: int = _env_int("SSH_PORT", 22)
SSH_TIMEOUT: int = _env_int("SSH_TIMEOUT", 10)
# true: 只連線 known_hosts 中已存在的主機；false: 自動接受新主機金鑰 (內網維運常見設定)
SSH_STRICT_HOST_KEY: bool = _env_bool("SSH_STRICT_HOST_KEY", False)

# ─── 新 VM 初始化基本設定 (VM Bootstrap) ───
BOOTSTRAP_INIT_USER: str = os.getenv("BOOTSTRAP_INIT_USER", "user1")
BOOTSTRAP_INIT_PASSWORD: str = os.getenv("BOOTSTRAP_INIT_PASSWORD", "")
BOOTSTRAP_USER1: str = os.getenv("BOOTSTRAP_USER1", "user1")
BOOTSTRAP_USER2: str = os.getenv("BOOTSTRAP_USER2", "user2")
BOOTSTRAP_PASSWORD_LENGTH: int = _env_int("BOOTSTRAP_PASSWORD_LENGTH", 12)
CS_PROXY_HOST: str = os.getenv("CS_PROXY_HOST", "")
CS_PROXY_PORT: str = os.getenv("CS_PROXY_PORT", "")
MDR_CID: str = os.getenv("MDR_CID", "")
MDR_TOKEN: str = os.getenv("MDR_TOKEN", "")
NGAV_CID: str = os.getenv("NGAV_CID", "")
NGAV_TOKEN: str = os.getenv("NGAV_TOKEN", "")
CROWDSTRIKE_DEFAULT_MODE: str = os.getenv("CROWDSTRIKE_DEFAULT_MODE", "NGAV").upper()
ZABBIX_SERVERS: str = os.getenv("ZABBIX_SERVERS", "")
ZABBIX_CONF_PATH: str = os.getenv("ZABBIX_CONF_PATH", "/etc/zabbix/zabbix_agentd.conf")
SCRIPTS_DIR: Path = _env_path("SCRIPTS_DIR", BASE_DIR / "scripts")
BOOTSTRAP_SCRIPTS_DIR: Path = _env_path("BOOTSTRAP_SCRIPTS_DIR", SCRIPTS_DIR / "bootstrap")
DIAG_SCRIPTS_DIR: Path = _env_path("DIAG_SCRIPTS_DIR", SCRIPTS_DIR / "diag")
CROWDSTRIKE_LOCAL_DIR: Path = _env_path("CROWDSTRIKE_LOCAL_DIR", BOOTSTRAP_SCRIPTS_DIR / "CrowdStrike")
BOOTSTRAP_LOG_DIR: Path = _env_path("BOOTSTRAP_LOG_DIR", BASE_DIR / "logs")

# ─── 工單資料夾 ───
TICKETS_DIR: Path = _env_path("TICKETS_DIR", BASE_DIR / "tickets")
TICKET_EXTENSIONS: tuple[str, ...] = (".txt", ".md")

# ─── 執行期資料 (checkpoint 資料庫) 與日誌 ───
DATA_DIR: Path = _env_path("DATA_DIR", BASE_DIR / "data")
CHECKPOINT_DB_PATH: Path = _env_path("CHECKPOINT_DB_PATH", DATA_DIR / "checkpoints.sqlite")
LOG_DIR: Path = _env_path("LOG_DIR", BASE_DIR / "logs")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ─── Policies (業務規則 TOML) ───
POLICIES_DIR: Path = _env_path("POLICIES_DIR", BASE_DIR / "policies")


# ─── Prompts ───
def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """遞迴合併：override 中的 key 覆蓋 base (dict 會往下合併，其餘型別直接取代)"""
    merged = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = _deep_merge(merged[k], v)
        else:
            merged[k] = v
    return merged


def _load_toml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "rb") as f:
        return tomllib.load(f)


PROMPTS_PATH: Path = BASE_DIR / "prompts.toml"
# 含內部資訊的覆蓋版 (不進版控)，以相同 [section].key 覆蓋 prompts.toml
PROMPTS_LOCAL_PATH: Path = BASE_DIR / "prompts.local.toml"


def _load_prompts() -> dict[str, Any]:
    return _deep_merge(_load_toml(PROMPTS_PATH), _load_toml(PROMPTS_LOCAL_PATH))


PROMPTS: dict[str, Any] = _load_prompts()
