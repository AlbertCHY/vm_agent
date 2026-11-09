"""內嵌 vRO (Orchestrator) action 執行 —— 實驗性

用途：嘗試直接執行 Custom Form 使用的 scriptAction (例如 Project_vLAN_List)，
取得與表單相同的動態選項。目前 vm_provisioning 仍以 policy 為準；
可用 `python -m clients.aria.inspect --vro <module>/<action>` 驗證此 API 在你的環境是否可用。
"""
from typing import Any

from clients.aria.session import AriaSession


def _extract_values(node: Any) -> list[Any]:
    """vRO 回傳值的型別包裝很多層 (string / array / properties…)，遞迴取出純量值"""
    if isinstance(node, dict):
        if "value" in node and not isinstance(node["value"], (dict, list)):
            return [node["value"]]
        out: list[Any] = []
        for v in node.values():
            out.extend(_extract_values(v))
        return out
    if isinstance(node, list):
        out = []
        for v in node:
            out.extend(_extract_values(v))
        return out
    return []


class VroAPI:
    def __init__(self, session: AriaSession):
        self.s = session

    def get_action(self, module: str, name: str) -> dict[str, Any]:
        return self.s.get(f"/vco/api/actions/{module}/{name}")

    def run_action(self, module: str, name: str, parameters: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        action = self.get_action(module, name)
        return self.s.post(f"/vco/api/actions/{action['id']}/executions", {"parameters": parameters or []})

    def run_action_values(self, fqn: str, parameters: list[dict[str, Any]] | None = None) -> list[Any]:
        """fqn 形如 com.example.module/ActionName；回傳攤平後的值清單"""
        module, name = fqn.rsplit("/", 1)
        return _extract_values(self.run_action(module, name, parameters))
