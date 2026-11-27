"""持久化層測試：AppStore (sessions / pipeline_sessions / background_jobs) 與 delete_thread_checkpoints"""
import sqlite3
import threading

from core.persistence import AppStore, delete_thread_checkpoints


def test_app_store_sessions_and_filtering(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")

    store.touch_session("chat-1", "原始標題", kind="chat")
    # 空標題更新不應覆蓋原本標題
    store.touch_session("chat-1", "", kind="chat")
    assert store.get_session("chat-1")["title"] == "原始標題"

    store.touch_session("vmjob-1", "proj-demo-AP01", kind="other")
    store.touch_session("vmjob-2", "proj-demo-AP02", kind="other")
    store.archive_session("vmjob-2")

    # 預設排除已封存
    lc = store.list_sessions(kind="other")
    assert [s["thread_id"] for s in lc] == ["vmjob-1"]

    # include_archived=True 包含已封存
    lc_all = store.list_sessions(kind="other", include_archived=True)
    assert {s["thread_id"] for s in lc_all} == {"vmjob-1", "vmjob-2"}

    # exclude_kind 排除指定類型 session
    chats = store.list_sessions(exclude_kind="other")
    assert [s["thread_id"] for s in chats] == ["chat-1"]


def test_app_store_pipeline_crud(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    assert store.load_pipeline("sid-1") is None

    store.save_pipeline("sid-1", "vm_provisioning", {"intent": {"cpu": 4}})
    assert store.load_pipeline("sid-1") == {"intent": {"cpu": 4}}

    store.save_pipeline("sid-1", "vm_provisioning", {"intent": {"cpu": 8}})
    assert store.load_pipeline("sid-1") == {"intent": {"cpu": 8}}

    store.delete_pipeline("sid-1")
    assert store.load_pipeline("sid-1") is None


def test_app_store_background_jobs_crud(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")
    store.save_job("j1", "job-j1", "vm_build", "vm-01", meta={"next_steps": [{"kind": "vm_bootstrap"}]}, stage="waiting")
    store.save_job("j2", "job-j2", "vm_disk", "vm-02", meta={"next_steps": []}, stage="review")
    assert store.get_job("j1")["meta"]["next_steps"] == [{"kind": "vm_bootstrap"}]
    assert store.get_job("j1")["stage"] == "waiting"
    assert store.has_uncached_job_stages() is False
    assert store.count_jobs_by_stages(("review", "retry")) == 1

    store.update_job_stage("j1", "review")
    assert store.count_jobs_by_stages(("review", "retry")) == 2

    store.archive_job("j2")
    assert store.count_jobs_by_stages(("review", "retry")) == 1
    assert [r["job_id"] for r in store.list_jobs()] == ["j1"]
    assert [r["job_id"] for r in store.list_jobs(include_archived=True)] == ["j1", "j2"]


def test_app_store_migrates_legacy_background_jobs_without_stage_column(tmp_path):
    db_path = tmp_path / "legacy_app.sqlite"
    conn = sqlite3.connect(str(db_path))
    conn.execute(
        "CREATE TABLE background_jobs ("
        "job_id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, kind TEXT NOT NULL, "
        "title TEXT, meta TEXT, archived INTEGER DEFAULT 0, created_at REAL, updated_at REAL)"
    )
    conn.execute(
        "INSERT INTO background_jobs VALUES ('old1', 'job-old1', 'vm_build', 'legacy-vm', '{}', 0, 1.0, 1.0)"
    )
    conn.commit()
    conn.close()

    store = AppStore(db_path)
    assert store.has_uncached_job_stages() is True
    rec = store.get_job("old1")
    assert rec is not None and rec["stage"] == ""
    store.update_job_stage("old1", "waiting")
    assert store.has_uncached_job_stages() is False
    assert store.count_jobs_by_stages(("waiting",)) == 1


def test_delete_thread_checkpoints_clears_target_thread_only(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "cp.sqlite"))
    conn.executescript(
        """
        CREATE TABLE checkpoints (thread_id TEXT, data TEXT);
        CREATE TABLE writes (thread_id TEXT, data TEXT);
        INSERT INTO checkpoints VALUES ('t1', 'a'), ('t2', 'b');
        INSERT INTO writes VALUES ('t1', 'x'), ('t2', 'y');
        """
    )
    conn.commit()

    class FakeSaver:
        def __init__(self, c):
            self.conn = c
            self.lock = threading.Lock()

    delete_thread_checkpoints("t1", checkpointer=FakeSaver(conn))
    assert conn.execute("SELECT thread_id FROM checkpoints").fetchall() == [("t2",)]
    assert conn.execute("SELECT thread_id FROM writes").fetchall() == [("t2",)]
    conn.close()
