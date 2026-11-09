"""表單欄位解析：意圖 + Aria 表單定義 + policy → 欄位值 (含來源)

與模板的「欄位約定」：以下欄位 ID 由 Aria 藍圖定義，所有 OS 家族的模板共用。
除了這些有特殊處理的欄位，其餘欄位一律依表單選項 / 預設值處理，模板新增欄位不需改程式。
"""
import ipaddress
import re
from typing import Any, Callable, Optional

from clients.aria.models import FormField
from core.policy import VMPolicy

CPU = "cpuCount"
MEMORY = "memorySize"          # 表單 value 單位為 MB (label 為 "16G")
OS_IMAGE = "OSimage"
SG = "sg"
NETWORK = "NetworkSegment"
IP_MODE = "IPmode"
ADDRESS = "address"
CLUSTER = "Cluster"
DATASTORE = "Datastore"
DEPLOYMENT_NAME = "deploymentName"

# 自由文字欄位 → VMIntent 屬性
TEXT_FIELDS = {
    "CaseNo": "case_no",
    "APOwner": "ap_owner",
    "OSOwner": "os_owner",
    "APUnit": "ap_unit",
    "DBOwner": "db_owner",
    "Description": "description",
}

# 值的來源 (審核畫面顯示)
SRC_USER = "審核修改"
SRC_INTENT = "需求敘述"
SRC_LLM = "模型選擇"
SRC_LOOKUP = "Aria 查詢"
SRC_FORM = "表單預設"
SRC_POLICY = "policy 預設"
SRC_RULE = "命名規則"

BLANK = (None, "", " ")


