"""K线逐bar最终性、REST/WS竞态及高周期聚合回归（全离线）。"""
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data import kline_store  # noqa: E402
from data.binance_public import prepare_closed_klines  # noqa: E402


def check(name: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  PASS  {name}")


def event(symbol: str, interval: str, open_ms: int, *, final: bool,
          event_ms: int | None = None, complete: bool = True,
          close: str = "101") -> dict:
    step = kline_store.INTERVAL_MS[interval]
    body = {
        "e": "kline", "E": event_ms or open_ms + step - 1,
        "s": symbol, "k": {
            "s": symbol, "i": interval, "t": open_ms,
            "T": open_ms + step - 1, "o": "100", "h": "102",
            "l": "99", "c": close, "v": "10", "x": final,
        },
    }
    if complete:
        body["k"].update({
            "q": "1000", "n": 3, "V": "6", "Q": "600", "B": "0",
        })
    return body


def main() -> int:
    kline_store._reset_for_test()
    print("\n[1] x=false不会因墙钟跨收盘自动升级")
    now_ms = int(time.time() * 1000)
    one_minute = kline_store.INTERVAL_MS["1m"]
    open_ms = (now_ms // one_minute - 2) * one_minute
    kline_store.push_kline_event(
        event("BTCUSDT", "1m", open_ms, final=False, close="999999")
    )
    rows = kline_store._ROWS[("BTCUSDT", "1m")]
    metadata = kline_store.get_bar_metadata("BTCUSDT", "1m", rows)
    closed, live, quality = prepare_closed_klines(
        rows, "1m", observed_at_ms=now_ms + 10 * one_minute,
        min_closed=1, bar_metadata=metadata,
    )
    check("未定稿bar不进入闭合集合", not closed)
    check("当前bar仍可单独展示", live is not None)
    check("未定稿原因码稳定", quality["reason_code"] == "KLINE_NOT_FINAL", quality)

    print("\n[2] final帧升级、迟到旧帧不能回退、重复帧不重复累计")
    kline_store.push_kline_event(
        event("BTCUSDT", "1m", open_ms, final=True, event_ms=open_ms + one_minute,
              close="101")
    )
    kline_store.push_kline_event(
        event("BTCUSDT", "1m", open_ms, final=False, event_ms=open_ms,
              close="1")
    )
    kline_store.push_kline_event(
        event("BTCUSDT", "1m", open_ms, final=True, event_ms=open_ms + one_minute,
              close="101")
    )
    rows = kline_store._ROWS[("BTCUSDT", "1m")]
    metadata = kline_store.get_bar_metadata("BTCUSDT", "1m", rows)
    meta = metadata[open_ms]
    check("迟到未定稿帧不能覆盖final", meta.final is True and rows[-1][4] == "101", meta)
    check("重复final帧revision不重复递增", meta.revision == 2, meta)
    closed, _, quality = prepare_closed_klines(
        rows, "1m", observed_at_ms=open_ms + 2 * one_minute,
        min_closed=1, bar_metadata=metadata,
    )
    check("final证据可用于闭合指标", len(closed) == 1 and quality["ok"], quality)

    print("\n[3] 旧缓存保持UNKNOWN并触发可信REST回补")
    kline_store._reset_for_test()
    cache_dir = Path(tempfile.mkdtemp(prefix="mta-kline-finality-"))
    kline_store.configure_kline_store(cache_dir)
    old_open = (int(time.time() * 1000) // one_minute - 3) * one_minute
    legacy_rows = [[
        old_open, "1", "2", "0.5", "1.5", "10", old_open + one_minute - 1,
        "15", 1, "5", "7", "0",
    ], [
        old_open + one_minute, "1", "2", "0.5", "1.5", "10",
        old_open + 2 * one_minute - 1, "15", 1, "5", "7", "0",
    ]]
    (cache_dir / "ETHUSDT_1m.json").write_text(
        json.dumps({"schema_version": 1, "rows": legacy_rows,
                    "updated_at": time.time()}), encoding="utf-8",
    )
    calls: list[dict] = []

    def fetch(_client, _path, params):
        calls.append(dict(params))
        return legacy_rows

    refreshed = kline_store.get_klines(object(), "ETHUSDT", "1m", 2, fetcher=fetch)
    refreshed_meta = kline_store.get_bar_metadata("ETHUSDT", "1m", refreshed)
    check("旧缓存不会直接命中", len(calls) == 1, calls)
    check("可信REST逐根升级final证据",
          all(item.final is True for item in refreshed_meta.values()), refreshed_meta)
    kline_store._reset_for_test()
    kline_store.configure_kline_store(cache_dir)
    reloaded = kline_store.get_bar_metadata("ETHUSDT", "1m")
    check("schema v2重启后保留逐bar证据",
          reloaded and all(item.final is True for item in reloaded.values()), reloaded)

    print("\n[4] 高周期只有全部组件final且字段完整才定稿")
    kline_store._reset_for_test()
    base_step = kline_store.INTERVAL_MS["15m"]
    hour_open = (int(time.time() * 1000) // kline_store.INTERVAL_MS["1h"] - 2) \
        * kline_store.INTERVAL_MS["1h"]
    for index in range(3):
        kline_store.push_kline_event(
            event("SOLUSDT", "15m", hour_open + index * base_step, final=True)
        )
    high_rows = kline_store._ROWS.get(("SOLUSDT", "1h"), [])
    check("组件数不足时不产生1h定稿", not high_rows or
          kline_store.get_bar_metadata("SOLUSDT", "1h", high_rows)[hour_open].final is not True)
    kline_store.push_kline_event(
        event("SOLUSDT", "15m", hour_open + 3 * base_step, final=True)
    )
    high_rows = kline_store._ROWS.get(("SOLUSDT", "1h"), [])
    high_meta = kline_store.get_bar_metadata("SOLUSDT", "1h", high_rows)
    check("四个完整final组件生成1h定稿", high_meta[hour_open].final is True, high_meta)
    check("重复组件不会把成交量再次相加",
          float(high_rows[0][5]) == 40.0, high_rows[0])
    kline_store.push_kline_event(
        event("SOLUSDT", "15m", hour_open + 4 * base_step, final=True,
              complete=False)
    )
    next_hour = hour_open + kline_store.INTERVAL_MS["1h"]
    check("扩展成交字段未知时不伪造高周期聚合",
          next_hour not in {int(row[0]) for row in kline_store._ROWS.get(("SOLUSDT", "1h"), [])})

    print("\n[5] 缺15m组件时不能补出高周期bar")
    kline_store._reset_for_test()
    gap_open = (int(time.time() * 1000) // kline_store.INTERVAL_MS["1h"] - 2) \
        * kline_store.INTERVAL_MS["1h"]
    for index in (0, 1, 3):
        kline_store.push_kline_event(
            event("XRPUSDT", "15m", gap_open + index * base_step, final=True)
        )
    gap_rows = kline_store._ROWS.get(("XRPUSDT", "1h"), [])
    gap_meta = kline_store.get_bar_metadata("XRPUSDT", "1h", gap_rows)
    check("中间缺口不会生成1h定稿",
          not gap_rows or all(item.final is not True for item in gap_meta.values()), gap_meta)

    print("\n结果：PASS 13 / FAIL 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
