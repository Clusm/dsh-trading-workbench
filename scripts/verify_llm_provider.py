"""显式联网验证当前 LLM 供应商的文本生成与 Function Calling。

脚本只发送最小测试提示，不读取交易数据、不创建 TradingService、不输出 API key。
它属于人工上线验收，不应放进离线默认回归集。
"""
from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from core.config_loader import load_settings  # noqa: E402
from llm.factory import build_llm_for_agent  # noqa: E402
from llm.mock_client import MockLLMClient  # noqa: E402


def main() -> int:
    """校验所有 LLM 节点模型一致，并完成一次强制工具调用。"""
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
    if models != {"deepseek-v4-flash"}:
        raise RuntimeError(f"LLM 模型未统一：{sorted(models)}")
    client = clients["ai_trader"][0]
    if isinstance(client, MockLLMClient):
        raise RuntimeError("当前配置回退到 Mock，请检查本机 SenseNova 凭据")
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
    if echoed_model and echoed_model != "deepseek-v4-flash":
        raise RuntimeError(f"供应商模型回显不一致：{echoed_model}")
    print(f"LLM PROVIDER OK model=deepseek-v4-flash agents={len(agent_names)} tool_calls={len(calls)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
