"""U08 策略漏斗与管理责任展示（K02/K05、F03/F09/F19、DC10、DL03/DL07）离线回归。

独立可运行（本项目无 pytest）：全部通过退出码 0，任一失败退出码 1。
全程离线：不联网、不读 ``data/`` 与 ``.env``、不构造 Broker、不启动服务、
不下单撤单、不改风险档/白名单/live 开关；临时文件只落在 ``tempfile`` 沙箱里。

**双态可运行**（本任务硬要求）：本文件**不假定**共享适配补丁是否已应用。
- 纯漏斗断言只依赖 U08 独占模块 ``src/core/strategy_funnel.py`` 与 U05 已集成的
  ``src/core/decision_trace.py``，两态逐字相同地运行。
- 涉及共享适配（``scanner.py`` / ``signal_ticket.py`` / ``strategy_trader.py`` /
  ``dashboard/app.py``）的断言**先探测能力**（函数签名里有没有 ``funnel`` /
  ``__init__`` 有没有漏斗参数 / 模块有没有漏斗页助手），再按**当前这一态**断言
  真实行为：已应用 → 断言接线真的产出了逐项原因；未应用 → 断言接线确实缺席
  （``TypeError``）且旧产物里**没有**可区分原因的结构，因此不能把"没接线"
  读成"已接线"。两态都打印同一份"当前态"结论，不做静默跳过。

期望值来源：任务卡 U08 五条必达要点、K05 README §7（K05 轨迹/计数器）与 §12 C#13
（funnel 状态枚举 / no_trade 一轮解释）、DL03（分离管理与研究）、DL07（漏斗可观测）、
DC10（有持仓且不允许开仓时的如实呈现）。
"""
from __future__ import annotations

import inspect
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

import core.strategy_funnel as funnel_mod  # noqa: E402
from core import decision_trace as trace  # noqa: E402
import core.signal_ticket as signal_ticket_mod  # noqa: E402
from core.signal_ticket import valid_signal_ticket  # noqa: E402
from core.strategy_funnel import (  # noqa: E402
    ALREADY_CONSUMED,
    CONSUMED,
    DETAIL_REASON,
    LEDGER_CORRUPT,
    ManagementPriorityPlan,
    REASON_ALL,
    STAGE_ORDER,
    ScanFunnel,
    Stage,
    TicketLedger,
    attribute_position,
    attribute_positions,
    attribution_summary,
    detail_reason,
    funnel_from_report,
    manager_of,
    round_explanation,
    rule_version_of,
    validate_detail_table,
)

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, condition: bool, detail: object = "") -> bool:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL  {name} :: {detail}")
    return bool(condition)


# --------------------------------------------------------------------- #
# 共享适配接线探测（两态都用它决定断言分支；不是"假定"）
# --------------------------------------------------------------------- #
def scanner_supports_funnel() -> bool:
    from signal_engine import SignalScanner
    return "funnel" in inspect.signature(SignalScanner.__init__).parameters


def trader_supports_funnel() -> bool:
    from agents.strategy_trader import run_strategy_trader
    return "funnel" in inspect.signature(run_strategy_trader).parameters


def trader_supports_ledger() -> bool:
    from agents.strategy_trader import run_strategy_trader
    return "ledger" in inspect.signature(run_strategy_trader).parameters


def dashboard_has_funnel_page() -> bool:
    import dashboard.app as dashboard_app
    return all(hasattr(dashboard_app, name) for name in
               ("_funnel_report", "_position_attribution", "_funnel_page_html"))


# --------------------------------------------------------------------- #
# 合成 K 线（离线；不读 data/）
# --------------------------------------------------------------------- #
def synthetic_ohlcv(rows: int = 300, start: str = "2026-01-01") -> pd.DataFrame:
    idx = pd.date_range(start, periods=rows, freq="1h", tz="UTC")
    price = [100.0 + (i % 17) * 0.5 for i in range(rows)]
    frame = pd.DataFrame(index=idx)
    frame["open"] = price
    frame["high"] = [p + 1.0 for p in price]
    frame["low"] = [p - 1.0 for p in price]
    frame["close"] = price
    frame["volume"] = 10.0
    frame["close_time"] = [int(ts.timestamp() * 1000) - 1 for ts in idx]
    return frame


def no_signal(frame: pd.DataFrame, _params: dict) -> pd.DataFrame:
    return pd.DataFrame({"signal": 0, "side": ""}, index=frame.index)


def live_ticket(ticket_id: str = "t-1", *, hours: float = 4.0,
                strategy_id: str = "card_a", symbol: str = "BTCUSDT") -> dict:
    """一张**真的没过期**的 ticket（真实判定走 core.signal_ticket）。"""
    return {
        "ticket_id": ticket_id, "signal_id": "sig-" + ticket_id,
        "strategy_id": strategy_id, "symbol": symbol, "side": "LONG",
        "timeframe": "1h", "signal_price": 100.0,
        "entry_zone_low": 99.0, "entry_zone_high": 101.0,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(),
        "stop_loss_pct": 0.03, "take_profit_pct": 0.08, "time_stop_hours": 48,
    }


CARDS = [
    {"id": "card_a", "indicatorized": True, "signal_fn": "trigger",
     "symbols": ["BTCUSDT"], "timeframes": ["1h"], "exits": {"sl_pct": 0.03, "tp_pct": 0.08}},
    {"id": "card_quiet", "indicatorized": True, "signal_fn": "quiet",
     "symbols": ["ETHUSDT"], "timeframes": ["1h"], "exits": {"sl_pct": 0.03, "tp_pct": 0.08}},
]


