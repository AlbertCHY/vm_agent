"""LLM 的兩種用途：抽取意圖 (parse_intent) 與從候選清單中選擇 (choose)

choose() 以動態 Literal 型別限制輸出，模型不可能選出清單外的值；
同一組候選的結果會快取在 state，審核修改後重算時不會重新呼叫模型。
"""
import hashlib
import json
from typing import Any, Literal, Optional

from langchain_core.messages import BaseMessage, HumanMessage, SystemMessage
from pydantic import Field, create_model

import config
from clients.llm import get_llm
from subgraphs.vm_provisioning.state import VMIntent


def parse_intent(messages: list[BaseMessage], prior: dict[str, Any]) -> dict[str, Any]:
    """從對話抽取 VMIntent，並與先前累積的意圖合併 (新值覆蓋舊值，null 不覆蓋)"""
    system = f"{config.PROMPTS['vm_intent']['system']}\n\n目前已知的需求 (JSON)：{json.dumps(prior, ensure_ascii=False)}"
    result: VMIntent = get_llm().with_structured_output(VMIntent).invoke([SystemMessage(content=system)] + messages)
    merged = dict(prior)
    merged.update({k: v for k, v in result.model_dump().items() if v not in (None, "")})
    return merged


def _candidates_key(candidates: list[tuple[str, str]], context: str) -> str:
    raw = json.dumps([candidates, context], ensure_ascii=False, sort_keys=True)
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def choose(
    name: str,
    candidates: list[tuple[str, str]],
    instruction: str,
    context: str,
    cache: dict[str, dict[str, Any]],
) -> tuple[Optional[str], str]:
    """
    從 candidates [(value, label)] 中依 context 選一個。
    回傳 (value, reason)；並將結果寫入 cache[name]。
    """
    if not candidates:
        return None, "沒有可選的候選"
    if len(candidates) == 1:
        return candidates[0][0], "唯一選項"

    key = _candidates_key(candidates, context)
    hit = cache.get(name)
    if hit and hit.get("key") == key:
        return hit["value"], hit.get("reason", "")

    values = tuple(v for v, _ in candidates)
    model = create_model(
        f"{name.title().replace('_', '')}Choice",
        choice=(Literal[values], Field(description="從候選清單中選出的值 (必須完全相同)")),  # type: ignore[valid-type]
        reason=(str, Field(description="選擇理由，一句話")),
    )
    listing = "\n".join(f"- {v}" + (f"  ({label})" if label and label != v else "") for v, label in candidates)
    prompt = f"{instruction}\n\n【需求】\n{context}\n\n【候選清單】\n{listing}"
    result = get_llm().with_structured_output(model).invoke([
        SystemMessage(content=config.PROMPTS["vm_choice"]["system"]),
        HumanMessage(content=prompt),
    ])
    cache[name] = {"key": key, "value": result.choice, "reason": result.reason}
    return result.choice, result.reason
