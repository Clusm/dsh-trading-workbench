"""阶段 1 运行脚本：接入免费数据源，循环跑多 Agent 决策。默认只跑一次，加 --loop 持续运行。"""
from __future__ import annotations

import argparse
import os
import sys
import time
import uuid
import warnings
from pathlib import Path

from langchain_core._api.deprecation import LangChainPendingDeprecationWarning  # noqa: E402
warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from exchange.factory import create_broker  # noqa: E402
from core.capital import CapitalManager  # noqa: E402
from core.channels import ChannelManager  # noqa: E402
from data.market_query import get_live_prices  # noqa: E402
from data.market_universe import MarketUniverse  # noqa: E402
from core.quotas import TradeQuotaManager  # noqa: E402
from audit.jsonl_logger import JsonlLogger  # noqa: E402
from core.agent_recorder import AgentRecorder  # noqa: E402
from core.config_loader import load_settings  # noqa: E402
from core.control import ControlState  # noqa: E402
from core.service import build_execution_feedback  # noqa: E402
from core.intel_cache import IntelCache  # noqa: E402
from core.graph import build_graph  # noqa: E402
from llm.factory import build_llm_for_agent  # noqa: E402
from signal_engine import resolve_strategy_lab_src  # noqa: E402


def signal_scanner_config(settings, universe=None) -> dict:
    cfg = dict(settings.raw_settings.get("signal_scanner", {}) or {})
    cfg["strategy_lab_src"] = resolve_strategy_lab_src(
        settings.project_root, cfg.get("strategy_lab_src")
    )
    cfg["signal_state_file"] = str(
        settings.project_root / str(cfg.get("signal_state_file", "data/signal_scanner_state.json"))
    )
    cfg["scan_pool"] = universe.get_scan_pool() if universe is not None else None
    cfg["universal_cards"] = bool(settings.raw_settings.get("signal_scanner", {}).get("universal_cards", True))
    return cfg