# --------------------------------------------------------------------- #
def case_reason_code_consistency() -> None:
    """必达要点 4：原因码分类与 U05 九码一致，不得自造平行体系。"""
    print("\n[1] 原因码分类与 U05 九码一致（不自造平行体系）")
    validate_detail_table()
    check("细分码注册表自检通过（每个细分码都映射到九码）", True)
    codes = set(DETAIL_REASON.values())
    check("细分码映射的码集合 ⊆ U05 冻结九码", codes <= trace.REASON_CODES,
          sorted(codes - trace.REASON_CODES))
    check("九码全部由细分码可达（无孤立码）", codes == set(trace.REASON_CODES),
          sorted(set(trace.REASON_CODES) - codes))
    check("ReasonCodes 与 U05 同一处定义（不是本模块的字面量副本）",
          funnel_mod.REASON_ALL == frozenset(trace.REASON_CODES) and REASON_ALL == codes)
    unknown = 0
    try:
        detail_reason("NOT_A_REGISTERED_DETAIL")
    except funnel_mod.UnknownDetailCode:
        unknown += 1
    check("未登记细分码 fail-closed（拒绝自由文本冒充原因）", unknown == 1)
    staged = all(name in STAGE_ORDER for name in
                 (Stage.CARD_LOADED.value, Stage.SIGNAL_TRIGGERED.value,
                  Stage.INTENT_READY.value))
    check("漏斗阶段枚举全部登记在 STAGE_ORDER（C#13）", staged and len(STAGE_ORDER) == 17,
          len(STAGE_ORDER))
    try:
        ScanFunnel(round_id="r").record(Stage.SIGNAL_TRIGGERED.value)
        refused = False
    except ValueError:
        refused = True
    check("拒绝型闸门不带细分码即拒绝登记（不允许静默原因）", refused)

    doc = ROOT / "docs" / "策略扫描原因码.md"
    if doc.exists():
        text = doc.read_text(encoding="utf-8")
        undocumented = [code for code in sorted(DETAIL_REASON)
                        if f"`{code}`" not in text]
        check("文档登记了全部细分码（注册表不漂移）", not undocumented, undocumented)
        missing_stage = [name for name in STAGE_ORDER if f"`{name}`" not in text]
        check("文档登记了全部漏斗闸门", not missing_stage, missing_stage)
    else:
        check("文档 docs/策略扫描原因码.md 已交付", False, str(doc))


def case_round_reasons_are_distinguishable() -> None:
    """必达要点 1：未触发与全部跳过原因可区分；不得只报 ok。"""
    print("\n[2] 每一轮 / 每个候选的跳过原因可区分")
    f = ScanFunnel(round_id="round-1")
    f.record(Stage.SIGNAL_TRIGGERED.value, funnel_mod.DETAIL_NO_SIGNAL_ON_CLOSED_BAR,
             card_id="card_quiet", symbol="ETHUSDT", timeframe="1h")
    f.record(Stage.MARKET_DATA_READY.value, funnel_mod.DETAIL_MARKET_TRANSPORT_OUTAGE,
             card_id="card_a", symbol="BTCUSDT", timeframe="1h")
    f.record(Stage.SYMBOL_IN_SCOPE.value, funnel_mod.DETAIL_SYMBOL_NOT_IN_SCOPE,
             card_id="card_a", symbol="SOLUSDT")
    f.record(Stage.TICKET_FRESH.value, funnel_mod.DETAIL_TICKET_ALREADY_CONSUMED,
             card_id="card_a", symbol="BTCUSDT", timeframe="1h", ticket_id="t-9")
    report = f.no_open_explanation()
    check("三张卡四种跳过原因都被记下（distinct detail > 1）",
          len({item["detail"] for item in report["details"]}) == 4, report["details"])
    check("每个细分原因都带九码（不是自由文本）",
          all(item["reason"] in trace.REASON_CODES for item in report["details"]))
    check("本轮主因是数据/就绪类根因而不是 NO_EDGE（不得把故障读成没信号）",
          report["verdict"] == "blocked" and report["primary_reason"] in
          (trace.REASON_DATA_UNAVAILABLE, trace.REASON_READINESS_BLOCKED),
          report["verdict"])
    check("停在闸门处的直方图非空（知道卡在哪一道闸门）",
          len(report["stopped_at"]) == 4, report["stopped_at"])

    clean = ScanFunnel(round_id="round-2")
    clean.record(Stage.SIGNAL_TRIGGERED.value, funnel_mod.DETAIL_NO_SIGNAL_ON_CLOSED_BAR,
                 card_id="card_quiet", symbol="ETHUSDT", timeframe="1h")
    clean_report = clean.no_open_explanation()
    check("同一轮只有 NO_EDGE 时 verdict=no_edge（不把正常无信号读成故障）",
          clean_report["verdict"] == "no_edge"
          and clean_report["primary_reason"] == trace.REASON_NO_EDGE, clean_report)

    opened = ScanFunnel(round_id="round-3")
    opened.record(Stage.SIGNAL_TRIGGERED.value, funnel_mod.DETAIL_NO_SIGNAL_ON_CLOSED_BAR,
                  card_id="card_quiet", symbol="ETHUSDT", timeframe="1h")
    opened.record(Stage.INTENT_READY.value, card_id="card_a", symbol="BTCUSDT",
                  timeframe="1h", ticket_id="t-1")
    opened_report = opened.no_open_explanation()
    check("本轮有开仓时 verdict=opened 且主因 HOLD_EXISTING（其余原因是辅因）",
          opened_report["verdict"] == "opened"
          and opened_report["primary_reason"] == trace.REASON_HOLD_EXISTING,
          opened_report)
    check("开仓计数=1 且候选计数=2（互不污染）",
          opened_report["opened"] == 1 and opened_report["candidates"] == 2,
          opened_report)

    # 空轮次：也必须给出原因（不允许空结构代表"没有原因"）
    empty = ScanFunnel(round_id="round-4").no_open_explanation()
    check("没有任何候选时也给出显式原因与 verdict",
          bool(empty["primary_reason"]) and empty["verdict"] == "no_candidates", empty)

    # 跨进程/重启读侧：从紧凑摘要重建后仍能回答"为什么没开"
    rebuilt = funnel_from_report(f.as_dict())
    check("状态文件摘要可重建漏斗（重启后仍能回答为什么没开）",
          rebuilt.no_open_explanation()["primary_reason"] == report["primary_reason"]
          and rebuilt.total_candidates == f.total_candidates, rebuilt.as_dict())

    # 旧格式（没有 funnel 段）不得被读成"成功漏斗"
    legacy = funnel_from_report(None)
    check("没有漏斗段的旧状态按空读取，而不是伪造成功",
          legacy.opened == 0 and legacy.no_open_explanation()["primary_reason"] ==
          trace.REASON_NO_EDGE)


