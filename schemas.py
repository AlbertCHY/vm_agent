"""跨模組共用的領域模型 (工單、VM 開立規格)

各子圖自己的 LangGraph State 定義在 subgraphs/<name>/state.py。
VM 規格欄位的中文標籤、是否必填 / 關鍵欄位等 metadata 只在 VMDeploymentParams 定義一次，
其餘地方 (完整性檢查、補問訊息、審核畫面) 一律由此推導。
"""
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, ValidationError, create_model, field_validator

from core.policy import get_vm_policy

# ════════════════════════════════════════════════════════════════
# A. 通用工單資料模型 (Ticket Subsystem ⇄ Supervisor ⇄ Subgraphs)
# ════════════════════════════════════════════════════════════════

# 工單類型列舉 (為未來彈性預留)
TicketType = Literal[
    "CREATE_VM",          # 建立虛擬機
    "EXPAND_RESOURCE",    # 資源擴容 (未來)
    "DECOMMISSION_VM",    # 註銷/下線虛擬機 (未來)
    "NETWORK_ADJUSTMENT", # 網路變更 (未來)
    "UNKNOWN"             # 未知/人工處理
]


class ParsedTicket(BaseModel):
    """統一工單封裝模型 (Ollama 解析產出)"""
    ticket_id: str = Field(description="工單單號，如 SRA26100500001")
    applicant: str = Field(description="申請人姓名/工號")
    department: str = Field(description="申請人部門單位")
    ticket_type: TicketType = Field(description="識別出的工單類型")
    summary: str = Field(description="工單需求摘要說明")
    approval_notes: Optional[str] = Field(default="", description="簽核審批意見")

    # 根據不同 ticket_type 存放具體的負載陣列
    # 例如 CREATE_VM 時，items 存放各台機器的描述文字或規格字典
    action_items: List[dict[str, Any]] = Field(
        default_factory=list,
        description="拆解出的具體動作清單 (每筆項目可獨立交由對應 Subgraph 處理)"
    )
    raw_content: Optional[str] = Field(default="", description="原始工單文本")


# ════════════════════════════════════════════════════════════════
# B. VM 開立規格 (單一欄位來源)
# ════════════════════════════════════════════════════════════════

def _meta(label: str, *, required: bool = True, critical: bool = False, hint: str = "") -> dict[str, Any]:
    return {"label": label, "required": required, "critical": critical, "hint": hint}


