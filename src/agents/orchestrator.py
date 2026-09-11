"""主交易 Agent：先通过工具查询市场，再输出结构化 TradeDecision。"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import copy

from audit.database import AuditDatabase, utc_now
from audit.jsonl_logger import JsonlLogger
from core.action_space import ActionSpace, VALID_SIDES
from core.agent_recorder import AgentRecorder
from core.decision_policy import enforced_issues, evaluate_open_intent
from core.portfolio_policy import evaluate_portfolio_intent
from data.market_query import TOOL_SPECS, execute_tool
from llm.base import LLMClient

from . import lessons
from .models import MacroBrief, MarketBrief, OnchainBrief, StrategyRefs, TradeDecision

# submit_decision 校验失败后允许严格纠错的次数；再失败就降级为 no_trade，避免死循环烧 token。
MAX_STRICT_SUBMITS = 2

# 主 Agent 工具循环的最大轮次（每轮一次 LLM 调用）。
MAX_ROUNDS = 5

# 每轮行情查询的默认预算（次）。这是 token 成本的核心闸门：
# orchestrator 占全系统 prompt token 的 95%，而查询结果在 messages 里逐轮累积、只增不减。
DEFAULT_QUERY_BUDGET = 8

# 计入预算的行情工具；query_review_library 是本地库查询，不计入。
QUERY_TOOL_NAMES = {"query_symbol_detail", "query_symbol_cards", "query_top_symbols"}

# AI 自决策通道的核心可交易池（主流币）。
# 决策原则：日常以主流为主，山寨币仅在出现异动信号时动态进入本轮可交易集合。
AI_CORE_SYMBOLS = [
    "BTCUSDT", "ETHUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "LINKUSDT", "AVAXUSDT", "LTCUSDT", "TRXUSDT", "DOTUSDT",
    "UNIUSDT", "BCHUSDT", "ETCUSDT", "XLMUSDT", "TONUSDT", "SUIUSDT",
    "NEARUSDT", "APTUSDT",
]


def extract_anomaly_symbols(market: MarketBrief) -> list[str]:
    """从 market_brief 的 anomalies / top_movers 提取异动 symbol（非核心池山寨币）。

    这是「主流为主 + 山寨异动」双层可交易集合的关键：异动山寨币被检测到后，
    自动进入本轮可交易集合与提示词，让 AI 能在主流之外捕捉异动机会。
    """
    symbols: list[str] = []
    seen: set[str] = set()
    core = set(AI_CORE_SYMBOLS)
    for text in list(market.anomalies or []) + list(market.top_movers or []):
        m = re.match(r"^([0-9A-Z]+USDT)", str(text).strip())
        if not m:
            continue
        sym = m.group(1).upper()
        if sym and sym not in seen and sym not in core:
            seen.add(sym)
            symbols.append(sym)
    return symbols


def _slim_risk_envelope(risk_envelope: dict[str, Any] | None) -> dict[str, Any]:
    """只保留 AI 决策真正用到的风控字段，剥离通道统计/复盘点冗余。

    被剥离的 channel_status / supported_symbols / always_enforced 等要么是复盘统计、
    要么与顶层字段重复，对决策是纯噪音，还会撑大提示词。
    """
    keep = {
        "max_leverage", "ai_max_leverage", "ai_strong_max_leverage",
        "strong_max_leverage", "strong_confidence_threshold",
        "max_open_positions", "max_risk_per_trade", "allow_bare_position",
    }
    return {k: v for k, v in (risk_envelope or {}).items() if k in keep}


# 非持仓标的只保留这几个字段作为「候选索引行」，够用来判断值不值得深挖。
# 完整 25 字段留给持仓标的和按需查询。
_INDEX_FIELDS = (
    "symbol", "last_price", "pct_24h",
    "trend_15m", "trend_1h", "funding_rate", "oi_usd",
)


def _slim_market(market: MarketBrief, held_symbols: set[str] | None = None) -> dict[str, Any]:
    """市场简报瘦身：errors 不进提示词（只进日志），metrics 剔除 null 字段。

    数据过期时压缩成一句可读提示，而不是把 HTTP 418 限流错误原样塞给模型。

    held_symbols 传入当前持仓标的集合时开启分层传递：
      - 持仓标的保留完整指标 —— 决策自己的仓位必须有全部数据，缺一项就可能误判止损位；
      - 其余标的压成一行索引（symbol/最新价/24h涨跌/趋势/资金费率/OI），
        只用于筛选候选，需要细节时让 Agent 调 query_symbol_detail 现查。
    这样既落实 config 里「AI 按需自己查询」的设计意图，也避免拿 1 小时前的
    缓存快照挤占实时查询的上下文预算。held_symbols 为 None 时保持全量（兼容旧调用）。
    """
    dump = market.model_dump()
    errors = dump.get("errors") or []
    if errors:
        # 只保留数据降级提示，不暴露运维噪音
        stale = [e for e in errors if "缓存快照" in str(e)]
        dump["data_note"] = ("; ".join(stale[:1]) if stale
                              else f"部分行情数据获取失败（{len(errors)} 项），以可用数据为准")
    dump.pop("errors", None)
    metrics = dump.get("metrics") or []
    if not isinstance(metrics, list):
        return dump
    clean = [{k: v for k, v in m.items() if v is not None}
             for m in metrics if isinstance(m, dict)]
    if held_symbols is None:
        dump["metrics"] = clean
        return dump
    held = set(held_symbols)
    dump["metrics"] = [
        m if m.get("symbol") in held
        else {k: m[k] for k in _INDEX_FIELDS if k in m}
        for m in clean
    ]
    if any(m.get("symbol") not in held for m in clean):
        dump["metrics_note"] = (
            "非持仓标的只给了索引行（用于筛选候选）。"
            "需要完整指标或K线时调 query_symbol_detail，拿到的是实时数据，比这里的快照更新鲜。")
    return dump


def _slim_onchain(onchain: OnchainBrief) -> dict[str, Any]:
    """链上简报瘦身：与 market/macro 保持一致，errors 不进提示词。

    此前三份简报里只有 onchain 直接 model_dump()，HTTP 错误原文会原样进提示词，
    既浪费 token 又可能让模型误读成市场信号。这里统一压成一句降级提示。
    """
    dump = onchain.model_dump()
    errors = dump.get("errors") or []
    if errors:
        dump["data_note"] = f"链上数据获取失败（{len(errors)} 项），以可用数据为准"
    dump.pop("errors", None)
    return dump


def _held_symbols(portfolio: dict[str, Any] | None,
                  execution_feedback: dict[str, Any] | None = None) -> set[str]:
    """从持仓与执行反馈里收集当前持有/挂单的标的，用于分层传递时保留完整指标。"""
    out: set[str] = set()
    for src in (portfolio, execution_feedback):
        if not isinstance(src, dict):
            continue
        for p in (src.get("positions") or []):
            if isinstance(p, dict) and p.get("symbol"):
                out.add(str(p["symbol"]))
    return out


def _slim_macro(macro: MacroBrief) -> dict[str, Any]:
    """宏观简报瘦身：risk_events_24h 过滤纯搜索标题，只保留可行动事件。

    errors 同样不进提示词，与 market / onchain 保持一致。
    """
    dump = macro.model_dump()
    errors = dump.get("errors") or []
    if errors:
        dump["data_note"] = f"宏观数据获取失败（{len(errors)} 项），以可用数据为准"
    dump.pop("errors", None)
    events = dump.get("risk_events_24h") or []
    if isinstance(events, list):
        # 保留含关键经济词/日期的条目，过滤纯网站标题
        kw = ("FOMC", "CPI", "PCE", "非农", "NFP", "降息", "加息", "美联储", "Fed", "利率")
        dump["risk_events_24h"] = [e for e in events if any(k in str(e) for k in kw)][:5]
    return dump


def _extract_query_symbols(fname: str, args: dict[str, Any]) -> list[str]:
    """从查询参数里取出本轮涉及的 symbol，用于"已查询清单"防重复查询。"""
    if fname == "query_symbol_detail":
        return [str(args.get("symbol") or "").strip().upper()]
    if fname == "query_symbol_cards":
        syms = args.get("symbols")
        if isinstance(syms, str):
            syms = [syms]
        return [str(s).strip().upper() for s in (syms or []) if s]
    return []


def _decision_evidence_schema() -> dict[str, Any]:
    """AI 开仓证据契约；静态与动态 submit schema 必须共用同一份定义。"""
    return {
        "type": "object",
        "description": "仅 open 必填。必须来自本轮 query_symbol_detail，不得凭空补时间或指标。",
        "properties": {
            "market_regime": {
                "type": "string",
                "enum": ["trend_up", "trend_down", "range", "volatile", "unknown"],
            },
            "higher_timeframe_trend": {
                "type": "string", "enum": ["up", "down", "range", "unknown"],
            },
            "supporting_evidence": {
                "type": "array", "minItems": 1, "items": {"type": "string"},
            },
            "counter_evidence": {"type": "array", "items": {"type": "string"}},
            "trigger_price": {"type": "number", "exclusiveMinimum": 0},
            "invalidation_price": {"type": "number", "exclusiveMinimum": 0},
            "invalidation_operator": {
                "type": "string", "enum": ["close_below", "close_above"],
            },
            "confirmation_interval": {
                "type": "string", "enum": ["1m", "5m", "15m", "1h", "4h", "1d"],
            },
            "risk_reward_ratio": {"type": "number", "minimum": 0},
            "chase_distance_pct": {"type": "number", "minimum": 0},
            "data_observed_at_ms": {"type": "integer", "exclusiveMinimum": 0},
            "confidence_basis": {"type": "string", "minLength": 1},
        },
        "required": [
            "market_regime", "higher_timeframe_trend", "supporting_evidence",
            "counter_evidence", "trigger_price", "invalidation_price",
            "invalidation_operator", "confirmation_interval", "risk_reward_ratio",
            "chase_distance_pct", "data_observed_at_ms", "confidence_basis",
        ],
    }


SUBMIT_DECISION_TOOL_SPEC = {
    "type": "function",
    "function": {
        "name": "submit_decision",
        "description": "提交最终交易决策。只有调用本工具才会被执行；不要输出裸 JSON 文本。",
        "parameters": {
            "type": "object",
            "properties": {
                "decision": {"type": "string", "enum": ["no_trade", "open", "close", "reduce", "adjust"]},
                "intents": {"type": "array", "items": {
                    "type": "object",
                    "properties": {
                        "symbol": {"type": "string"},
                        "action": {"type": "string", "enum": ["open", "close", "reduce", "adjust"]},
                        "side": {"type": "string", "enum": ["LONG", "SHORT"]},
                        "qty": {"type": "number", "minimum": 0},
                        "leverage": {"type": "integer", "minimum": 1, "maximum": 125},
                        "order_type": {"type": "string", "enum": ["MARKET", "LIMIT"]},
                        "order_source": {"type": "string", "enum": ["strategy", "ai"]},
                        "margin_mode": {"type": "string", "enum": ["ISOLATED", "CROSSED"]},
                        "ai_reason_type": {"type": "string"},
                        "limit_price": {"type": "number", "exclusiveMinimum": 0},
                        "limit_expires_at": {"type": "string"},
                        "stop_loss": {"type": "number"},
                        "take_profit": {"type": "number"},
                        "expected_holding_minutes": {"type": "integer", "minimum": 0},
                        "trade_level": {"type": "string", "enum": ["short", "hourly"]},
                        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "reasoning": {"type": "string"},
                        "strategy_refs": {"type": "array", "items": {"type": "string"}},
                        "strategy_id": {"type": "string"},
                        "invalidation": {"type": "string",
                                         "description": "（开仓必填）什么行情会证明本笔 thesis 错误，例如 '跌破前低 58000 即证伪'。复盘时据此判断是行情证伪还是拿错了。"},
                        "evidence": _decision_evidence_schema(),
                    },
                    "required": ["symbol", "action", "side"],
                }},
                "position_reviews": {"type": "array", "description":
                                     "对当前每个持仓的强制表态，必须覆盖 portfolio 里的全部 symbol。"
                                     "verdict: hold=继续持有 / reduce=减仓 / close=平仓 / adjust=调整止损止盈。"
                                     "无持仓时填 []。",
                                     "items": {"type": "object",
                                               "properties": {
                                                   "symbol": {"type": "string"},
                                                   "verdict": {"type": "string",
                                                               "enum": ["hold", "reduce", "close", "adjust"]},
                                                   "reason": {"type": "string"},
                                               },
                                               "required": ["symbol", "verdict"]}},
                "reasoning": {"type": "string"},
                "strategy_refs": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["decision", "intents", "position_reviews", "reasoning"],
        },
    },
}

REVIEW_TOOL_SPECS = [
    {
        "type": "function",
        "function": {
            "name": "query_review_library",
            "description": "查询历史亏损交易复盘库，获取最近亏损案例和教训，用于避免重复同样错误。需要谨慎交易时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "description": "返回案例数量，默认8"},
                },
                "required": [],
            },
        },
    },
]


# symbol 数量超过此阈值时改用描述约束：enum 会显著撑大 schema，反而涨 token
MAX_SYMBOL_ENUM = 60


def build_submit_decision_spec(space: ActionSpace | None = None) -> dict[str, Any]:
    """由 ActionSpace 生成 submit_decision 的 schema —— 动态 schema 的核心。

    allow_new=False 时 open 会从 enum 里彻底消失，模型根本不会面对它无权执行的选项。
    这比在 prompt 里写一百条"不要开仓"有效得多：前者是动作空间层面的约束，后者只是建议。

    剪枝只作用于 open：close / reduce / adjust 任何情况下都保留。
    存量管理必须有出口，否则会出现"只能开不能平"的死局。
    """
    if space is None:
        return copy.deepcopy(SUBMIT_DECISION_TOOL_SPEC)

    actions = list(space.allowed_actions)
    if space.allow_new:
        decision_desc = (f"提交最终交易决策。只有调用本工具才会被执行，不要输出裸 JSON 文本。"
                         f"本轮可开新仓，杠杆 1~{space.schema_max_leverage}x，"
                         f"剩余仓位槽位 {space.remaining_slots}/{space.max_open_positions}。")
    else:
        decision_desc = (f"提交最终交易决策。只有调用本工具才会被执行，不要输出裸 JSON 文本。"
                         f"本轮不允许新开仓（{space.allow_new_reason}）；"
                         f"但你仍必须管理已有持仓，close / reduce / adjust 全部可用。")

    if space.supported_symbols:
        pool = space.tradeable_symbols or space.supported_symbols
        if pool and len(pool) <= MAX_SYMBOL_ENUM:
            symbol_schema: dict[str, Any] = {
                "type": "string", "enum": list(pool),
                "description": "交易对，只能填列表内的值",
            }
        else:
            symbol_schema = {
                "type": "string",
                "description": f"交易对，必须是可交易列表内的 symbol（共 {len(pool) if pool else 0} 个）",
            }
    else:
        symbol_schema = {"type": "string", "description": "交易对，例如 BTCUSDT、ETHUSDT"}

    action_desc = "动作类型：" + ", ".join(actions)
    if not space.allow_new:
        action_desc += "（open 本轮不可用，不要尝试）"

    return {
        "type": "function",
        "function": {
            "name": "submit_decision",
            "description": decision_desc,
            "parameters": {
                "type": "object",
                "properties": {
                    "decision": {"type": "string", "enum": ["no_trade"] + actions},
                    "intents": {"type": "array", "items": {
                        "type": "object",
                        "properties": {
                            "symbol": symbol_schema,
                            "action": {"type": "string", "enum": actions, "description": action_desc},
                            "side": {"type": "string", "enum": list(VALID_SIDES)},
                            "qty": {"type": "number", "minimum": 0,
                                    "description": "数量；open/close/reduce 必须 > 0，adjust 可为 0"},
                            "leverage": {"type": "integer", "minimum": 1,
                                         "maximum": space.schema_max_leverage},
                            "order_type": {"type": "string", "enum": ["MARKET", "LIMIT"]},
                            "order_source": {"type": "string", "enum": ["strategy", "ai"]},
                            "margin_mode": {"type": "string", "enum": ["ISOLATED", "CROSSED"]},
                            "ai_reason_type": {"type": "string"},
                            "limit_price": {"type": "number", "exclusiveMinimum": 0},
                            "limit_expires_at": {"type": "string"},
                            "stop_loss": {"type": "number"},
                            "take_profit": {"type": "number"},
                            "expected_holding_minutes": {"type": "integer", "minimum": 0},
                            "trade_level": {"type": "string", "enum": ["short", "hourly"]},
                            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                            "reasoning": {"type": "string"},
                            "strategy_refs": {"type": "array", "items": {"type": "string"}},
                            "strategy_id": {"type": "string"},
                            "invalidation": {"type": "string",
                                             "description": "（开仓必填）什么行情会证明本笔 thesis 错误，"
                                                            "例如 '跌破前低 58000 即证伪'。复盘时据此判断是行情证伪还是拿错了。"},
                            "evidence": _decision_evidence_schema(),
                        },
                        "required": ["symbol", "action", "side"],
                    }},
                    "position_reviews": {"type": "array",
                                         "description": "对当前每个持仓的强制表态，必须覆盖 portfolio 里的全部 symbol。"
                                                        "verdict: hold=继续持有 / reduce=减仓 / close=平仓 / adjust=调整止损止盈。"
                                                        "无持仓时填 []。",
                                         "items": {"type": "object",
                                                   "properties": {
                                                       "symbol": {"type": "string"},
                                                       "verdict": {"type": "string",
                                                                   "enum": ["hold", "reduce", "close", "adjust"]},
                                                       "reason": {"type": "string"},
                                                   },
                                                   "required": ["symbol", "verdict"]}},
                    "reasoning": {"type": "string"},
                    "strategy_refs": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["decision", "intents", "position_reviews", "reasoning"],
            },
        },
    }


SYSTEM_PROMPT = """你是多 Agent 交易系统的 AI 自决策交易 Agent，负责独立自主决策，输出 order_source=ai 的订单。
你拥有按需行情查询工具，可以主动查询任意币安 USDT 永续合约的分钟级/小时级数据。
当前为模拟盘交易，但不设置成交数量目标。证据不足时选择 no_trade 是正确且完整的决策，不需要补偿上一轮没有交易。

