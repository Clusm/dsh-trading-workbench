"""本地控制台 FastAPI 应用。"""
from __future__ import annotations

import asyncio
import html
import json
import math
import os
import secrets
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from core.service import TradingService
from data.market_query import get_live_prices
from data.price_stream import PriceStream
from exchange.event_inbox import ExchangeEventInbox
from exchange.user_stream import BinanceUserDataStream

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


# ---- 看板读缓存：页面 5~15 秒轮询一次，不必每次都打交易所/扫库 ----
# 1) broker.state 短缓存（账户+持仓两个签名请求很贵，代理下 1-3 秒/次）
#    持仓数量/开仓价/保证金很少变化，15 秒缓存足够；最新价走公开行情接口，不受影响
_BROKER_STATE_TTL = 15.0
# 刷新函数内部会再拿同一把锁（冷启动同步穿透时就是从锁里调进去的），必须可重入
_BROKER_STATE_LOCK = threading.RLock()
_BROKER_STATE_CACHE: dict[str, Any] = {"ts": 0.0, "state": None, "refreshing": False}


def _fetch_broker_state(service: TradingService) -> dict[str, Any]:
    """读取一次 broker.state；该属性内部会请求账户和持仓接口。"""
    raw_state = service.broker.state
    return raw_state if isinstance(raw_state, dict) else {}


def _refresh_broker_state(service: TradingService) -> None:
    """后台刷新账户快照；失败时保留最后一次有效值，避免页面瞬间清空。"""
    try:
        state = _fetch_broker_state(service)
        if not state.get("error"):
            with _BROKER_STATE_LOCK:
                _BROKER_STATE_CACHE["ts"] = time.monotonic()
                _BROKER_STATE_CACHE["state"] = state
    finally:
        with _BROKER_STATE_LOCK:
            _BROKER_STATE_CACHE["refreshing"] = False


def _broker_state_cached(service: TradingService, max_age: float = _BROKER_STATE_TTL) -> dict[str, Any]:
    """账户快照采用 stale-while-revalidate，慢交易所请求不再阻塞页面轮询。"""
    now = time.monotonic()
    with _BROKER_STATE_LOCK:
        cached = _BROKER_STATE_CACHE["state"]
        fresh = cached is not None and now - _BROKER_STATE_CACHE["ts"] < max_age
        if fresh:
            return cached
        if cached is not None:
            if not _BROKER_STATE_CACHE["refreshing"]:
                _BROKER_STATE_CACHE["refreshing"] = True
                threading.Thread(target=_refresh_broker_state, args=(service,), daemon=True).start()
            return cached

    # 首次启动还没有可展示的数据，只允许这一轮同步穿透；之后全部后台更新。
    state = _fetch_broker_state(service)
    if not state.get("error"):
        with _BROKER_STATE_LOCK:
            _BROKER_STATE_CACHE["ts"] = time.monotonic()
            _BROKER_STATE_CACHE["state"] = state
    return state


# 2) 保护单（SL/TP 触发价）缓存：挂单后价格基本不变，30 秒刷新足够；
#    旧逻辑每个持仓一次 openAlgoOrders 签名请求，是 /api/status 卡顿的主因之一
_ALGOS_TTL = 30.0
_ALGOS_LOCK = threading.RLock()
_ALGOS_CACHE: dict[str, Any] = {"ts": 0.0, "by_symbol": {}, "refreshing": False}

# 3) 首轮未跑循环时的通道状态兜底缓存。
#    决策循环首轮要跑 LLM + 特征工程，可能好几分钟才有结果；这段时间里页面
#    每个请求都会走一次兜底（账户+行情+通道同步，约 4 秒）。改成命中即返回、
#    过期后台刷新后，页面不再被首轮循环拖慢。
_CHANNEL_FALLBACK_TTL = 60.0
_CHANNEL_FALLBACK_LOCK = threading.RLock()
_CHANNEL_FALLBACK_CACHE: dict[str, Any] = {"ts": 0.0, "state": None, "refreshing": False}


def _refresh_channel_fallback(service: TradingService) -> None:
    """后台重算通道兜底状态；失败时保留上一次结果，页面照常显示。"""
    try:
        state = _broker_state_cached(service)
        symbols = [str(p.get("symbol")) for p in state.get("positions", [])]
        prices = get_live_prices(symbols) if symbols else {}
        ch_state = service.channels.sync(
            prices, session_equity=_session_equity_from_state(service, state), read_only=True)
        with _CHANNEL_FALLBACK_LOCK:
            _CHANNEL_FALLBACK_CACHE["ts"] = time.monotonic()
            _CHANNEL_FALLBACK_CACHE["state"] = ch_state
    except Exception:
        pass
    finally:
        with _CHANNEL_FALLBACK_LOCK:
            _CHANNEL_FALLBACK_CACHE["refreshing"] = False


def _channel_fallback(service: TradingService) -> dict[str, Any]:
    """通道兜底：缓存命中直接返回，过期则返回旧值并在后台刷新，不阻塞页面请求。

    注意：首次必须同步取一次。缓存为空就返回 {} 会让两个通道卡片显示 0.00，
    看起来像通道资金全丢了，实际上只是后台线程还没算完。
    """
    now = time.monotonic()
    with _CHANNEL_FALLBACK_LOCK:
        state = _CHANNEL_FALLBACK_CACHE["state"]
        if state is None:
            # 冷启动：这一轮同步穿透，之后全部后台刷新
            _CHANNEL_FALLBACK_CACHE["refreshing"] = True
            _refresh_channel_fallback(service)
            return _CHANNEL_FALLBACK_CACHE["state"] or {}
        if now - _CHANNEL_FALLBACK_CACHE["ts"] < _CHANNEL_FALLBACK_TTL:
            return state
        if not _CHANNEL_FALLBACK_CACHE["refreshing"]:
            _CHANNEL_FALLBACK_CACHE["refreshing"] = True
            threading.Thread(target=_refresh_channel_fallback, args=(service,), daemon=True).start()
        return state


def _fetch_algo_prices(service: TradingService) -> None:
    by_symbol: dict[str, dict[str, float]] = {}
    try:
        for algo in service.broker._open_algos():
            otype = str(algo.get("orderType") or "")
            if otype in ("STOP_MARKET", "TAKE_PROFIT_MARKET"):
                try:
                    by_symbol.setdefault(str(algo.get("symbol") or ""), {})[otype] = \
                        float(algo.get("triggerPrice") or 0.0)
                except (TypeError, ValueError):
                    continue
        with _ALGOS_LOCK:
            _ALGOS_CACHE["ts"] = time.monotonic()
            _ALGOS_CACHE["by_symbol"] = by_symbol
    finally:
        with _ALGOS_LOCK:
            _ALGOS_CACHE["refreshing"] = False


def _algo_prices_by_symbol(service: TradingService) -> dict[str, dict[str, float]]:
    """保护单价格只消费缓存；过期时后台刷新，不阻塞价格轮询。

    注意：首次必须同步取一次。缓存为空就立刻返回会让"止盈/止损"那一栏
    在后台线程补上数据之前一直空白，看起来像止盈止损丢了。
    """
    if not hasattr(service.broker, "_open_algos"):
        return {}
    now = time.monotonic()
    with _ALGOS_LOCK:
        if _ALGOS_CACHE["ts"] <= 0:
            # 冷启动：这一轮同步穿透，之后全部后台刷新
            _ALGOS_CACHE["refreshing"] = True
            _fetch_algo_prices(service)
            return dict(_ALGOS_CACHE["by_symbol"])
        cached = dict(_ALGOS_CACHE["by_symbol"])
        fresh = now - _ALGOS_CACHE["ts"] < _ALGOS_TTL
        if not fresh and not _ALGOS_CACHE["refreshing"]:
            _ALGOS_CACHE["refreshing"] = True
            threading.Thread(target=_fetch_algo_prices, args=(service,), daemon=True).start()
        return cached


def _fmt(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value):,.{digits}f}"
    except Exception:
        return str(value)


def _signed(value: Any, digits: int = 2) -> str:
    try:
        v = float(value)
        return f"{v:+,.{digits}f}"
    except Exception:
        return str(value)


def _pct(value: Any, digits: int = 2) -> str:
    try:
        return f"{float(value) * 100:+.2f}%"
    except Exception:
        return "n/a"


def _price(value: Any) -> str:
    """价格格式化：定点 8 位小数后去掉尾零，绝不丢精度、绝不出现科学计数法。

    此前用 5 位有效数字（%.5g / toPrecision(5)），会把 BTC 这类大价位
    （118234.5 -> 118230.0 / "1.1823e+5"）截掉小数，也会把 108.234 压成
    108.23。价格量级跨度从 1e-8 到 1e5，必须按"保留足够小数位再删尾零"
    处理，而不是按有效数字截断。
    """
    if value is None or value == "":
        return "-"
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not math.isfinite(f):
        return "-"
    if f == 0:
        return "0"
    if abs(f) < 1e-6:
        # 极小价位（meme 币）：定点会直接显示成 0，退回有效数字表示
        s = f"{f:.8g}"
        return s
    s = f"{f:.8f}".rstrip("0").rstrip(".")
    return s or "0"


