"""策略研究模块接入冒烟测试：不访问网络、不写真实状态、不产生订单。"""
from __future__ import annotations

import json
import sys
import tempfile
import warnings
from pathlib import Path

import numpy as np
import pandas as pd
from pandas.errors import PerformanceWarning

warnings.filterwarnings("ignore", category=PerformanceWarning)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.config_loader import load_settings  # noqa: E402
from signal_engine import SignalScanner, resolve_strategy_lab_src  # noqa: E402


def synthetic_ohlcv(rows: int = 300) -> pd.DataFrame:
    index = pd.date_range("2026-01-01", periods=rows, freq="h", tz="UTC")
    trend = np.linspace(3000.0, 3300.0, rows)
    wave = np.sin(np.arange(rows) / 8.0) * 25.0
    close = trend + wave
    open_ = close - np.cos(np.arange(rows) / 7.0) * 4.0
    high = np.maximum(open_, close) + 8.0
    low = np.minimum(open_, close) - 8.0
    volume = 1000.0 + np.sin(np.arange(rows) / 5.0) * 100.0
    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=index,
    )


def main() -> None:
    settings = load_settings(ROOT)
    cfg = settings.raw_settings.get("signal_scanner", {}) or {}
    strategy_src = resolve_strategy_lab_src(ROOT, cfg.get("strategy_lab_src"))
    with tempfile.TemporaryDirectory(prefix="mta-signal-smoke-") as tmp:
        scanner = SignalScanner(
            knowledge_dir=settings.knowledge_dir,
            strategy_lab_src=strategy_src,
            state_file=Path(tmp) / "signal_state.json",
        )
        scanner._load_strategy_lab()
        cards = json.loads(
            (settings.knowledge_dir / "strategy_cards.json").read_text(encoding="utf-8")
        ).get("cards", [])
        frame = synthetic_ohlcv()
        checked: set[str] = set()
        for card in cards:
            signal_fn = str(card.get("signal_fn") or "")
            if not card.get("indicatorized") or not signal_fn or signal_fn in checked:
                continue
            out = scanner._registry[signal_fn](frame.copy(), card.get("params") or {})
            assert len(out) == len(frame), signal_fn
            assert {"side", "signal"}.issubset(out.columns), signal_fn
            assert set(out["side"].dropna().unique()).issubset({"", "LONG", "SHORT"}), signal_fn
            checked.add(signal_fn)
        assert checked, "没有可验证的策略函数"
        assert Path(strategy_src).is_dir(), strategy_src
        print(f"SIGNAL_SCANNER_INTEGRATION_OK functions={len(checked)} src={strategy_src}")


if __name__ == "__main__":
    main()
