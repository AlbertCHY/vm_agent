"""統一背景工作 (/jobs) 整合測試：
1. 三個獨立可背景執行子圖 (vm_build / vm_disk / vm_bootstrap) 與 next_steps 串接
2. 驗證背景執行時 emit() 累積寫入 get_job_logs(job_id)，不搶主畫面輸出，完成/失敗時推播通知
3. 驗證現有 VM 的 adjust_vm_disk 前景審核後進背景，以及 follow_up_bootstrap=True 自動串接初始設定
4. 驗證 Add Disk 冪等防呆 (不重複送出) 與連續查詢失敗上限停止重試
5. 驗證 Bootstrap 步驟失敗後自動切入 RETRY 保留已完成步驟，於 /jobs 續跑時從失敗步驟接續
6. 驗證真實背景執行緒端對端流程 (vm_build → vm_disk → vm_bootstrap)
"""
import time

import pytest
from langgraph.checkpoint.memory import InMemorySaver

import cli.jobs as jobs_cli
from core.persistence import AppStore
from helpers import build_agent_calling, drive
from subgraphs.common.runner import MAX_CONSECUTIVE_POLL_ERRORS, drain_notices, wait_for_background
from subgraphs.jobs import (
    DONE,
    FAILED,
    KIND_BOOTSTRAP,
    KIND_BUILD,
    KIND_DISK,
    RETRY,
    REVIEW,
    WAITING,
    cancel_job,
    get_job,
    get_job_graph,
    get_job_logs,
    job_config,
    list_jobs,
    start_build_job,
)
from subgraphs.vm_build import nodes as build_nodes
from subgraphs.vm_disk import nodes as disk_nodes
from subgraphs.vm_disk.executor import ADD_DISK, RESIZE_BOOT_DISK
from subgraphs.vm_disk.tool import adjust_vm_disk
from test_bootstrap_flow import fakes  # noqa: F401


class FakeDeployments:
    def __init__(self):
        self.dep_status: dict[str, list[tuple[str, str]]] = {}
        self._resources: dict[str, list[dict]] = {
            "dep-1": [
                {"id": "m-1", "type": "Cloud.vSphere.Machine", "name": "proj-demo-AP01",
                 "properties": {"address": "10.0.0.99", "resourceName": "proj-demo-AP01"}},
            ]
        }
        self.actions = [
            {"id": RESIZE_BOOT_DISK, "valid": True},
            {"id": ADD_DISK, "valid": True},
        ]
        self.submitted: list[tuple[str, str, dict]] = []
        self.req_status: dict[str, list[str]] = {}

    def get(self, dep_id, expand=None):
        seq = self.dep_status.get(dep_id) or [("SUCCESSFUL", "CREATE_SUCCESSFUL")]
        r_status, d_status = seq.pop(0) if len(seq) > 1 else seq[0]
        return {"id": dep_id, "status": d_status, "lastRequest": {"status": r_status}}

    def resources(self, dep_id):
        return list(self._resources.get(dep_id, []))

    def resource_actions(self, machine_id):
        return list(self.actions)

    def resource_action(self, machine_id, action_id):
        return {"schema": {"properties": {"capacityGb": {"default": 100}}}}

    def run_resource_action(self, machine_id, action_id, inputs, reason=""):
        self.submitted.append((machine_id, action_id, dict(inputs)))
        return {"id": f"req-{len(self.submitted)}"}

    def get_request(self, request_id):
        seq = self.req_status.get(request_id) or ["SUCCESSFUL"]
        status = seq.pop(0) if len(seq) > 1 else seq[0]
        return {"id": request_id, "status": status}


class FakeIaas:
    def find_machines_by_ip(self, ip):
        return [{"id": "iaas-m-1", "deploymentId": "dep-1", "name": "proj-demo-AP01", "address": ip}]


