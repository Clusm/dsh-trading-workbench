"""情报分层传递的离线单元测试。

不联网、不碰数据库，用构造的 brief 验证：
  1. 持仓标的保留完整指标，非持仓压成索引行
  2. 三份简报的 errors 都不再进提示词，只留一句可读的降级提示
  3. _held_symbols 能从 portfolio 与 execution_feedback 收集持仓
  4. build_user_prompt 端到端可调用，且分层真的生效
  5. 向后兼容：held_symbols=None 时保持全量

运行：python scripts/test_prompt_layering.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agents.models import MacroBrief, MarketBrief, OnchainBrief, StrategyRefs  # noqa: E402
from agents.orchestrator import (  # noqa: E402
    _held_symbols,
    _slim_macro,
    _slim_market,
    _slim_onchain,
    build_user_prompt,
)

PASS = 0
FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}" + (f"  <- {detail}" if detail else ""))


def section(title: str) -> None:
    print()
    print("=" * 68)
    print(title)
    print("=" * 68)


# 完整指标用 25 字段里的代表性字段；索引行只取 7 个
def make_metric(symbol: str, full: bool = True) -> dict:
    m = {
        "symbol": symbol, "last_price": 100.0, "pct_24h": -2.5,
        "trend_15m": "down", "trend_1h": "down", "funding_rate": 0.0001,
        "oi_usd": 1.2e8,
    }
    if full:
        m.update({
            "volume_ratio_15m": 1.4, "atr_pct_15m": 0.32, "atr_pct_1h": 0.55,
            "range_position_15m": 0.21, "high_10h": 108.0, "low_10h": 96.0,
            "return_10h_pct": -3.1, "trend_4h": "down", "trend_1d": "down",
            "atr_pct_4h": 0.9, "atr_pct_1d": 2.2, "return_7d_pct": -8.0,
            "high_7d": 130.0, "low_7d": 92.0, "spread_pct": 0.01,
            "bid_imbalance": 0.44, "long_short_ratio": 0.9,
            "taker_buy_sell_ratio": 0.8,
        })
    return m


HELD = {"ETHUSDT", "SOLUSDT", "ZECUSDT", "BTCUSDT"}
SYMS = ["UNIUSDT", "ARBUSDT", "ZECUSDT", "SOLUSDT", "XRPUSDT",
        "ETHUSDT", "BTCUSDT", "DOGEUSDT", "BNBUSDT", "NEARUSDT"]


def make_market(errors=None) -> MarketBrief:
    return MarketBrief(
        regime="trend_down",
        top_movers=[f"{s} -2.50%" for s in SYMS[:5]],
        anomalies=[],
        last_prices={s: 100.0 for s in SYMS},
        metrics=[make_metric(s) for s in SYMS],
        summary="扫描 10 个标的，市场状态=trend_down。",
        errors=errors or [],
    )


section("用例 1：持仓保留完整指标，非持仓压成索引行")
mk = make_market()
lay = _slim_market(mk, HELD)
full_count = {m["symbol"]: len(m) for m in lay["metrics"]}
for s in ("ZECUSDT", "SOLUSDT", "ETHUSDT", "BTCUSDT"):
    check(f"持仓 {s} 保留完整 {full_count[s]} 字段", full_count[s] >= 20, f"实际 {full_count.get(s)}")
for s in ("UNIUSDT", "ARBUSDT", "XRPUSDT", "DOGEUSDT"):
    check(f"候选 {s} 压成索引行 {full_count[s]} 字段", full_count[s] <= 8, f"实际 {full_count.get(s)}")

section("用例 2：分层后体积明显下降")
full = _slim_market(mk)
jf, jl = json.dumps(full, ensure_ascii=False), json.dumps(lay, ensure_ascii=False)
check(f"体积下降（{len(jf)} -> {len(jl)} 字符）", len(jl) < len(jf) * 0.75,
      f"降幅仅 {(len(jf)-len(jl))/len(jf)*100:.0f}%")
check("生成了 metrics_note 引导按需查询", "metrics_note" in lay)

section("用例 3：向后兼容 —— held_symbols=None 时保持全量")
compat = _slim_market(mk)
check("None 时所有标的都是完整字段",
      all(len(m) >= 20 for m in compat["metrics"]))
check("None 时不生成 metrics_note", "metrics_note" not in compat)

section("用例 4：三份简报的 errors 都不进提示词")
mb = make_market(errors=["BTCUSDT: ReadTimeout", "ETHUSDT: ReadTimeout"])
sm = _slim_market(mb, HELD)
check("market errors 已移除", "errors" not in sm)
check("market 生成 data_note", "data_note" in sm and "2 项" in sm["data_note"],
      str(sm.get("data_note")))

mc = MacroBrief(risk_events_24h=["FOMC 会议"], market_bias="neutral",
                notes=["标普 500 7631.47"], summary="宏观状态=neutral。",
                errors=["SPX: 403 Forbidden"])
sc = _slim_macro(mc)
check("macro errors 已移除", "errors" not in sc)
check("macro 生成 data_note", "data_note" in sc, str(sc.get("data_note")))

oc = OnchainBrief(stablecoin_change_24h="+0.27%", tvl_change_24h="-0.57%",
                  whale_activity=["DefiLlama稳定币TOP: Tether $183B"],
                  summary="稳定币24h +0.27%。",
                  errors=["CoinGecko: ReadTimeout"])
so = _slim_onchain(oc)
check("onchain errors 已移除", "errors" not in so)
check("onchain 生成 data_note", "data_note" in so, str(so.get("data_note")))

section("用例 5：无错误时不产生多余的 data_note")
clean_m = _slim_market(make_market(), HELD)
check("market 无错误时无 data_note", "data_note" not in clean_m)
clean_o = _slim_onchain(OnchainBrief(summary="ok"))
check("onchain 无错误时无 data_note", "data_note" not in clean_o)

section("用例 6：_held_symbols 从 portfolio 与 execution_feedback 收集")
pf = {"positions": [{"symbol": "ETHUSDT"}, {"symbol": "ZECUSDT"}, {}]}
ef = {"positions": [{"symbol": "SOLUSDT"}]}
got = _held_symbols(pf, ef)
check(f"合并收集到 {sorted(got)}", got == {"ETHUSDT", "ZECUSDT", "SOLUSDT"}, str(sorted(got)))
check("空输入返回空集合", _held_symbols(None, None) == set())
check("脏数据不炸（非 dict 元素）", _held_symbols({"positions": ["x", None]}) == set())

section("用例 7：build_user_prompt 端到端可调用且分层生效")
prompt = build_user_prompt(
    market=make_market(),
    macro=MacroBrief(risk_events_24h=["FOMC 会议"], market_bias="neutral",
                     notes=["标普 7631"], summary="宏观=neutral"),
    onchain=OnchainBrief(stablecoin_change_24h="+0.27%", summary="链上ok"),
    strategy=StrategyRefs(),
    risk_envelope={"equity": 500.0},
    portfolio={"cash": 100.0, "positions": [
        {"symbol": "ETHUSDT", "qty": 0.1, "side": "SHORT"},
        {"symbol": "ZECUSDT", "qty": 0.5, "side": "LONG"},
    ]},
)
check("成功返回字符串", isinstance(prompt, str) and len(prompt) > 500)
payload = json.loads(prompt)
mb_out = payload["market_brief"]
held_out = [m for m in mb_out["metrics"] if m["symbol"] in {"ETHUSDT", "ZECUSDT"}]
other_out = [m for m in mb_out["metrics"] if m["symbol"] not in {"ETHUSDT", "ZECUSDT"}]
check("端到端：持仓保留完整字段", all(len(m) >= 20 for m in held_out),
      str([(m["symbol"], len(m)) for m in held_out]))
check("端到端：非持仓压成索引行", all(len(m) <= 8 for m in other_out),
      str([(m["symbol"], len(m)) for m in other_out]))
check("端到端：三份简报都无 errors",
      all("errors" not in payload[k] for k in ("market_brief", "macro_brief", "onchain_brief")))
check("端到端：仍包含指令与组合信息",
      "instruction" in payload and "portfolio" in payload)
check("端到端：持仓标的名出现在 prompt 里",
      "ETHUSDT" in prompt and "ZECUSDT" in prompt)

print()
print("=" * 68)
print(f"结果：{PASS} 通过 / {FAIL} 失败")
print("=" * 68)
sys.exit(1 if FAIL else 0)
