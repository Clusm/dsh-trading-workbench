"""显式联网验证当前 LLM 供应商的文本生成与 Function Calling。

脚本只发送最小测试提示，不读取交易数据、不创建 TradingService、不输出 API key。
它属于人工上线验收，不应放进离线默认回归集。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.config_loader import load_settings  # noqa: E402
from llm.factory import build_llm_for_agent  # noqa: E402
from llm.mock_client import MockLLMClient  # noqa: E402
from llm.workbuddy_client import WorkBuddyLLMClient  # noqa: E402


def main() -> int:
    """校验默认提供方，或显式 WorkBuddy 决策适配器的工具协议。"""
    settings = load_settings(PROJECT_ROOT)
    agent_names = [
        "strategy_trader",
        "ai_trader",
        "market_agent",
        "macro_agent",
        "onchain_agent",
        "review_agent",
    ]
    clients = {name: build_llm_for_agent(settings, name) for name in agent_names}
    models = {model for _, model in clients.values()}
    client = clients["ai_trader"][0]
    workbuddy_enabled = os.getenv("MTA_AI_DECISION_PROVIDER", "").strip().lower() == "workbuddy"
    if workbuddy_enabled:
        if not isinstance(client, WorkBuddyLLMClient):
            raise RuntimeError("已请求 WorkBuddy，但 ai_trader 没有载入受限适配器")
        if clients["ai_trader"][1] != "deepseek-v4.1-flash":
            raise RuntimeError(f"WorkBuddy 模型不正确：{clients['ai_trader'][1]}")
        # 只允许自主决策通道切换；其它节点必须继续沿用原供应商，避免一次试用扩大范围。
        others = {model for name, (_, model) in clients.items() if name != "ai_trader"}
        if others != {"deepseek-v4-flash"}:
            raise RuntimeError(f"WorkBuddy 试用意外影响其它 Agent：{sorted(others)}")
    elif models != {"deepseek-v4-flash"}:
        raise RuntimeError(f"LLM 模型未统一：{sorted(models)}")
    if isinstance(client, MockLLMClient):
        raise RuntimeError("当前配置回退到 Mock，请检查本机 SenseNova 凭据")
    if not workbuddy_enabled:
        # 验收只需确认协议能力，不应沿用生产 Agent 的大输出预算。
        client.max_tokens = 128
        if client.reasoning_effort != "xhigh":
            raise RuntimeError(f"AI 决策推理档不是 xhigh：{client.reasoning_effort}")

    response = client.chat(
        [{"role": "user", "content": "调用工具检查 BTCUSDT"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "check_symbol",
                "description": "检查数字货币合约代码",
                "parameters": {
                    "type": "object",
                    "properties": {"symbol": {"type": "string"}},
                    "required": ["symbol"],
                },
            },
        }],
        tool_choice="required",
    )
    choice = (response.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    calls = message.get("tool_calls") or []
    if not calls:
        raise RuntimeError("供应商返回成功，但没有生成要求的工具调用")
    echoed_model = str(response.get("model") or "")
    expected_model = "deepseek-v4.1-flash" if workbuddy_enabled else "deepseek-v4-flash"
    if echoed_model and echoed_model != expected_model:
        raise RuntimeError(f"供应商模型回显不一致：{echoed_model}")
    provider = "workbuddy" if workbuddy_enabled else "sensenova"
    print(f"LLM PROVIDER OK provider={provider} model={expected_model} "
          f"agents={len(agent_names)} tool_calls={len(calls)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
