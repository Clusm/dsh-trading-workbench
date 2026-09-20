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
    ACCOUNT_REF_LOCAL_FALLBACK,
    ACCOUNT_REF_UNPROVEN,
    CONFIG_SOURCE_FILES,
    CONFIG_SOURCES_KEY,
    CONFIG_SOURCE_MISSING,
    ENVIRONMENT_OVERRIDE_WHITELIST,
    ENVIRONMENT_STATE_ROOT_MISMATCH,
    EXECUTION_PROFILE_NOT_DECLARED,
    EXECUTION_SCOPE_UNLOCKED,
    FALLBACK_PROVIDER_UNRECOGNISED,
    IDENTITY_BINDING_UNPROVEN,
    IDENTITY_GENERATION_MISMATCH,
    IDENTITY_MALFORMED,
    DEPENDENCY_COMPARISON_MATCH,
    DEPENDENCY_COMPARISON_MISMATCH,
    DEPENDENCY_COMPARISON_UNAVAILABLE,
    DEPENDENCY_LOCK_FILES,
    RuntimeIdentityError,
    compare_dependency_lock,
    PROVIDER_CAPABILITY_UNPROVEN,
    PROVIDER_UNAVAILABLE,
    READINESS_UNPROVEN,
    RUNTIME_GENERATION_MALFORMED,
    SCOPE_HASH_MISMATCH,
    SCOPE_HASH_UNBOUND,
    SETTINGS_WHITELIST,
    STATE_ROOT_PROFILE_UNLOCKED,
    LIVE_SHARED_STATE_ROOT,
    CredentialLeakError,
    ExecutionModeError,
    FallbackRouteError,
    MalformedInputError,
    ModelRouteConfigError,
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
    collect_artifact_files,
    validate_fallback_route,
    verify_account_binding,
    verify_environment_state_root,
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
          expected_git_commit: str | None = None,
          declared_account_ref: str | None = None,
          fallback_local: bool | None = None):
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
        declared_account_ref=declared_account_ref,
        fallback_local=fallback_local,
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

        # profile 覆盖（live channels 覆盖值改变）；
        # live 下必须声明账户引用（r2 起 live 不再允许 <env>:local 静默回退）
        live_settings = json.loads(json.dumps(SETTINGS))
        live_settings["profiles"]["live"]["channels"]["strategy_weight"] = 0.25
        profile_changed = build(tmp, settings=live_settings, profile="live",
                                execution_mode="live", declared_account_ref="acct-live")
        live_base = build(tmp, profile="live", execution_mode="live",
                          declared_account_ref="acct-live")
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


# --------------------------------------------------------------------------
# 1b. K01 EFFECTIVE_CONFIG_SOURCES 六类来源必须真正进入有效配置哈希（r2 P0-2）
# --------------------------------------------------------------------------

SOURCE_FILES: dict[str, str] = {
    "config/settings.yaml": "",  # 由 SETTINGS 写入，这里只占位
    "config/decision_policy.yaml": "version: 1\nposition_guardian:\n  mode: shadow\n",
    "config/agents.yaml": "",
    "config/risk_learning.yaml": (
        "profile: learning_adventurous\nmax_leverage: 20\ndaily_loss_stop: 0.15\n"
        "always_enforced:\n  reconcile_before_reopen: true\n"
    ),
    "config/risk_profile.lock.json": json.dumps(
        {"version": 1, "expected": {"risk.max_leverage": 20, "risk.daily_loss_stop": 0.15}},
        ensure_ascii=False),
    "config/windows_service.json": json.dumps(
        {"service_name": "MultiAgentTrader", "port": 8000, "entrypoint": "scripts/run_web.py"},
        ensure_ascii=False),
}


def make_source_root(tmp: Path) -> Path:
    """fixture 仓库补齐 K01 六类来源文件。"""
    make_root(tmp)
    for relative, text in SOURCE_FILES.items():
        if not text:
            continue
        (tmp / relative).write_text(text, encoding="utf-8")
    return tmp


def _drift_source(tmp: Path, relative: str, old: str, new: str) -> None:
    path = tmp / relative
    text = path.read_text(encoding="utf-8")
    assert old in text, f"{relative} does not contain {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def _restore_source(tmp: Path, relative: str, text: str) -> None:
    (tmp / relative).write_text(text, encoding="utf-8")


