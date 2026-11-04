from conftest import FIXTURES
from parsers.ticket_form import preprocess_ticket


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
    assert info["資源總計"]["VM總數"] == 1


def test_preprocess_non_form_text():
    info = preprocess_ticket("請幫我開一台 Ubuntu VM")
    assert info["單號"] == "未提供"
    assert info["審核覆寫意見"] == []
