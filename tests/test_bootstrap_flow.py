"""VM Bootstrap：審核 (含重新產生密碼、密碼遺失) → 逐步執行，並確認密碼不外洩到命令列 / state"""
import json

import pytest

from core.secrets import get_secret_store, set_credential_sink_factory
from helpers import build_agent_calling, drive
from subgraphs.vm_bootstrap import nodes
from subgraphs.vm_bootstrap.tool import bootstrap_vm_pipeline


class FakeSSH:
    def __init__(self):
        self.calls = []
        self.uploaded = []

    def ensure_initial_login(self, ip, username, init_password, new_password):
        return 0, "init-pw", False, "使用初始預設密碼連線正常"

    def upload_files(self, ip, files, remote_dir, extra_dirs, username, password):
        self.uploaded = [f.name for f in files]
        return 0, [f"{remote_dir}/{f.name}" for f in files], ""

    def exec_command(self, ip, cmd, username=None, password=None, sudo=False, sudo_password=None,
                     env=None, cmd_timeout=None, secret_env=None):
        self.calls.append({"cmd": cmd, "env": env or {}, "secret_env": secret_env or {},
                           "password": password, "sudo": sudo, "sudo_password": sudo_password})
        if cmd.startswith("hostname"):
            return 0, "host\nroot\n", ""
        return 0, "[OK]", ""


class MemorySink:
    def __init__(self):
        self.records = []

    def record(self, ip, hostname, accounts):
        self.records.append((ip, hostname, dict(accounts)))
        return "memory://credentials"


class FakeAria:
    def query_vm_by_ip(self, ip):
        return {"found": True, "deployment": {"name": "proj-demo-AP01"}}


@pytest.fixture
def fakes(monkeypatch):
    ssh, sink = FakeSSH(), MemorySink()
    monkeypatch.setattr(nodes, "get_ssh_client", lambda: ssh)
    monkeypatch.setattr(nodes, "get_aria_client", lambda: FakeAria())
    monkeypatch.setattr(nodes.config, "SSH_PRIVATE_KEY_PATH", nodes.Path("Z:/no/such/key"))
    set_credential_sink_factory(lambda: sink)
    yield ssh, sink
    set_credential_sink_factory(None)


def _shown_passwords(req):
    store = get_secret_store()
    return {f["key"]: (store.get(f["secret_ref"]) or {}).get(f["secret_key"])
            for f in req["fields"] if f.get("secret_ref")}


def test_bootstrap_flow_keeps_secrets_off_command_line(fakes):
    ssh, sink = fakes
    agent, saver = build_agent_calling(bootstrap_vm_pipeline, {"ip": "10.0.0.10", "skip_disk": True})
    answers = iter([{"action": "regenerate"}, {"action": "edit", "edits": {"cs_mode": "skip"}}])
    shown = []

    def decide(req):
        shown.append(_shown_passwords(req))   # 介面層在審核當下從 SecretStore 取值顯示
        return next(answers)

    requests, tool_msg = drive(agent, decide)

    # 重新產生後，審核畫面顯示新密碼；最終寫入的就是最後一次看到的密碼
    first, second = shown
    assert first["admin_password"] != second["admin_password"]
    assert requests[0]["fields"][1]["value"] == "proj-demo-AP01"     # hostname 由 Aria 反查
    admin_pw, user2_pw = second["admin_password"], second["user2_password"]

    assert tool_msg.artifact["status"] == "completed"
    executed = [c["cmd"].rsplit("/", 1)[-1] for c in ssh.calls]
    assert executed == ["add_pub_key.sh", "set_no_passwd.sh", "set_hostname.sh", "set_passwords.sh",
                        "set_zabbix.sh", "remover.sh"]   # CrowdStrike / 磁碟依設定略過

    pw_call = next(c for c in ssh.calls if c["cmd"].endswith("set_passwords.sh"))
    assert pw_call["secret_env"] == {"PW_ROOT": admin_pw, "PW_USER1": admin_pw, "PW_USER2": user2_pw}
    for c in ssh.calls:
        flat = c["cmd"] + json.dumps(c["env"])
        assert admin_pw not in flat and user2_pw not in flat
    # 改密碼後的步驟改用新密碼登入
    zabbix_call = next(c for c in ssh.calls if c["cmd"].endswith("set_zabbix.sh"))
    assert zabbix_call["password"] == admin_pw

    assert len(sink.records) == 1 and admin_pw in sink.records[0][2].values()
    # 明文密碼不進入 checkpoint 與給 LLM 的內容
    blob = json.dumps(list(saver.storage.items()), default=str) + str(dict(saver.writes)) + tool_msg.content
    assert admin_pw not in blob and user2_pw not in blob
    # 完成後記憶體中的密碼即清除
    assert get_secret_store().get(requests[1]["fields"][4]["secret_ref"]) is None


