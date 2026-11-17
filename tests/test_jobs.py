"""純 LangGraph 驅動的 VM 生命週期子圖測試 (100% 驅動真實節點，不使用模擬跳步函式)：
1. wait_vm_ready 真實節點迴圈：INITIALIZATION → APPROVAL_PENDING (SUBMITTED) → INPROGRESS (寫入 BUILDING Checkpoint) → SUCCESSFUL → disk_review interrupt
2. 驗證 Resume 時不會重新執行前置的狀態掃描 (wait_vm_ready 不重跑)
3. 驗證 submit_disk_op 與 wait_disk_op 分離：在送出 Add Disk 後、查詢狀態中途 Crash 重啟，絕不會重複送出第 2 次申請 (含磁碟已存在之冪等防呆測試)
4. 驗證初始設定 (Bootstrap) 在第 3 步被 Ctrl+C 中斷後，階段顯示為 BOOTSTRAP_RUNNING，進入 /jobs 會直接從第 3 步續跑，不重設也不重跑前 2 步
5. 驗證連續輪詢失敗達上限時自動推播通知並停止無限重試
"""
import json
import time
from collections import deque

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import cli.jobs as jobs_cli
import cli.review as review_mod
from helpers import build_agent_calling, drive
from subgraphs.vm_bootstrap import nodes as boot_nodes
from subgraphs.vm_disk import graph as disk_graph
from subgraphs.vm_disk import tool as disk_tool
from subgraphs.vm_disk.executor import ADD_DISK, RESIZE_BOOT_DISK
from subgraphs.vm_lifecycle import (
    BOOTSTRAP_REVIEW,
    BOOTSTRAP_RUNNING,
    BUILDING,
    DISK_REVIEW,
    DISK_RUNNING,
    DONE,
    FAILED,
    SUBMITTED,
    build_vm_lifecycle_graph,
    drain_notices,
    get_lifecycle_job,
    lifecycle_config,
    start_lifecycle_job,
)
from subgraphs.vm_lifecycle import nodes as lc_nodes
from test_bootstrap_flow import fakes  # noqa: F401
from tools.job_tools import track_vm_jobs


class FakeDeployments:
    def __init__(self):
        self.dep_status = {"dep-1": [("APPROVAL_PENDING", "CREATE_INPROGRESS")]}
        self.request_status: dict[str, list[str]] = {}
        self.submitted = []
        self.get_calls = 0
        self.extra_resources: list[dict] = []

    def get(self, dep_id, expand=None):
        self.get_calls += 1
        seq = self.dep_status[dep_id]
        r, d = seq[0] if len(seq) == 1 else seq.pop(0)
        return {"status": d, "lastRequest": {"status": r}}

    def resources(self, dep_id):
        base = [{"id": "res-m1", "type": "Cloud.vSphere.Machine",
                 "properties": {"resourceName": "proj-demo035404", "address": "10.0.0.60"}}]
        return base + list(self.extra_resources)

    def resource_actions(self, rid):
        return [{"id": RESIZE_BOOT_DISK, "valid": True}, {"id": ADD_DISK, "valid": True}]

    def resource_action(self, rid, aid):
        return {"schema": {"properties": {"capacityGb": {"default": 60}}}}

    def run_resource_action(self, rid, aid, inputs, reason=""):
        rid_req = f"req-{len(self.submitted) + 1}"
        self.submitted.append((rid, aid, inputs))
        self.request_status.setdefault(rid_req, ["INPROGRESS", "SUCCESSFUL"])
        return {"id": rid_req}

    def get_request(self, req_id):
        seq = self.request_status[req_id]
        return {"status": seq[0] if len(seq) == 1 else seq.pop(0)}


class FakeIaas:
    def find_machines_by_ip(self, ip):
        return [{"deploymentId": "dep-1", "name": "proj-demo035404"}] if ip == "10.0.0.60" else []


class FakeAria:
    def __init__(self):
        self.deployments = FakeDeployments()
        self.iaas = FakeIaas()


@pytest.fixture
def lc_graph(monkeypatch):
    drain_notices()
    g = build_vm_lifecycle_graph(checkpointer=InMemorySaver())
    monkeypatch.setattr("subgraphs.vm_lifecycle.runner.get_vm_lifecycle_graph", lambda: g)
    monkeypatch.setattr(lc_nodes, "wait_for_next_poll", lambda timeout: None)
    return g