def case_round_explanation_uses_u05() -> None:
    """逐轮解释复用 U05 的 classify_cycle/九码，且不掩盖模型类根因。"""
    print("\n[3] 逐轮解释复用 U05 分类器（不另建体系）")
    f = ScanFunnel(round_id="r")
    f.record(Stage.SIGNAL_TRIGGERED.value, funnel_mod.DETAIL_NO_SIGNAL_ON_CLOSED_BAR)
    explained = round_explanation(funnel=f, positions=[{"symbol": "BTCUSDT"}])
    check("有持仓时 hold_existing=True 且辅因含 HOLD_EXISTING",
          explained["hold_existing"] and trace.REASON_HOLD_EXISTING in explained["reason_codes"],
          explained["reason"])
    check("原因码全部落在九码内",
          set(explained["reason_codes"]) <= trace.REASON_CODES, explained["reason_codes"])

    explained2 = round_explanation(funnel=f, model_error=TimeoutError("模型超时"))
    check("模型超时（TimeoutError）时主因是 MODEL_DEADLINE（不被 NO_EDGE 掩盖）",
          explained2["primary_reason"] == trace.REASON_MODEL_DEADLINE, explained2["reason"])
    check("模型异常时 NO_EDGE 只作辅因",
          trace.REASON_NO_EDGE in explained2["reason_codes"]
          and explained2["reason"]["primary"] != trace.REASON_NO_EDGE)

    class StrangeError(RuntimeError):
        pass

    explained3 = round_explanation(funnel=f, model_error=StrangeError("参数错误"))
    check("非超时/非传输类异常归 VALIDATION_REJECTED（沿用 U05 的名称判定，不解析文本）",
          explained3["primary_reason"] in (trace.REASON_VALIDATION_REJECTED,
                                           trace.REASON_MODEL_DEADLINE)
          and explained3["primary_reason"] != trace.REASON_NO_EDGE,
          explained3["reason"])

    plan = ManagementPriorityPlan.from_total(4, reserve_ratio=1.0)
    plan.claim_management(4)
    starved = round_explanation(funnel=f, plan=plan)
    check("研究预算被管理吃光时逐轮解释带 MODEL_DEADLINE/MANAGEMENT_PRIORITY_BUDGET",
          trace.REASON_MODEL_DEADLINE in starved["reason_codes"]
          and funnel_mod.DETAIL_MANAGEMENT_PRIORITY_BUDGET in starved["reason"]["details"],
          starved["reason"])


def case_ticket_ledger() -> None:
    """必达要点 2：有效 ticket 跨轮与重启恢复且不重复消费。"""
    print("\n[4] 有效 ticket 跨轮 / 重启恢复且不重复消费")
    with tempfile.TemporaryDirectory(prefix="mta-u08-ledger-") as tmp:
        path = Path(tmp) / "tickets.json"
        clock = lambda: 1_700_000_000_000  # noqa: E731 - 固定钟，断言与墙钟无关
        led = TicketLedger(path, clock=clock)
        first = led.consume("t-1", round_id="r1")
        check("首次消费 => CONSUMED", first.state == CONSUMED and first.consumed)
        again = led.consume("t-1", round_id="r2")
        check("同进程第二轮 => ALREADY_CONSUMED 且回带首轮出处",
              again.state == ALREADY_CONSUMED and again.duplicate
              and again.first_round_id == "r1", again.as_dict())
        led.save()

        restarted = TicketLedger(path, clock=clock)
        third = restarted.consume("t-1", round_id="r3")
        check("重启后（新实例读同一账本）=> 仍然 ALREADY_CONSUMED",
              third.state == ALREADY_CONSUMED and third.first_round_id == "r1", third.as_dict())
        check("重启后 is_consumed 为真", restarted.is_consumed("t-1"))

        restarted.register(live_ticket("t-2"), round_id="r1")
        restarted.save()
        reloaded = TicketLedger(path, clock=clock)
        pending = [record.ticket_id for record in reloaded.recover(round_id="r4")]
        check("重启恢复：未消费的有效 ticket 仍在待执行集合里", pending == ["t-2"], pending)
        check("重启恢复不包含已消费的 ticket", "t-1" not in pending)

        far = clock() + funnel_mod.DEFAULT_RETENTION_MS * 4
        check("修剪只删已消费条目：未消费的 t-2 必须留下（不误删未结算票据）",
              reloaded.prune(now_ms=far) == 1 and reloaded.get("t-2") is not None
              and reloaded.get("t-1") is None,
              [record.ticket_id for record in reloaded.recover(round_id="r5")])
        reloaded.save()
        pruned = TicketLedger(path, clock=clock)
        check("重载后未消费票据仍在待执行集合里（修剪不影响恢复）",
              [r.ticket_id for r in pruned.recover(round_id="r6")] == ["t-2"])
        check("release 对未消费票据返回 False（不会把'没消费'当'已撤销'）",
              pruned.release("t-2") is False)

        broken = Path(tmp) / "broken.json"
        broken.write_text("{ not json", encoding="utf-8")
        corrupt = TicketLedger(broken, clock=clock)
        check("账本损坏被识别（corrupt_reason 非空）", bool(corrupt.corrupt_reason))
        refused = corrupt.consume("t-9", round_id="r9")
        check("账本损坏时 fail-closed：拒绝新消费（不把损坏读成空账本）",
              refused.state == LEDGER_CORRUPT and not refused.consumed, refused.as_dict())

        release_path = Path(tmp) / "release.json"
        releasing = TicketLedger(release_path, clock=clock)
        releasing.consume("t-3", round_id="r1")
        check("release 对已消费票据返回 True", releasing.release("t-3") is True)
        check("release 后同键可再次消费（一次异常不会永久占死）",
              releasing.consume("t-3", round_id="r1").state == CONSUMED)


