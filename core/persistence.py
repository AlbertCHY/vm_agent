"""持久化：LangGraph checkpointer (SQLite)、單一實例鎖與輕量的對話 / 流程 Session 紀錄

- checkpoints.sqlite：LangGraph 的 SqliteSaver，Supervisor 與所有業務子圖 (含背景工作子圖) 共用，
  中斷 (interrupt) 等待審核或程式異常結束後，都能以相同 thread_id 繼續。
- app.sqlite：
    * sessions           — 前景對話 Session 索引 (thread_id、標題、類型、封存狀態)
    * pipeline_sessions  — 子圖跨次呼叫需延續的資料 (例如 VM 開立補問欄位時暫存已解析規格)
    * background_jobs    — 統一背景工作 (/jobs) 索引、串接 next_steps 與 stage 快取
"""
import json
import os
import sqlite3
import threading
import time
from contextlib import nullcontext
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import config

_LOCK_FILE_HANDLE = None


def acquire_background_lock() -> bool:
    """
    取得跨行程背景輪詢鎖 (data/watcher.lock)。
    防止同時開啟多個 CLI 視窗時重複啟動背景輪詢或重複送出硬碟動作；行程結束時由作業系統自動釋放。
    """
    global _LOCK_FILE_HANDLE
    if _LOCK_FILE_HANDLE is not None:
        return True
    lock_path = config.DATA_DIR / "watcher.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fh = open(lock_path, "a+b")
        fh.seek(0)
        if fh.tell() == 0:
            fh.write(b"0")
            fh.flush()
        fh.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        _LOCK_FILE_HANDLE = fh
        return True
    except OSError:
        return False


@lru_cache(maxsize=1)
def get_checkpointer():
    from langgraph.checkpoint.sqlite import SqliteSaver

    config.CHECKPOINT_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(config.CHECKPOINT_DB_PATH), check_same_thread=False)
    return SqliteSaver(conn)


def delete_thread_checkpoints(thread_id: str, checkpointer=None) -> None:
    """清理已封存 thread 在 checkpoints.sqlite 中的歷史紀錄"""
    cp = checkpointer or get_checkpointer()
    conn = getattr(cp, "conn", None)
    if conn is None:
        return
    try:
        with getattr(cp, "lock", None) or nullcontext():
            conn.execute("DELETE FROM checkpoints WHERE thread_id=?", (thread_id,))
            conn.execute("DELETE FROM writes WHERE thread_id=?", (thread_id,))
            conn.commit()
    except Exception:
        pass


