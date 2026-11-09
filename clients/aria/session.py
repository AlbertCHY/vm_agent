"""Aria 登入與共用 HTTP (token 快取、連線池、GET 重試、401 重新登入、分頁)"""
import base64
import json
import logging
import time
from typing import Any, Optional

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import config

logger = logging.getLogger(__name__)

# 取不到 token 有效期限時的保守預設值 (秒)
_DEFAULT_TOKEN_TTL = 1500
# 提前更新 token 的緩衝秒數
_TOKEN_REFRESH_MARGIN = 60


def jwt_expiry(token: str) -> Optional[float]:
    """解析 JWT payload 的 exp (不驗簽，只用於估算更新時機)"""
    try:
        payload_b64 = token.split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload_b64)).get("exp")
        return float(exp) if exp else None
    except Exception:
        return None


class AriaSession:
    def __init__(
        self,
        base_url: Optional[str] = None,
        refresh_token: Optional[str] = None,
        verify_ssl: Optional[bool] = None,
        http: Optional[requests.Session] = None,
    ):
        self.base_url = (base_url or config.AUTOMATION_URL).rstrip("/")
        self.refresh_token = refresh_token or config.AUTOMATION_API_TOKEN
        self.verify_ssl = verify_ssl if verify_ssl is not None else config.AUTOMATION_VERIFY_SSL
        self.timeout = config.AUTOMATION_HTTP_TIMEOUT
        if not self.verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        self.http = http or self._build_http()
        self._access_token: Optional[str] = None
        self._token_expires_at: float = 0.0

    @staticmethod
    def _build_http() -> requests.Session:
        """共用連線池；只對冪等的 GET 自動重試 (POST 不重試，避免重複送出)"""
        s = requests.Session()
        retry = Retry(
            total=config.AUTOMATION_HTTP_RETRIES,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset({"GET"}),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry)
        s.mount("https://", adapter)
        s.mount("http://", adapter)
        return s

    # ─── 認證 ───
    def login(self, force: bool = False) -> str:
        """使用 refresh_token 向 /iaas/api/login 換取 access_token"""
        if not force and self._access_token and time.time() < self._token_expires_at:
            return self._access_token
        resp = self.http.post(
            f"{self.base_url}/iaas/api/login",
            json={"refreshToken": self.refresh_token},
            headers={"Content-Type": "application/json"},
            verify=self.verify_ssl, timeout=self.timeout,
        )
        resp.raise_for_status()
        self._access_token = resp.json().get("token")
        exp = jwt_expiry(self._access_token or "")
        self._token_expires_at = (exp - _TOKEN_REFRESH_MARGIN) if exp else time.time() + _DEFAULT_TOKEN_TTL
        return self._access_token

    def _headers(self, force_login: bool = False) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.login(force=force_login)}", "Content-Type": "application/json"}

    # ─── HTTP ───
    def get(self, path: str, params: Optional[dict[str, Any]] = None) -> Any:
        """GET (遇 401 重新登入後重試一次)"""
        url = f"{self.base_url}{path}"
        resp = self.http.get(url, headers=self._headers(), params=params, verify=self.verify_ssl, timeout=self.timeout)
        if resp.status_code == 401:
            logger.info("Aria access token 失效，重新登入後重試：%s", path)
            resp = self.http.get(url, headers=self._headers(force_login=True), params=params,
                                 verify=self.verify_ssl, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json()

    def post(self, path: str, body: Any, timeout: Optional[int] = None) -> Any:
        """POST (不自動重試；遇 401 代表請求未被受理，重新登入後重送一次)"""
        url = f"{self.base_url}{path}"
        t = max(timeout or self.timeout, 20)
        resp = self.http.post(url, json=body, headers=self._headers(), verify=self.verify_ssl, timeout=t)
        if resp.status_code == 401:
            resp = self.http.post(url, json=body, headers=self._headers(force_login=True),
                                  verify=self.verify_ssl, timeout=t)
        if resp.status_code >= 400:
            raise requests.HTTPError(f"HTTP {resp.status_code}: {resp.text[:500]}", response=resp)
        return resp.json() if resp.content else {}

    def get_all_pages(self, path: str, params: Optional[dict[str, Any]] = None,
                      page_size: int = 100, max_pages: int = 20) -> list[dict[str, Any]]:
        """處理 page/size 分頁，回傳所有 content 項目"""
        items: list[dict[str, Any]] = []
        base = dict(params or {})
        seen_first: set[Any] = set()
        for page in range(max_pages):
            data = self.get(path, params={**base, "page": page, "size": page_size})
            content = data.get("content", []) if isinstance(data, dict) else []
            # 防呆：若 API 忽略 page 參數而重複回傳同一頁，立即停止
            first = (content[0].get("id") if content and isinstance(content[0], dict) else None)
            if first is not None and first in seen_first:
                break
            seen_first.add(first)
            items.extend(content)
            total_pages = data.get("totalPages") if isinstance(data, dict) else None
            if not content or (isinstance(data, dict) and data.get("last") is True):
                break
            if total_pages is not None and page + 1 >= total_pages:
                break
        return items