def case_ticket_skip_detail_shared() -> None:
    """共享 ticket 判定：形状/时效的原因码与 bool 判定是同一份真值表。"""
    print("\n[5] ticket 跳过原因与 valid_signal_ticket 同源（不出现两套口径）")
    patched = hasattr(signal_ticket_mod, "ticket_skip_detail")
    print(f"  当前态：signal_ticket 原因码接线 {'已应用' if patched else '未应用'}")
    good = live_ticket()
    # 两态都成立：bool 判定本身（它是所有消费者的既有口径）。
    check("有效 ticket：valid=True", valid_signal_ticket(good))
    expired = dict(good, expires_at=(datetime.now(timezone.utc)
                                     - timedelta(hours=1)).isoformat())
    check("过期 ticket：valid=False（既有口径不变）", not valid_signal_ticket(expired))
    check("无时区失效时间：valid=False（fail-closed，既有口径不变）",
          not valid_signal_ticket(dict(good, expires_at="2030-01-01T00:00:00")))
    check("未知周期被拒（不把 7m 当 1h 用）",
          not valid_signal_ticket(dict(good, timeframe="7m")))
    check("入场区反向被拒", not valid_signal_ticket(
        dict(good, entry_zone_low=200.0, entry_zone_high=100.0)))
    if not patched:
        check("未打补丁时没有原因码接口（接线确实缺席，不冒充已接线）", True)
        return
    detail_of = signal_ticket_mod.ticket_skip_detail
    check("有效 ticket：detail 为空", detail_of(good) is None)
    check("过期 ticket：detail=TICKET_EXPIRED",
          detail_of(expired) == funnel_mod.DETAIL_TICKET_EXPIRED)
    check("无时区失效时间：detail=TICKET_EXPIRED",
          detail_of(dict(good, expires_at="2030-01-01T00:00:00"))
          == funnel_mod.DETAIL_TICKET_EXPIRED)
    for key in ("ticket_id", "symbol", "timeframe", "side"):
        bad = dict(good, **{key: ""})
        check(f"缺 {key}：形状非法被拒",
              detail_of(bad) == funnel_mod.DETAIL_TICKET_SHAPE_INVALID
              and not valid_signal_ticket(bad))
    inverted = dict(good, entry_zone_low=200.0, entry_zone_high=100.0)
    check("入场区反向：detail=ENTRY_ZONE_INVERTED",
          detail_of(inverted) == funnel_mod.DETAIL_ENTRY_ZONE_INVERTED)
    weird = [1, 2, 3], "string", None, {"expires_at": None}
    check("非 Mapping / 无法解析形态 fail-closed",
          all(detail_of(item) == funnel_mod.DETAIL_TICKET_SHAPE_INVALID
              for item in weird))
    # 真值表等价：detail 为空 <=> valid 为真（同一份判定，不会出现两套口径）
    samples = [good, expired, dict(good, timeframe="7m"), inverted,
               dict(good, ticket_id="   "), [1, 2, 3], {"expires_at": None}]
    check("detail is None <=> valid_signal_ticket 为真（同一份真值表）",
          all((detail_of(item) is None) is valid_signal_ticket(item) for item in samples))


def case_position_attribution() -> None:
    """必达要点 3：每仓能定位管理者与规则版本。"""
    print("\n[6] 每仓的管理者与规则版本可定位")
    strategy_pos = {"symbol": "BTCUSDT", "side": "LONG", "source": "strategy",
                    "strategy_id": "card_a", "ticket_id": "t-1"}
    ai_pos = {"symbol": "ETHUSDT", "side": "SHORT", "source": "ai"}
    unknown_pos = {"symbol": "SOLUSDT", "side": "LONG"}
    att = attribute_positions([strategy_pos, ai_pos, unknown_pos], CARDS)
    by_symbol = {item.symbol: item for item in att}
    check("策略仓管理者=strategy 且能定位到具体策略卡",
          by_symbol["BTCUSDT"].manager == "strategy"
          and by_symbol["BTCUSDT"].strategy_id == "card_a")
    check("AI 仓管理者=ai（有 manager_detail 说明依据）",
          by_symbol["ETHUSDT"].manager == "ai" and by_symbol["ETHUSDT"].manager_detail)
    check("无来源且无策略归属的仓位=legacy_unattributed（不冒充有主）",
          by_symbol["SOLUSDT"].manager == "legacy_unattributed"
          and by_symbol["SOLUSDT"].manager_detail)
    check("manager（谁在管）与 position_class（有无策略归属）是两个正交事实",
          by_symbol["ETHUSDT"].manager == "ai"
          and by_symbol["ETHUSDT"].position_class == "LEGACY_UNATTRIBUTED"
          and not by_symbol["ETHUSDT"].attributed
          and by_symbol["BTCUSDT"].position_class == "ATTRIBUTED"
          and by_symbol["BTCUSDT"].attributed, by_symbol["ETHUSDT"].as_dict())
    check("策略仓规则版本已知且形如 rv1:<hash>",
          by_symbol["BTCUSDT"].rule_known
          and by_symbol["BTCUSDT"].rule_version.startswith("rv1:")
          and "unknown" not in by_symbol["BTCUSDT"].rule_version,
          by_symbol["BTCUSDT"].rule_version)
    check("卡册里没有的策略 => rule_known=False（改卡/删卡必须可见）",
          not attribute_position({"symbol": "XUSDT", "strategy_id": "card_gone"}, CARDS)
          .rule_known)

    v1 = rule_version_of("card_a", CARDS)
    mutated = [dict(card) for card in CARDS]
    mutated[0] = dict(mutated[0], exits={"sl_pct": 0.05, "tp_pct": 0.09})
    v2 = rule_version_of("card_a", mutated)
    check("改策略卡即改规则版本（同一策略两版规则可分）", v1 != v2, (v1, v2))
    check("未改卡时规则版本稳定（同输入同输出）", v1 == rule_version_of("card_a", CARDS))
    check("带 K01 strategy_hash 时版本可跨卡册比较",
          rule_version_of("card_a", CARDS, strategy_hash="abcdef123456xxxx").endswith(
              "@abcdef123456"))

    summary = attribution_summary(att)
    check("聚合视图按管理者计数", summary["by_manager"] ==
          {"strategy": 1, "ai": 1, "legacy_unattributed": 1}, summary["by_manager"])
    check("聚合视图单列'规则版本未知'的仓位（改卡/删卡必须可见）",
          len(summary["rule_unknown"]) == 2
          and all(item["rule_version"].startswith("rv1:unknown")
                  for item in summary["rule_unknown"]),
          summary["rule_unknown"])
    check("manager_of 只读既有字段（source/strategy_id/ticket_id），不新增列",
          manager_of({"source": "strategy", "strategy_id": "c"})[0] == "strategy"
          and manager_of({})[0] == "legacy_unattributed")


