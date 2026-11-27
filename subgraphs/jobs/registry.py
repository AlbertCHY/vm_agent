"""背景工作規格註冊表 (JobSpec Registry)

各業務子圖透過 register_job_spec() 宣告自己的建圖、階段推導、完成/交棒規則、
啟動恢復策略、審核節點與詳情渲染資料，讓 JobManager 與 CLI 保持完全無類型耦合。
"""
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from subgraphs.jobs.models import KIND_LABELS, RUNNING


@dataclass(frozen=True)
class CompletionOutcome:
    status: str                       # "succeeded" | "skipped" | "failed" | "cancelled" | "aborted"
    carried: dict[str, Any] = field(default_factory=dict)
    log_message: str = ""
    done_notice: str = ""             # 無 next_steps 時推播的通知 (空字串不推播)
    chain_notice_prefix: str = ""     # 有 next_steps 時的通知前綴 (例如 "{title} 硬碟調整完成")
    error: str = ""
    on_failure: Optional[Callable[[str, Any], None]] = None


@dataclass(frozen=True)
class JobSpec:
    kind: str
    label: str
    build_graph: Callable[[Any], Any]
    derive_stage: Callable[[tuple[str, ...], dict[str, Any], dict[str, Any], bool, bool], tuple[str, str]]
    evaluate_completion: Callable[[str, str, dict[str, Any], dict[str, Any]], CompletionOutcome]
    enqueue_chained: Optional[Callable[[dict[str, Any], list[dict[str, Any]], Any], Any]] = None
    review_nodes: tuple[str, ...] = ()
    active_stage: str = RUNNING
    auto_resume_on_startup: bool = False
    derive_extra_fields: Optional[Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]] = None
    detail_table: Optional[
        Callable[[Any], Optional[tuple[str, tuple[str, ...], list[tuple[str, ...]]]]]
    ] = None
    prepare_retry_before_resume: Optional[
        Callable[[str, Any, dict[str, Any], Callable[[], dict[str, Any]]], bool]
    ] = None


_REGISTRY: dict[str, JobSpec] = {}
_BUILTINS_LOADED: bool = False


def register_job_spec(spec: JobSpec) -> JobSpec:
    """註冊一種背景工作規格；若重複註冊相同 kind 會覆蓋"""
    _REGISTRY[spec.kind] = spec
    return spec


def _ensure_builtin_specs() -> None:
    global _BUILTINS_LOADED
    if _BUILTINS_LOADED:
        return
    _BUILTINS_LOADED = True
    import subgraphs.vm_bootstrap.job_spec  # noqa: F401
    import subgraphs.vm_build.job_spec  # noqa: F401
    import subgraphs.vm_disk.job_spec  # noqa: F401


def get_job_spec(kind: str) -> JobSpec:
    _ensure_builtin_specs()
    spec = _REGISTRY.get(kind)
    if spec is None:
        raise ValueError(f"未知的背景工作類型：{kind}")
    return spec


def all_job_specs() -> dict[str, JobSpec]:
    _ensure_builtin_specs()
    return dict(_REGISTRY)


def kind_label(kind: str) -> str:
    _ensure_builtin_specs()
    spec = _REGISTRY.get(kind)
    if spec is not None:
        return spec.label
    return KIND_LABELS.get(kind, kind)