def _new_job(lc_graph, **kw):
    base = dict(
        deployment_id="dep-1",
        deployment_name="proj-demo-AP03",
        project="proj-demo",
        is_linux=True,
        auto_start=False,
        graph=lc_graph,
    )
    base.update(kw)
    return start_lifecycle_job(**base)


def test_real_wait_node_transitions_and_stops_at_disk_review_without_replaying_scan(lc_graph, monkeypatch):
    """直接以 lc_graph.invoke(None, cfg) 驅動真實 wait_vm_ready 節點，並驗證 Resume 時不重跑掃描"""
    aria = FakeAria()
    monkeypatch.setattr(disk_graph, "get_aria_client", lambda: aria)
    monkeypatch.setattr(lc_nodes, "get_aria_client", lambda: aria)

    # 先測試送出後處於 INITIALIZATION -> APPROVAL_PENDING -> 轉為 INPROGRESS 時 wait_vm_ready 會先存檔 BUILDING Checkpoint
    aria.deployments.dep_status["dep-1"] = [
        ("INITIALIZATION", "CREATE_INPROGRESS"),
        ("APPROVAL_PENDING", "CREATE_INPROGRESS"),
        ("INPROGRESS", "CREATE_INPROGRESS"),
        ("SUCCESSFUL", "CREATE_SUCCESSFUL"),
    ]
    job = _new_job(lc_graph, extra_disk_gb=100)
    assert get_lifecycle_job(job.id, graph=lc_graph).stage == SUBMITTED

    # 驅動真實節點：wait_vm_ready 內部經過 INITIALIZATION -> APPROVAL_PENDING -> INPROGRESS (存檔 BUILDING) -> SUCCESSFUL -> 停在 disk_review interrupt!
    lc_graph.invoke(None, config=lifecycle_config(job.id))

    j = get_lifecycle_job(job.id, graph=lc_graph)
    assert j.stage == DISK_REVIEW and j.ip == "10.0.0.60" and j.machine_id == "res-m1"
    assert j.vm_name == "proj-demo035404"
    notices = drain_notices()
    assert any("硬碟調整" in n and "/jobs" in n for n in notices)

    calls_before_resume = aria.deployments.get_calls
    assert calls_before_resume == 4

    # 使用者於 /jobs 執行 Resume 核准硬碟調整：驗證絕對不會重跑 wait_vm_ready！
    answers = deque(["a"])
    monkeypatch.setattr(review_mod.Prompt, "ask", staticmethod(lambda *a, **k: answers.popleft() if answers else ""))
    monkeypatch.setattr(jobs_cli, "spawn_background_continuation", lambda *a, **k: None)
    jobs_cli.run_disk_review(job.id, graph=lc_graph)

    assert aria.deployments.get_calls == calls_before_resume   # 0 次重跑！
    j_after = get_lifecycle_job(job.id, graph=lc_graph)
    assert j_after.stage == DISK_RUNNING
    assert [op["action_id"] for op in j_after.disk_ops] == [ADD_DISK]


