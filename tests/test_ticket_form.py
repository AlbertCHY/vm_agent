import json

from conftest import FIXTURES
from clients.ollama_client import OllamaClient
from parsers.ticket_form import preprocess_ticket
from parsers.ticket_parser import parse_ticket


def test_preprocess_sample_ticket():
    info = preprocess_ticket((FIXTURES / "sample_ticket.txt").read_text(encoding="utf-8"))
    assert info["單號"] == "REQ26010100001"
    assert info["申請人"] == "王小明"
    assert info["申請人工號"] == "A0000001"
    assert info["申請單位"] == "資訊服務處"
    assert info["專案名稱"] == "proj-demo"
    assert info["專案背景"] == "示範專案部署"
    # 「自動跳過」的簽核意見需被過濾
    assert info["審核覆寫意見"] == ["請先以地端資源開立", '請確認兩台規格為"其他"的正確資訊']
    assert info["資源總計"] == {"VM總數": 2, "vCore總數": 12, "Memory總數GB": 24, "HDD總數GB": 0, "SSD總數GB": 300}
    assert info["VM表格"].startswith("Ubuntu24")
    assert "前往" not in info["VM表格"]


def test_preprocess_empty_input():
    info = preprocess_ticket("")
    assert info["單號"] == "未提供"
    assert info["申請類別"] == "未提供"
    assert info["資源總計"]["VM總數"] == 1


def test_preprocess_non_form_text():
    info = preprocess_ticket("請幫我開一台 Ubuntu VM")
    assert info["單號"] == "未提供"
    assert info["審核覆寫意見"] == []


def test_parse_ticket_uses_form_fallback_and_structured_hint():
    captured: dict[str, str] = {}

    class FakeOllama(OllamaClient):
        def generate_json(self, system: str, prompt: str, schema=None) -> str:
            captured["prompt"] = prompt
            return json.dumps({
                "ticket_id": "",
                "applicant": "",
                "department": "",
                "ticket_type": "CREATE_VM",
                "summary": "開立測試主機",
                "approval_notes": "",
                "action_items": [{"vm_name": "proj-demo-AP01", "cpu": 4}],
            })

    raw = (FIXTURES / "sample_ticket.txt").read_text(encoding="utf-8")
    ticket = parse_ticket(raw, source_name="sample_ticket.txt", ollama_client=FakeOllama())
    assert "【資源管理人】" in captured["prompt"]
    assert ticket.ticket_id == "REQ26010100001"
    assert ticket.applicant == "王小明"
    assert ticket.department == "資訊服務處"
    assert ticket.ticket_type == "CREATE_VM"
    assert "請先以地端資源開立" in (ticket.approval_notes or "")
    assert ticket.action_items[0]["description"] == "vm_name: proj-demo-AP01，cpu: 4"


def test_resolve_ticket_file_blocks_path_traversal_and_resolves_ext(tmp_path, monkeypatch):
    import pytest
    import config
    from tools.ticket_tools import resolve_ticket_file

    monkeypatch.setattr(config, "TICKETS_DIR", tmp_path)
    sample = tmp_path / "REQ001.txt"
    sample.write_text("hello", encoding="utf-8")

    # 省略副檔名可自動補全
    assert resolve_ticket_file("REQ001") == sample.resolve()
    # 路徑遍歷攻擊必須拒絕
    with pytest.raises(ValueError, match="不允許存取"):
        resolve_ticket_file("../.env")
    # 找不到檔案應拋出 FileNotFoundError
    with pytest.raises(FileNotFoundError):
        resolve_ticket_file("NOT_EXIST")


def test_parse_ticket_file_caches_by_mtime(tmp_path, monkeypatch):
    from schemas import ParsedTicket
    import tools.ticket_tools as ticket_tools

    ticket_tools._PARSE_CACHE.clear()
    f = tmp_path / "t1.txt"
    f.write_text("開一台 Ubuntu", encoding="utf-8")

    calls = {"n": 0}

    def fake_parse(raw_text: str, source_name=None):
        calls["n"] += 1
        return ParsedTicket(
            ticket_id="REQ1", applicant="A", department="D",
            ticket_type="CREATE_VM", summary=f"call-{calls['n']}", action_items=[],
        )

    monkeypatch.setattr(ticket_tools, "parse_ticket", fake_parse)
    first = ticket_tools.parse_ticket_file(f)
    second = ticket_tools.parse_ticket_file(f)
    assert calls["n"] == 1 and first.summary == second.summary == "call-1"