工具使用规则：
1. 想交易或需要确认某只币时，先用 query_symbol_detail 查询真实 K 线、ATR、量能、资金费率、OI，再决定。
2. 不要使用没有查询过的 symbol，不要编造价格。
3. 可以用 query_top_symbols 查看当前成交额最高的合约。
4. 需要回顾历史教训时，可以调用 query_review_library 查看最近亏损案例复盘库，避免重复犯错。
5. 查询完成后，必须调用 submit_decision 工具提交最终决策；不要输出裸 JSON 文本。submit_decision 的参数结构等同下面的 JSON 格式：

最终 JSON 格式：
{"decision": "no_trade|open|close|reduce|adjust", "intents": [], "position_reviews": [], "reasoning": "...", "strategy_refs": []}
decision 含义：
- no_trade: 没有足够把握或没有符合条件的信号
- open: 开新仓
- close: 平掉已有仓位
- reduce: 减仓
- adjust: 调整止损/止盈
intents 仅在 decision != no_trade 时填写。每个 intent 字段：
{"symbol":"BTCUSDT","action":"open|close|reduce|adjust","side":"LONG|SHORT","qty":0.0,"leverage":1,"order_type":"MARKET|LIMIT",
"order_source":"ai","margin_mode":"ISOLATED","ai_reason_type":"trend_follow|range_fade|breakout|liquidity_sweep|news_event|macro",
"limit_price":null,"limit_expires_at":null,
"stop_loss":null,"take_profit":null,"expected_holding_minutes":60,"confidence":0.5,
"reasoning":"...","strategy_refs":[],"strategy_id":"ai:trend_follow:v1","trade_level":"short|hourly",
"invalidation":"15m收盘跌破结构低点即证伪",
"evidence":{"market_regime":"trend_up|trend_down|range|volatile|unknown","higher_timeframe_trend":"up|down|range|unknown",
"supporting_evidence":["..."],"counter_evidence":[],"trigger_price":0.0,"invalidation_price":0.0,
"invalidation_operator":"close_below|close_above","confirmation_interval":"1m|5m|15m|1h|4h|1d",
"risk_reward_ratio":0.0,"chase_distance_pct":0.0,"data_observed_at_ms":0,"confidence_basis":"..."}}
每条 intent 可以独立指定 action；当你需要同时平仓、开仓、调整时，分别写不同 action 的 intent。qty 对 adjust 可以为 0，对 open/close/reduce 必须 >0。

