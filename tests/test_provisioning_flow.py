"""VM Provisioning：Supervisor → 工具 → 子圖 interrupt 冒泡 → 審核恢復 → 送出 的完整流程"""
import pytest

from helpers import build_agent_calling, drive
from subgraphs.vm_provisioning import nodes
from subgraphs.vm_provisioning.state import SpecExtractionResult
from subgraphs.vm_provisioning.tool import deploy_vm_pipeline


class FakeStructured:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def invoke(self, messages):
        self.calls += 1
        return self.result


class FakeLLM:
    def __init__(self, result):
        self.structured = FakeStructured(result)

    def with_structured_output(self, schema):
        return self.structured


class FakeAria:
    def __init__(self):
        self.submitted = []

    def get_os_images(self):
        return {"ubuntu2404": {}, "win2022-cht": {}}

    def get_projects(self, name_filter=None):
        return [{"id": "p-1", "name": "proj-demo", "description": "REQ9/王小明/示範用途"}]

    def get_project_name(self, pid):
        return "proj-demo" if pid == "p-1" else None

    def get_blueprints(self, os_hint=None):
        return [{"id": "bp-1", "name": f"{os_hint} v1.0"}, {"id": "bp-2", "name": f"{os_hint} v2.0"}]

    def get_blueprint_name(self, bid):
        return {"bp-1": "Ubuntu v1.0", "bp-2": "Ubuntu v2.0"}.get(bid)

    def resolve_project(self, ident):
        return {"id": "p-2", "name": "proj-other"} if ident == "proj-other" else None

    def resolve_blueprint(self, ident):
        return None

    def submit_blueprint_request(self, payload):
        self.submitted.append(payload)
        return {"success": True, "request_id": "req-1", "deployment_id": "dep-1",
                "deployment_name": payload["deploymentName"], "status": "CREATED", "created_at": "now"}


@pytest.fixture
def fakes(monkeypatch):
    extraction = SpecExtractionResult(
        extracted_spec={"cpu_count": 4, "memory_size": 8192, "os_image": "Ubuntu 24", "ap_owner": "王小明",
                        "deployment_name": "proj-demo-AP01"},
        project_name_query="proj-demo",
        blueprint_os_query="Ubuntu 24",
        storage_type="SSD",
    )
    llm, aria = FakeLLM(extraction), FakeAria()
    monkeypatch.setattr(nodes, "get_llm", lambda: llm)
    monkeypatch.setattr(nodes, "get_aria_client", lambda: aria)
    return llm, aria


def test_edit_with_error_then_fix_and_submit(fakes):
    llm, aria = fakes
    agent, _ = build_agent_calling(deploy_vm_pipeline, {"requirement_description": "在 proj-demo 開 Ubuntu 24"})
    answers = iter([
        {"action": "edit", "edits": {"cpu_count": "abc"}},                         # 型別錯誤 → 回到審核
        {"action": "edit", "edits": {"cpu_count": "8", "project": "proj-other"}},  # 修正 → 送出
    ])
    requests, tool_msg = drive(agent, lambda req: next(answers))

    assert [r["kind"] for r in requests] == ["vm_deployment", "vm_deployment"]
    first = {f["key"]: f["value"] for f in requests[0]["fields"]}
    assert first["project"] == "proj-demo"
    assert first["case_no"] == "REQ9"                 # 由專案 description 依 policy 帶入
    assert first["datastore"] == "datastore-ssd"      # 存儲類型 SSD → policy 對應
    assert first["network_segment"] == "NET_A01"
    assert first["os_image"] == "ubuntu2404"
    assert requests[0]["errors"] == []
    assert any("cpu_count" in e for e in requests[1]["errors"])

    assert llm.structured.calls == 1                  # 審核恢復不會重新呼叫 LLM
    assert len(aria.submitted) == 1
    inputs = aria.submitted[0]["inputs"]
    assert inputs["cpuCount"] == 8
    assert aria.submitted[0]["projectId"] == "p-2"
    assert aria.submitted[0]["blueprintId"] == "bp-2"  # policy preferred_keyword = v2.0
    assert tool_msg.artifact["status"] == "submitted"
    assert "[status=submitted]" in tool_msg.content


def test_skip(fakes):
    _, aria = fakes
    agent, _ = build_agent_calling(deploy_vm_pipeline, {"requirement_description": "開一台"})
    _, tool_msg = drive(agent, lambda req: {"action": "skip", "feedback": "不需要了"})
    assert aria.submitted == []
    assert tool_msg.artifact["status"] == "aborted"


def test_needs_clarification_persists_session(fakes, monkeypatch):
    llm, _ = fakes
    llm.structured.result = SpecExtractionResult(extracted_spec={"os_image": "Ubuntu 24"})
    monkeypatch.setattr(FakeAria, "get_projects", lambda self, name_filter=None: [])
    agent, _ = build_agent_calling(deploy_vm_pipeline, {"requirement_description": "開一台 Ubuntu"})
    requests, tool_msg = drive(agent, lambda req: {"action": "approve"})
    assert requests == []
    art = tool_msg.artifact
    assert art["status"] == "needs_clarification"
    assert "project_id" in art["missing_fields"]

    from core.persistence import get_app_store
    saved = get_app_store().load_pipeline(art["session_id"])
    assert saved and saved["spec"]["os_image"] == "ubuntu2404"