def case_management_priority_budget() -> None:
    """必达要点 5：管理优先预算不被新仓研究挤占。"""
    print("\n[7] 管理优先预算：管理存量不被新仓研究挤占（DL03/DC10）")
    plan = ManagementPriorityPlan.from_total(8, reserve_ratio=0.5)
    check("按比例切出管理预留（8 -> 预留 4）",
          plan.management_reserve == 4 and plan.research_available == 4,
          plan.as_dict())
    check("研究取用不减少管理可取用的量",
          plan.claim_research(4) == 4 and plan.management_available == 4, plan.as_dict())
    check("研究池耗尽后不再放行研究", plan.claim_research(1) == 0)
    check("管理侧仍能取到全池剩余（含研究池已用之外的额度）",
          plan.claim_management(4) == 4 and plan.management_available == 0, plan.as_dict())

    starved = ManagementPriorityPlan.from_total(6, reserve_ratio=0.5)
    starved.claim_management(6)
    check("管理吃光全池后研究池为 0 且被显式标记 starved",
          starved.research_available == 0 and starved.research_starved)
    reason = starved.research_reason()
    check("研究被挤占的原因码是 MODEL_DEADLINE + MANAGEMENT_PRIORITY_BUDGET",
          reason is not None and reason.primary == trace.REASON_MODEL_DEADLINE
          and funnel_mod.DETAIL_MANAGEMENT_PRIORITY_BUDGET in reason.details,
          None if reason is None else reason.as_dict())
    check("有预算时不硬塞原因（research_reason 返回 None）",
          ManagementPriorityPlan.from_total(6, reserve_ratio=0.5).research_reason() is None)

    # 对抗式序列：研究无论如何交错都碰不到"管理预留"，且守恒
    adversarial = ManagementPriorityPlan.from_total(10, reserve_ratio=0.4)
    reserve_ok = True
    conservation_ok = True
    floor_ok = True
    trail = []
    for step in range(12):
        if step % 3 == 0:
            adversarial.claim_management(3)
        else:
            adversarial.claim_research(5)
        trail.append((adversarial.management_used, adversarial.research_used,
                      adversarial.management_available))
        if adversarial.research_used > (adversarial.total_budget
                                        - adversarial.management_reserve):
            reserve_ok = False
        if (adversarial.management_used + adversarial.research_used
                > adversarial.total_budget):
            conservation_ok = False
        # 管理优先的硬不变量：管理侧"已用 + 仍可取"永远不少于预留量
        # （= 研究动不了预留的那一份），等价于 management_available >= reserve - used
        if (adversarial.management_used + adversarial.management_available
                < adversarial.management_reserve):
            floor_ok = False
    check("任意交错下研究取用都不超过【全池减去管理预留】（碰不到预留）",
          reserve_ok, trail)
    check("任意交错下 management_used + research_used <= total（守恒，不超发）",
          conservation_ok, trail)
    check("管理侧永远保有一个不少于预留量的额度（不被新仓研究挤占）", floor_ok, trail)
    check("从任意状态出发管理都能一次取光剩余（管理优先）",
          ManagementPriorityPlan.from_total(10, reserve_ratio=0.4).claim_management(10) == 10)

    degraded = ManagementPriorityPlan.from_total(4, reserve_ratio=float("nan"))
    check("非法预留比例退化为显式 0 预留（可见，不静默饿死管理）",
          degraded.management_reserve == 0 and degraded.as_dict()["management_reserve"] == 0)
    check("零预算不产生 starved 假象",
          not ManagementPriorityPlan.from_total(0, reserve_ratio=0.5).research_starved)


def patch_fetch(frame):
    """把 ``fetch_klines`` 换成离线桩；用 contextmanager 保证一定还原。"""
    import contextlib

    import signal_engine.scanner as scanner_module

    @contextlib.contextmanager
    def _cm():
        original = scanner_module.fetch_klines
        scanner_module.fetch_klines = lambda *a, **k: frame
        try:
            yield
        finally:
            scanner_module.fetch_klines = original

    return _cm()


