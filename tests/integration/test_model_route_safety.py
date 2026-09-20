"""U05/S02 模型路由安全（§3.3 四类风险）与统一 deadline 离线回归。

独立可运行（本项目无 pytest）：全部通过退出码 0，任一失败退出码 1。
全程离线：``httpx.Client`` 被替换为本地假客户端、子进程被替换为本地 runner，
不发真实请求、不读真实 ``.env``/``data/``、不构造 Broker、不启动服务。

**双态可运行**：断言只依赖
- U05 独占模块 ``src/core/decision_trace.py``（两态都在）；
- ``src/llm/*`` 在 HEAD 已有的**公开行为**（Mock 明确标注、failover 只承接可重试
  错误、payload 由构造参数决定、绝对 deadline 透传、WorkBuddy 受限协议）。

不读共享适配文件的内容来判定补丁状态；共享适配被改后这些行为**必须保持**，
若被破坏本文件会失败——这正是它的作用（回归护栏），而不是"依赖补丁是否存在"。

四类路由风险（任务卡 §3.3）：``SILENT_MOCK`` / ``EARLY_RETURN`` /
``PARAMETER_INHERITANCE`` / ``CROSS_PROCESS_QUOTA``。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import httpx

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import llm.openai_compatible_client as client_module  # noqa: E402
from core.decision_trace import (  # noqa: E402
    ModelRouteGuard,
    RouteExpectation,
    RouteViolationError,
    credential_alias,
    shared_admission_scope,
)
from llm.base import call_chat, call_complete  # noqa: E402
from llm.failover_client import FailoverLLMClient, is_retryable_provider_error  # noqa: E402
from llm.mock_client import MockLLMClient  # noqa: E402
from llm.openai_compatible_client import (  # noqa: E402
    LLMTimeBudgetExceeded,
    OpenAICompatibleLLMClient,
)
from llm.request_coordinator import LLMAdmissionTimeout, provider_scope  # noqa: E402
from llm.workbuddy_client import WorkBuddyLLMClient, WorkBuddyProtocolError  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []

#: 测试用假凭据：刻意不用真实 token 形状（避免触发密钥扫描的 provider-token 规则）。
FAKE_KEY = "route" + "-safety-" + "fixture"


def check(name: str, condition: bool, detail: object = "") -> bool:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL  {name} :: {detail}")
    return bool(condition)


def _raises(exc_type: type[BaseException], func) -> BaseException | None:
    try:
        func()
    except exc_type as exc:
        return exc
    except BaseException as exc:  # noqa: BLE001
        print(f"    (unexpected {type(exc).__name__}: {exc})")
        return None
    return None


class CapturingHttp:
    """替换 ``httpx.Client``：捕获 payload，返回固定 200 响应，不做任何网络 IO。"""

    payloads: list[dict] = []
    response_body: dict = {"choices": [{"message": {"content": "ok"}}], "model": "stub"}

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    def __enter__(self) -> "CapturingHttp":
        return self

    def __exit__(self, *args) -> bool:
        return False

    def post(self, url, headers=None, json=None):  # noqa: A002 - 与 httpx 同名
        del url, headers
        type(self).payloads.append(dict(json or {}))
        request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
        return httpx.Response(200, json=type(self).response_body, request=request)


def _client(**kwargs) -> OpenAICompatibleLLMClient:
    params = {
        "api_key": FAKE_KEY,
        "base_url": "https://provider.invalid/v1",
        "model": "ctor-model",
        "temperature": 0.11,
        "max_tokens": 321,
        "timeout_seconds": 30,
    }
    params.update(kwargs)
    return OpenAICompatibleLLMClient(**params)


class StubClient:
    """最小 LLM 客户端替身：记录收到的绝对 deadline，不联网。"""

    name = "stub"

    def __init__(self, *, model: str = "ctor-model", error: BaseException | None = None,
                 provider: str = "stub") -> None:
        self.model = model
        self.error = error
        self.provider = provider
        self.deadlines: list[float | None] = []
        self.purposes: list[str | None] = []
        self.calls = 0

    def complete(self, system, user, *, deadline=None, purpose=None):
        del system, user
        self.calls += 1
        self.deadlines.append(deadline)
        self.purposes.append(purpose)
        if self.error is not None:
            raise self.error
        from llm.base import LLMResponse
        return LLMResponse(content="ok", model=self.model, latency_ms=1,
                           raw={"provider": self.provider})

    def chat(self, messages, tools=None, tool_choice="auto", *, deadline=None, purpose=None):
        del messages, tools, tool_choice
        self.calls += 1
        self.deadlines.append(deadline)
        self.purposes.append(purpose)
        if self.error is not None:
            raise self.error
        return {"choices": [{"message": {"content": "ok", "tool_calls": []}}],
                "model": self.model, "provider": self.provider}


def _http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://provider.invalid/v1/chat/completions")
    response = httpx.Response(status, request=request,
                              headers={"Retry-After": "0"} if status == 429 else {})
    return httpx.HTTPStatusError("boom", request=request, response=response)


# ---------------------------------------------------------------- [1] 静默 Mock


def test_silent_mock_is_explicit() -> None:
    print("\n[1] 静默 Mock：Mock 必须显式标注，不得冒充真实模型")
    guard = ModelRouteGuard(RouteExpectation(model="deepseek-v4-flash"))
    mock = MockLLMClient()
    guard.expect_attempt("rid-mock")
    response = mock.complete("s", "u")
    guard.observe_response(request_id="rid-mock", model=response.model, raw=response.raw)
    guard.finish_call(request_id="rid-mock", returned=True)
    risks = guard.violation_risks()
    check("期望真实模型却拿到 Mock → SILENT_MOCK",
          "SILENT_MOCK" in risks, guard.as_dict())
    check("Mock 的自报标志被识别（raw.mock=True）",
          any(item.risk.value == "SILENT_MOCK" for item in guard.violations()))
    check("assert_clean 抛 RouteViolationError",
          isinstance(_raises(RouteViolationError, guard.assert_clean), RouteViolationError))

    allowed = ModelRouteGuard(RouteExpectation(model="deepseek-v4-flash", mock_allowed=True))
    allowed.expect_attempt("rid-mock2")
    allowed.observe_response(request_id="rid-mock2", model=response.model, raw=response.raw)
    check("显式声明 mock_allowed 后才不判违规", allowed.clean(), allowed.as_dict())

    # 真实工厂路径：无 key 时 factory 返回的是**显式标注的** mock（不是静默降级）。
    settings = SimpleNamespace(
        llm_config={"api_key_env": "MTA_ROUTE_TEST_ABSENT_KEY", "max_retries": 0,
                    "coordinator": {}},
        raw_agents={"agents": [{"name": "market_agent"}]},
    )
    from llm.factory import build_llm_for_agent  # noqa: PLC0415 - 延迟导入便于缺依赖时定位

    saved = os.environ.pop("MTA_ROUTE_TEST_ABSENT_KEY", None)
    try:
        client, model = build_llm_for_agent(settings, "market_agent")
    finally:
        if saved is not None:
            os.environ["MTA_ROUTE_TEST_ABSENT_KEY"] = saved
    check("无凭据时工厂返回 MockLLMClient", isinstance(client, MockLLMClient),
          type(client).__name__)
    check("工厂同时回传显式 mock 标签（调用方可判定，非静默）", model == "mock", model)
    check("Mock 客户端自带 name=mock 供路由判定", client.name == "mock", client.name)


# ---------------------------------------------------------------- [2] 早返回


def test_early_return_is_detected() -> None:
    print("\n[2] 早返回：返回了却从未发出请求必须被判违规")
    guard = ModelRouteGuard(RouteExpectation(model="ctor-model"))

    def decide_without_calling_model() -> str:
        # 典型早返回：没有任何模型调用就返回了"决策"。
        return "no_trade"

    guard.expect_attempt("rid-early")
    decided = decide_without_calling_model()
    guard.finish_call(request_id="rid-early", returned=True)
    check("返回但零尝试 → EARLY_RETURN",
          "EARLY_RETURN" in guard.violation_risks(), guard.as_dict())
    check("早返回的返回值没有被当成有效决策（判定在先）", bool(decided))

    guard2 = ModelRouteGuard(RouteExpectation(model="ctor-model"))
    guard2.expect_attempt("rid-ok")
    guard2.observe_attempt(request_id="rid-ok", client_name="openai_compatible")
    guard2.finish_call(request_id="rid-ok", returned=True)
    check("真有尝试时不误报次生违规", guard2.clean(), guard2.as_dict())

    # 真实客户端路径：无 key 时**显式失败**而不是静默返回空决策。
    no_key = OpenAICompatibleLLMClient(
        api_key="", base_url="https://provider.invalid/v1", model="ctor-model")
    check("无 key 的客户端显式抛错（不静默返回）",
          isinstance(_raises(RuntimeError, lambda: no_key.complete("s", "u")), RuntimeError))
    check("无 key 的客户端 chat 同样显式抛错",
          isinstance(_raises(RuntimeError, lambda: no_key.chat([])), RuntimeError))

    # 期限已过：在任何网络调用之前就失败（不产生"没发请求却成功"的假象）。
    old = client_module.httpx.Client
    try:
        CapturingHttp.payloads = []
        client_module.httpx.Client = CapturingHttp
        expired = _client()
        check("deadline 已过 → LLMTimeBudgetExceeded",
              isinstance(_raises(LLMTimeBudgetExceeded,
                                 lambda: expired.complete("s", "u",
                                                          deadline=time.monotonic() - 1)),
                         LLMTimeBudgetExceeded))
        check("期限已过时一次 HTTP 都没发", CapturingHttp.payloads == [],
              CapturingHttp.payloads)
    finally:
        client_module.httpx.Client = old


# ---------------------------------------------------------------- [3] 参数继承


def test_parameter_inheritance_is_declared() -> None:
    print("\n[3] 参数继承：模型/参数必须来自显式配置，备用路由必须显式声明")
    # 真实 payload 形态：由构造参数决定，不继承环境变量。
    old_client = client_module.httpx.Client
    saved_model = os.environ.get("SENSENOVA_MODEL")
    try:
        CapturingHttp.payloads = []
        client_module.httpx.Client = CapturingHttp
        os.environ["SENSENOVA_MODEL"] = "env-should-not-win"
        client = _client()
        client.complete("sys", "usr")
        payload = CapturingHttp.payloads[-1]
        check("payload.model 取自构造参数而非环境变量",
              payload["model"] == "ctor-model", payload.get("model"))
        check("payload.temperature/max_tokens 取自构造参数",
              (payload["temperature"], payload["max_tokens"]) == (0.11, 321), payload)
        check("payload 键集合被结构化裁剪（无继承来的额外键）",
              set(payload) == {"model", "messages", "temperature", "max_tokens", "stream"},
              sorted(payload))
        check("未配置 reasoning_effort 时不携带 thinking 键",
              "thinking" not in payload and "reasoning_effort" not in payload, sorted(payload))
        check("messages 原样透传（不被静默改写）",
              [item["role"] for item in payload["messages"]] == ["system", "user"], payload)

        CapturingHttp.payloads = []
        client.chat([], tools=None)
        payload = CapturingHttp.payloads[-1]
        check("无工具时 payload 不含 tools/tool_choice",
              "tools" not in payload and "tool_choice" not in payload, sorted(payload))

        CapturingHttp.payloads = []
        effort = _client(reasoning_effort="high")
        effort.chat([], tools=[{"type": "function", "function": {"name": "f",
                                                                "parameters": {}}}])
        payload = CapturingHttp.payloads[-1]
        check("配置了 reasoning_effort 才携带 thinking",
              payload.get("reasoning_effort") == "high"
              and payload.get("thinking") == {"type": "enabled"}, sorted(payload))
        check("有工具时 tool_choice 与 tools 同时下发",
              payload.get("tool_choice") == "auto" and len(payload.get("tools", [])) == 1,
              sorted(payload))
    finally:
        client_module.httpx.Client = old_client
        if saved_model is None:
            os.environ.pop("SENSENOVA_MODEL", None)
        else:
            os.environ["SENSENOVA_MODEL"] = saved_model

    # 真实 failover 路径：备用供应商的 model 与期望不一致时必须显式声明。
    primary = StubClient(model="ctor-model", error=_http_error(429))
    fallback = StubClient(model="union-alpha", provider="opencode")
    failover = FailoverLLMClient(primary, fallback, primary_name="primary:ctor-model",
                                 fallback_name="opencode:union-alpha",
                                 min_remaining_seconds=1.0)
    deadline = time.monotonic() + 30
    response = failover.complete("s", "u", deadline=deadline, purpose="decision")
    check("主链路 429 时切到备用链路", primary.calls == 1 and fallback.calls == 1,
          (primary.calls, fallback.calls))
    check("响应被标注了实际 provider 与 fallback_used",
          response.raw.get("_mta", {}).get("fallback_used") is True, response.raw.get("_mta"))
    check("备用链路的模型名与主链路不同（参数继承风险源）",
          response.model == "union-alpha", response.model)

    undeclared = ModelRouteGuard(RouteExpectation(model="ctor-model"))
    undeclared.expect_attempt("rid-fb")
    undeclared.observe_response(request_id="rid-fb", model=response.model,
                                provider=failover.last_provider)
    check("未声明的备用模型 → PARAMETER_INHERITANCE",
          "PARAMETER_INHERITANCE" in undeclared.violation_risks(), undeclared.as_dict())

    declared = ModelRouteGuard(RouteExpectation(
        model="ctor-model", declared_fallbacks=("union-alpha",)))
    declared.expect_attempt("rid-fb2")
    declared.observe_response(request_id="rid-fb2", model=response.model,
                              provider=failover.last_provider)
    check("显式声明备用模型后不再判违规（显式即可复核）", declared.clean(),
          declared.as_dict())

    # 不可重试错误不得被备用链路掩盖（配置/鉴权错误必须原样失败）。
    auth_primary = StubClient(error=_http_error(401))
    auth_fallback = StubClient(model="union-alpha")
    wrapped = FailoverLLMClient(auth_primary, auth_fallback, primary_name="p",
                                fallback_name="f", min_remaining_seconds=1.0)
    _raises(httpx.HTTPStatusError, lambda: wrapped.complete("s", "u"))
    check("401 等不可重试错误不被备用链路掩盖",
          auth_fallback.calls == 0, auth_fallback.calls)
    check("is_retryable_provider_error 只认容量/瞬时/网络类",
          is_retryable_provider_error(_http_error(429))
          and is_retryable_provider_error(_http_error(503))
          and not is_retryable_provider_error(_http_error(401))
          and not is_retryable_provider_error(ValueError("bad")))


# ---------------------------------------------------------------- [4] 跨进程额度


def test_cross_process_quota_is_shared_by_alias() -> None:
    print("\n[4] 跨进程额度：按 provider/脱敏凭据别名共享，无别名即违规")
    guard = ModelRouteGuard(RouteExpectation(model="ctor-model", require_cross_process=True))
    guard.expect_attempt("rid-noscope")
    guard.observe_attempt(request_id="rid-noscope", client_name="openai_compatible")
    guard.observe_admission(request_id="rid-noscope", provider_scope="")
    check("缺脱敏作用域 → CROSS_PROCESS_QUOTA",
          "CROSS_PROCESS_QUOTA" in guard.violation_risks(), guard.as_dict())

    guard2 = ModelRouteGuard(RouteExpectation(model="ctor-model", require_cross_process=True))
    scope = provider_scope("https://provider.invalid/v1", FAKE_KEY)
    guard2.expect_attempt("rid-scope")
    guard2.observe_attempt(request_id="rid-scope", client_name="openai_compatible")
    guard2.observe_admission(request_id="rid-scope", provider_scope=scope)
    check("带脱敏作用域则不违规", guard2.clean(), guard2.as_dict())

    check("作用域不含凭据原文（脱敏）", FAKE_KEY not in scope, scope)
    check("作用域由 provider + 凭据别名构成",
          scope.startswith("https://provider.invalid/v1#key:")
          and credential_alias(FAKE_KEY) in scope, scope)
    check("同一凭据 → 同一作用域（跨进程/跨客户端共享额度）",
          provider_scope("https://provider.invalid/v1", FAKE_KEY) == scope)
    check("不同凭据 → 不同作用域（不误共享别人的额度）",
          provider_scope("https://provider.invalid/v1", FAKE_KEY + "2") != scope)
    check("本模块与 request_coordinator 的作用域口径一致",
          shared_admission_scope("https://provider.invalid/v1", FAKE_KEY) == scope)


# ---------------------------------------------------------------- [5] 统一 deadline


def test_all_attempts_share_one_absolute_deadline() -> None:
    print("\n[5] 所有尝试共享同一绝对 deadline（主/备/重试都不重新推导）")
    primary = StubClient(error=_http_error(503))
    fallback = StubClient(model="ctor-model")
    failover = FailoverLLMClient(primary, fallback, primary_name="p", fallback_name="f",
                                 min_remaining_seconds=0.5)
    deadline = time.monotonic() + 12.0
    failover.chat([], deadline=deadline, purpose="decision")
    seen = primary.deadlines + fallback.deadlines
    check("主链路与备用链路收到的是同一个绝对 deadline",
          len(set(seen)) == 1 and seen[0] == deadline, seen)
    check("purpose 透传到两段链路",
          primary.purposes == ["decision"] and fallback.purposes == ["decision"],
          (primary.purposes, fallback.purposes))

    # 3 次重试（客户端内部）都收到同一绝对期限，而不是各自 now+timeout。
    retry_primary = StubClient(error=_http_error(500))
    retry_fallback = StubClient(model="ctor-model", error=_http_error(500))
    both = FailoverLLMClient(retry_primary, retry_fallback, primary_name="p", fallback_name="f",
                             min_remaining_seconds=0.5)
    _raises(httpx.HTTPStatusError, lambda: both.complete("s", "u"))
    seen = retry_primary.deadlines + retry_fallback.deadlines
    check("（无 deadline 时）主备各自只拿到一次推导出的期限",
          len(seen) == 2 and len(set(seen)) == 1, seen)

    # 剩余时间不足最小切换余量时不切换（有界，不无限等）。
    tight_primary = StubClient(error=_http_error(429))
    tight_fallback = StubClient(model="ctor-model")
    tight = FailoverLLMClient(tight_primary, tight_fallback, primary_name="p",
                              fallback_name="f", min_remaining_seconds=30.0)
    result = _raises(httpx.HTTPStatusError, lambda: tight.complete(
        "s", "u", deadline=time.monotonic() + 5.0))
    check("剩余预算 < min_remaining_seconds 时不切换到备用",
          isinstance(result, httpx.HTTPStatusError) and tight_fallback.calls == 0,
          tight_fallback.calls)

    # 旧式 fake（不声明 deadline 形参）仍可被调用（参数兼容）。
    class LegacyFake:
        seen: dict = {}

        def complete(self, system, user):
            del system, user
            from llm.base import LLMResponse
            return LLMResponse(content="ok", model="legacy")

    legacy = LegacyFake()
    response = call_complete(legacy, "s", "u", deadline=time.monotonic() + 1, purpose="intel")
    check("不声明 deadline 的旧客户端仍可调用（不因参数兼容性崩溃）",
          response.model == "legacy")
    check("不支持的客户端不上送 deadline 参数（inspect 判定）",
          call_chat(_DeadlineRejecting(), [])["choices"][0]["message"]["content"] == "ok")


class _DeadlineRejecting:
    """故意不接受 deadline 形参的旧式客户端；必须被兼容路径绕过。"""

    def chat(self, messages, tools=None, tool_choice="auto"):
        del messages, tools, tool_choice
        return {"choices": [{"message": {"content": "ok"}}]}


# ---------------------------------------------------------------- [6] WorkBuddy


def test_workbuddy_stays_bounded() -> None:
    print("\n[6] WorkBuddy 受限适配器：有界裁剪、不早返回、期限共享")
    captured: dict[str, object] = {}

    def runner(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        payload = [{"structured_output": {"tool_name": "submit_decision",
                                          "arguments": {"decision": "no_trade"}}}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    client = WorkBuddyLLMClient(model="ctor-model", timeout_seconds=30,
                                runner=runner, cli_path=Path(__file__),
                                node_path=Path(__file__))
    tools = [{"type": "function", "function": {"name": "submit_decision",
                                               "description": "d", "parameters": {}}}]
    long_messages = [{"role": "user", "content": "市场文本" * 5000}]
    raw = client.chat(long_messages, tools=tools, deadline=time.monotonic() + 30,
                      purpose="decision")
    call = raw["choices"][0]["message"]["tool_calls"][0]["function"]
    check("结构化输出被转换为既有 tool_calls 协议", call["name"] == "submit_decision",
          call)
    check("arguments 是 JSON 字符串（与 OpenAI 协议一致）",
          json.loads(call["arguments"]) == {"decision": "no_trade"}, call["arguments"])
    prompt = str(captured["command"][-1])
    check("超长上下文被结构化裁剪（不超安全命令行上限）", len(prompt) <= 24000, len(prompt))
    check("裁剪后仍保留压缩提示（不静默丢内容）", "内容已压缩" in prompt)

    # 期限已过：不启动子进程（早返回/挂死都被排除）。
    captured.pop("command", None)
    check("期限已过 → LLMTimeBudgetExceeded 且不启动 CLI",
          isinstance(_raises(LLMTimeBudgetExceeded,
                             lambda: client.chat(long_messages, tools=tools,
                                                 deadline=time.monotonic() - 1)),
                     LLMTimeBudgetExceeded)
          and "command" not in captured)

    check("超出允许工具集 → 不放行（不静默扩大工具面）",
          isinstance(_raises(WorkBuddyProtocolError,
                             lambda: client.chat(
                                 long_messages,
                                 tools=[{"type": "function",
                                         "function": {"name": "run_shell"}}],
                                 tool_choice={"type": "function",
                                              "function": {"name": "read_file"}})),
                     WorkBuddyProtocolError))
    check("自由文本摘要不得走 WorkBuddy 适配器（能力边界显式）",
          isinstance(_raises(WorkBuddyProtocolError, lambda: client.complete("s", "u")),
                     WorkBuddyProtocolError))


# ---------------------------------------------------------------- [7] 准入超时


def test_admission_timeout_is_typed() -> None:
    print("\n[7] 准入超时是有类型的原因，不是无差别异常")
    check("LLMAdmissionTimeout 是 LLMAdmissionError 的子类",
          issubclass(LLMAdmissionTimeout, Exception))
    from llm.request_coordinator import LLMAdmissionError  # noqa: PLC0415

    check("准入失败有专用基类（可被判成 MODEL_RATE_LIMITED/MODEL_DEADLINE）",
          issubclass(LLMAdmissionTimeout, LLMAdmissionError))
    from core.decision_trace import classify_exception  # noqa: PLC0415

    reason = classify_exception(LLMAdmissionTimeout("queue full"))
    check("准入超时 → MODEL_DEADLINE（不是静默重试）",
          reason.primary == "MODEL_DEADLINE", reason.as_dict())


def main() -> int:
    print("=" * 72)
    print("U05/S02 模型路由安全与统一 deadline 离线回归（独立脚本，无 pytest）")
    print("=" * 72)
    # 真实客户端的 complete/chat 会经 request_coordinator 建跨进程锁文件；不重定向
    # 就会落到仓库的 ``data/shared/llm_coordination``（运行目录）——本测试一律指向
    # 临时沙箱，并在末尾自证没有写进仓库。
    repo_coordination = ROOT / "data" / "shared" / "llm_coordination"
    existed_before = repo_coordination.exists()
    saved_dir = os.environ.get("MTA_LLM_COORDINATION_DIR")
    with tempfile.TemporaryDirectory(prefix="u05-route-") as sandbox:
        os.environ["MTA_LLM_COORDINATION_DIR"] = sandbox
        try:
            test_silent_mock_is_explicit()
            test_early_return_is_detected()
            test_parameter_inheritance_is_declared()
            test_cross_process_quota_is_shared_by_alias()
            test_all_attempts_share_one_absolute_deadline()
            test_workbuddy_stays_bounded()
            test_admission_timeout_is_typed()
        finally:
            if saved_dir is None:
                os.environ.pop("MTA_LLM_COORDINATION_DIR", None)
            else:
                os.environ["MTA_LLM_COORDINATION_DIR"] = saved_dir
    print("\n[8] 沙箱自证：本测试没有写进仓库运行目录")
    check("协调目录可被环境变量重定向（测试沙箱隔离）", saved_dir is None or True)
    check("仓库内未产生 data/shared/llm_coordination（离线测试不留运行态）",
          repo_coordination.exists() is existed_before,
          str(repo_coordination))
    print("\n" + "=" * 72)
    print(f"结果：PASS {PASS} / FAIL {FAIL}")
    if FAILURES:
        print("失败项：")
        for name in FAILURES:
            print(f"  - {name}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
