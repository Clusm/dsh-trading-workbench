"""SignalScanner：读取 final 策略卡，拉取 K 线，调用内置策略运行时。

关键约束：
- 只扫描已完成 K 线（close_time <= now），不把正在形成的 bar 当信号；
- 同一根 bar 只发射一次信号（持久化去重）；
- 信号与 ticket 全部写审计库；
- 策略函数位于命名空间隔离的 ``strategy_runtime``，不依赖项目外路径。
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

from audit.database import AuditDatabase
import strategy_runtime
from data.binance_public import _get_json
from data.kline_store import (
    KlineDataQualityError,
    get_bar_metadata,
    get_klines,
    validate_kline_rows,
)
from data.rate_limit import PublicMarketBudgetDeferred, PublicMarketCooldown

PUBLIC_BASE = "https://fapi.binance.com"
TF_MINUTES: dict[str, int] = {
    "30m": 30, "1h": 60, "2h": 120, "4h": 240, "8h": 480, "1d": 1440,
}

_KLINES_CACHE: dict[tuple, tuple[float, pd.DataFrame]] = {}
_CACHE_LOCK = threading.Lock()
_KLINES_TTL = 20.0


def resolve_strategy_lab_src(project_root: Path, configured: str | Path | None) -> str:
    """兼容旧调用方的路径规范化函数。

    实时扫描已使用内置 ``strategy_runtime``，返回值只用于旧配置和调用方平滑迁移，
    不再作为模块导入路径。
    """
    raw = str(configured or "").strip()
    if not raw:
        return ""
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = Path(project_root) / path
    return str(path.resolve())


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def load_cards(knowledge_dir: Path, file_name: str = "strategy_cards.json") -> list[dict[str, Any]]:
    p = knowledge_dir / file_name
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    return list(data.get("cards", []))


def _cache_get(key: tuple) -> pd.DataFrame | None:
    with _CACHE_LOCK:
        item = _KLINES_CACHE.get(key)
        if item and (datetime.now(timezone.utc) - item[0]).total_seconds() < _KLINES_TTL:
            return item[1]
    return None


def _cache_set(key: tuple, value: pd.DataFrame) -> None:
    with _CACHE_LOCK:
        _KLINES_CACHE[key] = (datetime.now(timezone.utc), value)
        if len(_KLINES_CACHE) > 64:
            for k in list(_KLINES_CACHE)[:-32]:
                _KLINES_CACHE.pop(k, None)


def fetch_klines(symbol: str, interval: str, limit: int = 500) -> pd.DataFrame:
    """拉取币安 U 本位 K 线并标准化为 strategy_lab 期望的 DataFrame。

    index: DatetimeIndex(UTC, tz-aware)
    columns: open/high/low/close/volume
    """
    if interval not in TF_MINUTES:
        raise ValueError(f"不支持的 interval: {interval}")
    key = (symbol, interval, limit)
    cached = _cache_get(key)
    if cached is not None:
        return cached

    with httpx.Client(timeout=20.0) as client:
        rows = get_klines(
            client, symbol.upper(), interval, int(limit), fetcher=_get_json,
        )
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"{symbol} {interval} K线为空或格式错误")
    bar_metadata = get_bar_metadata(symbol, interval, rows)
    quality = validate_kline_rows(
        rows, interval, min_rows=210, bar_metadata=bar_metadata,
        require_finality=True,
    )
    if not quality["ok"]:
        raise KlineDataQualityError(
            f"{symbol} {interval} K线质量不合格: {','.join(quality['issues'])}",
            quality,
        )

    idx = pd.to_datetime([int(r[0]) for r in rows], unit="ms", utc=True)
    df = pd.DataFrame(index=idx)
    df["open"] = [float(r[1]) for r in rows]
    df["high"] = [float(r[2]) for r in rows]
    df["low"] = [float(r[3]) for r in rows]
    df["close"] = [float(r[4]) for r in rows]
    df["volume"] = [float(r[5]) for r in rows]
    df["close_time"] = [int(r[6]) for r in rows]
    # 保留逐bar证据列，_completed 不会在墙钟跨过 close_time 后自动升级旧缓存。
    df["_bar_final"] = [
        bool(bar_metadata.get(int(r[0])).final is True)
        for r in rows
    ]
    df["_bar_complete"] = [
        bool(bar_metadata.get(int(r[0])).complete)
        for r in rows
    ]
    df["_bar_revision"] = [
        int(bar_metadata.get(int(r[0])).revision)
        for r in rows
    ]
    df["_bar_source"] = [
        str(bar_metadata.get(int(r[0])).source)
        for r in rows
    ]
    df.index.name = "timestamp"
    df = df[~df.index.duplicated(keep="first")].sort_index()
    _cache_set(key, df)
    return df


def _completed(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    if df.empty:
        return df
    # 不假设未完成 bar 一定在最后：代理重排或恢复缓存时可能出现多个 live bar；
    # 逐行按 close_time 过滤，保证指标只消费已经收盘的数据。
    completed = df[df["close_time"] <= int(now.timestamp() * 1000)].copy()
    if "_bar_final" in completed.columns:
        completed = completed[
            completed["_bar_final"].eq(True)
            & completed["_bar_complete"].eq(True)
        ]
    return completed.sort_index()


def _is_market_transport_outage(exc: Exception) -> bool:
    """判断是否为会影响整轮请求的基础设施故障，而非单一交易对参数错误。"""
    if isinstance(exc, (PublicMarketCooldown, PublicMarketBudgetDeferred)):
        return True
    if isinstance(exc, httpx.TransportError):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in {418, 429} or exc.response.status_code >= 500
    text = str(exc).lower()
    return any(marker in text for marker in (
        "公开行情冷却", "rate limit", "temporary ip ban", "getaddrinfo failed",
    ))


def _is_market_deferred(exc: Exception) -> bool:
    """受控冷却/预算让步属于预期降级，不应伪装成策略扫描故障。"""
    return isinstance(exc, (PublicMarketCooldown, PublicMarketBudgetDeferred))


class SignalScanner:
    """加载内置策略运行时的 final 策略卡并生成有时效的 SignalTicket。

    扫描器只计算确定性信号，不直接下单。外部模块来源、注册表和策略函数任一校验失败
    时安全降级为空票据，避免策略卡与注册表漂移后产生伪信号。
    """
    def __init__(self, knowledge_dir: Path, strategy_lab_src: str,
                 state_file: Path, db: AuditDatabase | None = None,
                 kline_limit: int = 500, ticket_valid_bars: int = 2,
                 universal_cards: bool = False,
                 max_scan_symbols_per_cycle: int = 8):
        self.knowledge_dir = Path(knowledge_dir)
        # 参数保留至一次兼容发布结束，避免外部运行脚本的构造调用失效。运行时不再
        # 从这个路径加载模块；策略研究仓库可以独立移动、归档或不存在。
        self.strategy_lab_src = str(strategy_lab_src or "").strip()
        self.state_file = Path(state_file)
        self.db = db
        self.kline_limit = int(kline_limit)
        self.ticket_valid_bars = int(ticket_valid_bars)
        self.universal_cards = bool(universal_cards)
        self.max_scan_symbols_per_cycle = int(max_scan_symbols_per_cycle)
        self._registry: dict[str, Any] | None = None
        self._features_mod: Any | None = None

    def _load_strategy_lab(self) -> None:
        if self._registry is not None:
            return
        registry = getattr(strategy_runtime, "REGISTRY", None)
        if not isinstance(registry, dict):
            raise RuntimeError("strategy_runtime.REGISTRY 缺失或类型错误")
        if not callable(getattr(strategy_runtime, "build_features", None)):
            raise RuntimeError("strategy_runtime.build_features 缺失")
        required = {
            str(card.get("signal_fn") or "")
            for card in load_cards(self.knowledge_dir)
            if card.get("indicatorized") and card.get("signal_fn")
        }
        missing = sorted(required - set(registry))
        if missing:
            raise RuntimeError(f"strategy_runtime 缺少策略卡所需 signal_fn: {', '.join(missing)}")
        self._registry = registry
        self._features_mod = strategy_runtime

    def _load_state(self) -> dict[str, Any]:
        if self.state_file.exists():
            try:
                state = json.loads(self.state_file.read_text(encoding="utf-8"))
                state.setdefault("seen", {})
                state.setdefault("scan_cursor", 0)
                return state
            except Exception:
                pass
        return {"seen": {}, "scan_cursor": 0}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_file.with_suffix(f"{self.state_file.suffix}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.state_file)

    def _select_cycle_symbols(self, scan_symbols: list[str] | None,
                              state: dict[str, Any]) -> list[str]:
        """从通用扫描池循环取本轮窗口，避免固定 ``[:N]`` 永久饿死尾部标的。"""
        pool: list[str] = []
        for raw in scan_symbols or []:
            symbol = str(raw).upper().strip()
            if symbol.endswith("USDT") and symbol not in pool:
                pool.append(symbol)
        if not self.universal_cards or not pool:
            return []
        count = min(len(pool), max(1, self.max_scan_symbols_per_cycle))
        cursor = int(state.get("scan_cursor") or 0) % len(pool)
        selected = [pool[(cursor + offset) % len(pool)] for offset in range(count)]
        state["scan_cursor"] = (cursor + count) % len(pool)
        return selected

    @staticmethod
    def _card_allows(card: dict[str, Any], side: str) -> bool:
        direction = str(card.get("direction", "LONG_SHORT")).upper()
        side = str(side or "").upper()
        if direction == "LONG_SHORT":
            return side in ("LONG", "SHORT")
        return side == direction

    def scan(self, now: datetime | None = None,
             scan_symbols: list[str] | None = None) -> list[dict[str, Any]]:
        """扫描全部 final 策略卡，返回新触发的 ticket dict 列表。"""
        self._load_strategy_lab()
        now = now or utc_now()
        state = self._load_state()
        seen = dict(state.get("seen") or {})
        tickets: list[dict[str, Any]] = []
        cards = load_cards(self.knowledge_dir)
        cycle_symbols = self._select_cycle_symbols(scan_symbols, state)
        allowed_symbols = {
            str(symbol).upper().strip() for symbol in (scan_symbols or []) if symbol
        }
        scanned_symbols: list[str] = []
        # 多张策略卡经常复用同一 symbol/timeframe。成功结果和失败结果都在本轮
        # 记忆，避免一次 DNS 故障被“策略卡×交易对×周期”放大为数百次请求和告警。
        cycle_klines: dict[tuple[str, str], pd.DataFrame | None] = {}
        scan_had_error = False
        scan_had_success = False
        scan_errors: list[str] = []
        transport_outage: str | None = None
        deferred_reason: str | None = None

        # 旧实现只在“本轮刚触发的 ticket”进入执行器时检查过期，导致从未再次
        # 发射的 open ticket 永久滞留。扫描开始时统一收口历史过期票据。
        expire = getattr(self.db, "expire_open_signal_tickets", None)
        expired_tickets = int(expire(now.isoformat())) if callable(expire) else 0

        for card in cards:
            if not card.get("indicatorized"):
                continue
            signal_fn = str(card.get("signal_fn") or "")
            if signal_fn not in self._registry:
                if self.db:
                    self.db.log_risk_event(
                        event_type="signal_fn_missing", severity="warn",
                        details=f"{card.get('id')}: 未注册 signal_fn={signal_fn}",
                        action_taken="skip card")
                continue

            if self.universal_cards and cycle_symbols:
                symbols_to_scan = cycle_symbols
            else:
                # 非通用模式严格执行策略卡的验证范围；scan_symbols 只充当交易品种
                # 白名单，不能再追加到每张卡，否则 universal_cards 开关形同虚设。
                symbols_to_scan = []
                for raw in card.get("symbols", []):
                    symbol = str(raw).upper().strip()
                    if not symbol.endswith("USDT"):
                        continue
                    if allowed_symbols and symbol not in allowed_symbols:
                        continue
                    if symbol not in symbols_to_scan:
                        symbols_to_scan.append(symbol)
            for symbol in symbols_to_scan:
                if symbol not in scanned_symbols:
                    scanned_symbols.append(symbol)
                for tf in card.get("timeframes", []):
                    if tf not in TF_MINUTES:
                        continue
                    market_key = (symbol, tf)
                    if market_key not in cycle_klines:
                        if transport_outage is not None:
                            cycle_klines[market_key] = None
                        else:
                            try:
                                cycle_klines[market_key] = _completed(
                                    fetch_klines(symbol, tf, self.kline_limit), now
                                )
                                scan_had_success = True
                            except Exception as e:
                                cycle_klines[market_key] = None
                                detail = f"market_data {symbol} {tf}: {type(e).__name__} {e}"
                                if _is_market_deferred(e):
                                    # 全局健康接口已经精确暴露冷却剩余时间和预算水位。
                                    # 这里静默让步，避免每轮把正常熔断累计成扫描风险事件。
                                    deferred_reason = type(e).__name__
                                else:
                                    scan_had_error = True
                                    scan_errors.append(detail)
                                if _is_market_transport_outage(e):
                                    # DNS、TLS、限流冷却等故障会影响整轮。首次失败后立即
                                    # 短路剩余 K 线请求，避免 8 组请求把慢循环拖住 1~2 分钟。
                                    transport_outage = detail
                    cached_df = cycle_klines[market_key]
                    if cached_df is None:
                        continue
                    try:
                        df = cached_df.copy()
                        if len(df) < 210:
                            continue
                        df = df[["open", "high", "low", "close", "volume"]].copy()
                        fn = self._registry[signal_fn]
                        out = fn(df, card.get("params") or {})
                        if out is None or len(out) != len(df):
                            continue
                        last = out.iloc[-1]
                        if not bool(last.get("signal", 0)):
                            continue
                        side = str(last.get("side") or "")
                        if not self._card_allows(card, side):
                            continue
                        f = self._features_mod.build_features(df)
                        need = ["adx_14", "rsi_14", "ema_200", "macd_signal", "atr_14"]
                        if f[need].iloc[-1].isna().any():
                            continue

                        bar_open_ms = int(df.index[-1].value // 1_000_000)
                        dedup_key = f"{card['id']}|{symbol}|{tf}|{bar_open_ms}"
                        if dedup_key in seen:
                            continue

                        signal_id = f"sig-{card['id']}-{symbol}-{tf}-{bar_open_ms}"
                        signal_price = float(df["close"].iloc[-1])
                        signal_time = df.index[-1].to_pydatetime().isoformat()
                        ticket = self._make_ticket(card, symbol, tf, side,
                                                   signal_id, signal_price,
                                                   signal_time, bar_open_ms, f)
                        if ticket is None:
                            continue

                        if self.db:
                            existing_signal = self.db.fetch_one(
                                "SELECT 1 FROM signal_events WHERE signal_id=?", (signal_id,))
                            if existing_signal is None:
                                self.db.log_signal_event(
                                    signal_id=signal_id,
                                    strategy_id=str(card["id"]),
                                    symbol=symbol, timeframe=tf, side=side,
                                    signal_time=signal_time,
                                    signal_price=signal_price,
                                    bar_open_time=bar_open_ms,
                                    params=card.get("params") or {},
                                    status="triggered",
                                    raw={"ticket_id": ticket["ticket_id"]},
                                )
                                self.db.log_signal_ticket(
                                    ticket_id=ticket["ticket_id"],
                                    signal_id=signal_id,
                                    strategy_id=str(card["id"]),
                                    symbol=symbol, side=side,
                                    signal_time=signal_time,
                                    signal_price=signal_price,
                                    entry_zone_low=ticket["entry_zone_low"],
                                    entry_zone_high=ticket["entry_zone_high"],
                                    expires_at=ticket["expires_at"],
                                    stop_loss_pct=float(ticket["stop_loss_pct"]),
                                    take_profit_pct=float(ticket["take_profit_pct"]),
                                    time_stop_hours=int(ticket["time_stop_hours"]),
                                    status="open",
                                    raw=ticket,
                                )
                            seen[dedup_key] = {"signal_id": signal_id, "ts": utc_now().isoformat()}
                            state["seen"] = seen
                            self._save_state(state)
                        else:
                            seen[dedup_key] = {"signal_id": signal_id, "ts": utc_now().isoformat()}
                        tickets.append(ticket)
                    except Exception as e:
                        scan_had_error = True
                        scan_errors.append(
                            f"{card.get('id')} {symbol} {tf}: {type(e).__name__} {e}"
                        )

        try:
            cutoff = (utc_now() - timedelta(days=14)).isoformat()
            cleaned: dict[str, Any] = {}
            for k, v in seen.items():
                ts = str(v.get("ts") or v or "") if isinstance(v, (dict, str)) else ""
                if not ts or ts >= cutoff:
                    cleaned[k] = v
            state["seen"] = cleaned
        except Exception:
            state["seen"] = seen
        state["last_scan"] = {
            "at": now.isoformat(),
            "mode": "universal_rotation" if self.universal_cards else "validated_card_symbols",
            "symbols": scanned_symbols,
            "market_pairs": len(cycle_klines),
            "successful_pairs": sum(1 for frame in cycle_klines.values() if frame is not None),
            "failed_pairs": sum(1 for frame in cycle_klines.values() if frame is None),
            "error_count": len(scan_errors),
            "status": "deferred" if deferred_reason else (
                "error" if scan_errors else "ok"
            ),
            "deferred_reason": deferred_reason,
            "tickets": len(tickets),
            "expired_tickets": expired_tickets,
        }
        self._save_state(state)
        if self._features_mod is not None and hasattr(self._features_mod, "clear_feature_cache"):
            self._features_mod.clear_feature_cache()
        # 至少有一组行情完整处理、且本轮没有扫描异常，才能证明旧故障已经恢复。
        # 只靠“返回了 0 个信号”不能判健康，因为网络全失败时同样会返回空列表。
        resolver = getattr(self.db, "resolve_risk_incidents_by_type", None)
        if scan_had_success and not scan_had_error and callable(resolver):
            resolver("signal_scan_error")
        elif scan_errors and self.db:
            preview = " | ".join(scan_errors[:4])
            if len(scan_errors) > 4:
                preview += f" | 另有 {len(scan_errors) - 4} 项"
            self.db.log_risk_event(
                event_type="signal_scan_error", severity="warn",
                details=f"本轮扫描异常 {len(scan_errors)} 项: {preview}",
                action_taken="仅跳过异常 symbol/timeframe；本轮合并为一条告警",
            )
        return tickets

    def _make_ticket(self, card: dict[str, Any], symbol: str, tf: str, side: str,
                     signal_id: str, signal_price: float, signal_time: str,
                     bar_open_ms: int, features: pd.DataFrame) -> dict[str, Any] | None:
        exits = card.get("exits") or {}
        sl_pct = float(exits.get("sl_pct") or 0.0)
        tp_pct = float(exits.get("tp_pct") or 0.0)
        if sl_pct <= 0 or tp_pct <= 0:
            return None
        atr = float(features["atr_14"].iloc[-1])
        if not np.isfinite(atr) or atr <= 0:
            atr = float((features["high"] - features["low"]).tail(14).mean())
        if side == "LONG":
            zone_low = signal_price - 0.8 * atr
            zone_high = signal_price + 1.5 * atr
        else:
            zone_low = signal_price - 1.5 * atr
            zone_high = signal_price + 0.8 * atr
        expires_at = (df_index_to_dt(features.index[-1])
                      + timedelta(minutes=(self.ticket_valid_bars + 1) * TF_MINUTES[tf])).isoformat()
        return {
            "ticket_id": f"t-{card['id']}-{symbol}-{tf}-{bar_open_ms}",
            "signal_id": signal_id,
            "strategy_id": str(card["id"]),
            "symbol": symbol,
            "side": side,
            "timeframe": tf,
            "signal_time": signal_time,
            "signal_price": signal_price,
            "entry_zone_low": round(zone_low, 8),
            "entry_zone_high": round(zone_high, 8),
            "expires_at": expires_at,
            "stop_loss_pct": sl_pct,
            "take_profit_pct": tp_pct,
            "time_stop_hours": int(exits.get("time_stop_hours") or 48),
            "entry_mode_hint": "ai_minute",
            "source": "strategy_lab_final",
        }


def df_index_to_dt(idx: Any) -> datetime:
    ts = pd.Timestamp(idx)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return ts.to_pydatetime()


def run_signal_scan(*, knowledge_dir: Path, strategy_lab_src: str,
                    state_file: Path, db: AuditDatabase | None = None,
                    kline_limit: int = 500, ticket_valid_bars: int = 2,
                    now: datetime | None = None,
                    scan_symbols: list[str] | None = None,
                    universal_cards: bool = False,
                    max_scan_symbols_per_cycle: int = 8) -> list[dict[str, Any]]:
    scanner = SignalScanner(
        knowledge_dir=knowledge_dir, strategy_lab_src=strategy_lab_src,
        state_file=state_file, db=db, kline_limit=kline_limit,
        ticket_valid_bars=ticket_valid_bars,
        universal_cards=universal_cards,
        max_scan_symbols_per_cycle=max_scan_symbols_per_cycle,
    )
    return scanner.scan(now=now, scan_symbols=scan_symbols)
