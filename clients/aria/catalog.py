"""Service Broker catalog：模板清單、版本 schema、送出申請"""
from typing import Any, Optional

from clients.aria.cache import TTLCache
from clients.aria.models import CatalogItem, CatalogRequestResult, CatalogVersion
from clients.aria.session import AriaSession


class CatalogAPI:
    def __init__(self, session: AriaSession, cache_ttl: float = 300):
        self.s = session
        self._cache = TTLCache(cache_ttl)

    def list_items(self, project_id: Optional[str] = None) -> list[CatalogItem]:
        """列出 catalog items (指定 project_id 時只列出該專案可申請的模板)"""
        def load():
            params = {"projects": project_id} if project_id else None
            raw = self.s.get_all_pages("/catalog/api/items", params=params)
            return [CatalogItem(id=i["id"], name=i.get("name", ""), description=i.get("description") or "",
                                type=(i.get("type") or {}).get("id", "") if isinstance(i.get("type"), dict)
                                else str(i.get("type") or ""))
                    for i in raw]
        return self._cache.get_or_set(("items", project_id), load)

    def latest_version(self, item_id: str) -> Optional[str]:
        def load():
            versions = self.s.get_all_pages(f"/catalog/api/items/{item_id}/versions")
            if not versions:
                return None
            versions.sort(key=lambda v: v.get("createdAt") or "")
            return versions[-1]["id"]
        return self._cache.get_or_set(("latest", item_id), load)

    def get_version(self, item_id: str, version: Optional[str] = None) -> CatalogVersion:
        version = version or self.latest_version(item_id)
        if not version:
            raise ValueError(f"catalog item {item_id} 沒有可用版本")

        def load():
            data = self.s.get(f"/catalog/api/items/{item_id}/versions/{version}")
            schema = data.get("schema") or {}
            return CatalogVersion(
                item_id=item_id, version=version, form_id=data.get("formId"),
                properties=schema.get("properties") or {}, required=schema.get("required") or [],
            )
        return self._cache.get_or_set(("version", item_id, version), load)

    def submit_request(self, item_id: str, project_id: str, deployment_name: str, version: str,
                       inputs: dict[str, Any], reason: str = "") -> CatalogRequestResult:
        """POST /catalog/api/items/{id}/request (與 Service Broker 手動申請相同)"""
        body = {
            "projectId": project_id,
            "deploymentName": deployment_name,
            "version": version,
            "inputs": inputs,
            "reason": reason or "Automated deployment by Aria Agent",
        }
        data = self.s.post(f"/catalog/api/items/{item_id}/request", body)
        first = data[0] if isinstance(data, list) and data else (data if isinstance(data, dict) else {})
        return CatalogRequestResult(
            deployment_id=first.get("deploymentId"), deployment_name=first.get("deploymentName"), raw=data,
        )

    def clear_cache(self) -> None:
        self._cache.clear()
