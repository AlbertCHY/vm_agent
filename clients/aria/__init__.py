"""VMware Aria Automation REST API client (依服務拆分)

    from clients.aria import get_aria_client
    aria = get_aria_client()
    aria.catalog.list_items(project_id)      # Service Broker catalog
    aria.forms.get_form(form_id)             # Custom Form
    aria.iaas.resolve_project("proj-demo")   # IaaS
    aria.deployments.list_by_project(...)    # Deployment / Day-2
    aria.vro.run_action(module, name)        # 內嵌 vRO (實驗性)

業務規則 (網段、命名、預設值) 不在這裡，見 core.policy / policies/vm_defaults.toml。
"""
from functools import lru_cache
from typing import Any, Optional

from clients.aria.catalog import CatalogAPI
from clients.aria.deployment import DeploymentAPI
from clients.aria.forms import FormsAPI
from clients.aria.iaas import IaasAPI
from clients.aria.session import AriaSession
from clients.aria.vro import VroAPI


class AriaClient:
    """組合各服務 API 的入口；共用同一個登入 session"""

    def __init__(self, session: Optional[AriaSession] = None):
        self.session = session or AriaSession()
        self.catalog = CatalogAPI(self.session)
        self.forms = FormsAPI(self.session)
        self.iaas = IaasAPI(self.session)
        self.deployments = DeploymentAPI(self.session)
        self.vro = VroAPI(self.session)

    # ─── 跨服務的維運查詢 (唯讀工具使用) ───
    def query_vm_by_ip(self, ip: str) -> dict[str, Any]:
        """以 IP 反查 VM 規格、所屬專案與同 Deployment 下的全部元件"""
        ip = ip.strip()
        machines = self.iaas.find_machines_by_ip(ip)
        if not machines:
            return {"found": False, "ip": ip, "message": f"找不到 IP 為 {ip} 的虛擬機"}

        m = machines[0]
        project_id = m.get("projectId")
        result: dict[str, Any] = {
            "found": True,
            "ip": ip,
            "machine": IaasAPI.summarize_machine(m),
            "project_id": project_id,
            "project_name": self.iaas.get_project_name(project_id) if project_id else None,
            "deployment_id": m.get("deploymentId"),
        }
        if len(machines) > 1:
            result["other_matches"] = [{"id": x.get("id"), "name": x.get("name")} for x in machines[1:]]
        if m.get("deploymentId"):
            result.update(self.deployments.describe(m["deploymentId"]))
        return result

    def list_deployments_by_project(self, project_name: str) -> list[dict[str, Any]]:
        """列出指定專案下所有 Deployment 與機器；專案不存在時拋出 ValueError"""
        project = self.iaas.resolve_project(project_name)
        if not project:
            raise ValueError(f"找不到名稱或 ID 為 '{project_name}' 的專案")
        return [
            {**d, "project_id": project["id"], "project_name": project["name"]}
            for d in self.deployments.list_by_project(project["id"], expand_resources=True)
        ]


@lru_cache(maxsize=1)
def get_aria_client() -> AriaClient:
    """取得共用的 Aria client (測試時可用 get_aria_client.cache_clear() 或 monkeypatch 替換)"""
    return AriaClient()


__all__ = ["AriaClient", "AriaSession", "get_aria_client"]
