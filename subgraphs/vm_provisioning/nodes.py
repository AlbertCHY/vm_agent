"""VM Provisioning 子圖節點

節點：analyze_and_extract → check_completeness → (ask_clarification | human_review) → submit_request
業務預設值與對應規則來自 policies/vm_defaults.toml (core.policy.VMPolicy)。
"""
import time
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END

import config
from clients.aria_client import get_aria_client
from clients.llm import get_llm
from core.policy import get_vm_policy
from core.progress import emit
from core.secrets import get_audit_sink
from schemas import (
    CRITICAL_FIELDS,
    INT_FIELDS,
    REQUIRED_FIELDS,
    VM_FIELDS,
    VMDeploymentParams,
    field_label,
    validate_vm_spec,
)
from subgraphs.common.review import (
    APPROVE, EDIT, REJECT, SKIP, ReviewField, ask_review, review_request,
)
from subgraphs.vm_provisioning.state import ProvisioningState, SpecExtractionResult

PROMPTS = config.PROMPTS


def _policy_hint() -> str:
    """把 policy 中的可選值提供給 LLM 參考 (選項來源唯一：policies/vm_defaults.toml)"""
    p = get_vm_policy()
    mapping = ", ".join(f"{k} → {v}" for k, v in p.datastore_by_storage_type.items())
    return (
        f"【安全群組 sg 可選值】: {', '.join(p.sg_allowed)}\n"
        f"【存儲位置 datastore 可選值】: {', '.join(p.datastore_allowed)}"
        + (f" (依存儲類型：{mapping})" if mapping else "")
    )


def _safe_available_images() -> dict[str, Any]:
    try:
        return get_aria_client().get_os_images()
    except Exception:
        return {}


# 1. 意圖分析與規格抽取節點
def analyze_and_extract(state: ProvisioningState) -> dict[str, Any]:
    """使用 LLM 結構化輸出解析使用者自然語言，並透過 API 查詢補齊 project_id、os_image (image-profiles) 與 blueprint_id"""
    messages = state.get("messages", [])
    current_spec = dict(state.get("current_spec") or {})
    aria = get_aria_client()
    policy = get_vm_policy()

    # 取得系統中所有可用的 OS Image 映射清單 (來自 /iaas/api/image-profiles)
    available_images = _safe_available_images()
    image_keys_str = ", ".join(available_images.keys()) if available_images else "無"

    system_instruction = (
        f"{PROMPTS['vm_agent']['system']}\n\n"
        f"{PROMPTS['extractor']['system']}\n\n"
        f"【系統目前支援的 OS Image 映像檔名稱清單 (image-profiles)】:\n{image_keys_str}\n\n"
        f"注意：os_image 必須為上述清單中的具體名稱。\n\n"
        f"{_policy_hint()}\n\n"
        f"目前已知/已累積的規格參數: {current_spec}"
    )

    structured_llm = get_llm().with_structured_output(SpecExtractionResult)

    emit("[VM Provisioning] 正在分析需求並抽取規格參數...", stage="vm_provisioning")
    prompt_messages = [SystemMessage(content=system_instruction)] + messages
    result: SpecExtractionResult = structured_llm.invoke(prompt_messages)

    # 合併提取到的新欄位
    extracted_dict = result.extracted_spec.model_dump(exclude_none=True)
    current_spec.update(extracted_dict)

    # 1.1 若需要查詢專案 ID
    if result.project_name_query and not current_spec.get("project_id"):
        projects = aria.get_projects(name_filter=result.project_name_query)
        if projects:
            matched_proj = projects[0]
            current_spec["project_id"] = matched_proj["id"]
            current_spec["project_name"] = matched_proj["name"]
            # 依 policy 從專案 description (例如 '單號/負責人/用途') 自動輔助填補
            for key, val in policy.fields_from_project_description(matched_proj.get("description", "")).items():
                if not current_spec.get(key):
                    current_spec[key] = val

    if current_spec.get("project_id") and not current_spec.get("project_name"):
        current_spec["project_name"] = aria.get_project_name(current_spec["project_id"])

    # 1.2 處理 OS Image (從 image-profiles 選取) 與 藍圖 ID
    os_query = result.blueprint_os_query or current_spec.get("os_image")
    if available_images and current_spec.get("os_image") not in available_images and os_query:
        matched_img = policy.match_os_image(os_query, available_images)
        if matched_img:
            current_spec["os_image"] = matched_img

    if not current_spec.get("blueprint_id"):
        bp_query = policy.blueprint_query_for(current_spec.get("os_image"), os_query)
        selected_bp = policy.pick_blueprint(aria.get_blueprints(os_hint=bp_query))
        if selected_bp:
            current_spec["blueprint_id"] = selected_bp["id"]
            current_spec["blueprint_name"] = selected_bp["name"]

    if current_spec.get("blueprint_id") and not current_spec.get("blueprint_name"):
        current_spec["blueprint_name"] = aria.get_blueprint_name(current_spec["blueprint_id"])

    # 1.3 存儲類型 → datastore (policy 對應)
    if not current_spec.get("datastore") and result.storage_type:
        ds = policy.datastore_for_storage_type(result.storage_type)
        if ds:
            current_spec["datastore"] = ds

    # 1.4 補足常規基礎預設值 (若尚未設定)
    current_spec = policy.apply_defaults(current_spec)

    # 若未指定 deployment_name，自動生成一個易辨識的名稱
    if not current_spec.get("deployment_name"):
        base_name = (result.project_name_query or policy.deployment_name_prefix).lower()
        suffix = int(time.time()) % 10000
        sg_tag = str(current_spec.get("sg", "ap")).upper()
        current_spec["deployment_name"] = f"{base_name}-{sg_tag}-{suffix:04d}"

    return {
        "current_spec": current_spec,
        "action_type": result.action_type,
        "validation_errors": [],
    }


