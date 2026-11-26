"""Bootstrap 步驟定義載入 (policies/bootstrap_steps*.toml) 與樣板展開"""
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Optional

from core.policy import load_policy

DEFAULT_TIMEOUT = 120
SUPPORTED_OS_TYPES = ("linux", "windows")


def normalize_os_type(os_type: Optional[str]) -> str:
    return "windows" if (os_type or "").strip().lower() == "windows" else "linux"


@dataclass(frozen=True)
class StepDef:
    id: str
    name: str
    script: str
    sudo: bool = False
    sudo_password: bool = False
    skip_if: str = ""
    stop_on_fail: bool = False
    rotates_password: bool = False
    timeout: int = DEFAULT_TIMEOUT
    env: dict[str, str] = field(default_factory=dict)
    secret_env: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StepDef":
        return cls(
            id=d["id"],
            name=d.get("name", d["id"]),
            script=d["script"],
            sudo=bool(d.get("sudo", False)),
            sudo_password=bool(d.get("sudo_password", False)),
            skip_if=d.get("skip_if", ""),
            stop_on_fail=bool(d.get("stop_on_fail", False)),
            rotates_password=bool(d.get("rotates_password", False)),
            timeout=int(d.get("timeout", DEFAULT_TIMEOUT)),
            env=dict(d.get("env") or {}),
            secret_env=dict(d.get("secret_env") or {}),
        )


@dataclass(frozen=True)
class BootstrapPlan:
    os_type: str
    remote_dir: str
    crowdstrike_remote_dir: str
    steps: tuple[StepDef, ...]
    cleanup: Optional[StepDef] = None
    sccm_remote_dir: str = ""
    secret_temp_dir: str = ""
    reboot_on_success: bool = False

    @property
    def script_files(self) -> list[str]:
        """需上傳的腳本 (去重、維持順序)"""
        names = [s.script for s in self.steps]
        if self.cleanup is not None:
            names.append(self.cleanup.script)
        return list(dict.fromkeys(names))


@lru_cache(maxsize=4)
def get_bootstrap_plan(os_type: str = "linux") -> BootstrapPlan:
    os_key = normalize_os_type(os_type)
    data = load_policy(f"bootstrap_steps_{os_key}")
    upload = data.get("upload") or {}
    finalize_cfg = data.get("finalize") or {}
    default_remote = "C:/Users/{init_user}/Desktop/bootstrap" if os_key == "windows" else "/tmp"
    default_secret_tmp = "C:/Users/{init_user}/AppData/Local/Temp" if os_key == "windows" else "/tmp"
    cleanup_raw = data.get("cleanup")
    return BootstrapPlan(
        os_type=os_key,
        remote_dir=upload.get("remote_dir", default_remote),
        crowdstrike_remote_dir=upload.get("crowdstrike_remote_dir", f"{default_remote.rstrip('/')}/CrowdStrike"),
        sccm_remote_dir=upload.get("sccm_remote_dir", ""),
        secret_temp_dir=upload.get("secret_temp_dir", default_secret_tmp),
        steps=tuple(StepDef.from_dict(s) for s in data.get("steps") or []),
        cleanup=StepDef.from_dict(cleanup_raw) if cleanup_raw else None,
        reboot_on_success=bool(finalize_cfg.get("reboot_on_success", False)),
    )


def render(template: str, context: dict[str, Any]) -> str:
    return template.format_map(context)


def render_env(env: dict[str, str], context: dict[str, Any]) -> dict[str, str]:
    """展開樣板並移除空值"""
    out: dict[str, str] = {}
    for k, tmpl in env.items():
        val = render(str(tmpl), context)
        if val != "":
            out[k] = val
    return out
