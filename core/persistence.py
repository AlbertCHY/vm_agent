"""持久化：LangGraph checkpointer (SQLite) 與輕量的對話 / 流程 Session 紀錄

- checkpoints.sqlite：LangGraph 的 SqliteSaver，Supervisor 與其內嵌子圖共用，
  中斷 (interrupt) 等待審核或程式異常結束後，都能以相同 thread_id 繼續。
- app.sqlite：
    * sessions           — 對話 Session 清單 (供 /pending 列出可續跑的流程)
    * pipeline_sessions  — 子圖跨次呼叫需延續的資料 (例如 VM 開立補問欄位時暫存已解析規格)
"""
import json
import sqlite3
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import config


@lru_cache(maxsize=1)
def get_checkpointer():
    from langgraph.checkpoint.sqlite import SqliteSaver

    config.CHECKPOINT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.CHECKPOINT_DB_PATH), check_same_thread=False)
    return SqliteSaver(conn)


class AppStore:
    """應用程式自用的 SQLite 小型資料表 (執行緒安全)"""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    thread_id  TEXT PRIMARY KEY,
                    title      TEXT,
                    kind       TEXT,
                    created_at REAL,
                    updated_at REAL
                );
                CREATE TABLE IF NOT EXISTS pipeline_sessions (
                    session_id TEXT PRIMARY KEY,
                    kind       TEXT,
                    data       TEXT,
                    updated_at REAL
                );
                """
            )
            self._conn.commit()

    # ─── 對話 Session ───
    def touch_session(self, thread_id: str, title: str, kind: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(thread_id, title, kind, created_at, updated_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(thread_id) DO UPDATE SET updated_at=excluded.updated_at",
                (thread_id, title[:120], kind, now, now),
            )
            self._conn.commit()

    def list_sessions(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT thread_id, title, kind, created_at, updated_at FROM sessions "
                "ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        keys = ("thread_id", "title", "kind", "created_at", "updated_at")
        return [dict(zip(keys, r)) for r in rows]

    # ─── 子圖延續資料 ───
    def save_pipeline(self, session_id: str, kind: str, data: dict[str, Any]) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO pipeline_sessions(session_id, kind, data, updated_at) VALUES (?,?,?,?) "
                "ON CONFLICT(session_id) DO UPDATE SET data=excluded.data, updated_at=excluded.updated_at",
                (session_id, kind, json.dumps(data, ensure_ascii=False, default=str), time.time()),
            )
            self._conn.commit()

    def load_pipeline(self, session_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT data FROM pipeline_sessions WHERE session_id=?", (session_id,)
            ).fetchone()
        return json.loads(row[0]) if row else None

    def delete_pipeline(self, session_id: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM pipeline_sessions WHERE session_id=?", (session_id,))
            self._conn.commit()


@lru_cache(maxsize=1)
def get_app_store() -> AppStore:
    return AppStore(config.DATA_DIR / "app.sqlite")
