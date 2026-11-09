"""IaaS API：專案、image profiles、machines"""
from typing import Any, Optional

from clients.aria.cache import TTLCache
from clients.aria.session import AriaSession


def _to_int(val: Any) -> Optional[int]:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return None


def _pick(source: dict[str, Any], *keys: str) -> Any:
    """依序從頂層、customProperties、properties 取第一個非空值 (不同 Aria 版本欄位位置不一)"""
    for k in keys:
        for container in (source, source.get("customProperties") or {}, source.get("properties") or {}):
            val = container.get(k)
            if val not in (None, ""):
                return val
    return None


class IaasAPI:
    def __init__(self, session: AriaSession, cache_ttl: float = 300):
        self.s = session
        self._cache = TTLCache(cache_ttl)

    def _content(self, path: str) -> list[dict[str, Any]]:
        """IaaS API 以 $top/$skip 分頁 (與 page/size 不同)，一次取足量資料"""
        data = self.s.get(path, params={"$top": 2000})
        return data.get("content", []) if isinstance(data, dict) else []

    # ─── 專案 ───
    def get_projects(self, name_filter: Optional[str] = None) -> list[dict[str, Any]]:
        projects = self._cache.get_or_set("projects", lambda: [
            {"id": p.get("id"), "name": p.get("name"), "description": p.get("description", "")}
            for p in self._content("/iaas/api/projects")
        ])
        if name_filter:
            kw = name_filter.strip().lower()
            projects = [p for p in projects if kw in (p["name"] or "").lower() or kw in (p["description"] or "").lower()]
        return projects

    def find_projects(self, identifier: str) -> list[dict[str, str]]:
        """依 ID → 完全相符名稱 → 部分相符名稱 的順序回傳候選 (第一層有結果就停止)"""
        if not identifier:
            return []
        ident = identifier.strip()
        projects = self.get_projects()
        for matcher in (
            lambda p: p["id"] == ident,
            lambda p: (p["name"] or "").lower() == ident.lower(),
            lambda p: ident.lower() in (p["name"] or "").lower(),
        ):
            hits = [{"id": p["id"], "name": p["name"]} for p in projects if matcher(p)]
            if hits:
                return hits
        return []

    def resolve_project(self, identifier: str) -> Optional[dict[str, str]]:
        hits = self.find_projects(identifier)
        return hits[0] if hits else None

    def get_project_name(self, project_id: Optional[str]) -> Optional[str]:
        return next((p["name"] for p in self.get_projects() if p["id"] == project_id), None) if project_id else None

    # ─── Image profiles ───
    def get_images(self) -> dict[str, dict[str, Any]]:
        """/iaas/api/image-profiles 中所有可用的 image 名稱 → 資訊"""
        def load():
            images: dict[str, dict[str, Any]] = {}
            for item in self._content("/iaas/api/image-profiles"):
                for key, info in ((item.get("imageMappings") or {}).get("mapping") or {}).items():
                    images[key] = {
                        "key": key,
                        "name": info.get("name", key),
                        "osFamily": info.get("osFamily", ""),
                        "description": info.get("description", ""),
                    }
            return images
        return self._cache.get_or_set("images", load)

    # ─── Machines ───
    def find_machines_by_ip(self, ip: str) -> list[dict[str, Any]]:
        data = self.s.get("/iaas/api/machines", params={"$filter": f"address eq '{ip.strip()}'"})
        return data.get("content", []) if isinstance(data, dict) else []

    @staticmethod
    def summarize_machine(m: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": m.get("id"),
            "name": m.get("name"),
            "address": m.get("address"),
            "power_state": m.get("powerState"),
            "cpu_count": _to_int(_pick(m, "cpuCount")),
            "memory_gb": _to_int(_pick(m, "memoryGB")),
            "os_type": _pick(m, "image"),
            "created_at": m.get("createdAt"),
        }
