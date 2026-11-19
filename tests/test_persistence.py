"""持久化層測試：AppStore (sessions / pipeline_sessions) 與 delete_thread_checkpoints"""
import sqlite3
import threading

from core.persistence import AppStore, delete_thread_checkpoints


def test_app_store_sessions_and_filtering(tmp_path):
    store = AppStore(tmp_path / "app.sqlite")

    store.touch_session("chat-1", "原始標題", kind="chat")
    # 空標題更新不應覆蓋原本標題
    store.touch_session("chat-1", "", kind="chat")
    assert store.get_session("chat-1")["title"] == "原始標題"

    store.touch_session("vmjob-1", "proj-demo-AP01", kind="vm_lifecycle")
    store.touch_session("vmjob-2", "proj-demo-AP02", kind="vm_lifecycle")
    store.archive_session("vmjob-2")

    # 預設排除已封存
    lc = store.list_sessions(kind="vm_lifecycle")
    assert [s["thread_id"] for s in lc] == ["vmjob-1"]

    # include_archived=True 包含已封存
    lc_all = store.list_sessions(kind="vm_lifecycle", include_archived=True)
    assert [s["thread_id"] for s in lc_all] == ["vmjob-1", "vmjob-2"]

    # exclude_kind 排除生命週期 session
    chats = store.list_sessions(exclude_kind="vm_lifecycle")
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