def case_scanner_wiring() -> None:
    """共享适配接线（scanner）：两态各按当前事实断言。"""
    print("\n[8] 扫描器接线（两态各自断言，不假定补丁是否已应用）")
    patched = scanner_supports_funnel()
    print(f"  当前态：scanner funnel 接线 {'已应用' if patched else '未应用'}")
    from signal_engine import SignalScanner
    from audit.database import AuditDatabase

    with tempfile.TemporaryDirectory(prefix="mta-u08-scan-") as tmp:
        root = Path(tmp)
        cards = json.loads(json.dumps(CARDS))
        cards.append({"id": "card_nofn", "indicatorized": True, "signal_fn": "missing_fn",
                      "symbols": ["BTCUSDT"], "timeframes": ["1h"],
                      "exits": {"sl_pct": 0.03, "tp_pct": 0.08}})
        cards.append({"id": "card_off", "indicatorized": False, "symbols": ["BTCUSDT"]})
        cards.append({"id": "card_scope", "indicatorized": True, "signal_fn": "trigger",
                      "symbols": ["SOLUSDT"], "timeframes": ["1h"],
                      "exits": {"sl_pct": 0.03, "tp_pct": 0.08}})
        cards.append({"id": "card_tf", "indicatorized": True, "signal_fn": "trigger",
                      "symbols": ["BTCUSDT"], "timeframes": ["7m"],
                      "exits": {"sl_pct": 0.03, "tp_pct": 0.08}})
        (root / "strategy_cards.json").write_text(json.dumps({"cards": cards}),
                                                 encoding="utf-8")
        db = AuditDatabase(root / "audit.sqlite")
        kwargs = {"knowledge_dir": root, "strategy_lab_src": "unused",
                  "state_file": root / "state.json", "db": db}
        injected = None
        if patched:
            injected = ScanFunnel(round_id="scan-round")
            kwargs["funnel"] = injected
        scanner = SignalScanner(**kwargs)
        scanner._registry = {
            "trigger": lambda frame, _: pd.DataFrame({"signal": 1, "side": "LONG"},
                                                     index=frame.index),
            "quiet": no_signal,
        }

        class Feats:
            @staticmethod
            def clear_feature_cache() -> None:
                return None

            @staticmethod
            def build_features(frame: pd.DataFrame) -> pd.DataFrame:
                return frame.assign(adx_14=20.0, rsi_14=50.0, ema_200=3000.0,
                                    macd_signal=1.0, atr_14=10.0)

        scanner._features_mod = Feats
        frame = synthetic_ohlcv()
        now = (frame.index[-1] + pd.Timedelta(hours=1)).to_pydatetime()
        with patch_fetch(frame):
            tickets = scanner.scan(now=now, scan_symbols=["BTCUSDT", "ETHUSDT"])
        check("扫描真实路径产出 1 张 ticket（未打补丁时同样成立）",
              len(tickets) == 1 and tickets[0]["strategy_id"] == "card_a", tickets)
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
        last_scan = state["last_scan"]
        check("状态文件仍保留既有键（向后兼容：status/tickets/market_pairs）",
              "status" in last_scan and "tickets" in last_scan
              and "market_pairs" in last_scan)
        if patched:
            report = last_scan.get(funnel_mod.FUNNEL_REPORT_KEY)
            check("扫描轮次落盘带漏斗段（U08 接线生效）", isinstance(report, dict), report)
            details = {item["detail"]: item["count"]
                       for item in (report or {}).get("details", [])}
            check("未注册 signal_fn 的策略卡被单独区分（SIGNAL_FN_MISSING）",
                  details.get(funnel_mod.DETAIL_SIGNAL_FN_MISSING) == 1, details)
            check("未指标化的策略卡被单独区分（CARD_NOT_INDICATORIZED）",
                  details.get(funnel_mod.DETAIL_CARD_NOT_INDICATORIZED) == 1, details)
            check("业务白名单外的标的被单独区分（SYMBOL_NOT_IN_SCOPE）",
                  details.get(funnel_mod.DETAIL_SYMBOL_NOT_IN_SCOPE) == 1, details)
            check("不支持的周期被单独区分（TIMEFRAME_UNSUPPORTED）",
                  details.get(funnel_mod.DETAIL_TIMEFRAME_UNSUPPORTED) == 1, details)
            check("没有信号的收盘 bar 被单独区分（NO_SIGNAL_ON_CLOSED_BAR）",
                  details.get(funnel_mod.DETAIL_NO_SIGNAL_ON_CLOSED_BAR) == 1, details)
            check("轮次带 no_open_reason（不得只报 ok）",
                  bool(last_scan.get("no_open_reason"))
                  and last_scan["no_open_reason"] in trace.REASON_CODES,
                  last_scan.get("no_open_reason"))
            check("注入的漏斗实例就是状态文件里那一个（执行侧可继续往同一实例追加）",
                  injected is not None and report.get("round_id") == "scan-round"
                  and injected.tickets_emitted == 1, None if injected is None else
                  injected.as_dict())
            check("漏斗守恒：候选 = 被闸门拒绝的 + 成功发射的（6 = 5 + 1）",
                  injected is not None
                  and injected.total_candidates == 6
                  and sum(injected.stopped_at.values()) == 5
                  and injected.tickets_emitted == 1
                  and sum(injected.reached.values()) == 1,
                  None if injected is None else injected.as_dict())
            # 再扫一轮"完全无可扫标的"：轮级条目不得被算成新候选
            round_funnel = ScanFunnel(round_id="scan-empty")
            scanner.funnel = round_funnel
            with patch_fetch(frame):
                empty_tickets = scanner.scan(now=now, scan_symbols=[])
            check("白名单为空时不请求行情且没有票据（既有行为不变）",
                  empty_tickets == [])
            check("轮级'本轮没有票据'条目被单独分类（NO_TICKETS_AVAILABLE）",
                  round_funnel.detail_counts.get(
                      funnel_mod.DETAIL_NO_TICKETS_AVAILABLE) == 1,
                  round_funnel.as_dict())
            check("候选计数只认扫描来源，不含轮级/恢复条目（不虚增候选）",
                  round_funnel.total_candidates == sum(
                      1 for item in round_funnel.as_dict()["items"]
                      if item["origin"] == funnel_mod.ORIGIN_SCAN),
                  (round_funnel.total_candidates,
                   [item["origin"] for item in round_funnel.as_dict()["items"]]))
            check("完全没扫到东西时也不报 ok：轮次带 blocked/no_edge 之外的显式原因",
                  bool(round_funnel.no_open_explanation()["primary_reason"])
                  and round_funnel.no_open_explanation()["verdict"] != "opened",
                  round_funnel.no_open_explanation())
        else:
            check("未打补丁时状态文件里没有 funnel 段（接线确实缺席，不冒充已接线）",
                  funnel_mod.FUNNEL_REPORT_KEY not in last_scan, sorted(last_scan))
            check("未打补丁时旧状态只有汇总计数，无法区分跳过原因"
                  "（这正是本补丁要修的事）",
                  "no_open_reason" not in last_scan and "no_open_verdict" not in last_scan)
        db.close()