决策规则：
1. symbol 必须来自你刚刚查询到的真实数据，不得编造。
2. strategy_refs 只是策略库提供的参考案例，不是下单命令。策略通道由 strategy_trader 单独负责，你只做自己的独立判断。
2.1 如果没有 strategy ticket / signal 被 SignalScanner 触发，就不允许说“策略库支持做多/做空”或“策略库匹配”。strategy_refs 只是背景资料，只有当本轮确实有 strategy ticket 进入策略通道时，才可以使用对应策略作为依据。
3. 杠杆不得超过 risk_envelope.max_leverage，仓位数量不得超过 max_open_positions。
4. trade_level: 15分钟及以上级别交易填 "short"，小时及以上级别交易填 "hourly"。系统不做 1 分钟级高频，不要试图开分钟级短线仓。
5. trading_quota 只是已成交统计，不是目标、下限或催单信号；不得为了提高交易次数而降低证据标准。
6. no_trade 与 open/close/reduce/adjust 同等有效。没有完整、相互一致且新鲜的证据时必须 no_trade；不得因连续空窗、模拟盘或查询预算尚有剩余而凑单。
7. 强信心档：如果你对某个机会有强烈信心（confidence >= 0.75），可以使用更高杠杆和更高仓位（参考 risk_envelope 里的 strong_max_*），但必须同时设置止损 stop_loss；没有设置止损时只能按常规限制执行。
8. 新开仓必须同时设置初始止损 stop_loss 和初始止盈 take_profit，且方向正确：LONG 需 stop_loss<入场价<take_profit，SHORT 需 take_profit<入场价<stop_loss。缺止盈或止损的单会被风控直接拒绝。
8.1 止盈止损必须基于明确的技术分析理论给出，禁止拍脑袋填数字。可选依据（任选其一并在 reasoning 里写明）：ATR 波动止损（如 1.5~2 倍 ATR）、前高/前低结构位、关键支撑/阻力位、斐波那契回撤位、移动平均线、布林带上下轨、成交密集区/流动性区间。reasoning 必须写清「用什么理论 + 为什么这个价位」。
8.2 已有持仓的止损止盈不要无意义微调：只有当价格突破了某个关键技术位、趋势结构发生实质变化、或持仓盈亏/时间因素需要收紧保护时，才允许用 adjust 调整 SL/TP。单纯因为「价格动了一点」就去重设一个几乎相同的止损，属于无效操作，禁止这样做。若你认为当前止损仍然有效，position_reviews 应给 hold，不要发 adjust intent。
9. 你可以下限价单：order_type=LIMIT 时必须同时给出 limit_price 和 limit_expires_at（ISO8601 UTC），例如 2025-08-31T12:00:00Z；否则会被拒绝。
10. 所有你产出的订单 order_source 一律填 "ai"，margin_mode 默认填 "ISOLATED"。单笔最大风险不超过会话权益的 2%，超过时风控会自动缩减数量。
11. 如果 portfolio 中某 symbol 已经存在 source=strategy 的仓位，本轮不要对该 symbol 开新仓，避免与策略通道冲突。
12. 如果 supported_symbols 列表非空，你只能交易该列表内的 symbol；列表为空时也以 query_top_symbols / 实际下单结果为准，不要使用无法成交的虚构 symbol。
13. 每轮必须通过 submit_decision 的 position_reviews 字段对当前每个持仓明确表态
    （hold/reduce/close/adjust + 理由）；意图与表态必须一致，例如 position_reviews 声明 close 就必须有对应 close intent。
    同时每个 open intent 必须写明 strategy_id、invalidation 和 evidence。evidence 的 data_observed_at_ms
    必须原样使用本轮 query_symbol_detail 返回值；不得伪造当前时间。支持证据至少一项，反证即使为空也要显式填 []。