# 2. 完整性檢查與路由節點
def check_completeness(state: ProvisioningState) -> dict[str, Any]:
    """檢查必填欄位是否完整"""
    current_spec = state.get("current_spec") or {}
    missing = [f for f in REQUIRED_FIELDS if not current_spec.get(f)]
    return {"missing_fields": missing}


def route_completeness(state: ProvisioningState) -> str:
    """若有核心欄位缺失則發問，齊全則進入人工審查"""
    missing = state.get("missing_fields", [])
    if any(f in CRITICAL_FIELDS for f in missing):
        return "ask_clarification"
    return "human_review"


# 3. 補充詢問節點
def ask_clarification(state: ProvisioningState) -> dict[str, Any]:
    """向使用者詢問缺少的必要資訊"""
    missing = state.get("missing_fields", [])
    missing_text = "\n".join(f"- {field_label(f)}" for f in missing)
    msg = PROMPTS["clarification"]["reply"].format(missing_fields_text=missing_text)
    return {"messages": [AIMessage(content=msg)]}


# ─── 人工審核：修改內容的解析 (UI 只傳原始字串，所有轉型 / 名稱解析都在這裡) ───
def apply_edits(spec: dict[str, Any], edits: dict[str, str]) -> tuple[dict[str, Any], list[str]]:
    new_spec = dict(spec)
    errors: list[str] = []
    aria = get_aria_client()
    policy = get_vm_policy()

    for key, raw in (edits or {}).items():
        val = (raw or "").strip()
        if key in ("project", "project_name", "project_id"):
            resolved = aria.resolve_project(val)
            if resolved:
                new_spec["project_id"], new_spec["project_name"] = resolved["id"], resolved["name"]
                if "network_segment" not in edits:
                    new_spec["network_segment"] = policy.network_segment_for(resolved["name"])
            else:
                errors.append(f"找不到專案「{val}」")
        elif key in ("blueprint", "blueprint_name", "blueprint_id"):
            resolved = aria.resolve_blueprint(val)
            if resolved:
                new_spec["blueprint_id"], new_spec["blueprint_name"] = resolved["id"], resolved["name"]
            else:
                errors.append(f"找不到藍圖「{val}」")
        elif key == "os_image":
            new_spec["os_image"] = policy.match_os_image(val, _safe_available_images()) or val
        elif key in INT_FIELDS:
            try:
                new_spec[key] = int(val)
            except ValueError:
                errors.append(f"{field_label(key, with_hint=False)} [{key}] 必須是整數：{val}")
        elif key == "ip_mode":
            new_spec[key] = val.upper()
        elif key in VM_FIELDS:
            new_spec[key] = val
        else:
            errors.append(f"未知的欄位：{key}")
    return new_spec, errors


def _enrich_names(spec: dict[str, Any]) -> dict[str, Any]:
    spec = dict(spec)
    aria = get_aria_client()
    if spec.get("project_id") and not spec.get("project_name"):
        spec["project_name"] = aria.get_project_name(spec["project_id"])
    if spec.get("blueprint_id") and not spec.get("blueprint_name"):
        spec["blueprint_name"] = aria.get_blueprint_name(spec["blueprint_id"])
    return spec


