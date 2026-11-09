"""VM Provisioning 子圖節點 (模仿 Service Broker > Catalog 的手動申請流程)

parse_intent → select_template → load_form → resolve_fields → validate
  → (ask_clarification | human_review | submit_request)

- 欄位、選項、必填來自 Aria catalog schema + Custom Form (clients.aria.forms)。
- LLM 只做兩件事：抽取意圖、從真實候選中選擇 (模板、OS image)。
- 其餘由程式確定性處理 (專案 ID、CPU / 記憶體、網段、Datastore、流水號)。
"""
import json
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END

import config
from clients.aria import get_aria_client
from clients.aria.models import FormField
from core.policy import get_vm_policy
from core.progress import emit
from core.secrets import get_audit_sink
from subgraphs.common.review import APPROVE, EDIT, REJECT, SKIP, ReviewField, ask_review, review_request
from subgraphs.vm_provisioning import reasoning
from subgraphs.vm_provisioning.resolver import (
    DEPLOYMENT_NAME, OS_IMAGE, display_value, resolve_values, validate_values,
)
from subgraphs.vm_provisioning.state import ProvisioningState

PROMPTS = config.PROMPTS


def _fields(state: ProvisioningState) -> list[FormField]:
    return [FormField.model_validate(f) for f in state.get("fields") or []]


def _context(state: ProvisioningState) -> str:
    """提供給 choose() 的需求內容：累積意圖 + 審核退回意見"""
    intent = state.get("intent") or {}
    feedback = [m.content for m in state.get("messages", []) if isinstance(m, HumanMessage)
                and str(m.content).startswith("人工審查已退回")]
    ctx = json.dumps({k: v for k, v in intent.items() if v not in (None, "")}, ensure_ascii=False)
    return ctx + ("\n" + "\n".join(feedback[-2:]) if feedback else "")


# 1. 意圖抽取
def parse_intent(state: ProvisioningState) -> dict[str, Any]:
    emit("[VM Provisioning] 正在分析需求敘述...", stage="vm_provisioning")
    intent = reasoning.parse_intent(state.get("messages", []), state.get("intent") or {})
    return {"intent": intent}


# 2. 專案與模板
def select_template(state: ProvisioningState) -> dict[str, Any]:
    aria = get_aria_client()
    intent = state.get("intent") or {}
    overrides = state.get("overrides") or {}
    choices = dict(state.get("choices") or {})
    notes: list[str] = []
    errors: list[str] = []

    project = None
    query = overrides.get("project") or intent.get("project")
    if query:
        hits = aria.iaas.find_projects(query)
        if hits:
            project = hits[0]
            if len(hits) > 1:
                notes.append(f"專案「{query}」有多個相符：{', '.join(h['name'] for h in hits)}；已選 {project['name']}")
        else:
            errors.append(f"找不到專案「{query}」")

    template = None
    if project:
        items = aria.catalog.list_items(project["id"])
        wanted = overrides.get("template")
        if wanted:
            matched = [it for it in items if wanted.lower() in it.name.lower() or it.id == wanted]
            if matched:
                template = {"id": matched[0].id, "name": matched[0].name}
            else:
                errors.append(f"專案 {project['name']} 沒有名稱包含「{wanted}」的模板")
        elif intent.get("os_family") or intent.get("os_version"):
            emit("[VM Provisioning] 正在選擇 catalog 模板...", stage="vm_provisioning")
            name, reason = reasoning.choose(
                "template", [(it.name, it.description) for it in items],
                PROMPTS["vm_choice"]["template"], _context(state), choices,
            )
            hit = next((it for it in items if it.name == name), None)
            if hit:
                template = {"id": hit.id, "name": hit.name}
                if reason and reason != "唯一選項":
                    notes.append(f"模板選擇理由：{reason}")
        if not items:
            errors.append(f"專案 {project['name']} 沒有可申請的 catalog 模板")

    return {"project": project, "template": template, "choices": choices,
            "notes": notes, "errors": errors}


