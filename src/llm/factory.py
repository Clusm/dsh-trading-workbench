"""按 Agent 配置/环境变量创建 LLM 客户端。"""
from __future__ import annotations

import os
from urllib.parse import urlparse
from typing import Any

from core.config_loader import Settings
from core.decision_trace import RouteExpectation
from llm.base import LLMClient
from llm.failover_client import FailoverLLMClient
from llm.mock_client import MockLLMClient
from llm.openai_compatible_client import OpenAICompatibleLLMClient
from llm.request_coordinator import configure_default_coordinator
from llm.workbuddy_client import WorkBuddyLLMClient


def _agent_cfg(settings: Settings, agent_name: str) -> dict[str, Any]:
    for item in settings.raw_agents.get("agents", []):
        if item.get("name") == agent_name:
            return item
    return {}


#: 允许 Mock 的**显式开关**：只有进程显式声明"现在就是 Mock/Demo 形态"时才为真。
#: 缺省 False（fail-closed）——**绝不**由"取不到凭据"反推。
MOCK_ALLOWANCE_ENV = "MTA_ALLOW_MOCK_LLM"
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def mock_explicitly_allowed() -> bool:
    """Mock 是否被**独立于本轮取不到 key 这件事**地显式允许。

    判定必须独立于被守卫对象（B-r1 P1-3）：旧口径把 ``mock_allowed`` 写成
    ``not api_key``，与"降级到 Mock"用的是**同一个条件**，守卫因此在结构上
    不可能发现它所针对的那次静默降级（自证清白）。
    """
    return os.getenv(MOCK_ALLOWANCE_ENV, "").strip().lower() in _TRUTHY


