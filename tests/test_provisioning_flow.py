"""VM Provisioning (catalog 版)：Supervisor → 工具 → 子圖 interrupt 冒泡 → 審核恢復 → catalog 送出"""
import json

import pytest

from conftest import FIXTURES
from clients.aria.forms import build_form_fields
from clients.aria.models import CatalogItem, CatalogRequestResult, CatalogVersion
from helpers import build_agent_calling, drive
from subgraphs.vm_provisioning import nodes, reasoning
from subgraphs.vm_provisioning.state import VMIntent
from subgraphs.vm_provisioning.tool import deploy_vm_pipeline

BASE_INTENT = dict(
    project="proj-demo", os_family="Oracle", os_version="9.6", function="AP", cpu=6, memory_gb=16,
    storage_type="SSD", case_no="REQ1", ap_owner="王小明", os_owner="王小明", ap_unit="資訊服務部",
    description="示範用途",
)


class FakeLLM:
    """VMIntent 依序回傳 intents；選擇題依 choice_answers 回答，並記錄呼叫次數"""

    def __init__(self, intents):
        self.intents = list(intents)
        self.choice_answers = {"TemplateChoice": "[DEMO] Oracle v2.0", "OsImageChoice": "oracle9.6-50G"}
        self.calls: dict[str, int] = {}

    def with_structured_output(self, schema):
        llm = self

        class Runner:
            def invoke(self, messages):
                llm.calls[schema.__name__] = llm.calls.get(schema.__name__, 0) + 1
                if schema is VMIntent:
                    return VMIntent(**llm.intents.pop(0))
                return schema(choice=llm.choice_answers[schema.__name__], reason="依版本選擇")
        return Runner()


class FakeIaas:
    def find_projects(self, q):
        return [{"id": "p-1", "name": "proj-demo"}] if "demo" in q else []

    def get_images(self):
        return {"oracle9.6-50G": {"description": "Oracle 9.6"}, "ubuntu2404": {"description": "Ubuntu 24.04"}}

    def find_machines_by_ip(self, ip):
        return [{"name": "old-vm"}] if ip == "10.0.0.50" else []


class FakeCatalog:
    def __init__(self):
        self.submitted = []
        doc = json.loads((FIXTURES / "catalog_version.json").read_text(encoding="utf-8"))
        self.version = CatalogVersion(item_id="item-oracle", version="1.0", form_id="form-oracle",
                                      properties=doc["schema"]["properties"], required=doc["schema"]["required"])

    def list_items(self, project_id):
        return [CatalogItem(id="item-oracle", name="[DEMO] Oracle v2.0"),
                CatalogItem(id="item-ubuntu", name="[DEMO] Ubuntu v2.0")]

    def get_version(self, item_id, version=None):
        return self.version

    def submit_request(self, item_id, project_id, deployment_name, version, inputs, reason=""):
        self.submitted.append(dict(item_id=item_id, project_id=project_id, deployment_name=deployment_name,
                                   version=version, inputs=inputs, reason=reason))
        return CatalogRequestResult(deployment_id="dep-1", deployment_name=deployment_name)


class FakeForms:
    def fields_for(self, version):
        form = json.loads((FIXTURES / "custom_form.json").read_text(encoding="utf-8"))
        return build_form_fields(version, form)


class FakeDeployments:
    def next_serial(self, project_id, prefix):
        return 3 if prefix == "proj-demo-AP" else 1


class FakeAria:
    def __init__(self):
        self.iaas, self.catalog, self.forms, self.deployments = FakeIaas(), FakeCatalog(), FakeForms(), FakeDeployments()


@pytest.fixture
def aria(monkeypatch):
    fake = FakeAria()
    monkeypatch.setattr(nodes, "get_aria_client", lambda: fake)
    return fake


def _use_llm(monkeypatch, intents) -> FakeLLM:
    llm = FakeLLM(intents)
    monkeypatch.setattr(reasoning, "get_llm", lambda: llm)
    return llm