def _ago(value: Any) -> str:
    try:
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            dt = datetime.fromtimestamp(float(value), tz=timezone.utc)
        sec = max(0, int(time.time() - dt.timestamp()))
        if sec < 60:
            return f"{sec}秒前"
        if sec < 3600:
            return f"{sec // 60}分钟前"
        if sec < 86400:
            return f"{sec // 3600}小时前"
        return f"{sec // 86400}天前"
    except Exception:
        return str(value)


def _pnl_cls(value: Any) -> str:
    try:
        return "pos" if float(value) >= 0 else "neg"
    except Exception:
        return ""


templates.env.filters["fmt"] = _fmt
templates.env.filters["signed"] = _signed
templates.env.filters["pct"] = _pct
templates.env.filters["ago"] = _ago
templates.env.filters["pnl_cls"] = _pnl_cls
templates.env.filters["price"] = _price

def _decision_cn(value: Any) -> str:
    return {
        "no_trade": "不交易",
        "open": "开仓",
        "close": "平仓",
        "reduce": "减仓",
        "adjust": "调整",
    }.get(str(value), str(value))

def _side_cn(value: Any) -> str:
    return {
        "LONG": "做多",
        "SHORT": "做空",
        "BUY": "买入",
        "SELL": "卖出",
        "CLOSE": "平仓",
        "STOP": "止损",
        "TAKE": "止盈",
    }.get(str(value), str(value))

def _status_cn(value: Any) -> str:
    return {
        "normal": "正常",
        "pause": "暂停开仓",
        "close_only": "只允许平仓",
        "emergency": "紧急模式",
        "NEW": "新单",
        "PARTIALLY_FILLED": "部分成交",
        "FILLED": "已成交",
        "CANCELED": "已取消",
        "EXPIRED": "已过期",
        "REJECTED": "已拒绝",
        "OPEN": "持仓中",
        "CLOSED": "已平仓",
        "REDUCED": "已减仓",
        "ok": "正常",
        "error": "错误",
    }.get(str(value), str(value))

def _severity_cn(value: Any) -> str:
    return {"info": "信息", "warn": "警告", "error": "错误", "critical": "严重"}.get(str(value), str(value))

def _event_type_cn(value: Any) -> str:
    return {
        "paper_skip": "纸面跳过",
        "paper_reject_no_price": "纸面无价格拒绝",
        "paper_reject_bare": "纸面空仓拒绝",
        "paper_reject_margin": "纸面保证金拒绝",
        "paper_reject_conflict": "纸面冲突拒绝",
        "paper_close_nopos": "纸面无持仓平仓",
        "paper_adjust": "纸面调整",
        "paper_missing": "纸面执行器缺失",
        "paper_error": "纸面执行错误",
        "risk_rejected": "风控拒绝",
        "margin_cap_close_error": "保证金上限平仓错误",
        "margin_cap_breach": "保证金上限突破",
        "capital_reset_close_error": "资金重置平仓错误",
        "capital_reset": "资金重启",
        "equity_unavailable": "账户权益不可用",
        "account_risk_stop": "账户级风险熔断",
        "startup_safety_block": "启动安全核验拦截",
        "manual_emergency_close": "手动紧急平仓",
        "cycle_error": "决策循环错误",
        "service_shutdown_deferred": "服务关闭等待在途决策",
        "futures_order_error": "合约下单错误",
        "futures_sl_failed": "止损设置失败",
        "futures_tp_failed": "止盈设置失败",
        "futures_close_nopos": "合约无持仓平仓",
        "futures_adjust_skip": "合约调整跳过",
        "futures_adjust": "合约自动调整",
        "futures_adjust_error": "合约调整错误",
        "futures_close_all_error": "合约全平错误",
        "futures_sync_error": "合约持仓同步错误",
        "futures_protection_close": "保护单失败紧急平仓",
        "futures_protection_close_error": "保护单失败紧急平仓错误",
        "futures_limit_sync_error": "限价单台账同步错误",
        "futures_ledger_adopt_legacy": "旧仓位归入AI台账",
        "futures_protection_fallback": "仓位兕底挂保护",
        "futures_order_submit_lost": "下单请求丢失未对账",
        "futures_order_submit_reconciled": "下单请求丢失已对账",
        "futures_close_reconciled": "外部平仓已补记",
        "futures_close_reconcile_error": "外部平仓补记失败",
        "control_block_open": "控制状态拦截开仓",
        "snapshot_rejected": "权益快照跳变已拦截",
        "futures_close_reconcile_mismatch": "外部平仓补记数量不符",
        "futures_close_source_error": "分通道强平错误",
        "futures_unsupported_symbol": "不支持的交易对已拦截",
        "signal_fn_missing": "策略信号函数缺失",
        "signal_scan_error": "信号扫描错误",
        "signal_scanner_error": "信号扫描器异常",
        "strategy_ticket_expired": "策略ticket过期",
        "strategy_conflict_skip": "策略通道冲突跳过",
        "channel_daily_rebalance": "通道每日重算",
        "channel_margin_breach": "通道保证金超限强平",
        "ai_daily_loss_stop": "AI当日亏损熔断",
        "broker_created": "交易通道创建",
        "broker_fallback_paper": "降级纸面交易",
        "broker_unknown_mode": "未知执行模式已拒绝启动",
        "baseline_implausible": "资金基线失效已重锚",
        "baseline_reanchored": "资金基线手动重锚",
        "capital_reset_skipped": "破产重置已跳过",
        "decision_downgraded": "决策校验降级",
        "action_space_no_open": "本轮禁止开仓",
        "intent_log_error": "intent落库错误",
        "outcome_log_error": "平仓归因错误",
        "fast_cycle_error": "快循环错误",
        "futures_min_qty_skip": "低于最小下单量跳过",
        "futures_fill_unconfirmed": "成交状态未确认",
        "futures_leverage_downgraded": "杠杆自动降级",
        "futures_leverage_error": "杠杆设置失败",
        "paper_reject_duplicate_limit": "重复挂单拒绝",
        "paper_reject_limit_price": "限价单缺价格拒绝",
        "market_universe_full_scan_fail": "全量扫描失败",
        "market_universe_price_fail": "行情获取失败",
        "manual_capital_reset": "手动重置资金",
        "manual_reset_close_error": "手动重置平仓错误",
        "manual_reset_rejected": "手动重置被拒绝",
        "auto_review_done": "自动复盘完成",
        "auto_review_error": "自动复盘失败",
        "mode_switch_saved": "执行模式已保存",
    }.get(str(value), str(value))

def _agent_cn(value: Any) -> str:
    return {
        "ai_trader": "AI决策者",
        "strategy_trader": "策略调度者",
        "signal_scanner": "信号扫描器",
        "market_agent": "市场情报Agent",
        "macro_agent": "宏观情报Agent",
        "onchain_agent": "链上情报Agent",
        "strategy_agent": "策略库Agent",
        "risk_agent": "风控Agent",
        "execution_agent": "执行Agent",
        "review_agent": "复盘Agent",
    }.get(str(value), str(value))

def _regime_cn(value: Any) -> str:
    return {"trend_up": "上升趋势", "trend_down": "下降趋势", "choppy": "震荡", "degraded": "数据降级", "mock": "模拟"}.get(str(value), str(value))

def _bias_cn(value: Any) -> str:
    return {"risk_on": "风险偏好", "risk_off": "风险规避", "neutral": "中性"}.get(str(value), str(value))

def _mode_cn(value: Any) -> str:
    return {"demo": "模拟盘", "paper": "本地纸面", "testnet": "测试网", "live": "实盘"}.get(str(value), str(value))

# 后端可切换的全部模式（保留完整，供内部校验/兼容）
SWITCHABLE_MODES = ("paper", "demo", "testnet", "live")
# 前端下拉仅展示的两个选项：模拟盘 + 实盘
VISIBLE_MODES = ("demo", "live")


def _env_file(service: TradingService) -> Path:
    return Path(service.settings.project_root) / ".env"


def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            values[key.strip()] = val.strip().strip('"').strip("'")
    except Exception:
        pass
    return values