14. 输出语言：你的思考过程（思维链、工具调用前的分析）可以用英文；但 submit_decision 里最终提交的
    reasoning、每条 intent 的 reasoning、position_reviews 的 reason、invalidation 等所有面向用户的决策说明文字，
    一律用简体中文书写。数字、交易对、专业术语可保留英文。"""


def _holding_pressure_notes(portfolio: dict[str, Any] | None) -> list[str]:
    """Compile 环节：找出"持有时间已超过自己设定的预期"的持仓，强制模型给出新理由或离场。

    这是把 position_reviews 从"形式表态"变成"决策压力"的关键：超时就是触发点。
    """
    notes: list[str] = []
    for p in (portfolio or {}).get("positions", []):
        if not isinstance(p, dict):
            continue
        symbol = str(p.get("symbol") or "")
        opened = p.get("opened_at")
        expected = p.get("expected_holding_minutes")
        if not symbol or not opened or expected is None:
            continue
        try:
            dt = datetime.fromisoformat(str(opened).replace("Z", "+00:00"))
            held = (datetime.now(timezone.utc) - dt).total_seconds() / 60.0
        except Exception:
            continue
        if held > float(expected):
            notes.append(
                f"{symbol} 已持有 {held:.0f} 分钟，超过你设定的预期持有时间 {float(expected):.0f} 分钟。"
                f"position_reviews 里必须给出继续持有的新理由；给不出来就应 close/reduce，不要无条件扛单。")
    return notes


def build_user_prompt(market: MarketBrief, macro: MacroBrief, onchain: OnchainBrief,
                      strategy: StrategyRefs, risk_envelope: dict[str, Any],
                      portfolio: dict[str, Any] | None = None,
                      trading_quota: dict[str, Any] | None = None,
                      supported_symbols: list[str] | None = None,
                      execution_feedback: dict[str, Any] | None = None,
                      action_space: ActionSpace | None = None,
                      query_budget: int = DEFAULT_QUERY_BUDGET) -> str:
    instruction = ("先使用工具查询你认为需要了解的品种，再调用 submit_decision 提交最终决策。"
                   "execution_feedback 是上一轮每个 intent 的最终命运和当前持仓；"
                   "你必须对仍然存活的上一轮 thesis 明确表态：继续持有 / 减仓 / 平仓 / 调整止损止盈。"
                   "如果 thesis 已被行情否定且你无法给出新的持有理由，就应主动平仓。"
                   "当前持仓的 stop_loss / take_profit 就是交易所里真实挂着的止损止盈价："
                   "除非有明确技术依据（突破关键位、ATR 变化、趋势结构反转等）需要移动，否则不要重复提交几乎相同的 adjust。"
                   "不设成交数量目标；没有完整且一致的证据时直接 no_trade，不要为了模拟盘或交易空窗凑单。"
                   f"查询预算每轮 {query_budget} 次：先用 query_symbol_cards 宽扫候选，"
                   "再对筛出的 2~3 只调 query_symbol_detail 深挖，不要逐只 detail。")
    if action_space is not None:
        instruction += ("若 submit_decision 返回校验错误，请按 issues 修正；"
                        "如果无法取得真实、完整证据，应提交有明确原因的 no_trade。")
    pressure = _holding_pressure_notes(portfolio)
    if pressure:
        instruction += "\n\n持仓超时提醒（必须逐条处理，不能无视）：\n" + "\n".join("⚠ " + n for n in pressure)
    payload = {
        "action_space": {
            "allowed_actions": action_space.allowed_actions if action_space else None,
            "allow_new": action_space.allow_new if action_space else None,
            "allow_new_reason": action_space.allow_new_reason if action_space else None,
            "summary": action_space.summary() if action_space else None,
        },
        "market_brief": _slim_market(market, _held_symbols(portfolio, execution_feedback)),
        "macro_brief": _slim_macro(macro),
        "onchain_brief": _slim_onchain(onchain),
        "strategy_refs": strategy.model_dump(),
        "portfolio": portfolio or {"cash": 0, "positions": []},
        "trading_quota": trading_quota or {"short_done_today": 0, "hourly_done_today": 0,
                                             "enabled": False, "policy": "no_cap_evidence_based"},
        "risk_envelope": _slim_risk_envelope(risk_envelope),
        "supported_symbols": list(supported_symbols or []),
        "execution_feedback": execution_feedback or {},
        "holding_pressure": pressure,
        "strategy_channel": {
            "positions": [p for p in (portfolio or {}).get("positions", [])
                          if str(p.get("source", "")) == "strategy"],
            "tickets": strategy.model_dump().get("refs", []),
        },
        "instruction": instruction,
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


class DecisionValidationError(ValueError):
    """submit_decision 参数不合法。issues 原样回传模型，供其自我修正后重试。"""

    def __init__(self, issues: list[str], hint: str = ""):
        self.issues = issues
        self.hint = hint
        super().__init__("; ".join(issues))


VALID_ACTIONS = ("open", "close", "reduce", "adjust")
VALID_DECISIONS = ("no_trade",) + VALID_ACTIONS
REQUIRED_OPEN_EVIDENCE = (
    "market_regime", "higher_timeframe_trend", "supporting_evidence",
    "counter_evidence", "trigger_price", "invalidation_price",
    "invalidation_operator", "confirmation_interval", "risk_reward_ratio",
    "chase_distance_pct", "data_observed_at_ms", "confidence_basis",
)


def _validate_decision_payload(payload: dict[str, Any]) -> list[str]:
    """返回所有问题；空列表表示通过。不做任何静默修正——静默丢单会让模型误以为提交成功。"""
    issues: list[str] = []
    decision = payload.get("decision")
    if decision not in VALID_DECISIONS:
        issues.append(f"decision={decision!r} 非法，必须是 {list(VALID_DECISIONS)} 之一")
    raw_intents = payload.get("intents")
    if not isinstance(raw_intents, list):
        issues.append("intents 必须是数组（无操作请填 [] 并把 decision 设为 no_trade）")
        return issues
    if decision == "no_trade" and raw_intents:
        issues.append("decision=no_trade 时 intents 必须为空数组，请把意图写进 decision")
    if decision != "no_trade" and not raw_intents:
        issues.append(f"decision={decision} 但 intents 为空，请填写至少一条 intent，或改 decision=no_trade")
    for i, it in enumerate(raw_intents):
        if not isinstance(it, dict):
            issues.append(f"intents[{i}] 必须是对象")
            continue
        action = str(it.get("action") or decision or "open")
        if action not in VALID_ACTIONS:
            issues.append(f"intents[{i}].action={action!r} 非法，必须是 {list(VALID_ACTIONS)} 之一")
            continue
        if not str(it.get("symbol") or "").strip():
            issues.append(f"intents[{i}] 缺少 symbol")
        if action == "open":
            if not str(it.get("strategy_id") or "").strip():
                issues.append(f"intents[{i}]（{it.get('symbol')} open）缺少 strategy_id")
            if not str(it.get("invalidation") or "").strip():
                issues.append(f"intents[{i}]（{it.get('symbol')} open）缺少 invalidation："
                              f"必须写明什么行情会证伪这笔 thesis，例如 '跌破前低 58000 即证伪'")
            evidence = it.get("evidence")
            if not isinstance(evidence, dict):
                issues.append(f"intents[{i}]（{it.get('symbol')} open）缺少结构化 evidence")
            else:
                missing = [key for key in REQUIRED_OPEN_EVIDENCE if key not in evidence]
                if missing:
                    issues.append(f"intents[{i}].evidence 缺少字段: {','.join(missing)}")
                support = evidence.get("supporting_evidence")
                if not isinstance(support, list) or not any(str(item).strip() for item in support):
                    issues.append(f"intents[{i}].evidence.supporting_evidence 至少需要一项真实证据")
                if not isinstance(evidence.get("counter_evidence"), list):
                    issues.append(f"intents[{i}].evidence.counter_evidence 必须是数组，无反证时填 []")
        side = str(it.get("side") or "").upper()
        if side not in ("LONG", "SHORT"):
            issues.append(f"intents[{i}].side={it.get('side')!r} 非法，必须是 LONG 或 SHORT")
        try:
            qty = float(it.get("qty") or 0.0)
        except (TypeError, ValueError):
            issues.append(f"intents[{i}].qty={it.get('qty')!r} 不是数字")
            continue
        if action != "adjust" and qty <= 0:
            issues.append(
                f"intents[{i}]（{it.get('symbol')} {action}）qty 必须 > 0，当前为 {qty}。"
                f"只想调整止损止盈请用 action=adjust（qty 可为 0）；确实要 {action} 请给出具体数量。"
            )

    raw_reviews = payload.get("position_reviews")
    if raw_reviews is None:
        issues.append("缺少必填字段 position_reviews：对当前每个持仓的表态数组，无持仓填 []")
    elif not isinstance(raw_reviews, list):
        issues.append("position_reviews 必须是数组")
    else:
        for i, r in enumerate(raw_reviews):
            if not isinstance(r, dict):
                issues.append(f"position_reviews[{i}] 必须是对象")
                continue
            if not str(r.get("symbol") or "").strip():
                issues.append(f"position_reviews[{i}] 缺少 symbol")
            if str(r.get("verdict") or "").lower() not in ("hold", "reduce", "close", "adjust"):
                issues.append(f"position_reviews[{i}].verdict={r.get('verdict')!r} 非法，"
                              f"必须是 hold/reduce/close/adjust")
    return issues


def _check_open_query_provenance(intents: list[Any],
                                 query_evidence: dict[str, dict[str, Any]]) -> list[str]:
    """证明 AI 开仓证据确实来自本轮成功的 detail 查询，而不是模型自造时间戳。"""
    issues: list[str] = []
    for i, intent in enumerate(intents):
        if getattr(intent, "action", None) != "open":
            continue
        symbol = str(getattr(intent, "symbol", "") or "").strip().upper()
        quality = query_evidence.get(symbol)
        if quality is None:
            issues.append(f"intents[{i}] {symbol} 未完成本轮 query_symbol_detail，禁止开仓")
            continue
        if not quality.get("ok"):
            issues.append(f"intents[{i}] {symbol} 行情质量未通过: {quality.get('issues') or ['unknown']}")
            continue
        evidence = getattr(intent, "evidence", None)
        evidence_at = getattr(evidence, "data_observed_at_ms", None)
        observed_at = quality.get("observed_at_ms")
        if not evidence_at or not observed_at or int(evidence_at) != int(observed_at):
            issues.append(
                f"intents[{i}] {symbol} evidence.data_observed_at_ms 与本轮查询不一致"
            )
    return issues


def _build_decision(payload: dict[str, Any], *, strict: bool) -> tuple[TradeDecision, list[str]]:
    """构建 TradeDecision。

    strict=True：任何问题都抛 DecisionValidationError，交给模型重试。
    strict=False：降级为 no_trade 并返回问题清单，用于重试次数耗尽后保证收敛。
    """
    if not strict:
        # 宽松路径没有重试机会：position_reviews 缺省按"无表态"处理，不因此误伤合法决策
        payload.setdefault("position_reviews", [])
    issues = _validate_decision_payload(payload)
    if issues:
        if strict:
            raise DecisionValidationError(
                issues,
                hint="请修正上述问题后重新调用 submit_decision；若无法取得真实证据，请提交 no_trade。",
            )
        return TradeDecision(
            decision="no_trade",
            reasoning=(payload.get("reasoning") or "")
                      + f" （参数校验未通过，已降级不交易：{'；'.join(issues)}）",
            strategy_refs=list(payload.get("strategy_refs") or []),
        ), issues
    for it in payload.get("intents") or []:
        if isinstance(it, dict):
            it.setdefault("action", payload.get("decision") or "open")
            if it.get("side"):
                it["side"] = str(it["side"]).upper()
    for r in payload.get("position_reviews") or []:
        if isinstance(r, dict) and r.get("symbol"):
            r["symbol"] = str(r["symbol"]).strip().upper()
    payload.setdefault("strategy_refs", [])
    payload.setdefault("position_reviews", [])
    try:
        return TradeDecision.model_validate(payload), []
    except Exception as e:
        if strict:
            raise DecisionValidationError([f"schema 校验失败: {type(e).__name__}: {e}"])
        return TradeDecision(decision="no_trade",
                             reasoning=f"决策解析失败，已降级不交易: {e}"), [str(e)]


def _pack_tool_result(result: Any, max_chars: int = 8000) -> str:
    """结构化截断：宁可丢字段，也不返回半截 JSON——模型会把语法非法的 JSON 当乱码。"""
    text = json.dumps(result, ensure_ascii=False)
    if len(text) <= max_chars:
        return text
    if isinstance(result, dict):
        trimmed = dict(result)
        for key in ("recent_candles", "candles", "symbols", "metrics", "klines"):
            trimmed.pop(key, None)
        if len(json.dumps(trimmed, ensure_ascii=False)) <= max_chars:
            trimmed["_omitted"] = "K线/列表字段已省略（超出上下文预算）；如需原始K线请用更小的 limit 重新查询"
            return json.dumps(trimmed, ensure_ascii=False)
        keep = {k: result[k] for k in ("error", "symbol", "interval", "last_price") if k in result}
        keep["_truncated"] = True
        keep["_note"] = f"返回体超过 {max_chars} 字符预算已被裁剪，请缩小 limit 或换个品种查询"
        return json.dumps(keep, ensure_ascii=False)
    return json.dumps({"_truncated": True, "_note": f"返回体超过 {max_chars} 字符预算"},
                      ensure_ascii=False)


def _extract_decision_json(content: str) -> str:
    """从 LLM 输出中稳健提取 JSON 对象。

    DeepSeek 有时会在 JSON 前面写中文分析，偶尔也会用 ```json 代码块包裹，
    直接 model_validate_json 会失败。这里先剥掉外层，再取第一个 { 到最后一个 }。
    """
    raw = content.strip()
    if raw.startswith("```"):
        # 去掉 ```json / ``` 围栏
        lines = raw.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        raw = "\n".join(lines).strip()

    start = raw.find("{")
    end = raw.rfind("}")
    if start != -1 and end != -1 and end > start:
        return raw[start:end + 1]
    return raw