def test_full_flow_edit_static_ip_then_submit(aria, monkeypatch):
    llm = _use_llm(monkeypatch, [BASE_INTENT])
    agent, _ = build_agent_calling(deploy_vm_pipeline, {"requirement_description": "在 proj-demo 開 Oracle 9.6"})
    requests, tool_msg = drive(agent, lambda req: {"action": "edit", "edits": {"address": "10.0.0.50"}})

    assert len(requests) == 1                          # Edit 後驗證通過直接送出
    fields = {f["key"]: f for f in requests[0]["fields"]}
    assert fields["template"]["value"] == "[DEMO] Oracle v2.0 (v1.0)"
    assert fields["deploymentName"]["value"] == "proj-demo-AP03"
    assert fields["OSimage"]["value"].startswith("Oracle 9.6") and fields["OSimage"]["source"] == "模型選擇"
    assert fields["memorySize"]["value"] == "16G (16384)"
    assert fields["Datastore"]["value"] == "SSD (vSAN) (datastore-ssd)"
    assert fields["NetworkSegment"]["value"] == "NET_A01"
    assert fields["Cluster"]["value"].startswith("Cluster08")
    assert "address" not in fields                     # DHCP 時不顯示
    assert "16G" in fields["memorySize"]["options"]
    assert any("CPU" in n and "不在表單選項內" in n for n in requests[0]["notes"])

    sub = aria.catalog.submitted[0]
    assert sub["item_id"] == "item-oracle" and sub["project_id"] == "p-1" and sub["version"] == "1.0"
    assert sub["deployment_name"] == "proj-demo-AP03"
    inputs = sub["inputs"]
    assert inputs["IPmode"] == "Manual" and inputs["address"] == "10.0.0.50"
    assert inputs["cpuCount"] == 6 and inputs["memorySize"] == 16384
    assert inputs["Datastore"] == "datastore-ssd" and inputs["sg"] == "ap" and inputs["DBOwner"] == ""
    assert "description" not in inputs and "project" not in inputs
    assert tool_msg.artifact["status"] == "submitted"
    # 審核修改後重新解析時，模型的選擇沿用快取
    assert llm.calls == {"VMIntent": 1, "TemplateChoice": 1, "OsImageChoice": 1}


def test_needs_clarification_then_continue(aria, monkeypatch):
    first = {k: v for k, v in BASE_INTENT.items() if k != "case_no"}
    _use_llm(monkeypatch, [first, {"case_no": "REQ2"}])
    agent, _ = build_agent_calling(deploy_vm_pipeline, {"requirement_description": "開一台 Oracle"})
    requests, tool_msg = drive(agent, lambda req: {"action": "approve"})
    assert requests == []
    art = tool_msg.artifact
    assert art["status"] == "needs_clarification"
    assert any("CaseNo" in m for m in art["missing"])

    agent2, _ = build_agent_calling(deploy_vm_pipeline,
                                    {"requirement_description": "單號 REQ2", "session_id": art["session_id"]})
    requests2, tool_msg2 = drive(agent2, lambda req: {"action": "approve"}, thread_id="t2")
    assert len(requests2) == 1
    assert {f["key"]: f["value"] for f in requests2[0]["fields"]}["CaseNo"] == "REQ2"
    assert aria.catalog.submitted[0]["inputs"]["APOwner"] == "王小明"   # 先前抽取的意圖有延續
    assert tool_msg2.artifact["status"] == "submitted"


def test_invalid_edit_returns_to_review_then_skip(aria, monkeypatch):
    _use_llm(monkeypatch, [BASE_INTENT])
    agent, _ = build_agent_calling(deploy_vm_pipeline, {"requirement_description": "開一台"})
    answers = iter([{"action": "edit", "edits": {"memorySize": "abc", "foo": "1"}}, {"action": "skip"}])
    requests, tool_msg = drive(agent, lambda req: next(answers))
    assert len(requests) == 2
    errors = "\n".join(requests[1]["errors"])
    assert "memorySize" in errors and "foo" in errors
    assert aria.catalog.submitted == []
    assert tool_msg.artifact["status"] == "aborted"


def test_edit_memory_by_label_and_template(aria, monkeypatch):
    _use_llm(monkeypatch, [BASE_INTENT])
    agent, _ = build_agent_calling(deploy_vm_pipeline, {"requirement_description": "開一台"})
    drive(agent, lambda req: {"action": "edit", "edits": {"memorySize": "32G", "template": "ubuntu"}})
    sub = aria.catalog.submitted[0]
    assert sub["item_id"] == "item-ubuntu"
    assert sub["inputs"]["memorySize"] == 32768
