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
        self.upload_remote_dir = ""
        self.upload_extra_dirs = []
        self.upload_extra_files = []
        self.upload_exclude_dir_names = set()
        self.waited_sshd = []
        self.os_release = ""

    def ensure_initial_login(self, ip, username, init_password, new_password, os_type="linux"):
        return 0, "init-pw", False, "使用初始預設密碼連線正常"

    def wait_port_ready(self, ip, initial_delay=3.0, timeout=20.0):
        self.waited_sshd.append(ip)
        return True

    def upload_files(
        self,
        ip,
        files,
        remote_dir,
        extra_dirs,
        username,
        password,
        extra_files=None,
        exclude_dir_names=None,
        os_type="linux",
        transfer_timeout=120,
    ):
        self.uploaded = [f.name for f in files]
        self.upload_remote_dir = remote_dir
        self.upload_extra_dirs = list(extra_dirs or [])
        self.upload_extra_files = list(extra_files or [])
        self.upload_exclude_dir_names = set(exclude_dir_names or set())
        return 0, [f"{remote_dir}/{f.name}" for f in files], ""

    def exec_command(self, ip, cmd, username=None, password=None, sudo=False, sudo_password=None,
                     env=None, cmd_timeout=None, secret_env=None, os_type="linux", remote_temp_dir=None):
        self.calls.append({"cmd": cmd, "env": env or {}, "secret_env": secret_env or {},
                           "password": password, "sudo": sudo, "sudo_password": sudo_password,
                           "os_type": os_type, "remote_temp_dir": remote_temp_dir})
        if cmd == "cat /etc/os-release":
            return 0, self.os_release, ""
        if "GetFolderPath('Desktop')" in cmd:
            return 0, f"C:\\Users\\{username or 'user1'}\\Desktop\nC:\\Users\\{username or 'user1'}\\AppData\\Local\\Temp\\\n", ""
        if cmd.endswith("add_pub_key.ps1"):
            return 0, "[OK] Detached sshd restart scheduled. [SSHD_RESTART_SCHEDULED]", ""
        if cmd.startswith("hostname"):
            return 0, "host\nroot\n", ""
        if cmd == "whoami":
            return 0, f"WIN-HOST\\{username or 'user1'}\n", ""
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
    answers = iter([
        {"action": "regenerate"},
        {"action": "edit", "edits": {"cs_mode": "skip"}},
        {"action": "approve"},
    ])
    shown = []

    def decide(req):
        shown.append(_shown_passwords(req))   # 介面層在審核當下從 SecretStore 取值顯示
        return next(answers)

    requests, tool_msg = drive(agent, decide)

    # 重新產生後，審核畫面顯示新密碼；修改 cs_mode 後先回到審核畫面確認，再核准執行
    first, second, third = shown
    assert first["admin_password"] != second["admin_password"]
    assert second == third
    assert requests[0]["fields"][1]["value"] == "proj-demo-AP01"     # hostname 由 Aria 反查
    assert "SKIP" in requests[2]["fields"][7]["value"]
    admin_pw, user2_pw = second["admin_password"], second["user2_password"]

    assert tool_msg.artifact["status"] == "completed"
    executed = [c["cmd"].rsplit("/", 1)[-1] if c["cmd"].endswith((".sh", ".ps1")) else c["cmd"] for c in ssh.calls]
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
    assert agent.get_state(cfg).next  # 仍有待執行節點，可於後續直接從斷點續跑

    agent.invoke(None, cfg)  # 續跑
    executed = [c["cmd"].rsplit("/", 1)[-1] for c in ssh.calls]
    assert executed == ["add_pub_key.sh", "set_no_passwd.sh", "set_hostname.sh", "set_passwords.sh",
                        "set_zabbix.sh", "remover.sh"]
    tool_msgs = [m for m in agent.get_state(cfg).values["messages"] if m.type == "tool"]
    assert tool_msgs[-1].artifact["status"] == "completed"


