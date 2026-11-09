"""Deployment API：deployment 清單 / 詳情、request 狀態、resources、Day-2 actions"""
import re
from typing import Any, Optional

import requests

from clients.aria.session import AriaSession


def summarize_resource(res: dict[str, Any]) -> dict[str, Any]:
    """將 Deployment resource 精簡為關鍵欄位"""
    rtype = res.get("type", "")
    props = res.get("properties") or {}
    summary: dict[str, Any] = {
        "id": res.get("id"),
        "name": props.get("resourceName") or res.get("name"),
        "type": rtype,
        "state": res.get("state"),
    }
    if "machine" in rtype.lower():
        summary.update({
            "address": props.get("address"),
            "power_state": props.get("powerState"),
            "dependency": props.get("dependsOn"),
        })
    return {k: v for k, v in summary.items() if v not in (None, "")}


def _group(rtype: str) -> str:
    t = rtype.lower()
    if "machine" in t:
        return "machines"
    if "disk" in t or "volume" in t:
        return "disks"
    if "network" in t:
        return "networks"
    if "security" in t:
        return "security_groups"
    return "others"


class DeploymentAPI:
    def __init__(self, session: AriaSession):
        self.s = session

    # ─── Deployment ───
    def get(self, deployment_id: str, expand: Optional[str] = None) -> dict[str, Any]:
        params = {"expand": expand} if expand else None
        return self.s.get(f"/deployment/api/deployments/{deployment_id}", params=params)

    def resources(self, deployment_id: str) -> list[dict[str, Any]]:
        return self.s.get_all_pages(f"/deployment/api/deployments/{deployment_id}/resources")

    def list_by_project(self, project_id: str, search: Optional[str] = None,
                        expand_resources: bool = False) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"projects": project_id}
        if search:
            params["search"] = search
        if expand_resources:
            params["expand"] = "resources"
        out = []
        for d in self.s.get_all_pages("/deployment/api/deployments", params=params):
            item = {
                "id": d.get("id"),
                "name": d.get("name"),
                "status": d.get("status"),
                "created_by": d.get("createdBy"),
                "created_at": d.get("createdAt"),
            }
            if expand_resources:
                item["machines"] = [summarize_resource(r) for r in d.get("resources") or []
                                    if "machine" in (r.get("type") or "").lower()]
            out.append(item)
        return out

    def next_serial(self, project_id: str, prefix: str) -> int:
        """同專案中以 prefix 開頭、後接數字的 deployment 名稱，取最大號 + 1"""
        pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$", re.IGNORECASE)
        numbers = [int(m.group(1)) for d in self.list_by_project(project_id, search=prefix)
                   if (m := pattern.match(d.get("name") or ""))]
        return max(numbers, default=0) + 1

    def describe(self, deployment_id: str) -> dict[str, Any]:
        """Deployment 基本資訊與依類型分組的資源 (查詢失敗時以 error 欄位回報)"""
        out: dict[str, Any] = {}
        try:
            dep = self.get(deployment_id)
            out["deployment"] = {k: dep.get(src) for k, src in
                                 (("name", "name"), ("status", "status"), ("created_by", "createdBy"),
                                  ("created_at", "createdAt"))}
        except requests.RequestException as e:
            out["deployment"] = {"error": f"無法取得 Deployment 資訊: {e}"}
        try:
            grouped: dict[str, list[dict[str, Any]]] = {}
            for r in self.resources(deployment_id):
                grouped.setdefault(_group(r.get("type") or ""), []).append(summarize_resource(r))
            out["deployment_resources"] = grouped
        except requests.RequestException as e:
            out["deployment_resources"] = {"error": f"無法取得 Deployment 資源: {e}"}
        return out

    # ─── Request 狀態 (第 3 階段：等待建立完成) ───
    def get_request(self, request_id: str) -> dict[str, Any]:
        return self.s.get(f"/deployment/api/requests/{request_id}")

    # ─── Day-2 actions (第 4 階段：硬碟調整) ───
    def resource_actions(self, resource_id: str) -> list[dict[str, Any]]:
        data = self.s.get(f"/deployment/api/resources/{resource_id}/actions")
        return data if isinstance(data, list) else data.get("content", [])

    def resource_action(self, resource_id: str, action_id: str) -> dict[str, Any]:
        return self.s.get(f"/deployment/api/resources/{resource_id}/actions/{action_id}")

    def run_resource_action(self, resource_id: str, action_id: str, inputs: dict[str, Any],
                            reason: str = "") -> dict[str, Any]:
        return self.s.post(f"/deployment/api/resources/{resource_id}/requests",
                           {"actionId": action_id, "inputs": inputs, "reason": reason})
