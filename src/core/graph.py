"""阶段 0 最小多 Agent 图：三个情报源 -> 策略库 -> 主Agent -> 风控。"""
from __future__ import annotations

import time
from typing import Any, Callable, TypedDict

from pathlib import Path
import uuid

from langgraph.graph import END, START, StateGraph

from agents import macro, market, onchain, orchestrator, risk, strategy_library, strategy_trader, summarizer
from agents import beliefs
from agents.models import (
    ChannelDecision, MacroBrief, MarketBrief, OnchainBrief, RiskVerdict,
    StrategyRefs, TradeDecision,
)
from signal_engine import run_signal_scan
from audit.database import AuditDatabase
from audit.jsonl_logger import JsonlLogger
from core.action_space import ActionSpace
from core.agent_recorder import AgentRecorder
from core.decision_pipeline import authorize_decision, sign_execution
from core.position_guardian import evaluate_positions
from core.trading_scope import TradingScope
from data.market_query import get_live_prices
from llm.base import LLMClient


class AgentState(TypedDict, total=False):
    """LangGraph 单轮共享状态；字段均为当前 run 的结构化中间产物。"""
    run_id: str
    decision_id: str
    market: MarketBrief
    macro: MacroBrief
    onchain: OnchainBrief
    strategy: StrategyRefs
    tickets: list[dict[str, Any]]
    strategy_decision: ChannelDecision
    ai_decision: ChannelDecision
    guardian_decision: TradeDecision
    # 会合屏障：merge 有两个上游分支（strategy_trader 2 跳 / ai_trader 3 跳），
    # LangGraph 的扇入屏障只在同一超级步内有效，跨步完成时 merge 会被触发两次，
    # 导致下游 risk/execution 各跑两遍（adjust 意图重复撤旧挂新）。该标记确保
    # risk/execution 只在双通道都已就位、decision 完整产出的那一次真正执行。
    channels_merged: bool
    decision: TradeDecision
    risk_verdict: RiskVerdict
    execution: list[dict[str, Any]]
    execution_prices: dict[str, float]
    conflicts: list[dict[str, Any]]
    error: str
    signal_error: str
    strategy_error: str
    ai_error: str


def _match_block_reason(verdict: RiskVerdict, intent: Any) -> str | None:
    """从风控 reasons 里挑出属于该 symbol 的拦截原因，让被拒 intent 不再是黑盒。"""
    symbol = str(getattr(intent, "symbol", ""))
    fallback: str | None = None
    for r in verdict.reasons or []:
        text = str(r)
        if symbol and symbol not in text:
            continue
        if text.startswith("已跳过"):
            return text
        if fallback is None:
            fallback = text
    if fallback:
        return fallback
    if verdict.verdict == "rejected" and verdict.reasons:
        return " | ".join(str(r) for r in verdict.reasons[:3])
    return None


def _log_intents(db: AuditDatabase, run_id: str | None, decision_id: str | None,
                 decision: TradeDecision | None, verdict: RiskVerdict) -> None:
    """逐条 intent 落库。这是 Outcome Tracker / invalidation 的归因地基——
    intent 不落库，订单就无法连回当初的 thesis，复盘只能看赚亏。"""
    if decision is None:
        return
    passed: list[tuple[str, str, str]] = []
    if verdict is not None and verdict.verdict in ("approved", "reduced"):
        passed = [(str(i.symbol), str(getattr(i, "action", "") or ""), str(i.side))
                  for i in verdict.adjusted_intents]
    for idx, intent in enumerate(decision.intents):
        key = (str(intent.symbol), str(getattr(intent, "action", "") or ""), str(intent.side))
        if key in passed:
            passed.remove(key)
            status, blocked = "approved", None
        else:
            status = "rejected"
            blocked = _match_block_reason(verdict, intent) if verdict is not None else None
        try:
            db.log_trade_intent(
                intent_id=f"{decision_id}-{idx}" if decision_id else str(uuid.uuid4()),
                decision_id=decision_id, run_id=run_id, intent=intent,
                status=status, blocked_reason=blocked,
            )
        except Exception as e:
            db.log_risk_event(event_type="intent_log_error", severity="warn",
                              details=f"{type(e).__name__}: {e}", action_taken="skip intent log")