def is_blank(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def manual_ip_value(field: Optional[FormField]) -> str:
    """IPmode 選項中非 DHCP 的那個 (目前為 Manual)"""
    if field and field.options:
        for o in field.options:
            if str(o.value).upper() != "DHCP":
                return str(o.value)
    return "Manual"


def display_value(field: Optional[FormField], value: Any) -> str:
    if value is None:
        return ""
    opt = field.option_for(value) if field else None
    if opt and opt.label != str(opt.value):
        return f"{opt.label} ({opt.value})"
    return str(value)


def parse_memory_mb(raw: Any) -> Optional[int]:
    """'16G' / '16 GB' / '16' (視為 GB，若 >= 512 視為 MB) → MB"""
    text = str(raw).strip().upper().replace(" ", "")
    m = re.fullmatch(r"(\d+)(G|GB)?", text)
    if not m:
        m2 = re.fullmatch(r"(\d+)MB?", text)
        return int(m2.group(1)) if m2 else None
    n = int(m.group(1))
    if m.group(2) or n < 512:
        return n * 1024
    return n


def normalize(field: FormField, raw: Any) -> tuple[Any, Optional[str]]:
    """把使用者輸入的原始值轉為送出用的值；選項以 label 或 value 皆可。回傳 (值, 錯誤訊息)"""
    if isinstance(raw, str):
        raw = raw.strip()
    opt = field.option_for(raw)
    if opt:
        return opt.value, None
    if field.id == MEMORY:
        mb = parse_memory_mb(raw)
        return (mb, None) if mb else (raw, f"{field.label} [{field.id}] 無法解析：{raw} (例如 16G)")
    if field.data_type == "integer":
        try:
            return int(str(raw)), None
        except ValueError:
            return raw, f"{field.label} [{field.id}] 必須是整數：{raw}"
    return raw, None


def _match_option_label(field: Optional[FormField], prefix: str) -> Optional[Any]:
    if not field or not field.options:
        return None
    for o in field.options:
        if o.label.strip().upper().startswith(prefix.upper()) or str(o.value).upper() == prefix.upper():
            return o.value
    return None


def resolve_values(
    intent: dict[str, Any],
    project: Optional[dict[str, str]],
    fields: list[FormField],
    overrides: dict[str, str],
    policy: VMPolicy,
    choose_os_image: Callable[[FormField], tuple[Optional[str], str]],
    next_serial: Callable[[str], int],
) -> tuple[dict[str, Any], dict[str, str], list[str], list[str]]:
    """回傳 (values, sources, notes, errors)"""
    by_id = {f.id: f for f in fields}
    values: dict[str, Any] = {}
    sources: dict[str, str] = {}
    notes: list[str] = []
    errors: list[str] = []

    def put(fid: str, value: Any, source: str) -> None:
        values[fid] = value
        sources[fid] = source

    for f in fields:
        fid = f.id
        if fid == CPU and intent.get("cpu"):
            put(fid, int(intent["cpu"]), SRC_INTENT)
        elif fid == MEMORY and intent.get("memory_gb"):
            put(fid, int(intent["memory_gb"]) * 1024, SRC_INTENT)
        elif fid == OS_IMAGE:
            value, reason = choose_os_image(f)
            if value:
                put(fid, value, SRC_LLM)
                if reason and reason != "唯一選項":
                    notes.append(f"OS image 選擇理由：{reason}")
        elif fid == SG:
            put(fid, intent.get("sg") or policy.default_sg, SRC_INTENT if intent.get("sg") else SRC_POLICY)
        elif fid == NETWORK:
            if project:
                put(fid, policy.network_segment_for(project["name"]), SRC_POLICY)
        elif fid == IP_MODE:
            if intent.get("static_ip"):
                put(fid, manual_ip_value(f), SRC_INTENT)
            else:
                put(fid, f.default or policy.default_ip_mode, SRC_FORM if f.default else SRC_POLICY)
        elif fid == ADDRESS:
            if intent.get("static_ip"):
                put(fid, str(intent["static_ip"]).strip(), SRC_INTENT)
            elif f.default is not None:
                put(fid, f.default, SRC_FORM)
        elif fid == CLUSTER:
            hint = intent.get("cluster")
            matched = None
            if hint and f.options:
                matched = next((o.value for o in f.options
                                if hint.lower() in str(o.value).lower() or hint.lower() in o.label.lower()), None)
            if matched:
                put(fid, matched, SRC_INTENT)
            elif hint:
                put(fid, hint, SRC_INTENT)
            elif f.options:
                put(fid, f.options[0].value, SRC_FORM if len(f.options) == 1 else SRC_POLICY)
        elif fid == DATASTORE:
            storage = intent.get("storage_type") or policy.default_storage_type
            ds = _match_option_label(f, storage)
            if ds is not None:
                put(fid, ds, SRC_INTENT if intent.get("storage_type") else SRC_POLICY)
        elif fid in TEXT_FIELDS:
            val = intent.get(TEXT_FIELDS[fid])
            if not is_blank(val):
                put(fid, val, SRC_INTENT)
            elif fid in policy.field_defaults:
                put(fid, policy.field_defaults[fid], SRC_POLICY)
            elif f.default is not None:
                put(fid, f.default, SRC_FORM)
        elif f.default is not None:
            put(fid, f.default, SRC_FORM)
        elif f.options and len(f.options) == 1:
            put(fid, f.options[0].value, SRC_FORM)

    # 部署名稱
    if intent.get("deployment_name"):
        put(DEPLOYMENT_NAME, intent["deployment_name"], SRC_INTENT)
    elif project:
        function = policy.clean_function(intent.get("function"))
        serial = next_serial(policy.name_prefix(project["name"], function))
        put(DEPLOYMENT_NAME, policy.deployment_name(project["name"], function, serial), SRC_RULE)

    # 使用者在審核中的修改最後套用
    for key, raw in (overrides or {}).items():
        if key in ("project", "template"):
            continue
        if key == DEPLOYMENT_NAME:
            put(key, str(raw).strip(), SRC_USER)
            continue
        f = by_id.get(key)
        if not f:
            errors.append(f"未知的欄位：{key}")
            continue
        value, err = normalize(f, raw)
        if err:
            errors.append(err)
        put(key, value, SRC_USER)
        if key == ADDRESS and not is_blank(value) and IP_MODE in by_id:
            put(IP_MODE, manual_ip_value(by_id[IP_MODE]), SRC_USER)

    # 清單外的值：允許送出，但提示 (表單清單是給外部人員的限制，送出後 Aria 仍有審核)
    for fid in (CPU, MEMORY, OS_IMAGE, CLUSTER, DATASTORE):
        f = by_id.get(fid)
        if f and f.options and not is_blank(values.get(fid)) and not f.option_for(values[fid]):
            notes.append(f"{f.label} = {display_value(f, values[fid])} 不在表單選項內 (允許送出，仍需 Aria 審核)")
    return values, sources, notes, errors


def validate_values(
    fields: list[FormField],
    values: dict[str, Any],
    find_machines_by_ip: Callable[[str], list[dict[str, Any]]],
) -> tuple[list[str], list[str], list[str]]:
    """回傳 (missing, errors, notes)"""
    missing: list[str] = []
    errors: list[str] = []
    notes: list[str] = []
    by_id = {f.id: f for f in fields}

    if is_blank(values.get(DEPLOYMENT_NAME)):
        missing.append("部署名稱 [deploymentName]")
    for f in fields:
        if not f.submit or not f.is_visible(values):
            continue
        v = values.get(f.id)
        if f.required and is_blank(v):
            missing.append(f"{f.label} [{f.id}]")
        elif f.data_type == "integer" and not is_blank(v) and not isinstance(v, int):
            errors.append(f"{f.label} [{f.id}] 必須是整數：{v}")

    ip_field = by_id.get(IP_MODE)
    if ip_field and str(values.get(IP_MODE)) == manual_ip_value(ip_field):
        addr = values.get(ADDRESS)
        if is_blank(addr):
            missing.append("IP Address [address] (IP Mode 為指定 IP 時必填)")
        else:
            try:
                ipaddress.ip_address(str(addr).strip())
            except ValueError:
                errors.append(f"IP Address [address] 格式不正確：{addr}")
            else:
                try:
                    used = find_machines_by_ip(str(addr).strip())
                except Exception:
                    used = []
                if used:
                    names = ", ".join(m.get("name") or "?" for m in used)
                    notes.append(f"IP {addr} 目前已被 {names} 使用 (重新安裝系統時屬正常)")
    return missing, errors, notes