def check_config_sources_enter_identity() -> None:
    """六类来源逐类漂移都必须改变身份；缺失/敏感值不得静默。"""
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_source_root(Path(raw))
        base = build(tmp, env={})
        base_deploy = base.identity.deployment_hash()
        base_eff = base.identity.effective_config_hash

        # ① settings.yaml 白名单之外的风险项（E/C 的实测漂移样本）
        drifted = json.loads(json.dumps(SETTINGS))
        drifted["channels"] = {**drifted.get("channels", {}), "ai_daily_loss_stop": 0.99,
                               "require_initial_sl_tp": False}
        drifted["capital"] = {"initial_equity": 100000}
        drifted["execution"] = {**drifted["execution"],
                                "base_url_map": {"live": "https://evil.example.com"}}
        check("settings-risk-drift-changes-identity",
              build(tmp, settings=drifted, env={}).identity.deployment_hash() != base_deploy)

        # ② decision_policy.yaml
        _drift_source(tmp, "config/decision_policy.yaml", "mode: shadow", "mode: enforce")
        check("decision-policy-drift-changes-identity",
              build(tmp, env={}).identity.deployment_hash() != base_deploy)
        _restore_source(tmp, "config/decision_policy.yaml", SOURCE_FILES["config/decision_policy.yaml"])

        # ③ agencies.yaml（模型覆盖）
        agents = {"agents": [{"name": "ai_trader", "model": "deepseek-v4.1-flash",
                              "reasoning_effort": "high"}]}
        check("agents-file-drift-changes-identity",
              build(tmp, agents=agents, env={}).identity.deployment_hash() != base_deploy)

        # ④ risk_learning.yaml
        _drift_source(tmp, "config/risk_learning.yaml", "max_leverage: 20", "max_leverage: 100")
        check("risk-learning-drift-changes-identity",
              build(tmp, env={}).identity.deployment_hash() != base_deploy)
        _restore_source(tmp, "config/risk_learning.yaml", SOURCE_FILES["config/risk_learning.yaml"])

        # ⑤ risk_profile.lock.json
        _drift_source(tmp, "config/risk_profile.lock.json", '"risk.max_leverage": 20',
                      '"risk.max_leverage": 100')
        check("risk-profile-lock-drift-changes-identity",
              build(tmp, env={}).identity.deployment_hash() != base_deploy)
        _restore_source(tmp, "config/risk_profile.lock.json",
                        SOURCE_FILES["config/risk_profile.lock.json"])

        # ⑥ windows_service.json
        _drift_source(tmp, "config/windows_service.json", '"port": 8000', '"port": 8001')
        check("windows-service-drift-changes-identity",
              build(tmp, env={}).identity.deployment_hash() != base_deploy)
        _restore_source(tmp, "config/windows_service.json", SOURCE_FILES["config/windows_service.json"])

        # 逐类漂移后身份必须回到基线（确定性与可复现）
        check("identity-restored-after-source-restore",
              build(tmp, env={}).identity.deployment_hash() == base_deploy)

        # 来源文件缺失：哈希必须改变 + advisory + 可要求结构化拒绝
        (tmp / "config/risk_profile.lock.json").unlink()
        missing = build(tmp, env={})
        check("missing-source-changes-identity",
              missing.identity.deployment_hash() != base_deploy
              and "config/risk_profile.lock.json" in missing.effective_config.missing_sources)
        check("missing-source-is-visible-as-advisory",
              any(code.startswith(CONFIG_SOURCE_MISSING)
                  for code in missing.advisories), str(missing.advisories))
        expect_raises("missing-source-rejected-when-required", (MissingRuntimeInputError,),
                      collect_runtime_identity, tmp, environment="demo",
                      runtime_instance_id="rt-test", execution_mode="demo", env={},
                      credentials_present={"SENSENOVA_API_KEY": True},
                      raw_settings=SETTINGS, raw_agents=AGENTS,
                      require_source_configs=True)
        (tmp / "config/risk_profile.lock.json").write_text(
            SOURCE_FILES["config/risk_profile.lock.json"], encoding="utf-8")

        # 来源文件里的凭据键：只丢键、不入哈希、值不外泄，但被计数
        leaky = json.dumps({"version": 1, "max_leverage": 20,
                            "api_key": "SK-SOURCE-LEAK-0001",
                            "nested": {"secret_token": "tok-123"}}, ensure_ascii=False)
        _restore_source(tmp, "config/risk_learning.yaml", leaky)
        leaky_bundle = build(tmp, env={})
        blob = json.dumps(leaky_bundle.as_dict(), ensure_ascii=False, sort_keys=True)
        check("source-file-credentials-not-hashed",
              "SK-SOURCE-LEAK-0001" not in blob and "tok-123" not in blob)
        check("source-file-credentials-counted",
              any(path.startswith("risk_learning.api_key")
                  for path in leaky_bundle.effective_config.dropped_sensitive)
              and any(path.startswith("risk_learning.nested.secret_token")
                      for path in leaky_bundle.effective_config.dropped_sensitive),
              str(leaky_bundle.effective_config.dropped_sensitive))
        check("source-file-sensitive-drop-changes-hash",
              leaky_bundle.identity.deployment_hash() != base_deploy)
        check("source-config-hash-reflects-effective-config",
              leaky_bundle.identity.effective_config_hash != base_eff)


def check_input_shape_and_path_bounds() -> None:
    """r2 P1-2/P1-3/P1-4：工件路径 root 边界、畸形载荷、零卡策略卡。"""
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        outside = Path(raw).parent / "outside_secret.txt"
        outside.write_text("SK-FAKE-OUTSIDE-0003\n", encoding="utf-8")
        # ① 绝对路径 / 逃逸 / .env 名一律拒绝
        expect_raises("artifact-absolute-path-rejected", (MalformedInputError,),
                      collect_artifact_files, tmp, relatives=[str(outside)])
        expect_raises("artifact-escaping-path-rejected", (MalformedInputError,),
                      collect_artifact_files, tmp, relatives=["../outside_secret.txt"])
        expect_raises("artifact-env-file-name-rejected", (CredentialLeakError,),
                      collect_artifact_files, tmp, relatives=[".env"])
        expect_raises("artifact-credential-name-rejected", (CredentialLeakError,),
                      collect_artifact_files, tmp, relatives=["src/api_key_store.py"])
        ok_artifacts = collect_artifact_files(tmp, relatives=["src/app.py"])
        check("artifact-relative-path-accepted", list(ok_artifacts) == ["src/app.py"])
        expect_raises("artifact-path-bounds-in-identity", (MalformedInputError,),
                      collect_runtime_identity, tmp, environment="demo",
                      runtime_instance_id="rt-test", execution_mode="demo", env={},
                      credentials_present={"SENSENOVA_API_KEY": True},
                      raw_settings=SETTINGS, raw_agents=AGENTS,
                      artifact_relatives=[str(outside)])
        check("artifact-outside-content-not-in-identity",
              "SK-FAKE-OUTSIDE-0003" not in json.dumps(build(tmp).as_dict()))

        # ② 畸形载荷结构化拒绝（不再静默当空配置）
        base_kwargs = {"environment": "demo", "runtime_instance_id": "rt-test",
                       "execution_mode": "demo",
                       "credentials_present": {"SENSENOVA_API_KEY": True},
                       "raw_settings": SETTINGS, "raw_agents": AGENTS}
        for label, bad in (("raw_settings", [1, 2]), ("raw_agents", ["ai_trader"]),
                           ("env", ["EXECUTION_MODE"])):
            expect_raises(f"malformed-{label}-rejected", (MalformedInputError,),
                          collect_runtime_identity, tmp,
                          **{**base_kwargs, label: bad})
        expect_raises("malformed-profile-overrides-rejected", (MalformedInputError,),
                      resolve_model_route, raw_settings=SETTINGS, raw_agents=AGENTS,
                      profile_overrides=["llm"])
        expect_raises("malformed-credentials-rejected", (MalformedInputError,),
                      resolve_model_route, raw_settings=SETTINGS, raw_agents=AGENTS,
                      credentials_present=["SENSENOVA_API_KEY"])
        expect_raises("empty-runtime-instance-rejected", (MissingRuntimeInputError,),
                      collect_runtime_identity, tmp, environment="demo",
                      runtime_instance_id="", execution_mode="demo", env={},
                      credentials_present={"SENSENOVA_API_KEY": True},
                      raw_settings=SETTINGS, raw_agents=AGENTS)

        # ③ 零卡 / 截断策略卡：不得建出可放行身份
        cards_path = tmp / "knowledge" / "strategy_cards.json"
        cards_path.write_text('{"cards": []}', encoding="utf-8")
        expect_raises("zero-card-strategy-rejected", (MissingRuntimeInputError,),
                      load_strategy_cards, tmp)
        expect_raises("zero-card-strategy-rejected-in-identity",
                      (MissingRuntimeInputError,), collect_runtime_identity, tmp,
                      environment="demo", runtime_instance_id="rt-test",
                      execution_mode="demo", env={},
                      credentials_present={"SENSENOVA_API_KEY": True},
                      raw_settings=SETTINGS, raw_agents=AGENTS)
        cards_path.write_text('{"version": 3}', encoding="utf-8")
        expect_raises("cardless-strategy-file-rejected", (MissingRuntimeInputError,),
                      load_strategy_cards, tmp)
        cards_path.write_text(json.dumps(CARDS), encoding="utf-8")
        check("valid-cards-still-accepted", load_strategy_cards(tmp).card_count == 2)


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
        # 未出现在配置里的白名单键不得被默认值填充。
        # r2 起 settings 里恒有 CONFIG_SOURCES_KEY 命名空间（六类来源），
        # 断言先把它剔除再比较白名单点分键集合。
        empty_settings = {"execution": {"default_mode": "demo"}}
        cfg = collect_effective_config(raw_settings=empty_settings, profile=None, env={})
        whitelisted = {k: v for k, v in cfg.settings.items() if k != CONFIG_SOURCES_KEY}
        check("absent-settings-keys-not-filled",
              "llm.default_model" not in cfg.settings
              and whitelisted == {"execution.default_mode": "demo"},
              str(whitelisted))
        check("settings-carries-all-six-config-sources",
              set(cfg.settings[CONFIG_SOURCES_KEY]) ==
              {label for label, _ in CONFIG_SOURCE_FILES},
              str(sorted(cfg.settings[CONFIG_SOURCES_KEY])))
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
        make_root(Path(raw))
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
        # 空模型：本模块结构化拒绝（不再抛 pydantic ValidationError）
        expect_raises("empty-model-is-structured-rejection", (ModelRouteConfigError,),
                      resolve_model_route, raw_settings={"llm": {}},
                      raw_agents={"agents": [{"name": "ai_trader"}]}, env={},
                      credentials_present={"SENSENOVA_API_KEY": True},
                      execution_mode="demo")
        # probe 结果本身可进身份哈希
        check("route-hashable", len(agent_wins.identity_hash()) == 64)
        check("route-hash-changes-with-model",
              agent_wins.identity_hash() != default_wins.identity_hash())


