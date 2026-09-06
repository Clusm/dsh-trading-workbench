"""按 Agent 配置/环境变量创建 LLM 客户端。"""
from __future__ import annotations

import os
from typing import Any

from core.config_loader import Settings
from llm.base import LLMClient
from llm.mock_client import MockLLMClient
from llm.openai_compatible_client import OpenAICompatibleLLMClient


def _agent_cfg(settings: Settings, agent_name: str) -> dict[str, Any]:
    for item in settings.raw_agents.get("agents", []):
        if item.get("name") == agent_name:
            return item
    return {}


def build_llm_for_agent(settings: Settings, agent_name: str) -> tuple[LLMClient, str]:
    base = settings.llm_config
    acfg = _agent_cfg(settings, agent_name)
    api_key_env = base.get("api_key_env", "SENSENOVA_API_KEY")
    api_key = os.getenv(api_key_env, "").strip()
    model = (
        str(acfg.get("model") or os.getenv(base.get("model_env", "SENSENOVA_MODEL"), "").strip()
            or base.get("default_model", "deepseek-v4-flash"))
    )
    if not api_key or not model or model in ("", "none"):
        return MockLLMClient(), "mock"

    base_url = (
        os.getenv(base.get("base_url_env", "SENSENOVA_BASE_URL"), "").strip()
        or base.get("default_base_url", "https://token.sensenova.cn/v1")
    )
    client = OpenAICompatibleLLMClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=float(acfg.get("temperature", base.get("temperature", 0.2))),
        max_tokens=int(acfg.get("max_tokens", base.get("max_tokens", 2000))),
        timeout_seconds=int(base.get("timeout_seconds", 60)),
        reasoning_effort=acfg.get("reasoning_effort", base.get("reasoning_effort")),
        max_retries=int(base.get("max_retries", 1)),
        retry_backoff_seconds=float(base.get("retry_backoff_seconds", 2.0)),
    )
    return client, client.model
