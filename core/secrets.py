"""機密資料處理

- SecretStore：密碼只存在程式記憶體中，graph state / checkpoint 只保存參照 ID (ref)。
  程式重啟後參照會失效，流程需重新產生密碼。介面保留未來替換為 Vault 等外部儲存的空間。
- CredentialSink：新密碼的留存方式。目前為本機明文紀錄檔 (個人助手使用情境)，
  未來可替換為加密檔或公司 PAM 系統而不需改動流程程式碼。
- AuditSink：操作稽核介面，目前為不記錄的空實作，保留未來擴充。
"""
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Optional, Protocol


class SecretStore(Protocol):
    def put(self, values: dict[str, str], ref: Optional[str] = None) -> str: ...
    def get(self, ref: Optional[str]) -> Optional[dict[str, str]]: ...
    def has(self, ref: Optional[str]) -> bool: ...
    def delete(self, ref: Optional[str]) -> None: ...


class InMemorySecretStore:
    """行程內的機密儲存 (執行緒安全)"""

    def __init__(self):
        self._data: dict[str, dict[str, str]] = {}
        self._lock = threading.Lock()

    def put(self, values: dict[str, str], ref: Optional[str] = None) -> str:
        ref = ref or f"secret-{uuid.uuid4().hex}"
        with self._lock:
            self._data[ref] = dict(values)
        return ref

    def get(self, ref: Optional[str]) -> Optional[dict[str, str]]:
        if not ref:
            return None
        with self._lock:
            val = self._data.get(ref)
            return dict(val) if val is not None else None

    def has(self, ref: Optional[str]) -> bool:
        return self.get(ref) is not None

    def delete(self, ref: Optional[str]) -> None:
        if ref:
            with self._lock:
                self._data.pop(ref, None)


_secret_store: SecretStore = InMemorySecretStore()


def get_secret_store() -> SecretStore:
    return _secret_store


def set_secret_store(store: SecretStore) -> None:
    global _secret_store
    _secret_store = store


# ─────────────────────────── 密碼留存 ───────────────────────────

class CredentialSink(Protocol):
    def record(self, ip: str, hostname: str, accounts: dict[str, str]) -> str:
        """留存新密碼，回傳給使用者看的留存位置描述"""
        ...


class PlaintextFileCredentialSink:
    """附加寫入本機明文紀錄檔 (預設 logs/bootstrap_credentials.log)"""

    def __init__(self, path: Path):
        self.path = path

    def record(self, ip: str, hostname: str, accounts: dict[str, str]) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"[{ts}] IP={ip} | Hostname={hostname}"]
        lines += [f"  {acct:<13} : {pw}" for acct, pw in accounts.items()]
        lines.append("-" * 60)
        with open(self.path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        return str(self.path)


_credential_sink_factory: Optional[Callable[[], CredentialSink]] = None


def get_credential_sink() -> CredentialSink:
    if _credential_sink_factory is not None:
        return _credential_sink_factory()
    import config
    return PlaintextFileCredentialSink(config.BOOTSTRAP_LOG_DIR / "bootstrap_credentials.log")


def set_credential_sink_factory(factory: Optional[Callable[[], CredentialSink]]) -> None:
    global _credential_sink_factory
    _credential_sink_factory = factory


# ─────────────────────────── 操作稽核 ───────────────────────────

class AuditSink(Protocol):
    def record(self, event: str, **details: Any) -> None: ...


class NullAuditSink:
    """預設不記錄；未來可替換為 JSON Lines 檔或資料庫"""

    def record(self, event: str, **details: Any) -> None:
        return None


_audit_sink: AuditSink = NullAuditSink()


def get_audit_sink() -> AuditSink:
    return _audit_sink


def set_audit_sink(sink: AuditSink) -> None:
    global _audit_sink
    _audit_sink = sink
