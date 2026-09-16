"""WorkBuddy 受限结构化决策适配器离线冒烟。"""
from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from llm.workbuddy_client import WorkBuddyLLMClient, WorkBuddyProtocolError  # noqa: E402


FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"[OK] {name}")
    else:
        FAILURES.append(name)
        print(f"[FAIL] {name}: {detail}")


def fake_runtime() -> tuple[Path, Path]:
    root = Path(tempfile.mkdtemp(prefix="mta-workbuddy-smoke-"))
    node = root / "node.exe"
    cli = root / "codebuddy"
    node.touch()
    cli.touch()
    return node, cli


def test_structured_output_translation() -> None:
    node, cli = fake_runtime()
    captured: dict[str, object] = {}

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        captured["command"] = command
        captured["cwd"] = kwargs.get("cwd")
        captured["prompt"] = command[-1]
        return subprocess.CompletedProcess(
            command, 0,
            stdout=json.dumps([{"type": "result", "structured_output": {
                "tool_name": "query_symbol_detail", "arguments": {"symbol": "BTCUSDT"},
            }}]),
            stderr="",
        )

    client = WorkBuddyLLMClient(node_path=node, cli_path=cli, runner=runner)
    tools = [{"type": "function", "function": {
        "name": "query_symbol_detail", "description": "实时行情",
        "parameters": {"type": "object"},
    }}]
    raw = client.chat([{"role": "user", "content": "仅作为测试数据"}], tools=tools)
    call = raw["choices"][0]["message"]["tool_calls"][0]
    command = list(captured["command"])
    check("返回 OpenAI 风格 tool_call", call["function"]["name"] == "query_symbol_detail")
    check("参数保持 JSON 对象", json.loads(call["function"]["arguments"]) == {"symbol": "BTCUSDT"})
    check("仅开放 StructuredOutput", command[command.index("--tools") + 1] == "StructuredOutput")
    check("不加载项目级 WorkBuddy 配置", command[command.index("--setting-sources") + 1] == "user")
    check("禁用外部 MCP", "--strict-mcp-config" in command and "--mcp-config" in command)
    check("提示作为末尾非交互参数传入", "--system-prompt-file" not in command and command[-1] == captured["prompt"])
    check("模型输入包含既有会话", "CONVERSATION" in str(captured["prompt"]))


def test_forced_function_and_invalid_response() -> None:
    node, cli = fake_runtime()

    def runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            command, 0,
            stdout=json.dumps({"structured_output": {
                "tool_name": "query_top_symbols", "arguments": {},
            }}),
            stderr="",
        )

    client = WorkBuddyLLMClient(node_path=node, cli_path=cli, runner=runner)
    tools = [
        {"type": "function", "function": {"name": "query_top_symbols", "parameters": {}}},
        {"type": "function", "function": {"name": "submit_decision", "parameters": {}}},
    ]
    try:
        client.chat(
            [{"role": "user", "content": "test"}], tools=tools,
            tool_choice={"type": "function", "function": {"name": "submit_decision"}},
        )
    except WorkBuddyProtocolError:
        rejected = True
    else:
        rejected = False
    check("强制 submit 时拒绝其它函数", rejected)


def test_factory_opt_in() -> None:
    from core.config_loader import load_settings  # noqa: E402
    from llm.factory import build_llm_for_agent  # noqa: E402

    old = __import__("os").environ.pop("MTA_AI_DECISION_PROVIDER", None)
    try:
        settings = load_settings(ROOT, profile="live")
        normal, _ = build_llm_for_agent(settings, "ai_trader")
        __import__("os").environ["MTA_AI_DECISION_PROVIDER"] = "workbuddy"
        workbuddy, model = build_llm_for_agent(settings, "ai_trader")
        market, _ = build_llm_for_agent(settings, "market_agent")
        check("默认不切换 WorkBuddy", not isinstance(normal, WorkBuddyLLMClient))
        check("仅 ai_trader 受显式变量切换", isinstance(workbuddy, WorkBuddyLLMClient))
        check("模型锁定 DeepSeek V4.1 Flash", model == "deepseek-v4.1-flash")
        check("情报 Agent 保持原提供方", not isinstance(market, WorkBuddyLLMClient))
    finally:
        env = __import__("os").environ
        if old is None:
            env.pop("MTA_AI_DECISION_PROVIDER", None)
        else:
            env["MTA_AI_DECISION_PROVIDER"] = old


def main() -> int:
    test_structured_output_translation()
    test_forced_function_and_invalid_response()
    test_factory_opt_in()
    if FAILURES:
        print("FAILED: " + ", ".join(FAILURES))
        return 1
    print("WORKBUDDY ADAPTER SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