# 3. 模板表單
def load_form(state: ProvisioningState) -> dict[str, Any]:
    template = state.get("template")
    if not template:
        return {"fields": []}
    aria = get_aria_client()
    version = aria.catalog.get_version(template["id"])
    fields = aria.forms.fields_for(version)
    return {"template": {**template, "version": version.version},
            "fields": [f.model_dump() for f in fields]}


# 4. 欄位解析
def resolve_fields(state: ProvisioningState) -> dict[str, Any]:
    aria = get_aria_client()
    policy = get_vm_policy()
    project = state.get("project")
    choices = dict(state.get("choices") or {})
    fields = _fields(state)

    def choose_os_image(field: FormField) -> tuple[Any, str]:
        excluded = {x.lower() for x in policy.exclude_os_options}
        candidates = [(str(o.value), o.label) for o in field.options or [] if str(o.value).lower() not in excluded]
        try:
            images = aria.iaas.get_images()
        except Exception:
            images = {}
        known = {c[0] for c in candidates}
        candidates += [(k, f"image profile: {info.get('description') or info.get('name') or k}")
                       for k, info in sorted(images.items()) if k not in known and k.lower() not in excluded]
        return reasoning.choose("os_image", candidates, PROMPTS["vm_choice"]["os_image"], _context(state), choices)

    def next_serial(prefix: str) -> int:
        return aria.deployments.next_serial(project["id"], prefix) if project else 1

    values, sources, notes, errors = resolve_values(
        state.get("intent") or {}, project, fields, state.get("overrides") or {},
        policy, choose_os_image, next_serial,
    )
    return {
        "values": values, "sources": sources, "choices": choices,
        "notes": (state.get("notes") or []) + notes,
        "errors": (state.get("errors") or []) + errors,
    }


# 5. 驗證與路由
def validate(state: ProvisioningState) -> dict[str, Any]:
    fields = _fields(state)
    values = state.get("values") or {}
    missing: list[str] = []
    if not state.get("project"):
        missing.append("Aria 專案名稱")
    if not state.get("template"):
        missing.append("作業系統 (用於選擇 catalog 模板)")
    if fields:
        m, errs, notes = validate_values(fields, values, get_aria_client().iaas.find_machines_by_ip)
        missing += m
    else:
        errs, notes = [], []
    return {"missing": missing,
            "errors": (state.get("errors") or []) + errs,
            "notes": (state.get("notes") or []) + notes}


def route_validate(state: ProvisioningState) -> str:
    problems = state.get("missing") or state.get("errors")
    if not state.get("reviewed") and state.get("missing"):
        return "ask_clarification"
    if state.get("submit_after_validate") and not problems:
        return "submit_request"
    return "human_review"


# 6. 補問
def ask_clarification(state: ProvisioningState) -> dict[str, Any]:
    lines = [f"- {m}" for m in state.get("missing") or []] + [f"- ⚠️ {e}" for e in state.get("errors") or []]
    msg = PROMPTS["clarification"]["reply"].format(missing_fields_text="\n".join(lines))
    return {"messages": [AIMessage(content=msg)]}


# 7. 人工審核
def build_review_fields(state: ProvisioningState) -> list[ReviewField]:
    values = state.get("values") or {}
    sources = state.get("sources") or {}
    project = state.get("project") or {}
    template = state.get("template") or {}
    rows: list[ReviewField] = [
        ReviewField(key="project", label="專案", value=project.get("name"), editable=True,
                    source="Aria 查詢" if project else ""),
        ReviewField(key="template", label="Catalog 模板",
                    value=f"{template.get('name')} (v{template.get('version')})" if template else None,
                    editable=True, source="模型選擇" if template and "template" in (state.get("choices") or {}) else ""),
        ReviewField(key=DEPLOYMENT_NAME, label="部署名稱", value=values.get(DEPLOYMENT_NAME), editable=True,
                    source=sources.get(DEPLOYMENT_NAME, "")),
    ]
    for f in _fields(state):
        if not f.submit or not f.is_visible(values):
            continue
        options = ""
        if f.options:
            shown = [o.label for o in f.options]
            options = " / ".join(shown[:10]) + (" …" if len(shown) > 10 else "")
        rows.append(ReviewField(
            key=f.id, label=f.label + (" *" if f.required else ""), value=display_value(f, values.get(f.id)),
            editable=True, source=sources.get(f.id, ""), options=options,
        ))
    return rows


