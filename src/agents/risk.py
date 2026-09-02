"""风控 Agent：确定性规则。阶段 0 只做静态参数检查，不查真实账户。"""
from __future__ import annotations

import json
import uuid

from audit.database import AuditDatabase
from core.action_space import NEW_POSITION_ACTION, ActionSpace
from core.agent_recorder import AgentRecorder

from .models import RiskVerdict, TradeDecision


def evaluate_risk(decision: TradeDecision, risk_config: dict[str, Any],
                  db: AuditDatabase, recorder: AgentRecorder,
                  run_id: str, decision_id: str | None = None,
                  allow_new_positions: bool = True,
                  prices: dict[str, float] | None = None,
                  available_cash: float | None = None,
                  portfolio: dict[str, Any] | None = None) -> RiskVerdict:
    reasons: list[str] = []
    adjusted: list[Any] = []
    failures: list[str] = []
    prices = prices or {}

    if decision.decision == "no_trade":
        verdict = RiskVerdict(verdict="approved", reasons=["no_trade，无需风控拦截。"])
    else:
        max_leverage = int(risk_config.get("max_leverage", 20))
        max_notional = float(risk_config.get("max_position_notional", 100000))
        max_margin_usage = float(risk_config.get("max_margin_usage", 0.80))
        max_slots = int(risk_config.get("max_open_positions", 3))
        strong_threshold = float(risk_config.get("strong_confidence_threshold", 0.75))
        strong_max_leverage = int(risk_config.get("strong_max_leverage", 50))
        strong_max_notional = float(risk_config.get("strong_max_position_notional", 200000))
        strong_max_margin_usage = float(risk_config.get("strong_max_margin_usage", 0.95))
        max_risk_per_trade = float(risk_config.get("max_risk_per_trade", 0.02))
        ai_max_leverage = int(risk_config.get("ai_max_leverage", 20))
        channel_status = risk_config.get("channel_status") or {}
        needs_reduction = False

        if len(decision.intents) > max_slots:
            return _reject(decision, run_id, db, recorder,
                           f"同时下单数 {len(decision.intents)} 超过 max_open_positions={max_slots}", decision_id)

        # 总持仓数强制（结构性修复）：此前只约束单轮批次数量，不统计账户现有
        # 持仓总数（含 strategy 通道），LLM 每轮开 1 仓即可无限累积，提示词里
        # 的仓位上限纯属软约束。现在：现有总持仓 + 本批真正新增的 open（不含
        # 对已持仓 symbol 的加仓）必须 ≤ max_open_positions。
        # 语义：同批先平后开不抵扣——被上限卡住时先执行平仓，下一轮再开。
        held_symbols: set[str] = set()
        if isinstance(portfolio, dict):
            for p in (portfolio.get("positions") or []):
                if isinstance(p, dict) and p.get("symbol"):
                    held_symbols.add(str(p["symbol"]).strip().upper())
        new_open_symbols = {
            str(getattr(it, "symbol", "") or "").strip().upper()
            for it in decision.intents
            if str(getattr(it, "action", None) or decision.decision) == NEW_POSITION_ACTION
            and str(getattr(it, "symbol", "") or "").strip().upper() not in held_symbols
        }
        new_open_symbols.discard("")
        if held_symbols and len(held_symbols) + len(new_open_symbols) > max_slots:
            return _reject(
                decision, run_id, db, recorder,
                f"总持仓 {len(held_symbols)} 个 ({', '.join(sorted(held_symbols))}) + "
                f"本批新开 {len(new_open_symbols)} 个 ({', '.join(sorted(new_open_symbols))}) "
                f"超过 max_open_positions={max_slots}；先平仓或减仓，下一轮再开新仓",
                decision_id)

        # 所有布尔谓词都来自 ActionSpace —— 与动态 schema、submit_decision 的 Validator
        # 回环共用同一份规则。改规则只改 core/action_space.py，三处同时生效。
        # 这里按通道各建一份，因为 allow_new 是通道级的（AI 熔断不影响策略通道）。
        spaces = {
            "ai": ActionSpace.from_context(
                risk_config=risk_config, allow_new_positions=allow_new_positions,
                portfolio=portfolio if isinstance(portfolio, dict) else None, channel="ai"),
            "strategy": ActionSpace.from_context(
                risk_config=risk_config, allow_new_positions=allow_new_positions,
                portfolio=portfolio if isinstance(portfolio, dict) else None, channel="strategy"),
        }

        for intent in decision.intents:
            action = str(getattr(intent, "action", None) or decision.decision)
            source = str(getattr(intent, "order_source", "ai") or "ai")
            space = spaces.get(source) or spaces["ai"]

            issues = space.check_intent(intent, prices)
            if issues:
                failures.append(f"{intent.symbol} " + "；".join(issues))
                continue

            if action == "adjust":
                # adjust 不是“免检通道”：SL/TP 方向错（如 LONG 的止损高于现价）
                # 会挂出立即触发的 STOP，先撤旧保护再挂错单，市价平掉仓位。
                # 必须与 open 同样的方向谓词校验，以 mark/最新价为参照。
                price = prices.get(intent.symbol)
                if not price:
                    failures.append(f"{intent.symbol} 缺少最新价格，无法校验 adjust")
                    continue
                has_sl = intent.stop_loss is not None and float(intent.stop_loss) > 0
                has_tp = intent.take_profit is not None and float(intent.take_profit) > 0
                if not has_sl and not has_tp:
                    failures.append(f"{intent.symbol} adjust 单必须至少提供 SL/TP 之一")
                    continue
                sl_v = float(intent.stop_loss) if has_sl else None
                tp_v = float(intent.take_profit) if has_tp else None
                if intent.side == "LONG":
                    # LONG: 止损在现价下方、止盈在上方；提供哪个校验哪个
                    if sl_v is not None and sl_v >= float(price):
                        failures.append(f"{intent.symbol} LONG adjust 止损 {sl_v} 必须低于现价 {price}")
                        continue
                    if tp_v is not None and tp_v <= float(price):
                        failures.append(f"{intent.symbol} LONG adjust 止盈 {tp_v} 必须高于现价 {price}")
                        continue
                else:
                    if sl_v is not None and sl_v <= float(price):
                        failures.append(f"{intent.symbol} SHORT adjust 止损 {sl_v} 必须高于现价 {price}")
                        continue
                    if tp_v is not None and tp_v >= float(price):
                        failures.append(f"{intent.symbol} SHORT adjust 止盈 {tp_v} 必须低于现价 {price}")
                        continue
                reasons.append(f"{intent.symbol} 通过(调整单)：stop_loss={intent.stop_loss}, take_profit={intent.take_profit}")
                adjusted.append(intent)
                continue

            if action in ("close", "reduce"):
                reasons.append(f"{intent.symbol} 通过({action}单)：数量={intent.qty}，平仓不受开仓额度校验限制")
                adjusted.append(intent)
                continue

            downgrade_note = None
            strong = float(getattr(intent, "confidence", 0.0) or 0.0) >= strong_threshold
            if strong and (intent.stop_loss is None or float(intent.stop_loss) <= 0):
                strong = False
                downgrade_note = f"{intent.symbol} 强信心未设置止损，已降级常规档"
            else:
                downgrade_note = None

            if strong:
                cur_max_leverage = strong_max_leverage
                cur_max_notional = strong_max_notional
                cur_max_margin_usage = strong_max_margin_usage
                tier_label = "强信心档"
            else:
                cur_max_leverage = max_leverage
                cur_max_notional = max_notional
                cur_max_margin_usage = max_margin_usage
                tier_label = "常规档" + ("(强信心未带止损)" if downgrade_note else "")

            if str(getattr(intent, "order_source", "ai") or "ai") == "ai":
                cur_max_leverage = min(cur_max_leverage, ai_max_leverage)

            if intent.leverage > cur_max_leverage:
                failures.append(f"{intent.symbol} leverage={intent.leverage} 超过{tier_label}上限 {cur_max_leverage}")
                continue

            price = prices.get(intent.symbol)
            if not price:
                failures.append(f"{intent.symbol} 缺少最新价格，无法校验")
                continue

            supported_symbols = risk_config.get("supported_symbols")
            if supported_symbols and intent.symbol not in supported_symbols:
                failures.append(f"{intent.symbol} 不在当前交易所支持的 symbol 列表内，拒绝开仓")
                continue

            order_source = str(getattr(intent, "order_source", "ai") or "ai")
            # 通道一致性复核（防绕过）：ai_trader_node 已强制改写，这里再按
            # 决策通道兜底复核一次。schema 允许模型自报 order_source，
            # 若直接走 strategy_trader/merge 之外的路徟拼装 decision，仍可能报 strategy。
            if str(getattr(decision, "channel", "merged") or "merged") == "ai" and order_source != "ai":
                failures.append(f"{intent.symbol} AI 通道决策不允许自报 order_source={order_source}，已拒绝")
                continue
            ch = channel_status.get(order_source)
            if isinstance(ch, dict) and not ch.get("allow_new", True):
                halt_note = "；AI通道已当日熔断" if ch.get("halted") else ""
                failures.append(f"{intent.symbol} {order_source} 通道当前禁止开新仓"
                                f"（margin_used={ch.get('margin_used', 0):.2f} > "
                                f"cap={ch.get('margin_cap', 0):.2f}{halt_note}）")
                continue

            entry_ref = float(intent.limit_price) if intent.order_type == "LIMIT" and intent.limit_price else float(price)
            if intent.order_type == "LIMIT":
                if not intent.limit_price or float(intent.limit_price) <= 0:
                    failures.append(f"{intent.symbol} 限价单必须设置有效 limit_price")
                    continue
                if intent.order_source == "ai" and not getattr(intent, "limit_expires_at", None):
                    failures.append(f"{intent.symbol} AI限价单必须设置 limit_expires_at")
                    continue

            if intent.stop_loss is None or float(intent.stop_loss) <= 0:
                failures.append(f"{intent.symbol} 新开仓必须设置初始止损 stop_loss>0")
                continue
            if intent.take_profit is None or float(intent.take_profit) <= 0:
                failures.append(f"{intent.symbol} 新开仓必须设置初始止盈 take_profit>0")
                continue

            sl = float(intent.stop_loss)
            tp = float(intent.take_profit)
            if intent.side == "LONG":
                risk_per_unit = entry_ref - sl
                if not (sl < entry_ref < tp):
                    failures.append(f"{intent.symbol} LONG 止损/止盈方向错误：需 stop_loss<入场价<take_profit，当前 SL={sl}, entry_ref={entry_ref}, TP={tp}")
                    continue
            else:
                risk_per_unit = sl - entry_ref
                if not (tp < entry_ref < sl):
                    failures.append(f"{intent.symbol} SHORT 止损/止盈方向错误：需 take_profit<入场价<stop_loss，当前 TP={tp}, entry_ref={entry_ref}, SL={sl}")
                    continue
            if risk_per_unit <= 0:
                failures.append(f"{intent.symbol} 止损方向错误：{intent.side} 入场参考价 {entry_ref}，止损 {intent.stop_loss}")
                continue
            risk_amount = float(intent.qty) * risk_per_unit
            if available_cash is not None and available_cash > 0:
                risk_equity = available_cash
                if isinstance(ch, dict) and float(ch.get("equity") or 0.0) > 0:
                    risk_equity = min(available_cash, float(ch["equity"]))
                max_risk_amount = risk_equity * max_risk_per_trade
                if risk_amount > max_risk_amount:
                    allowed_qty = max_risk_amount / risk_per_unit
                    if allowed_qty <= 0:
                        failures.append(f"{intent.symbol} 单笔风险 {risk_amount:.2f} 超过上限 {max_risk_amount:.2f}，且无法自动缩减")
                        continue
                    reduced = intent.model_copy(update={"qty": round(allowed_qty, 8)})
                    reasons.append(f"{intent.symbol} 单笔风险 {risk_amount:.2f} > 上限 {max_risk_amount:.2f}，数量自动从 {intent.qty} 缩减到 {reduced.qty}")
                    intent = reduced
                    needs_reduction = True

            notional = float(intent.qty) * (float(intent.limit_price) if intent.order_type == "LIMIT" and intent.limit_price else float(price))
            if notional > cur_max_notional:
                failures.append(f"{intent.symbol} 名义金额 {notional:.2f} 超过{tier_label}上限 {cur_max_notional}")
                continue
            margin = notional / int(intent.leverage)
            if available_cash is not None and margin > available_cash * cur_max_margin_usage:
                failures.append(f"{intent.symbol} 保证金 {margin:.2f} 超过{tier_label}可用上限 {available_cash * cur_max_margin_usage:.2f}")
                continue
            if downgrade_note:
                reasons.append(downgrade_note)
            reasons.append(f"{intent.symbol} 通过({tier_label}, action={action}, confidence={intent.confidence})：价格={price}，名义={notional:.2f}，保证金={margin:.2f}，leverage={intent.leverage}")
            adjusted.append(intent)

        if adjusted:
            if failures:
                reasons.extend(f"已跳过: {f}" for f in failures)
            verdict = RiskVerdict(
                verdict="reduced" if needs_reduction else "approved",
                reasons=reasons,
                adjusted_intents=adjusted,
            )
        elif failures:
            return _reject(decision, run_id, db, recorder,
                           "；".join(failures), decision_id)
        else:
            verdict = RiskVerdict(verdict="approved", reasons=["空批次，通过。"], adjusted_intents=[])

    recorder.record(
        agent_name="risk_agent",
        model=None,
        role="risk_guardian",
        input_summary=decision.model_dump_json()[:500],
        output=verdict,
        run_id=run_id,
    )
    db.log_risk_verdict(
        verdict_id=str(uuid.uuid4()),
        decision_id=decision_id,
        verdict=verdict.verdict,
        reasons=json.dumps(verdict.reasons, ensure_ascii=False),
        raw_json=verdict.model_dump_json(),
    )
    return verdict


def _reject(decision: TradeDecision, run_id: str, db: AuditDatabase,
            recorder: AgentRecorder, reason: str, decision_id: str | None = None) -> RiskVerdict:
    verdict = RiskVerdict(verdict="rejected", reasons=[reason], adjusted_intents=[])
    recorder.record(
        agent_name="risk_agent",
        model=None,
        role="risk_guardian",
        input_summary=decision.model_dump_json()[:500],
        output=verdict,
        run_id=run_id,
    )
    db.log_risk_verdict(
        verdict_id=str(uuid.uuid4()),
        decision_id=decision_id,
        verdict=verdict.verdict,
        reasons=json.dumps(verdict.reasons, ensure_ascii=False),
        raw_json=verdict.model_dump_json(),
    )
    db.log_risk_event(event_type="risk_rejected", severity="warn", details=reason,
                      action_taken="本轮不执行交易")
    return verdict