def test_custom_password_edit_via_cli_review_never_leaks_to_checkpoint(fakes, monkeypatch):
    """透過 CLI 審核 [E] 修改自訂密碼 (含 ? 獨立輸入框支援逗號)，驗證新密碼生效且絕不落盤至 Checkpoint"""
    from collections import deque
    import cli.review as review_mod

    ssh, sink = fakes
    agent, saver = build_agent_calling(
        bootstrap_vm_pipeline, {"ip": "10.0.0.14", "cs_mode": "SKIP", "skip_disk": True}
    )
    custom_admin = "CustomAdmin#2026"
    custom_user2 = "User2,With,Comma!9"
    inputs = deque([
        "e",
        f"admin_password={custom_admin}, user2_password=?",
        custom_user2,
        "a",
    ])
    monkeypatch.setattr(review_mod.Prompt, "ask", staticmethod(lambda *a, **k: inputs.popleft()))

    shown = []

    def decide(req):
        shown.append(_shown_passwords(req))
        return review_mod.handle_review(req)

    requests, tool_msg = drive(agent, decide)
    assert len(requests) == 2
    assert shown[1]["admin_password"] == custom_admin
    assert shown[1]["user2_password"] == custom_user2
    assert tool_msg.artifact["status"] == "completed"

    pw_call = next(c for c in ssh.calls if c["cmd"].endswith("set_passwords.sh"))
    assert pw_call["secret_env"] == {"PW_ROOT": custom_admin, "PW_USER1": custom_admin, "PW_USER2": custom_user2}
    assert custom_admin in sink.records[0][2].values()
    assert custom_user2 in sink.records[0][2].values()

    # 嚴格驗證：自訂明文密碼絕不出現在 Command(resume)、Checkpoint storage/writes 或給 LLM 的訊息中
    blob = json.dumps(list(saver.storage.items()), default=str) + str(dict(saver.writes)) + tool_msg.content
    assert custom_admin not in blob and custom_user2 not in blob


def test_empty_password_edit_returns_validation_error(fakes):
    agent, _ = build_agent_calling(bootstrap_vm_pipeline, {"ip": "10.0.0.15"})
    answers = iter([{"action": "edit", "edits": {"admin_password": ""}}, {"action": "skip"}])
    requests, _ = drive(agent, lambda req: next(answers))
    assert len(requests) == 2
    assert any("admin_password" in e and "不可為空" in e for e in requests[1]["errors"])


