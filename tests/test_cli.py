"""CLI 介面層：通用審核畫面與對話回合 (以預先排好的輸入取代鍵盤)"""
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
    agent, _ = build_agent_calling(bootstrap_vm_pipeline, {"ip": "10.0.0.20", "cs_mode": "SKIP", "skip_disk": True})
    scripted_input.extend(["a"])
    final = session_mod.run_turn(agent, "cli-t1", "幫我對 10.0.0.20 做基本設定")
    assert final == "done"
    ssh, sink = fakes
    assert ssh.calls and len(sink.records) == 1
    assert session_mod.list_pending(agent) == []
