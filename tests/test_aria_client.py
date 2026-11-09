import base64
import json
import time

from conftest import FIXTURES
from clients.aria.catalog import CatalogAPI
from clients.aria.deployment import DeploymentAPI
from clients.aria.forms import build_form_fields
from clients.aria.models import CatalogVersion
from clients.aria.session import AriaSession, jwt_expiry


class FakeResponse:
    def __init__(self, status: int, data):
        self.status_code = status
        self._data = data
        self.content = b"x"
        self.text = json.dumps(data)

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeHttp:
    def __init__(self, get_responses=(), post_responses=()):
        self.get_responses = list(get_responses)
        self.post_responses = list(post_responses)
        self.gets, self.posts = [], []

    def post(self, url, **kw):
        if url.endswith("/iaas/api/login"):
            return FakeResponse(200, {"token": _make_jwt(time.time() + 3600)})
        self.posts.append((url, kw.get("json")))
        return self.post_responses.pop(0)

    def get(self, url, **kw):
        self.gets.append((url, kw.get("params")))
        return self.get_responses.pop(0)


def _make_jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def _session(http) -> AriaSession:
    return AriaSession(base_url="https://aria", refresh_token="r", verify_ssl=True, http=http)


def test_jwt_expiry():
    exp = time.time() + 100
    assert abs(jwt_expiry(_make_jwt(exp)) - exp) < 1
    assert jwt_expiry("not-a-jwt") is None


def test_relogin_on_401():
    http = FakeHttp([FakeResponse(401, {}), FakeResponse(200, {"ok": 1})])
    assert _session(http).get("/x") == {"ok": 1}
    assert len(http.gets) == 2


def test_get_all_pages_stops_when_page_param_ignored():
    page = {"content": [{"id": "a"}, {"id": "b"}]}      # 沒有 totalPages / last
    http = FakeHttp([FakeResponse(200, page), FakeResponse(200, page), FakeResponse(200, page)])
    assert [i["id"] for i in _session(http).get_all_pages("/x")] == ["a", "b"]


def test_catalog_submit_request_body_and_result():
    http = FakeHttp(post_responses=[FakeResponse(200, [{"deploymentId": "dep-1", "deploymentName": "n1"}])])
    res = CatalogAPI(_session(http)).submit_request("item-1", "p-1", "n1", "1.0", {"cpuCount": 4}, reason="r")
    url, body = http.posts[0]
    assert url.endswith("/catalog/api/items/item-1/request")
    assert body == {"projectId": "p-1", "deploymentName": "n1", "version": "1.0", "inputs": {"cpuCount": 4}, "reason": "r"}
    assert res.deployment_id == "dep-1"


def test_next_serial():
    deps = {"content": [{"name": "proj-demo-AP01"}, {"name": "proj-demo-ap07"}, {"name": "proj-demo-AP02-old"},
                        {"name": "proj-demo-DB09"}], "last": True}
    http = FakeHttp([FakeResponse(200, deps)])
    assert DeploymentAPI(_session(http)).next_serial("p-1", "proj-demo-AP") == 8


def _fields():
    version_doc = json.loads((FIXTURES / "catalog_version.json").read_text(encoding="utf-8"))
    form = json.loads((FIXTURES / "custom_form.json").read_text(encoding="utf-8"))
    schema = version_doc["schema"]
    version = CatalogVersion(item_id="item-oracle", version="1.0", form_id="form-oracle",
                             properties=schema["properties"], required=schema["required"])
    return {f.id: f for f in build_form_fields(version, form)}


def test_build_form_fields_merges_schema_and_form():
    fields = _fields()
    # 只包含 catalog schema 中的欄位 (表單顯示用的 description / textArea / project 不送出)
    assert "description" not in fields and "textArea_f2231387" not in fields and "project" not in fields
    assert list(fields)[:4] == ["cpuCount", "memorySize", "OSimage", "sg"]       # 依表單版面排序
    mem = fields["memorySize"]
    assert mem.data_type == "integer" and mem.required
    assert mem.option_for("16G").value == 16384
    assert fields["NetworkSegment"].options_source == "com.example/Project_vLAN_List"
    assert fields["NetworkSegment"].default_source == "com.example/Project_vLAN"
    assert fields["IPmode"].default == "DHCP"
    assert fields["DBOwner"].required is False and fields["DBOwner"].default == ""
    addr = fields["address"]
    assert addr.is_visible({"IPmode": "Manual"}) and not addr.is_visible({"IPmode": "DHCP"})
