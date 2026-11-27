"""CLI 介面層：通用審核畫面、對話回合與未完成審核自動詢問續跑 (以預先排好的輸入取代鍵盤)"""
from collections import deque

import pytest

import cli.review as review_mod
import cli.session as session_mod
from core.secrets import get_secret_store
from helpers import build_agent_calling
from subgraphs.vm_bootstrap.tool import bootstrap_vm_pipeline
from test_bootstrap_flow import fakes  # noqa: F401  (共用 fixture)


@pytest.fixture
def scripted_input(monkeypatch):
    answers: deque = deque()

    def ask(prompt, *args, **kwargs):
        return answers.popleft() if answers else kwargs.get("default", "")

    monkeypatch.setattr(review_mod.Prompt, "ask", staticmethod(ask))
    monkeypatch.setattr(session_mod.Prompt, "ask", staticmethod(ask))
    return answers


def test_handle_review_renders_secret_and_collects_edits(scripted_input):
    ref = get_secret_store().put({"pw": "S3cret!"})
    request = {
        "kind": "demo", "title": "Demo", "message": "請審核",
        "fields": [
            {"key": "hostname", "label": "主機名稱", "value": "h1", "editable": True},
            {"key": "pw", "label": "密碼", "secret_ref": ref, "secret_key": "pw", "editable": False},
        ],
        "actions": [
            {"action": "approve", "hotkey": "A", "label": "核准"},
            {"action": "edit", "hotkey": "E", "label": "修改", "needs_edits": True},
        ],
        "errors": ["cpu_count 必須是整數"],
        "notes": ["測試提示"],
    }
    scripted_input.extend(["e", "hostname=h2, x=1"])
    decision = review_mod.handle_review(request)
    assert decision == {"action": "edit", "edits": {"hostname": "h2", "x": "1"}, "feedback": ""}


def test_run_turn_with_review(fakes, scripted_input, monkeypatch):  # noqa: F811
    from subgraphs.jobs import list_jobs, wait_for_background

    agent, _ = build_agent_calling(bootstrap_vm_pipeline, {"ip": "10.0.0.20", "cs_mode": "SKIP", "skip_disk": True})
    scripted_input.extend(["a"])
    final = session_mod.run_turn(agent, "cli-t1", "幫我對 10.0.0.20 做基本設定")
    assert final == "done"
    for j in list_jobs():
        wait_for_background(j.id, timeout=5.0)
    ssh, sink = fakes
    assert ssh.calls and len(sink.records) == 1
    assert list(agent.get_state({"configurable": {"thread_id": "cli-t1"}}).interrupts or ()) == []


def test_interrupted_turn_auto_prompts_resume_or_cancel(fakes, scripted_input):  # noqa: F811
    agent, _ = build_agent_calling(bootstrap_vm_pipeline, {"ip": "10.0.0.20", "cs_mode": "SKIP", "skip_disk": True})

    # 模擬第一次審核時按下 Ctrl+C 中斷
    def raise_kb(*a, **k):
        raise KeyboardInterrupt()

    orig_ask = review_mod.Prompt.ask
    review_mod.Prompt.ask = staticmethod(raise_kb)
    session_mod.run_turn(agent, "cli-t2", "幫我對 10.0.0.20 做基本設定")
    review_mod.Prompt.ask = orig_ask

    # 確認此時 Session 停在 interrupt
    assert len(agent.get_state({"configurable": {"thread_id": "cli-t2"}}).interrupts) == 1

    # 下次呼叫 resolve_dangling_interrupts 時自動詢問「是否繼續審核？[y/n]」→ 輸入 y 並核准 a
    from subgraphs.jobs import list_jobs, wait_for_background

    scripted_input.extend(["y", "a"])
    final = session_mod.resolve_dangling_interrupts(agent, "cli-t2")
    assert final == "done"
    for j in list_jobs():
        wait_for_background(j.id, timeout=5.0)
    assert list(agent.get_state({"configurable": {"thread_id": "cli-t2"}}).interrupts or ()) == []


def test_interrupted_turn_cancel_clears_interrupts(fakes, scripted_input):  # noqa: F811
    """中斷後選擇 n 放棄審核，應補齊 ToolMessage 並清空 interrupts，不影響後續對話"""
    agent, _ = build_agent_calling(bootstrap_vm_pipeline, {"ip": "10.0.0.21", "cs_mode": "SKIP", "skip_disk": True})

    def raise_kb(*a, **k):
        raise KeyboardInterrupt()

    orig_ask = review_mod.Prompt.ask
    review_mod.Prompt.ask = staticmethod(raise_kb)
    session_mod.run_turn(agent, "cli-t3", "幫我對 10.0.0.21 做基本設定")
    review_mod.Prompt.ask = orig_ask

    assert len(agent.get_state({"configurable": {"thread_id": "cli-t3"}}).interrupts) == 1

    scripted_input.extend(["n"])
    final = session_mod.resolve_dangling_interrupts(agent, "cli-t3")
    assert final is None
    assert list(agent.get_state({"configurable": {"thread_id": "cli-t3"}}).interrupts or ()) == []
    ssh, _ = fakes
    assert ssh.calls == []


def test_check_startup_interrupted_session_resumes_chat_and_skips_bg_jobs(fakes, scripted_input):  # noqa: F811
    """背景工作獨立存放在 background_jobs 表，不干擾啟動時對未完成審核對話 Session 的恢復詢問"""
    from core.persistence import get_app_store
    from subgraphs.jobs import KIND_BUILD, list_jobs, wait_for_background

    store = get_app_store()
    store.save_job("ignore01", "job-ignore01", KIND_BUILD, "bg-job")
    store.touch_session("vmjob-legacy", "old-lc", kind="vm_lifecycle")

    agent, _ = build_agent_calling(bootstrap_vm_pipeline, {"ip": "10.0.0.22", "cs_mode": "SKIP", "skip_disk": True})

    def raise_kb(*a, **k):
        raise KeyboardInterrupt()

    orig_ask = review_mod.Prompt.ask
    review_mod.Prompt.ask = staticmethod(raise_kb)
    session_mod.run_turn(agent, "cli-startup-1", "啟動恢復測試")
    review_mod.Prompt.ask = orig_ask

    scripted_input.extend(["y", "a"])
    resumed_tid = session_mod.check_startup_interrupted_session(agent)
    for j in list_jobs():
        wait_for_background(j.id, timeout=5.0)
    assert resumed_tid == "cli-startup-1"
    assert list(agent.get_state({"configurable": {"thread_id": "cli-startup-1"}}).interrupts or ()) == []
