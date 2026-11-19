"""本地 Ollama REST API 封裝 (無狀態底層 SDK：模型探測、JSON 結構化生成與解析)"""
import json
import re
from functools import lru_cache
from typing import Any, Optional

import requests

import config


class OllamaClient:
    """負責與本機 Ollama 服務溝通 (模型自動適配 + JSON 結構化輸出)"""

    def __init__(self, model: Optional[str] = None, base_url: Optional[str] = None):
        self.base_url = (base_url or config.OLLAMA_BASE_URL).rstrip("/")
        self.configured_model = model or config.OLLAMA_MODEL
        self.timeout = config.OLLAMA_TIMEOUT
        self.num_ctx = config.OLLAMA_NUM_CTX
        self._active_model: Optional[str] = None

    # ────────────────────────────── 模型管理 ──────────────────────────────

    def get_available_models(self) -> list[str]:
        """向 Ollama 查詢本地已下載的模型清單"""
        try:
            resp = requests.get(f"{self.base_url}/api/tags", timeout=5)
            if resp.status_code == 200:
                data = resp.json()
                return [m.get("name", "") for m in data.get("models", [])]
        except Exception:
            pass
        return []

    def get_active_model(self) -> str:
        """決定實際使用的模型 (若配置之模型未下載，則智慧適配本地現存模型)"""
        if self._active_model:
            return self._active_model

        available = self.get_available_models()
        if not available:
            self._active_model = self.configured_model
            return self._active_model

        # 1. 完全比對或前綴比對
        target = self.configured_model.lower()
        for m in available:
            if m.lower() == target or m.lower().startswith(target):
                self._active_model = m
                return self._active_model

        # 2. 若配置的模型尚未下載，自動選取本地現有模型 (優先 gemma 或 qwen)
        for m in available:
            if "gemma" in m.lower() or "qwen" in m.lower():
                self._active_model = m
                return self._active_model

        self._active_model = available[0]
        return self._active_model

    # ────────────────────────────── 底層呼叫 ──────────────────────────────

    def generate_json(self, system: str, prompt: str, schema: Optional[dict[str, Any]] = None) -> str:
        """
        呼叫 /api/generate 並要求 JSON 輸出。
        優先使用 JSON Schema 結構化輸出 (Ollama >= 0.5)，若伺服器不支援則退回 format="json"。
        """
        payload: dict[str, Any] = {
            "model": self.get_active_model(),
            "system": system,
            "prompt": prompt,
            "stream": False,
            "format": schema or "json",
            "options": {"num_ctx": self.num_ctx, "temperature": 0.1},
        }
        url = f"{self.base_url}/api/generate"
        resp = requests.post(url, json=payload, timeout=self.timeout)
        if resp.status_code >= 400 and schema is not None:
            payload["format"] = "json"
            resp = requests.post(url, json=payload, timeout=self.timeout)
        resp.raise_for_status()
        return resp.json().get("response", "").strip()

    @staticmethod
    def extract_json_object(text: str) -> Optional[dict[str, Any]]:
        """清洗 Markdown codeblock 並解析出第一個 JSON 物件"""
        if not text:
            return None
        clean = re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.MULTILINE)
        clean = re.sub(r"\s*```$", "", clean, flags=re.MULTILINE).strip()
        try:
            data = json.loads(clean)
            return data if isinstance(data, dict) else None
        except json.JSONDecodeError:
            pass
        # 退而求其次：抓最外層大括號
        start, end = clean.find("{"), clean.rfind("}")
        if start != -1 and end > start:
            try:
                data = json.loads(clean[start:end + 1])
                return data if isinstance(data, dict) else None
            except json.JSONDecodeError:
                return None
        return None


@lru_cache(maxsize=1)
def get_ollama_client() -> OllamaClient:
    return OllamaClient()