def check_mock_is_visible_and_blocking() -> None:
    with tempfile.TemporaryDirectory() as raw:
        make_root(Path(raw))
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
        # 严格断言（U15 r2：删除旧的 `A or not B` 恒真形式）
        check("workbuddy-allowed-in-live",
              "WORKBUDDY_OUTSIDE_LIVE" not in model_route_blockers(
                  wb_demo.route, mock=False, execution_mode="live", provider="workbuddy"))


NESTED_SECRET = "sk-nested-NEVER-LEAK-0002"


def check_provider_evidence_and_fallback() -> None:
    """r2 P1-1/P1-4/P1-6：递归敏感过滤、provider 能力证据、备用路由不静默。"""
    # ① 嵌套敏感键不得进入 profile_overrides / 身份哈希 / as_dict（C P1-1）
    leaked = resolve_model_route(
        raw_settings=SETTINGS, raw_agents=AGENTS, env={},
        credentials_present={"SENSENOVA_API_KEY": True}, execution_mode="demo",
        profile_overrides={"llm": {"api_key": NESTED_SECRET, "model": "m"}})
    blob = json.dumps(leaked.as_dict(), ensure_ascii=False, sort_keys=True)
    check("nested-secret-not-in-model-route",
          NESTED_SECRET not in blob
          and sensitive_paths(leaked.route.as_payload()) == []
          and leaked.route.profile_overrides["profile"] == {"llm": {"model": "m"}},
          str(leaked.route.profile_overrides))
    check("nested-secret-drop-is-counted",
          any(path.endswith("llm.api_key") for path in leaked.dropped_overrides),
          str(leaked.dropped_overrides))

    # ② WorkBuddy：能力未声明即阻断，且不得写伪证据
    wb_unknown = resolve_model_route(
        raw_settings=SETTINGS, raw_agents=AGENTS,
        env={"MTA_AI_DECISION_PROVIDER": "workbuddy"}, credentials_present={},
        execution_mode="live")
    check("workbuddy-capability-unproven-blocks",
          PROVIDER_CAPABILITY_UNPROVEN in wb_unknown.blockers)
    check("workbuddy-no-fake-credential-evidence",
          wb_unknown.route.profile_overrides.get("mock_source") != "credential_present"
          and wb_unknown.provider_evidence == "unproven",
          str(wb_unknown.as_dict()))
    wb_absent = resolve_model_route(
        raw_settings=SETTINGS, raw_agents=AGENTS,
        env={"MTA_AI_DECISION_PROVIDER": "workbuddy"}, credentials_present={},
        execution_mode="live", provider_available={"workbuddy": False})
    check("workbuddy-declared-unavailable-blocks",
          PROVIDER_UNAVAILABLE in wb_absent.blockers
          and wb_absent.provider_evidence == "declared_unavailable")
    wb_ok = resolve_model_route(
        raw_settings=SETTINGS, raw_agents=AGENTS,
        env={"MTA_AI_DECISION_PROVIDER": "workbuddy"}, credentials_present={},
        execution_mode="live", provider_available={"workbuddy": True})
    check("workbuddy-declared-available-allows",
          wb_ok.blockers == () and wb_ok.provider_evidence == "declared_available"
          and not wb_ok.mock, str(wb_ok.as_dict()))
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_source_root(Path(raw))
        live_env = {"EXECUTION_MODE": "live", "MTA_AI_DECISION_PROVIDER": "workbuddy",
                    "MTA_ACCOUNT_REF": "acct-live"}
        unproven_bundle = collect_runtime_identity(
            tmp, environment="live", runtime_instance_id="rt-test", profile="live",
            execution_mode="live", env=live_env, credentials_present={},
            raw_settings=SETTINGS, raw_agents=AGENTS)
        proven_bundle = collect_runtime_identity(
            tmp, environment="live", runtime_instance_id="rt-test", profile="live",
            execution_mode="live", env=live_env, credentials_present={},
            provider_available={"workbuddy": True},
            raw_settings=SETTINGS, raw_agents=AGENTS)
        check("workbuddy-live-blocks-entry-without-evidence",
              not unproven_bundle.entry_allowed
              and PROVIDER_CAPABILITY_UNPROVEN in unproven_bundle.blockers,
              str(unproven_bundle.blockers))
        check("workbuddy-live-allows-entry-with-evidence",
              proven_bundle.entry_allowed, str(proven_bundle.blockers))

    # ③ 备用 provider 不认识 → 原因码；非 ai_trader 不构造备用（C P1-4/P1-6）
    dash = {"EXECUTION_MODE": "live", "MTA_AI_DECISION_FALLBACK_PROVIDER": "anthropic",
            "ANTHROPIC_API_KEY": "present"}
    unknown_fb = resolve_model_route(raw_settings=SETTINGS, raw_agents=AGENTS, env=dash,
                                     credentials_present={"SENSENOVA_API_KEY": True},
                                     execution_mode="live")
    check("unrecognised-fallback-provider-is-not-silent",
          FALLBACK_PROVIDER_UNRECOGNISED in unknown_fb.blockers
          and tuple(unknown_fb.route.fallback_routes) == ()
          and unknown_fb.sources["fallback"].startswith("unrecognised-provider"),
          str(unknown_fb.sources))
    analyst = {"EXECUTION_MODE": "live", "MTA_AI_DECISION_FALLBACK_PROVIDER": "opencode",
               "OPENCODE_MODEL": "union-alpha", "OPENCODE_BASE_URL": "https://opencode.ai/zen/v1",
               "OPENCODE_API_KEY": "present"}
    analyst_route = resolve_model_route(raw_settings=SETTINGS, raw_agents=AGENTS, env=analyst,
                                        credentials_present={"SENSENOVA_API_KEY": True},
                                        agent_name="market_analyst", execution_mode="live")
    check("no-fallback-for-non-ai-trader",
          tuple(analyst_route.route.fallback_routes) == ()
          and analyst_route.sources["fallback"] == "skipped:agent-not-ai-trader"
          and analyst_route.blockers == (), str(analyst_route.sources))
    # 调用方显式声明备用凭据缺失 → 结构化拒绝（不再只有 env 一条判定路径）
    expect_raises("fallback-declared-unavailable-rejected", (FallbackRouteError,),
                  resolve_model_route, raw_settings=SETTINGS, raw_agents=AGENTS,
                  env=analyst,
                  credentials_present={"SENSENOVA_API_KEY": True, "OPENCODE_API_KEY": False},
                  execution_mode="live")
    check("fallback-opencode-explicitly-declared-path", True,
          "credentials_present 优先于 env 缺省")