def test_windows_bootstrap_flow_end_to_end(fakes, monkeypatch):
    """驗證 Windows Bootstrap：Aria 自動偵測 OS、無 cleanup、僅改 user1/user2 密碼、無 sudo、完成後排程重開機"""
    ssh, sink = fakes

    class FakeWinAria:
        def query_vm_by_ip(self, ip):
            return {
                "found": True,
                "deployment": {"name": "proj-win-AP01"},
                "machine": {"name": "proj-win-AP01", "os_type": "Windows_Server_2022"},
            }

    monkeypatch.setattr(nodes, "get_aria_client", lambda: FakeWinAria())
    agent, saver = build_agent_calling(
        bootstrap_vm_pipeline, {"ip": "10.0.0.30", "cs_mode": "MDR", "skip_disk": False}
    )
    shown = []

    def decide(req):
        shown.append(_shown_passwords(req))
        return {"action": "approve"}

    requests, tool_msg = drive(agent, decide)
    assert len(requests) == 1
    fields_by_key = {f["key"]: f for f in requests[0]["fields"]}
    assert fields_by_key["os_type"]["value"] == "windows"
    assert fields_by_key["hostname"]["value"] == "proj-win-AP01"
    assert "sccm" in fields_by_key

    admin_pw = shown[0]["admin_password"]
    user2_pw = shown[0]["user2_password"]

    assert tool_msg.artifact["status"] == "completed"
    # 驗證上傳至桌面 bootstrap 資料夾、機密暫存檔分開放 Temp、CrowdStrike 只傳 exe 且 SCCM 排除 i386
    assert ssh.upload_remote_dir.endswith("/Desktop/bootstrap")
    assert ssh.upload_exclude_dir_names == {"i386"}
    assert ssh.waited_sshd == ["10.0.0.30"]
    if ssh.upload_extra_files:
        assert all(p.name.lower().endswith(".exe") for p, _ in ssh.upload_extra_files)

    step_calls = [c for c in ssh.calls if "GetFolderPath('Desktop')" not in c["cmd"]]
    executed = [c["cmd"].rsplit("/", 1)[-1] if c["cmd"].endswith((".sh", ".ps1")) else c["cmd"] for c in step_calls]
    assert executed == [
        "add_pub_key.ps1",
        "set_dns.ps1",
        "set_hostname.ps1",
        "set_passwords.ps1",
        "delete_task.ps1",
        "set_zabbix.ps1",
        "set_sccm.ps1",
        "set_crowdstrike.ps1",
        "expand_root_disk.ps1",
        "mount_extra_disk.ps1",
        "check_system.ps1",
        'shutdown /r /t 5 /c "VM Bootstrap completed"',
    ]
    assert all(c["os_type"] == "windows" and c["sudo"] is False for c in ssh.calls)

    pw_call = next(c for c in step_calls if c["cmd"].endswith("set_passwords.ps1"))
    assert pw_call["secret_env"] == {"PW_USER1": admin_pw, "PW_USER2": user2_pw}
    assert pw_call["remote_temp_dir"].endswith("/AppData/Local/Temp")
    # 改密碼後的步驟切換為新密碼
    zabbix_call = next(c for c in step_calls if c["cmd"].endswith("set_zabbix.ps1"))
    assert zabbix_call["password"] == admin_pw
    # Windows 密碼紀錄不含 root
    assert len(sink.records) == 1
    assert all("root" not in k for k in sink.records[0][2])
    # 確認明文密碼不外洩
    blob = json.dumps(list(saver.storage.items()), default=str) + str(dict(saver.writes)) + tool_msg.content
    assert admin_pw not in blob and user2_pw not in blob
    assert "自動重開機" in tool_msg.content


def test_linux_targeted_crowdstrike_upload(fakes, tmp_path, monkeypatch):
    """驗證 Linux 先偵測 /etc/os-release 後，只上傳對應發行版的 CrowdStrike 子目錄，不傳 Windows 或其他版本"""
    ssh, _ = fakes
    ssh.os_release = 'ID="ol"\nID_LIKE="fedora"\nVERSION_ID="9.4"\n'
    cs_root = tmp_path / "CrowdStrike"
    for sub in ("Ubuntu", "RHEL_8", "RHEL_9", "Windows"):
        (cs_root / sub).mkdir(parents=True)
        (cs_root / sub / "dummy.pkg").write_text("x", encoding="utf-8")
    monkeypatch.setattr(nodes.config, "CROWDSTRIKE_INSTALLERS_DIR", cs_root)

    assert nodes.detect_linux_cs_subdir('ID=ubuntu\nVERSION_ID="24.04"') == "Ubuntu"
    assert nodes.detect_linux_cs_subdir('ID="rhel"\nVERSION_ID="8.9"') == "RHEL_8"
    assert nodes.detect_linux_cs_subdir('ID="rocky"\nID_LIKE="rhel"\nVERSION_ID="9.3"') == "RHEL_9"
    assert nodes.detect_linux_cs_subdir("unknown") is None

    agent, _ = build_agent_calling(bootstrap_vm_pipeline, {"ip": "10.0.0.31", "cs_mode": "NGAV", "skip_disk": True})
    _, tool_msg = drive(agent, lambda req: {"action": "approve"})
    assert tool_msg.artifact["status"] == "completed"
    assert ssh.upload_extra_dirs == [(cs_root / "RHEL_9", "/tmp/CrowdStrike/RHEL_9")]
