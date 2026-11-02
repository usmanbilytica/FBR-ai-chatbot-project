from __future__ import annotations

import json
import logging
from typing import Any

from openai import OpenAI

from app.core.config import get_settings

log = logging.getLogger(__name__)


def get_llm_client() -> OpenAI:
    s = get_settings()
    return OpenAI(api_key=s.llm_api_key, base_url=s.llm_base_url)


def chat_completion(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.1,
    max_tokens: int = 2048,
) -> str:
    s = get_settings()
    client = get_llm_client()
    resp = client.chat.completions.create(
        model=s.llm_model_name,
        messages=messages,  # type: ignore[arg-type]
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return (resp.choices[0].message.content or "").strip()


def chat_json(
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.0,
) -> dict[str, Any]:
    raw = chat_completion(messages, temperature=temperature)
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:].strip()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start >= 0 and end > start:
            return json.loads(raw[start : end + 1])
        raise
