"""業務規則 (Policy) 載入與套用

policies/<name>.toml 為實際值 (可含內部資訊，不進版控)；
找不到時退回 policies/<name>.example.toml，讓剛 clone 下來的專案也能啟動。
"""
import logging
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

import config

logger = logging.getLogger(__name__)


def policy_path(name: str) -> Path:
    real = config.POLICIES_DIR / f"{name}.toml"
    if real.exists():
        return real
    example = config.POLICIES_DIR / f"{name}.example.toml"
    if example.exists():
        logger.warning("找不到 %s，改用範本 %s", real.name, example.name)
        return example
    raise FileNotFoundError(f"找不到 policy 檔案: {real} (或 {example.name})")


@lru_cache(maxsize=None)
def load_policy(name: str) -> dict[str, Any]:
    with open(policy_path(name), "rb") as f:
        return tomllib.load(f)


class VMPolicy:
    """VM 開立規則：預設值、上下限、網段、藍圖與 OS Image 對應"""

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.defaults: dict[str, Any] = dict(data.get("defaults") or {})
        self.limits: dict[str, int] = dict(data.get("limits") or {})
        ds = data.get("datastore") or {}
        self.datastore_allowed: list[str] = list(ds.get("allowed") or [])
        self.datastore_by_storage_type: dict[str, str] = {
            k.upper(): v for k, v in (ds.get("by_storage_type") or {}).items()
        }
        self.sg_allowed: list[str] = list((data.get("security_group") or {}).get("allowed") or ["ap", "db", "web"])
        self.deployment_name_prefix: str = (data.get("deployment_name") or {}).get("fallback_prefix", "vm")

    # ─── 預設值 ───
    def apply_defaults(self, spec: dict[str, Any]) -> dict[str, Any]:
        """補上未填欄位的預設值 (ap_unit 只在已填 ap_owner 時補)"""
        out = dict(spec)
        for key, val in self.defaults.items():
            if key == "ap_unit" and not out.get("ap_owner"):
                continue
            if not out.get(key):
                out[key] = val
        if not out.get("network_segment"):
            out["network_segment"] = self.network_segment_for(out.get("project_name"))
        return out

    # ─── 網段 ───
    def network_segment_for(self, project_name: Optional[str]) -> str:
        net = self.data.get("network") or {}
        default = net.get("default", "")
        name = (project_name or "").strip()
        if not name:
            return default
        by_project = net.get("by_project") or {}
        if name in by_project:
            return by_project[name]
        lowered = name.lower()
        for rule in net.get("by_prefix") or []:
            if lowered.startswith(str(rule.get("prefix", "")).lower()):
                return rule["segment"]
        return default

    # ─── 專案 description 自動帶入 ───
    def fields_from_project_description(self, description: str) -> dict[str, str]:
        rule = self.data.get("project_description") or {}
        sep = rule.get("separator", "/")
        fields = rule.get("fields") or []
        parts = [p.strip() for p in (description or "").split(sep)]
        return {f: parts[i] for i, f in enumerate(fields) if i < len(parts) and parts[i]}

    # ─── 藍圖 ───
    def blueprint_query_for(self, os_image: Optional[str], os_query: Optional[str]) -> str:
        bp = self.data.get("blueprint") or {}
        img = (os_image or "").lower()
        if img:
            for rule in bp.get("os_rules") or []:
                if "prefix" in rule and img.startswith(str(rule["prefix"]).lower()):
                    return rule["query"]
                if "contains" in rule and str(rule["contains"]).lower() in img:
                    return rule["query"]
        return os_query or bp.get("default_query", "")

    def pick_blueprint(self, blueprints: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not blueprints:
            return None
        preferred = (self.data.get("blueprint") or {}).get("preferred_keyword")
        if preferred:
            for b in blueprints:
                if preferred in (b.get("name") or ""):
                    return b
        return blueprints[0]

    # ─── 存儲 ───
    def datastore_for_storage_type(self, storage_type: Optional[str]) -> Optional[str]:
        return self.datastore_by_storage_type.get((storage_type or "").strip().upper())

    # ─── OS Image ───
    def match_os_image(self, query: Optional[str], available: Optional[dict[str, Any]] = None) -> Optional[str]:
        """自然語言 OS 需求 → image-profiles 名稱 (先精確比對可用清單，再套家族規則，最後模糊比對)"""
        if not query:
            return None
        available = available or {}
        raw = query.strip()
        for k in available:
            if k.lower() == raw.lower():
                return k

        q = raw.lower().replace(" ", "").replace("-", "").replace("_", "")
        is_eng = any(t in q for t in ("eng", "english", "en"))
        for fam in (self.data.get("os_images") or {}).get("families") or []:
            if not any(kw in q for kw in fam.get("keywords") or []):
                continue
            for v in fam.get("versions") or []:
                if str(v.get("match", "")) in q:
                    return (v.get("image_eng") if is_eng and v.get("image_eng") else v["image"])
            return fam.get("default_eng") if is_eng and fam.get("default_eng") else fam.get("default")

        low = raw.lower()
        for k, info in available.items():
            if low in k.lower() or low in str((info or {}).get("description", "")).lower():
                return k
        return None


@lru_cache(maxsize=1)
def get_vm_policy() -> VMPolicy:
    return VMPolicy(load_policy("vm_defaults"))
