"""Custom Form：取得並與 catalog schema 合併成 FormField 清單"""
import json
from typing import Any, Optional

from clients.aria.cache import TTLCache
from clients.aria.models import CatalogVersion, FormField, FormOption
from clients.aria.session import AriaSession

# 申請請求的頂層欄位 (不放在 inputs 內)
TOP_LEVEL_FIELDS = ("project", "deploymentName")


def _parse_options(value_list: Any) -> tuple[Optional[list[FormOption]], Optional[str]]:
    if isinstance(value_list, dict):            # {"id": "...", "type": "scriptAction"}
        return None, value_list.get("id")
    if isinstance(value_list, list):
        opts = []
        for v in value_list:
            if isinstance(v, dict):
                opts.append(FormOption(value=v.get("value"), label=str(v.get("label", v.get("value")))))
            else:
                opts.append(FormOption(value=v, label=str(v)))
        return opts, None
    return None, None


def _layout_visibility(layout: dict[str, Any]) -> dict[str, Any]:
    vis: dict[str, Any] = {}
    for page in layout.get("pages") or []:
        for section in page.get("sections") or []:
            for f in section.get("fields") or []:
                vis[f["id"]] = (f.get("state") or {}).get("visible", True)
    return vis


def _layout_order(layout: dict[str, Any]) -> list[str]:
    return [f["id"] for p in layout.get("pages") or [] for s in p.get("sections") or [] for f in s.get("fields") or []]


def build_form_fields(version: CatalogVersion, form: Optional[dict[str, Any]]) -> list[FormField]:
    """
    以 catalog schema 決定「送出哪些欄位、型別、必填」，以 Custom Form 補上 label / 選項 / 預設 / 顯示條件。
    沒有 Custom Form 時，只用 catalog schema 的 enum / default。
    """
    form_schema: dict[str, Any] = {}
    layout: dict[str, Any] = {}
    if form:
        form_json = form.get("form")
        parsed = json.loads(form_json) if isinstance(form_json, str) else (form_json or {})
        form_schema = parsed.get("schema") or {}
        layout = parsed.get("layout") or {}
    visibility = _layout_visibility(layout)

    order = [fid for fid in _layout_order(layout) if fid in version.properties]
    order += [fid for fid in version.properties if fid not in order]

    fields: list[FormField] = []
    for fid in order:
        prop = version.properties[fid]
        fs = form_schema.get(fid) or {}
        options, options_source = _parse_options(fs.get("valueList"))
        if options is None and prop.get("enum"):
            options = [FormOption(value=v, label=str(v)) for v in prop["enum"]]

        default = fs.get("default", prop.get("default"))
        default_source = conditional = None
        if isinstance(default, dict) and default.get("type") == "scriptAction":
            default_source, default = default.get("id"), prop.get("default")
        elif isinstance(default, list):
            conditional, default = default, prop.get("default")

        vis = visibility.get(fid, True)
        fields.append(FormField(
            id=fid,
            label=fs.get("label") or prop.get("title") or fid,
            data_type=prop.get("type") or (fs.get("type") or {}).get("dataType") or "string",
            required=fid in version.required or bool((fs.get("constraints") or {}).get("required")),
            options=options,
            options_source=options_source,
            default=default,
            default_source=default_source,
            conditional_default=conditional,
            visible_when=vis if isinstance(vis, list) else None,
            submit=True,
            description=fs.get("description") or prop.get("description") or "",
        ))
    return fields


class FormsAPI:
    def __init__(self, session: AriaSession, cache_ttl: float = 300):
        self.s = session
        self._cache = TTLCache(cache_ttl)

    def get_form(self, form_id: str) -> dict[str, Any]:
        return self._cache.get_or_set(form_id, lambda: self.s.get(f"/form-service/api/forms/{form_id}"))

    def fields_for(self, version: CatalogVersion) -> list[FormField]:
        form = self.get_form(version.form_id) if version.form_id else None
        return build_form_fields(version, form)