def build_review_fields(spec: dict[str, Any]) -> list[ReviewField]:
    rows: list[ReviewField] = []
    for key in REQUIRED_FIELDS:
        if key == "project_id":
            rows.append(ReviewField(key="project", label=field_label("project_name", False),
                                    value=spec.get("project_name") or spec.get("project_id"), editable=True))
        elif key == "blueprint_id":
            rows.append(ReviewField(key="blueprint", label=field_label("blueprint_name", False),
                                    value=spec.get("blueprint_name") or spec.get("blueprint_id"), editable=True))
        else:
            rows.append(ReviewField(key=key, label=field_label(key), value=spec.get(key), editable=True))
    return rows


# 4. 人工審核中斷節點 (HITL)
def human_review(state: ProvisioningState) -> dict[str, Any]:
    """觸發中斷等待人工審查 (送出前已先做 Pydantic 驗證)，支援核准、修改參數、退回或取消"""
    spec = _enrich_names(state.get("current_spec") or {})
    errors = state.get("validation_errors") or validate_vm_spec(spec)

    decision = ask_review(review_request(
        kind="vm_deployment",
        title="📋 待審核的虛擬機規格參數清單",
        message="VM 部署規格已組合完成，請人工審查確認。" + ("（目前規格有錯誤，請以 E 修正或退回）" if errors else ""),
        fields=build_review_fields(spec),
        actions=[APPROVE, EDIT, REJECT, SKIP],
        errors=errors,
    ))
    feedback = decision.model_dump()

    if decision.action in ("approve", "edit"):
        new_spec, edit_errors = apply_edits(spec, decision.edits) if decision.edits else (spec, [])
        all_errors = edit_errors + validate_vm_spec(new_spec)
        return {"current_spec": new_spec, "human_feedback": feedback, "validation_errors": all_errors}

    if decision.action == "skip":
        reason = decision.feedback
        return {
            "current_spec": spec,
            "human_feedback": feedback,
            "validation_errors": [],
            "execution_result": {"aborted": True, "reason": reason or "使用者於人工審查取消"},
            "messages": [AIMessage(content=f"🚫 已取消此 VM 部署。{('原因: ' + reason) if reason else ''}")],
        }

    # reject：帶著意見回到 analyze 重新推論
    return {
        "current_spec": spec,
        "human_feedback": feedback,
        "validation_errors": [],
        "messages": [HumanMessage(content=f"人工審查已退回，意見為: {decision.feedback or '請調整規格'}")],
    }


def route_review_result(state: ProvisioningState) -> str:
    """根據人工審查結果路由"""
    fb = state.get("human_feedback") or {}
    action = fb.get("action")
    if action in ("approve", "edit"):
        return "human_review" if state.get("validation_errors") else "submit_request"
    if action == "skip":
        return END
    return "analyze_and_extract"


# 5. 提交請求節點
def submit_request(state: ProvisioningState) -> dict[str, Any]:
    """將驗證後的規格轉為 API 請求並送出至 Aria Automation"""
    spec = state.get("current_spec") or {}
    emit("[VM Provisioning] 正在送出 Aria Automation 部署請求...", stage="vm_provisioning")

    try:
        validated_params = VMDeploymentParams(**spec)
        payload = validated_params.to_aria_request_payload()
        result = get_aria_client().submit_blueprint_request(payload)
        get_audit_sink().record("vm_deployment_submitted", spec=spec, result=result)

        msg = (
            f"🎉 **VM 部署請求已成功送出！**\n\n"
            f"- **部署名稱**: `{result.get('deployment_name')}`\n"
            f"- **請求編號 (Request ID)**: `{result.get('request_id')}`\n"
            f"- **部署編號 (Deployment ID)**: `{result.get('deployment_id')}`\n"
            f"- **狀態**: `{result.get('status')}`\n"
            f"- **建立時間**: `{result.get('created_at')}`\n\n"
            f"Aria Automation 已開始在背景建立該虛擬機，您可以登入主控台查看進度。"
        )
        return {"execution_result": result, "messages": [AIMessage(content=msg)]}
    except Exception as e:
        get_audit_sink().record("vm_deployment_failed", spec=spec, error=str(e))
        return {
            "execution_result": {"error": str(e)},
            "messages": [AIMessage(content=f"❌ 送出部署請求失敗: {e}")],
        }
