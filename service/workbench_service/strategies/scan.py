"""v3.1 P2 确定性边沿触发扫描（docs/18 §6.2/§6.3、docs/19 R5）。

扫描只做三件事：

1. 对每个（已授权的用户策略 × ``rules.symbols``）拿 ``evaluator.evaluate_entry`` 的
   求值结果（行情 / K 线由调用方取好传进来）；
2. 按"边沿 + 冷却 + 重新武装 + 停机保护"决定这一轮该触发什么，并给出全序排序；
3. 交回 ``rule_eval_state`` 的新状态，由调用方在同一个事务里写库。

扫描**不拿租约、不调 P1/P2、不下单**。真正的派发在 Host 触发引擎，派发结果通过
``POST /api/rules/dispatched`` 回写（见 :func:`apply_dispatch`）。

时间只有一个来源：调用方传入的 ``now_ms``；模块里没有隐式时钟或随机数，同一份
输入 + 同一个 now_ms 一定得到同一份结论（排序是全序的）。

边沿规则（docs/18 §6.2 + docs/19 R5）：

* 只有 ``last_matched`` 0→1 才触发；首次求值就为真只记状态（不追已经发生的信号）；
* 持续为真不重复触发；一次求值为假就置 ``last_matched=0``；
* 停机保护（R5①）：上一次求值距今超过阈值时按首次求值处理，只更新状态。阈值与
  规则的正常求值节奏匹配：mark 类 = 3 × scan_interval（默认 45s），K 线类 =
  K 线周期 + 3 × scan_interval（15m 规则 945s）——既不会把"每根收盘求值一次"
  的正常节奏误判成停机，也不会容忍几十分钟停机后迟到入场；
  ``now_ms`` 早于上次求值（时钟回拨 / 乱序重试）同样按"不追"处理，且不让
  ``last_eval_ms`` 倒退；
* 重新武装（R5②）：派发回写过（``rearm_pending=1``）之后，必须观察到一次"不满足"
  才能再次产生边沿；P1 拒绝额外冷却 1 个扫描间隔；
* 冷却期内、"该周期信号已消费"都不触发（周期桶去重继续叠加生效）。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .evaluator import PERIOD_MS, entry_uses_klines

#: 扫描覆盖范围：mark=只求值标记价/资金费率类规则；kline=只求值 K 线类规则；all=全部。
#: Host 的节奏是 mark 每 15s 扫一次、K 线收盘 +3s 用 all 再扫一次（docs/18 §3）。
SCAN_SCOPES = ("mark", "kline", "all")
DEFAULT_SCOPE = "all"

#: 扫描间隔：默认 15s（Host 的 mark 定时器），body 可传 scan_interval_ms 覆盖，
#: 夹在 [5s, 60s]：太快会打爆行情缓存，太慢会让冷却/停机保护失去意义。
SCAN_INTERVAL_MS_DEFAULT = 15_000
SCAN_INTERVAL_MS_MIN = 5_000
SCAN_INTERVAL_MS_MAX = 60_000
#: R5① 停机保护：超过 3 个求值节奏没扫过，就按首次求值处理。
STALE_INTERVAL_FACTOR = 3

#: 派发回写结果：dispatched=已派发（P2 成交）；rejected=P1/P2 拒绝或租约/cycle 忙。
DISPATCH_OUTCOMES = ("dispatched", "rejected")

#: 这些 reason_code 表示"这一轮没有可用数据"，不是"条件不满足"：
#: 不参与边沿状态推进（否则行情抖动会被误当成一次"不满足"而重新武装）。
UNAVAILABLE_REASON_CODES = frozenset(
    {"QUOTE_STALE", "KLINE_STALE", "INSUFFICIENT_BARS", "FUNDING_UNAVAILABLE"}
)

_RULE_KEY_RE = re.compile(r"^strategy:([A-Za-z0-9][A-Za-z0-9._-]{0,127}):(\d{1,9})$")


def clamp_interval_ms(value: Any) -> int:
    """把 body 里的 scan_interval_ms 夹到 [5s, 60s]；缺省 15s。"""

    if value is None or value == "":
        return SCAN_INTERVAL_MS_DEFAULT
    try:
        number = int(value)
    except (TypeError, ValueError):
        return SCAN_INTERVAL_MS_DEFAULT
    return max(SCAN_INTERVAL_MS_MIN, min(SCAN_INTERVAL_MS_MAX, number))


def normalize_scope(scope: Any) -> str:
    """校验扫描范围；非法值返回空串，由 API 层转 422。"""

    text = str(scope or DEFAULT_SCOPE).strip().lower()
    return text if text in SCAN_SCOPES else ""


def rule_key_for(strategy_id: str, version: int) -> str:
    return f"strategy:{strategy_id}:{int(version)}"


def split_rule_key(rule_key: str) -> tuple[str, int] | None:
    match = _RULE_KEY_RE.match(str(rule_key or "").strip())
    if not match:
        return None
    return match.group(1), int(match.group(2))


def rule_scope(entry: Any) -> str:
    """一条规则属于哪个扫描范围：K 线类还是标记价类。"""

    return "kline" if entry_uses_klines(entry) else "mark"


def scope_allows(scope: str, entry: Any) -> bool:
    return scope == "all" or scope == rule_scope(entry)


def stale_after_ms(scan_interval_ms: Any, rules: Mapping[str, Any] | None) -> int:
    """R5① 停机保护阈值（毫秒）：上一次求值距今超过它，就按"首次求值"处理。

    口径（t8 裁定）：

    * mark 类规则：3 × scan_interval（默认 45s）。它们每轮扫描都求值，3 个间隔
      没有求值就是真的停机过。
    * K 线类规则：K 线周期 + 3 × scan_interval（15m 规则 = 900s + 45s = 945s）。
      正常运行时 Host 只在每根 K 线收盘后 +3s 求值一次（docs/18 §3），所以阈值
      必须**大于一个周期**，否则正常节奏会被误判成停机；但也**不能取 3 个周期**
      （15m 规则 = 45 分钟），那会把 20–45 分钟前就已经发生的信号当成新边沿入场，
      正好违背 R5① 防迟到入场的目的。
    """

    interval = clamp_interval_ms(scan_interval_ms)
    entry = (rules or {}).get("entry")
    if not entry_uses_klines(entry):
        return STALE_INTERVAL_FACTOR * interval
    frames = (rules or {}).get("timeframes") or ["15m"]
    timeframe = str(frames[0]) if frames else "15m"
    return int(PERIOD_MS.get(timeframe, 900_000)) + STALE_INTERVAL_FACTOR * interval


@dataclass(frozen=True, slots=True)
class EvalState:
    """``rule_eval_state`` 的一行（last_matched/rearm_pending 用 0/1 存）。"""

    rule_key: str
    symbol: str
    last_matched: int
    last_eval_ms: int
    last_bar_open_ms: int | None = None
    cooldown_until_ms: int | None = None
    rearm_pending: int = 0

    def view(self) -> dict[str, Any]:
        return {
            "rule_key": self.rule_key,
            "symbol": self.symbol,
            "last_matched": bool(self.last_matched),
            "last_eval_ms": int(self.last_eval_ms),
            "last_bar_open_ms": None if self.last_bar_open_ms is None else int(self.last_bar_open_ms),
            "cooldown_until_ms": None if self.cooldown_until_ms is None else int(self.cooldown_until_ms),
            "rearm_pending": bool(self.rearm_pending),
        }


@dataclass(frozen=True, slots=True)
class ScanInput:
    """一条（规则 × 标的）的本轮求值结果；只带扫描需要的字段。"""

    rule_key: str
    strategy_id: str
    name: str
    version: int
    generation: int
    mode_revision: int
    execution_mode: str
    symbol: str
    side: str
    priority: float
    created_at: str
    matched: bool
    reason_code: str
    detail: str
    signal_key: str
    mark: float | None
    distance_pct: float | None
    facts: Mapping[str, Any]
    consumed: bool
    #: R5① 停机保护阈值（毫秒），由 :func:`stale_after_ms` 按规则类型算好传进来。
    stale_after_ms: int

    def is_usable(self) -> bool:
        return self.reason_code not in UNAVAILABLE_REASON_CODES


@dataclass(frozen=True, slots=True)
class ScanPlan:
    triggered: tuple[dict[str, Any], ...]
    waiting: tuple[dict[str, Any], ...]
    transitioned: tuple[dict[str, Any], ...]
    states: tuple[EvalState, ...]


def _sort_key(item: ScanInput) -> tuple[Any, ...]:
    """确定性排序（docs/18 §6.3）：priority 降序 → 创建时间升序 → 全序兜底。"""

    priority = float(item.priority) if math.isfinite(float(item.priority)) else 0.0
    return (-priority, str(item.created_at or ""), item.strategy_id, item.symbol, int(item.version))


def _trigger_ref(item: ScanInput) -> float | None:
    """触发参考价：触发原语的阈值（mark/candle_close），取不到时退回标记价。"""

    facts = item.facts or {}
    for key in ("threshold", "candle_close"):
        value = facts.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if math.isfinite(float(value)):
            return float(value)
    return item.mark


def _strategy_context(item: ScanInput) -> dict[str, Any]:
    """P1/P2 现有的 strategy_context 信封（5 个键，Host 原样透传）。"""

    return {
        "strategy_id": item.strategy_id,
        "version": int(item.version),
        "generation": int(item.generation),
        "mode_revision": int(item.mode_revision),
        "signal_key": item.signal_key,
    }


def _triggered_entry(item: ScanInput, *, now_ms: int) -> dict[str, Any]:
    return {
        "rule_key": item.rule_key,
        "kind": "strategy",
        "strategy_id": item.strategy_id,
        "name": item.name,
        "version": int(item.version),
        "generation": int(item.generation),
        "mode_revision": int(item.mode_revision),
        "execution_mode": item.execution_mode,
        "symbol": item.symbol,
        "side": item.side,
        "signal_key": item.signal_key,
        "mark": item.mark,
        "trigger_ref": _trigger_ref(item),
        "distance_pct": item.distance_pct,
        "priority": float(item.priority) if math.isfinite(float(item.priority)) else 0.0,
        "triggered_at_ms": int(now_ms),
        "strategy_context": _strategy_context(item),
        "facts": dict(item.facts or {}),
        "detail": item.detail,
    }


def _waiting_entry(
    item: ScanInput,
    *,
    blocked_by: str | None,
    state_reset: bool,
    cooldown_until_ms: int | None,
    rearm_pending: bool,
    now_ms: int,
) -> dict[str, Any]:
    return {
        "rule_key": item.rule_key,
        "strategy_id": item.strategy_id,
        "name": item.name,
        "version": int(item.version),
        "generation": int(item.generation),
        "symbol": item.symbol,
        "side": item.side,
        "signal_key": item.signal_key,
        "matched": bool(item.matched),
        "mark": item.mark,
        "trigger_ref": _trigger_ref(item),
        "distance_pct": item.distance_pct,
        "reason_code": item.reason_code,
        "detail": item.detail,
        "blocked_by": blocked_by,
        "state_reset": bool(state_reset),
        "cooldown_until_ms": None if cooldown_until_ms is None else int(cooldown_until_ms),
        "rearm_pending": bool(rearm_pending),
        "evaluated_at_ms": int(now_ms),
        "facts": dict(item.facts or {}),
    }


def plan_scan(
    inputs: Iterable[ScanInput],
    states: Mapping[tuple[str, str], EvalState],
    *,
    now_ms: int,
) -> ScanPlan:
    """纯函数：算出本轮 triggered/waiting/transitioned 和要写回的状态。

    ``states`` 是扫描前的 ``rule_eval_state`` 快照；每条输入自带 ``stale_after_ms``
    （mark 规则 = 3×扫描间隔，K 线规则 = K 线周期 + 3×扫描间隔，见
    :func:`stale_after_ms`），停机保护按它判定。
    没有可用数据的求值（:`UNAVAILABLE_REASON_CODES`）只出现在 waiting 里，不写状态。
    """

    now = int(now_ms)
    triggered: list[dict[str, Any]] = []
    waiting: list[dict[str, Any]] = []
    transitioned: list[dict[str, Any]] = []
    next_states: list[EvalState] = []

    for item in sorted(inputs, key=_sort_key):
        prev = states.get((item.rule_key, item.symbol))
        if not item.is_usable():
            # 没有可用行情/K 线：不算"不满足"，也不推进边沿状态（防抖）。
            waiting.append(
                _waiting_entry(
                    item,
                    blocked_by="data_unavailable",
                    state_reset=False,
                    cooldown_until_ms=None if prev is None else prev.cooldown_until_ms,
                    rearm_pending=False if prev is None else bool(prev.rearm_pending),
                    now_ms=now,
                )
            )
            continue

        first = prev is None
        prev_eval_ms = None if first else int(prev.last_eval_ms)
        # now_ms 早于上次求值（时钟回拨 / 乱序重试的 tick）同样按"不追"处理，
        # 并且不让 last_eval_ms 倒退（状态单调，便于复现与排障）。
        clock_skew = prev_eval_ms is not None and now < prev_eval_ms
        stale = prev_eval_ms is not None and (
            clock_skew or (now - prev_eval_ms) >= item.stale_after_ms
        )
        protected = first or stale
        prev_matched = 0 if protected else int(prev.last_matched)
        rearm_pending = 0 if first else int(prev.rearm_pending)
        cooldown_until = None if first else prev.cooldown_until_ms
        cooldown_active = cooldown_until is not None and int(cooldown_until) > now
        if not item.matched:
            # 观察到一次"不满足"：清除重新武装标记（R5②）。
            rearm_pending = 0
        crossed = bool(item.matched) and prev_matched == 0

        blocked_by: str | None = None
        if clock_skew:
            blocked_by = "clock_skew"
        elif protected:
            blocked_by = "first_eval" if first else "stale_gap"
        elif not crossed:
            blocked_by = "level_hold" if item.matched else None
        elif rearm_pending:
            blocked_by = "rearm_pending"
        elif cooldown_active:
            blocked_by = "cooldown"
        elif item.consumed:
            blocked_by = "already_signaled"
        fired = bool(item.matched) and crossed and blocked_by is None

        last_bar_open_ms = item.facts.get("bar_open_time_ms") if isinstance(item.facts, Mapping) else None
        if last_bar_open_ms is None and prev is not None:
            last_bar_open_ms = prev.last_bar_open_ms
        state = EvalState(
            rule_key=item.rule_key,
            symbol=item.symbol,
            last_matched=1 if item.matched else 0,
            last_eval_ms=now if prev_eval_ms is None else max(now, prev_eval_ms),
            last_bar_open_ms=None if last_bar_open_ms is None else int(last_bar_open_ms),
            cooldown_until_ms=None if cooldown_until is None else int(cooldown_until),
            rearm_pending=int(bool(rearm_pending)),
        )
        next_states.append(state)

        if prev is not None and (
            int(prev.last_matched) != state.last_matched
            or int(prev.rearm_pending) != state.rearm_pending
            or (None if prev.cooldown_until_ms is None else int(prev.cooldown_until_ms))
            != state.cooldown_until_ms
        ):
            transitioned.append(
                {
                    "rule_key": item.rule_key,
                    "symbol": item.symbol,
                    "strategy_id": item.strategy_id,
                    "at_ms": now,
                    "from": {
                        "last_matched": bool(prev.last_matched),
                        "rearm_pending": bool(prev.rearm_pending),
                        "cooldown_until_ms": None
                        if prev.cooldown_until_ms is None
                        else int(prev.cooldown_until_ms),
                    },
                    "to": {
                        "last_matched": bool(state.last_matched),
                        "rearm_pending": bool(state.rearm_pending),
                        "cooldown_until_ms": state.cooldown_until_ms,
                    },
                    "triggered": fired,
                }
            )

        if fired:
            triggered.append(_triggered_entry(item, now_ms=now))
        else:
            waiting.append(
                _waiting_entry(
                    item,
                    blocked_by=blocked_by,
                    state_reset=protected,
                    cooldown_until_ms=state.cooldown_until_ms,
                    rearm_pending=bool(state.rearm_pending),
                    now_ms=now,
                )
            )

    return ScanPlan(
        triggered=tuple(triggered),
        waiting=tuple(waiting),
        transitioned=tuple(transitioned),
        states=tuple(next_states),
    )


# --------------------------------------------------------------- 状态持久化
# 纯 SQL 助手：只读写 rule_eval_state，不碰别的表，方便调用方放进同一个事务。
_STATE_COLUMNS = (
    "rule_key",
    "symbol",
    "last_matched",
    "last_eval_ms",
    "last_bar_open_ms",
    "cooldown_until_ms",
    "rearm_pending",
)


def _state_from_row(row: Mapping[str, Any]) -> EvalState:
    return EvalState(
        rule_key=str(row["rule_key"]),
        symbol=str(row["symbol"]),
        last_matched=int(row["last_matched"]),
        last_eval_ms=int(row["last_eval_ms"]),
        last_bar_open_ms=None if row["last_bar_open_ms"] is None else int(row["last_bar_open_ms"]),
        cooldown_until_ms=None if row["cooldown_until_ms"] is None else int(row["cooldown_until_ms"]),
        rearm_pending=int(row["rearm_pending"]),
    )


def load_states(conn: Any) -> dict[tuple[str, str], EvalState]:
    """读全部边沿状态（表规模 = 已启用策略的标的数，上限 5×10 行，直接全读）。"""

    rows = conn.execute(
        f"SELECT {', '.join(_STATE_COLUMNS)} FROM rule_eval_state"
    ).fetchall()
    return {(row["rule_key"], row["symbol"]): _state_from_row(row) for row in rows}


def load_state(conn: Any, rule_key: str, symbol: str) -> EvalState | None:
    row = conn.execute(
        f"SELECT {', '.join(_STATE_COLUMNS)} FROM rule_eval_state WHERE rule_key=? AND symbol=?",
        (rule_key, symbol),
    ).fetchone()
    return None if row is None else _state_from_row(row)


def write_states(conn: Any, states: Iterable[EvalState]) -> int:
    """按主键 upsert；返回写入行数。"""

    count = 0
    for state in states:
        conn.execute(
            "INSERT INTO rule_eval_state "
            "(rule_key,symbol,last_matched,last_eval_ms,last_bar_open_ms,cooldown_until_ms,rearm_pending) "
            "VALUES (?,?,?,?,?,?,?) "
            "ON CONFLICT(rule_key,symbol) DO UPDATE SET "
            "last_matched=excluded.last_matched,last_eval_ms=excluded.last_eval_ms,"
            "last_bar_open_ms=excluded.last_bar_open_ms,cooldown_until_ms=excluded.cooldown_until_ms,"
            "rearm_pending=excluded.rearm_pending",
            (
                state.rule_key,
                state.symbol,
                int(state.last_matched),
                int(state.last_eval_ms),
                None if state.last_bar_open_ms is None else int(state.last_bar_open_ms),
                None if state.cooldown_until_ms is None else int(state.cooldown_until_ms),
                int(state.rearm_pending),
            ),
        )
        count += 1
    return count


def apply_dispatch(
    conn: Any,
    *,
    rule_key: str,
    symbol: str,
    outcome: str,
    at_ms: Any,
    scan_interval_ms: Any = SCAN_INTERVAL_MS_DEFAULT,
) -> tuple[EvalState, bool]:
    """回写一次派发结果（R5②），返回 (新状态, 是否幂等重放)。

    * ``dispatched``：``rearm_pending=1``、``last_matched=1``——必须观察到一次
      "不满足"才能重新武装；
    * ``rejected``：在 dispatched 的基础上额外冷却 1 个扫描间隔。冷却用 max()
      合并，所以同一个 at_ms 重复回写不会叠加（幂等）。
    """

    if outcome not in DISPATCH_OUTCOMES:
        raise ValueError(f"unsupported dispatch outcome: {outcome}")
    key = str(rule_key)
    sym = str(symbol).upper()
    at = int(at_ms)
    interval = clamp_interval_ms(scan_interval_ms)
    prev = load_state(conn, key, sym)
    if prev is None:
        cooldown = at + interval if outcome == "rejected" else None
        state = EvalState(
            rule_key=key,
            symbol=sym,
            last_matched=1,
            last_eval_ms=at,
            last_bar_open_ms=None,
            cooldown_until_ms=cooldown,
            rearm_pending=1,
        )
        idempotent = False
    else:
        cooldown = prev.cooldown_until_ms
        if outcome == "rejected":
            cooldown = max(int(prev.cooldown_until_ms or 0), at + interval)
        state = EvalState(
            rule_key=key,
            symbol=sym,
            last_matched=1,
            last_eval_ms=int(prev.last_eval_ms),
            last_bar_open_ms=prev.last_bar_open_ms,
            cooldown_until_ms=None if cooldown is None else int(cooldown),
            rearm_pending=1,
        )
        idempotent = state == prev
    write_states(conn, (state,))
    return state, idempotent