def test_secrets_lost_regenerates_and_reviews_again(fakes):
    ssh, _ = fakes
    agent, _ = build_agent_calling(bootstrap_vm_pipeline, {"ip": "10.0.0.11"})
    seen = []

    def decide(req):
        seen.append(_shown_passwords(req))
        if len(seen) == 1:
            # 模擬程式重啟：記憶體中的密碼消失後才按下核准
            get_secret_store().delete(req["fields"][4]["secret_ref"])
            return {"action": "approve"}
        return {"action": "skip", "feedback": "測試結束"}

    requests, tool_msg = drive(agent, decide)
    assert len(requests) == 2
    assert requests[0]["fields"][4]["secret_ref"] != requests[1]["fields"][4]["secret_ref"]
    assert all(seen[1].values())
    assert tool_msg.artifact["status"] == "aborted"
    assert ssh.calls == []                                            # 未核准前不會執行任何遠端指令


def test_invalid_edit_returns_to_review(fakes):
    agent, _ = build_agent_calling(bootstrap_vm_pipeline, {"ip": "10.0.0.12"})
    answers = iter([{"action": "edit", "edits": {"cs_mode": "BAD", "foo": "1"}}, {"action": "skip"}])
    requests, _ = drive(agent, lambda req: next(answers))
    assert len(requests) == 2
    assert any("cs_mode" in e for e in requests[1]["errors"])
    assert any("foo" in e for e in requests[1]["errors"])


def test_resume_after_crash_continues_from_next_step(fakes):
    """程式在步驟中途被中斷 (例如 Ctrl+C / 異常結束) 後，以相同 thread 繼續時從下一步續跑，不重做已完成步驟"""
    from langgraph.types import Command

    ssh, _ = fakes
    original = ssh.exec_command
    crashed = {"done": False}

    def flaky(ip, cmd, **kw):
        if cmd.endswith("set_hostname.sh") and not crashed["done"]:
            crashed["done"] = True
            raise KeyboardInterrupt  # 非 Exception，模擬行程中斷
        return original(ip, cmd, **kw)

    ssh.exec_command = flaky
    agent, _ = build_agent_calling(bootstrap_vm_pipeline, {"ip": "10.0.0.13", "cs_mode": "SKIP", "skip_disk": True})
    cfg = {"configurable": {"thread_id": "crash"}, "recursion_limit": 60}

    agent.invoke({"messages": [("user", "go")]}, cfg)
    itr = agent.get_state(cfg).interrupts[0]
    with pytest.raises(KeyboardInterrupt):
        agent.invoke(Command(resume={itr.id: {"action": "approve"}}), cfg)
    assert [c["cmd"].rsplit("/", 1)[-1] for c in ssh.calls] == ["add_pub_key.sh", "set_no_passwd.sh"]
    assert agent.get_state(cfg).next  # 仍有待執行節點 → /pending 會列出

    agent.invoke(None, cfg)  # 續跑
    executed = [c["cmd"].rsplit("/", 1)[-1] for c in ssh.calls]
    assert executed == ["add_pub_key.sh", "set_no_passwd.sh", "set_hostname.sh", "set_passwords.sh",
                        "set_zabbix.sh", "remover.sh"]
    tool_msgs = [m for m in agent.get_state(cfg).values["messages"] if m.type == "tool"]
    assert tool_msgs[-1].artifact["status"] == "completed"
