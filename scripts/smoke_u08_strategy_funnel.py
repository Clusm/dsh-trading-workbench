"""U08 策略漏斗冒烟：真实扫描轮次 + 执行侧 + 账本 + 看板展示的离线端到端。

独立可运行（本项目无 pytest）：全部通过退出码 0，任一失败退出码 1。
全程离线：**行情层用模块内桩替换**（``signal_engine.scanner.fetch_klines``），
不联网、不读 ``.env``/真实 ``data/``、不构造 Broker、不启停服务、不下单撤单、
不改风险档/白名单/live 开关；所有临时文件只落在 ``tempfile`` 沙箱内。

**双态可运行**：共享适配（``scanner.py`` / ``signal_ticket.py`` /
``strategy_trader.py`` / ``dashboard/app.py``）未应用时，本脚本退化为
"独占模块 + 既有行为兼容"断言（仍 exit 0）；已应用时追加逐项漏斗的端到端断言。
是否已应用由**运行时能力**探测（函数签名 / 模块属性）决定，不读共享文件内容。

覆盖的必达要点：
1. 扫描未触发及全部跳过原因可区分（不得只报 ok）；
2. 有效 ticket 跨轮与重启恢复且不重复消费；
3. 每仓能定位管理者与规则版本；
4. 原因码分类与 U05 九码一致（不自造平行体系）；
5. 管理优先预算不被新仓研究挤占。

运行：
    PYTHONUTF8=1 <repo>/.venv/Scripts/python.exe scripts/smoke_u08_strategy_funnel.py

SCRIPTS 登记行（由 I 加入 ``scripts/run_offline_regression.py`` 的 ``SCRIPTS`` 元组）：
    "scripts/smoke_u08_strategy_funnel.py",
"""
from __future__ import annotations

import inspect
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import pandas as pd  # noqa: E402

import core.strategy_funnel as funnel_mod  # noqa: E402
from agents.strategy_trader import run_strategy_trader  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402
from core import decision_trace as trace  # noqa: E402
from core.strategy_funnel import (  # noqa: E402
    DETAIL_NATURE,
    DETAIL_REASON,
    DETAIL_STAGES,
    NATURE_FAULT,
    NATURE_SCOPE,
    ROUND_ONLY_DETAILS,
    ManagementPriorityPlan,
    ScanFunnel,
    Stage,
    TicketLedger,
    attribution_summary,
    attribute_positions,
    detail_codes_for_reason,
    funnel_from_report,
    rule_version_of,
    validate_detail_table,
)
from signal_engine import SignalScanner  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []


def check(name: str, cond: bool, extra: object = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL  {name}  {extra}")


def note(text: str) -> None:
    print(f"  NOTE  {text}")


def run_case(title: str, fn) -> None:
    print(f"\n{title}")
    try:
        fn()
    except Exception as exc:  # noqa: BLE001 - 冒烟必须把异常变成失败而不是崩溃
        import traceback
        traceback.print_exc()
        check(f"{title} 未抛异常", False, f"{type(exc).__name__}: {exc}")


# --------------------------------------------------------------------- #
def synthetic_ohlcv(rows: int = 300) -> pd.DataFrame:
    idx = pd.date_range("2026-01-01", periods=rows, freq="1h", tz="UTC")
    price = [100.0 + (i % 17) * 0.5 for i in range(rows)]
    frame = pd.DataFrame(index=idx)
    frame["open"] = price
    frame["high"] = [p + 1.0 for p in price]
    frame["low"] = [p - 1.0 for p in price]
    frame["close"] = price
    frame["volume"] = 10.0
    frame["close_time"] = [int(ts.timestamp() * 1000) - 1 for ts in idx]
    return frame


def live_ticket(ticket_id: str = "t-1", *, hours: float = 4.0,
                strategy_id: str = "card_a") -> dict:
    return {
        "ticket_id": ticket_id, "signal_id": "sig-" + ticket_id,
        "strategy_id": strategy_id, "symbol": "BTCUSDT", "side": "LONG",
        "timeframe": "1h", "signal_price": 100.0,
        "entry_zone_low": 99.0, "entry_zone_high": 101.0,
        "expires_at": (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat(),
        "stop_loss_pct": 0.03, "take_profit_pct": 0.08, "time_stop_hours": 48,
    }


CARDS = [
    {"id": "card_a", "indicatorized": True, "signal_fn": "trigger",
     "symbols": ["BTCUSDT"], "timeframes": ["1h"],
     "exits": {"sl_pct": 0.03, "tp_pct": 0.08}},
    {"id": "card_quiet", "indicatorized": True, "signal_fn": "quiet",
     "symbols": ["ETHUSDT"], "timeframes": ["1h"],
     "exits": {"sl_pct": 0.03, "tp_pct": 0.08}},
]


def scanner_supports_funnel() -> bool:
    return "funnel" in inspect.signature(SignalScanner.__init__).parameters


def trader_supports_funnel() -> bool:
    return "funnel" in inspect.signature(run_strategy_trader).parameters


def trader_supports_ledger() -> bool:
    return "ledger" in inspect.signature(run_strategy_trader).parameters


def dashboard_supports_funnel() -> bool:
    try:
        import dashboard.app as dashboard_app
    except Exception:
        return False
    return all(hasattr(dashboard_app, name) for name in
               ("_funnel_report", "_position_attribution", "_funnel_page_html"))


# --------------------------------------------------------------------- #
def test_module_registry() -> None:
    """必达要点 4：原因码只有九码一个分类轴，细分码必须显式登记。"""
    validate_detail_table()
    check("细分码注册表自检通过", True)
    check("细分码映射 ⊆ U05 九码",
          set(DETAIL_REASON.values()) <= trace.REASON_CODES,
          sorted(set(DETAIL_REASON.values()) - trace.REASON_CODES))
    check("九码全部由细分码可达（无孤立码）",
          set(DETAIL_REASON.values()) == set(trace.REASON_CODES),
          sorted(set(trace.REASON_CODES) - set(DETAIL_REASON.values())))
    check("九码各自都能反查到细分码",
          all(detail_codes_for_reason(code) for code in trace.REASON_CODES))
    check("未登记细分码 fail-closed",
          _raises(funnel_mod.UnknownDetailCode,
                  lambda: funnel_mod.detail_reason("NOT_REGISTERED")))
    check("拒绝型闸门不带细分码即拒绝登记（原因不允许缺失）",
          _raises(ValueError, lambda: ScanFunnel(round_id="r").record(
              Stage.SIGNAL_TRIGGERED.value)))
    check("每个细分码都登记了性质与允许的闸门（两张表不是各自自洽）",
          set(DETAIL_NATURE) == set(DETAIL_REASON) == set(DETAIL_STAGES))
    check("细分码挂错闸门即拒绝（不是'随便挂哪都行'）",
          _raises(ValueError, lambda: ScanFunnel(round_id="r").record(
              Stage.SIGNAL_TRIGGERED.value, funnel_mod.DETAIL_KLINE_FETCH_FAILED))
          and _raises(ValueError, lambda: ScanFunnel(round_id="r").record(
              Stage.CARD_INDICATORIZED.value, funnel_mod.DETAIL_CARDS_UNAVAILABLE)))
    check("终态/通过型闸门带细分码即拒绝（不被静默算成'被拒绝'）",
          _raises(ValueError, lambda: ScanFunnel(round_id="r").record(
              Stage.INTENT_READY.value, funnel_mod.DETAIL_NO_SIGNAL_ON_CLOSED_BAR)))
    check("轮级专用码只能经 record_round_fault 登记",
          bool(ROUND_ONLY_DETAILS)
          and ScanFunnel(round_id="r").record_round_fault(
              funnel_mod.DETAIL_CARDS_UNAVAILABLE).reason
          == trace.REASON_READINESS_BLOCKED)


def test_faults_are_not_read_as_no_edge() -> None:
    """必达要点 1 的**核心安全性质**：故障与"真的没有信号"必须可区分。

    左侧：三类故障各自**不再**产出 no_edge；右侧：真的 no_edge 仍产出 no_edge。
    """
    # 故障①：卡片配置缺陷（三张卡全部未指标化）
    cards_off = ScanFunnel(round_id="fault-card")
    for idx in range(3):
        cards_off.record(Stage.CARD_INDICATORIZED.value,
                         funnel_mod.DETAIL_CARD_NOT_INDICATORIZED, card_id=f"c{idx}")
    report = cards_off.no_open_explanation()
    check("故障①卡册配置缺陷：verdict/主因都不是 NO_EDGE",
          report["verdict"] != "no_edge"
          and report["primary_reason"] == trace.REASON_VALIDATION_REJECTED, report)

    # 故障②：票据源读取失败（轮级故障，与"本轮没有票据"区分）
    source = ScanFunnel(round_id="fault-source")
    source.record_round_fault(funnel_mod.DETAIL_TICKET_SOURCE_UNAVAILABLE,
                              note="fetch_open_signal_tickets 抛错")
    report = source.no_open_explanation()
    check("故障②票据源读取失败：blocked + DATA_UNAVAILABLE（不是 no_edge）",
          report["verdict"] == "blocked"
          and report["primary_reason"] == trace.REASON_DATA_UNAVAILABLE, report)
    check("票据源读取失败与'本轮没有票据'是两个不同的细分码（可区分）",
          funnel_mod.DETAIL_TICKET_SOURCE_UNAVAILABLE
          != funnel_mod.DETAIL_NO_TICKETS_AVAILABLE
          and DETAIL_NATURE[funnel_mod.DETAIL_TICKET_SOURCE_UNAVAILABLE] == NATURE_FAULT
          and DETAIL_NATURE[funnel_mod.DETAIL_NO_TICKETS_AVAILABLE] != NATURE_FAULT)

    # 故障③：轮级前置失败（卡册读不到 / 运行时不可用 / 通道预算为 0）
    for detail, expect in (
            (funnel_mod.DETAIL_CARDS_UNAVAILABLE, trace.REASON_READINESS_BLOCKED),
            (funnel_mod.DETAIL_CARDS_INVALID, trace.REASON_VALIDATION_REJECTED),
            (funnel_mod.DETAIL_STRATEGY_LAB_UNAVAILABLE, trace.REASON_READINESS_BLOCKED),
            (funnel_mod.DETAIL_STRATEGY_LAB_DRIFT, trace.REASON_VALIDATION_REJECTED)):
        f = ScanFunnel(round_id="fault-round")
        f.record_round_fault(detail, note="轮级前置失败")
        rep = f.no_open_explanation()
        check(f"故障③轮级 {detail}：blocked/{expect}，候选数不被虚增",
              rep["verdict"] == "blocked" and rep["primary_reason"] == expect
              and rep["candidates"] == 0 and rep["evaluated"] == 0, rep)

    # 反向误标：范围界定不得把真实 no_edge 挤成 blocked
    scoped = ScanFunnel(round_id="scoped")
    scoped.record(Stage.SIGNAL_TRIGGERED.value,
                  funnel_mod.DETAIL_NO_SIGNAL_ON_CLOSED_BAR)
    scoped.record(Stage.SYMBOL_IN_SCOPE.value, funnel_mod.DETAIL_SYMBOL_NOT_IN_SCOPE,
                  symbol="SOLUSDT")
    rep = scoped.no_open_explanation()
    check("真 no_edge + 范围外的卡：仍 no_edge/NO_EDGE（不再常态误判 blocked）",
          rep["verdict"] == "no_edge" and rep["primary_reason"] == trace.REASON_NO_EDGE,
          rep)
    check("范围界定码的性质是 scope（可见但不抢主因）",
          DETAIL_NATURE[funnel_mod.DETAIL_SYMBOL_NOT_IN_SCOPE] == NATURE_SCOPE
          and trace.REASON_READINESS_BLOCKED in rep["reason_codes"])

    # 右侧：真的没有信号仍然必须是 no_edge
    plain = ScanFunnel(round_id="plain")
    plain.record(Stage.SIGNAL_TRIGGERED.value,
                 funnel_mod.DETAIL_NO_SIGNAL_ON_CLOSED_BAR)
    rep = plain.no_open_explanation()
    check("真的没有信号：仍 no_edge/NO_EDGE 且 evaluated>0",
          rep["verdict"] == "no_edge" and rep["primary_reason"] == trace.REASON_NO_EDGE
          and rep["evaluated"] == 1, rep)

    # 已在处理（全部票已消费）不得读成 no_edge
    consumed = ScanFunnel(round_id="consumed")
    consumed.record(Stage.TICKET_FRESH.value,
                    funnel_mod.DETAIL_TICKET_ALREADY_CONSUMED, ticket_id="t-1")
    rep = consumed.no_open_explanation()
    check("所有票都已消费：verdict=handled/HOLD_EXISTING（不是 no_edge）",
          rep["verdict"] == "handled"
          and rep["primary_reason"] == trace.REASON_HOLD_EXISTING, rep)

    # 顶层与内嵌 no_open 共用同一次判定（不允许自相矛盾）
    for label, kwargs in (
            ("源级错误", {"source_errors": ["macro_public: 5xx"],
                          "source_labels": ["macro_public"]}),
            ("模型超时", {"model_error": TimeoutError("超时")})):
        explained = funnel_mod.round_explanation(funnel=plain, **kwargs)
        check(f"顶层与内嵌 no_open 一致（{label}）",
              explained["verdict"] == explained["no_open"]["verdict"] == "blocked"
              and explained["primary_reason"]
              == explained["no_open"]["primary_reason"],
              (explained["verdict"], explained["no_open"]["verdict"]))


def test_cross_process_consumption() -> None:
    """必达要点 2 的**跨进程**形态（B-r1 P0-3）：N 个真实子进程抢同一张票。

    修复前：4 个子进程**全部**拿到 CONSUMED（各自只读自己内存里的账本）。
    修复后：恰好 1 个 CONSUMED，其余 ALREADY_CONSUMED。
    """
    import multiprocessing as mp

    src_dir = str(ROOT / "src")
    with tempfile.TemporaryDirectory(prefix="mta-u08-xproc-") as tmp:
        ledger_path = str(Path(tmp) / "led.json")
        ready = Path(tmp) / "ready"
        n_procs = 4
        ctx = mp.get_context("spawn") if hasattr(mp, "get_context") else mp
        with ctx.Pool(n_procs) as pool:
            states = pool.map(_xproc_child, [(src_dir, ledger_path, str(ready), n_procs,
                                              idx) for idx in range(n_procs)])
        check(f"{n_procs} 个进程抢同一张票：恰好 1 个 CONSUMED",
              states.count("CONSUMED") == 1, states)
        check(f"{n_procs} 个进程抢同一张票：其余全部 ALREADY_CONSUMED",
              states.count("ALREADY_CONSUMED") == n_procs - 1, states)
        reloaded = TicketLedger(ledger_path, clock=lambda: 1_700_000_000_000)
        check("跨进程消费事实已落盘（重载后仍 is_consumed）",
              reloaded.is_consumed("t-shared")
              and sum(1 for r in reloaded._records.values() if r.consumed) == 1)
        check("并发下没有子进程抛异常（tmp 名带 pid/uuid，不再 PermissionError）",
              not [s for s in states if str(s).startswith("EXC:")], states)


def _xproc_child(args) -> str:
    """子进程体：先 load 完成，等所有兄弟 ready 后再 consume（确定性竞态窗口）。"""
    import os
    import time

    src_dir, ledger_path, ready_dir, n_procs, idx = args
    sys.path.insert(0, src_dir)
    from core.strategy_funnel import TicketLedger as _Ledger

    led = _Ledger(ledger_path, clock=lambda: 1_700_000_000_000)
    ready = Path(ready_dir)
    ready.mkdir(parents=True, exist_ok=True)
    (ready / f"{os.getpid()}_{idx}").write_text("1", encoding="utf-8")
    deadline = time.time() + 30
    while len(list(ready.iterdir())) < n_procs and time.time() < deadline:
        time.sleep(0.005)
    try:
        return led.consume("t-shared", round_id=f"round-{idx}").state
    except Exception as exc:  # noqa: BLE001
        return f"EXC:{type(exc).__name__}:{exc}"[:80]


def _raises(exc_type, fn) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


def test_scan_round_end_to_end() -> None:
    """必达要点 1：一轮真实扫描的每个候选都停在一道带原因的闸门。"""
    import signal_engine.scanner as scanner_module

    with tempfile.TemporaryDirectory(prefix="mta-u08-smoke-scan-") as tmp:
        root = Path(tmp)
        cards = json.loads(json.dumps(CARDS))
        cards.append({"id": "card_nofn", "indicatorized": True,
                      "signal_fn": "missing_fn", "symbols": ["BTCUSDT"],
                      "timeframes": ["1h"], "exits": {"sl_pct": 0.03, "tp_pct": 0.08}})
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
        patched = scanner_supports_funnel()
        note("scanner funnel 接线 "
             f"{'applied' if patched else 'not_applied'}")
        kwargs = {"knowledge_dir": root, "strategy_lab_src": "unused",
                  "state_file": root / "state.json", "db": db}
        injected = None
        if patched:
            injected = ScanFunnel(round_id="smoke-round")
            kwargs["funnel"] = injected
        scanner = SignalScanner(**kwargs)
        scanner._registry = {
            "trigger": lambda frame, _: pd.DataFrame(
                {"signal": 1, "side": "LONG"}, index=frame.index),
            "quiet": lambda frame, _: pd.DataFrame(
                {"signal": 0, "side": ""}, index=frame.index),
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
        original = scanner_module.fetch_klines
        scanner_module.fetch_klines = lambda *a, **k: frame
        try:
            tickets = scanner.scan(now=now, scan_symbols=["BTCUSDT", "ETHUSDT"])
        finally:
            scanner_module.fetch_klines = original

        check("真实扫描轮次产出 1 张 ticket", len(tickets) == 1, tickets)
        last_scan = json.loads((root / "state.json").read_text(
            encoding="utf-8"))["last_scan"]
        check("状态文件保留既有键（向后兼容）",
              {"status", "tickets", "market_pairs", "successful_pairs",
               "error_count"} <= set(last_scan))
        if patched:
            report = last_scan.get(funnel_mod.FUNNEL_REPORT_KEY)
            check("轮次落盘带漏斗段", isinstance(report, dict))
            details = {item["detail"] for item in (report or {}).get("details", [])}
            check("五类跳过原因各自可区分",
                  details == {funnel_mod.DETAIL_SIGNAL_FN_MISSING,
                              funnel_mod.DETAIL_CARD_NOT_INDICATORIZED,
                              funnel_mod.DETAIL_SYMBOL_NOT_IN_SCOPE,
                              funnel_mod.DETAIL_TIMEFRAME_UNSUPPORTED,
                              funnel_mod.DETAIL_NO_SIGNAL_ON_CLOSED_BAR}, details)
            check("轮次带 no_open_reason（不得只报 ok）",
                  last_scan.get("no_open_reason") in trace.REASON_CODES,
                  last_scan.get("no_open_reason"))
            check("注入实例与落盘摘要同源",
                  injected is not None
                  and (report or {}).get("round_id") == "smoke-round"
                  and injected.tickets_emitted == 1)
            check("漏斗从摘要重建后仍能回答为什么没开（重启后读侧）",
                  funnel_from_report(report).no_open_explanation()["primary_reason"]
                  == last_scan.get("no_open_reason")
                  and funnel_from_report(report).no_open_explanation()["verdict"]
                  == last_scan.get("no_open_verdict"),
                  (funnel_from_report(report).no_open_explanation(),
                   last_scan.get("no_open_reason")))
        else:
            check("未接线时状态文件没有漏斗段（不冒充已接线）",
                  funnel_mod.FUNNEL_REPORT_KEY not in last_scan)
            check("未接线时旧状态无法区分跳过原因（本补丁要修的事）",
                  "no_open_reason" not in last_scan and "no_open_verdict" not in last_scan)
        db.close()


def test_execution_and_ledger() -> None:
    """必达要点 1/2：执行侧逐票原因 + 消费一次（跨轮/重启）。"""
    patched = trader_supports_funnel()
    note("strategy_trader funnel 接线 "
         f"{'applied' if patched else 'not_applied'}")
    if not patched:
        decision = run_strategy_trader(tickets=[live_ticket("t-x")],
                                       prices={"BTCUSDT": 100.0})
        check("未接线时既有行为不变：有效 ticket 仍产出 1 笔 intent",
              len(decision.intents) == 1)
        check("未接线时执行器不接受 funnel/ledger 参数（不冒充已接线）",
              not trader_supports_funnel() and not trader_supports_ledger())
        return

    with tempfile.TemporaryDirectory(prefix="mta-u08-smoke-exec-") as tmp:
        ledger = TicketLedger(Path(tmp) / "led.json", clock=lambda: 1_700_000_000_000)
        first = ScanFunnel(round_id="exec-1")
        d1 = run_strategy_trader(tickets=[live_ticket("t-led")],
                                 prices={"BTCUSDT": 100.0}, funnel=first, ledger=ledger)
        check("首轮消费 => 1 笔 intent 且账本记为 CONSUMED",
              len(d1.intents) == 1 and ledger.is_consumed("t-led"), d1.reasoning)
        check("执行侧到达终态计入 opened", first.opened == 1, first.as_dict())

        second = ScanFunnel(round_id="exec-2")
        d2 = run_strategy_trader(tickets=[live_ticket("t-led")],
                                 prices={"BTCUSDT": 100.0}, funnel=second, ledger=ledger)
        check("同 ticket 跨轮不得重复消费（第二轮回带 0 笔）", len(d2.intents) == 0)
        check("跨轮跳过的原因是 HOLD_EXISTING/TICKET_ALREADY_CONSUMED",
              second.detail_counts.get(funnel_mod.DETAIL_TICKET_ALREADY_CONSUMED) == 1,
              second.as_dict())

        reloaded = TicketLedger(Path(tmp) / "led.json",
                               clock=lambda: 1_700_000_000_000)
        third = ScanFunnel(round_id="exec-3")
        d3 = run_strategy_trader(tickets=[live_ticket("t-led")],
                                 prices={"BTCUSDT": 100.0}, funnel=third, ledger=reloaded)
        check("重启后（重载账本）仍不重复消费", len(d3.intents) == 0, d3.reasoning)
        check("重启后账本仍记得首轮出处",
              reloaded.get("t-led") is not None
              and reloaded.get("t-led").consumed_round_id == "exec-1")

        cases = [
            ("价格在入场区外", dict(prices={"BTCUSDT": 1.0}),
             funnel_mod.DETAIL_PRICE_OUTSIDE_ENTRY_ZONE, trace.REASON_NO_EDGE),
            ("白名单外", dict(prices={"BTCUSDT": 100.0}, allowed_symbols=["ETHUSDT"]),
             funnel_mod.DETAIL_SYMBOL_NOT_ALLOWED, trace.REASON_READINESS_BLOCKED),
            ("同 symbol 已有仓位",
             dict(prices={"BTCUSDT": 100.0},
                  portfolio={"positions": [{"symbol": "BTCUSDT", "source": "strategy",
                                            "strategy_id": "card_a"}]}),
             funnel_mod.DETAIL_SYMBOL_ALREADY_HELD, trace.REASON_CHANNEL_CONFLICT),
        ]
        for idx, (label, kwargs, detail, reason) in enumerate(cases):
            f = ScanFunnel(round_id=f"skip-{idx}")
            decision = run_strategy_trader(
                tickets=[live_ticket(f"t-{idx}")], funnel=f,
                ledger=TicketLedger(Path(tmp) / f"l{idx}.json"), **kwargs)
            check(f"{label}：0 笔 intent 且原因是 {reason}/{detail}",
                  len(decision.intents) == 0
                  and f.detail_counts.get(detail) == 1
                  and f.no_open_explanation()["primary_reason"] == reason,
                  f.as_dict())
            check(f"{label}：note 带九码（运维可直接读）", reason in decision.reasoning,
                  decision.reasoning[:120])

        expired_f = ScanFunnel(round_id="expired")
        expired_decision = run_strategy_trader(
            tickets=[dict(live_ticket("t-exp"),
                          expires_at=(datetime.now(timezone.utc)
                                      - timedelta(hours=1)).isoformat())],
            prices={"BTCUSDT": 100.0}, funnel=expired_f)
        check("过期票据：0 笔 intent 且原因是 VALIDATION_REJECTED/TICKET_EXPIRED",
              len(expired_decision.intents) == 0
              and expired_f.detail_counts.get(funnel_mod.DETAIL_TICKET_EXPIRED) == 1,
              expired_decision.reasoning)


def test_attribution() -> None:
    """必达要点 3：每仓能定位管理者与规则版本。"""
    positions = [
        {"symbol": "BTCUSDT", "side": "LONG", "source": "strategy",
         "strategy_id": "card_a", "ticket_id": "t-1"},
        {"symbol": "ETHUSDT", "side": "SHORT", "source": "ai"},
        {"symbol": "SOLUSDT", "side": "LONG"},
    ]
    att = attribute_positions(positions, CARDS)
    by_symbol = {item.symbol: item for item in att}
    check("三个仓位都给出了管理者",
          {item.manager for item in att} == {"strategy", "ai", "legacy_unattributed"},
          [item.manager for item in att])
    check("策略仓规则版本已知（rv1:<hash>）",
          by_symbol["BTCUSDT"].rule_known
          and by_symbol["BTCUSDT"].rule_version.startswith("rv1:"))
    check("卡片不在卡册的仓位标 rule_known=False（不冒充已知版本）",
          not by_symbol["ETHUSDT"].rule_known)
    check("manager 与 position_class 是两个正交事实",
          by_symbol["ETHUSDT"].manager == "ai"
          and by_symbol["ETHUSDT"].position_class == "LEGACY_UNATTRIBUTED")
    summary = attribution_summary(att)
    check("聚合视图按管理者计数并单列规则未知仓位",
          summary["by_manager"] == {"strategy": 1, "ai": 1, "legacy_unattributed": 1}
          and len(summary["rule_unknown"]) == 2, summary["by_manager"])
    v1 = rule_version_of("card_a", CARDS)
    mutated = [dict(CARDS[0], exits={"sl_pct": 0.05, "tp_pct": 0.09}), CARDS[1]]
    check("改卡即改规则版本（同策略两版规则可分）",
          v1 != rule_version_of("card_a", mutated))


def test_management_priority_budget() -> None:
    """必达要点 5：管理优先预算不被新仓研究挤占。"""
    plan = ManagementPriorityPlan.from_total(8, reserve_ratio=0.5)
    check("切出管理预留（8 -> 预留 4）", plan.management_reserve == 4, plan.as_dict())
    # 真正的不变量是"研究碰不到预留池"，**不是**"management_available 不下降"（P2-4）。
    check("研究取用碰不到预留池（管理'已用+仍可取' >= 预留量）",
          plan.claim_research(4) == 4
          and plan.management_used + plan.management_available >= plan.management_reserve
          and plan.management_available == 4, plan.as_dict())
    check("研究池耗尽后不再放行研究", plan.claim_research(1) == 0)
    check("管理侧请求全满足 => meets_management_demand()=True",
          plan.meets_management_demand() and plan.management_shortfall == 0)
    squeezed = ManagementPriorityPlan.from_total(8, reserve_ratio=0.5)
    squeezed.claim_management(9)
    check("管理侧没拿到请求量 => meets_management_demand()=False（旧实现恒 True）",
          squeezed.meets_management_demand() is False and squeezed.management_shortfall == 1)
    zero_reason = ManagementPriorityPlan.from_total(0, reserve_ratio=0.5).research_reason()
    check("总预算为 0 也有显式原因（MODEL_DEADLINE + RESEARCH_BUDGET_ZERO）",
          zero_reason is not None
          and funnel_mod.DETAIL_RESEARCH_BUDGET_ZERO in zero_reason.details,
          None if zero_reason is None else zero_reason.as_dict())
    check("非法比例的实际生效比例在 as_dict 里可见（P3-3）",
          ManagementPriorityPlan.from_total(4, reserve_ratio=float("nan")).as_dict()[
              "reserve_ratio_effective"] == 0.0)

    starved = ManagementPriorityPlan.from_total(6, reserve_ratio=0.5)
    starved.claim_management(6)
    reason = starved.research_reason()
    check("研究被管理挤占时给出 MODEL_DEADLINE + MANAGEMENT_PRIORITY_BUDGET",
          starved.research_starved and reason is not None
          and reason.primary == trace.REASON_MODEL_DEADLINE
          and funnel_mod.DETAIL_MANAGEMENT_PRIORITY_BUDGET in reason.details)

    adversarial = ManagementPriorityPlan.from_total(10, reserve_ratio=0.4)
    reserve_ok = conservation_ok = floor_ok = True
    for step in range(12):
        if step % 3 == 0:
            adversarial.claim_management(3)
        else:
            adversarial.claim_research(5)
        if adversarial.research_used > (adversarial.total_budget
                                        - adversarial.management_reserve):
            reserve_ok = False
        if (adversarial.management_used + adversarial.research_used
                > adversarial.total_budget):
            conservation_ok = False
        if (adversarial.management_used + adversarial.management_available
                < adversarial.management_reserve):
            floor_ok = False
    check("任意交错下研究都碰不到管理预留", reserve_ok)
    check("任意交错下不超发（守恒）", conservation_ok)
    check("管理侧永远保有不低于预留量的额度（不被新仓研究挤占）", floor_ok)


def test_dashboard_display() -> None:
    """共享适配（看板）：两态各按当前事实断言。"""
    patched = dashboard_supports_funnel()
    note(f"dashboard 漏斗展示 {'applied' if patched else 'not_applied'}")
    if not patched:
        check("未接线时看板没有漏斗页助手（不冒充已接线）", True)
        return
    import dashboard.app as dashboard_app

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

    with tempfile.TemporaryDirectory(prefix="mta-u08-smoke-dash-") as tmp:
        funnel = ScanFunnel(round_id="dash")
        funnel.record(Stage.SIGNAL_TRIGGERED.value,
                      funnel_mod.DETAIL_NO_SIGNAL_ON_CLOSED_BAR,
                      card_id="card_quiet", symbol="ETHUSDT", timeframe="1h")
        funnel.record(Stage.INTENT_READY.value, card_id="card_a", symbol="BTCUSDT",
                      timeframe="1h", ticket_id="t-1")
        (Path(tmp) / "state.json").write_text(json.dumps({"last_scan": {
            "at": "2026-09-21T00:00:00+00:00", "status": "ok", "tickets": 1,
            funnel_mod.FUNNEL_REPORT_KEY: funnel.as_dict(),
            "no_open_reason": funnel.no_open_explanation()["primary_reason"],
            "no_open_verdict": funnel.no_open_explanation()["verdict"],
        }}), encoding="utf-8")

        class StatefulSettings(FakeSettings):
            project_root = Path(tmp)
            raw_settings = {"signal_scanner": {"signal_state_file": "state.json"}}

        class StatefulService(FakeService):
            settings = StatefulSettings()

        service = StatefulService()
        report = dashboard_app._funnel_report(service)
        attribution = dashboard_app._position_attribution(service)
        page = dashboard_app._funnel_page_html(report, attribution)
        check("看板读到漏斗并展示主因/候选/开仓",
              report.get("available") is True
              and report.get("primary_reason") == trace.REASON_HOLD_EXISTING
              and report.get("opened") == 1, report)
        check("看板展示每个仓位的管理者与规则版本",
              attribution["total"] == 1
              and attribution["positions"][0]["manager"] == "strategy"
              and attribution["positions"][0]["rule_version"].startswith("rv1:"),
              attribution)
        check("漏斗页含闸门/细分码/策略/规则版本",
              all(marker in page for marker in
                  (Stage.SIGNAL_TRIGGERED.value, "NO_SIGNAL_ON_CLOSED_BAR",
                   "F_OLD_MACD_RSI_8H", "rv1:")))
        check("漏斗段缺失时给出显式说明而不是空白成功页",
              "没有可用漏斗数据" in dashboard_app._funnel_page_html(
                  dashboard_app._funnel_report(FakeService()), {}))


def main() -> int:
    print("=" * 72)
    print("U08 策略漏斗冒烟（离线；两态均可运行）")
    print("=" * 72)
    run_case("[1] 原因码注册表与九码一致", test_module_registry)
    run_case("[2] 故障不得被读成'没有信号'（含反向误标）",
             test_faults_are_not_read_as_no_edge)
    run_case("[3] 真实扫描轮次的逐项原因", test_scan_round_end_to_end)
    run_case("[4] 执行侧原因 + ticket 消费一次", test_execution_and_ledger)
    run_case("[5] 跨进程并发消费（P0-3）", test_cross_process_consumption)
    run_case("[6] 每仓管理者与规则版本", test_attribution)
    run_case("[7] 管理优先预算", test_management_priority_budget)
    run_case("[8] 看板展示", test_dashboard_display)
    print("\n" + "=" * 72)
    print(f"结果：PASS {PASS} / FAIL {FAIL}")
    if FAILURES:
        print("失败项：")
        for name in FAILURES:
            print(f"  - {name}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