def route_expectation(settings: Settings, agent_name: str, *,
                      mock_allowed: bool | None = None) -> RouteExpectation:
    """本轮**显式**路由期望：供执行侧判定静默 Mock 与参数继承（U05/S02）。

    ``RouteExpectation`` 的字段全部来自配置/环境变量的**解析后有效值**，与
    ``build_llm_for_agent`` 用的是同一套取值口径。**唯一例外是 ``mock_allowed``**：
    它只来自调用方显式传入的 ``mock_allowed`` 参数，或进程级显式开关
    :data:`MOCK_ALLOWANCE_ENV`，缺省 ``False``。**不从"本轮能不能取到 key"推导**——
    否则就是"由被守卫的降级条件自己宣布允许降级"，守卫形同虚设。
    """
    base = settings.llm_config
    acfg = _agent_cfg(settings, agent_name)
    model = str(
        acfg.get("model") or os.getenv(base.get("model_env", "SENSENOVA_MODEL"), "").strip()
        or base.get("default_model", "deepseek-v4-flash")
    )
    fallback_cfg = base.get("decision_fallback", {}) or {}
    fallback_provider = os.getenv(
        str(fallback_cfg.get("provider_env", "MTA_AI_DECISION_FALLBACK_PROVIDER")), ""
    ).strip().lower()
    declared: tuple[str, ...] = ()
    if agent_name == "ai_trader" and fallback_provider == "opencode":
        declared = (str(
            os.getenv(str(fallback_cfg.get("model_env", "OPENCODE_MODEL")), "").strip()
            or str(fallback_cfg.get("default_model", "union-alpha"))
        ),)
    effort = acfg.get("reasoning_effort", base.get("reasoning_effort"))
    return RouteExpectation(
        model=model,
        effort=None if effort is None else str(effort),
        temperature=float(acfg.get("temperature", base.get("temperature", 0.2))),
        max_tokens=int(acfg.get("max_tokens", base.get("max_tokens", 2000))),
        # 显式开关优先；缺省 fail-closed（**不**看 api_key）。
        mock_allowed=(mock_explicitly_allowed() if mock_allowed is None
                      else bool(mock_allowed)),
        require_cross_process=True,
        declared_fallbacks=declared,
    )


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
        client.route_role = "workbuddy"
        client.declared_fallbacks = ()
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
        # **不静默**：把降级原因贴在客户端上，并把 "mock" 作为显式模型标签回传，
        # 调用方据此判定 SILENT_MOCK（U05/S02 §3.3）。只有进程**显式**
        # （MOCK_ALLOWANCE_ENV）声明"现在就是 Mock 形态"时才不标降级原因——
        # "缺 key"本身永远是一次**降级**，不是许可（B-r1 P1-3）。
        allowed = mock_explicitly_allowed()
        degraded = MockLLMClient()
        degraded.degraded_reason = (
            None if allowed
            else ("missing_api_key" if not api_key else "model_not_configured"))
        degraded.route_role = "mock" if allowed else "degraded_mock"
        degraded.declared_fallbacks = ()
        return degraded, "mock"

    base_url = (
        os.getenv(base.get("base_url_env", "SENSENOVA_BASE_URL"), "").strip()
        or base.get("default_base_url", "https://token.sensenova.cn/v1")
    )
    fallback_cfg = base.get("decision_fallback", {}) or {}
    fallback_provider = os.getenv(
        str(fallback_cfg.get("provider_env", "MTA_AI_DECISION_FALLBACK_PROVIDER")), ""
    ).strip().lower()
    use_opencode_fallback = agent_name == "ai_trader" and fallback_provider == "opencode"
    if use_opencode_fallback:
        fallback_key = os.getenv(str(fallback_cfg.get("api_key_env", "OPENCODE_API_KEY")), "").strip()
        fallback_base_url = (os.getenv(str(fallback_cfg.get("base_url_env", "OPENCODE_BASE_URL")), "").strip()
                             or str(fallback_cfg.get("default_base_url", "https://opencode.ai/zen/v1"))).strip()
        fallback_model = (os.getenv(str(fallback_cfg.get("model_env", "OPENCODE_MODEL")), "").strip()
                          or str(fallback_cfg.get("default_model", "union-alpha"))).strip()
        parsed = urlparse(fallback_base_url)
        if (not fallback_key or not fallback_model or fallback_model.lower() == "none"
                or parsed.scheme not in {"https", "http"} or not parsed.hostname
                or parsed.username or parsed.password):
            raise ValueError("Explicit OpenCode fallback requires a valid key, base URL and model")
    primary_max_retries = (
        int(fallback_cfg.get("primary_max_retries", 0))
        if use_opencode_fallback else int(base.get("max_retries", 1))
    )
    client = OpenAICompatibleLLMClient(
        api_key=api_key,
        base_url=base_url,
        model=model,
        temperature=float(acfg.get("temperature", base.get("temperature", 0.2))),
        max_tokens=int(acfg.get("max_tokens", base.get("max_tokens", 2000))),
        timeout_seconds=int(base.get("timeout_seconds", 60)),
        reasoning_effort=acfg.get("reasoning_effort", base.get("reasoning_effort")),
        max_retries=primary_max_retries,
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
    if use_opencode_fallback:
        fallback_key = os.getenv(
            str(fallback_cfg.get("api_key_env", "OPENCODE_API_KEY")), ""
        ).strip()
        if fallback_key:
            fallback_base_url = os.getenv(
                str(fallback_cfg.get("base_url_env", "OPENCODE_BASE_URL")), ""
            ).strip() or str(fallback_cfg.get("default_base_url", "https://opencode.ai/zen/v1"))
            fallback_model = os.getenv(
                str(fallback_cfg.get("model_env", "OPENCODE_MODEL")), ""
            ).strip() or str(fallback_cfg.get("default_model", "union-alpha"))
            fallback = OpenAICompatibleLLMClient(
                api_key=fallback_key,
                base_url=fallback_base_url,
                model=fallback_model,
                temperature=float(acfg.get("temperature", base.get("temperature", 0.2))),
                max_tokens=int(acfg.get("max_tokens", base.get("max_tokens", 2000))),
                timeout_seconds=int(fallback_cfg.get("timeout_seconds", 90)),
                reasoning_effort=acfg.get("reasoning_effort", base.get("reasoning_effort")),
                max_retries=int(fallback_cfg.get("max_retries", 0)),
                retry_backoff_seconds=float(base.get("retry_backoff_seconds", 2.0)),
                purpose="decision",
                coordinator=coordinator,
                default_429_cooldown_seconds=float(
                    fallback_cfg.get("default_429_cooldown_seconds", 5.0)
                ),
            )
            client = FailoverLLMClient(
                client, fallback,
                primary_name=f"sensenova:{model}",
                fallback_name=f"opencode:{fallback_model}",
                min_remaining_seconds=float(
                    fallback_cfg.get("min_remaining_seconds", 15.0)
                ),
                # 备用模型必须**显式声明**：否则备用链路返回的模型名会被判成
                # 静默参数继承（U05/S02 PARAMETER_INHERITANCE）。
                declared_models=(model, fallback_model),
            )
            client.route_role = "failover"
            model = f"{model}|fallback:{fallback_model}"
    return client, model
