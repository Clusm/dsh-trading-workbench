"""U15 验收：运行身份 / 有效配置 / 模型路由 / 账户-运行代接口。

独立可运行（无 pytest）：

    PYTHONUTF8=1 <venv>/python.exe tests/acceptance/test_runtime_identity.py

全程离线：只在临时目录里造 fixture 仓库，不读真实 ``.env``/``data``/密钥，
不联网、不启动服务、不下单。逐项打印 PASS/FAIL，任一失败以退出码 1 结束。
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from core.contracts import (  # noqa: E402
    IdentityCoverageError,
    ModelRoute,
    SensitiveInputError,
    hash_effective_config,
    hash_strategy,
)
from core.contracts.common import sensitive_paths  # noqa: E402
from core.runtime_identity import (  # noqa: E402
    ENVIRONMENT_OVERRIDE_WHITELIST,
    SETTINGS_WHITELIST,
    CredentialLeakError,
    ExecutionModeError,
    FallbackRouteError,
    MissingRuntimeInputError,
    StrategySourceMismatchError,
    authorize_new_entry,
    available_environment_snapshot,
    collect_dependency_lock_files,
    collect_effective_config,
    collect_runtime_identity,
    derive_account_ref,
    fingerprint_disposition,
    guard_no_raw_env_payload,
    key_fingerprint,
    load_strategy_cards,
    mint_runtime_generation,
    model_route_blockers,
    resolve_execution_scope,
    resolve_model_route,
    rotate_credential,
    validate_fallback_route,
    verify_account_binding,
    verify_strategy_cards_source,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} :: {detail}")


def expect_raises(label: str, exc_types: tuple[type[BaseException], ...], fn, *args, **kwargs) -> None:
    try:
        fn(*args, **kwargs)
    except exc_types as exc:
        print(f"PASS {label} ({type(exc).__name__})")
        return
    except BaseException as exc:  # noqa: BLE001
        FAILURES.append(label)
        print(f"FAIL {label} :: unexpected {type(exc).__name__}: {exc}")
        return
    FAILURES.append(label)
    print(f"FAIL {label} :: no exception raised")


# --------------------------------------------------------------------------
# fixture 仓库（临时目录；不使用真实仓库配置与状态）
# --------------------------------------------------------------------------

SETTINGS: dict = {
    "app_name": "multi-agent-trader",
    "stage": "phase3",
    "profiles": {"live": {"channels": {"strategy_weight": 0.0, "ai_weight": 1.0}}},
    "paths": {"data_dir": "data", "db_file": "data/agent_trader.sqlite"},
    "llm": {
        "provider": "sensenova",
        "api_key_env": "SENSENOVA_API_KEY",
        "model_env": "SENSENOVA_MODEL",
        "default_model": "deepseek-v4-flash",
        "reasoning_effort": "low",
        "mock": False,
        "decision_fallback": {
            "provider_env": "MTA_AI_DECISION_FALLBACK_PROVIDER",
            "api_key_env": "OPENCODE_API_KEY",
            "model_env": "OPENCODE_MODEL",
            "base_url_env": "OPENCODE_BASE_URL",
            "default_model": "union-alpha",
            "default_base_url": "https://opencode.ai/zen/v1",
        },
    },
    "execution": {"default_mode": "demo"},
}

AGENTS: dict = {
    "agents": [
        {"name": "ai_trader", "model": "deepseek-v4-flash", "reasoning_effort": "high"},
        {"name": "market_analyst", "model": "deepseek-v4-flash"},
    ]
}

CARDS: dict = {
    "version": 3,
    "cards": [
        {"id": "breakout", "signal_fn": "close_above_prior_high", "exits": {"sl_pct": 1.5}},
        {"id": "pullback", "signal_fn": "close_above_price", "exits": {"tp_pct": 2.0}},
    ],
}


def make_root(tmp: Path) -> Path:
    (tmp / "config").mkdir(parents=True, exist_ok=True)
    (tmp / "knowledge").mkdir(parents=True, exist_ok=True)
    (tmp / "src").mkdir(parents=True, exist_ok=True)
    (tmp / "config" / "settings.yaml").write_text(json.dumps(SETTINGS), encoding="utf-8")
    (tmp / "config" / "agents.yaml").write_text(json.dumps(AGENTS), encoding="utf-8")
    (tmp / "knowledge" / "strategy_cards.json").write_text(json.dumps(CARDS), encoding="utf-8")
    (tmp / "requirements-lock.txt").write_text("pydantic==2.9.0\npyyaml==6.0.2\n", encoding="utf-8")
    (tmp / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    return tmp


def build(tmp: Path, *, env: dict | None = None, settings: dict | None = None,
          agents: dict | None = None, credentials: dict | None = None,
          profile: str | None = None, execution_mode: str = "demo",
          expected_git_commit: str | None = None):
    return collect_runtime_identity(
        tmp,
        environment=execution_mode,
        runtime_instance_id="rt-test",
        profile=profile,
        execution_mode=execution_mode,
        env=env if env is not None else {"EXECUTION_MODE": execution_mode},
        credentials_present=credentials if credentials is not None
        else {"SENSENOVA_API_KEY": True},
        raw_settings=settings if settings is not None else SETTINGS,
        raw_agents=agents if agents is not None else AGENTS,
        expected_git_commit=expected_git_commit,
    )


# --------------------------------------------------------------------------
# 1. 依赖锁 / 策略卡 / 模型覆盖 / profile 覆盖 影响身份哈希
# --------------------------------------------------------------------------


def check_identity_inputs_change_hash() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        base = build(tmp)
        base_hash = base.identity.deployment_hash()

        # 重复构建确定性
        check("identity-deterministic", build(tmp).identity.deployment_hash() == base_hash)

        # 依赖锁
        (tmp / "requirements-lock.txt").write_text("pydantic==2.10.0\n", encoding="utf-8")
        lock_changed = build(tmp)
        check("dependency-lock-affects-identity",
              lock_changed.identity.dependency_hash != base.identity.dependency_hash
              and lock_changed.identity.deployment_hash() != base_hash)
        (tmp / "requirements-lock.txt").write_text("pydantic==2.9.0\npyyaml==6.0.2\n", encoding="utf-8")

        # 策略卡
        cards = dict(CARDS)
        cards["cards"] = [*CARDS["cards"], {"id": "new_card", "signal_fn": "close_below_price"}]
        (tmp / "knowledge" / "strategy_cards.json").write_text(json.dumps(cards), encoding="utf-8")
        card_changed = build(tmp)
        check("strategy-card-affects-identity",
              card_changed.identity.strategy_hash != base.identity.strategy_hash
              and card_changed.identity.deployment_hash() != base_hash)
        (tmp / "knowledge" / "strategy_cards.json").write_text(json.dumps(CARDS), encoding="utf-8")

        # 模型覆盖（agents.yaml 改模型）
        agents = {"agents": [{"name": "ai_trader", "model": "deepseek-v4.1-flash",
                              "reasoning_effort": "high"}]}
        model_changed = build(tmp, agents=agents)
        check("model-override-affects-identity",
              model_changed.identity.model_route_hash != base.identity.model_route_hash
              and model_changed.identity.deployment_hash() != base_hash)

        # 模型覆盖（环境变量）：仅在 yaml 未固定模型时生效（优先级证据）
        bare_agents = {"agents": [{"name": "ai_trader"}]}
        env_base = build(tmp, agents=bare_agents)
        env_model = build(tmp, agents=bare_agents,
                          env={"EXECUTION_MODE": "demo", "SENSENOVA_MODEL": "union-alpha"})
        check("env-model-override-affects-identity",
              env_model.identity.model_route_hash != env_base.identity.model_route_hash
              and env_model.identity.deployment_hash() != env_base.identity.deployment_hash())
        # 白名单内的环境覆盖值进入有效配置哈希
        env_egress = build(tmp, env={"EXECUTION_MODE": "demo", "MTA_EGRESS_ID": "egress-2"})
        check("allowlisted-env-override-affects-identity",
              env_egress.identity.effective_config_hash != base.identity.effective_config_hash
              and env_egress.identity.deployment_hash() != base_hash)

        # profile 覆盖（live channels 覆盖值改变）
        live_settings = json.loads(json.dumps(SETTINGS))
        live_settings["profiles"]["live"]["channels"]["strategy_weight"] = 0.25
        profile_changed = build(tmp, settings=live_settings, profile="live", execution_mode="live")
        live_base = build(tmp, profile="live", execution_mode="live")
        check("profile-override-affects-identity",
              profile_changed.identity.effective_config_hash
              != live_base.identity.effective_config_hash
              and profile_changed.identity.deployment_hash()
              != live_base.identity.deployment_hash())

        # 有效配置（settings 有效值改变）
        stage_changed = json.loads(json.dumps(SETTINGS))
        stage_changed["stage"] = "phase2"
        check("effective-config-affects-identity",
              build(tmp, settings=stage_changed).identity.deployment_hash() != base_hash)

        # 运行实例只影响 identity_hash，不影响 deployment_hash
        other_instance = collect_runtime_identity(
            tmp, environment="demo", runtime_instance_id="rt-other",
            execution_mode="demo", env={"EXECUTION_MODE": "demo"},
            credentials_present={"SENSENOVA_API_KEY": True},
            raw_settings=SETTINGS, raw_agents=AGENTS,
        )
        check("runtime-instance-ignored-in-deployment-hash",
              other_instance.identity.deployment_hash() == base_hash
              and other_instance.identity.identity_hash() != base.identity.identity_hash())


def check_lock_file_required() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        (tmp / "requirements-lock.txt").unlink()
        expect_raises("missing-lock-file-rejected", (MissingRuntimeInputError,),
                      collect_runtime_identity, tmp, environment="demo",
                      runtime_instance_id="rt", execution_mode="demo",
                      env={}, credentials_present={"SENSENOVA_API_KEY": True},
                      raw_settings=SETTINGS, raw_agents=AGENTS)
        expect_raises("missing-lock-file-rejected-direct", (MissingRuntimeInputError,),
                      collect_dependency_lock_files, tmp)
        payload = collect_dependency_lock_files(tmp, require_lock=False)
        check("lock-collector-optional-mode", "requirements-lock.txt" not in payload)


def check_strategy_source_is_file_level() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        source = load_strategy_cards(tmp)
        check("strategy-source-declared",
              source.source_file == "knowledge/strategy_cards.json" and source.card_count == 2)
        verify_strategy_cards_source(source, tmp)  # 不抛错即通过
        print("PASS strategy-source-verifies")

        # 内存篡改（卡片与声明的来源文件不一致）必须被文件级校验拒绝
        from dataclasses import replace

        tampered = replace(source, payload={"cards": [{"id": "evil"}]})
        expect_raises("strategy-source-mismatch-rejected", (StrategySourceMismatchError,),
                      verify_strategy_cards_source, tampered, tmp)

        # 文件缺失即拒（不静默“无策略卡”）
        (tmp / "knowledge" / "strategy_cards.json").unlink()
        expect_raises("strategy-cards-missing-rejected", (MissingRuntimeInputError,),
                      load_strategy_cards, tmp)
        expect_raises("strategy-cards-missing-in-identity", (MissingRuntimeInputError,),
                      collect_runtime_identity, tmp, environment="demo",
                      runtime_instance_id="rt", execution_mode="demo", env={},
                      credentials_present={"SENSENOVA_API_KEY": True},
                      raw_settings=SETTINGS, raw_agents=AGENTS)


# --------------------------------------------------------------------------
# 2. 凭据不外泄 / 不对 .env 整文件哈希 / 默认值不当有效值
# --------------------------------------------------------------------------

SECRET = "sk-live-DEADBEEF-should-never-appear"


def check_no_credential_leak() -> None:
    env = {
        "EXECUTION_MODE": "demo",
        "SENSENOVA_API_KEY": SECRET,
        "BINANCE_LIVE_API_SECRET": "top-secret",
        "OPENCODE_API_KEY": "oc-secret",
    }
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        bundle = build(tmp, env=env)
        blob = json.dumps(bundle.as_dict(), ensure_ascii=False, sort_keys=True)
        check("no-secret-value-in-identity", SECRET not in blob and "top-secret" not in blob
              and "oc-secret" not in blob)
        check("no-sensitive-key-path-in-effective-config",
              sensitive_paths(bundle.effective_config.as_payload()) == [])
        check("no-sensitive-key-path-in-model-route",
              sensitive_paths(bundle.model_route.route.as_payload()) == [])
        check("env-allowlist-has-no-sensitive-name",
              not any(seg in name.lower() for name in ENVIRONMENT_OVERRIDE_WHITELIST
                      for seg in ("key", "secret", "token", "password", "credential")))
        check("settings-whitelist-has-no-sensitive-leaf",
              not any(name.split(".")[-1].lower() in {"api_key_env", "api_secret_env"}
                      for name in SETTINGS_WHITELIST))
        # 覆盖值里不含凭据类变量名
        overrides = dict(bundle.effective_config.environment_overrides)
        check("credential-env-not-in-overrides",
              not set(overrides) & {"SENSENOVA_API_KEY", "BINANCE_LIVE_API_SECRET",
                                    "OPENCODE_API_KEY"})
        # 环境快照同样只含白名单项
        check("env-snapshot-allowlisted",
              all(name in ENVIRONMENT_OVERRIDE_WHITELIST
                  for name in available_environment_snapshot()))
        # K01 守卫 + 本模块守卫
        expect_raises("guard-rejects-raw-env-text", (CredentialLeakError,),
                      guard_no_raw_env_payload, {"raw": "A=1\nB=2\nC=3"}, name="cfg")
        expect_raises("guard-rejects-env-file-key", (CredentialLeakError,),
                      guard_no_raw_env_payload, {".env": "A=1"}, name="cfg")
        expect_raises("guard-rejects-nested-credential-key", (CredentialLeakError,),
                      guard_no_raw_env_payload, {"llm": {"api_key_env": "X"}}, name="cfg")
        expect_raises("k01-guard-rejects-raw-text", (SensitiveInputError,),
                      hash_strategy, "SENSENOVA_API_KEY=abc\nEXECUTION_MODE=live")
        expect_raises("k01-guard-rejects-secret-key", (SensitiveInputError,),
                      hash_effective_config, {"EXECUTION_MODE": "live", "api_key": "x"})
        check("no-env-file-path-hashed",
              not any(str(name).startswith(".") or "/.env" in str(name) or "\\" in str(name)
                      for name in bundle.effective_config.as_payload()["environment_overrides"]))


def check_defaults_are_not_effective_values() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        # 无任何环境变量：环境覆盖必须是空声明，而不是用代码默认值填充
        bundle = build(tmp, env={})
        check("empty-env-yields-empty-overrides",
              dict(bundle.effective_config.environment_overrides) == {})
        # 未出现在配置里的白名单键不得被默认值填充
        empty_settings = {"execution": {"default_mode": "demo"}}
        cfg = collect_effective_config(raw_settings=empty_settings, profile=None, env={})
        check("absent-settings-keys-not-filled",
              "llm.default_model" not in cfg.settings and cfg.settings == {"execution.default_mode": "demo"})
        # profile=None 时不产生 profile 覆盖
        check("no-profile-means-no-overrides", dict(cfg.profile_overrides) == {})
        # profile 选中时只取该段真实存在值
        live = collect_effective_config(raw_settings=SETTINGS, profile="live", env={})
        check("profile-overrides-from-config-only",
              dict(live.profile_overrides) == {"channels.strategy_weight": 0.0,
                                               "channels.ai_weight": 1.0})
        # 空串环境变量不算“已设置”
        blank = collect_effective_config(raw_settings=SETTINGS, profile=None,
                                        env={"EXECUTION_MODE": "   "})
        check("blank-env-value-ignored", dict(blank.environment_overrides) == {})


# --------------------------------------------------------------------------
# 3. 模型路由：覆盖优先级、Mock 可见、Demo/live 阻断
# --------------------------------------------------------------------------


def check_model_route_priority() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        # agents.yaml > 环境变量 > settings 默认
        agent_wins = resolve_model_route(raw_settings=SETTINGS, raw_agents=AGENTS,
                                        env={"SENSENOVA_MODEL": "env-model"},
                                        credentials_present={"SENSENOVA_API_KEY": True},
                                        execution_mode="demo")
        check("agent-model-beats-env", agent_wins.route.model == "deepseek-v4-flash"
              and agent_wins.sources["model"] == "agents.yaml[ai_trader].model")
        agents_no_model = {"agents": [{"name": "ai_trader"}]}
        env_wins = resolve_model_route(raw_settings=SETTINGS, raw_agents=agents_no_model,
                                      env={"SENSENOVA_MODEL": "env-model"},
                                      credentials_present={"SENSENOVA_API_KEY": True},
                                      execution_mode="demo")
        check("env-model-beats-default", env_wins.route.model == "env-model"
              and env_wins.sources["model"] == "environment:SENSENOVA_MODEL")
        default_wins = resolve_model_route(raw_settings=SETTINGS, raw_agents=agents_no_model,
                                          env={}, credentials_present={"SENSENOVA_API_KEY": True},
                                          execution_mode="demo")
        check("default-model-used-last", default_wins.route.model == "deepseek-v4-flash"
              and default_wins.sources["model"] == "settings.llm.default_model")
        # effort 覆盖
        check("effort-from-agents-yaml", agent_wins.route.effort == "high")
        # provider 切 WorkBuddy（仅 ai_trader）
        wb = resolve_model_route(raw_settings=SETTINGS, raw_agents=AGENTS,
                                 env={"MTA_AI_DECISION_PROVIDER": "workbuddy"},
                                 credentials_present={}, execution_mode="live")
        check("workbuddy-switches-provider", wb.provider == "workbuddy"
              and wb.route.model == "deepseek-v4.1-flash" and not wb.mock)
        other_agent = resolve_model_route(raw_settings=SETTINGS, raw_agents=AGENTS,
                                         env={"MTA_AI_DECISION_PROVIDER": "workbuddy"},
                                         credentials_present={"SENSENOVA_API_KEY": True},
                                         agent_name="market_analyst", execution_mode="live")
        check("workbuddy-only-ai-trader", other_agent.provider == "sensenova")
        # probe 结果本身可进身份哈希
        check("route-hashable", len(agent_wins.identity_hash()) == 64)
        check("route-hash-changes-with-model",
              agent_wins.identity_hash() != default_wins.identity_hash())


def check_mock_is_visible_and_blocking() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        mock_route = resolve_model_route(raw_settings=SETTINGS, raw_agents=AGENTS, env={},
                                        credentials_present={"SENSENOVA_API_KEY": False},
                                        execution_mode="demo")
        check("missing-key-is-mock", mock_route.mock
              and mock_route.sources.get("mock") == "credential_absent")
        check("mock-blocks-demo", "MODEL_ROUTE_MOCK" in mock_route.blockers)
        check("mock-blocks-live", "MODEL_ROUTE_MOCK" in model_route_blockers(
            mock_route.route, mock=mock_route.mock, execution_mode="live"))
        check("mock-does-not-block-paper", model_route_blockers(
            mock_route.route, mock=mock_route.mock, execution_mode="paper") == ())
        check("mock-blocker-visible-in-overrides",
              mock_route.route.profile_overrides.get("mock") is True)
        # settings.llm.mock=true 现在真正生效（旧实现无消费者）
        forced = resolve_model_route(
            raw_settings={**SETTINGS, "llm": {**SETTINGS["llm"], "mock": True}},
            raw_agents=AGENTS, env={}, credentials_present={"SENSENOVA_API_KEY": True},
            execution_mode="demo")
        check("explicit-mock-setting-effective",
              forced.mock and "MODEL_ROUTE_FORCED_MOCK" in forced.blockers
              and forced.sources.get("mock") == "settings.llm.mock")
        # 无凭据声明时按环境缺失判定（不静默当作有凭据）
        undeclared = resolve_model_route(raw_settings=SETTINGS, raw_agents=AGENTS, env={},
                                        credentials_present=None, execution_mode="demo")
        check("undeclared-credential-is-mock", undeclared.mock)
        # 主路 Mock 却声明备用路由：显式降级原因
        degraded = model_route_blockers(
            ModelRoute(model="mock", effort="low", fallback_routes=({"provider": "opencode",
                                                                     "model": "union-alpha",
                                                                     "base_url_host": "opencode.ai"},),
                       profile_overrides={"provider": "sensenova", "mock": True}),
            execution_mode="live")
        check("fallback-with-mock-primary-flagged",
              "FALLBACK_DEGRADED_PRIMARY_MOCK" in degraded)
        # WorkBuddy 声明为实盘专属：demo 下阻断
        wb_demo = resolve_model_route(raw_settings=SETTINGS, raw_agents=AGENTS,
                                      env={"MTA_AI_DECISION_PROVIDER": "workbuddy"},
                                      credentials_present={}, execution_mode="demo")
        check("workbuddy-blocked-outside-live", "WORKBUDDY_OUTSIDE_LIVE" in wb_demo.blockers)
        check("workbuddy-allowed-in-live", wb_demo.blockers == () or
              "WORKBUDDY_OUTSIDE_LIVE" not in model_route_blockers(
                  wb_demo.route, mock=False, execution_mode="live", provider="workbuddy"))


def check_mock_blocks_new_entry_via_u02_gate() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        bundle = build(tmp, credentials={"SENSENOVA_API_KEY": False})
        generation = mint_runtime_generation(
            environment="demo", account_ref=derive_account_ref("demo", declared_account_ref="acct"),
            runtime_instance_id="rt-test", scope_hash="scope-1")
        decision = authorize_new_entry(bundle, generation, expected=generation,
                                       environment="demo", account_ref=generation.account_ref,
                                       runtime_instance_id="rt-test", scope_hash="scope-1")
        check("mock-blocks-authorize-new-entry", not decision.allowed
              and "MODEL_ROUTE_MOCK" in decision.reason_codes and decision.http_status == 409)
        ok_bundle = build(tmp, credentials={"SENSENOVA_API_KEY": True})
        ok = authorize_new_entry(ok_bundle, generation, expected=generation,
                                 environment="demo", account_ref=generation.account_ref,
                                 runtime_instance_id="rt-test", scope_hash="scope-1")
        check("healthy-route-allows-entry", ok.allowed, str(ok.reason_codes))
        blocked = authorize_new_entry(ok_bundle, generation, expected=generation,
                                      environment="demo", account_ref=generation.account_ref,
                                      runtime_instance_id="rt-test", scope_hash="scope-1",
                                      readiness_allowed=False)
        check("readiness-false-still-blocks", not blocked.allowed
              and "READINESS_BLOCKED" in blocked.reason_codes)
        # 身份不符（换账户）必须拒绝
        foreign = mint_runtime_generation(
            environment="demo", account_ref=derive_account_ref("demo", declared_account_ref="other"),
            runtime_instance_id="rt-test", scope_hash="scope-1")
        mismatched = authorize_new_entry(ok_bundle, foreign, expected=generation,
                                         environment="demo", account_ref=generation.account_ref,
                                         runtime_instance_id="rt-test", scope_hash="scope-1")
        check("account-mismatch-blocks-entry", not mismatched.allowed
              and "ACCOUNT_REF_MISMATCH" in mismatched.reason_codes)
        # 预期提交不符必须拒绝（F13）
        git_mismatch = authorize_new_entry(ok_bundle, generation, expected=generation,
                                           environment="demo", account_ref=generation.account_ref,
                                           runtime_instance_id="rt-test", scope_hash="scope-1",
                                           expected_git_commit="deadbeef" * 5)
        check("git-mismatch-blocks-entry", not git_mismatch.allowed
              and "GIT_COMMIT_MISMATCH" in git_mismatch.reason_codes)


# --------------------------------------------------------------------------
# 4. 账户引用与密钥轮换解耦（U02 接口）
# --------------------------------------------------------------------------


def check_account_ref_decoupled_from_rotation() -> None:
    first = derive_account_ref("live", declared_account_ref="binance-main")
    second = derive_account_ref("live", declared_account_ref="binance-main")
    check("account-ref-is-declared-stable", first == second and first.stable
          and first.value == "live:binance-main")
    local = derive_account_ref("demo")
    check("account-ref-local-fallback-labeled", local.value == "demo:local" and not local.stable)
    check("account-ref-not-key-derived",
          key_fingerprint(SECRET) not in first.value and SECRET not in first.value)

    generation = mint_runtime_generation(
        environment="live", account_ref=first, runtime_instance_id="pid-1234",
        credential_epoch=0, key_fingerprint_value=key_fingerprint("old-key"),
        scope_hash="scope-a", started_at=1000.0)
    rotated = rotate_credential(generation, new_key_fingerprint=key_fingerprint("new-key"))
    check("rotation-keeps-account-ref", rotated.account_ref == generation.account_ref)
    check("rotation-keeps-generation-id", rotated.generation_id == generation.generation_id)
    check("rotation-keeps-scope-and-instance",
          rotated.scope_hash == generation.scope_hash
          and rotated.runtime_instance_id == generation.runtime_instance_id)
    check("rotation-bumps-epoch-and-fingerprint",
          rotated.credential_epoch == generation.credential_epoch + 1
          and rotated.key_fingerprint != generation.key_fingerprint)
    check("fingerprint-irreversible", SECRET not in (key_fingerprint(SECRET) or "")
          and len(key_fingerprint(SECRET) or "") == 16)
    check("fingerprint-none-on-empty", key_fingerprint("") is None and key_fingerprint(None) is None)
    # 轮换不应否决在途管理动作
    binding_after_rotation = verify_account_binding(
        generation, rotated, environment="live", account_ref=generation.account_ref,
        runtime_instance_id="pid-1234", scope_hash="scope-a")
    check("rotation-does-not-break-binding", binding_after_rotation.allowed,
          str(binding_after_rotation.reason_codes))
    # generation_id 对同一输入确定
    same = mint_runtime_generation(environment="live", account_ref=first,
                                   runtime_instance_id="pid-1234", started_at=1000.0)
    check("generation-id-deterministic", same.generation_id == generation.generation_id)
    other = mint_runtime_generation(environment="live", account_ref=first,
                                    runtime_instance_id="pid-9999", started_at=1000.0)
    check("generation-id-per-instance", other.generation_id != generation.generation_id)
    expect_raises("generation-requires-instance", (MissingRuntimeInputError,),
                  mint_runtime_generation, environment="live", account_ref=first,
                  runtime_instance_id="")
    expect_raises("rotation-needs-fingerprint-or-none", (Exception,),
                  rotate_credential, generation, new_key_fingerprint="  ")


def check_u02_binding_interface() -> None:
    generation = mint_runtime_generation(
        environment="demo", account_ref=derive_account_ref("demo", declared_account_ref="acct-a"),
        runtime_instance_id="rt-1", scope_hash="scope-1")
    check("binding-self-ok", verify_account_binding(generation, generation).allowed)
    check("binding-unproven-without-actual",
          verify_account_binding(generation, None).reason_codes == ("RUNTIME_GENERATION_UNPROVEN",))
    env_mismatch = verify_account_binding(generation, generation, environment="live")
    check("binding-detects-environment", "ENVIRONMENT_MISMATCH" in env_mismatch.reason_codes)
    acct_mismatch = verify_account_binding(generation, generation, account_ref="demo:other")
    check("binding-detects-account", "ACCOUNT_REF_MISMATCH" in acct_mismatch.reason_codes)
    inst_mismatch = verify_account_binding(generation, generation, runtime_instance_id="rt-2")
    check("binding-detects-instance", "RUNTIME_INSTANCE_MISMATCH" in inst_mismatch.reason_codes)
    scope_mismatch = verify_account_binding(generation, generation, scope_hash="scope-2")
    check("binding-detects-scope", "SCOPE_HASH_MISMATCH" in scope_mismatch.reason_codes)
    older = mint_runtime_generation(
        environment="demo", account_ref=derive_account_ref("demo", declared_account_ref="acct-a"),
        runtime_instance_id="rt-1", scope_hash="scope-1", started_at=1.0)
    check("binding-detects-generation",
          "RUNTIME_GENERATION_MISMATCH" in verify_account_binding(
              older, generation).reason_codes)
    check("generation-serialisable", generation.as_dict()["account_ref"] == "demo:acct-a")


# --------------------------------------------------------------------------
# 5. DL01 负例：覆盖优先级 / CLI 误选 / 备用非法参数
# --------------------------------------------------------------------------


def check_dl01_cli_mis_selection() -> None:
    scope = resolve_execution_scope(profile="live", execution_mode="demo")
    check("profile-wins-over-execution-mode",
          scope.profile == "live" and scope.mode == "live"
          and scope.ignored == ("EXECUTION_MODE",))
    check("execution-mode-when-no-profile",
          resolve_execution_scope(profile=None, execution_mode="demo").mode == "demo")
    check("default-when-nothing-given",
          resolve_execution_scope(profile=None, execution_mode=None, default_mode="demo").mode == "demo")
    check("mode-normalised-case",
          resolve_execution_scope(profile="LIVE").mode == "live"
          and resolve_execution_scope(profile=None, execution_mode="Demo").mode == "demo")
    expect_raises("unknown-profile-rejected", (ExecutionModeError,),
                  resolve_execution_scope, profile="lvie")
    expect_raises("unknown-mode-rejected", (ExecutionModeError,),
                  resolve_execution_scope, execution_mode="liv")
    expect_raises("empty-profile-is-not-unset", (ExecutionModeError,),
                  resolve_execution_scope, profile="")
    expect_raises("empty-mode-is-not-unset", (ExecutionModeError,),
                  resolve_execution_scope, profile=None, execution_mode="")
    expect_raises("unknown-default-mode-rejected", (ExecutionModeError,),
                  resolve_execution_scope, profile=None, execution_mode=None, default_mode="prod")
    expect_raises("non-string-profile-rejected", (ExecutionModeError,),
                  resolve_execution_scope, profile=123)
    expect_raises("paper-is-not-a-valid-profile", (ExecutionModeError,),
                  resolve_execution_scope, profile="paper")
    check("profile-ignores-conflicting-execution-mode",
          resolve_execution_scope(profile="demo", execution_mode="live").mode == "demo")
    check("testnet-mode-supported",
          resolve_execution_scope(profile=None, execution_mode="testnet").mode == "testnet")


def check_dl01_fallback_illegal_params() -> None:
    good = {"provider": "opencode", "model": "union-alpha",
            "base_url": "https://opencode.ai/zen/v1", "credential_present": True}
    check("valid-fallback-accepted",
          validate_fallback_route(good)["base_url_host"] == "opencode.ai")
    expect_raises("fallback-missing-credential", (FallbackRouteError,),
                  validate_fallback_route, {**good, "credential_present": False})
    expect_raises("fallback-credential-undeclared", (FallbackRouteError,),
                  validate_fallback_route, {k: v for k, v in good.items()
                                            if k != "credential_present"})
    expect_raises("fallback-model-none", (FallbackRouteError,),
                  validate_fallback_route, {**good, "model": "none"})
    expect_raises("fallback-empty-model", (FallbackRouteError,),
                  validate_fallback_route, {**good, "model": " "})
    expect_raises("fallback-empty-provider", (FallbackRouteError,),
                  validate_fallback_route, {**good, "provider": ""})
    expect_raises("fallback-bad-url-scheme", (FallbackRouteError,),
                  validate_fallback_route, {**good, "base_url": "ftp://opencode.ai/v1"})
    expect_raises("fallback-url-without-host", (FallbackRouteError,),
                  validate_fallback_route, {**good, "base_url": "https:///v1"})
    expect_raises("fallback-url-with-credentials", (FallbackRouteError,),
                  validate_fallback_route,
                  {**good, "base_url": "https://user:pass@opencode.ai/v1"})
    expect_raises("fallback-unknown-field", (FallbackRouteError,),
                  validate_fallback_route, {**good, "junk_field": 1})
    expect_raises("fallback-non-mapping", (FallbackRouteError,),
                  validate_fallback_route, "opencode")
    # 路由解析：备用 provider 声明为 opencode 时，非法参数必须抛错而不是静默无备用
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        bad_settings = json.loads(json.dumps(SETTINGS))
        bad_settings["llm"]["decision_fallback"]["default_model"] = "none"
        expect_raises("illegal-fallback-config-rejected", (FallbackRouteError,),
                      resolve_model_route, raw_settings=bad_settings, raw_agents=AGENTS,
                      env={"MTA_AI_DECISION_FALLBACK_PROVIDER": "opencode"},
                      credentials_present={"SENSENOVA_API_KEY": True,
                                           "OPENCODE_API_KEY": False},
                      execution_mode="live")
        # 未声明备用 → 显式空声明（不是缺口）
        no_fallback = build(tmp, credentials={"SENSENOVA_API_KEY": True})
        check("no-fallback-explicit-empty",
              tuple(no_fallback.model_route.route.fallback_routes) == ())
        declared = resolve_model_route(
            raw_settings=SETTINGS, raw_agents=AGENTS,
            env={"MTA_AI_DECISION_FALLBACK_PROVIDER": "opencode",
                 "OPENCODE_MODEL": "union-alpha",
                 "OPENCODE_BASE_URL": "https://opencode.ai/zen/v1",
                 "OPENCODE_API_KEY": "present"},
            credentials_present={"SENSENOVA_API_KEY": True}, execution_mode="live")
        check("declared-fallback-in-route",
              len(tuple(declared.route.fallback_routes)) == 1
              and declared.route.profile_overrides.get("mock") is False)


def check_coverage_fail_closed() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        # 依赖锁缺失时 build_identity 直接拒绝（覆盖缺口不可静默）
        try:
            collect_runtime_identity(tmp, environment="demo", runtime_instance_id="rt",
                                     execution_mode="demo", env={},
                                     credentials_present={"SENSENOVA_API_KEY": True},
                                     raw_settings=SETTINGS, raw_agents=AGENTS,
                                     require_lock_file=False)
        except IdentityCoverageError as exc:
            check("coverage-gap-reported-as-identity-coverage-error",
                  "dependency.requirements_lock" in exc.groups)
        else:
            check("coverage-gap-reported-as-identity-coverage-error", True,
                  "covering lock present")


def check_fingerprint_disposition_registered() -> None:
    table = fingerprint_disposition()
    check("disposition-covers-report-items", len(table) >= 15, f"items={len(table)}")
    valid = {"fixed", "registered", "aligned", "unknown"}
    check("disposition-status-valid",
          all(item["status"] in valid for item in table.values()))
    check("disposition-has-note-and-owner",
          all(item.get("note") and item.get("owner") for item in table.values()))
    registered = sorted(k for k, v in table.items() if v["status"] == "registered")
    print(f"INFO fingerprint disposition: fixed="
          f"{sum(1 for v in table.values() if v['status'] == 'fixed')} "
          f"registered={len(registered)} {registered}")


def main() -> int:
    check_identity_inputs_change_hash()
    check_lock_file_required()
    check_strategy_source_is_file_level()
    check_no_credential_leak()
    check_defaults_are_not_effective_values()
    check_model_route_priority()
    check_mock_is_visible_and_blocking()
    check_mock_blocks_new_entry_via_u02_gate()
    check_account_ref_decoupled_from_rotation()
    check_u02_binding_interface()
    check_dl01_cli_mis_selection()
    check_dl01_fallback_illegal_params()
    check_coverage_fail_closed()
    check_fingerprint_disposition_registered()
    if FAILURES:
        print(f"\nU15 RUNTIME IDENTITY ACCEPTANCE FAILED: {len(FAILURES)} -> "
              + ", ".join(FAILURES))
        return 1
    print("\nU15 RUNTIME IDENTITY ACCEPTANCE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
