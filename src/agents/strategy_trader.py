"""策略交易 AI（P1 确定性版）。

P1 先做安全的最小闭环：
- 只执行 SignalScanner 生成、且仍在 entry_zone 内的 ticket；
- 方向/SL/TP/品种/周期全部来自策略卡，不允许修改；
- 同 symbol 通道互斥：已有任何仓位则跳过；
- 每张策略同时最多 1 个仓位。

后续 P3 再让 LLM 在 ticket 有效期内做分钟级进场择时，并用影子基准评估。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, Iterable

from audit.database import AuditDatabase
from core.signal_ticket import (
    TICKET_DETAIL_EXPIRED,
    ticket_order_id,
    ticket_skip_detail,
)
from core.strategy_funnel import (
    ALREADY_CONSUMED,
    CONSUMED,
    DETAIL_AUDIT_UNAVAILABLE,
    DETAIL_CHANNEL_BUDGET_ZERO,
    DETAIL_LEDGER_SAVE_DEFERRED,
    DETAIL_NO_LATEST_PRICE,
    DETAIL_NO_TICKETS_AVAILABLE,
    DETAIL_PRICE_OUTSIDE_ENTRY_ZONE,
    DETAIL_STRATEGY_ALREADY_HELD,
    DETAIL_STRATEGY_TRADER_EXCEPTION,
    DETAIL_SYMBOL_ALREADY_HELD,
    DETAIL_SYMBOL_NOT_ALLOWED,
    DETAIL_TICKET_ALREADY_CONSUMED,
    DETAIL_TICKET_LEDGER_UNAVAILABLE,
    ORIGIN_EXECUTE,
    ScanFunnel,
    Stage,
    TicketLedger,
    detail_reason,
)
from data.market_query import get_live_prices

from .models import ChannelDecision, TradeIntent


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _price_in_zone(price: float, ticket: dict[str, Any]) -> bool:
    return float(ticket["entry_zone_low"]) <= float(price) <= float(ticket["entry_zone_high"])


def _not_expired(ticket: dict[str, Any], now: datetime | None = None) -> bool:
    now = now or utc_now()
    try:
        expires = datetime.fromisoformat(str(ticket["expires_at"]).replace("Z", "+00:00"))
        return expires.tzinfo is not None and expires > now
    except (KeyError, TypeError, ValueError):
        return False


def build_intent_from_ticket(ticket: dict[str, Any], price: float,
                             notional: float, leverage: int,
                             holding_minutes: int | None = None) -> TradeIntent:
    qty = max(0.0, float(notional) / float(price)) if price > 0 else 0.0
    side = str(ticket["side"]).upper()
    sl_pct = float(ticket["stop_loss_pct"])
    tp_pct = float(ticket["take_profit_pct"])
    if side == "LONG":
        sl = price * (1.0 - sl_pct)
        tp = price * (1.0 + tp_pct)
    else:
        sl = price * (1.0 + sl_pct)
        tp = price * (1.0 - tp_pct)
    return TradeIntent(
        symbol=str(ticket["symbol"]).strip().upper(),
        action="open",
        side="LONG" if side == "LONG" else "SHORT",
        qty=round(qty, 8),
        leverage=int(leverage),
        order_type="MARKET",
        stop_loss=round(sl, 8),
        take_profit=round(tp, 8),
        expected_holding_minutes=int(holding_minutes or 0),
        trade_level="hourly" if str(ticket.get("timeframe", "1h")) not in ("30m",) else "short",
        confidence=0.6,
        reasoning=f"ticket={ticket['ticket_id']} signal_price={ticket['signal_price']} "
                  f"current_price={price} notional={notional:.2f} leverage={leverage}x",
        strategy_refs=[str(ticket.get("strategy_id", ""))],
        order_source="strategy",
        strategy_id=str(ticket.get("strategy_id", "")),
        signal_id=str(ticket.get("signal_id", "")),
        ticket_id=str(ticket.get("ticket_id", "")),
        client_order_id=ticket_order_id(str(ticket["ticket_id"])),
        entry_mode="market",
        sl_pct=sl_pct,
        tp_pct=tp_pct,
        margin_mode="ISOLATED",
    )


def _skip(funnel: ScanFunnel | None, stage: str, detail: str, ticket: dict[str, Any],
          *, origin: str = ORIGIN_EXECUTE) -> None:
    """把一次跳过写进漏斗（note 由 :func:`_note` 附加可读的原因码）。"""
    if funnel is None:
        return
    funnel.record(stage, detail, card_id=str(ticket.get("strategy_id") or ""),
                  symbol=str(ticket.get("symbol") or ""),
                  timeframe=str(ticket.get("timeframe") or ""),
                  ticket_id=str(ticket.get("ticket_id") or ""), origin=origin)


def _note(notes: list[str], text: str, detail: str = "") -> None:
    """note 文本附加 ``[九码/细分码]``，让"为什么跳过"在 UI/日志里直接可读。"""
    suffix = f" [{detail_reason(detail)}/{detail}]" if detail else ""
    notes.append(f"{text}{suffix}")


def _audit_write(notes: list[str], what: str, call: Callable[[], Any]) -> None:
    """执行一次**纯审计写入**：失败不得中断本轮，但必须留痕（U08-R5-FU2）。

    审计写入不参与交易决策（它只记录"为什么跳过"），因此异常一律局部化——
    与 N6 对 ``ledger.*`` 的做法同形：``except Exception``，``KeyboardInterrupt`` /
    ``SystemExit``（``BaseException``）必须继续穿出。**不静默**：把操作名与底层
    异常名写进 note，运维能看见"本轮的审计链缺了一段"，而不是只看到一次安静的成功。
    """
    try:
        call()
    except Exception as exc:  # noqa: BLE001 - 审计写入异常不得穿出执行器
        notes.append(f"审计写入失败（{what}，{type(exc).__name__}: {exc}），本轮继续")


def run_strategy_trader(*, tickets: list[dict[str, Any]],
                        portfolio: dict[str, Any] | None = None,
                        prices: dict[str, float] | None = None,
                        min_notional: float = 20.0,
                        max_notional: float = 120.0,
                        max_leverage: int = 20,
                        risk_per_trade: float = 0.02,
                        channel_status: dict[str, Any] | None = None,
                        db: AuditDatabase | None = None,
                        allowed_symbols: Iterable[str] | None = None,
                        funnel: ScanFunnel | None = None,
                        ledger: TicketLedger | None = None) -> ChannelDecision:
    intents: list[TradeIntent] = []
    notes: list[str] = []
    portfolio = portfolio or {}
    positions = list(portfolio.get("positions") or [])

    if min_notional <= 0 or max_notional <= 0:
        # 早退分支原先在循环外、**没有任何地方记录它**——状态文件里只会看到"没有票"，
        # 而真相是"策略通道被配置成只扫描不下单"。现在作为**轮级故障**留痕。
        if funnel is not None:
            funnel.record_round_fault(
                DETAIL_CHANNEL_BUDGET_ZERO,
                note=f"min_notional={min_notional} max_notional={max_notional}")
        return ChannelDecision(
            channel="strategy", decision="no_trade",
            reasoning="strategy_min_notional_usdt/strategy_max_notional_usdt=0，策略通道只扫描不下单。"
                      f" [{DETAIL_CHANNEL_BUDGET_ZERO}]",
            intents=[],
        )

    # 策略账本权益：优先取 ChannelManager 计算的策略通道权益；没有时按总权益 60% 估算
    ch_strategy = (channel_status or {}).get("strategy") or {}
    strategy_equity = float(ch_strategy.get("equity") or 0.0)
    if strategy_equity <= 0:
        session_equity = float((portfolio or {}).get("session_equity") or 0.0)
        if session_equity <= 0:
            session_equity = float((portfolio or {}).get("cash") or 0.0)
        strategy_equity = session_equity * 0.6 if session_equity > 0 else 0.0
    margin_cap = strategy_equity * 0.80

    now = utc_now()
    # 本轮标识：注入漏斗时用漏斗的轮次；**未注入漏斗时也必须非空**——
    # ``TicketLedger.release()`` 的门禁要求 ``round_id`` 非空且与
    # ``consumed_round_id`` 一致，否则"建意图失败后立刻撤销"会被直接拒绝，
    # 该票据被账本**永久占死**（B-r3 N4：一次异常 = 该信号再也不能开仓）。
    exec_round_id = (funnel.round_id if funnel is not None
                     else f"exec-{now.isoformat()}")
    # 逐票说明"为什么这张有效票据没被执行"，而不是静默过滤掉（B-r1 P0-4③：
    # 原先这一行会把"所有票都已消费"读成"本轮没有票据"，进而被读成没有信号）。
    # 形状/时效由 core.signal_ticket 的**同一份**真值表判定；已递交过订单的票据由
    # 审计库证明（跨进程权威），账本只补"消费了但订单还没落库"的窗口。
    incoming = list(tickets)
    admitted: list[dict[str, Any]] = []
    for ticket in incoming:
        detail = ticket_skip_detail(ticket, now)
        if detail:
            _skip(funnel, Stage.TICKET_FRESH.value, detail, ticket)
            _note(notes,
                  f"{ticket.get('ticket_id') or ticket.get('symbol') or '?'} 不可用，跳过",
                  detail)
            continue
        ticket_id = str(ticket.get("ticket_id") or "")
        # 去重读取是**跨进程权威**（"这张票已经下过单吗"）。这里必须 fail-closed
        # （U08-R5-FU2）：审计库**读不出答案**时绝不得当成"没下过"放行——那会重复下单。
        # 与 N6 同形的是"异常不得穿出执行器"；方向相反的是**失败语义**：N6 局部化后
        # 照常开仓，这里局部化后**不**开仓（状态不可验证 -> 不交易）。
        already_ordered = False
        if db is not None:
            try:
                already_ordered = bool(db.signal_ticket_has_order(ticket_id))
            except Exception as exc:  # noqa: BLE001 - 审计库异常不得穿出，且必须 fail-closed
                # 本票本轮跳过并**如实披露**（细分码区分于"已消费"：这不是"已下过单"，
                # 而是"无法断定"）。同轮其余票据的去重读取各自独立，不受牵连。
                # 跳过**不**烧票：账本未被消费，审计库恢复后下一轮仍可正常评估。
                _skip(funnel, Stage.TICKET_FRESH.value, DETAIL_AUDIT_UNAVAILABLE, ticket)
                _note(notes,
                      f"{ticket_id} 审计库不可用"
                      f"（{type(exc).__name__}: {exc}），状态不可验证，跳过",
                      DETAIL_AUDIT_UNAVAILABLE)
                continue
        if already_ordered or (ledger is not None and ledger.is_consumed(ticket_id)):
            _skip(funnel, Stage.TICKET_FRESH.value, DETAIL_TICKET_ALREADY_CONSUMED, ticket)
            _note(notes, f"{ticket_id} 已消费过，跳过", DETAIL_TICKET_ALREADY_CONSUMED)
            continue
        admitted.append(ticket)
    tickets = admitted
    if not tickets:
        if funnel is not None and not incoming:
            # 只有"本轮一张票都没有"才是轮级空集；全部被过滤时逐票原因已在漏斗里
            # （verdict=handled），不能再用 NO_TICKETS_AVAILABLE 冒充"没有信号"。
            funnel.record_round_fault(DETAIL_NO_TICKETS_AVAILABLE, note="本轮无票据")
        return ChannelDecision(
            channel="strategy", decision="no_trade",
            reasoning="; ".join(notes) if notes else "无 ticket 或没有可执行 ticket。",
            intents=[],
        )
    allowed_set = ({str(s).strip().upper() for s in allowed_symbols if s}
                   if allowed_symbols is not None else None)
    symbols = [str(t["symbol"]).strip().upper() for t in tickets
               if allowed_set is None or str(t.get("symbol") or "").strip().upper() in allowed_set]
    prices = dict(prices or {})
    missing = [s for s in symbols if s not in prices]
    if missing:
        try:
            prices.update(get_live_prices(missing))
        except Exception:
            pass

    held_symbols = {str(p.get("symbol")) for p in positions if p.get("symbol")}
    held_strategy = {str(p.get("strategy_id")) for p in positions
                     if str(p.get("source", "ai")) == "strategy" and p.get("strategy_id")}

    for ticket in tickets:
        symbol = str(ticket["symbol"]).strip().upper()
        strategy_id = str(ticket.get("strategy_id", ""))
        if allowed_set is not None and symbol not in allowed_set:
            _skip(funnel, Stage.NO_POSITION_CONFLICT.value, DETAIL_SYMBOL_NOT_ALLOWED,
                  ticket)
            _note(notes,
                  f"{ticket.get('ticket_id', symbol)} 不在当前业务开仓白名单内，跳过",
                  DETAIL_SYMBOL_NOT_ALLOWED)
            if db:
                # 纯审计写入：失败不得中断本轮（U08-R5-FU2），但必须留痕。
                _audit_write(notes, f"log_risk_event(strategy_scope_skip,{symbol})",
                             lambda: db.log_risk_event(
                                 event_type="strategy_scope_skip", severity="info",
                                 details=f"{symbol} 不在当前业务开仓白名单内",
                                 action_taken="skip open"))
            continue
        if not _not_expired(ticket, now):
            _skip(funnel, Stage.TICKET_FRESH.value, TICKET_DETAIL_EXPIRED, ticket)
            _note(notes, f"{ticket['ticket_id']} 已过期，跳过", TICKET_DETAIL_EXPIRED)
            if db:
                # 两笔都是纯审计写入，逐笔独立局部化（一笔失败不影响另一笔）。
                _audit_write(notes,
                             f"log_risk_event(strategy_ticket_expired,{ticket['ticket_id']})",
                             lambda: db.log_risk_event(
                                 event_type="strategy_ticket_expired", severity="info",
                                 details=f"{ticket['ticket_id']} 过期跳过", action_taken="skip"))
                _audit_write(notes,
                             f"update_signal_ticket(expired,{ticket['ticket_id']})",
                             lambda: db.update_signal_ticket(
                                 ticket_id=str(ticket["ticket_id"]), status="expired"))
            continue
        if symbol in held_symbols:
            _skip(funnel, Stage.NO_POSITION_CONFLICT.value, DETAIL_SYMBOL_ALREADY_HELD,
                  ticket)
            _note(notes, f"{ticket['ticket_id']} 同 symbol 已有仓位，跳过",
                  DETAIL_SYMBOL_ALREADY_HELD)
            if db:
                _audit_write(notes,
                             f"log_risk_event(strategy_conflict_skip,{ticket['ticket_id']})",
                             lambda: db.log_risk_event(
                                 event_type="strategy_conflict_skip", severity="warn",
                                 details=f"{ticket['ticket_id']} 与现有 {symbol} 仓位冲突",
                                 action_taken="skip"))
            continue
        if strategy_id in held_strategy:
            _skip(funnel, Stage.NO_POSITION_CONFLICT.value, DETAIL_STRATEGY_ALREADY_HELD,
                  ticket)
            _note(notes, f"{ticket['ticket_id']} 策略 {strategy_id} 已有持仓，跳过",
                  DETAIL_STRATEGY_ALREADY_HELD)
            continue
        price = prices.get(symbol)
        if not price:
            _skip(funnel, Stage.PRICE_AVAILABLE.value, DETAIL_NO_LATEST_PRICE, ticket)
            _note(notes, f"{ticket['ticket_id']} 无最新价，跳过", DETAIL_NO_LATEST_PRICE)
            continue
        if not _price_in_zone(float(price), ticket):
            _skip(funnel, Stage.IN_ENTRY_ZONE.value, DETAIL_PRICE_OUTSIDE_ENTRY_ZONE,
                  ticket)
            _note(
                notes,
                f"{ticket['ticket_id']} 当前价 {price} 不在入场区 "
                f"[{ticket['entry_zone_low']}, {ticket['entry_zone_high']}]，本轮跳过",
                DETAIL_PRICE_OUTSIDE_ENTRY_ZONE)
            continue
        if ledger is not None:
            # 下单前消费：跨轮/跨进程**只消费一次**。这里覆盖的是"消费了但订单还没
            # 落库就被重启"的窗口——审计库的 signal_ticket_has_order 只证明已落库部分。
            # **认领本身也可能失败**（B-r4 N6）：账本写盘会抛 `OSError`（磁盘满/只读
            # 挂载）、`PermissionError`（Windows 上 `os.replace` 目标被占用）、
            # `sqlite3.OperationalError` 等。这些异常与"取不到跨进程锁"同等待遇——
            # fail-closed **不**下单这张票，异常名留在 note 里，同一轮其它票据与
            # 已构造的 intents 照常；绝不允许一句泛泛的异常把整轮作废。
            try:
                claim = ledger.consume(
                    str(ticket.get("ticket_id") or ""),
                    round_id=exec_round_id)
            except Exception as exc:  # noqa: BLE001 - 账本异常不得穿出执行器
                _skip(funnel, Stage.TICKET_FRESH.value,
                      DETAIL_TICKET_LEDGER_UNAVAILABLE, ticket)
                _note(notes,
                      f"{ticket['ticket_id']} 票据账本不可用"
                      f"（{type(exc).__name__}: {exc}），跳过",
                      DETAIL_TICKET_LEDGER_UNAVAILABLE)
                continue
            if claim.state == ALREADY_CONSUMED:
                _skip(funnel, Stage.TICKET_FRESH.value, DETAIL_TICKET_ALREADY_CONSUMED,
                      ticket)
                _note(notes, f"{ticket['ticket_id']} 账本已消费过，跳过",
                      DETAIL_TICKET_ALREADY_CONSUMED)
                continue
            if claim.state != CONSUMED:
                # 账本损坏 / 取不到跨进程锁 / **写盘失败**：fail-closed，宁可本轮不开
                # 也不重复消费。`claim.error` 带上底层异常名（B-r4 N6：诊断不丢）。
                why = f"{claim.state}:{claim.error}" if getattr(claim, "error", "") else (
                    claim.state)
                _skip(funnel, Stage.TICKET_FRESH.value, DETAIL_TICKET_LEDGER_UNAVAILABLE,
                      ticket)
                _note(notes,
                      f"{ticket['ticket_id']} 票据账本不可用（{why}），跳过",
                      DETAIL_TICKET_LEDGER_UNAVAILABLE)
                continue
        holding_minutes = int(ticket.get("time_stop_hours", 48) or 48) * 60
        # 自适应 sizing：先按单笔风险预算和 SL% 定名义金额，再按保证金上限动态选杠杆
        sl_pct = float(ticket.get("stop_loss_pct", 0.03) or 0.03)
        risk_budget = strategy_equity * float(risk_per_trade)
        notional_by_risk = risk_budget / sl_pct if sl_pct > 0 else max_notional
        notional = min(float(max_notional), max(float(min_notional), notional_by_risk))
        # 保证金约束：notional / leverage <= margin_cap
        if margin_cap > 0:
            max_notional_by_margin = margin_cap * float(max_leverage)
            notional = min(notional, max_notional_by_margin)
            import math
            needed_leverage = max(1, int(math.ceil(notional / margin_cap)))
            leverage = min(int(max_leverage), needed_leverage)
        else:
            leverage = 1
        try:
            intent = build_intent_from_ticket(ticket, float(price), notional, leverage,
                                              holding_minutes=holding_minutes)
        except Exception as e:
            # 建意图失败：把刚消费掉的 ticket 立刻释放（同轮次才允许撤销），否则这个
            # ticket 会被账本永久占死（一次异常 = 该信号再也不能开仓）。
            if ledger is not None:
                try:
                    ledger.release(str(ticket.get("ticket_id") or ""),
                                   round_id=exec_round_id)
                except Exception as exc:  # noqa: BLE001 - 释放失败不得穿出（B-r4 N6）
                    # 释放本身写盘失败（磁盘满/文件被占用…）：**不**穿出执行器，
                    # 但必须如实说出来——票据可能仍被账本占死，运维按
                    # `orphan_consumed()` 复核后用 `force=True` 人工撤销。
                    notes.append(f"{ticket['ticket_id']} 账本释放未完成"
                                 f"（{type(exc).__name__}: {exc}），票据可能仍被占死")
            _skip(funnel, Stage.PRICE_AVAILABLE.value, DETAIL_STRATEGY_TRADER_EXCEPTION,
                  ticket)
            _note(notes, f"{ticket['ticket_id']} 建意图失败（{type(e).__name__}），跳过",
                  DETAIL_STRATEGY_TRADER_EXCEPTION)
            continue
        intents.append(intent)
        if funnel is not None:
            funnel.record(Stage.INTENT_READY.value,
                          card_id=str(ticket.get("strategy_id") or ""),
                          symbol=symbol, timeframe=str(ticket.get("timeframe") or ""),
                          ticket_id=str(ticket.get("ticket_id") or ""), origin=ORIGIN_EXECUTE)
        notes.append(f"{ticket['ticket_id']} 进入待执行: {symbol} {intent.side} @ {price} "
                     f"名义={notional:.2f}U 杠杆={leverage}x")

    if ledger is not None and not ledger.corrupt_reason:
        # 损坏态不落盘（save() 会抛 LedgerCorrupt）；正常态落盘保证"已消费"跨进程可见。
        # **本失败必须局部化**（B-r3 N3）：save() 取不到跨进程锁时抛
        # ``LedgerLockTimeout``，原先它直接穿出整个执行器——已经消费落盘的票据
        # 被丢掉、本轮已构造的 intents 全部作废、调用方只得到一句泛泛的
        # "策略交易AI异常"。同一账本的 ``consume()``/``release()`` 都吞掉了锁
        # 超时（返回 ``LEDGER_BUSY`` / 只在内存撤销），这里也必须吞：本轮照常返回
        # intents，把"合并落盘被推迟"登记为**轮级故障**（票据仍在
        # ``orphan_consumed`` 的可审计路径上——审计库是权威，只补单不重发）。
        # B-r4 N6：**范围**不止锁超时/损坏——`OSError`（磁盘满/只读挂载）、
        # `PermissionError`（`os.replace` 目标被占用）、`sqlite3.OperationalError`
        # 等任何异常，若穿出执行器，后果与 N3 完全同形（已消费落盘的票据被丢掉、
        # 本轮已构造的 intents 全部作废）。因此这里收敛为 `except Exception`：
        # 失败照样登记轮级故障 + 保留 intents + 孤儿可审计。**只**吞 ``Exception``，
        # `KeyboardInterrupt`/`SystemExit`（`BaseException`）必须继续穿出（U14 规矩）。
        try:
            ledger.save()
        except Exception as exc:  # noqa: BLE001 - 收尾落盘异常一律局部化
            if funnel is not None:
                funnel.record_round_fault(
                    DETAIL_LEDGER_SAVE_DEFERRED,
                    note=f"{type(exc).__name__}: {exc}"[:160])
            notes.append(f"账本收尾落盘未完成（{type(exc).__name__}），"
                         f"已消费事实留待下次写入 [{DETAIL_LEDGER_SAVE_DEFERRED}]")
    if not intents:
        return ChannelDecision(
            channel="strategy", decision="no_trade",
            reasoning="; ".join(notes) if notes else "无 ticket 或没有可执行 ticket。",
            intents=[],
        )
    return ChannelDecision(
        channel="strategy", decision="open",
        intents=intents,
        reasoning="; ".join(notes),
        strategy_refs=[i.strategy_id or "" for i in intents],
    )