def check_mock_blocks_new_entry_via_u02_gate() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        # r3：bundle 与 generation 必须指向同一账户。此前 bundle 走 <env>:local
        # 回退（demo:local），与 generation 的 demo:acct 不一致 —— 引入 NEW-A
        # 交叉校验后这种“身份与运行代根本不是一回事”的组合必须先被
        # IDENTITY_GENERATION_MISMATCH 拦下，本函数要验的模型/就绪阻断就测不到了。
        bundle = build(tmp, credentials={"SENSENOVA_API_KEY": False},
                       declared_account_ref="acct")
        generation = mint_runtime_generation(
            environment="demo", account_ref=derive_account_ref("demo", declared_account_ref="acct"),
            runtime_instance_id="rt-test", scope_hash="scope-1")
        decision = authorize_new_entry(bundle, generation, expected=generation,
                                       environment="demo", account_ref=generation.account_ref,
                                       runtime_instance_id="rt-test", scope_hash="scope-1",
                                       readiness_allowed=True)
        check("mock-blocks-authorize-new-entry", not decision.allowed
              and "MODEL_ROUTE_MOCK" in decision.reason_codes and decision.http_status == 409)
        ok_bundle = build(tmp, credentials={"SENSENOVA_API_KEY": True},
                          declared_account_ref="acct")
        ok = authorize_new_entry(ok_bundle, generation, expected=generation,
                                 environment="demo", account_ref=generation.account_ref,
                                 runtime_instance_id="rt-test", scope_hash="scope-1",
                                 readiness_allowed=True)
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
                                         runtime_instance_id="rt-test", scope_hash="scope-1",
                                         readiness_allowed=True)
        check("account-mismatch-blocks-entry", not mismatched.allowed
              and "ACCOUNT_REF_MISMATCH" in mismatched.reason_codes)
        # 预期提交不符必须拒绝（F13）
        git_mismatch = authorize_new_entry(ok_bundle, generation, expected=generation,
                                           environment="demo", account_ref=generation.account_ref,
                                           runtime_instance_id="rt-test", scope_hash="scope-1",
                                           expected_git_commit="deadbeef" * 5,
                                           readiness_allowed=True)
        check("git-mismatch-blocks-entry", not git_mismatch.allowed
              and "GIT_COMMIT_MISMATCH" in git_mismatch.reason_codes)