def test_crash_after_submit_disk_op_does_not_resubmit(lc_graph, monkeypatch):
    """
    實測問題 1 驗證：
    在 Add Disk 送出後 (submit_disk_op 完成)、查詢狀態時 (wait_disk_op 內) 行程中斷，
    再從 Checkpoint 續跑，驗證 request_id 已寫入 Checkpoint，Aria 只會收到 1 次申請！
    """
    aria = FakeAria()
    monkeypatch.setattr(disk_graph, "get_aria_client", lambda: aria)
    monkeypatch.setattr(lc_nodes, "get_aria_client", lambda: aria)
    aria.deployments.dep_status["dep-1"] = [("SUCCESSFUL", "CREATE_SUCCESSFUL")]

    job = _new_job(lc_graph, extra_disk_gb=100)
    lc_graph.invoke(None, config=lifecycle_config(job.id))
    assert get_lifecycle_job(job.id, graph=lc_graph).stage == DISK_REVIEW

    # 核准硬碟調整 (停在 disk_finalize 之後、submit_disk_op 之前)
    answers = deque(["a"])
    monkeypatch.setattr(review_mod.Prompt, "ask", staticmethod(lambda *a, **k: answers.popleft() if answers else ""))
    monkeypatch.setattr(jobs_cli, "spawn_background_continuation", lambda *a, **k: None)
    jobs_cli.run_disk_review(job.id, graph=lc_graph)

    # 模擬在 get_request 查詢狀態時行程中斷 (KeyboardInterrupt)
    orig_get_request = aria.deployments.get_request
    crashed = {"once": False}

    def crash_on_poll(req_id):
        if not crashed["once"]:
            crashed["once"] = True
            raise KeyboardInterrupt("simulated crash during wait_disk_op")
        return orig_get_request(req_id)

    aria.deployments.get_request = crash_on_poll
    with pytest.raises(KeyboardInterrupt):
        lc_graph.invoke(None, config=lifecycle_config(job.id))

    # 確認此時 Add Disk 已送出 1 次，且 Checkpoint 停在 wait_disk_op (帶有 request_id)
    assert len(aria.deployments.submitted) == 1
    state_mid = lc_graph.get_state(lifecycle_config(job.id))
    assert state_mid.next == ("wait_disk_op",)
    assert state_mid.values["ops"][0]["request_id"] == "req-1"

    # 重啟後從 Checkpoint 續跑：直接從 wait_disk_op 查詢 req-1，絕不重複呼叫 run_resource_action！
    lc_graph.invoke(None, config=lifecycle_config(job.id))
    assert len(aria.deployments.submitted) == 1               # 仍然只有 1 次申請！
    assert get_lifecycle_job(job.id, graph=lc_graph).stage == BOOTSTRAP_REVIEW


def test_submit_disk_op_idempotency_skips_existing_extra_disk(lc_graph, monkeypatch):
    """驗證冪等防呆：若 deployment 中已存在同名額外硬碟 (proj-demo035404-disk2-*)，submit_disk_op 自動略過不重送"""
    aria = FakeAria()
    aria.deployments.extra_resources = [
        {"id": "disk-existing", "type": "Cloud.vSphere.Disk",
         "properties": {"resourceName": "proj-demo035404-disk2-123456"}}
    ]
    monkeypatch.setattr(disk_graph, "get_aria_client", lambda: aria)
    monkeypatch.setattr(lc_nodes, "get_aria_client", lambda: aria)
    aria.deployments.dep_status["dep-1"] = [("SUCCESSFUL", "CREATE_SUCCESSFUL")]

    job = _new_job(lc_graph, extra_disk_gb=100)
    lc_graph.invoke(None, config=lifecycle_config(job.id))

    answers = deque(["a"])
    monkeypatch.setattr(review_mod.Prompt, "ask", staticmethod(lambda *a, **k: answers.popleft() if answers else ""))
    monkeypatch.setattr(jobs_cli, "spawn_background_continuation", lambda *a, **k: None)
    jobs_cli.run_disk_review(job.id, graph=lc_graph)

    lc_graph.invoke(None, config=lifecycle_config(job.id))
    assert aria.deployments.submitted == []                    # 偵測到已存在，0 次送出
    assert get_lifecycle_job(job.id, graph=lc_graph).stage == BOOTSTRAP_REVIEW