def build_graph(*, llm: LLMClient, model: str, db: AuditDatabase, logger: JsonlLogger,
                recorder: AgentRecorder, risk_config: dict[str, Any],
                knowledge_dir: Any, market_top_n: int = 40, market_analyze_top: int = 10,
                market_core_symbols: list[str] | None = None,
                tavily_api_key: str | None = None, etherscan_api_key: str | None = None,
                cache_dir: Any = None, enable_live_data: bool = True,
                intel_cache: Any = None, intel_cache_ttl: float = 3600,
                broker: Any = None, allow_new_positions: bool = True,
                allow_new_reason: str = "",
                control: Any = None,
                agent_llms: dict[str, tuple[LLMClient, str]] | None = None,
                portfolio_state: dict[str, Any] | None = None,
                available_cash: float | None = None,
                quota_manager: Any = None,
                signal_scanner_config: dict[str, Any] | None = None,
                execution_feedback: dict[str, Any] | None = None,
                query_budget: int = 8, execution_mode: str = "paper",
                llm_total_timeout_seconds: float = 240.0,
                llm_aux_timeout_seconds: float = 60.0,
                trading_scope: TradingScope | None = None,
                entry_readiness_provider: Callable[[], Any] | None = None) -> Any:
    # 生产服务显式注入 TradingScope；离线阶段脚本仍可只传旧参数。
    # 有 risk_config 范围时也尝试构造一个只读副本，保证直接调用 build_graph 的
    # 入口不会重新退回全市场开仓。
    scope = trading_scope
    if scope is None:
        configured_scope = risk_config.get("allowed_open_symbols")
        if configured_scope is None:
            configured_scope = market_core_symbols
        if configured_scope:
            scope = TradingScope.from_symbols(configured_scope)

    aux_timeout = max(1.0, float(llm_aux_timeout_seconds))

    def aux_deadline() -> float:
        """每个情报摘要单独拥有有限预算，不把供应商阻塞带入无限等待。"""
        return time.monotonic() + aux_timeout

    def market_node(state: AgentState) -> dict[str, Any]:
        pair = (agent_llms or {}).get("market_agent")

        def held_symbols() -> list[str]:
            """当前持仓标的，强制纳入采集，避免决策时看不到自己的仓位。

            取不到就返回空列表，不能因为读持仓失败拖垮整个情报采集。
            """
            if broker is None:
                return []
            try:
                return sorted({str(p.get("symbol")) for p in broker.positions()
                               if p.get("symbol")})
            except Exception:
                return []

        def refresh_market() -> MarketBrief:
            brief = market.run_market_agent(top_n=market_top_n, analyze_top=market_analyze_top,
                                            core_symbols=market_core_symbols,
                                            extra_symbols=held_symbols())
            if pair is not None:
                summary = summarizer.summarize(
                    "market_agent", pair[0], pair[1],
                    "你是加密市场情报员。只输出一个JSON对象："
                    "{\"summary\":\"不超过150字中文总结市场状态、关键异动和风险\","
                    "\"belief\":{\"trend\":\"up|down|range\",\"strength\":0到1,"
                    "\"confidence\":0到1,\"invalidation\":\"什么条件会证明判断错误\"}}",
                    brief.model_dump(), brief.summary, db, logger, state["run_id"],
                    deadline=aux_deadline(), purpose="intel")
                brief.summary, brief.belief = beliefs.parse_belief(
                    summary, brief.summary, beliefs.market_default(brief.regime))
            recorder.record(agent_name="market_agent", model=pair[1] if pair else model,
                            role="crypto_market_intel",
                            input_summary=f"top_n={market_top_n}, analyze_top={market_analyze_top}",
                            output=brief, run_id=state["run_id"])
            return brief

        if not enable_live_data:
            brief: MarketBrief = MarketBrief(regime="mock", top_movers=["BTCUSDT"], summary="phase0 mock市场简报")
            recorder.record(agent_name="market_agent", model=pair[1] if pair else model,
                            role="crypto_market_intel",
                            input_summary=f"top_n={market_top_n}, analyze_top={market_analyze_top}",
                            output=brief, run_id=state["run_id"])
        elif intel_cache is not None:
            brief = intel_cache.get_or_refresh("market", refresh_market, ttl_seconds=intel_cache_ttl)
        else:
            brief = refresh_market()
        return {"market": brief}

    def guardian_node(state: AgentState) -> dict[str, Any]:
        """模型链路之前先复核既有 AI 仓位；失败不影响后续情报与保护同步。"""
        try:
            portfolio = portfolio_state if isinstance(portfolio_state, dict) else (
                broker.state if broker is not None and isinstance(broker.state, dict) else {})
            decision = evaluate_positions(
                positions=list(portfolio.get("positions") or []), db=db,
                execution_mode=execution_mode,
            )
            error = ""
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            decision = TradeDecision(
                decision="no_trade", channel="merged",
                reasoning=f"Position Guardian 异常：{error}；不推断退出，保留交易所保护",
                policy_checks=[{
                    "check_id": "position_guardian", "version": 1, "mode": "enforce",
                    "status": "NOT_EVALUATED", "state": "UNKNOWN", "reason": error,
                    "evidence_at": None, "evidence": {},
                }],
            )
            db.log_risk_event(event_type="guardian_error", severity="critical",
                              details=error, action_taken="do not infer exit; continue protections")
        recorder.record(agent_name="position_guardian", model=None,
                        role="deterministic_position_supervision",
                        input_summary=f"positions={len((portfolio_state or {}).get('positions') or [])}",
                        output=decision, error=error or None, run_id=state["run_id"])
        return {"guardian_decision": decision}

    def macro_node(state: AgentState) -> dict[str, Any]:
        pair = (agent_llms or {}).get("macro_agent")

        def refresh_macro() -> MacroBrief:
            brief = macro.run_macro_agent(tavily_api_key=tavily_api_key)
            if pair is not None:
                summary = summarizer.summarize(
                    "macro_agent", pair[0], pair[1],
                    "你是宏观情报员。只输出一个JSON对象："
                    "{\"summary\":\"不超过150字中文总结美股、VIX、美债与美联储事件对加密市场的影响\","
                    "\"belief\":{\"bias\":\"risk_on|risk_off|neutral\",\"confidence\":0到1,"
                    "\"invalidation\":\"什么条件会证明判断错误\"}}",
                    brief.model_dump(), brief.summary, db, logger, state["run_id"],
                    deadline=aux_deadline(), purpose="intel")
                brief.summary, brief.belief = beliefs.parse_belief(
                    summary, brief.summary, beliefs.macro_default(brief.market_bias))
            recorder.record(agent_name="macro_agent", model=pair[1] if pair else model,
                            role="macro_intel", input_summary="yahoo+tavily",
                            output=brief, run_id=state["run_id"])
            return brief

        if not enable_live_data:
            brief: MacroBrief = MacroBrief(summary="phase0 mock宏观简报")
            recorder.record(agent_name="macro_agent", model=pair[1] if pair else model,
                            role="macro_intel", input_summary="mock",
                            output=brief, run_id=state["run_id"])
        elif intel_cache is not None:
            brief = intel_cache.get_or_refresh("macro", refresh_macro, ttl_seconds=intel_cache_ttl)
        else:
            brief = refresh_macro()
        return {"macro": brief}

    def onchain_node(state: AgentState) -> dict[str, Any]:
        pair = (agent_llms or {}).get("onchain_agent")

        def refresh_onchain() -> OnchainBrief:
            brief = onchain.run_onchain_agent(etherscan_api_key=etherscan_api_key, cache_dir=cache_dir)
            if pair is not None:
                summary = summarizer.summarize(
                    "onchain_agent", pair[0], pair[1],
                    "你是Web3链上情报员。只输出一个JSON对象："
                    "{\"summary\":\"不超过150字中文总结稳定币、TVL与趋势币种变化\","
                    "\"belief\":{\"flow_bias\":\"risk_on|risk_off|neutral\",\"confidence\":0到1,"
                    "\"invalidation\":\"什么条件会证明判断错误\"}}",
                    brief.model_dump(), brief.summary, db, logger, state["run_id"],
                    deadline=aux_deadline(), purpose="intel")
                brief.summary, brief.belief = beliefs.parse_belief(
                    summary, brief.summary, beliefs.onchain_default())
            recorder.record(agent_name="onchain_agent", model=pair[1] if pair else model,
                            role="web3_intel", input_summary="defillama+coingecko+etherscan",
                            output=brief, run_id=state["run_id"])
            return brief

        if not enable_live_data:
            brief: OnchainBrief = OnchainBrief(summary="phase0 mock链上简报")
            recorder.record(agent_name="onchain_agent", model=pair[1] if pair else model,
                            role="web3_intel", input_summary="mock",
                            output=brief, run_id=state["run_id"])
        elif intel_cache is not None:
            brief = intel_cache.get_or_refresh("onchain", refresh_onchain, ttl_seconds=intel_cache_ttl)
        else:
            brief = refresh_onchain()
        return {"onchain": brief}

    def strategy_node(state: AgentState) -> dict[str, Any]:
        # 真正触发检测在前：signal_scanner 已经完成，strategy_agent 只展示实际触发的 ticket
        refs = strategy_library.run_strategy_agent(
            knowledge_dir=knowledge_dir,
            market=state["market"],
            macro=state["macro"],
            onchain=state["onchain"],
            tickets=state.get("tickets", []),
        )
        recorder.record(agent_name="strategy_agent", model=None, role="strategy_library",
                        input_summary=f"tickets={len(state.get('tickets', []))} regime={state['market'].regime}",
                        output=refs, run_id=state["run_id"])
        return {"strategy": refs}

    def signal_scanner_node(state: AgentState) -> dict[str, Any]:
        cfg = signal_scanner_config or {}
        tickets: list[dict[str, Any]] = []
        signal_error = ""
        try:
            tickets = run_signal_scan(
                knowledge_dir=knowledge_dir,
                strategy_lab_src=str(cfg.get("strategy_lab_src", "")),
                state_file=Path(cfg.get("signal_state_file", "data/signal_scanner_state.json")),
                db=db,
                kline_limit=int(cfg.get("kline_limit", 500)),
                ticket_valid_bars=int(cfg.get("ticket_valid_bars", 2)),
                scan_symbols=cfg.get("scan_pool"),
                universal_cards=bool(cfg.get("universal_cards", False)),
                max_scan_symbols_per_cycle=int(cfg.get("max_scan_symbols_per_cycle", 8)),
            )
        except Exception as e:
            signal_error = f"{type(e).__name__}: {e}"
            db.log_risk_event(event_type="signal_scanner_error", severity="warn",
                              details=signal_error, action_taken="本轮策略通道无信号")
        recorder.record(agent_name="signal_scanner", model=None, role="deterministic_signal_engine",
                        input_summary="scan final strategy cards",
                        output={"tickets": tickets, "error": signal_error},
                        run_id=state["run_id"])
        return {"tickets": tickets, "signal_error": signal_error}

    def strategy_trader_node(state: AgentState) -> dict[str, Any]:
        cfg = signal_scanner_config or {}
        try:
            decision = strategy_trader.run_strategy_trader(
                tickets=state.get("tickets", []),
                portfolio=portfolio_state if portfolio_state is not None else (broker.state if broker is not None else None),
                prices=dict(state.get("market").last_prices) if state.get("market") else {},
                min_notional=float(cfg.get("strategy_min_notional_usdt", 20.0) or 0.0),
                max_notional=float(cfg.get("strategy_max_notional_usdt", 120.0) or 0.0),
                max_leverage=int(cfg.get("strategy_max_leverage", 20) or 1),
                risk_per_trade=float(cfg.get("strategy_risk_per_trade", 0.02) or 0.02),
                channel_status=risk_config.get("channel_status") or {},
                db=db,
                allowed_symbols=list(scope.symbols) if scope is not None else None,
            )
            strategy_error = ""
        except Exception as e:
            decision = ChannelDecision(
                channel="strategy", decision="no_trade",
                reasoning=f"策略交易AI异常，本轮策略通道不交易: {type(e).__name__}: {e}",
            )
            strategy_error = f"{type(e).__name__}: {e}"
        recorder.record(agent_name="strategy_trader", model=None, role="strategy_execution",
                        input_summary=f"tickets={len(state.get('tickets', []))}",
                        output=decision, error=strategy_error or None,
                        run_id=state["run_id"])
        return {"strategy_decision": decision, "strategy_error": strategy_error}

    def ai_trader_node(state: AgentState) -> dict[str, Any]:
        try:
            ai_portfolio = portfolio_state if portfolio_state is not None else (
                broker.state if broker is not None else None)
            # 双层可交易集合：核心主流 + 异动山寨。异动币被检测到后进入本轮可交易池。
            anomaly_symbols = (
                orchestrator.extract_anomaly_symbols(
                    state["market"],
                    allowed_symbols=scope.symbols if scope is not None else None,
                )
                if state.get("market") else []
            )
            if scope is not None:
                # 异动只是在白名单内改变本轮排序/关注度，不能扩大业务开仓集合。
                tradeable = scope.candidate_symbols(
                    list(scope.symbols) + anomaly_symbols,
                )
            else:
                tradeable = list(orchestrator.AI_CORE_SYMBOLS)
                for s in anomaly_symbols:
                    if s not in tradeable:
                        tradeable.append(s)
            # 同一份 ActionSpace 同时喂给：动态 schema 生成 + submit_decision 的 Validator 回环。
            # risk_node 里的运行时闸门用同一谓词再复核一次。
            action_space = ActionSpace.from_context(
                risk_config=risk_config,
                allow_new_positions=allow_new_positions,
                allow_new_reason=allow_new_reason,
                portfolio=ai_portfolio if isinstance(ai_portfolio, dict) else None,
                channel="ai",
                tradeable_symbols=tradeable,
            )
            if not action_space.allow_new:
                db.log_risk_event(event_type="action_space_no_open", severity="info",
                                  details=action_space.allow_new_reason or "不允许新开仓",
                                  action_taken="submit_decision schema 已剪掉 open")
            raw = orchestrator.run_orchestrator(
                llm=llm,
                model=model,
                recorder=recorder,
                db=db,
                logger=logger,
                run_id=state["run_id"],
                market=state["market"],
                macro=state["macro"],
                onchain=state["onchain"],
                strategy=state["strategy"],
                risk_envelope=risk_config,
                portfolio=ai_portfolio,
                trading_quota=quota_manager.status() if quota_manager is not None else None,
                supported_symbols=tradeable,
                execution_feedback=execution_feedback,
                action_space=action_space,
                prices=dict(state["market"].last_prices) if state.get("market") else {},
                query_budget=query_budget,
                total_timeout_seconds=llm_total_timeout_seconds,
            )
            decision = ChannelDecision(
                channel="ai", decision=raw.decision, intents=raw.intents,
                reasoning=raw.reasoning, strategy_refs=raw.strategy_refs,
                policy_checks=raw.policy_checks,
            )
            # AI 通道产出的 intent 必须归 ai 通道：LLM 自报 order_source="strategy"
            # 可以绕过 AI 熔断（-30%）与 AI 杠杆上限。以通道归属为准强制改写，
            # 不信任模型自报字段（risk 节点会再做一次一致性复核）。
            rewritten = 0
            for idx, it in enumerate(decision.intents):
                if str(getattr(it, "order_source", "ai") or "ai") != "ai":
                    decision.intents[idx] = it.model_copy(update={"order_source": "ai"})
                    rewritten += 1
            if rewritten:
                decision.reasoning = (decision.reasoning or "") + f" [系统修正: {rewritten} 笔 intent 的 order_source 被强制改写为 ai]"
            ai_error = ""
        except Exception as e:
            decision = ChannelDecision(
                channel="ai", decision="no_trade",
                reasoning=f"AI自决策Agent异常，本轮AI通道不交易: {type(e).__name__}: {e}",
            )
            ai_error = f"{type(e).__name__}: {e}"
        recorder.record(agent_name="ai_trader", model=model, role="discretionary_trader",
                        input_summary=f"strategy_refs={state.get('strategy')}",
                        output=decision, error=ai_error or None,
                        run_id=state["run_id"])
        return {"ai_decision": decision, "ai_error": ai_error}

    def merge_node(state: AgentState) -> dict[str, Any]:
        # 会合屏障：仅当双通道决策都已就位才真正产出 decision。两分支跳数不同，
        # merge 会先被短分支（strategy_trader）触发一次、再被长分支（ai_trader）
        # 触发一次；第一次触发时下游 risk/execution 因 channels_merged 未置位而空转，
        # 避免「先按半个决策执行、再按完整决策执行一遍」的双执行。
        if state.get("strategy_decision") is None or state.get("ai_decision") is None:
            return {}
        strategy_decision = state.get("strategy_decision") or ChannelDecision(
            channel="strategy", decision="no_trade", reasoning="策略通道无决策")
        ai_decision = state.get("ai_decision") or ChannelDecision(
            channel="ai", decision="no_trade", reasoning="AI通道无决策")
        guardian_decision = state.get("guardian_decision") or TradeDecision(
            decision="no_trade", channel="merged", reasoning="Guardian 未产生任务")
        # Guardian 退出优先级最高。相同 symbol 的新开、调整或重复退出全部丢弃，
        # 这样模型断链时退出仍可执行，模型正常时也不能覆盖原论点失效事实。
        intents = list(guardian_decision.intents)
        # 幂等 clientOrderId：decision_id + intent index，重启/重试不会重复下单
        decision_id = state.get("decision_id") or ""
        base_cid = "mta-" + str(decision_id).replace("-", "")[:24] if decision_id else ""
        for idx, intent in enumerate(intents):
            if base_cid and not getattr(intent, "client_order_id", None):
                intents[idx] = intent.model_copy(update={"client_order_id": f"{base_cid}-{idx}"})
        guardian_symbols = {it.symbol for it in guardian_decision.intents}
        protected = set(guardian_symbols)
        conflicts: list[dict[str, Any]] = []
        for intent in strategy_decision.intents:
            if intent.symbol in protected:
                conflict = {"symbol": intent.symbol, "source": "strategy",
                            "reason": "Guardian退出优先，已丢弃策略通道同标的意图"}
                conflicts.append(conflict)
                db.log_decision_conflict(
                    run_id=state.get("run_id"), decision_id=state.get("decision_id"),
                    symbol=conflict["symbol"], source=conflict["source"], reason=conflict["reason"])
                continue
            intents.append(intent)
            protected.add(intent.symbol)
        for intent in ai_decision.intents:
            if intent.symbol in protected:
                conflict = {"symbol": intent.symbol, "source": "ai",
                            "reason": "AI单与策略单冲突，已丢弃AI单"}
                conflicts.append(conflict)
                db.log_decision_conflict(
                    run_id=state.get("run_id"), decision_id=state.get("decision_id"),
                    symbol=conflict["symbol"], source=conflict["source"],
                    reason=conflict["reason"])
                continue
            if base_cid and not getattr(intent, "client_order_id", None):
                intent = intent.model_copy(update={"client_order_id": f"{base_cid}-{len(intents)}"})
            intents.append(intent)

        # Guardian 已自带跨轮稳定 ID；其余通道在这里统一补齐本轮幂等 ID。
        for idx, intent in enumerate(intents):
            if base_cid and not getattr(intent, "client_order_id", None):
                intents[idx] = intent.model_copy(update={"client_order_id": f"{base_cid}-{idx}"})

        if not intents:
            merged = TradeDecision(
                decision="no_trade", channel="merged",
                reasoning=(f"[Guardian] {guardian_decision.reasoning} | "
                           f"[策略] {strategy_decision.reasoning} | [AI] {ai_decision.reasoning}"),
                strategy_refs=list(dict.fromkeys(strategy_decision.strategy_refs + ai_decision.strategy_refs)),
                policy_checks=list(guardian_decision.policy_checks) + list(ai_decision.policy_checks),
            )
        else:
            actions = {getattr(i, "action", "open") for i in intents}
            if "open" in actions:
                decision_type = "open"
            elif "close" in actions or "reduce" in actions:
                decision_type = "close"
            else:
                decision_type = "adjust"
            merged = TradeDecision(
                decision=decision_type, channel="merged", intents=intents,
                reasoning=(f"[Guardian] {guardian_decision.reasoning} | "
                           f"[策略] {strategy_decision.reasoning} | [AI] {ai_decision.reasoning}"),
                strategy_refs=list(dict.fromkeys(strategy_decision.strategy_refs + ai_decision.strategy_refs)),
                policy_checks=list(guardian_decision.policy_checks) + list(ai_decision.policy_checks),
            )
        return {"decision": merged, "conflicts": conflicts, "channels_merged": True}

    def route_after_merge(state: AgentState) -> str:
        """未完成会合的分支就地结束，避免它继续调度 risk/execution。"""
        return "pipeline" if state.get("channels_merged") else "end"

    def pipeline_node(state: AgentState) -> dict[str, Any]:
        decision = state.get("decision") or TradeDecision(
            decision="no_trade", channel="merged", reasoning="汇合节点未产出决策")
        return {"decision": authorize_decision(
            decision, db=db, run_id=state.get("run_id"),
        )}

    def risk_node(state: AgentState) -> dict[str, Any]:
        # 会合屏障：未双通道合并完成前不评估（对应 merge 的首次空转触发）
        if not state.get("channels_merged"):
            return {}
        decision = state.get("decision")
        if decision is None:
            decision = TradeDecision(decision="no_trade", reasoning="双通道未产出决策")
        prices = dict(state.get("market").last_prices) if state.get("market") else {}
        # 如果某个交易对不在市场快照里，实时补一个最新价，避免误拒
        if decision is not None and decision.intents:
            missing = [it.symbol for it in decision.intents if it.symbol not in prices]
            if missing:
                try:
                    prices.update(get_live_prices(missing))
                except Exception:
                    pass
        cash = available_cash if available_cash is not None else (
            float(broker.state.get("cash", 0.0)) if broker is not None else None)
        verdict = risk.evaluate_risk(decision, risk_config, db, recorder,
                                        state["run_id"], state.get("decision_id"),
                                        allow_new_positions, prices=prices,
                                        available_cash=cash,
                                        portfolio=portfolio_state if isinstance(portfolio_state, dict) else None)
        _log_intents(db, state.get("run_id"), state.get("decision_id"), decision, verdict)
        trace = list(decision.pipeline_trace)
        for item in trace:
            if item.get("stage") == "risk_guard":
                item["status"] = "PASS" if verdict.verdict in ("approved", "reduced") else "BLOCK"
        checked_decision = decision.model_copy(update={"pipeline_trace": trace})
        authorization_context = None
        if entry_readiness_provider is not None and any(
                item.action == "open" for item in (verdict.adjusted_intents or decision.intents)):
            try:
                gate = entry_readiness_provider()
                if getattr(gate, "new_entry_allowed", False):
                    authorization_context = gate.authorization_context(
                        decision_id=state.get("decision_id"))
            except Exception as exc:
                # 不能在授权上下文不可读时继续签发 open；退出仍由原路径保留。
                db.log_risk_event(
                    event_type="entry_readiness_unavailable", severity="critical",
                    details=f"{type(exc).__name__}: {exc}", action_taken="block open authorization")
        checked_decision = sign_execution(
            checked_decision, verdict, getattr(broker, "_pipeline_secret", None),
            authorization_context)
        return {"decision": checked_decision,
                "risk_verdict": verdict, "execution_prices": prices}

    def execution_node(state: AgentState) -> dict[str, Any]:
        # 会合屏障：未双通道合并完成前不执行，防止决策链每轮跑两遍导致的重复下单风险。
        if not state.get("channels_merged"):
            return {}
        decision = state.get("decision")
        verdict = state.get("risk_verdict")
        receipts: list[dict[str, Any]] = []
        # 即使本轮 no_trade 也调用 broker：纸面 broker 需要借此处理待成交限价单。
        exec_decision = decision or TradeDecision(decision="no_trade", channel="merged")
        exec_verdict = verdict or RiskVerdict(verdict="approved", reasons=[], adjusted_intents=[])
        if broker is None:
            db.log_risk_event(event_type="paper_missing", severity="warn",
                              details="未配置纸面执行器", action_taken="skip")
        elif exec_verdict.verdict in ("approved", "reduced"):
            def _drop_opens(intents: list[Any]) -> tuple[list[Any], int]:
                kept: list[Any] = []
                dropped = 0
                for it in intents:
                    action = str(getattr(it, "action", "") or exec_decision.decision or "")
                    if action == "open":
                        dropped += 1
                    else:
                        kept.append(it)
                return kept, dropped

            def _decision_for(intents: list[Any]) -> str:
                if any(getattr(it, "action", "") == "open" for it in intents):
                    return "open"
                if any(getattr(it, "action", "") in {"close", "reduce"} for it in intents):
                    return "close"
                if any(getattr(it, "action", "") == "adjust" for it in intents):
                    return "adjust"
                return "no_trade"

            # LLM 运行期间可能发生 cooldown、镜像失配、UNKNOWN 或控制状态变化。
            # 执行前重新消费同一判定器；readiness 不可读也按 fail closed 处理。
            if entry_readiness_provider is not None and any(
                    item.action == "open"
                    for item in (exec_verdict.adjusted_intents or exec_decision.intents)):
                try:
                    gate = entry_readiness_provider()
                    gate_allowed = bool(getattr(gate, "new_entry_allowed", False))
                    gate_reasons = list(getattr(gate, "blocking_reasons", ()) or ())
                except Exception as exc:
                    gate_allowed = False
                    gate_reasons = [f"READINESS_UNAVAILABLE:{type(exc).__name__}"]
                if not gate_allowed:
                    new_adj, dropped_adj = _drop_opens(list(exec_verdict.adjusted_intents))
                    new_intents, dropped_raw = _drop_opens(list(exec_decision.intents))
                    dropped = dropped_adj + dropped_raw
                    if dropped:
                        reason = "；".join(gate_reasons) or "READINESS_BLOCKED"
                        exec_verdict = exec_verdict.model_copy(update={
                            "adjusted_intents": new_adj,
                            "reasons": list(exec_verdict.reasons) + [
                                f"统一就绪闸门执行前拦截 {dropped} 笔开仓 intent：{reason}"],
                        })
                        exec_decision = exec_decision.model_copy(update={
                            "intents": new_intents,
                            "decision": _decision_for(new_intents),
                        })
                        db.log_risk_event(
                            event_type="entry_readiness_block_open", severity="warn",
                            details=f"执行前拦截 {dropped} 笔开仓 intent：{reason}",
                            action_taken="skip open intents; preserve exits")
            # 决策周期可能长达数分钟，期间用户可能已触发 emergency/pause。
            # 执行前重读控制状态：非 normal 时丢弃所有 open intent（平仓/减仓仍放行），
            # 避免「决策时允许、执行时已被禁止」的在途开仓。
            cur_control = control.state if control is not None else "normal"
            if cur_control != "normal" and exec_decision.decision != "no_trade":
                new_adj, dropped_adj = _drop_opens(list(exec_verdict.adjusted_intents))
                new_intents, dropped_raw = _drop_opens(list(exec_decision.intents))
                dropped = dropped_adj + dropped_raw
                if dropped:
                    exec_verdict = exec_verdict.model_copy(update={
                        "adjusted_intents": new_adj,
                        "reasons": list(exec_verdict.reasons) + [
                            f"控制状态={cur_control}，执行前拦截 {dropped} 笔开仓 intent"],
                    })
                    exec_decision = exec_decision.model_copy(update={
                        "intents": new_intents,
                        "decision": _decision_for(new_intents),
                    })
                    db.log_risk_event(event_type="control_block_open", severity="warn",
                                      details=f"控制状态为 {cur_control}，执行前丢弃 {dropped} 笔开仓 intent",
                                      action_taken="skip open intents")
            exec_prices = dict(state.get("execution_prices") or {})
            if not exec_prices and state.get("market"):
                exec_prices = dict(state["market"].last_prices or {})
            try:
                receipts = broker.execute(
                    exec_decision, exec_verdict, exec_prices, risk_config, db
                )
            except Exception as e:
                db.log_risk_event(event_type="paper_error", severity="error",
                                  details=f"{type(e).__name__}: {e}", action_taken="skip")
        recorder.record(agent_name="execution_agent", model=None, role="exchange_execution",
                        input_summary=exec_decision.model_dump_json()[:300] if decision else "no decision",
                        output={"receipts": receipts}, run_id=state["run_id"])
        return {"execution": receipts}

    g = StateGraph(AgentState)
    g.add_node("market", market_node)
    g.add_node("guardian", guardian_node)
    g.add_node("macro", macro_node)
    g.add_node("onchain", onchain_node)
    g.add_node("strategy", strategy_node)
    g.add_node("signal_scanner", signal_scanner_node)
    g.add_node("strategy_trader", strategy_trader_node)
    g.add_node("ai_trader", ai_trader_node)
    g.add_node("merge", merge_node)
    g.add_node("pipeline", pipeline_node)
    g.add_node("risk", risk_node)
    g.add_node("execution", execution_node)
    g.add_edge(START, "guardian")
    g.add_edge("guardian", "market")
    g.add_edge("market", "macro")
    g.add_edge("macro", "onchain")
    g.add_edge("onchain", "signal_scanner")
    # 先做真实触发检测，再做策略匹配与双通道决策
    g.add_edge("signal_scanner", "strategy")
    g.add_edge("signal_scanner", "strategy_trader")
    g.add_edge("strategy", "ai_trader")
    g.add_edge("strategy_trader", "merge")
    g.add_edge("ai_trader", "merge")
    g.add_conditional_edges("merge", route_after_merge, {"pipeline": "pipeline", "end": END})
    g.add_edge("pipeline", "risk")
    g.add_edge("risk", "execution")
    g.add_edge("execution", END)
    return g.compile()