def check_gate_is_fail_closed() -> None:
    """U15 r2 P0-1：省略 expected / readiness 不再等于“跳过校验”。"""
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        bundle = build(tmp, credentials={"SENSENOVA_API_KEY": True})
        bound = mint_runtime_generation(
            environment="demo", account_ref=derive_account_ref("demo", declared_account_ref="acct"),
            runtime_instance_id="rt-test", scope_hash="scope-1")
        foreign = mint_runtime_generation(
            environment="live", account_ref=derive_account_ref("live", declared_account_ref="other"),
            runtime_instance_id="rt-OTHER", scope_hash="scope-other")

        omitted = authorize_new_entry(bundle, foreign, readiness_allowed=True)
        check("gate-omitted-expected-is-fail-closed",
              not omitted.allowed and omitted.http_status == 409
              and IDENTITY_BINDING_UNPROVEN in omitted.reason_codes,
              str(omitted.as_dict()))

        no_readiness = authorize_new_entry(bundle, bound, expected=bound)
        check("gate-omitted-readiness-is-fail-closed",
              not no_readiness.allowed and READINESS_UNPROVEN in no_readiness.reason_codes,
              str(no_readiness.as_dict()))

        # 异环境/异账户/异运行代：显式绑定校验必须逐项报出
        bound_check = authorize_new_entry(bundle, foreign, expected=bound,
                                          environment="demo",
                                          account_ref=bound.account_ref,
                                          runtime_instance_id="rt-test",
                                          scope_hash="scope-1",
                                          readiness_allowed=True)
        check("gate-foreign-generation-rejected",
              not bound_check.allowed
              and {"ENVIRONMENT_MISMATCH", "ACCOUNT_REF_MISMATCH",
                   "RUNTIME_INSTANCE_MISMATCH", "RUNTIME_GENERATION_MISMATCH"}
              <= set(bound_check.reason_codes), str(bound_check.reason_codes))

        # scope_hash：调用方要求了域绑定而运行代没有 → UNBOUND（不是静默通过）
        unbound = mint_runtime_generation(
            environment="demo", account_ref=derive_account_ref("demo", declared_account_ref="acct"),
            runtime_instance_id="rt-test")
        unbound_binding = verify_account_binding(bound, unbound, scope_hash="scope-1")
        check("scope-hash-unbound-rejected",
              not unbound_binding.allowed and SCOPE_HASH_UNBOUND in unbound_binding.reason_codes,
              str(unbound_binding.reason_codes))
        # expected 与 actual 的 scope_hash 不一致 → MISMATCH（此前从不比较）
        other_scope = mint_runtime_generation(
            environment="demo", account_ref=derive_account_ref("demo", declared_account_ref="acct"),
            runtime_instance_id="rt-test", scope_hash="scope-2")
        cross = verify_account_binding(bound, other_scope)
        check("scope-hash-compared-both-ways",
              not cross.allowed and SCOPE_HASH_MISMATCH in cross.reason_codes,
              str(cross.reason_codes))
        # 两边都未声明 scope 时不比较（不制造假阳性）
        no_scope_a = mint_runtime_generation(
            environment="demo", account_ref=derive_account_ref("demo", declared_account_ref="acct"),
            runtime_instance_id="rt-test")
        check("scope-hash-absent-on-both-sides-is-not-a-mismatch",
              verify_account_binding(no_scope_a, no_scope_a).allowed)
        # 畸形输入结构化拒绝（不抛 AttributeError）
        malformed = authorize_new_entry(bundle, {"generation_id": "x"}, expected=bound,
                                        readiness_allowed=True)
        check("gate-malformed-generation-rejected",
              not malformed.allowed and RUNTIME_GENERATION_MALFORMED in malformed.reason_codes)
        malformed_bundle = authorize_new_entry("not-a-bundle", bound, expected=bound,
                                               readiness_allowed=True)
        check("gate-malformed-bundle-rejected",
              not malformed_bundle.allowed and IDENTITY_MALFORMED in malformed_bundle.reason_codes)
        malformed_expected = authorize_new_entry(bundle, bound, expected="bound",
                                                 readiness_allowed=True)
        check("gate-malformed-expected-rejected", not malformed_expected.allowed
              and RUNTIME_GENERATION_MALFORMED in malformed_expected.reason_codes)


def check_bundle_generation_cross_check() -> None:
    """r3 NEW-A：bundle.identity 与 generation 必须指向同一环境/账户。"""
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        demo_bundle = build(tmp, declared_account_ref="acct-demo")
        live_bundle = collect_runtime_identity(
            tmp, environment="live", runtime_instance_id="rt-LIVE",
            profile="live", execution_mode="live",
            env={"EXECUTION_MODE": "live", "MTA_PROFILE": "live"},
            credentials_present={"SENSENOVA_API_KEY": True},
            raw_settings=SETTINGS, raw_agents=AGENTS,
            declared_account_ref="live-acct-other", fallback_local=True,
        )
        gen_demo = mint_runtime_generation(
            environment="demo", account_ref=derive_account_ref("demo", declared_account_ref="acct-demo"),
            runtime_instance_id="rt-demo", scope_hash="scope-D")
        gen_live = mint_runtime_generation(
            environment="live", account_ref=derive_account_ref("live", declared_account_ref="live-acct-other"),
            runtime_instance_id="rt-LIVE", scope_hash="scope-L")

        # 修复前：两者均为 allowed=True / 200 / reason_codes=()
        forward = authorize_new_entry(demo_bundle, gen_live, expected=gen_live,
                                      readiness_allowed=True)
        check("new-a-demo-bundle-live-generation-rejected",
              not forward.allowed and forward.http_status == 409
              and forward.reason_codes == (IDENTITY_GENERATION_MISMATCH,),
              str(forward.as_dict()))
        # 全部显式绑定到该 live 运行代也**不能**绕过（这正是复查的绕过形态）
        fully_bound = authorize_new_entry(
            demo_bundle, gen_live, expected=gen_live,
            environment="live", account_ref=gen_live.account_ref,
            runtime_instance_id="rt-LIVE", scope_hash="scope-L",
            readiness_allowed=True)
        check("new-a-explicit-bindings-cannot-bypass",
              not fully_bound.allowed and fully_bound.http_status == 409
              and IDENTITY_GENERATION_MISMATCH in fully_bound.reason_codes,
              str(fully_bound.as_dict()))
        # 反向：live bundle + demo generation
        reverse = authorize_new_entry(live_bundle, gen_demo, expected=gen_demo,
                                      readiness_allowed=True)
        check("new-a-live-bundle-demo-generation-rejected",
              not reverse.allowed and IDENTITY_GENERATION_MISMATCH in reverse.reason_codes,
              str(reverse.as_dict()))
        # 对照：身份与运行代自洽时必须放行（不制造假阳性）
        control = authorize_new_entry(demo_bundle, gen_demo, expected=gen_demo,
                                      readiness_allowed=True)
        check("new-a-consistent-identity-and-generation-allowed",
              control.allowed, str(control.reason_codes))
        # 交叉校验只比环境/账户：实例号不同不误报（实例绑定用 runtime_instance_id）
        other_instance = mint_runtime_generation(
            environment="demo", account_ref=derive_account_ref("demo", declared_account_ref="acct-demo"),
            runtime_instance_id="rt-other-instance", scope_hash="scope-D")
        check("new-a-instance-difference-is-not-a-cross-mismatch",
              authorize_new_entry(demo_bundle, other_instance, expected=other_instance,
                                  readiness_allowed=True).allowed)
        # 账户/环境逐项：同一环境换账户也要拒绝
        same_env_other_account = mint_runtime_generation(
            environment="demo", account_ref=derive_account_ref("demo", declared_account_ref="someone-else"),
            runtime_instance_id="rt-demo", scope_hash="scope-D")
        check("new-a-same-environment-other-account-rejected",
              IDENTITY_GENERATION_MISMATCH in authorize_new_entry(
                  demo_bundle, same_env_other_account,
                  expected=same_env_other_account,
                  readiness_allowed=True).reason_codes)


