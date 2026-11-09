"""業務規則 (Policy) 載入與套用

policies/<name>.toml 為實際值 (可含內部資訊，不進版控)；
找不到時退回 policies/<name>.example.toml，讓剛 clone 下來的專案也能啟動。
"""
import logging
import re
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
    """VM 開立的業務規則 (Aria 查不到的部分)：預設值、命名、網段、OS 選項排除

    模板欄位與下拉選項 (CPU、記憶體、OS、Cluster、Datastore) 一律從 Aria 讀取，不在這裡。
    """

    def __init__(self, data: dict[str, Any]):
        self.data = data
        self.defaults: dict[str, Any] = dict(data.get("defaults") or {})
        self.field_defaults: dict[str, Any] = dict(data.get("field_defaults") or {})
        naming = data.get("naming") or {}
        self.naming_pattern: str = naming.get("pattern", "{project}-{function}{serial:02d}")
        self.function_max_length: int = int(naming.get("function_max_length", 8))
        self.exclude_os_options: list[str] = list((data.get("templates") or {}).get("exclude_os_options") or [])

    @property
    def default_sg(self) -> str:
        return self.defaults.get("sg", "ap")

    @property
    def default_ip_mode(self) -> str:
        return self.defaults.get("ip_mode", "DHCP")

    @property
    def default_storage_type(self) -> str:
        return self.defaults.get("storage_type", "HDD")

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

    # ─── 命名 ───
    def clean_function(self, function: Optional[str]) -> str:
        """功能名稱只保留英數字並限制長度"""
        cleaned = re.sub(r"[^A-Za-z0-9]", "", function or "")[: self.function_max_length]
        return cleaned or "VM"

    def name_prefix(self, project: str, function: str) -> str:
        """命名格式中流水號之前的部分 (用於查詢現有的最大號)"""
        head = self.naming_pattern.split("{serial", 1)[0]
        return head.format(project=project, function=self.clean_function(function))

    def deployment_name(self, project: str, function: str, serial: int) -> str:
        return self.naming_pattern.format(project=project, function=self.clean_function(function), serial=serial)


@lru_cache(maxsize=1)
def get_vm_policy() -> VMPolicy:
    return VMPolicy(load_policy("vm_defaults"))