class VMDeploymentParams(BaseModel):
    """完整的 VM 建立參數 (送出 Aria 前的最終驗證)；選項與上下限由 policies/vm_defaults.toml 決定"""
    deployment_name: str = Field(description="部署名稱 (英文數字底線破折號)",
                                 json_schema_extra=_meta("部署名稱", hint="英文、數字、底線、破折號"))
    blueprint_id: str = Field(description="藍圖範本 ID (UUID)",
                              json_schema_extra=_meta("藍圖範本", critical=True, hint="例如 Windows、Ubuntu"))
    project_id: str = Field(description="Aria Automation 專案 ID (UUID)",
                            json_schema_extra=_meta("Aria 專案名稱", critical=True))
    blueprint_name: Optional[str] = Field(default=None, description="藍圖範本名稱 (方便人工辨識)",
                                          json_schema_extra=_meta("藍圖範本名稱", required=False))
    project_name: Optional[str] = Field(default=None, description="專案名稱 (方便人工辨識)",
                                        json_schema_extra=_meta("專案名稱", required=False))
    cpu_count: int = Field(description="CPU 核心數 (vCore)",
                           json_schema_extra=_meta("CPU 核心數 (vCore)", critical=True, hint="例如 4"))
    memory_size: int = Field(description="記憶體大小 (MB)",
                             json_schema_extra=_meta("記憶體容量 (MB)", critical=True, hint="例如 16GB = 16384"))
    os_image: str = Field(description="作業系統映像檔在系統 image-profiles 中的特定名稱",
                          json_schema_extra=_meta("作業系統映像檔", hint="例如 Windows Server 2022、Ubuntu 22.04"))
    sg: str = Field(description="NSX-T 安全群組 Application Type",
                    json_schema_extra=_meta("安全群組 (SG)"))
    network_segment: str = Field(description="網段選項/Network",
                                 json_schema_extra=_meta("網路網段 (Network)"))
    ip_mode: Literal["DHCP", "STATIC"] = Field(default="DHCP", description="IP 取得模式 (DHCP 或 STATIC)",
                                               json_schema_extra=_meta("IP 模式", hint="DHCP / STATIC"))
    cluster: str = Field(description="機器建立運算群組位置 (Cluster)",
                         json_schema_extra=_meta("運算集群 (Cluster)"))
    datastore: str = Field(description="虛擬機存放存儲位置 (Datastore)",
                           json_schema_extra=_meta("存儲位置 (Datastore)"))
    case_no: str = Field(description="需求單號或工單號 (CaseNo)",
                         json_schema_extra=_meta("需求單號 (CaseNo)", critical=True))
    ap_owner: str = Field(description="應用系統負責人 (APOwner)",
                          json_schema_extra=_meta("AP 負責人 (APOwner)", critical=True))
    os_owner: str = Field(description="作業系統負責人 (OSOwner)",
                          json_schema_extra=_meta("OS 負責人 (OSOwner)"))
    ap_unit: str = Field(description="應用單位或部門 (APUnit)",
                         json_schema_extra=_meta("應用單位 (APUnit)"))
    description: str = Field(default="", description="用途或備註描述",
                             json_schema_extra=_meta("用途描述 (Description)"))

    @field_validator("cpu_count")
    @classmethod
    def _check_cpu(cls, v: int) -> int:
        lim = get_vm_policy().limits
        lo, hi = lim.get("cpu_min", 1), lim.get("cpu_max", 128)
        if not lo <= v <= hi:
            raise ValueError(f"CPU 核心數需介於 {lo} ~ {hi}")
        return v

    @field_validator("memory_size")
    @classmethod
    def _check_memory(cls, v: int) -> int:
        lim = get_vm_policy().limits
        lo, hi = lim.get("memory_mb_min", 1), lim.get("memory_mb_max", 65536)
        if not lo <= v <= hi:
            raise ValueError(f"記憶體需介於 {lo} ~ {hi} MB")
        return v

    @field_validator("sg")
    @classmethod
    def _check_sg(cls, v: str) -> str:
        allowed = get_vm_policy().sg_allowed
        if allowed and v not in allowed:
            raise ValueError(f"安全群組必須是 {allowed} 其中之一")
        return v

    @field_validator("datastore")
    @classmethod
    def _check_datastore(cls, v: str) -> str:
        allowed = get_vm_policy().datastore_allowed
        if allowed and v not in allowed:
            raise ValueError(f"存儲位置必須是 {allowed} 其中之一")
        return v

    def to_aria_request_payload(self) -> dict[str, Any]:
        """轉換為 Aria Automation /blueprint/api/blueprint-requests 所需的 JSON body"""
        return {
            "deploymentName": self.deployment_name,
            "blueprintId": self.blueprint_id,
            "projectId": self.project_id,
            "inputs": {
                "cpuCount": self.cpu_count,
                "memorySize": self.memory_size,
                "OSimage": self.os_image,
                "sg": self.sg,
                "NetworkSegment": self.network_segment,
                "IPmode": self.ip_mode,
                "Cluster": self.cluster,
                "Datastore": self.datastore,
                "CaseNo": self.case_no,
                "APOwner": self.ap_owner,
                "OSOwner": self.os_owner,
                "APUnit": self.ap_unit,
                "DBOwner": "",
                "Description": self.description
            },
            "reason": self.description or "Automated deployment by Aria Agent"
        }


# ─── 由 VMDeploymentParams 推導的欄位清單 ───
def _field_extra(name: str) -> dict[str, Any]:
    return VMDeploymentParams.model_fields[name].json_schema_extra or {}  # type: ignore[return-value]


VM_FIELDS: list[str] = list(VMDeploymentParams.model_fields)
REQUIRED_FIELDS: list[str] = [f for f in VM_FIELDS if _field_extra(f).get("required")]
CRITICAL_FIELDS: list[str] = [f for f in VM_FIELDS if _field_extra(f).get("critical")]
# 送出前需轉型為整數的欄位
INT_FIELDS: tuple[str, ...] = ("cpu_count", "memory_size")


def field_label(name: str, with_hint: bool = True) -> str:
    """欄位中文標籤；選項類欄位會附上 policy 允許的值"""
    extra = _field_extra(name) if name in VMDeploymentParams.model_fields else {}
    label = extra.get("label", name)
    if not with_hint:
        return label
    policy = get_vm_policy()
    choices = {
        "sg": policy.sg_allowed,
        "datastore": policy.datastore_allowed,
    }.get(name)
    if choices:
        return f"{label} (可選：{' / '.join(choices)})"
    hint = extra.get("hint", "")
    return f"{label} ({hint})" if hint else label


def validate_vm_spec(spec: dict[str, Any]) -> list[str]:
    """回傳人類可讀的驗證錯誤清單 (空清單代表可送出)"""
    try:
        VMDeploymentParams(**spec)
    except ValidationError as e:
        errors: list[str] = []
        for err in e.errors():
            loc = ".".join(str(x) for x in err.get("loc", ()))
            label = field_label(loc, with_hint=False) if loc in VMDeploymentParams.model_fields else loc
            msg = err.get("msg", "").removeprefix("Value error, ")
            errors.append(f"{label} [{loc}]：{msg}")
        return errors
    return []


# 暫存或提取中的部分參數 (欄位皆為 Optional，供模型逐步抽取)；由 VMDeploymentParams 自動產生
PartialVMDeploymentParams = create_model(
    "PartialVMDeploymentParams",
    **{
        name: (Optional[f.annotation], Field(default=None, description=f.description))  # type: ignore[valid-type]
        for name, f in VMDeploymentParams.model_fields.items()
    },
)