_SESSION_KEYS = ("thread_id", "title", "kind", "archived", "created_at", "updated_at")
_JOB_KEYS = ("job_id", "thread_id", "kind", "title", "stage", "meta", "archived", "created_at", "updated_at")


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
                    archived   INTEGER DEFAULT 0,
                    created_at REAL,
                    updated_at REAL
                );
                CREATE TABLE IF NOT EXISTS pipeline_sessions (
                    session_id TEXT PRIMARY KEY,
                    kind       TEXT,
                    data       TEXT,
                    updated_at REAL
                );
                CREATE TABLE IF NOT EXISTS background_jobs (
                    job_id     TEXT PRIMARY KEY,
                    thread_id  TEXT NOT NULL,
                    kind       TEXT NOT NULL,
                    title      TEXT,
                    stage      TEXT DEFAULT '',
                    meta       TEXT,
                    archived   INTEGER DEFAULT 0,
                    created_at REAL,
                    updated_at REAL
                );
                """
            )
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(background_jobs)").fetchall()}
            if "stage" not in cols:
                self._conn.execute("ALTER TABLE background_jobs ADD COLUMN stage TEXT DEFAULT ''")
            self._conn.commit()

    # ─── 對話 Session ───
    def touch_session(self, thread_id: str, title: str, kind: str) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                "INSERT INTO sessions(thread_id, title, kind, archived, created_at, updated_at) VALUES (?,?,?,0,?,?) "
                "ON CONFLICT(thread_id) DO UPDATE SET updated_at=excluded.updated_at, "
                "title= CASE WHEN excluded.title != '' THEN excluded.title ELSE sessions.title END",
                (thread_id, (title or "")[:120], kind, now, now),
            )
            self._conn.commit()

    def get_session(self, thread_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT thread_id, title, kind, archived, created_at, updated_at FROM sessions WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
        if not row:
            return None
        return dict(zip(_SESSION_KEYS, row))

    def archive_session(self, thread_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE sessions SET archived=1, updated_at=? WHERE thread_id=?",
                (time.time(), thread_id),
            )
            self._conn.commit()

    def list_sessions(
        self,
        limit: int = 50,
        kind: Optional[str] = None,
        exclude_kind: Optional[str] = None,
        include_archived: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT thread_id, title, kind, archived, created_at, updated_at FROM sessions WHERE 1=1"
        args: list[Any] = []
        if not include_archived:
            sql += " AND COALESCE(archived, 0) = 0"
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        if exclude_kind:
            sql += " AND kind != ?"
            args.append(exclude_kind)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [dict(zip(_SESSION_KEYS, r)) for r in rows]

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

    # ─── 統一背景工作索引 (/jobs) ───
    def save_job(
        self,
        job_id: str,
        thread_id: str,
        kind: str,
        title: str,
        meta: Optional[dict[str, Any]] = None,
        stage: str = "",
    ) -> None:
        now = time.time()
        meta_json = json.dumps(meta or {}, ensure_ascii=False, default=str)
        with self._lock:
            self._conn.execute(
                "INSERT INTO background_jobs(job_id, thread_id, kind, title, stage, meta, archived, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,0,?,?) "
                "ON CONFLICT(job_id) DO UPDATE SET title=excluded.title, meta=excluded.meta, "
                "stage=CASE WHEN excluded.stage != '' THEN excluded.stage ELSE background_jobs.stage END, "
                "updated_at=excluded.updated_at",
                (job_id, thread_id, kind, (title or "")[:120], stage or "", meta_json, now, now),
            )
            self._conn.commit()

    def update_job_stage(self, job_id: str, stage: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE background_jobs SET stage=?, updated_at=? WHERE job_id=?",
                (stage or "", time.time(), job_id),
            )
            self._conn.commit()

    def get_job(self, job_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            row = self._conn.execute(
                "SELECT job_id, thread_id, kind, title, stage, meta, archived, created_at, updated_at "
                "FROM background_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
        if not row:
            return None
        d = dict(zip(_JOB_KEYS, row))
        d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
        return d

    def update_job_meta(self, job_id: str, meta: dict[str, Any], title: Optional[str] = None) -> None:
        now = time.time()
        meta_json = json.dumps(meta, ensure_ascii=False, default=str)
        with self._lock:
            if title is not None:
                self._conn.execute(
                    "UPDATE background_jobs SET meta=?, title=?, updated_at=? WHERE job_id=?",
                    (meta_json, title[:120], now, job_id),
                )
            else:
                self._conn.execute(
                    "UPDATE background_jobs SET meta=?, updated_at=? WHERE job_id=?",
                    (meta_json, now, job_id),
                )
            self._conn.commit()

    def archive_job(self, job_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE background_jobs SET archived=1, updated_at=? WHERE job_id=?",
                (time.time(), job_id),
            )
            self._conn.commit()

    def has_uncached_job_stages(self) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM background_jobs WHERE COALESCE(archived, 0) = 0 AND COALESCE(stage, '') = '' LIMIT 1"
            ).fetchone()
        return row is not None

    def count_jobs_by_stages(self, stages: tuple[str, ...]) -> int:
        if not stages:
            return 0
        placeholders = ",".join("?" for _ in stages)
        sql = (
            f"SELECT COUNT(*) FROM background_jobs "
            f"WHERE COALESCE(archived, 0) = 0 AND stage IN ({placeholders})"
        )
        with self._lock:
            row = self._conn.execute(sql, list(stages)).fetchone()
        return int(row[0]) if row else 0

    def list_jobs(self, limit: int = 200, include_archived: bool = False) -> list[dict[str, Any]]:
        sql = (
            "SELECT job_id, thread_id, kind, title, stage, meta, archived, created_at, updated_at "
            "FROM background_jobs WHERE 1=1"
        )
        args: list[Any] = []
        if not include_archived:
            sql += " AND COALESCE(archived, 0) = 0"
        sql += " ORDER BY created_at ASC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(zip(_JOB_KEYS, r))
            d["meta"] = json.loads(d["meta"]) if d.get("meta") else {}
            out.append(d)
        return out


@lru_cache(maxsize=1)
def get_app_store() -> AppStore:
    return AppStore(config.DATA_DIR / "app.sqlite")