def test_bootstrap_interrupted_at_step3_resumes_from_jobs_without_reset(lc_graph, fakes, monkeypatch):  # noqa: F811
    """
    實測問題 2 驗證：
    在初始設定第 3 步 (set_hostname.sh) 按 Ctrl+C 中斷後：
    1. 階段顯示為 BOOTSTRAP_RUNNING (等你續跑：③ 初始設定 (中斷待續))
    2. 進 /jobs 執行 run_bootstrap 時，直接從第 3 步 stream(None) 續跑，不重設也不重跑第 1、2 步！
    """
    ssh, _ = fakes
    aria = FakeAria()
    monkeypatch.setattr(lc_nodes, "get_aria_client", lambda: aria)
    aria.deployments.dep_status["dep-1"] = [("SUCCESSFUL", "CREATE_SUCCESSFUL")]

    job = _new_job(lc_graph)
    lc_graph.invoke(None, config=lifecycle_config(job.id))
    assert get_lifecycle_job(job.id, graph=lc_graph).stage == BOOTSTRAP_REVIEW

    # 在第 3 步 (set_hostname.sh) 模擬按 Ctrl+C
    orig_exec = ssh.exec_command
    interrupted = {"once": False}

    def interrupt_at_step3(ip, cmd, **kw):
        if cmd.endswith("set_hostname.sh") and not interrupted["once"]:
            interrupted["once"] = True
            raise KeyboardInterrupt()
        return orig_exec(ip, cmd, **kw)

    ssh.exec_command = interrupt_at_step3
    answers = deque(["e", "cs_mode=SKIP, skip_disk=true"])
    monkeypatch.setattr(review_mod.Prompt, "ask", staticmethod(lambda *a, **k: answers.popleft() if answers else ""))

    # 第一次執行 run_bootstrap：審核通過，跑完第 1、2 步，在第 3 步中斷
    jobs_cli.run_bootstrap(job.id, graph=lc_graph)
    j_mid = get_lifecycle_job(job.id, graph=lc_graph)
    assert j_mid.stage == BOOTSTRAP_RUNNING
    assert [c["cmd"].rsplit("/", 1)[-1] for c in ssh.calls] == ["add_pub_key.sh", "set_no_passwd.sh"]

    # 第二次進入 /jobs 執行 run_bootstrap：不需再輸入任何審核選項 (answers 為空)，直接從第 3 步續跑到完成！
    assert len(answers) == 0
    jobs_cli.run_bootstrap(job.id, graph=lc_graph)

    j_done = get_lifecycle_job(job.id, graph=lc_graph)
    assert j_done.stage == DONE
    assert [c["cmd"].rsplit("/", 1)[-1] for c in ssh.calls] == [
        "add_pub_key.sh",
        "set_no_passwd.sh",
        "set_hostname.sh",
        "set_passwords.sh",
        "set_zabbix.sh",
        "remover.sh",
    ]


def test_lifecycle_without_disk_goes_to_bootstrap_or_done(lc_graph, monkeypatch):
    aria = FakeAria()
    monkeypatch.setattr(lc_nodes, "get_aria_client", lambda: aria)
    aria.deployments.dep_status["dep-1"] = [("SUCCESSFUL", "CREATE_SUCCESSFUL")]

    linux = _new_job(lc_graph)
    lc_graph.invoke(None, config=lifecycle_config(linux.id))
    assert get_lifecycle_job(linux.id, graph=lc_graph).stage == BOOTSTRAP_REVIEW

    win = _new_job(lc_graph, is_linux=False)
    lc_graph.invoke(None, config=lifecycle_config(win.id))
    win_job = get_lifecycle_job(win.id, graph=lc_graph)
    assert win_job.stage == DONE and "Windows" in win_job.note


def test_lifecycle_failure_and_overdue_persisted_in_checkpoint(lc_graph, monkeypatch):
    aria = FakeAria()
    monkeypatch.setattr(lc_nodes, "get_aria_client", lambda: aria)
    aria.deployments.dep_status["dep-1"] = [
        ("APPROVAL_PENDING", "CREATE_INPROGRESS"),
        ("APPROVAL_REJECTED", "CREATE_FAILED"),
    ]
    job = _new_job(lc_graph)
    # 將 stage_since 設為 25 小時前以觸發 overdue Checkpoint 存檔，再接著讀到 APPROVAL_REJECTED
    lc_graph.update_state(
        lifecycle_config(job.id),
        {"stage_since": time.time() - 25 * 3600},
        as_node="init_lifecycle",
    )
    lc_graph.invoke(None, config=lifecycle_config(job.id))
    notices = drain_notices()
    assert any("超過 24 小時" in n for n in notices)
    assert any("失敗" in n for n in notices)
    assert get_lifecycle_job(job.id, graph=lc_graph).stage == FAILED