def run_one_cycle(settings, db, logger, recorder, broker, capital, quota, llm, model, agent_llms, args, intel_cache, control, execution_feedback=None) -> dict:
    run_id = str(uuid.uuid4())
    decision_id = str(uuid.uuid4())
    capital_result = capital.sync()
    equity_available = bool(capital_result.get("equity_available", True))
    ch_cfg = settings.raw_settings.get("channels", {}) or {}
    channels = ChannelManager(
        state_file=settings.project_root / str(ch_cfg.get("state_file", "data/channel_state.json")),
        broker=broker, db=db,
        strategy_weight=float(ch_cfg.get("strategy_weight", 0.6)),
        ai_weight=float(ch_cfg.get("ai_weight", 0.4)),
        channel_max_margin_usage=float(ch_cfg.get("channel_max_margin_usage", 0.80)),
        ai_daily_loss_stop=float(ch_cfg.get("ai_daily_loss_stop", 0.30)),
    )
    position_symbols = [str(p.get("symbol")) for p in broker.state.get("positions", [])]
    pre_prices = get_live_prices(position_symbols) if position_symbols else {}
    channel_status = channels.sync(pre_prices, session_equity=capital.session_equity())
    mu_cfg = settings.raw_settings.get("market_universe", {}) or {}
    universe = MarketUniverse(
        state_file=settings.project_root / str(mu_cfg.get("state_file", "data/market_universe_state.json")),
        core_symbols=mu_cfg.get("core_symbols") or [],
        quick_core_top=mu_cfg.get("quick_core_top", 3),
        quick_anomaly_top=mu_cfg.get("quick_anomaly_top", 2),
        full_top_n=mu_cfg.get("full_top_n", 20),
        full_anomaly_n=mu_cfg.get("full_anomaly_n", 5),
        short_move_pct=mu_cfg.get("short_move_pct", 3.0),
        full_scan_interval_minutes=mu_cfg.get("full_scan_interval_minutes", 60),
        db=db,
    )
    risk_cfg = dict(settings.raw_risk)
    risk_cfg["channel_max_margin_usage"] = float(ch_cfg.get("channel_max_margin_usage", 0.80))
    risk_cfg["ai_max_leverage"] = int(ch_cfg.get("ai_max_leverage", 20))
    risk_cfg["channel_status"] = channel_status
    risk_cfg["supported_symbols"] = list(getattr(broker, "_symbol_filters", {}).keys()) or None
    graph = build_graph(
        llm=llm,
        model=model,
        db=db,
        logger=logger,
        recorder=recorder,
        risk_config=risk_cfg,
        knowledge_dir=settings.knowledge_dir,
        market_top_n=int(args.top_n),
        market_analyze_top=int(args.analyze_top),
        tavily_api_key=os.getenv("TAVILY_API_KEY", "").strip() or None,
        etherscan_api_key=os.getenv("ETHERSCAN_API_KEY", "").strip() or None,
        cache_dir=settings.project_root / "data/cache",
        enable_live_data=True,
        intel_cache=intel_cache,
        intel_cache_ttl=intel_cache.default_ttl_seconds,
        broker=broker,
        agent_llms=agent_llms,
        portfolio_state=capital.portfolio(),
        available_cash=capital.session_equity(),
        quota_manager=quota,
        signal_scanner_config=signal_scanner_config(settings, universe),
        allow_new_positions=bool(control.allow_new_positions and equity_available),
        execution_feedback=dict(execution_feedback or {}),
    )
    state = graph.invoke({"run_id": run_id, "decision_id": decision_id})
    decision = state.get("decision")
    verdict = state.get("risk_verdict")
    if decision is not None:
        db.log_trade_decision(
            decision_id=decision_id,
            run_id=run_id,
            decision=decision.decision,
            reasoning=decision.reasoning,
            raw_json=decision.model_dump_json(),
            channel=str(getattr(decision, "channel", "ai") or "ai"),
        )
    broker_state = broker.state if isinstance(broker.state, dict) else {}
    return {
        "run_id": run_id,
        "decision": decision,
        "risk_verdict": verdict,
        "market": state.get("market"),
        "macro": state.get("macro"),
        "onchain": state.get("onchain"),
        "strategy": state.get("strategy"),
        "tickets": state.get("tickets"),
        "strategy_decision": state.get("strategy_decision"),
        "ai_decision": state.get("ai_decision"),
        "execution": state.get("execution"),
        "capital_state": {"session_equity": capital.session_equity(),
                          "initial_equity": capital.initial_equity},
        "quota_state": quota.status(),
        "channel_state": channel_status,
        "execution_feedback": build_execution_feedback(
            state, decision, channel_status, broker_state),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="持续运行")
    parser.add_argument("--interval-minutes", type=float, default=None)
    parser.add_argument("--top-n", type=int, default=None)
    parser.add_argument("--analyze-top", type=int, default=None)
    args = parser.parse_args()

    settings = load_settings(PROJECT_ROOT)
    db = AuditDatabase(settings.db_file)
    logger = JsonlLogger(settings.logs_dir)
    recorder = AgentRecorder(db, logger, settings.artifacts_dir)
    broker = create_broker(settings, db)
    if hasattr(broker, "sync_positions"):
        try:
            broker.sync_positions()
        except Exception as e:
            db.log_risk_event(event_type="futures_sync_error", severity="warn",
                              details=f"{type(e).__name__}: {e}", action_taken="skip")
    cap_cfg = settings.raw_settings.get("capital", {})
    quota = TradeQuotaManager(db, settings.raw_settings.get("quotas", {}))
    capital = CapitalManager(
        state_file=settings.project_root / cap_cfg.get("state_file", "data/capital_state.json"),
        broker=broker, db=db,
        initial_equity=float(cap_cfg.get("initial_equity", 200)),
        bankruptcy_threshold=float(cap_cfg.get("bankruptcy_threshold", 1.0)),
        reset_on_bankruptcy=bool(cap_cfg.get("reset_on_bankruptcy", True)),
        # 硬性保证金熔断上限（最后一道防线）：须高于风控常规档(0.80)，避免误杀强信心档仓位。
        max_margin_usage=float(settings.raw_risk.get(
            "strong_max_margin_usage",
            settings.raw_risk.get("max_margin_usage", 0.80),
        )),
    )
    control = ControlState(settings.project_root / "data/control_state.json")
    execution_feedback: dict | None = None
    llm, model = build_llm_for_agent(settings, "ai_trader")
    agent_llms = {}
    for name in ["market_agent", "macro_agent", "onchain_agent"]:
        client, m = build_llm_for_agent(settings, name)
        if m != "mock":
            agent_llms[name] = (client, m)
    intel_cache = IntelCache(
        ttl_seconds=float(settings.raw_settings.get("intel_cache", {}).get("ttl_seconds", 3600))
    )

    if args.top_n is None:
        args.top_n = int(settings.raw_settings.get("symbol_universe", {}).get("top_n", 40))
    if args.analyze_top is None:
        args.analyze_top = min(int(settings.raw_settings.get("symbol_universe", {}).get("analyze_top", 40)), args.top_n)
    interval_min = args.interval_minutes or float(
        settings.raw_settings.get("decision_cycle", {}).get("interval_minutes", 5)
    )

    print(f"phase1 start: llm={model}, top_n={args.top_n}, analyze_top={args.analyze_top}")
    try:
        while True:
            t0 = time.time()
            try:
                result = run_one_cycle(settings, db, logger, recorder, broker, capital, quota, llm, model, agent_llms, args, intel_cache, control, execution_feedback=execution_feedback)
                execution_feedback = result.get("execution_feedback")
                mkt = result["market"]
                mac = result["macro"]
                chain = result["onchain"]
                print("=" * 60)
                print("run_id:", result["run_id"])
                print("market :", mkt.summary if mkt else None)
                print("macro  :", mac.summary if mac else None)
                print("onchain:", chain.summary if chain else None)
                strat = result["strategy"]
                if strat is not None:
                    print("strategy: " + " | ".join(
                        f"{r.strategy_id} {r.name} score_note={r.note}" for r in strat.refs
                    ))
                print("decision:", result["decision"].model_dump_json() if result["decision"] else None)
                print("risk    :", result["risk_verdict"].model_dump_json() if result["risk_verdict"] else None)
                print("quota   :", result.get("quota_state"))
                print("elapsed :", round(time.time() - t0, 1), "s")
            except Exception as e:
                print("cycle_error:", type(e).__name__, e)
                db.log_risk_event(event_type="cycle_error", severity="error",
                                  details=f"{type(e).__name__}: {e}", action_taken="skip cycle")
            if not args.loop:
                break
            time.sleep(interval_min * 60)
    except KeyboardInterrupt:
        print("stopped by user")
    finally:
        db.close()


if __name__ == "__main__":
    main()