def check_generation_input_types_are_strict() -> None:
    """r3 NEW-D：mint 的数值/文本字段类型不对 → 结构化拒绝，不抛裸异常。"""
    ref = derive_account_ref("demo", declared_account_ref="acct")
    base = dict(environment="demo", account_ref=ref, runtime_instance_id="rt-1")
    for label, extra in (
        ("epoch-string", {"credential_epoch": "3"}),
        ("epoch-none", {"credential_epoch": None}),
        ("epoch-bool", {"credential_epoch": True}),
        ("epoch-float", {"credential_epoch": 3.7}),
        ("epoch-list", {"credential_epoch": [3]}),
        ("started-at-string", {"started_at": "now"}),
        ("started-at-object", {"started_at": object()}),
        ("scope-string-int", {"scope_hash": 123}),
        ("fingerprint-int", {"key_fingerprint_value": 123}),
        ("environment-int", {"environment": 123}),
        ("account-ref-dict", {"account_ref": {"value": "x"}}),
        ("account-ref-none", {"account_ref": None}),
        ("instance-int", {"runtime_instance_id": 7}),
    ):
        kwargs = dict(base)
        kwargs.update(extra)
        expect_raises(f"mint-rejects-{label}", (MalformedInputError,),
                      mint_runtime_generation, **kwargs)
    # 保留既有的语义化拒绝（不能被上面的类型检查掩盖）
    expect_raises("mint-still-rejects-empty-environment", (MissingRuntimeInputError,),
                  mint_runtime_generation, environment="", account_ref=ref,
                  runtime_instance_id="rt-1")
    expect_raises("mint-still-rejects-unknown-environment", (ExecutionModeError,),
                  mint_runtime_generation, environment="prod", account_ref=ref,
                  runtime_instance_id="rt-1")
    expect_raises("mint-still-rejects-empty-instance", (MissingRuntimeInputError,),
                  mint_runtime_generation, environment="demo", account_ref=ref,
                  runtime_instance_id="   ")
    expect_raises("mint-still-rejects-negative-epoch", (RuntimeIdentityError,),
                  mint_runtime_generation, environment="demo", account_ref=ref,
                  runtime_instance_id="rt-1", credential_epoch=-1)
    # 合法输入不受影响
    ok = mint_runtime_generation(environment="demo", account_ref=ref,
                                 runtime_instance_id="rt-1", credential_epoch=2,
                                 started_at=1000.0, scope_hash="scope-1",
                                 key_fingerprint_value=key_fingerprint("k"))
    check("mint-accepts-well-formed-inputs",
          ok.credential_epoch == 2 and ok.started_at == 1000.0
          and ok.scope_hash == "scope-1", str(ok.as_dict()))


def check_fallback_provider_env_is_honoured() -> None:
    """r3 NEW-F：``llm.decision_fallback.provider_env`` 改名后备用不得静默丢失。"""
    renamed = json.loads(json.dumps(SETTINGS))
    renamed["llm"]["decision_fallback"]["provider_env"] = "MY_FALLBACK_ENV"
    env_common = {"OPENCODE_MODEL": "union-alpha",
                  "OPENCODE_BASE_URL": "https://opencode.ai/zen/v1"}
    creds = {"SENSENOVA_API_KEY": True, "OPENCODE_API_KEY": True}
    via_declared_name = resolve_model_route(
        raw_settings=renamed, raw_agents=AGENTS,
        env={**env_common, "MY_FALLBACK_ENV": "opencode"},
        credentials_present=creds, execution_mode="live")
    check("fallback-provider-env-declared-name-is-read",
          len(tuple(via_declared_name.route.fallback_routes)) == 1
          and via_declared_name.sources["fallback"].startswith("declared:opencode"),
          str(via_declared_name.sources))
    # 未设置声明名 → 仍按缺省名（不因改名而把旧变量当不存在）
    via_default_name = resolve_model_route(
        raw_settings=renamed, raw_agents=AGENTS,
        env={**env_common, "MTA_AI_DECISION_FALLBACK_PROVIDER": "opencode"},
        credentials_present=creds, execution_mode="live")
    check("fallback-provider-env-default-name-still-works",
          len(tuple(via_default_name.route.fallback_routes)) == 1,
          str(via_default_name.sources))
    # 两个名字都没设 → 如实 absent（不是静默识别）
    absent = resolve_model_route(
        raw_settings=renamed, raw_agents=AGENTS, env=dict(env_common),
        credentials_present=creds, execution_mode="live")
    check("fallback-provider-env-absent-is-reported",
          tuple(absent.route.fallback_routes) == ()
          and absent.sources["fallback"] == "absent", str(absent.sources))
    check("fallback-provider-env-name-is-exported-as-evidence",
          via_declared_name.sources["fallback_provider_env"] == "MY_FALLBACK_ENV")


def check_dependency_comparison_status() -> None:
    """r3 NEW-E：diff-2 缺口②的 comparison_status 必须真的产出。"""
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        (tmp / "requirements-dev-lock.txt").write_text("pytest-lock==1\n", encoding="utf-8")
        check("dependency-comparison-match-when-all-declared",
              compare_dependency_lock(tmp) == DEPENDENCY_COMPARISON_MATCH)
        check("dependency-comparison-match-with-explicit-declaration",
              compare_dependency_lock(tmp, declared=list(DEPENDENCY_LOCK_FILES))
              == DEPENDENCY_COMPARISON_MATCH)
        check("dependency-comparison-mismatch-on-drifted-declaration",
              compare_dependency_lock(tmp, declared=["Pipfile.lock"])
              == DEPENDENCY_COMPARISON_MISMATCH)
        check("dependency-comparison-unavailable-on-bad-shape",
              compare_dependency_lock(tmp, declared="requirements-lock.txt")
              == DEPENDENCY_COMPARISON_UNAVAILABLE)
        # 名单齐备但盘上缺文件 → UNAVAILABLE（名单漂移优先于缺文件，见 MISMATCH 用例）
        (tmp / "requirements-lock.txt").unlink()
        check("dependency-comparison-unavailable-when-declared-file-missing",
              compare_dependency_lock(tmp, declared=list(DEPENDENCY_LOCK_FILES))
              == DEPENDENCY_COMPARISON_UNAVAILABLE)
        check("dependency-comparison-unavailable-without-deployed-lock",
              compare_dependency_lock(tmp) == DEPENDENCY_COMPARISON_UNAVAILABLE)
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        (tmp / "requirements-dev-lock.txt").write_text("pytest-lock==1\n", encoding="utf-8")
        bundle = build(tmp, declared_account_ref="acct")
        check("bundle-produces-dependency-comparison-status",
              bundle.dependency_comparison_status == DEPENDENCY_COMPARISON_MATCH
              and bundle.as_dict()["dependency_comparison_status"]
              == DEPENDENCY_COMPARISON_MATCH,
              str(bundle.as_dict()["dependency_comparison_status"]))
        (tmp / "requirements-dev-lock.txt").unlink()
        degraded = build(tmp, declared_account_ref="acct")
        check("bundle-reports-unavailable-when-lock-missing",
              degraded.dependency_comparison_status == DEPENDENCY_COMPARISON_UNAVAILABLE)


