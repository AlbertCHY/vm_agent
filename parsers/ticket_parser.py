"""通用工單解析引擎

流程：原始工單文字 ─► parsers.ticket_form 去雜訊擷取提示 ─► 本地 Ollama 判斷 ticket_type 並拆解 action_items ─► ParsedTicket
"""
from typing import Any, Optional, get_args

import requests

import config
from clients.ollama_client import OllamaClient, get_ollama_client
from parsers.ticket_form import preprocess_ticket
from schemas import ParsedTicket, TicketType

VALID_TICKET_TYPES: tuple[str, ...] = get_args(TicketType)
_MISSING = ("", "未提供", "無", "N/A", "n/a", None)


class TicketParseError(RuntimeError):
    """工單解析失敗 (Ollama 連線異常或回傳內容無法解析為 ParsedTicket)"""


def _llm_output_schema() -> dict[str, Any]:
    """提供給 Ollama 的 JSON Schema (ParsedTicket 去掉 raw_content)"""
    return {
        "type": "object",
        "properties": {
            "ticket_id": {"type": "string"},
            "applicant": {"type": "string"},
            "department": {"type": "string"},
            "ticket_type": {"type": "string", "enum": list(VALID_TICKET_TYPES)},
            "summary": {"type": "string"},
            "approval_notes": {"type": "string"},
            "action_items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"description": {"type": "string"}},
                    "required": ["description"],
                    "additionalProperties": True,
                },
            },
        },
        "required": ["ticket_id", "applicant", "department", "ticket_type", "summary", "action_items"],
    }


def build_structured_hint(info: dict[str, Any]) -> str:
    """將表單預處理器 (parsers.ticket_form) 結果整理成提示區塊；表單格式不符時回報未辨識"""
    if not info or info.get("單號") in _MISSING:
        return "（未辨識為標準申請單格式，請直接閱讀原文）"

    comments = info.get("審核覆寫意見") or []
    res = info.get("資源總計") or {}
    return (
        f"【工單單號】：{info.get('單號', '未提供')}\n"
        f"【申請類別】：{info.get('申請類別', '未提供')}\n"
        f"【申請人】：{info.get('申請人', '未提供')} (工號: {info.get('申請人工號', '未提供')}, "
        f"單位: {info.get('申請單位', '未提供')})\n"
        f"【專案名稱】：{info.get('專案名稱', '未提供')}\n"
        f"【資源管理人】：{info.get('資源管理人', '未提供')} (工號: {info.get('資源管理人工號', '未提供')})\n"
        f"【專案背景】：{info.get('專案背景', '無')}\n"
        f"【有效簽核意見】：{'; '.join(comments) if comments else '無'}\n"
        f"【資源總計】：VM總數: {res.get('VM總數', 1)}, vCore總數: {res.get('vCore總數', 0)}, "
        f"Memory總數: {res.get('Memory總數GB', 0)}GB, HDD總數: {res.get('HDD總數GB', 0)}GB, "
        f"SSD總數: {res.get('SSD總數GB', 0)}GB\n"
        f"【VM 主機規格表格內容】：\n{(info.get('VM表格') or '').strip() or '無主機表格內容'}"
    )


def normalize_action_items(items: Any) -> list[dict[str, Any]]:
    """確保 action_items 為 list[dict] 且每筆都有 description"""
    if not isinstance(items, list):
        return []
    normalized: list[dict[str, Any]] = []
    for it in items:
        if isinstance(it, str) and it.strip():
            normalized.append({"description": it.strip()})
        elif isinstance(it, dict) and it:
            item = {k: v for k, v in it.items() if v not in (None, "")}
            if not item.get("description"):
                # 由結構化欄位合成描述，確保後續子圖有自然語言可用
                item["description"] = "，".join(f"{k}: {v}" for k, v in item.items())
            normalized.append(item)
    return normalized


def parse_ticket(
    raw_text: str,
    source_name: Optional[str] = None,
    ollama_client: Optional[OllamaClient] = None,
) -> ParsedTicket:
    """
    解析任意工單文字為通用 ParsedTicket。

    Raises:
        TicketParseError: Ollama 連線失敗或回傳內容無法解析。
    """
    if not raw_text or not raw_text.strip():
        raise TicketParseError("工單內容為空")

    prompts = config.PROMPTS.get("ollama", {})
    system_prompt = prompts.get("system")
    user_template = prompts.get("reply")
    if not system_prompt or not user_template:
        raise TicketParseError("prompts.toml 缺少 [ollama] system / reply 設定")

    info: dict[str, Any] = {}
    try:
        info = preprocess_ticket(raw_text) or {}
    except Exception:
        info = {}

    truncated = raw_text.strip()
    if len(truncated) > config.OLLAMA_MAX_RAW_CHARS:
        truncated = truncated[:config.OLLAMA_MAX_RAW_CHARS] + "\n...(以下內容過長已截斷)"

    user_prompt = user_template.format(
        source_name=source_name or "使用者輸入",
        structured_hint=build_structured_hint(info),
        raw_text=truncated,
    )

    client = ollama_client or get_ollama_client()
    try:
        raw_response = client.generate_json(system_prompt, user_prompt, schema=_llm_output_schema())
    except requests.RequestException as e:
        raise TicketParseError(
            f"Ollama 連線或推理失敗 ({client.base_url}, model={client.get_active_model()}): {e}"
        ) from e

    data = client.extract_json_object(raw_response)
    if not data:
        raise TicketParseError(f"Ollama 回傳內容非有效 JSON：{raw_response[:300]}")

    # ── 正規化與以表單預處理結果補值 (避免小模型漏填) ──
    def pick(llm_key: str, info_key: Optional[str] = None, default: str = "未提供") -> str:
        val = data.get(llm_key)
        if isinstance(val, str) and val.strip() not in _MISSING:
            return val.strip()
        if info_key and info.get(info_key) not in _MISSING:
            return str(info[info_key]).strip()
        return default

    ticket_type = str(data.get("ticket_type", "UNKNOWN")).strip().upper()
    if ticket_type not in VALID_TICKET_TYPES:
        ticket_type = "UNKNOWN"

    approval = data.get("approval_notes")
    if isinstance(approval, list):
        approval = "; ".join(str(a) for a in approval)
    if not approval and info.get("審核覆寫意見"):
        approval = "; ".join(info["審核覆寫意見"])

    try:
        return ParsedTicket(
            ticket_id=pick("ticket_id", "單號", default=source_name or "未提供"),
            applicant=pick("applicant", "申請人"),
            department=pick("department", "申請單位"),
            ticket_type=ticket_type,
            summary=pick("summary", default="(模型未提供摘要)"),
            approval_notes=str(approval or ""),
            action_items=normalize_action_items(data.get("action_items")),
            raw_content=raw_text,
        )
    except Exception as e:
        raise TicketParseError(f"解析結果不符合 ParsedTicket 結構：{e}") from e