def _set_env_mode(path: Path, mode: str) -> None:
    """把 EXECUTION_MODE 写回 .env（保留其它行与注释）。重启后生效。"""
    lines: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        lines = []
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith("EXECUTION_MODE="):
            lines[i] = f"EXECUTION_MODE={mode}"
            replaced = True
            break
    if not replaced:
        if lines and lines[-1].strip():
            lines.append("")
        lines.append(f"EXECUTION_MODE={mode}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

templates.env.filters["decision_cn"] = _decision_cn
templates.env.filters["side_cn"] = _side_cn
templates.env.filters["status_cn"] = _status_cn
templates.env.filters["severity_cn"] = _severity_cn
templates.env.filters["event_type_cn"] = _event_type_cn
templates.env.filters["agent_cn"] = _agent_cn
templates.env.filters["regime_cn"] = _regime_cn
templates.env.filters["bias_cn"] = _bias_cn
templates.env.filters["mode_cn"] = _mode_cn


# 4) 最近一条决策的库内兜底。决策循环首轮要跑很久（拉K线+特征+多轮LLM），
#    这段时间页面"最新决策"会一直显示"无 / 0s"，看起来像系统没在工作。
#    首轮跑完前先用数据库里最近一条决策顶上，并如实显示它产生的时间。
_LAST_DECISION_TTL = 20.0
_LAST_DECISION_CACHE: dict[str, Any] = {"ts": 0.0, "row": None}


def _ts_from_iso(value: Any) -> float | None:
    if not value:
        return None
    try:
        d = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        return None


def _last_decision_from_db(service: TradingService) -> dict[str, Any] | None:
    now = time.monotonic()
    if _LAST_DECISION_CACHE["row"] is not None and now - _LAST_DECISION_CACHE["ts"] < _LAST_DECISION_TTL:
        return _LAST_DECISION_CACHE["row"]
    row = None
    try:
        rows = service.db.fetch_all("SELECT * FROM trade_decisions ORDER BY id DESC LIMIT 1")
        # sqlite3.Row 没有 .get()，统一转成 dict 再交给模板/接口
        row = dict(rows[0]) if rows else None
    except Exception:
        row = None
    _LAST_DECISION_CACHE["ts"] = now
    _LAST_DECISION_CACHE["row"] = row
    return row


def _latest_or_empty(service: TradingService) -> dict[str, Any]:
    latest = service.latest()
    empty = {
        "market": None, "macro": None, "onchain": None, "strategy": None,
        "decision": None, "risk_verdict": None, "execution": [],
        # 页面切换时不在这里实时请求币安账户，避免每次导航都卡一下
        "paper_state": latest.get("paper_state") or {},
        "control_state": service.control.state,
        "capital_state": latest.get("capital_state") or {
            "session_equity": service.capital.initial_equity,
            "initial_equity": service.capital.initial_equity,
        },
        "quota_state": service.quota.status(),
        "channel_state": latest.get("channel_state") or {},
        "intel_cache": latest.get("intel_cache"),
        "ts": None,
    }
    if not empty.get("channel_state") and not (latest or {}).get("channel_state"):
        # 首轮循环尚未跑完时才走这条兜底；结果缓存 60 秒且后台刷新，
        # 避免重启后头几分钟每个页面轮询都被 4 秒的交易所请求卡住
        empty["channel_state"] = _channel_fallback(service)
    merged = {**empty, **latest}
    if not merged.get("decision"):
        # 首轮循环还没产出决策：用库里最近一条顶上，时间戳也用它，
        # 这样"最新决策"不再显示无 / 0s，循环年龄也是真实值。
        # 注意只能放进独立键：_compute_status 需要 "decision" 是 pydantic 模型（要 model_dump）。
        row = _last_decision_from_db(service)
        if row:
            merged["decision_from_db"] = row
            merged["ts"] = _ts_from_iso(row.get("created_at")) or merged["ts"]
    return merged


def _session_equity_from_state(service: TradingService, broker_state: dict[str, Any]) -> float:
    """尽量复用已经拉到的 broker state，不再额外请求一次账户接口。"""
    mode = getattr(service.broker, "mode", "paper")
    try:
        if mode in ("demo", "testnet", "live"):
            total = float(broker_state.get("initial_equity") or 0.0)
            if total > 0:
                return round(total - float(service.capital.baseline), 2)
    except Exception:
        pass
    return service.capital.session_equity()


def _capital_overview(service: TradingService, broker_state: dict[str, Any],
                      positions: list[dict[str, Any]]) -> dict[str, float]:
    """资金口径统一（页面四个数字一套来源、严格联动）：

    - 已实现收益 = totalWalletBalance - (baseline + 起始资金)，即钱包余额相对会话起点的变化
      （只随真实成交/手续费/资金费变动，不随行情抖动）
    - 持仓浮盈亏 = Σ 实时价计算的持仓浮盈（与"当前仓位"表格完全一致）
    - 总盈亏    = 已实现 + 浮盈亏
    - 会话权益  = 起始资金 + 总盈亏

    旧口径把"已实现"当残差（总盈亏-浮盈），而总盈亏来自交易所标记价、浮盈来自实时 ticker，
    两套价格差直接漏进"已实现"，导致没有平仓它也每几秒跳一次。
    """
    initial_eq = float(service.capital.initial_equity)
    # 看板金额统一到分，保证用户看到的「已实现 + 浮盈亏 = 总盈亏」逐项严格相等。
    unrealized = round(sum(float(p.get("upnl") or 0.0) for p in positions), 2)
    mode = getattr(service.broker, "mode", "paper")
    wallet = 0.0
    if mode in ("demo", "testnet", "live"):
        try:
            wallet = float(broker_state.get("wallet_balance") or 0.0)
        except Exception:
            wallet = 0.0
    if wallet > 0:
        # baseline = 会话开始时的交易所总权益 - 起始资金，
        # 钱包基线 = baseline + 起始资金（会话开始时的 totalWalletBalance）
        realized = round(wallet - float(service.capital.baseline) - initial_eq, 2)
        pnl = round(realized + unrealized, 2)
        session_eq = round(initial_eq + pnl, 2)
    else:
        # paper 模式 / 钱包字段不可用：退回旧口径
        session_eq = _session_equity_from_state(service, broker_state)
        pnl = round(session_eq - initial_eq, 2)
        realized = round(pnl - unrealized, 2)
    pnl_pct = (pnl / initial_eq * 100) if initial_eq else 0.0
    # 基线严重失配检测：钱包余额与(baseline+起始资金)偏差超过 2 倍起始资金时，
    # 几乎可以断定不是交易盈亏，而是外部事件（典型：币安测试网每月重置资金、
    # 手动从 faucet 领取/转出）。此时"已实现收益"数字不可信，需要页面提示用户重置。
    mismatch = bool(wallet > 0 and initial_eq > 0
                    and abs(realized) > initial_eq * 2)
    return {
        "session_equity": session_eq,
        "initial_equity": initial_eq,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "baseline_mismatch": mismatch,
    }


def _enriched_positions(service: TradingService, latest: dict[str, Any],
                         broker_state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    market = latest.get("market")
    snapshot_prices = market.last_prices if market is not None else {}
    if broker_state is None:
        raw_state = service.broker.state
        broker_state = raw_state if isinstance(raw_state, dict) else {}
    raw_positions = broker_state.get("positions", [])
    symbols = [str(p.get("symbol")) for p in raw_positions if p.get("symbol")]
    # 最新价优先用主网实时 ticker（get_live_prices，秒级刷新）；demo 交易所 positionRisk
    # 返回的 mark_price 是滞后快照，直接展示会长时间不动。取不到实时价再退回 mark_price。
    # 持仓用 1.5 秒短 TTL：WebSocket 推送每秒写入缓存时直接命中、零 REST 请求；
    # 推送不可用时退化成 1.5 秒一次的并发单点查询（约 0.25s，权重可忽略）。
    live_prices = get_live_prices(symbols, ttl_seconds=_POSITION_PRICE_TTL) if symbols else {}
    mark_prices: dict[str, float] = {}
    for p in raw_positions:
        mp = float(p.get("mark_price") or 0.0)
        if mp > 0:
            mark_prices[str(p.get("symbol"))] = mp
    rows = []
    for p in raw_positions:
        entry = float(p.get("entry_price") or 0.0)
        qty = float(p.get("qty") or 0.0)
        notional = abs(entry * qty)
        side = p.get("side", "LONG")
        last = float(live_prices.get(p["symbol"]) or mark_prices.get(p["symbol"]) or snapshot_prices.get(p["symbol"], entry))
        upnl = ((last - entry) * qty if side == "LONG" else (entry - last) * qty) if last > 0 else float(p.get("upnl") or 0.0)
        upnl_pct = (upnl / notional * 100) if notional else 0.0
        margin = float(p.get("margin") or 0.0)
        if margin <= 0:
            margin = notional / max(1, int(p.get("leverage") or 1))
        # 金额类保留原精度（仅收敛浮点长尾），价格类一律不再截断：
        # 之前对 entry/last/SL/TP 用 %.5g（5 位有效数字）会让 BTC 这类大价位
        # 从 118234.5 变成 118230.0，且截断发生在 upnl 计算之后，导致页面
        # 展示的价格与对应的盈亏对不上账。展示层统一交给 |price / FMT.px 处理。
        if upnl:
            upnl = float(f"{upnl:.6f}")
            upnl_pct = float(f"{upnl_pct:.4f}")
        if notional:
            notional = float(f"{notional:.6f}")
        if margin:
            margin = float(f"{margin:.6f}")
        stop_loss = p.get("stop_loss")
        take_profit = p.get("take_profit")
        if stop_loss is not None:
            stop_loss = float(stop_loss)
        if take_profit is not None:
            take_profit = float(take_profit)
        if stop_loss is None or take_profit is None:
            # 一次性批量取保护单触发价（30s 缓存），替代旧的逐仓 _algo_prices 签名请求
            prot = _algo_prices_by_symbol(service).get(p["symbol"]) or {}
            if stop_loss is None:
                stop_loss = prot.get("STOP_MARKET")
            if take_profit is None:
                take_profit = prot.get("TAKE_PROFIT_MARKET")
        rows.append({**p, "entry_price": entry, "last_price": last, "upnl": upnl, "upnl_pct": upnl_pct,
                     "notional": notional, "margin": margin, "stop_loss": stop_loss,
                     "take_profit": take_profit,
                     "mode": getattr(service.broker, "mode", "paper")})
    return rows

def _is_open_order(row: dict[str, Any], raw: dict[str, Any]) -> bool:
    action = str(raw.get("action") or "")
    side = str(row.get("side") or "")
    if action == "algo_protection" or side in ("STOP", "TAKE"):
        return False
    if action in ("close", "close_all", "reduce"):
        return False
    if side == "CLOSE":
        return False
    reduce_only = raw.get("reduceOnly")
    if reduce_only is True or str(reduce_only).lower() == "true":
        return False
    return True


def _compute_realized_pnls(history_rows) -> dict[int, float]:
    """根据历史订单流水，用 FIFO 方式估算历史平仓/减仓的已实现盈亏。"""
    pnls: dict[int, float] = {}
    opens: dict[str, list[dict[str, Any]]] = {}
    for r0 in sorted(history_rows, key=lambda x: int(x["id"])):
        r = dict(r0)
        try:
            raw = json.loads(r.get("raw_json") or "{}")
        except Exception:
            raw = {}
        action = str(raw.get("action") or "")
        side = str(r.get("side") or "")
        if action == "algo_protection" or side in ("STOP", "TAKE"):
            continue
        symbol = str(r.get("symbol") or "")
        qty = float(r.get("qty") or 0.0)
        price = float(r.get("price") or 0.0)
        if qty <= 0 or not symbol:
            continue

        if _is_open_order(r, raw):
            opens.setdefault(symbol, []).append({
                "remaining": qty,
                "price": price,
                "side": str(r.get("side") or "LONG"),
            })
        else:
            # 平仓/减仓/全平
            total_pnl = 0.0
            remaining = qty
            queue = opens.setdefault(symbol, [])
            while remaining > 1e-12 and queue:
                o = queue[0]
                used = min(remaining, float(o["remaining"]))
                if o["side"] == "LONG":
                    total_pnl += used * (price - float(o["price"]))
                else:
                    total_pnl += used * (float(o["price"]) - price)
                o["remaining"] = float(o["remaining"]) - used
                remaining -= used
                if float(o["remaining"]) <= 1e-12:
                    queue.pop(0)
            pnls[int(r["id"])] = total_pnl
    return pnls


def _realized_pnl_total(history_rows) -> float:
    return float(sum(_compute_realized_pnls(history_rows).values()))


def _enrich_orders(rows, history_rows=None, exclude_protection: bool = False):
    pnl_map = _compute_realized_pnls(history_rows if history_rows is not None else rows)
    out = []
    for r in rows:
        d = dict(r)
        try:
            raw = json.loads(d.get("raw_json") or "{}")
        except Exception:
            raw = {}
        # 保护单（移动止损/止盈）会随行情反复重设，每轮产生新的挂单记录。
        # exclude_protection=True 时：真实交易全保留，保护单只保留「当前有效」的挂单
        # （status 仍为 NEW、未被撤/未被触发），已失效的历史保护单不再刷屏。
        is_protection = raw.get("action") == "algo_protection" or str(d.get("side")) in ("STOP", "TAKE")
        if exclude_protection and is_protection and str(d.get("status") or "").upper() != "NEW":
            continue
        action = str(raw.get("action") or "")
        side = str(d.get("side") or "")
        reduce_only = raw.get("reduceOnly")
        is_reduce_only = reduce_only is True or str(reduce_only).lower() == "true"
        if action in ("close", "reduce"):
            display_side = "平多" if side == "LONG" else "平空"
        elif action == "close_all" or side == "CLOSE":
            display_side = "平仓"
        elif is_reduce_only:
            display_side = "平多" if side == "LONG" else "平空"
        elif side == "STOP":
            display_side = "止损"
        elif side == "TAKE":
            display_side = "止盈"
        else:
            display_side = "做多" if side == "LONG" else ("做空" if side == "SHORT" else side)
        d["display_side"] = display_side
        d["is_protection"] = is_protection
        qty = float(d.get("qty") or 0.0)
        price = float(d.get("price") or 0.0)
        # 价格显示用 5 位有效数字，去掉浮点尾巴
        if price:
            price = float(f"{price:.5g}")
            d["price"] = price
        d["notional"] = abs(qty * price)
        d["margin"] = float(raw.get("margin") or 0.0)
        d["commission"] = raw.get("commission")
        pnl = raw.get("net_pnl") if raw.get("net_pnl") is not None else raw.get("pnl")
        if pnl is not None:
            d["pnl"] = float(f"{float(pnl):.6g}")
        else:
            d["pnl"] = pnl_map.get(int(d.get("id") or 0))
        out.append(d)
    return out

def _md_inline(text: str) -> str:
    text = html.escape(text)
    text = text.replace("**", "")
    return text


def _md_to_sections(text: str) -> list[dict[str, str]]:
    sections: list[dict[str, str]] = []
    cur_title = ""
    cur_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("# "):
            continue
        if line.startswith("## "):
            if cur_lines:
                sections.append({"title": cur_title or "报告信息", "html": _render_md_lines(cur_lines)})
            cur_title = line[3:].strip()
            cur_lines = []
        else:
            cur_lines.append(line)
    if cur_lines:
        sections.append({"title": cur_title, "html": _render_md_lines(cur_lines)})
    return sections


def _render_md_lines(lines: list[str]) -> str:
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        if not line:
            i += 1
            continue
        if line.startswith("|"):
            table = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                table.append(lines[i].strip())
                i += 1
            rows = []
            for tr in table:
                cells = [c.strip() for c in tr.strip("|").split("|")]
                if all(set(c) <= set("-: ") and c for c in cells):
                    continue
                rows.append(cells)
            if rows:
                out.append('<table class="md-table">')
                for idx, cells in enumerate(rows):
                    tag = "th" if idx == 0 else "td"
                    out.append("<tr>" + "".join(f"<{tag}>{_md_inline(c)}</{tag}>" for c in cells) + "</tr>")
                out.append("</table>")
            continue
        if line.startswith("- "):
            items = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(f"<li>{_md_inline(lines[i].strip()[2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue
        para = []
        while i < len(lines) and lines[i].strip() and not lines[i].strip().startswith(("|", "- ")):
            para.append(lines[i].strip())
            i += 1
        out.append(f"<p>{_md_inline(' '.join(para))}</p>")
    return "".join(out)


# ---- 看板侧快照节流：执行器只在成交时写快照，这里按 60s 节流补齐周期快照，
# 保证权益曲线 / 每日盈亏图有连续数据（失败静默，不影响状态接口）----
_last_snapshot_mono = 0.0
_SNAPSHOT_INTERVAL_S = 60.0

# 权益快照合理性闸门。交易所偶发脏读会把余额读成几千 U（实测出现过 4998.05 / 999.58，
# 而会话起始资金只有 500 U）。一条脏快照就会永久毁掉「最高 / 最低 / 最大回撤」这类
# 区间统计，且只能事后人工清理，所以必须在写入前拦掉。
#
# 判据用「相对上一条快照的突变幅度」而不是绝对金额：这样与资金规模无关，账户长大后
# 依然有效。0.6（±60%）能稳稳拦住上述脏读，正常的逐周期盈亏波动远不到这个量级。
# 注意：若人工充值/提现导致权益一次性大幅跳变，这里会拦一拍并留下 snapshot_rejected
# 事件；看到该事件时确认是真实变动即可，无需改代码。
_SNAPSHOT_MAX_JUMP_RATIO = 0.6
_last_bad_snapshot_log_mono = 0.0
_BAD_SNAPSHOT_LOG_INTERVAL_S = 600.0


def _last_snapshot_equity(service: TradingService) -> float | None:
    try:
        row = service.db.fetch_one(
            "SELECT equity FROM portfolio_snapshots ORDER BY id DESC LIMIT 1")
        if row:
            v = float(row["equity"] if isinstance(row, dict) else row[0])
            return v if v > 0 else None
    except Exception:
        pass
    return None


def _parse_iso(value: Any) -> float | None:
    """把 ISO 时间串转为 epoch 秒，失败返回 None。"""
    try:
        s = str(value).strip()
        if not s:
            return None
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _baseline_just_reanchored(service: TradingService) -> bool:
    """基线是否在最后一条快照之后被重锚过。

    重锚（修正污染基线、手动重置、破产重启）会让会话权益一次性大幅跳变——
    例如基线被清零污染时显示 5028U，重锚后回到 532U，跳变 89%。这是合法
    修正，不能被脏读闸门当成异常丢弃（历史上被丢弃 22 次，导致错误数值
    长期驻留在区间统计里）。
    """
    try:
        cap_cfg = service.settings.raw_settings.get("capital", {}) or {}
        path = service.settings.project_root / str(
            cap_cfg.get("state_file", "data/capital_state.json"))
        if not path.exists():
            return False
        data = json.loads(path.read_text(encoding="utf-8"))
        anchor_ts = _parse_iso(data.get("anchored_at"))
        if anchor_ts is None:
            return False
        row = service.db.fetch_one(
            "SELECT created_at FROM portfolio_snapshots ORDER BY id DESC LIMIT 1")
        snap_ts = _parse_iso(row["created_at"] if isinstance(row, dict) else row[0]) if row else None
        return snap_ts is None or anchor_ts > snap_ts
    except Exception:
        return False


def _maybe_snapshot(service: TradingService, equity: float,
                    unrealized_pnl: float, positions: list[dict[str, Any]]) -> None:
    global _last_snapshot_mono, _last_bad_snapshot_log_mono
    now = time.monotonic()
    if now - _last_snapshot_mono < _SNAPSHOT_INTERVAL_S:
        return
    _last_snapshot_mono = now
    try:
        eq = float(equity or 0.0)
        prev = _last_snapshot_equity(service)
        if prev and abs(eq - prev) / prev > _SNAPSHOT_MAX_JUMP_RATIO:
            if _baseline_just_reanchored(service):
                # 基线刚重锚：跳变是修正而非脏读，放行并留审计痕迹
                try:
                    service.db.log_risk_event(
                        event_type="baseline_reanchored", severity="warn",
                        details=f"资金基线已重锚，权益快照跳变视为合法修正并放行："
                                f"上一条 {prev:.2f} → 本次 {eq:.2f}",
                        action_taken="accept_snapshot")
                except Exception:
                    pass
                margin_used = sum(float(p.get("margin") or 0.0) for p in positions)
                service.db.log_portfolio_snapshot(
                    equity=eq, margin_balance=eq, available_balance=eq - margin_used,
                    unrealized_pnl=float(unrealized_pnl or 0.0), margin_used=margin_used,
                    raw={"source": "dashboard_status", "baseline_reanchored": True},
                )
                return
            # 脏读：跳过本次写入。仍推进节流时间戳，避免每个周期都触发告警。
            if now - _last_bad_snapshot_log_mono >= _BAD_SNAPSHOT_LOG_INTERVAL_S:
                _last_bad_snapshot_log_mono = now
                try:
                    service.db.log_risk_event(
                        event_type="snapshot_rejected", severity="warn",
                        details=f"权益快照异常已丢弃：本次 equity={eq:.2f}，"
                                f"上一条 {prev:.2f}，跳变 "
                                f"{abs(eq - prev) / prev * 100:.0f}% "
                                f"超过 {_SNAPSHOT_MAX_JUMP_RATIO * 100:.0f}% 阈值",
                        action_taken="skip_snapshot")
                except Exception:
                    pass
            return
        margin_used = sum(float(p.get("margin") or 0.0) for p in positions)
        service.db.log_portfolio_snapshot(
            equity=eq,
            margin_balance=eq,
            available_balance=eq - margin_used,
            unrealized_pnl=float(unrealized_pnl or 0.0),
            margin_used=margin_used,
            raw={"source": "dashboard_status"},
        )
    except Exception:
        pass


def _warmup(service: TradingService) -> None:
    """启动时后台预热账户快照与通道兜底，让第一个页面请求不必等交易所往返。"""
    try:
        state = _fetch_broker_state(service)
        if not state.get("error"):
            with _BROKER_STATE_LOCK:
                _BROKER_STATE_CACHE["ts"] = time.monotonic()
                _BROKER_STATE_CACHE["state"] = state
    except Exception:
        pass
    try:
        if not (service.latest() or {}).get("channel_state"):
            with _CHANNEL_FALLBACK_LOCK:
                _CHANNEL_FALLBACK_CACHE["refreshing"] = True
            _refresh_channel_fallback(service)
    except Exception:
        with _CHANNEL_FALLBACK_LOCK:
            _CHANNEL_FALLBACK_CACHE["refreshing"] = False
    try:
        # 保护单（止盈/止损）同样预热，否则首屏这两列会空白
        if hasattr(service.broker, "_open_algos"):
            with _ALGOS_LOCK:
                _ALGOS_CACHE["refreshing"] = True
            _fetch_algo_prices(service)
    except Exception:
        with _ALGOS_LOCK:
            _ALGOS_CACHE["refreshing"] = False


# 持仓最新价的缓存 TTL（秒）。WebSocket 推送正常时每秒写入，这里留 1.5 秒余量；
# 推送断开后缓存自然过期，自动回落到 REST 单点查询。
_POSITION_PRICE_TTL = 1.5


def _position_symbols(service: TradingService) -> list[str]:
    """推送订阅源：当前持仓的 symbol 列表（读缓存，不额外请求交易所）。"""
    try:
        state = _broker_state_cached(service)
    except Exception:
        return []
    return [str(p.get("symbol")) for p in (state.get("positions") or []) if p.get("symbol")]


def create_app(service: TradingService) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # 行情推送：可用时把 REST 轮询降到零；出口不支持时自动降级，不影响页面
        stream = PriceStream(lambda: _position_symbols(service))
        app.state.price_stream = stream
        task = asyncio.create_task(stream.serve())

        # 私有账户流分阶段接入：当前只把原始事件写入幂等收件箱，不能更新账户、
        # 不能驱动订单。配置若关闭、不适用于paper、或缺少凭据，健康接口会明确说明。
        exchange_cfg = service.settings.raw_settings.get("exchange_stream", {}) or {}
        exchange_enabled = bool(exchange_cfg.get("enabled", False))
        observe_only = bool(exchange_cfg.get("observe_only", True))
        user_stream = None
        user_task = None
        user_stream_runtime: dict[str, Any] = {
            "enabled": exchange_enabled,
            "observe_only": observe_only,
            "state": "disabled",
            "active": False,
            "reason": "configuration_disabled",
        }
        if exchange_enabled and not observe_only:
            user_stream_runtime.update(state="blocked", reason="observe_only_required")
        elif exchange_enabled:
            broker = service.broker
            api_key = str(getattr(broker, "api_key", "") or "")
            rest_base_url = str(getattr(broker, "base_url", "") or "")
            environment = str(getattr(broker, "environment", getattr(broker, "mode", "")) or "")
            if not api_key or not rest_base_url or environment not in {"demo", "testnet", "live"}:
                user_stream_runtime.update(state="unavailable", reason="broker_not_supported")
            else:
                user_stream = BinanceUserDataStream(
                    api_key=api_key,
                    rest_base_url=rest_base_url,
                    environment=environment,
                    inbox=ExchangeEventInbox(service.db),
                    keepalive_minutes=float(exchange_cfg.get("keepalive_minutes", 30)),
                    planned_reconnect_hours=float(exchange_cfg.get("planned_reconnect_hours", 23)),
                    reconnect_seconds=float(exchange_cfg.get("reconnect_seconds", 5)),
                    queue_size=int(exchange_cfg.get("queue_size", 5000)),
                    trust_env=bool(getattr(broker, "_trust_env", True)),
                )
                user_stream_runtime = user_stream.status()
                user_task = asyncio.create_task(user_stream.serve())
        app.state.user_stream = user_stream
        app.state.user_stream_runtime = user_stream_runtime
        try:
            yield
        finally:
            stream.stop()
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if user_stream is not None:
                user_stream.stop()
            if user_task is not None:
                user_task.cancel()
                await asyncio.gather(user_task, return_exceptions=True)
            if user_stream is not None:
                await user_stream.close()

    app = FastAPI(title="Multi-Agent Trader Console", lifespan=lifespan)

    # ---- 写操作防 CSRF（P0-3）----
    # 控制台接口会真实平仓/重置资金/切模式，但本地 CSRF（恶意网页用
    # fetch(mode:'no-cors', text/plain) 打 127.0.0.1）可以无凭据触发。
    # 该模式下浏览器发不了自定义头，也无法携带 application/json Content-Type，
    # 因此「随机 token 头 + 强制 JSON Content-Type」即可可靠拦截。
    # token 启动时生成，注入到页面模板（同源脚本可读），并打印到控制台。
    _write_token = secrets.token_urlsafe(24)

    @app.middleware("http")
    async def _csrf_guard(request: Request, call_next):
        if request.method == "POST" and request.url.path.startswith("/api/"):
            if (request.headers.get("x-console-token") or "") != _write_token:
                return JSONResponse({"ok": False, "error": "missing or invalid console token"},
                                    status_code=403)
            if "application/json" not in (request.headers.get("content-type") or "").lower():
                return JSONResponse({"ok": False, "error": "content-type must be application/json"},
                                    status_code=415)
        return await call_next(request)

    print(f"console write token: {_write_token}")
    app.state.console_token = _write_token
    threading.Thread(target=_warmup, args=(service,), daemon=True).start()

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        latest = _latest_or_empty(service)
        broker_state = _broker_state_cached(service)
        positions = _enriched_positions(service, latest, broker_state)
        broker_error = broker_state.get("error") if isinstance(broker_state, dict) else None
        cap = _capital_overview(service, broker_state, positions)
        session_eq = cap["session_equity"]
        initial_eq = cap["initial_equity"]
        pnl = cap["pnl"]
        pnl_pct = cap["pnl_pct"]
        realized_pnl = cap["realized_pnl"]
        all_order_rows = service.db.fetch_all("SELECT * FROM orders ORDER BY id ASC LIMIT 2000")
        recent_rows = service.db.fetch_all("SELECT * FROM orders ORDER BY id DESC LIMIT 300")
        orders = _enrich_orders(recent_rows, all_order_rows, exclude_protection=True)[:30]
        events = service.db.fetch_all("SELECT * FROM risk_events ORDER BY id DESC LIMIT 15")
        decisions = service.db.fetch_all("SELECT * FROM trade_decisions ORDER BY id DESC LIMIT 8")
        # 交易统计统一计数：优先用新字段，旧缓存的 latest 里没有则现算
        quota_state = latest.get("quota_state") or {}
        quota_total = quota_state.get("total_done_today")
        if quota_total is None:
            quota_total = (int(quota_state.get("short_done_today") or 0)
                           + int(quota_state.get("hourly_done_today") or 0))
        broker_mode = str(getattr(service.broker, "mode", "paper"))
        env_vals = _read_env_file(_env_file(service))
        key_map = (service.settings.raw_settings.get("execution", {}) or {}).get("api_key_env_map", {}) or {}
        mode_options = {}
        for m in VISIBLE_MODES:
            key_env = key_map.get(m)
            configured = True
            if key_env:
                val = os.environ.get(key_env, "") or env_vals.get(key_env, "")
                configured = bool(val.strip())
            mode_options[m] = {"label": _mode_cn(m), "configured": configured}
        return templates.TemplateResponse(request, "index.html", {
            "latest": latest,
            "positions": positions,
            "session_eq": session_eq,
            "initial_eq": initial_eq,
            "pnl": pnl,
            "pnl_pct": pnl_pct,
            "realized_pnl": realized_pnl,
            "baseline_mismatch": cap.get("baseline_mismatch"),
            "broker_error": broker_error,
            "orders": orders,
            "events": events,
            "decisions": decisions,
            "now_ts": time.time(),
            "console_token": _write_token,
            "broker_mode": broker_mode,
            "mode_options": mode_options,
            "quota_total": quota_total,
        })

    @app.get("/decisions", response_class=HTMLResponse)
    def decisions_page(request: Request):
        """交易（决策中心）：把「情报 → 匹配 → 调度 → 拍板 → 执行」串成一条链。

        数据全部来自 service.latest()，与 /api/status 同源，没有新增后端查询。
        决策记录表承接原 /trades 页的历史决策，订单页不再重复展示。
        """
        latest = _latest_or_empty(service)
        decisions = service.db.fetch_all("SELECT * FROM trade_decisions ORDER BY id DESC LIMIT 30")
        return templates.TemplateResponse(request, "decisions.html", {
            "latest": latest,
            "decisions": decisions,
            "now_ts": time.time(),
            "console_token": _write_token,
        })

    @app.get("/agents", response_class=HTMLResponse)
    def agents_page(request: Request):
        latest = _latest_or_empty(service)
        runs = service.db.fetch_all(
            """SELECT agent_name,
                      SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) ok_n,
                      SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) err_n,
                      COALESCE(AVG(CASE WHEN status='ok' THEN latency_ms END),0) ok_ms,
                      COALESCE(AVG(CASE WHEN status='error' THEN latency_ms END),0) err_ms,
                      MAX(ended_at) last_ts
               FROM agent_runs WHERE agent_name != 'orchestrator' GROUP BY agent_name ORDER BY agent_name"""
        )
        llm_calls = service.db.fetch_all(
            """SELECT agent_name, model,
                      SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) ok_n,
                      SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) err_n,
                      COALESCE(SUM(CASE WHEN status='ok' THEN prompt_tokens ELSE 0 END),0) pt,
                      COALESCE(SUM(CASE WHEN status='ok' THEN completion_tokens ELSE 0 END),0) ct,
                      COALESCE(SUM(CASE WHEN status='ok' THEN prompt_tokens + completion_tokens ELSE 0 END),0) total_tokens,
                      COALESCE(AVG(CASE WHEN status='ok' THEN latency_ms END),0) ms
               FROM llm_calls WHERE agent_name != 'orchestrator' GROUP BY agent_name, model ORDER BY agent_name"""
        )
        token_stats = {
            "prompt": sum(int(r["pt"] or 0) for r in llm_calls),
            "completion": sum(int(r["ct"] or 0) for r in llm_calls),
            "total": sum(int(r["total_tokens"] or 0) for r in llm_calls),
        }
        return templates.TemplateResponse(request, "agents.html", {
            "latest": latest, "runs": runs, "llm_calls": llm_calls,
            "token_stats": token_stats,
            "console_token": _write_token,
        })

    @app.get("/channels", response_class=HTMLResponse)
    def channels_page(request: Request):
        latest = _latest_or_empty(service)
        signals = service.db.fetch_all("SELECT * FROM signal_events ORDER BY id DESC LIMIT 20")
        tickets = service.db.fetch_all("SELECT * FROM signal_tickets ORDER BY id DESC LIMIT 20")
        return templates.TemplateResponse(request, "channels.html", {
            "latest": latest,
            "signals": signals,
            "tickets": tickets,
            "console_token": _write_token,
        })

    @app.get("/orders", response_class=HTMLResponse)
    def orders_page(request: Request):
        db = service.db
        all_order_rows = db.fetch_all("SELECT * FROM orders ORDER BY id ASC LIMIT 3000")
        orders = _enrich_orders(
            db.fetch_all("SELECT * FROM orders ORDER BY id DESC LIMIT 60"),
            all_order_rows,
        )
        positions = db.fetch_all("SELECT * FROM positions ORDER BY id DESC LIMIT 60")
        snapshots = db.fetch_all("SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 60")
        counts = {
            "decisions": db.fetch_one("SELECT COUNT(*) c FROM trade_decisions")["c"],
            "filled_orders": db.fetch_one("SELECT COUNT(*) c FROM orders WHERE status='FILLED'")["c"],
            "protection_orders": db.fetch_one(
                "SELECT COUNT(*) c FROM orders WHERE side IN ('STOP','TAKE') OR raw_json LIKE '%algo_protection%'"
            )["c"],
            "positions": db.fetch_one("SELECT COUNT(*) c FROM positions")["c"],
            "snapshots": db.fetch_one("SELECT COUNT(*) c FROM portfolio_snapshots")["c"],
        }
        return templates.TemplateResponse(request, "orders.html", {
            "orders": orders, "positions": positions,
            "snapshots": snapshots, "counts": counts,
            "console_token": _write_token,
        })

    @app.get("/trades", response_class=HTMLResponse)
    def trades_legacy(request: Request):
        """旧链接兼容：/trades 已更名为 /orders，保留 302 让外部书签不失效。"""
        return RedirectResponse(url="/orders", status_code=302)

    @app.get("/reviews", response_class=HTMLResponse)
    def reviews_page(request: Request):
        reviews = service.db.fetch_all("SELECT * FROM daily_reviews ORDER BY review_date DESC LIMIT 30")
        cards = []
        for r in reviews:
            p = Path(r["report_path"])
            if p.exists():
                text = p.read_text(encoding="utf-8")
                cards.append({
                    "row": r,
                    "exists": True,
                    "sections": _md_to_sections(text),
                })
            else:
                cards.append({"row": r, "exists": False, "sections": []})
        return templates.TemplateResponse(request, "reviews.html", {"cards": cards, "console_token": _write_token})

    # ---- /api/status：5 秒结果缓存 + 并发合并。
    # 账户快照通过 stale-while-revalidate 后台更新；前台状态读取只消费缓存，
    # 避免交易所网络抖动时多个页面轮询相互排队。----
    _STATUS_TTL = 5.0
    _status_lock = threading.Lock()
    _status_cache: dict[str, Any] = {"ts": 0.0, "payload": None, "recomputing": False}

    def _compute_status() -> dict[str, Any]:
        latest = _latest_or_empty(service)
        initial_eq = service.capital.initial_equity
        broker_state = _broker_state_cached(service)
        positions = _enriched_positions(service, latest, broker_state)
        cap = _capital_overview(service, broker_state, positions)
        broker_error = broker_state.get("error") if isinstance(broker_state, dict) else None
        _maybe_snapshot(service, cap["session_equity"], cap["unrealized_pnl"], positions)
        quota = service.quota.status()
        return {
            "control_state": service.control.state,
            "account_risk": latest.get("account_risk") or {},
            "startup_safety": latest.get("startup_safety") or getattr(service, "_startup_safety", {}),
            "broker_mode": getattr(service.broker, "mode", "paper"),
            "capital": {
                "session_equity": cap["session_equity"],
                "initial_equity": initial_eq,
                "pnl": cap["pnl"],
                "pnl_pct": cap["pnl_pct"],
                "last_reset": service.capital.last_reset,
            },
            "unrealized_pnl": cap["unrealized_pnl"],
            "realized_pnl": cap["realized_pnl"],
            "broker_error": broker_error,
            "positions": positions,
            "quota_state": quota,
            "decision": latest["decision"].model_dump() if latest["decision"] else None,
            "risk_verdict": latest["risk_verdict"].model_dump() if latest["risk_verdict"] else None,
            "strategy_decision": latest.get("strategy_decision").model_dump() if latest.get("strategy_decision") else None,
            "ai_decision": latest.get("ai_decision").model_dump() if latest.get("ai_decision") else None,
            "strategy_refs": latest["strategy"].model_dump() if latest["strategy"] else {"refs": []},
            # generated_at 一并返回：交易页要靠它算「情报新鲜度」，
            # 用户需要知道这份情报是刚跑出来的还是半小时前的。
            "market": {"summary": latest["market"].summary, "regime": latest["market"].regime,
                       "generated_at": latest["market"].generated_at} if latest["market"] else None,
            "macro": {"summary": latest["macro"].summary, "bias": latest["macro"].market_bias,
                      "generated_at": latest["macro"].generated_at} if latest["macro"] else None,
            "onchain": {"summary": latest["onchain"].summary,
                        "generated_at": latest["onchain"].generated_at} if latest["onchain"] else None,
            "intel_cache": latest.get("intel_cache"),
            "channel_state": latest.get("channel_state") or {},
            "cycle_age_seconds": round(time.time() - float(latest.get("ts") or time.time()), 1),
            "cycle_running": service.cycle_running(),
            "quota": quota,
            "server_time": time.time(),
        }

    @app.get("/api/status")
    def api_status():
        now = time.monotonic()
        with _status_lock:
            payload = _status_cache["payload"]
            if payload is not None and now - _status_cache["ts"] < _STATUS_TTL:
                return payload
            # 计算移出锁外（不阻塞其它读者），但用"临时占位"防击穿：
            # 第一个到达的请求负责重算，锁间隙内到达的其它请求直接返回旧值。
            if _status_cache["recomputing"]:
                stale = _status_cache["payload"]
                if stale is not None:
                    return stale
                # 冷启动且无旧值：极少数情况下才真正串行等待
                payload = _compute_status()
                _status_cache["ts"] = time.monotonic()
                _status_cache["payload"] = payload
                return payload
            _status_cache["recomputing"] = True
        try:
            payload = _compute_status()
        finally:
            with _status_lock:
                _status_cache["recomputing"] = False
        with _status_lock:
            _status_cache["ts"] = time.monotonic()
            _status_cache["payload"] = payload
        return payload

    @app.get("/api/health")
    def api_health():
        """只读运维健康摘要；账户或保护单状态未知即视为不健康。"""
        latest = _latest_or_empty(service)
        safety = latest.get("startup_safety") or getattr(service, "_startup_safety", {})
        broker_state = _broker_state_cached(service)
        user_stream = getattr(app.state, "user_stream", None)
        user_stream_status = (user_stream.status() if user_stream is not None else
                              dict(getattr(app.state, "user_stream_runtime", {
                                  "enabled": False, "observe_only": True,
                                  "state": "disabled", "active": False,
                                  "reason": "not_started",
                              })))
        try:
            pending_row = service.db.fetch_one(
                "SELECT COUNT(*) AS count FROM exchange_events WHERE status='RECEIVED'")
            pending_events = int(pending_row["count"] if pending_row else 0)
            db_readable = True
        except Exception:
            pending_events = None
            db_readable = False
        user_stream_status["persisted_pending"] = pending_events
        last_event_at = user_stream_status.get("last_event_at")
        user_stream_status["last_event_age_seconds"] = (
            round(max(0.0, time.time() - float(last_event_at)), 3)
            if last_event_at is not None else None
        )
        cycle_ts = latest.get("ts")
        account_risk = (latest.get("account_risk") or
                        getattr(service, "_account_risk_status", {}) or {})
        runtime = service.runtime_status() if hasattr(service, "runtime_status") else {}
        now_epoch = time.time()
        for field in ("last_fast_cycle_at", "last_slow_cycle_started_at", "last_slow_cycle_at"):
            value = runtime.get(field)
            runtime[field.replace("_at", "_age_seconds")] = (
                round(max(0.0, now_epoch - float(value)), 3) if value else None
            )
        return {
            "ok": (bool(safety.get("ready", False)) and
                   not broker_state.get("error") and db_readable),
            "broker_mode": str(getattr(service.broker, "mode", "paper")),
            "control_state": service.control.state,
            "startup_safety": safety,
            "account_risk": account_risk,
            "broker_error": broker_state.get("error"),
            "cycle_running": service.cycle_running(),
            "cycle_age_seconds": (round(max(0.0, time.time() - float(cycle_ts)), 3)
                                  if cycle_ts else None),
            "db_readable": db_readable,
            "runtime": runtime,
            # M4观察期不把私有流纳入总健康判定；REST仍是账户真值源。
            "exchange_stream": user_stream_status,
            "server_time": time.time(),
        }

    @app.get("/api/ready")
    def api_ready():
        """开仓就绪门：只呈现现有前置条件，不新增或改变任何风险阈值。"""
        latest = _latest_or_empty(service)
        safety = latest.get("startup_safety") or getattr(service, "_startup_safety", {})
        account_risk = (latest.get("account_risk") or
                        getattr(service, "_account_risk_status", {}) or {})
        broker_state = _broker_state_cached(service)
        reasons: list[str] = []
        if broker_state.get("error"):
            reasons.append("broker_state_unavailable")
        if not safety.get("ready", False):
            reasons.append("startup_safety_not_ready")
        if service.control.state != "normal":
            reasons.append(f"control_state_{service.control.state}")
        if not account_risk.get("allow_new", False):
            reasons.append("account_risk_blocks_new")
        payload = {
            "ready": not reasons,
            "reasons": reasons,
            "broker_mode": str(getattr(service.broker, "mode", "paper")),
            "control_state": service.control.state,
            "startup_safety": safety,
            "account_risk": account_risk,
            "server_time": time.time(),
        }
        return JSONResponse(payload, status_code=200 if payload["ready"] else 503)

    @app.get("/api/badges")
    def api_badges():
        """导航角标：只要「当前仍需关注」的量，不做未读追踪（刷新即丢失且无法区分是谁看的）。

        critical 事件限定 24 小时内，红点会随事件过期自然消失，不会永久挂着吓人的数字。
        查询失败一律降级为 0，绝不因为角标这种增强提示影响页面主流程。
        """
        try:
            row = service.db.fetch_one(
                "SELECT COUNT(*) c FROM risk_events WHERE severity='critical' "
                "AND datetime(created_at) >= datetime('now','-1 day')"
            )
            critical = int(row["c"]) if row else 0
        except Exception:
            critical = 0
        try:
            channel_state = _latest_or_empty(service).get("channel_state") or {}
            ai_halted = bool((channel_state.get("ai") or {}).get("halted"))
        except Exception:
            ai_halted = False
        return {"critical_events": critical, "ai_halted": ai_halted, "server_time": time.time()}

    # ---- 轻量价格接口：只给"当前仓位"表快轮询用。
    # 持仓数量/开仓价来自 8 秒缓存的 broker state（变化很少），
    # 最新价/浮盈每次实时取公开行情（不打签名接口），几百毫秒即返回。----
    _prices_cache: dict[str, Any] = {"ts": 0.0, "payload": None}
    _prices_lock = threading.Lock()

    @app.get("/api/prices")
    def api_prices():
        now = time.monotonic()
        with _prices_lock:
            payload = _prices_cache["payload"]
            if payload is not None and now - _prices_cache["ts"] < 1.0:
                return payload
        latest = _latest_or_empty(service)
        broker_state = _broker_state_cached(service)
        positions = _enriched_positions(service, latest, broker_state)
        stream = getattr(app.state, "price_stream", None)
        payload = {
            "positions": positions,
            "unrealized_pnl": round(sum(float(p.get("upnl") or 0.0) for p in positions), 4),
            "server_time": time.time(),
            # 数据链路状态：让用户一眼看清当前是推送还是 REST 轮询
            "stream": {
                "active": bool(stream and stream.active),
                "text": stream.status_text() if stream else "推送未启动",
            },
        }
        with _prices_lock:
            _prices_cache["ts"] = time.monotonic()
            _prices_cache["payload"] = payload
        return payload

    @app.get("/api/orders")
    def api_orders():
        """最近订单（首页表格用）。

        首页的"最近订单"原本是服务端一次性渲染，新订单只有整页刷新才会出现。
        平仓成交后如果还要手动 F5，体验上就像"没记上"，所以这里单独给它一个轻量接口。
        """
        all_rows = service.db.fetch_all("SELECT * FROM orders ORDER BY id ASC LIMIT 2000")
        recent = service.db.fetch_all("SELECT * FROM orders ORDER BY id DESC LIMIT 300")
        orders = _enrich_orders(recent, all_rows, exclude_protection=True)[:30]
        return {
            "orders": [{
                "id": o.get("id"),
                "created_at": o.get("created_at"),
                "symbol": o.get("symbol"),
                "display_side": o.get("display_side"),
                "side": o.get("side"),
                "is_protection": o.get("is_protection", False),
                "notional": o.get("notional"),
                "margin": o.get("margin"),
                "price": o.get("price"),
                "pnl": o.get("pnl"),
                "status": o.get("status"),
                "status_cn": _status_cn(o.get("status")),
            } for o in orders],
        }

    @app.get("/api/channels")
    def api_channels():
        latest = _latest_or_empty(service)
        if latest.get("channel_state"):
            return latest["channel_state"]
        # 首轮尚未完成时，实时读取 ChannelManager，保证页面立即可用
        try:
            state = service.broker.state if isinstance(service.broker.state, dict) else {}
            symbols = [str(p.get("symbol")) for p in state.get("positions", [])]
            prices = get_live_prices(symbols) if symbols else {}
            return service.channels.sync(
                prices,
                session_equity=service.capital.session_equity(),
                read_only=True,
            )
        except Exception:
            return {}

    @app.get("/api/overview")
    def api_overview():
        latest = _latest_or_empty(service)
        state = service.broker.state
        broker_state = state if isinstance(state, dict) else {}
        return {
            "control_state": service.control.state,
            "paper_state": state,
            "broker_error": broker_state.get("error") if isinstance(broker_state, dict) else None,
            "capital_state": {
                "session_equity": service.capital.session_equity(),
                "initial_equity": service.capital.initial_equity,
                "baseline": service.capital.baseline,
                "last_reset": service.capital.last_reset,
            },
            "positions": _enriched_positions(service, latest, broker_state),
            "quota_state": service.quota.status(),
            "decision": latest["decision"].model_dump() if latest["decision"] else None,
            "risk_verdict": latest["risk_verdict"].model_dump() if latest["risk_verdict"] else None,
            "execution": latest["execution"],
        }

    @app.get("/api/agents/latest")
    def api_agents():
        latest = _latest_or_empty(service)
        return {
            "market": latest["market"].model_dump() if latest["market"] else None,
            "macro": latest["macro"].model_dump() if latest["macro"] else None,
            "onchain": latest["onchain"].model_dump() if latest["onchain"] else None,
            "strategy": latest["strategy"].model_dump() if latest["strategy"] else None,
        }

    @app.get("/api/strategy/refs")
    def api_strategy():
        latest = _latest_or_empty(service)
        return latest["strategy"].model_dump() if latest["strategy"] else {"refs": []}

    @app.get("/api/trades")
    def api_trades():
        db = service.db
        return {
            "decisions": [dict(r) for r in db.fetch_all("SELECT * FROM trade_decisions ORDER BY id DESC LIMIT 50")],
            "orders": [dict(r) for r in db.fetch_all("SELECT * FROM orders ORDER BY id DESC LIMIT 50")],
            "positions": [dict(r) for r in db.fetch_all("SELECT * FROM positions ORDER BY id DESC LIMIT 50")],
            "snapshots": [dict(r) for r in db.fetch_all("SELECT * FROM portfolio_snapshots ORDER BY id DESC LIMIT 30")],
        }

    @app.get("/api/equity-curve")
    def api_equity_curve(range: str = "all"):
        """权益曲线。range: 24h / 7d / all（默认全部）。"""
        range = str(range or "all").lower()
        if range in ("24h", "7d"):
            hours = 24 if range == "24h" else 24 * 7
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")
            rows = service.db.fetch_all(
                "SELECT created_at, equity, available_balance, unrealized_pnl, margin_used "
                "FROM portfolio_snapshots WHERE created_at >= ? ORDER BY id ASC LIMIT 5000",
                (cutoff,))
        else:
            rows = service.db.fetch_all(
                "SELECT created_at, equity, available_balance, unrealized_pnl, margin_used "
                "FROM portfolio_snapshots ORDER BY id ASC LIMIT 5000"
            )
        return [
            {
                "ts": str(r["created_at"] or "")[:19].replace("T", " "),
                "equity": round(float(r["equity"] or 0.0), 2),
                "upnl": round(float(r["unrealized_pnl"] or 0.0), 2),
                "margin_used": round(float(r["margin_used"] or 0.0), 2),
            }
            for r in rows
        ]

    @app.get("/api/reviews")
    def api_reviews():
        return [dict(r) for r in service.db.fetch_all("SELECT review_date, report_path, summary FROM daily_reviews ORDER BY review_date DESC LIMIT 30")]

    @app.post("/api/control")
    def api_control(payload: dict[str, Any]):
        state = str(payload.get("state", ""))
        # emergency 会立即市价平掉全部仓位：要求请求体带 confirm 二次确认参数
        if state == "emergency" and str(payload.get("confirm") or "") != "emergency":
            raise HTTPException(status_code=400,
                                detail="emergency 需要二次确认：请求体必须包含 confirm='emergency'")
        result = service.set_control(state)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error"))
        return result

    @app.post("/api/cycle/run")
    def api_run_cycle():
        if service.cycle_running():
            # 不是错误：只是后台慢循环已经在跑，避免重复启动
            return {"ok": True, "started": False, "message": "决策循环正在运行中，本轮完成后自动刷新"}
        started = service.request_run()
        if not started:
            return {"ok": False, "error": "无法启动决策循环"}
        return {"ok": True, "started": True, "message": "已在后台启动，完成后自动刷新状态"}

    # ---- 执行模式切换（写 .env，重启生效；实盘必须先配好 Key）----

    @app.get("/api/mode")
    def api_mode():
        current = str(getattr(service.broker, "mode", "paper"))
        env_vals = _read_env_file(_env_file(service))
        pending = env_vals.get("EXECUTION_MODE", "")
        exec_cfg = service.settings.raw_settings.get("execution", {}) or {}
        key_map = exec_cfg.get("api_key_env_map", {}) or {}
        available = {}
        for m in VISIBLE_MODES:
            key_env = key_map.get(m)
            configured = True
            if key_env:
                val = os.environ.get(key_env, "") or env_vals.get(key_env, "")
                configured = bool(val.strip())
            available[m] = {"label": _mode_cn(m), "configured": configured}
        return {
            "current": current,
            "pending": pending if pending and pending != current else None,
            "available": available,
        }

    @app.post("/api/mode")
    def api_mode_set(payload: dict[str, Any]):
        mode = str(payload.get("mode", ""))
        if mode not in SWITCHABLE_MODES:
            raise HTTPException(status_code=400, detail=f"未知模式：{mode}")
        env_vals = _read_env_file(_env_file(service))
        exec_cfg = service.settings.raw_settings.get("execution", {}) or {}
        key_map = exec_cfg.get("api_key_env_map", {}) or {}
        secret_map = exec_cfg.get("api_secret_env_map", {}) or {}
        if mode == "live":
            key = os.environ.get("BINANCE_LIVE_API_KEY", "") or env_vals.get("BINANCE_LIVE_API_KEY", "")
            secret = os.environ.get("BINANCE_LIVE_API_SECRET", "") or env_vals.get("BINANCE_LIVE_API_SECRET", "")
            if not key.strip() or not secret.strip():
                raise HTTPException(status_code=400,
                                    detail="未配置实盘 API Key（BINANCE_LIVE_API_KEY / BINANCE_LIVE_API_SECRET），拒绝切换")
        _set_env_mode(_env_file(service), mode)
        service.db.log_risk_event(
            event_type="mode_switch_saved", severity="warn" if mode == "live" else "info",
            details=f"执行模式已保存为 {mode}（{_mode_cn(mode)}），等待服务重启生效",
            action_taken="saved to .env")
        return {"ok": True, "mode": mode, "restart_required": True,
                "message": f"已保存为「{_mode_cn(mode)}」，重启服务后生效"}

    # ---- 手动重置资金（仅测试模式）----

    @app.post("/api/capital/reset")
    def api_capital_reset():
        mode = str(getattr(service.broker, "mode", "paper"))
        if mode == "live":
            raise HTTPException(status_code=403, detail="实盘模式不允许重置资金")
        prices: dict[str, float] = {}
        try:
            state = service.broker.state if isinstance(service.broker.state, dict) else {}
            symbols = [str(p.get("symbol")) for p in state.get("positions", []) if p.get("symbol")]
            prices = get_live_prices(symbols) if symbols else {}
        except Exception:
            prices = {}
        result = service.capital.manual_reset(prices)
        if not result.get("ok"):
            raise HTTPException(status_code=400, detail=result.get("error"))
        # 同步刷新双通道日基准，避免重置后通道权益/保证金上限仍显示旧值
        try:
            service.channels.reset_day_start(float(result.get("session_equity") or service.capital.initial_equity))
        except Exception:
            pass
        # 实时刷新页面缓存，让通道资金/会话权益立即显示重置后的值
        service.refresh_after_reset()
        return {"ok": True, "session_equity": result.get("session_equity"),
                "message": f"已平掉全部仓位，资金重置回 {result.get('session_equity')} U"}

    # ---- 风险事件独立页（过滤 + 分页）----

    @app.get("/events", response_class=HTMLResponse)
    def events_page(request: Request, severity: str = "", q: str = "", page: int = 1):
        page = max(1, page)
        page_size = 50
        conds, params = [], []
        if severity in ("info", "warn", "error", "critical"):
            conds.append("severity=?")
            params.append(severity)
        if q.strip():
            conds.append("(event_type LIKE ? OR details LIKE ?)")
            params.extend([f"%{q.strip()}%"] * 2)
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        total = service.db.fetch_one(f"SELECT COUNT(*) n FROM risk_events{where}", tuple(params))["n"]
        rows = service.db.fetch_all(
            f"SELECT * FROM risk_events{where} ORDER BY id DESC LIMIT ? OFFSET ?",
            tuple(params) + (page_size, (page - 1) * page_size))
        pages = max(1, (total + page_size - 1) // page_size)
        severity_counts = {r["severity"]: r["n"] for r in service.db.fetch_all(
            "SELECT severity, COUNT(*) n FROM risk_events GROUP BY severity")}
        return templates.TemplateResponse(request, "events.html", {
            "events": rows, "total": total, "page": page, "pages": pages,
            "severity": severity, "q": q.strip(),
            "severity_counts": severity_counts,
            "console_token": _write_token,
        })

    return app