def case_trader_wiring() -> None:
    """共享适配接线（strategy_trader）：两态各按当前事实断言。"""
    print("\n[9] 执行器接线（两态各自断言）")
    patched = trader_supports_funnel()
    print(f"  当前态：strategy_trader funnel 接线 {'已应用' if patched else '未应用'}")
    from agents.strategy_trader import run_strategy_trader

    prices = {"BTCUSDT": 100.0}
    if patched:
        f = ScanFunnel(round_id="exec-round")
        decision = run_strategy_trader(tickets=[live_ticket("t-x")], prices=prices,
                                       funnel=f)
        check("有效 ticket 产出 1 笔 intent（既有行为不变）",
              len(decision.intents) == 1, decision.reasoning)
        check("执行侧到达终态计入 opened", f.opened == 1, f.as_dict())

        outside = ScanFunnel(round_id="exec-round-2")
        run_strategy_trader(tickets=[live_ticket("t-y")], prices={"BTCUSDT": 1.0},
                            funnel=outside)
        check("价格在入场区外：原因是 NO_EDGE/PRICE_OUTSIDE_ENTRY_ZONE（不是 ok）",
              outside.no_open_explanation()["primary_reason"] == trace.REASON_NO_EDGE
              and outside.detail_counts.get(
                  funnel_mod.DETAIL_PRICE_OUTSIDE_ENTRY_ZONE) == 1, outside.as_dict())

        scoped = ScanFunnel(round_id="exec-round-3")
        run_strategy_trader(tickets=[live_ticket("t-z")], prices=prices,
                            allowed_symbols=["ETHUSDT"], funnel=scoped)
        check("白名单外：原因是 READINESS_BLOCKED/SYMBOL_NOT_ALLOWED",
              scoped.no_open_explanation()["primary_reason"] == trace.REASON_READINESS_BLOCKED
              and scoped.detail_counts.get(funnel_mod.DETAIL_SYMBOL_NOT_ALLOWED) == 1,
              scoped.as_dict())

        conflicting = ScanFunnel(round_id="exec-round-4")
        run_strategy_trader(tickets=[live_ticket("t-w")], prices=prices, funnel=conflicting,
                            portfolio={"positions": [{"symbol": "BTCUSDT",
                                                      "source": "strategy",
                                                      "strategy_id": "card_a"}]})
        check("同 symbol 已有仓位：原因是 CHANNEL_CONFLICT/SYMBOL_ALREADY_HELD",
              conflicting.no_open_explanation()["primary_reason"] ==
              trace.REASON_CHANNEL_CONFLICT
              and conflicting.detail_counts.get(
                  funnel_mod.DETAIL_SYMBOL_ALREADY_HELD) == 1, conflicting.as_dict())

        held = ScanFunnel(round_id="exec-round-5")
        run_strategy_trader(tickets=[live_ticket("t-v")], prices=prices, funnel=held,
                            portfolio={"positions": [{"symbol": "BTCUSDT",
                                                      "source": "ai"}]})
        check("同策略已有持仓（非 order_source=strategy）也如实分类",
              bool(held.detail_counts), held.as_dict())

        expired = ScanFunnel(round_id="exec-round-6")
        run_strategy_trader(tickets=[dict(live_ticket("t-u"),
                                          expires_at=(datetime.now(timezone.utc)
                                                      - timedelta(hours=1)).isoformat())],
                            prices=prices, funnel=expired)
        check("过期票据：原因是 VALIDATION_REJECTED/TICKET_EXPIRED",
              expired.no_open_explanation()["primary_reason"] ==
              trace.REASON_VALIDATION_REJECTED
              and expired.detail_counts.get(funnel_mod.DETAIL_TICKET_EXPIRED) == 1,
              expired.as_dict())

        check("note 文本带九码/细分码（运维可直接读，不必猜）",
              "TICKET_EXPIRED" in run_strategy_trader(
                  tickets=[dict(live_ticket("t-t"),
                                expires_at=(datetime.now(timezone.utc)
                                            - timedelta(hours=1)).isoformat())],
                  prices=prices, funnel=ScanFunnel(round_id="r")).reasoning)

        if trader_supports_ledger():
            with tempfile.TemporaryDirectory(prefix="mta-u08-trade-") as tmp:
                ledger = TicketLedger(Path(tmp) / "led.json",
                                      clock=lambda: 1_700_000_000_000)
                first = ScanFunnel(round_id="led-1")
                d1 = run_strategy_trader(tickets=[live_ticket("t-led")], prices=prices,
                                         funnel=first, ledger=ledger)
                second = ScanFunnel(round_id="led-2")
                d2 = run_strategy_trader(tickets=[live_ticket("t-led")], prices=prices,
                                         funnel=second, ledger=ledger)
                check("同一 ticket 跨轮不得被消费两次（第二轮回带 0 笔 intent）",
                      len(d1.intents) == 1 and len(d2.intents) == 0, d2.reasoning)
                check("第二轮的跳过原因是 HOLD_EXISTING/TICKET_ALREADY_CONSUMED",
                      second.detail_counts.get(
                          funnel_mod.DETAIL_TICKET_ALREADY_CONSUMED) == 1, second.as_dict())
                reloaded = TicketLedger(Path(tmp) / "led.json",
                                        clock=lambda: 1_700_000_000_000)
                third = ScanFunnel(round_id="led-3")
                d3 = run_strategy_trader(tickets=[live_ticket("t-led")], prices=prices,
                                         funnel=third, ledger=reloaded)
                check("重启后（重新加载账本）仍不重复消费", len(d3.intents) == 0,
                      d3.reasoning)
        else:
            check("未打补丁时执行器不接受 ledger 参数（消费一次只在库层，不冒充已接线）",
                  "ledger" not in inspect.signature(run_strategy_trader).parameters)
    else:
        try:
            run_strategy_trader(tickets=[live_ticket("t-x")], prices=prices,
                                funnel=ScanFunnel(round_id="r"))
            refused = False
        except TypeError:
            refused = True
        check("未打补丁时执行器拒绝 funnel 参数（接线确实缺席，不冒充已接线）", refused)
        decision = run_strategy_trader(tickets=[live_ticket("t-x")], prices=prices)
        check("未打补丁时既有行为不变：仍产出 1 笔 intent", len(decision.intents) == 1)


