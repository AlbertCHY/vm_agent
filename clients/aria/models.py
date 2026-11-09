"""Aria API 的資料模型 (catalog / form / deployment)"""
from typing import Any, Optional

from pydantic import BaseModel, Field


class CatalogItem(BaseModel):
    id: str
    name: str
    description: str = ""
    type: str = ""


class CatalogVersion(BaseModel):
    """catalog item 某一版本的 inputs schema"""
    item_id: str
    version: str
    form_id: Optional[str] = None
    properties: dict[str, dict[str, Any]] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)


class FormOption(BaseModel):
    value: Any
    label: str


class FormField(BaseModel):
    """catalog schema + Custom Form 合併後的單一欄位定義"""
    id: str
    label: str
    data_type: str = "string"
    required: bool = False
    options: Optional[list[FormOption]] = None       # 常數下拉選項
    options_source: Optional[str] = None             # 動態選項來源 (scriptAction id)
    default: Any = None                              # 常數預設值
    default_source: Optional[str] = None             # 動態預設來源 (scriptAction id)
    conditional_default: Optional[list[dict[str, Any]]] = None
    visible_when: Optional[list[dict[str, Any]]] = None  # [{"equals": {...}, "value": bool}]
    submit: bool = True                              # 是否屬於 catalog schema (決定是否送出)
    description: str = ""

    def option_for(self, raw: Any) -> Optional[FormOption]:
        """以 value 或 label (不分大小寫) 找出對應選項"""
        if not self.options or raw is None:
            return None
        text = str(raw).strip().lower()
        for opt in self.options:
            if str(opt.value).lower() == text or opt.label.lower() == text:
                return opt
        return None

    def is_visible(self, values: dict[str, Any]) -> bool:
        if not self.visible_when:
            return True
        for cond in self.visible_when:
            equals = cond.get("equals") or {}
            if all(values.get(k) == v for k, v in equals.items()):
                return bool(cond.get("value", True))
        return True


class CatalogRequestResult(BaseModel):
    deployment_id: Optional[str] = None
    deployment_name: Optional[str] = None
    raw: Any = None