def check_unknown_mode_is_strictest() -> None:
    """U15 r2 P1-5：模式未知时 WorkBuddy 阻断必须触发（旧实现为空则不阻断）。"""
    route = ModelRoute(model="deepseek-v4.1-flash", effort="max",
                       profile_overrides={"provider": "workbuddy", "mock": False})
    for unknown in (None, "", "   "):
        codes = model_route_blockers(route, mock=False, provider="workbuddy",
                                     execution_mode=unknown)
        check(f"workbuddy-blocked-when-mode-unknown({unknown!r})",
              "WORKBUDDY_OUTSIDE_LIVE" in codes, str(codes))
    check("workbuddy-allowed-when-mode-live",
          model_route_blockers(route, mock=False, provider="workbuddy",
                               execution_mode="live") == ())
    check("workbuddy-allowed-in-paper",
          model_route_blockers(route, mock=False, provider="workbuddy",
                               execution_mode="paper") == ())


def check_scope_is_locked_or_flagged() -> None:
    """decl-7：未选 profile 时作用域不得静默 —— 有 warning，live 下阻断。"""
    unlocked = resolve_execution_scope(profile=None, execution_mode="live")
    check("unlocked-scope-has-warning",
          not unlocked.locked and EXECUTION_PROFILE_NOT_DECLARED in unlocked.warnings,
          str(unlocked.as_dict()))
    locked = resolve_execution_scope(profile="live", execution_mode="live")
    check("locked-scope-has-no-warning", locked.locked and locked.warnings == ())
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        live_unlocked = build(tmp, profile=None, execution_mode="live", env={},
                              declared_account_ref="acct-live")
        check("live-without-profile-blocked",
              not live_unlocked.entry_allowed
              and EXECUTION_SCOPE_UNLOCKED in live_unlocked.blockers,
              str(live_unlocked.blockers))
        check("live-without-profile-advisory-visible",
              EXECUTION_PROFILE_NOT_DECLARED in live_unlocked.advisories,
              str(live_unlocked.advisories))
        demo_unlocked = build(tmp, profile=None, execution_mode="demo", env={})
        check("demo-without-profile-not-blocked",
              EXECUTION_SCOPE_UNLOCKED not in demo_unlocked.blockers
              and EXECUTION_PROFILE_NOT_DECLARED in demo_unlocked.advisories)


def check_account_ref_is_proven_or_blocked() -> None:
    """r2 P1-2 关闭：account_ref 是否来自声明必须可见，live 不得静默回退。"""
    with tempfile.TemporaryDirectory() as raw:
        tmp = make_root(Path(raw))
        # demo：允许回退，但必须有 advisory + 溯源字段
        demo = build(tmp, execution_mode="demo", env={})
        check("local-fallback-labelled-in-bundle",
              demo.identity.account_ref == "demo:local" and not demo.account_ref_stable
              and demo.account_ref_source == "local-fallback"
              and ACCOUNT_REF_LOCAL_FALLBACK in demo.advisories,
              str(demo.as_dict()["account_ref_provenance"]))
        check("local-fallback-not-blocking-in-demo",
              ACCOUNT_REF_UNPROVEN not in demo.blockers)
        # live：回退必须是阻断项（跨账户碰撞）
        live = build(tmp, profile="live", execution_mode="live", env={})
        check("local-fallback-blocked-in-live",
              not live.entry_allowed and ACCOUNT_REF_UNPROVEN in live.blockers
              and ACCOUNT_REF_LOCAL_FALLBACK in live.advisories,
              str(live.blockers))
        # 显式声明 → 稳定引用，无回退告警
        declared = build(tmp, execution_mode="demo", env={},
                         declared_account_ref="binance-main")
        check("declared-account-ref-is-stable",
              declared.identity.account_ref == "demo:binance-main"
              and declared.account_ref_stable
              and ACCOUNT_REF_LOCAL_FALLBACK not in declared.advisories)
        # 显式允许 live 回退：可见但不阻断
        live_allowed = build(tmp, profile="live", execution_mode="live", env={},
                             fallback_local=True)
        check("live-fallback-explicitly-allowed",
              live_allowed.entry_allowed
              and ACCOUNT_REF_LOCAL_FALLBACK in live_allowed.advisories)
        # 显式禁止回退：缺失即结构化拒绝
        expect_raises("fallback-local-false-rejects-missing-account",
                      (MissingRuntimeInputError,), collect_runtime_identity, tmp,
                      environment="demo", runtime_instance_id="rt-test",
                      execution_mode="demo", env={},
                      credentials_present={"SENSENOVA_API_KEY": True},
                      raw_settings=SETTINGS, raw_agents=AGENTS,
                      fallback_local=False)


