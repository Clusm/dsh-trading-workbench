"""SignalScanner：读取 final 策略卡，拉取 K 线，调用 strategy_lab 的 signal_fn。

关键约束：
- 只扫描已完成 K 线（close_time <= now），不把正在形成的 bar 当信号；
- 同一根 bar 只发射一次信号（持久化去重）；
- 信号与 ticket 全部写审计库；
- 不复制 strategy_lab 源码，通过配置的 src 路径动态 import。
"""
from __future__ import annotations

import importlib
import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import numpy as np
import pandas as pd

from audit.database import AuditDatabase
from data.binance_public import _get_json

PUBLIC_BASE = "https://fapi.binance.com"
TF_MINUTES: dict[str, int] = {
    "30m": 30, "1h": 60, "2h": 120, "4h": 240, "8h": 480, "1d": 1440,
}

_KLINES_CACHE: dict[tuple, tuple[float, pd.DataFrame]] = {}
_CACHE_LOCK = threading.Lock()
_KLINES_TTL = 20.0


def resolve_strategy_lab_src(project_root: Path, configured: str | Path | None) -> str:
    """把策略研究目录稳定解析为绝对路径；空配置仍保留为空以触发安全降级。"""
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
        rows = _get_json(client, "/fapi/v1/klines", {
            "symbol": symbol.upper(), "interval": interval, "limit": int(limit),
        })
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f"{symbol} {interval} K线为空或格式错误")

    idx = pd.to_datetime([int(r[0]) for r in rows], unit="ms", utc=True)
    df = pd.DataFrame(index=idx)
    df["open"] = [float(r[1]) for r in rows]
    df["high"] = [float(r[2]) for r in rows]
    df["low"] = [float(r[3]) for r in rows]
    df["close"] = [float(r[4]) for r in rows]
    df["volume"] = [float(r[5]) for r in rows]
    df["close_time"] = [int(r[6]) for r in rows]
    df.index.name = "timestamp"
    df = df[~df.index.duplicated(keep="first")].sort_index()
    _cache_set(key, df)
    return df


def _completed(df: pd.DataFrame, now: datetime) -> pd.DataFrame:
    if df.empty:
        return df
    last_close = int(df["close_time"].iloc[-1])
    if last_close > int(now.timestamp() * 1000):
        return df.iloc[:-1].copy()
    return df.copy()


class SignalScanner:
    """加载外部 strategy_lab 的 final 策略卡并生成有时效的 SignalTicket。

    扫描器只计算确定性信号，不直接下单。外部模块来源、注册表和策略函数任一校验失败
    时安全降级为空票据，避免导入同名错误模块后产生伪信号。
    """
    def __init__(self, knowledge_dir: Path, strategy_lab_src: str,
                 state_file: Path, db: AuditDatabase | None = None,
                 kline_limit: int = 500, ticket_valid_bars: int = 2,
                 universal_cards: bool = False,
                 max_scan_symbols_per_cycle: int = 8):
        self.knowledge_dir = Path(knowledge_dir)
        # 保存原始字符串：Path('') 会规范化为 '.'，导致后续 exists() 恒 True、
        # 空配置检测失效（实测每轮快循环重试 import signals → 16 次/轮误报）
        self._strategy_lab_src_raw = str(strategy_lab_src or "").strip()
        self.strategy_lab_src = Path(self._strategy_lab_src_raw)
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
        if not self._strategy_lab_src_raw or not self.strategy_lab_src.exists():
            # 未配置 strategy_lab（换机/路径不存在）：策略通道降级为无信号，
            # 不再因硬编码绝对路径不存在而反复抛异常拖慢每轮决策。
            raise RuntimeError("strategy_lab 未配置或路径不存在，策略信号扫描跳过")
        src = str(self.strategy_lab_src.resolve())
        if src not in sys.path:
            sys.path.insert(0, src)
        # 注意：本目录下的模块名不要叫 signals/features，避免遮蔽 strategy_lab。
        signals_mod = importlib.import_module("signals")
        features_mod = importlib.import_module("features")
        expected_dir = self.strategy_lab_src.resolve()
        for name, module in (("signals", signals_mod), ("features", features_mod)):
            module_file = Path(str(getattr(module, "__file__", ""))).resolve()
            if module_file.parent != expected_dir:
                raise RuntimeError(
                    f"strategy_lab 模块冲突：{name} 实际加载自 {module_file}，"
                    f"预期目录 {expected_dir}"
                )
        registry = getattr(signals_mod, "REGISTRY", None)
        if not isinstance(registry, dict):
            raise RuntimeError("strategy_lab signals.REGISTRY 缺失或类型错误")
        if not callable(getattr(features_mod, "build_features", None)):
            raise RuntimeError("strategy_lab features.build_features 缺失")
        required = {
            str(card.get("signal_fn") or "")
            for card in load_cards(self.knowledge_dir)
            if card.get("indicatorized") and card.get("signal_fn")
        }
        missing = sorted(required - set(registry))
        if missing:
            raise RuntimeError(f"strategy_lab 缺少策略卡所需 signal_fn: {', '.join(missing)}")
        self._registry = registry
        self._features_mod = features_mod

    def _load_state(self) -> dict[str, Any]:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {"seen": {}}

    def _save_state(self, state: dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

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

            if self.universal_cards and scan_symbols:
                symbols_to_scan = list(scan_symbols)[:self.max_scan_symbols_per_cycle]
            else:
                symbols_to_scan = list(card.get("symbols", []))
                if scan_symbols:
                    for _s in scan_symbols:
                        if _s not in symbols_to_scan:
                            symbols_to_scan.append(_s)
            for symbol in symbols_to_scan:
                for tf in card.get("timeframes", []):
                    if tf not in TF_MINUTES:
                        continue
                    try:
                        raw = fetch_klines(symbol, tf, self.kline_limit)
                        df = _completed(raw, now)
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
                            self._save_state(state)
                        else:
                            seen[dedup_key] = {"signal_id": signal_id, "ts": utc_now().isoformat()}
                        tickets.append(ticket)
                    except Exception as e:
                        if self.db:
                            self.db.log_risk_event(
                                event_type="signal_scan_error", severity="warn",
                                details=f"{card.get('id')} {symbol} {tf}: {type(e).__name__} {e}",
                                action_taken="skip card/symbol/tf")

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
        self._save_state(state)
        if self._features_mod is not None and hasattr(self._features_mod, "clear_feature_cache"):
            self._features_mod.clear_feature_cache()
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
