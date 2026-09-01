"""ActionSpace：交易通道的合法动作空间 —— 单一谓词源。

同一份 check() 被三处复用：

  1. 动态 schema 生成（编译期剪掉模型无权执行的动作，模型根本看不到 open）
  2. submit_decision 的 Validator 回环（错误以 tool result 回传，模型自我修正后重试）
  3. risk.py 运行时闸门（执行前复核，因为状态可能在 schema 生成之后发生变化）

三处共用同一谓词，是为了避免"schema 说合法、闸门却拒绝"的漂移。那种漂移的后果最坏：
模型会在它认为合法的集合里反复重试，表现为 token 暴涨且本轮颗粒无收，而且日志里看不出原因。

本模块的规则逐条对应 risk.py 中原本内联的判据，不新增规则。要改规则改这里，三处同时生效。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

# 存量管理动作：任何情况下都不得被剪掉。
# "允许新开仓"的开关只能影响 open，绝不能影响对已有持仓的平/减/调，
# 否则会出现"只能开不能平"的死局。
ALWAYS_ALLOWED: tuple[str, ...] = ("close", "reduce", "adjust")
NEW_POSITION_ACTION = "open"
ALL_ACTIONS: tuple[str, ...] = (NEW_POSITION_ACTION,) + ALWAYS_ALLOWED
VALID_SIDES = ("LONG", "SHORT")


def _fmt_symbols(symbols: Sequence[str], limit: int = 12) -> str:
    items = [str(s) for s in symbols if s][:limit]
    if not items:
        return "（无）"
    if len([s for s in symbols if s]) > limit:
        return ", ".join(items) + f" 等 {len([s for s in symbols if s])} 个"
    return ", ".join(items)


@dataclass
class ActionSpace:
    """一个循环内、某个通道可执行的动作集合与数值上限。"""

    channel: str = "ai"
    # 通道是否允许新开仓 + 被禁原因（会原样展示给模型）
    allow_new: bool = True
    allow_new_reason: str = ""

    max_leverage: int = 20
    ai_max_leverage: int = 20
    ai_strong_max_leverage: int = 25
    strong_max_leverage: int = 50
    strong_confidence_threshold: float = 0.75

    max_open_positions: int = 8
    # 全量交易所白名单：用于 check_intent 兜底校验（确保 symbol 真实可交易）。
    supported_symbols: list[str] = field(default_factory=list)
    # 本轮可交易引导池（核心主流 + 异动山寨）：用于 submit_decision 的 enum 与提示词展示。
    # 一定是 supported_symbols 的子集，避免“enum 说合法、闸门却拒绝”的漂移。
    tradeable_symbols: list[str] = field(default_factory=list)
    held_symbols: list[str] = field(default_factory=list)
    strategy_held_symbols: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ 动作集

    @property
    def allowed_actions(self) -> list[str]:
        actions = list(ALWAYS_ALLOWED)
        if self.allow_new:
            actions.insert(0, NEW_POSITION_ACTION)
        return actions

    def blocks(self, action: str) -> str:
        """返回该动作被禁的原因；空串表示允许。"""
        if action == NEW_POSITION_ACTION and not self.allow_new:
            return self.allow_new_reason or "当前运行状态不允许新开仓"
        return ""

    @property
    def remaining_slots(self) -> int:
        return max(0, int(self.max_open_positions) - len(self.held_symbols))

    @property
    def schema_max_leverage(self) -> int:
        """schema 里 leverage 的 maximum：AI 通道取带止损时可达的强信心档上限（25x）。"""
        if self.channel == "ai":
            return int(min(self.strong_max_leverage, self.ai_strong_max_leverage))
        return int(max(self.max_leverage, self.strong_max_leverage))

    def max_leverage_for(self, intent: Any) -> int:
        """杠杆上限算法：强信心档需同时满足 confidence 阈值与带止损。

        用户约定（2026-09-01）：AI 常规档 20x 封顶；强信心档最高 25x。
        """
        confidence = float(getattr(intent, "confidence", 0.0) or 0.0)
        stop_loss = getattr(intent, "stop_loss", None)
        strong = (confidence >= self.strong_confidence_threshold
                  and stop_loss is not None and float(stop_loss) > 0)
        if self.channel == "ai":
            if strong:
                return int(min(self.strong_max_leverage, self.ai_strong_max_leverage))
            return int(min(self.max_leverage, self.ai_max_leverage))
        return int(self.strong_max_leverage if strong else self.max_leverage)

    # ------------------------------------------------------------------ 校验谓词

    def check_intent(self, intent: Any, prices: dict[str, float] | None = None) -> list[str]:
        """逐条业务规则校验。返回问题清单，空列表表示通过。"""
        prices = prices or {}
        action = str(getattr(intent, "action", "") or "").strip().lower()
        symbol = str(getattr(intent, "symbol", "") or "").strip().upper()
        label = symbol or "(缺 symbol)"

        if action not in ALL_ACTIONS:
            return [f"未知 action={action!r}，必须是 {list(ALL_ACTIONS)} 之一"]

        blocked = self.blocks(action)
        if blocked:
            return [f"{label} 不可执行 {action}：{blocked}"]

        if not symbol:
            return ["缺少 symbol"]

        issues: list[str] = []

        if self.supported_symbols and symbol not in {s.upper() for s in self.supported_symbols}:
            issues.append(f"{symbol} 不在交易所支持的 symbol 列表内（{_fmt_symbols(self.supported_symbols)}）")

        # 存量管理动作必须打在真实持仓上，避免幻觉 symbol
        held = {s.upper() for s in self.held_symbols}
        if action in ALWAYS_ALLOWED and symbol not in held:
            issues.append(f"{symbol} 当前无持仓，不能 {action}；仅可对 {_fmt_symbols(self.held_symbols)} 执行")

        side = str(getattr(intent, "side", "") or "").strip().upper()
        if side not in VALID_SIDES:
            issues.append(f"{label}.side={side!r}，必须是 LONG 或 SHORT")

        # 调整单至少要有止损或止盈之一；其余动作数量必须 > 0
        if action == "adjust":
            if getattr(intent, "stop_loss", None) is None and getattr(intent, "take_profit", None) is None:
                issues.append(f"{symbol} 调整单必须至少设置止损或止盈之一")
        else:
            try:
                qty = float(getattr(intent, "qty", 0) or 0.0)
            except (TypeError, ValueError):
                qty = 0.0
                issues.append(f"{symbol}.qty={getattr(intent, 'qty', None)!r} 不是数字")
            if qty <= 0:
                issues.append(f"{symbol} {action} 数量必须大于 0，当前为 {qty}")

        if action != NEW_POSITION_ACTION:
            return issues

        # ---- 以下仅约束新开仓 ----
        conflicts = {s.upper() for s in self.strategy_held_symbols}
        if symbol in conflicts:
            issues.append(f"{symbol} 已有策略通道持仓，AI 通道不得重复开仓（会触发通道冲突被丢弃）")

        leverage = int(getattr(intent, "leverage", 1) or 1)
        cap = self.max_leverage_for(intent)
        if leverage > cap:
            issues.append(f"{symbol} leverage={leverage} 超过上限 {cap}"
                          f"（confidence={getattr(intent, 'confidence', None)}，带止损可走强信心档）")

        order_type = str(getattr(intent, "order_type", "MARKET") or "MARKET").upper()
        if order_type == "LIMIT":
            limit_price = getattr(intent, "limit_price", None)
            if not limit_price or float(limit_price) <= 0:
                issues.append(f"{symbol} 限价单必须设置有效的 limit_price > 0")
            if self.channel == "ai" and not getattr(intent, "limit_expires_at", None):
                issues.append(f"{symbol} AI 限价单必须设置 limit_expires_at（ISO8601 UTC）")

        entry_ref = None
        if order_type == "LIMIT" and getattr(intent, "limit_price", None):
            entry_ref = float(intent.limit_price)
        else:
            price = prices.get(symbol)
            if price:
                entry_ref = float(price)

        if entry_ref is None:
            issues.append(f"{symbol} 缺少最新价格，无法校验止损止盈方向")
            return issues

        stop_loss = getattr(intent, "stop_loss", None)
        take_profit = getattr(intent, "take_profit", None)
        if stop_loss is None or float(stop_loss) <= 0:
            issues.append(f"{symbol} 新开仓必须设置初始止损 stop_loss > 0")
        if take_profit is None or float(take_profit) <= 0:
            issues.append(f"{symbol} 新开仓必须设置初始止盈 take_profit > 0")
        if issues:
            return issues

        sl, tp = float(stop_loss), float(take_profit)
        if side == "LONG" and not (sl < entry_ref < tp):
            issues.append(f"{symbol} LONG 止损/止盈方向错误：需 stop_loss < 入场价 < take_profit，"
                          f"当前 SL={sl}, 入场参考={entry_ref}, TP={tp}")
        elif side == "SHORT" and not (tp < entry_ref < sl):
            issues.append(f"{symbol} SHORT 止损/止盈方向错误：需 take_profit < 入场价 < stop_loss，"
                          f"当前 TP={tp}, 入场参考={entry_ref}, SL={sl}")
        return issues

    def check_decision(self, intents: Iterable[Any],
                       prices: dict[str, float] | None = None) -> list[str]:
        """跨 intent 的规则：条数上限、重复 symbol。"""
        prices = prices or {}
        items = list(intents)
        issues: list[str] = []
        if len(items) > int(self.max_open_positions):
            issues.append(f"本轮 intent 数量 {len(items)} 超过 max_open_positions={self.max_open_positions}")
        seen: set[str] = set()
        for it in items:
            symbol = str(getattr(it, "symbol", "") or "").strip().upper()
            if not symbol:
                continue
            if symbol in seen:
                issues.append(f"{symbol} 在同一轮出现多条 intent，请合并为一条")
            seen.add(symbol)
        for it in items:
            issues.extend(self.check_intent(it, prices))
        return issues

    def check_position_reviews(self, reviews: Iterable[Any],
                               intents: Iterable[Any]) -> list[str]:
        """position_reviews 交叉校验：每个持仓必须表态、不得评述非持仓、
        声称 close/reduce 的必须有对应 intent 动作。这是"只开不管"的结构性修复。"""
        issues: list[str] = []
        review_items = [r for r in reviews if r is not None]
        held = {s.upper() for s in self.held_symbols}
        if not held:
            if review_items:
                issues.append(f"当前没有持仓，position_reviews 应为空数组，却收到 {len(review_items)} 条")
            return issues

        review_symbols: set[str] = set()
        verdicts: dict[str, str] = {}
        for r in review_items:
            if isinstance(r, dict):
                symbol = str(r.get("symbol") or "").strip().upper()
                verdict = str(r.get("verdict") or "").strip().lower()
            else:
                symbol = str(getattr(r, "symbol", "") or "").strip().upper()
                verdict = str(getattr(r, "verdict", "") or "").strip().lower()
            if not symbol:
                issues.append("position_reviews 中有一条缺少 symbol")
                continue
            review_symbols.add(symbol)
            if verdict not in ("hold", "reduce", "close", "adjust"):
                issues.append(f"position_reviews[{symbol}].verdict={verdict!r} 非法，"
                              f"必须是 hold/reduce/close/adjust")
            verdicts[symbol] = verdict

        missing = sorted(held - review_symbols)
        if missing:
            issues.append(f"position_reviews 必须覆盖全部持仓，缺少对 {', '.join(missing)} 的表态")
        fake = sorted(review_symbols - held)
        if fake:
            issues.append(f"position_reviews 只能评述真实持仓，出现非持仓 symbol：{', '.join(fake)}")

        actions_by_symbol: dict[str, set[str]] = {}
        for it in intents:
            sym = str(getattr(it, "symbol", "") or "").strip().upper()
            if sym:
                actions_by_symbol.setdefault(sym, set()).add(str(getattr(it, "action", "") or ""))
        for symbol, verdict in verdicts.items():
            if verdict in ("close", "reduce") and (
                    symbol not in actions_by_symbol or verdict not in actions_by_symbol[symbol]):
                issues.append(f"position_reviews[{symbol}] 声明 {verdict}，但 intents 里没有对应的 "
                              f"{verdict} 动作，请保持一致（要么加 intent，要么改 verdict）")
        return issues

    # ------------------------------------------------------------------ 提示词

    def summary(self) -> str:
        """给模型看的动作空间说明。动态 schema 剪掉 open 之后，这里负责解释"为什么没有 open"。"""
        lines = [f"本轮可执行动作：{', '.join(self.allowed_actions)}"]
        if not self.allow_new:
            lines.append(f"注意：本轮不允许新开仓（{self.allow_new_reason}）。"
                         f"你仍然必须管理已有持仓：close / reduce / adjust 照常可用。")
        else:
            if self.channel == "ai":
                lines.append(f"开仓额度：常规档杠杆上限 {min(self.max_leverage, self.ai_max_leverage)}x；"
                             f"强信心档（confidence≥{self.strong_confidence_threshold} 且带止损）"
                             f"最高 {self.schema_max_leverage}x；"
                             f"剩余仓位槽位 {self.remaining_slots}/{self.max_open_positions}")
            else:
                lines.append(f"开仓额度：杠杆上限 {self.max_leverage}x，"
                             f"剩余仓位槽位 {self.remaining_slots}/{self.max_open_positions}")
        lines.append(f"当前持仓：{_fmt_symbols(self.held_symbols)}")
        if self.strategy_held_symbols:
            lines.append(f"策略通道已占用（AI 不得重复开仓）：{_fmt_symbols(self.strategy_held_symbols)}")
        pool = self.tradeable_symbols or self.supported_symbols
        if pool:
            lines.append(f"可交易 symbol 共 {len(pool)} 个：{_fmt_symbols(pool)}")
        return "\n".join(lines)

    # ------------------------------------------------------------------ 构造

    @classmethod
    def from_context(cls, *, risk_config: dict[str, Any],
                     allow_new_positions: bool = True,
                     portfolio: dict[str, Any] | None = None,
                     channel: str = "ai",
                     tradeable_symbols: list[str] | None = None) -> "ActionSpace":
        """从运行上下文构造。risk.py 与 orchestrator 都走这里，保证三处谓词同源。

        tradeable_symbols 是本轮可交易引导池（核心主流 + 异动山寨）；未提供时回退到全量白名单。
        强制与全量白名单取交集，保证不变量：tradeable ⊆ supported。
        """
        risk_config = risk_config or {}
        portfolio = portfolio or {}
        positions = [p for p in (portfolio.get("positions") or []) if isinstance(p, dict)]
        held = [str(p.get("symbol") or "").strip().upper() for p in positions if p.get("symbol")]
        strategy_held = [str(p.get("symbol") or "").strip().upper() for p in positions
                         if p.get("symbol") and str(p.get("source") or "") == "strategy"]

        channel_status = risk_config.get("channel_status") or {}
        ch = channel_status.get(channel)
        allow_new = True
        reason = ""
        if not allow_new_positions:
            allow_new = False
            reason = "控制面运行状态已禁止新开仓"
        elif isinstance(ch, dict) and not ch.get("allow_new", True):
            allow_new = False
            margin_used = float(ch.get("margin_used") or 0.0)
            cap = float(ch.get("margin_cap") or 0.0)
            reason = (f"{channel} 通道已用保证金 {margin_used:.2f} 达到/超过上限 {cap:.2f}"
                      + ("，且当日已熔断" if ch.get("halted") else ""))

        supported = risk_config.get("supported_symbols") or []
        supported_list = [str(s).strip().upper() for s in supported if s]
        supported_set = set(supported_list)
        if tradeable_symbols is not None:
            tradeable = [str(s).strip().upper() for s in tradeable_symbols if s]
        else:
            tradeable = list(supported_list)
        # 引导池必须是白名单子集，且保持顺序去重
        tradeable = [s for s in tradeable if s in supported_set]
        seen: set[str] = set()
        tradeable = [s for s in tradeable if not (s in seen or seen.add(s))]
        return cls(
            channel=channel,
            allow_new=allow_new,
            allow_new_reason=reason,
            max_leverage=int(risk_config.get("max_leverage", 20)),
            ai_max_leverage=int(risk_config.get("ai_max_leverage", 20)),
            ai_strong_max_leverage=int(risk_config.get("ai_strong_max_leverage", 25)),
            strong_max_leverage=int(risk_config.get("strong_max_leverage", 50)),
            strong_confidence_threshold=float(risk_config.get("strong_confidence_threshold", 0.75)),
            max_open_positions=int(risk_config.get("max_open_positions", 8)),
            supported_symbols=supported_list,
            tradeable_symbols=tradeable,
            held_symbols=held,
            strategy_held_symbols=strategy_held,
        )
