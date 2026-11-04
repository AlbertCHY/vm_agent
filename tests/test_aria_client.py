import base64
import json
import time

from clients.aria_client import AriaAutomationClient, _jwt_expiry


class FakeResponse:
    def __init__(self, status: int, data: dict):
        self.status_code = status
        self._data = data

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    def __init__(self, get_responses):
        self.get_responses = list(get_responses)
        self.get_calls = []
        self.post_calls = 0

    def post(self, url, **kw):
        self.post_calls += 1
        return FakeResponse(200, {"token": _make_jwt(time.time() + 3600)})

    def get(self, url, **kw):
        self.get_calls.append((url, kw["headers"]["Authorization"]))
        return self.get_responses.pop(0)


def _make_jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"header.{payload}.sig"


def test_jwt_expiry():
    exp = time.time() + 100
    assert abs(_jwt_expiry(_make_jwt(exp)) - exp) < 1
    assert _jwt_expiry("not-a-jwt") is None


def test_token_cached_and_relogin_on_401():
    session = FakeSession([FakeResponse(200, {"content": []}),
                           FakeResponse(401, {}), FakeResponse(200, {"content": [{"id": "p"}]})])
    client = AriaAutomationClient(base_url="https://aria", refresh_token="r", verify_ssl=True, session=session)

    assert client.get_projects() == []
    assert session.post_calls == 1                      # 首次登入
    projects = client.get_projects()                    # 401 → 強制重新登入後重試
    assert projects == [{"id": "p", "name": None, "description": ""}]
    assert session.post_calls == 2
    assert len(session.get_calls) == 3