def check_generation_construction_is_strict() -> None:
    """r2 P1-2/P1-3：mint 不得静默忽略 environment，裸字符串必须归一化。"""
    demo_ref = derive_account_ref("demo", declared_account_ref="acct")
    expect_raises("mint-rejects-conflicting-environment", (ExecutionModeError,),
                  mint_runtime_generation, environment="live", account_ref=demo_ref,
                  runtime_instance_id="rt-1")
    expect_raises("mint-rejects-unknown-environment", (ExecutionModeError,),
                  mint_runtime_generation, environment="prod",
                  account_ref=derive_account_ref("prod", declared_account_ref="acct"),
                  runtime_instance_id="rt-1")
    expect_raises("mint-rejects-empty-environment", (MissingRuntimeInputError,),
                  mint_runtime_generation, environment="", account_ref=demo_ref,
                  runtime_instance_id="rt-1")
    bare = mint_runtime_generation(environment="demo", account_ref="acct",
                                   runtime_instance_id="rt-1")
    via_ref = mint_runtime_generation(environment="demo", account_ref=demo_ref,
                                      runtime_instance_id="rt-1")
    check("bare-string-account-ref-is-normalised",
          bare.account_ref == "demo:acct" and bare.account_ref == via_ref.account_ref
          and bare.generation_id == via_ref.generation_id, bare.account_ref)
    expect_raises("mint-rejects-empty-string-account-ref", (MissingRuntimeInputError,),
                  mint_runtime_generation, environment="demo", account_ref="",
                  runtime_instance_id="rt-1")


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
        # 锁文件在场时不得误报缺口（对照）
        complete = build(tmp)
        check("coverage-complete-when-lock-present",
              len(complete.identity.dependency_hash) == 64 and complete.entry_allowed,
              str(complete.blockers))
        # 依赖锁缺失时 build_identity 直接拒绝（覆盖缺口不可静默）。
        # r2 起这条断言真的会触发缺口：先删锁文件再关掉 require_lock_file。
        (tmp / "requirements-lock.txt").unlink()
        expect_raises("coverage-gap-reported-as-identity-coverage-error",
                      (IdentityCoverageError,),
                      collect_runtime_identity, tmp, environment="demo",
                      runtime_instance_id="rt", execution_mode="demo", env={},
                      credentials_present={"SENSENOVA_API_KEY": True},
                      raw_settings=SETTINGS, raw_agents=AGENTS,
                      require_lock_file=False)
        # 缺口内容也可直接查（不依赖异常路径）
        lock_files = collect_dependency_lock_files(tmp, require_lock=False)
        check("coverage-gap-name-is-dependency-lock", lock_files == {})


def check_fingerprint_disposition_registered() -> None:
    table = fingerprint_disposition()
    check("disposition-covers-report-items", len(table) >= 15, f"items={len(table)}")
    valid = {"fixed", "registered", "aligned", "unknown"}
    check("disposition-status-valid",
          all(item["status"] in valid for item in table.values()))
    check("disposition-has-note-and-owner",
          all(item.get("note") and item.get("owner") for item in table.values()))
    # r2：F 报告 §3 的 15 条 decl + §1 的 7 条 diff 必须逐条出现（不得漏项）
    required = {f"diff-{i}" for i in range(1, 8)} | {f"decl-{i}" for i in range(1, 16)}
    check("disposition-covers-all-decl-and-diff-items",
          required <= set(table), str(sorted(required - set(table))))
    # r2：声称 fixed 的项不得同时存在"未接线/未做"的自认（diff-7 曾声称接口已提供）
    check("diff-7-registration-is-honest",
          table["diff-7"]["status"] == "registered"
          and "未做" in table["diff-7"]["note"],
          table["diff-7"]["note"])
    check("decl-7-registration-is-honest",
          table["decl-7"]["status"] == "fixed"
          and "EXECUTION_SCOPE_UNLOCKED" in table["decl-7"]["note"])
    registered = sorted(k for k, v in table.items() if v["status"] == "registered")
    print(f"INFO fingerprint disposition: fixed="
          f"{sum(1 for v in table.values() if v['status'] == 'fixed')} "
          f"registered={len(registered)} {registered}")


def check_environment_state_root_interface() -> None:
    """diff-7 缺口② 的最小一致性判定接口（纯函数，接线归 U13）。"""
    ok = verify_environment_state_root("live", "data/live", profile="live",
                                       project_root="C:/repo")
    check("state-root-consistent-combination", ok == (), str(ok))
    check("state-root-live-without-profile-flagged",
          STATE_ROOT_PROFILE_UNLOCKED in verify_environment_state_root(
              "live", "data/live", project_root="C:/repo"))
    check("state-root-shared-live-root-flagged",
          LIVE_SHARED_STATE_ROOT in verify_environment_state_root(
              "live", "data", profile="live", project_root="C:/repo")
          and LIVE_SHARED_STATE_ROOT in verify_environment_state_root(
              "live", ".", profile="live", project_root="C:/repo"))
    check("state-root-profile-environment-mismatch",
          ENVIRONMENT_STATE_ROOT_MISMATCH in verify_environment_state_root(
              "live", "data/live", profile="demo", project_root="C:/repo"))
    check("state-root-unbound-inputs",
          {"ENVIRONMENT_UNBOUND", "STATE_ROOT_UNBOUND"} <= set(
              verify_environment_state_root("prod", "", profile=None)))
    check("state-root-demo-shared-root-is-fine",
          LIVE_SHARED_STATE_ROOT not in verify_environment_state_root(
              "demo", "data", profile="demo", project_root="C:/repo"))


def main() -> int:
    check_identity_inputs_change_hash()
    check_config_sources_enter_identity()
    check_input_shape_and_path_bounds()
    check_lock_file_required()
    check_strategy_source_is_file_level()
    check_no_credential_leak()
    check_defaults_are_not_effective_values()
    check_model_route_priority()
    check_mock_is_visible_and_blocking()
    check_provider_evidence_and_fallback()
    check_mock_blocks_new_entry_via_u02_gate()
    check_gate_is_fail_closed()
    check_bundle_generation_cross_check()
    check_generation_input_types_are_strict()
    check_fallback_provider_env_is_honoured()
    check_dependency_comparison_status()
    check_unknown_mode_is_strictest()
    check_scope_is_locked_or_flagged()
    check_account_ref_is_proven_or_blocked()
    check_generation_construction_is_strict()
    check_account_ref_decoupled_from_rotation()
    check_u02_binding_interface()
    check_dl01_cli_mis_selection()
    check_dl01_fallback_illegal_params()
    check_coverage_fail_closed()
    check_environment_state_root_interface()
    check_fingerprint_disposition_registered()
    if FAILURES:
        print(f"\nU15 RUNTIME IDENTITY ACCEPTANCE FAILED: {len(FAILURES)} -> "
              + ", ".join(FAILURES))
        return 1
    print("\nU15 RUNTIME IDENTITY ACCEPTANCE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