def _log_llm_round(db: AuditDatabase, logger: JsonlLogger, run_id: str, model: str,
                   messages: list[dict[str, Any]], message: dict[str, Any],
                   raw: dict[str, Any], latency_ms: int, status: str, error: Exception | None) -> None:
    call_id = str(uuid.uuid4())
    prompt_file = logger.log_llm("orchestrator", {
        "ts": utc_now(), "call_id": call_id, "messages": messages,
    }).as_posix()
    completion_file = logger.log_llm("orchestrator", {
        "ts": utc_now(), "call_id": call_id, "message": message,
        "raw": raw,
    }).as_posix()
    usage = raw.get("usage", {}) if isinstance(raw, dict) else {}
    db.log_llm_call(
        call_id=call_id, run_id=run_id, agent_name="orchestrator", model=model,
        started_at=utc_now(), ended_at=utc_now(),
        prompt_tokens=int(usage.get("prompt_tokens") or 0),
        completion_tokens=int(usage.get("completion_tokens") or 0),
        cost_estimate=0.0, latency_ms=latency_ms, status=status,
        error=str(error) if error else None,
        prompt_file=prompt_file, completion_file=completion_file,
    )


def run_orchestrator(*, llm: LLMClient, model: str, recorder: AgentRecorder,
                     db: AuditDatabase, logger: JsonlLogger, run_id: str,
                     market: MarketBrief, macro: MacroBrief, onchain: OnchainBrief,
                     strategy: StrategyRefs, risk_envelope: dict[str, Any],
                     portfolio: dict[str, Any] | None = None,
                     trading_quota: dict[str, Any] | None = None,
                     supported_symbols: list[str] | None = None,
                     execution_feedback: dict[str, Any] | None = None,
                     action_space: ActionSpace | None = None,
                     prices: dict[str, float] | None = None,
                     query_budget: int = DEFAULT_QUERY_BUDGET) -> TradeDecision:
    query_budget = max(1, int(query_budget))
    user_prompt = build_user_prompt(market, macro, onchain, strategy, risk_envelope, portfolio,
                                    trading_quota, supported_symbols=supported_symbols,
                                    execution_feedback=execution_feedback,
                                    action_space=action_space,
                                    query_budget=query_budget)
    tools = TOOL_SPECS + REVIEW_TOOL_SPECS + [build_submit_decision_spec(action_space)]
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]
    decision: TradeDecision | None = None
    last_error: Exception | None = None
    submit_attempts = 0
    query_used = 0
    queried: list[str] = []
    query_evidence: dict[str, dict[str, Any]] = {}
    query_details: dict[str, dict[str, Any]] = {}
    force_submit = False
    decision_was_downgraded = False

    for round_no in range(MAX_ROUNDS):
        started = time.monotonic()
        # force_submit 只在「下一轮」的 tool_choice 上生效，而 range(MAX_ROUNDS) 没有下一轮；
        # 加上模型通常每轮只调 1 次工具（5 轮共约 5 次，够不着 8 的预算），
        # 单靠预算触发的 force_submit 事实上永远不可能生效——轮次被 continue 走空，
        # 必然落到末尾的「主Agent连续多轮未输出最终JSON」。
        # 所以这里在最后一轮无条件兜底：钉死 submit_decision，保证本轮一定有结论，
        # 哪怕结论是一句有明确原因的 no_trade，也远好过一句笼统的超时报错。
        is_final_round = round_no == MAX_ROUNDS - 1
        try:
            # 预算耗尽后把模型钉死在 submit_decision 上，保证本轮一定有结论而不是拖成 no_trade
            choice: Any = ({"type": "function", "function": {"name": "submit_decision"}}
                           if (force_submit or is_final_round) else "auto")
            raw = llm.chat(messages, tools=tools, tool_choice=choice)
            status = "ok"
            error = None
        except Exception as e:
            status = "error"
            error = e
            last_error = e
            raw = {"choices": [], "usage": {}, "error": str(e)}
            _log_llm_round(db, logger, run_id, model, messages, {}, raw,
                           int((time.monotonic() - started) * 1000), status, error)
            break

        latency_ms = int((time.monotonic() - started) * 1000)
        choices = raw.get("choices") or []
        message = choices[0].get("message", {}) if choices else {}
        _log_llm_round(db, logger, run_id, model, messages, message, raw, latency_ms, status, error)

        tool_calls = message.get("tool_calls") or []
        if tool_calls:
            # 必须完整回传 assistant message（含 reasoning_content/tool_calls）
            messages.append(message)
            submitted = False
            # 所有 tool_calls 都必须给出 tool 响应，否则违反协议下一轮会 400
            for tc in tool_calls:
                fname = tc.get("function", {}).get("name", "")
                try:
                    args = json.loads(tc.get("function", {}).get("arguments") or "{}")
                except Exception:
                    args = {}
                if fname == "submit_decision":
                    submit_attempts += 1
                    # 前 MAX_STRICT_SUBMITS 次严格纠错并回传问题清单，之后降级保证本轮收敛。
                    # 最后一轮不再严格纠错：此时已没有下一轮可重试，严格只会把本轮拖成
                    # "未产出决策"。改为宽松解析后，不合规的内容由下方统一降级为 no_trade
                    # 并记 decision_downgraded 事件，不会放行违规交易。
                    strict = submit_attempts <= MAX_STRICT_SUBMITS and not is_final_round
                    try:
                        decision, issues = _build_decision(args, strict=strict)
                        if not issues:
                            issues = _check_open_query_provenance(decision.intents, query_evidence)
                            if issues and strict:
                                raise DecisionValidationError(
                                    issues,
                                    hint="请先完成本轮 detail 查询并原样引用其数据时点；"
                                         "无法取得合格数据时提交 no_trade。",
                                )
                        if not issues:
                            policy_checks = [
                                check
                                for intent in decision.intents
                                for check in (
                                    evaluate_open_intent(intent)
                                    + evaluate_portfolio_intent(
                                        intent, portfolio, query_details, db,
                                    )
                                )
                            ]
                            decision = decision.model_copy(update={"policy_checks": policy_checks})
                            issues = enforced_issues(policy_checks)
                            if issues and strict:
                                raise DecisionValidationError(
                                    issues,
                                    hint="决策质量检查未通过；可修正证据，或提交 no_trade。",
                                )
                        # 业务规则校验：与动态 schema、risk.py 运行时闸门共用同一个谓词，
                        # 三者不会漂移。错误回传给模型重试，服从率远高于 prompt 禁令。
                        if not issues and action_space is not None:
                            issues = action_space.check_decision(decision.intents, prices)
                            if not issues:
                                # 持仓必答：每个持仓必须表态，声称 close/reduce 必须有对应 intent
                                issues = action_space.check_position_reviews(
                                    decision.position_reviews, decision.intents)
                            if issues and strict:
                                raise DecisionValidationError(
                                    issues,
                                    hint="以上是本轮硬性约束（不是建议）。请逐条修正后重新提交；"
                                         "若没有满足约束的机会，请提交有明确原因的 no_trade。")
                    except DecisionValidationError as e:
                        result = {
                            "error": "submit_decision 未通过校验，请修正后重新提交",
                            "issues": e.issues,
                            "hint": e.hint,
                            "attempt": f"{submit_attempts}/{MAX_STRICT_SUBMITS + 1}",
                        }
                        messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                         "content": _pack_tool_result(result)})
                        continue
                    except Exception as e:
                        # 校验器自身异常时必须 fail-closed。过去这里重新宽松解析原 payload，
                        # 反而可能把尚未经过业务校验的 open 放行。
                        issues = [f"decision validation runtime error: {type(e).__name__}: {e}"]
                        decision = TradeDecision(
                            decision="no_trade",
                            reasoning=(args.get("reasoning") or "")
                                      + " （决策校验器异常，已安全降级不交易）",
                        )
                        db.log_risk_event(
                            event_type="decision_downgraded", severity="critical",
                            details=issues[0], action_taken="no_trade",
                        )
                        last_error = e
                        decision_was_downgraded = True
                        submitted = True
                        break

                    last_error = None
                    submitted = True
                    if issues:
                        decision_was_downgraded = True
                        db.log_risk_event(
                            event_type="decision_downgraded", severity="warn",
                            details=f"submit_decision 重试 {submit_attempts} 次后降级不交易：{'；'.join(issues)}",
                            action_taken="no_trade")
                        if decision.decision != "no_trade":
                            decision = TradeDecision(
                                decision="no_trade",
                                reasoning=(decision.reasoning or "")
                                          + f" （业务校验未通过，已降级不交易：{'；'.join(issues)}）",
                                strategy_refs=list(decision.strategy_refs),
                            )
                    break
                elif fname in QUERY_TOOL_NAMES:
                    if query_used >= query_budget:
                        # 预算耗尽：不再放行任何查询，并钉死下一轮 tool_choice 为 submit_decision
                        force_submit = True
                        result = {"error": f"查询预算已用尽（{query_budget}/{query_budget}）。"
                                          "请基于现有数据直接调用 submit_decision 提交决策，不要再查询。"}
                    else:
                        result = execute_tool(fname, args)
                        query_used += 1
                        for s in _extract_query_symbols(fname, args):
                            if s and s not in queried:
                                queried.append(s)
                        if isinstance(result, dict) and not result.get("error"):
                            if fname == "query_symbol_detail":
                                symbol = str(result.get("symbol") or "").strip().upper()
                                quality = result.get("data_quality")
                                if symbol and isinstance(quality, dict):
                                    query_evidence[symbol] = dict(quality)
                                    query_details[symbol] = dict(result)
                            result["_budget"] = {
                                "used": query_used,
                                "limit": query_budget,
                                "queried": list(queried),
                                "note": "以上为本轮已查询品种，直接复用结论，不要重复查询",
                            }
                elif fname == "query_review_library":
                    try:
                        result = lessons.query_review_library(db, limit=int(args.get("limit") or 8))
                    except Exception as e:
                        result = {"error": f"{type(e).__name__}: {e}"}
                else:
                    result = execute_tool(fname, args)
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "content": _pack_tool_result(result)})
            if submitted:
                break
            continue

        content = (message.get("content") or "").strip()
        if content:
            candidate = _extract_decision_json(content)
            try:
                payload = json.loads(candidate)
                decision, issues = _build_decision(payload, strict=False)
                last_error = None
                if not issues:
                    issues = _check_open_query_provenance(decision.intents, query_evidence)
                if not issues:
                    policy_checks = [
                        check
                        for intent in decision.intents
                        for check in (
                            evaluate_open_intent(intent)
                            + evaluate_portfolio_intent(intent, portfolio, query_details, db)
                        )
                    ]
                    decision = decision.model_copy(update={"policy_checks": policy_checks})
                    issues = enforced_issues(policy_checks)
                # 裸 JSON 同样要过业务规则与持仓必答校验；这里无法回传重试，
                # 任何问题都只能 fail-closed 到 no_trade。
                if not issues and action_space is not None:
                    issues = action_space.check_decision(decision.intents, prices)
                    if not issues:
                        issues = action_space.check_position_reviews(
                            decision.position_reviews, decision.intents)
                if issues:
                    decision_was_downgraded = True
                    decision = TradeDecision(
                        decision="no_trade",
                        reasoning=(decision.reasoning or "")
                                  + f" （裸输出未过业务校验，已降级不交易：{'；'.join(issues)}）",
                        strategy_refs=list(decision.strategy_refs),
                    )
                    db.log_risk_event(
                        event_type="decision_downgraded", severity="warn",
                        details=f"裸 JSON 输出降级不交易：{'；'.join(issues)}",
                        action_taken="no_trade")
            except Exception as e:
                decision = TradeDecision(decision="no_trade", reasoning=f"LLM输出解析失败，自动不交易: {e}")
                last_error = e
            break
        elif tool_calls:
            # 理论上前面的 if tool_calls 已经处理，这里兜底
            continue
        else:
            # LLM 可能因为 reasoning 太长被 max_tokens 截断，content 为空。
            # 先给一次“直接输出JSON”的提示，避免空转；仍无效则标记本轮不交易。
            finish_reason = (raw.get("choices", [{}])[0].get("finish_reason") or "unknown")
            if finish_reason == "length" and round_no < 3:
                messages.append({
                    "role": "user",
                    "content": "注意：你上一轮因思考过长被截断。请不要再输出长分析，直接输出完整最终决策 JSON。",
                })
                continue
            last_error = RuntimeError(f"LLM未输出有效决策，finish_reason={finish_reason}")
            decision = TradeDecision(
                decision="no_trade",
                reasoning=f"LLM输出为空或截断，自动不交易。finish_reason={finish_reason}",
            )
            break

    if decision is None:
        if last_error is None:
            last_error = RuntimeError("主Agent连续多轮未输出最终JSON（可能一直在调用工具或被截断）")
        decision = TradeDecision(
            decision="no_trade",
            reasoning=f"LLM未产出有效决策，自动不交易。last_error={last_error}",
        )

    recorder.record(
        agent_name="ai_trader",
        model=model,
        role="discretionary_trader",
        input_summary=user_prompt[:500],
        output=decision,
        error=last_error,
        run_id=run_id,
    )
    if not decision_was_downgraded and last_error is None:
        db.resolve_risk_incidents_by_type("decision_downgraded")
    return decision