def human_review(state: ProvisioningState) -> dict[str, Any]:
    errors = list(state.get("errors") or []) + [f"缺少：{m}" for m in state.get("missing") or []]
    decision = ask_review(review_request(
        kind="vm_deployment",
        title="📋 待審核的 VM 開機申請 (Service Broker Catalog)",
        message="VM 申請內容已組合完成，請人工審查確認。" + ("（目前有錯誤或缺漏，請以 E 修正或退回）" if errors else ""),
        fields=build_review_fields(state),
        actions=[APPROVE, EDIT, REJECT, SKIP],
        errors=errors,
        notes=state.get("notes") or [],
    ))
    feedback = decision.model_dump()
    base = {"human_feedback": feedback, "reviewed": True, "notes": [], "errors": []}

    if decision.action in ("approve", "edit"):
        overrides = {**(state.get("overrides") or {}), **decision.edits}
        if decision.action == "approve" and not decision.edits:
            problems = bool(errors)
            return {**base, "errors": state.get("errors") or [], "notes": state.get("notes") or [],
                    "submit_after_validate": not problems}
        return {**base, "overrides": overrides, "submit_after_validate": True}

    if decision.action == "skip":
        reason = decision.feedback
        return {
            **base,
            "execution_result": {"aborted": True, "reason": reason or "使用者於人工審查取消"},
            "messages": [AIMessage(content=f"🚫 已取消此 VM 申請。{('原因: ' + reason) if reason else ''}")],
        }

    # reject：帶意見重新抽取意圖，並清除模型的選擇快取
    return {
        **base,
        "choices": {},
        "submit_after_validate": False,
        "messages": [HumanMessage(content=f"人工審查已退回，意見為: {decision.feedback or '請調整規格'}")],
    }


def route_review(state: ProvisioningState) -> str:
    action = (state.get("human_feedback") or {}).get("action")
    if action == "skip":
        return END
    if action == "reject":
        return "parse_intent"
    if action == "approve" and state.get("submit_after_validate"):
        return "submit_request"
    if action == "approve":
        return "human_review"          # 仍有錯誤，不能送出
    return "select_template"           # edit：重新解析 (修改值最後套用)


# 8. 送出 catalog request
def submit_request(state: ProvisioningState) -> dict[str, Any]:
    emit("[VM Provisioning] 正在送出 Service Broker catalog 申請...", stage="vm_provisioning")
    project = state["project"]
    template = state["template"]
    values = state.get("values") or {}
    inputs = {f.id: values[f.id] for f in _fields(state) if f.submit and f.id in values and values[f.id] is not None}
    deployment_name = values[DEPLOYMENT_NAME]
    try:
        result = get_aria_client().catalog.submit_request(
            template["id"], project["id"], deployment_name, template["version"], inputs,
            reason=str(values.get("Description") or ""),
        )
        exec_res = {
            "success": True,
            "deployment_id": result.deployment_id,
            "deployment_name": result.deployment_name or deployment_name,
            "project": project, "template": template, "inputs": inputs,
        }
        get_audit_sink().record("vm_deployment_submitted", **exec_res)
        msg = (
            f"🎉 **VM 申請已送出！**\n\n"
            f"- **部署名稱**: `{exec_res['deployment_name']}`\n"
            f"- **Deployment ID**: `{result.deployment_id}`\n"
            f"- **模板**: {template['name']} (v{template['version']})\n"
            f"- **專案**: {project['name']}\n\n"
            "申請會先經過 Aria 上的審核，核准後開始建立虛擬機。"
        )
        return {"execution_result": exec_res, "messages": [AIMessage(content=msg)]}
    except Exception as e:
        get_audit_sink().record("vm_deployment_failed", inputs=inputs, error=str(e))
        return {"execution_result": {"error": str(e)},
                "messages": [AIMessage(content=f"❌ 送出 catalog 申請失敗: {e}")]}
