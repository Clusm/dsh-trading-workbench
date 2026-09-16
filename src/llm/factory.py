"""按 Agent 配置/环境变量创建 LLM 客户端。"""
from __future__ import annotations

import os
from typing import Any

from core.config_loader import Settings
from llm.base import LLMClient
from llm.mock_client import MockLLMClient
from llm.openai_compatible_client import OpenAICompatibleLLMClient
from llm.request_coordinator import configure_default_coordinator
from llm.workbuddy_client import WorkBuddyLLMClient


def _agent_cfg(settings: Settings, agent_name: str) -> dict[str, Any]:
    for item in settings.raw_agents.get("agents", []):
        if item.get("name") == agent_name:
            return item
    return {}


def build_llm_for_agent(settings: Settings, agent_name: str) -> tuple[LLMClient, str]:
    base = settings.llm_config
    # WorkBuddy 是桌面端编码 Agent，不可作为通用供应商替换。仅当明确的进程环境变量
    # 开启时，才把 ai_trader 接到其受限 StructuredOutput 适配器；这让 Demo、情报摘要
    # 与复盘仍使用原有 SenseNova 链路，也避免配置文件误改后静默进入实盘。
    decision_provider = os.getenv("MTA_AI_DECISION_PROVIDER", "").strip().lower()
    if agent_name == "ai_trader" and decision_provider == "workbuddy":
        wb_cfg = base.get("workbuddy", {}) or {}
        client = WorkBuddyLLMClient(
            model=str(wb_cfg.get("model", "deepseek-v4.1-flash")),
            timeout_seconds=float(wb_cfg.get("timeout_seconds", 90)),
            effort=str(wb_cfg.get("effort", "max")),
        )
        return client, client.model
    coordinator_cfg = base.get("coordinator", {}) or {}
    coordinator = configure_default_coordinator(
        max_concurrency=int(coordinator_cfg.get("max_concurrency", 1)),
        queue_poll_seconds=float(coordinator_cfg.get("queue_poll_seconds", 0.25)),
        default_queue_deadline_seconds=float(
            coordinator_cfg.get("queue_deadline_seconds", 30.0)
        ),
    )
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
        purpose=("decision" if agent_name == "ai_trader"
                 else "review" if agent_name == "review_agent"
                 else "intel" if agent_name.endswith("_agent")
                 else "normal"),
        coordinator=coordinator,
        default_429_cooldown_seconds=float(
            base.get("default_429_cooldown_seconds", 5.0)
        ),
    )
    return client, client.model