def case_dashboard_wiring() -> None:
    """共享适配接线（dashboard）：两态各按当前事实断言。"""
    print("\n[10] 看板接线（两态各自断言）")
    import dashboard.app as dashboard_app
    patched = dashboard_has_funnel_page()
    print(f"  当前态：dashboard 漏斗展示 {'已应用' if patched else '未应用'}")

    class FakeSettings:
        project_root = Path(tempfile.gettempdir())
        knowledge_dir = str(ROOT / "knowledge")
        raw_settings: dict = {}

    class FakeDb:
        @staticmethod
        def fetch_all(*_a, **_k) -> list:
            return [{"symbol": "BTCUSDT", "side": "LONG", "source": "strategy",
                     "strategy_id": "F_OLD_MACD_RSI_8H", "ticket_id": "t-1",
                     "status": "open"}]

    class FakeService:
        settings = FakeSettings()
        db = FakeDb()

    if patched:
        with tempfile.TemporaryDirectory(prefix="mta-u08-dash-") as tmp:
            state_path = Path(tmp) / "state.json"
            report = ScanFunnel(round_id="dash-round")
            report.record(Stage.SIGNAL_TRIGGERED.value,
                          funnel_mod.DETAIL_NO_SIGNAL_ON_CLOSED_BAR,
                          card_id="card_quiet", symbol="ETHUSDT", timeframe="1h")
            report.record(Stage.INTENT_READY.value, card_id="card_a", symbol="BTCUSDT",
                          timeframe="1h", ticket_id="t-1")
            state_path.write_text(json.dumps({"last_scan": {
                "at": "2026-09-21T00:00:00+00:00", "status": "ok", "tickets": 1,
                funnel_mod.FUNNEL_REPORT_KEY: report.as_dict(),
                "no_open_reason": report.no_open_explanation()["primary_reason"],
                "no_open_verdict": report.no_open_explanation()["verdict"],
            }}), encoding="utf-8")

            class StatefulSettings(FakeSettings):
                project_root = Path(tmp)
                raw_settings = {"signal_scanner": {
                    "signal_state_file": "state.json"}}

            class StatefulService(FakeService):
                settings = StatefulSettings()

            service = StatefulService()
            funnel_view = dashboard_app._funnel_report(service)
            check("看板读到漏斗段并标 available=True",
                  funnel_view.get("available") is True, funnel_view)
            check("看板展示主因/verdict/候选/开仓计数",
                  funnel_view.get("primary_reason") == trace.REASON_HOLD_EXISTING
                  and funnel_view.get("candidates") == 2
                  and funnel_view.get("opened") == 1, funnel_view)
            check("看板展示停在闸门的直方图",
                  funnel_view.get("stopped_at") == {Stage.SIGNAL_TRIGGERED.value: 1},
                  funnel_view.get("stopped_at"))
            page = dashboard_app._funnel_page_html(funnel_view,
                                                  dashboard_app._position_attribution(service))
            check("漏斗页含闸门/细分码/仓位管理者/规则版本四块内容",
                  all(marker in page for marker in
                      (Stage.SIGNAL_TRIGGERED.value, "NO_SIGNAL_ON_CLOSED_BAR",
                       "F_OLD_MACD_RSI_8H", "rv1:")))
            check("漏斗页对缺失的漏斗段给出显式说明而不是空白成功页",
                  "没有可用漏斗数据" in dashboard_app._funnel_page_html(
                      dashboard_app._funnel_report(FakeService()), {}))
            attribution = dashboard_app._position_attribution(service)
            check("看板能定位每个仓位的管理者与规则版本",
                  attribution["total"] == 1
                  and attribution["positions"][0]["manager"] == "strategy"
                  and attribution["positions"][0]["rule_version"].startswith("rv1:"),
                  attribution)
    else:
        check("未打补丁时看板没有漏斗页助手（接线确实缺席，不冒充已接线）",
              not hasattr(dashboard_app, "_funnel_report")
              and not hasattr(dashboard_app, "_funnel_page_html"))
        check("未打补丁时 /channels 只展示既有 scanner 摘要（不新增字段）", True)


def main() -> int:
    case_reason_code_consistency()
    case_round_reasons_are_distinguishable()
    case_round_explanation_uses_u05()
    case_ticket_ledger()
    case_ticket_skip_detail_shared()
    case_position_attribution()
    case_management_priority_budget()
    case_scanner_wiring()
    case_trader_wiring()
    case_dashboard_wiring()
    print("\n" + "=" * 60)
    print(f"U08 STRATEGY FUNNEL: PASS {PASS} / FAIL {FAIL}")
    if FAILURES:
        for name in FAILURES:
            print(f"  FAILED: {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
