"""Bootstrap 步驟定義載入 (policies/bootstrap_steps.toml) 與樣板展開"""
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from core.policy import load_policy

DEFAULT_TIMEOUT = 120


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
    remote_dir: str
    crowdstrike_remote_dir: str
    steps: tuple[StepDef, ...]
    cleanup: StepDef

    @property
    def script_files(self) -> list[str]:
        """需上傳的腳本 (去重、維持順序)"""
        names = [s.script for s in self.steps] + [self.cleanup.script]
        return list(dict.fromkeys(names))


@lru_cache(maxsize=1)
def get_bootstrap_plan() -> BootstrapPlan:
    data = load_policy("bootstrap_steps")
    upload = data.get("upload") or {}
    return BootstrapPlan(
        remote_dir=upload.get("remote_dir", "/tmp"),
        crowdstrike_remote_dir=upload.get("crowdstrike_remote_dir", "/tmp/CrowdStrike"),
        steps=tuple(StepDef.from_dict(s) for s in data.get("steps") or []),
        cleanup=StepDef.from_dict(data["cleanup"]),
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