def test_consecutive_poll_errors_stops_and_notifies(lc_graph, monkeypatch):
    """驗證連續查詢失敗達 MAX_CONSECUTIVE_POLL_ERRORS 次時推播通知並停止無限重試"""
    aria = FakeAria()
    calls = {"n": 0}

    def broken_get(dep_id, expand=None):
        calls["n"] += 1
        raise RuntimeError("API unreachable")

    aria.deployments.get = broken_get
    monkeypatch.setattr(lc_nodes, "get_aria_client", lambda: aria)

    job = _new_job(lc_graph)
    with pytest.raises(RuntimeError):
        lc_graph.invoke(None, config=lifecycle_config(job.id))

    assert calls["n"] == lc_nodes.MAX_CONSECUTIVE_POLL_ERRORS
    notices = drain_notices()
    assert any("連續查詢失敗" in n and "已暫停背景輪詢" in n for n in notices)


def test_adjust_vm_disk_tool_review_and_execute(monkeypatch):
    aria = FakeAria()
    monkeypatch.setattr(disk_graph, "get_aria_client", lambda: aria)
    monkeypatch.setattr(disk_tool, "get_aria_client", lambda: aria)
    monkeypatch.setattr("subgraphs.vm_disk.executor.time.sleep", lambda s: None)
    agent, _ = build_agent_calling(disk_tool.adjust_vm_disk, {"ip": "10.0.0.60", "extra_disk_gb": 50})
    answers = iter([{"action": "edit", "edits": {"extra_disk_gb": "abc"}},
                    {"action": "edit", "edits": {"boot_disk_gb": "80G", "extra_disk_gb": "100"}}])
    requests, tool_msg = drive(agent, lambda req: next(answers))
    assert len(requests) == 2 and any("整數" in e for e in requests[1]["errors"])
    assert tool_msg.artifact["status"] == "completed"
    assert [(a, i["capacityGb"]) for _, a, i in aria.deployments.submitted] == [(RESIZE_BOOT_DISK, 80), (ADD_DISK, 100)]


def test_background_thread_end_to_end_and_track_tool(lc_graph, monkeypatch):
    """驗證真實背景執行緒：wait_vm_ready → disk_review → submit_disk_op → wait_disk_op → bootstrap_review"""
    aria = FakeAria()
    monkeypatch.setattr(disk_graph, "get_aria_client", lambda: aria)
    monkeypatch.setattr(lc_nodes, "get_aria_client", lambda: aria)
    aria.deployments.dep_status["dep-1"] = [
        ("APPROVAL_PENDING", "CREATE_INPROGRESS"),
        ("INPROGRESS", "CREATE_INPROGRESS"),
        ("SUCCESSFUL", "CREATE_SUCCESSFUL"),
    ]

    job = start_lifecycle_job(
        deployment_id="dep-1",
        deployment_name="proj-demo-AP03",
        project="proj-demo",
        is_linux=True,
        boot_disk_gb=80,
        extra_disk_gb=100,
        auto_start=True,
        graph=lc_graph,
    )
    deadline = time.time() + 5
    while time.time() < deadline and get_lifecycle_job(job.id, graph=lc_graph).stage != DISK_REVIEW:
        time.sleep(0.02)

    assert get_lifecycle_job(job.id, graph=lc_graph).stage == DISK_REVIEW
    calls_after_wait = aria.deployments.get_calls
    assert calls_after_wait == 3

    tracked = json.loads(track_vm_jobs.invoke({"include_finished": False}))
    assert tracked["total"] == 1 and tracked["jobs"][0]["stage"] == DISK_REVIEW

    answers = deque(["a"])
    monkeypatch.setattr(review_mod.Prompt, "ask", staticmethod(lambda *a, **k: answers.popleft() if answers else ""))
    jobs_cli.run_disk_review(job.id, graph=lc_graph)
    assert aria.deployments.get_calls == calls_after_wait   # Resume 不重跑 wait_vm_ready

    deadline = time.time() + 5
    while time.time() < deadline and get_lifecycle_job(job.id, graph=lc_graph).stage != BOOTSTRAP_REVIEW:
        time.sleep(0.02)

    assert get_lifecycle_job(job.id, graph=lc_graph).stage == BOOTSTRAP_REVIEW
    assert [a for _, a, _ in aria.deployments.submitted] == [RESIZE_BOOT_DISK, ADD_DISK]

    answers.extend(["s", ""])
    jobs_cli.run_bootstrap(job.id, graph=lc_graph)
    assert get_lifecycle_job(job.id, graph=lc_graph).stage == DONE