class FakeAria:
    def __init__(self):
        self.deployments = FakeDeployments()
        self.iaas = FakeIaas()

    def query_vm_by_ip(self, ip):
        return {
            "found": True,
            "deployment": {"id": "dep-1", "name": "proj-demo-AP01"},
            "machine": {"id": "m-1", "name": "proj-demo-AP01", "address": ip, "os_type": "Ubuntu_24"},
        }


@pytest.fixture
def job_env(tmp_path, monkeypatch):
    store = AppStore(tmp_path / "app.sqlite")
    cp = InMemorySaver()
    monkeypatch.setattr("core.persistence.get_app_store", lambda: store)
    monkeypatch.setattr("subgraphs.jobs.manager.get_app_store", lambda: store)
    monkeypatch.setattr("subgraphs.jobs.manager.get_checkpointer", lambda: cp)
    from subgraphs.jobs import manager as jm
    jm._default_bg_graph.cache_clear()
    monkeypatch.setattr(build_nodes.config, "JOB_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(disk_nodes.config, "JOB_FAST_POLL_INTERVAL", 0.01)
    drain_notices()
    yield cp
    jm._default_bg_graph.cache_clear()


def test_build_job_chains_to_disk_and_bootstrap_in_background(job_env, fakes, monkeypatch):  # noqa: F811
    """端對端驗證真實背景執行緒：vm_build → (自動串接) vm_disk 審核 + 背景執行 → (自動串接) vm_bootstrap 審核 + 背景執行"""
    ssh, _ = fakes
    aria = FakeAria()
    monkeypatch.setattr(build_nodes, "get_aria_client", lambda: aria)
    monkeypatch.setattr(disk_nodes, "get_aria_client", lambda: aria)

    aria.deployments.dep_status["dep-1"] = [
        ("APPROVAL_PENDING", "CREATE_INPROGRESS"),
        ("INPROGRESS", "CREATE_INPROGRESS"),
        ("SUCCESSFUL", "CREATE_SUCCESSFUL"),
    ]
    aria.deployments.req_status["req-1"] = ["INPROGRESS", "SUCCESSFUL"]
    aria.deployments.req_status["req-2"] = ["SUCCESSFUL"]

    # 1. 建立 vm_build 背景工作 (附帶 boot+extra disk 與 bootstrap 串接)
    build_job = start_build_job(
        deployment_id="dep-1",
        deployment_name="proj-demo-AP01",
        is_linux=True,
        boot_disk_gb=150,
        extra_disk_gb=200,
        auto_start=True,
    )
    assert wait_for_background(build_job.id, timeout=5.0)

    # vm_build 完成後應自動封存並長出 vm_disk (REVIEW)
    active = list_jobs()
    assert len(active) == 1
    disk_job = active[0]
    assert disk_job.kind == KIND_DISK and disk_job.stage == REVIEW and disk_job.ip == "10.0.0.99"
    assert any("硬碟調整" in n for n in drain_notices())

    # 2. 進 /jobs 審核硬碟調整 (核准後立刻進背景執行)
    monkeypatch.setattr(jobs_cli, "handle_review", lambda req: {"action": "approve", "edits": {}, "feedback": ""})
    jobs_cli.run_job_review(disk_job.id)
    assert wait_for_background(disk_job.id, timeout=5.0)
    assert [a for _, a, _ in aria.deployments.submitted] == [RESIZE_BOOT_DISK, ADD_DISK]

    # vm_disk 完成後自動封存並長出 vm_bootstrap (REVIEW)
    active = list_jobs()
    assert len(active) == 1
    boot_job = active[0]
    assert boot_job.kind == KIND_BOOTSTRAP and boot_job.stage == REVIEW and boot_job.os_type == "linux"
    assert any("初始設定" in n for n in drain_notices())

    # 3. 進 /jobs 審核初始設定 (核准後立刻進背景逐步執行並累積輸出)
    monkeypatch.setattr(
        jobs_cli,
        "handle_review",
        lambda req: {"action": "approve", "edits": {"cs_mode": "skip", "skip_disk": "true"}, "feedback": ""},
    )
    jobs_cli.run_job_review(boot_job.id)
    assert wait_for_background(boot_job.id, timeout=5.0)

    done_job = get_job(boot_job.id)
    assert done_job is not None and done_job.stage == DONE
    assert any("初始設定完成" in n for n in drain_notices())
    logs = get_job_logs(boot_job.id)
    assert any("set_hostname.sh" in ln for ln in logs)
    assert [c["cmd"].rsplit("/", 1)[-1] for c in ssh.calls] == [
        "add_pub_key.sh", "set_no_passwd.sh", "set_hostname.sh",
        "set_passwords.sh", "set_zabbix.sh", "remover.sh",
    ]


def test_existing_vm_adjust_disk_runs_in_background_and_chains_bootstrap(job_env, fakes, monkeypatch):  # noqa: F811
    """對現有 VM 呼叫 adjust_vm_disk(follow_up_bootstrap=True)：前景審核 → 背景執行硬碟 → 自動長出 Bootstrap 待審核"""
    aria = FakeAria()
    monkeypatch.setattr(disk_nodes, "get_aria_client", lambda: aria)
    aria.deployments.req_status["req-1"] = ["INPROGRESS", "SUCCESSFUL"]

    agent, _ = build_agent_calling(
        adjust_vm_disk,
        {"ip": "10.0.0.99", "extra_disk_gb": 200, "follow_up_bootstrap": True, "os_type": "windows"},
    )
    requests, tool_msg = drive(agent, lambda req: {"action": "approve"})
    assert len(requests) == 1
    assert tool_msg.artifact["status"] == "submitted"
    disk_job_id = tool_msg.artifact["job_id"]
    assert wait_for_background(disk_job_id, timeout=5.0)
    assert [a for _, a, _ in aria.deployments.submitted] == [ADD_DISK]

    # 硬碟完成後應自動長出 Windows Bootstrap 待審核工作進 /jobs
    active = list_jobs()
    assert len(active) == 1
    boot_job = active[0]
    assert boot_job.kind == KIND_BOOTSTRAP and boot_job.stage == REVIEW and boot_job.os_type == "windows"
    assert boot_job.ip == "10.0.0.99"


def test_add_disk_idempotency_skips_duplicate_submission(job_env, monkeypatch):
    """Add Disk 送出前偵測到同名硬碟已存在於 Aria 時應冪等略過，不重複送出"""
    aria = FakeAria()
    aria.deployments._resources["dep-1"].append(
        {"id": "d-existing", "type": "Cloud.vSphere.Disk", "name": "proj-demo-AP01-disk2-123456"}
    )
    monkeypatch.setattr(disk_nodes, "get_aria_client", lambda: aria)

    agent, _ = build_agent_calling(adjust_vm_disk, {"ip": "10.0.0.99", "extra_disk_gb": 100})
    _, tool_msg = drive(agent, lambda req: {"action": "approve"})
    job_id = tool_msg.artifact["job_id"]
    assert wait_for_background(job_id, timeout=5.0)
    assert aria.deployments.submitted == []
    assert get_job(job_id).stage == DONE
    assert any("略過重複送出" in ln for ln in get_job_logs(job_id))


def test_build_job_windows_chains_windows_bootstrap(job_env, monkeypatch):
    """Windows VM 建機完成 (無額外硬碟) 應自動串接 os_type=windows 的 vm_bootstrap 待審核"""
    aria = FakeAria()
    monkeypatch.setattr(build_nodes, "get_aria_client", lambda: aria)
    aria.deployments.dep_status["dep-1"] = [("SUCCESSFUL", "CREATE_SUCCESSFUL")]

    win = start_build_job(deployment_id="dep-1", deployment_name="proj-win-01", is_linux=False, auto_start=True)
    assert wait_for_background(win.id, timeout=5.0)

    active = list_jobs()
    assert len(active) == 1
    assert active[0].kind == KIND_BOOTSTRAP and active[0].stage == REVIEW and active[0].os_type == "windows"


def test_build_failure_overdue_and_consecutive_errors(job_env, monkeypatch):
    """驗證 vm_build 逾時提醒、建立失敗與連續查詢失敗達上限時暫停並推播通知"""
    aria = FakeAria()
    monkeypatch.setattr(build_nodes, "get_aria_client", lambda: aria)
    aria.deployments.dep_status["dep-1"] = [
        ("APPROVAL_PENDING", "CREATE_INPROGRESS"),
        ("APPROVAL_REJECTED", "CREATE_FAILED"),
    ]
    job = start_build_job(deployment_id="dep-1", deployment_name="proj-fail-01", auto_start=False)
    g = get_job_graph(KIND_BUILD)
    g.update_state(job_config(job.id), {"stage_since": time.time() - 25 * 3600}, as_node="init_build")
    g.invoke(None, config=job_config(job.id))
    notices = drain_notices()
    assert any("超過 24 小時" in n for n in notices)
    assert any("失敗" in n for n in notices)
    assert get_job(job.id).stage == FAILED

    # 連續查詢失敗達上限
    calls = {"n": 0}

    def broken_get(dep_id, expand=None):
        calls["n"] += 1
        raise RuntimeError("API unreachable")

    aria.deployments.get = broken_get
    job2 = start_build_job(deployment_id="dep-2", deployment_name="proj-err-02", auto_start=False)
    with pytest.raises(RuntimeError):
        g.invoke(None, config=job_config(job2.id))
    assert calls["n"] == MAX_CONSECUTIVE_POLL_ERRORS
    assert any("連續查詢失敗" in n for n in drain_notices())


def test_bootstrap_background_failure_retries_from_failed_step(job_env, fakes, monkeypatch):  # noqa: F811
    """背景 Bootstrap 在 set_hostname 失敗後自動切回 RETRY 保留已完成步驟，/jobs 續跑從該步接續"""
    ssh, _ = fakes
    orig_exec = ssh.exec_command
    should_fail = {"on": True, "failed_attempts": 0}

    def flaky(ip, cmd, **kw):
        if cmd.endswith("set_hostname.sh") and should_fail["on"]:
            should_fail["failed_attempts"] += 1
            return 1, "", "hostnamectl failed"
        return orig_exec(ip, cmd, **kw)

    ssh.exec_command = flaky

    from subgraphs.jobs import enqueue_bootstrap_job
    job = enqueue_bootstrap_job(ip="10.0.0.99", hostname="proj-demo-AP01", os_type="linux",
                                cs_mode="SKIP", skip_disk=True, needs_review=True)
    monkeypatch.setattr(jobs_cli, "handle_review", lambda req: {"action": "approve", "edits": {}, "feedback": ""})
    jobs_cli.run_job_review(job.id)
    assert wait_for_background(job.id, timeout=5.0)

    # 失敗後應自動進入 RETRY，保留前面成功的 add_pub_key / set_no_passwd，不重跑它們
    j_retry = get_job(job.id)
    assert j_retry is not None and j_retry.stage == RETRY and j_retry.step_index == 2
    assert should_fail["failed_attempts"] >= 1
    assert any("初始設定未完成" in n for n in drain_notices())
    assert any("hostnamectl failed" in ln for ln in get_job_logs(job.id))

    # 修復後從斷點續跑 (背景)
    should_fail["on"] = False
    jobs_cli.run_job_retry(job.id)
    assert wait_for_background(job.id, timeout=5.0)

    j_done = get_job(job.id)
    assert j_done is not None and j_done.stage == DONE
    executed = [c["cmd"].rsplit("/", 1)[-1] for c in ssh.calls]
    assert executed.count("add_pub_key.sh") == 1
    assert executed.count("set_no_passwd.sh") == 1
    assert executed.count("set_hostname.sh") == 1
    assert executed[-1] == "remover.sh"


def test_cancel_job_archives_and_hides_from_list(job_env):
    job = start_build_job(deployment_id="dep-1", deployment_name="proj-cancel", auto_start=False)
    assert job.stage == WAITING
    assert any(j.id == job.id for j in list_jobs())
    assert cancel_job(job.id) is True
    assert all(j.id != job.id for j in list_jobs())


def test_skip_or_zero_disk_in_chain_still_spawns_bootstrap(job_env, monkeypatch):
    """串接中硬碟審核按 [S] 略過或將大小改為 0 後，仍須正確交棒產生 vm_bootstrap；無後續時顯示已略過"""
    from subgraphs.jobs import complete_job, enqueue_disk_job

    aria = FakeAria()
    monkeypatch.setattr(disk_nodes, "get_aria_client", lambda: aria)

    # 1. 串接流程在 vm_disk 審核按 [S] 略過 -> 封存 vm_disk 並長出 vm_bootstrap (保留 os_type)
    disk_skip = enqueue_disk_job(
        deployment_id="dep-1",
        deployment_name="proj-skip-01",
        ip="10.0.0.99",
        vm_name="proj-skip-01",
        machine_id="m-1",
        boot_disk_gb=150,
        os_type="windows",
        next_steps=[{"kind": KIND_BOOTSTRAP, "os_type": "windows"}],
        needs_review=True,
        auto_start=False,
    )
    monkeypatch.setattr(jobs_cli, "handle_review", lambda req: {"action": "skip", "edits": {}, "feedback": "略過"})
    jobs_cli.run_job_review(disk_skip.id)

    active = list_jobs()
    assert len(active) == 1
    assert active[0].kind == KIND_BOOTSTRAP and active[0].stage == REVIEW and active[0].os_type == "windows"
    assert any("已略過硬碟調整" in n for n in drain_notices())
    cancel_job(active[0].id)

    # 2. 串接流程在 vm_disk 審核將所有硬碟大小改為 0 (no_ops) -> 同樣交棒產生 vm_bootstrap
    disk_zero = enqueue_disk_job(
        deployment_id="dep-1",
        deployment_name="proj-zero-02",
        ip="10.0.0.99",
        vm_name="proj-zero-02",
        machine_id="m-1",
        boot_disk_gb=150,
        os_type="linux",
        next_steps=[{"kind": KIND_BOOTSTRAP, "os_type": "linux"}],
        needs_review=True,
        auto_start=False,
    )
    answers = iter([
        {"action": "edit", "edits": {"boot_disk_gb": "0"}, "feedback": ""},
        {"action": "approve", "edits": {}, "feedback": ""},
    ])
    monkeypatch.setattr(jobs_cli, "handle_review", lambda req: next(answers))
    jobs_cli.run_job_review(disk_zero.id)

    active2 = list_jobs()
    assert len(active2) == 1
    assert active2[0].kind == KIND_BOOTSTRAP and active2[0].stage == REVIEW and active2[0].os_type == "linux"
    assert any("無需硬碟調整" in n for n in drain_notices())
    cancel_job(active2[0].id)

    # 3. 無後續步驟的獨立 vm_disk 被略過 -> 標籤應為「已略過硬碟調整」，而非「硬碟調整完成」
    standalone_disk = enqueue_disk_job(
        deployment_id="dep-1",
        deployment_name="proj-alone-03",
        ip="10.0.0.99",
        vm_name="proj-alone-03",
        machine_id="m-1",
        extra_disk_gb=100,
        needs_review=True,
        auto_start=False,
    )
    monkeypatch.setattr(jobs_cli, "handle_review", lambda req: {"action": "skip", "edits": {}, "feedback": ""})
    jobs_cli.run_job_review(standalone_disk.id)
    done_disk = get_job(standalone_disk.id)
    assert done_disk is not None and done_disk.stage == DONE and done_disk.stage_label == "已略過硬碟調整"
    assert complete_job(standalone_disk.id) is None


def test_cancel_slow_bootstrap_stops_before_next_step_and_releases_lock(job_env, fakes, monkeypatch):  # noqa: F811
    """長耗時 Bootstrap 步驟執行中途按 [X] 停止：不得清空取消旗標，須攔截後續步驟並在執行緒收尾後自動封存與釋放鎖"""
    import threading
    from subgraphs.common import runner
    from subgraphs.jobs import enqueue_bootstrap_job, is_cancelling

    ssh, _ = fakes
    orig_exec = ssh.exec_command
    step1_started = threading.Event()
    allow_step1_finish = threading.Event()

    def slow_step1(ip, cmd, **kw):
        if cmd.endswith("add_pub_key.sh"):
            step1_started.set()
            allow_step1_finish.wait(timeout=5.0)
        return orig_exec(ip, cmd, **kw)

    ssh.exec_command = slow_step1

    job = enqueue_bootstrap_job(
        ip="10.0.0.99", hostname="proj-slow-01", os_type="linux",
        cs_mode="SKIP", skip_disk=True, needs_review=True,
    )
    monkeypatch.setattr(jobs_cli, "handle_review", lambda req: {"action": "approve", "edits": {}, "feedback": ""})
    jobs_cli.run_job_review(job.id)

    assert step1_started.wait(timeout=5.0)
    # 超過 2 秒等待門檻（此處設 0.05s 模擬）：應延遲封存、保持取消狀態與顯示「停止中...」
    assert cancel_job(job.id, wait_timeout=0.05) is False
    assert is_cancelling(job.id) is True
    cur = get_job(job.id)
    assert cur is not None and cur.stage_label == "停止中..."

    # 放行第 1 步，背景執行緒應立即攔截第 2 步並完成自動封存與鎖釋放
    allow_step1_finish.set()
    assert wait_for_background(job.id, timeout=5.0)

    executed = [c["cmd"].rsplit("/", 1)[-1] for c in ssh.calls]
    assert executed == ["add_pub_key.sh"]
    assert all(j.id != job.id for j in list_jobs())
    assert job.id not in runner._TASK_LOCKS
    assert any("已停止並移除" in n for n in drain_notices())


def test_chained_step_merge_ignores_empty_values(job_env, monkeypatch):
    """串接交棒時，next_steps 內的空字串 os_type 不應覆蓋上一步 carried 的有效 os_type"""
    from subgraphs.jobs.manager import _spawn_chained_step

    prev = start_build_job(deployment_id="dep-1", deployment_name="proj-merge-01", is_linux=False, auto_start=False)
    nxt = _spawn_chained_step(
        prev.id,
        [{"kind": KIND_BOOTSTRAP, "os_type": "", "cs_mode": "MDR"}],
        carried={
            "deployment_id": "dep-1",
            "deployment_name": "proj-merge-01",
            "ip": "10.0.0.88",
            "os_type": "windows",
        },
    )
    assert nxt is not None and nxt.os_type == "windows"


def test_progress_contextvar_propagates_to_child_thread(job_env):
    """透過 contextvars 傳遞的背景 task_id 在子執行緒中仍可透過 emit() 寫入累計輸出"""
    import contextvars
    import threading
    from core.progress import bind_background_task, emit

    tid = "ctx-test-01"
    with bind_background_task(tid):
        ctx = contextvars.copy_context()
        t = threading.Thread(target=lambda: ctx.run(emit, "child-thread-log"))
        t.start()
        t.join(timeout=2.0)

    logs = get_job_logs(tid)
    assert any("child-thread-log" in ln for ln in logs)


def test_count_jobs_in_stages_uses_cached_stage_without_reading_checkpoints(job_env, monkeypatch):
    """count_jobs_in_stages 應直接從 DB stage 快取統計，不重讀每個工作的 LangGraph Checkpoint"""
    from subgraphs.jobs import REVIEW_STAGES, count_jobs_in_stages, enqueue_disk_job, manager as jm

    start_build_job(deployment_id="dep-1", deployment_name="proj-cache-01", auto_start=False)
    enqueue_disk_job(
        deployment_id="dep-1",
        deployment_name="proj-cache-02",
        ip="10.0.0.99",
        needs_review=True,
        auto_start=False,
    )

    def fail_if_called(*a, **kw):
        raise AssertionError("should not read checkpoint when stage cache is populated")

    monkeypatch.setattr(jm, "get_job_graph", fail_if_called)
    assert count_jobs_in_stages(REVIEW_STAGES) == 1
    assert count_jobs_in_stages((WAITING,)) == 1


def test_custom_job_spec_works_without_modifying_manager_or_cli(job_env, monkeypatch):
    """透過 register_job_spec 註冊第 4 種客製工作後，JobManager 與 CLI 可直接支援建圖、串接、明細表與啟動恢復"""
    from langgraph.graph import END, START, StateGraph
    from typing_extensions import TypedDict
    from core.persistence import get_app_store
    from subgraphs.jobs import (
        CompletionOutcome,
        JobSpec,
        complete_job,
        get_job_spec,
        job_config,
        register_job_spec,
        registry,
        resume_active_jobs,
        spawn_job_background,
        thread_id_for,
    )

    class DemoState(TypedDict, total=False):
        ip: str
        done_ok: bool

    def build_demo_graph(cp):
        b = StateGraph(DemoState)
        b.add_node("run_demo", lambda s: {"done_ok": True, "ip": s.get("ip") or "10.0.0.77"})
        b.add_edge(START, "run_demo")
        b.add_edge("run_demo", END)
        return b.compile(checkpointer=cp, interrupt_before=["run_demo"])

    custom_kind = "vm_custom_demo"
    spec = JobSpec(
        kind=custom_kind,
        label="客製快照",
        build_graph=build_demo_graph,
        derive_stage=lambda nxt, vals, meta, intr, bg: (DONE, "快照完成") if not nxt else (WAITING, "快照排程中"),
        evaluate_completion=lambda jid, title, vals, meta: CompletionOutcome(
            status="succeeded",
            carried={"ip": vals.get("ip") or "10.0.0.77", "deployment_name": title, "os_type": "linux"},
            log_message="客製快照完成",
            chain_notice_prefix=f"{title} 客製快照完成",
            done_notice=f"✅ {title} 客製快照完成",
        ),
        active_stage=WAITING,
        auto_resume_on_startup=True,
        detail_table=lambda job: ("📸 快照明細", ("項目", "狀態"), [(job.title, job.stage_label)]),
    )

    try:
        register_job_spec(spec)
        assert get_job_spec(custom_kind).label == "客製快照"

        jid = "demo0001"
        tid = thread_id_for(jid)
        get_app_store().save_job(
            jid, tid, custom_kind, "proj-demo-snap",
            meta={"next_steps": [{"kind": KIND_BOOTSTRAP}]},
            stage=WAITING,
        )
        g = get_job_graph(custom_kind)
        g.update_state(job_config(tid), {"ip": "10.0.0.77"}, as_node=START)

        # 1. CLI render_job_detail 能直接渲染 JobSpec.detail_table
        job_before = get_job(jid)
        assert job_before is not None and job_before.kind_label == "客製快照"
        jobs_cli.render_job_detail(job_before)

        # 2. resume_active_jobs 依 auto_resume_on_startup=True 自動啟動背景並完成串接
        spawned: list[str] = []
        orig_spawn = spawn_job_background

        def track_and_run(target_id, checkpointer=None):
            spawned.append(target_id)
            orig_spawn(target_id, checkpointer=checkpointer)

        from subgraphs.jobs import manager as jm
        monkeypatch.setattr(jm, "spawn_job_background", track_and_run)
        resume_active_jobs()
        assert spawned == [jid]
        assert wait_for_background(jid, timeout=5.0)

        # 完成後自動封存客製工作並串接出下一筆 vm_bootstrap (REVIEW)
        active = list_jobs()
        assert len(active) == 1
        assert active[0].kind == KIND_BOOTSTRAP and active[0].stage == REVIEW and active[0].ip == "10.0.0.77"
        assert any("客製快照完成" in n for n in drain_notices())
        assert complete_job(jid) is None
    finally:
        registry._REGISTRY.pop(custom_kind, None)
