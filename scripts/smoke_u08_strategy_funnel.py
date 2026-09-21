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
import sqlite3
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
    LEDGER_WRITE_FAILED,
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
    # B-r3 N3：收尾落盘被推迟也是一个**轮级故障**，必须登记齐全（否则
    # record_round_fault 会 ValueError，故障反而变成崩溃）。
    check("收尾落盘推迟码 LEDGER_SAVE_DEFERRED 登记为轮级故障",
          funnel_mod.DETAIL_LEDGER_SAVE_DEFERRED in ROUND_ONLY_DETAILS
          and DETAIL_REASON[funnel_mod.DETAIL_LEDGER_SAVE_DEFERRED]
          == trace.REASON_READINESS_BLOCKED
          and DETAIL_NATURE[funnel_mod.DETAIL_LEDGER_SAVE_DEFERRED] == NATURE_FAULT
          and ScanFunnel(round_id="r").record_round_fault(
              funnel_mod.DETAIL_LEDGER_SAVE_DEFERRED).reason
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

        # --- B-r3 N1：卡册损坏必须在**主路径**（注册表尚未加载）上记 CARDS_INVALID ---
        broken_root = root / "broken_cards_first_run"
        broken_root.mkdir()
        (broken_root / "strategy_cards.json").write_text("{ nope", encoding="utf-8")
        broken_funnel = ScanFunnel(round_id="smoke-broken-cards")
        # 用**独立的库**：上面的扫描已经把一张 ticket 落库，同一个库会让"本轮票据"
        # 非空，从而掩盖"卡册损坏时本轮没有新候选"这个事实。
        broken_db = AuditDatabase(root / "audit_broken.sqlite")
        broken_scanner = SignalScanner(        # 注意：**不**预置 _registry/_features_mod
            knowledge_dir=broken_root, strategy_lab_src="unused",
            state_file=root / "state_broken.json", db=broken_db)
        if patched:
            broken_scanner.funnel = broken_funnel
        broken_error = ""
        broken_tickets: list = []
        original_fetch = scanner_module.fetch_klines
        scanner_module.fetch_klines = lambda *a, **k: frame
        try:
            broken_tickets = broken_scanner.scan(now=now, scan_symbols=["BTCUSDT"])
        except Exception as exc:  # noqa: BLE001 - 修复前会抛 JSONDecodeError
            broken_error = type(exc).__name__
        finally:
            scanner_module.fetch_klines = original_fetch
        if patched:
            broken_report = broken_funnel.no_open_explanation()
            broken_faults = [f["detail"] for f in broken_report["round_faults"]]
            check("N1 卡册损坏在主路径上记 CARDS_INVALID"
                  "（修复前被误标成 STRATEGY_LAB_DRIFT，主路径不可达）",
                  broken_error == "" and broken_tickets == []
                  and funnel_mod.DETAIL_CARDS_INVALID in broken_faults
                  and funnel_mod.DETAIL_STRATEGY_LAB_DRIFT not in broken_faults
                  and broken_report["verdict"] == "blocked"
                  and broken_report["primary_reason"] == trace.REASON_VALIDATION_REJECTED,
                  (broken_error, broken_faults))
        else:
            check("未接线时卡册损坏的失败方向不变（仍是整轮中断，不是 no_edge）",
                  broken_error != "" and broken_tickets == [],
                  broken_error)
        broken_db.close()
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

        # --- B-r3 N3：收尾落盘取不到锁不得毁掉整轮 ---------------------------- #
        # 构造：只让收尾的 save() 抛真类型 LedgerLockTimeout（consume()/release() 走
        # 内部 _save_locked()，不受影响），精确复刻"消费已落盘、只有合并写取不到锁"。
        class _LockedOutLedger(TicketLedger):
            def save(self) -> None:  # noqa: D102 - 构造用替身
                raise funnel_mod.LedgerLockTimeout("构造：收尾落盘取不到跨进程锁")

        n3_path = Path(tmp) / "led_n3.json"
        n3_ledger = _LockedOutLedger(n3_path, clock=lambda: 1_700_000_000_000)
        n3 = ScanFunnel(round_id="smoke-ledger-timeout")
        n3_decision = run_strategy_trader(tickets=[live_ticket("t-n3")],
                                          prices={"BTCUSDT": 100.0}, funnel=n3,
                                          ledger=n3_ledger)
        check("N3 收尾 save() 锁超时不再穿出执行器，本轮仍有明确结论",
              n3_decision.decision == "open", n3_decision.reasoning[:160])
        check("N3 已构造的 intents 不被丢弃（仍回带 1 笔）",
              len(n3_decision.intents) == 1, n3_decision.intents)
        check("N3 落盘被推迟登记为轮级故障 LEDGER_SAVE_DEFERRED",
              n3.detail_counts.get(funnel_mod.DETAIL_LEDGER_SAVE_DEFERRED) == 1
              and [f.detail for f in n3.round_faults]
              == [funnel_mod.DETAIL_LEDGER_SAVE_DEFERRED], n3.as_dict())
        reread = TicketLedger(n3_path, clock=lambda: 1_700_000_000_000)
        check("N3 消费事实仍在盘上（orphan_consumed 可定位，补单不重发）",
              reread.is_consumed("t-n3")
              and [r.ticket_id for r in reread.orphan_consumed()] == ["t-n3"])

        # --- B-r3 N4：未注入漏斗时 release 也必须真的撤销 ---------------------- #
        import agents.strategy_trader as trader_mod

        n4_ledger = TicketLedger(Path(tmp) / "led_n4.json",
                                 clock=lambda: 1_700_000_000_000)
        original_build = trader_mod.build_intent_from_ticket

        def _boom(*_a: object, **_k: object) -> None:
            raise ValueError("构造：建意图失败")

        try:
            trader_mod.build_intent_from_ticket = _boom
            n4_decision = run_strategy_trader(tickets=[live_ticket("t-n4")],
                                              prices={"BTCUSDT": 100.0}, ledger=n4_ledger)
        finally:
            trader_mod.build_intent_from_ticket = original_build
        n4_record = n4_ledger.get("t-n4")
        check("N4 未注入漏斗时建意图失败 → 同轮释放，票据不被永久占死",
              n4_decision.intents == [] and not n4_ledger.is_consumed("t-n4"),
              None if n4_record is None else n4_record.as_dict())
        check("N4 释放有审计留痕（release_count=1 / release_round_id 非空）",
              n4_record is not None and n4_record.release_count == 1
              and bool(n4_record.release_round_id),
              None if n4_record is None else n4_record.as_dict())

        # --- B-r4 N6：账本**写盘本身**失败（磁盘满/只读盘/文件被占用）也不得穿出 --- #
        # 收尾 save()：任何异常都局部化（不再只吞 LedgerLockTimeout/LedgerCorrupt）。
        for exc_name, exc_factory in (
                ("OSError", lambda: OSError(28, "No space left on device")),
                ("PermissionError", lambda: PermissionError(13, "denied")),
                ("sqlite3.OperationalError",
                 lambda: sqlite3.OperationalError("database is locked"))):
            class _SaveBoom(TicketLedger):
                def save(self, _factory=exc_factory) -> None:  # noqa: D102 - 构造用替身
                    raise _factory()

            boom_path = Path(tmp) / f"led_n6_{exc_name.replace('.', '_')}.json"
            boom_led = _SaveBoom(boom_path, clock=lambda: 1_700_000_000_000)
            boom_f = ScanFunnel(round_id=f"smoke-save-{exc_name}")
            escaped = None
            decision = None
            try:
                decision = run_strategy_trader(tickets=[live_ticket("t-n6")],
                                               prices={"BTCUSDT": 100.0},
                                               funnel=boom_f, ledger=boom_led)
            except Exception as inner:  # noqa: BLE001 - 修复前这里就是穿出点
                escaped = type(inner).__name__
            check(f"N6 收尾 save() 抛 {exc_name} 不再穿出执行器，intents 不被丢弃",
                  escaped is None and decision is not None
                  and decision.decision == "open" and len(decision.intents) == 1,
                  (escaped, None if decision is None else decision.reasoning[:140]))
            check(f"N6 {exc_name} 照样登记轮级故障 LEDGER_SAVE_DEFERRED",
                  boom_f.detail_counts.get(funnel_mod.DETAIL_LEDGER_SAVE_DEFERRED) == 1
                  and [x.detail for x in boom_f.round_faults]
                  == [funnel_mod.DETAIL_LEDGER_SAVE_DEFERRED], boom_f.as_dict())
            n6_reread = TicketLedger(boom_path, clock=lambda: 1_700_000_000_000)
            check(f"N6 {exc_name} 下孤儿可审计（盘上已消费、orphan_consumed 可定位）",
                  n6_reread.is_consumed("t-n6")
                  and [r.ticket_id for r in n6_reread.orphan_consumed()] == ["t-n6"])

        # 账本内部的写盘收敛（consume/release/BaseException）见 [9]：那一组不依赖
        # 共享适配接线，两态都跑；这里只留"经由执行器"的那部分。

        # 执行器侧：认领抛异常 → 该票 fail-closed 跳过，其余票照常
        cc_path = Path(tmp) / "led_n6_consume_call.json"
        cc_led = TicketLedger(cc_path, clock=lambda: 1_700_000_000_000)
        cc_real_consume = cc_led.consume

        def _consume_boom(ticket_id, **kwargs):
            if str(ticket_id) == "t-bad":
                raise OSError(28, "No space left on device")
            return cc_real_consume(ticket_id, **kwargs)

        cc_led.consume = _consume_boom  # type: ignore[method-assign]
        cc_f = ScanFunnel(round_id="smoke-consume-boom")
        cc_decision = run_strategy_trader(
            tickets=[live_ticket("t-bad"), live_ticket("t-good")],
            prices={"BTCUSDT": 100.0}, funnel=cc_f, ledger=cc_led)
        check("N6 认领抛 OSError 不再穿出：坏票 fail-closed 跳过，同轮其余票照常成交",
              cc_decision.decision == "open" and len(cc_decision.intents) == 1
              and cc_f.detail_counts.get(
                  funnel_mod.DETAIL_TICKET_LEDGER_UNAVAILABLE) == 1
              and cc_led.is_consumed("t-good") and "OSError" in cc_decision.reasoning,
              cc_decision.reasoning[:160])


def test_ledger_write_failure_contained() -> None:
    """B-r4 N6：账本**写盘本身**失败（磁盘满/只读盘/文件被占用）不得穿出，也不得
    在内存里留下"没落盘的已消费"（否则下一次成功 save() 会把它洗成已消费）。

    只依赖 U08 独占模块，**两态都跑**。
    """
    note("账本写盘失败收敛（N6）")
    with tempfile.TemporaryDirectory(prefix="mta-u08-smoke-n6-") as tmp:
        clock = lambda: 1_700_000_000_000  # noqa: E731
        for exc in (OSError(28, "No space left on device"),
                    PermissionError(13, "denied"),
                    sqlite3.OperationalError("database is locked")):
            exc_name = type(exc).__name__
            exc_label = f"{type(exc).__module__}.{exc_name}"
            w_path = Path(tmp) / f"led_n6_w_{exc_name}.json"
            w_led = TicketLedger(w_path, clock=clock)
            w_real_save_locked = w_led._save_locked

            def _boom_save(_exc=exc) -> None:
                raise _exc

            w_led._save_locked = _boom_save
            w_escaped = None
            claim = None
            try:
                claim = w_led.consume("t-w", round_id="r1")
            except Exception as inner:  # noqa: BLE001 - 修复前这里就是穿出点
                w_escaped = type(inner).__name__
            w_led._save_locked = w_real_save_locked
            check(f"N6 账本写盘抛 {exc_label}：consume() 不穿出，返回 LEDGER_WRITE_FAILED"
                  f"（带底层异常名）且内存/盘上都不算已消费",
                  w_escaped is None and claim is not None
                  and claim.state == LEDGER_WRITE_FAILED and claim.error == exc_name
                  and not w_led.is_consumed("t-w")
                  and not TicketLedger(w_path, clock=clock).is_consumed("t-w"),
                  (w_escaped, None if claim is None else claim.as_dict()))

        # release() 写盘失败同样不得穿出（内存已撤销 → True）
        r_path = Path(tmp) / "led_n6_release.json"
        r_led = TicketLedger(r_path, clock=clock)
        r_led.consume("t-r", round_id="r1")
        r_real_save_locked = r_led._save_locked

        def _boom_release_save() -> None:
            raise PermissionError(13, "denied")

        r_led._save_locked = _boom_release_save
        r_escaped = None
        r_result = None
        try:
            r_result = r_led.release("t-r", round_id="r1")
        except Exception as inner:  # noqa: BLE001 - 修复前这里就是穿出点
            r_escaped = type(inner).__name__
        r_led._save_locked = r_real_save_locked
        check("N6 release() 写盘抛 PermissionError：不穿出，内存已撤销（返回 True）",
              r_escaped is None and r_result is True
              and not r_led.is_consumed("t-r"), (r_escaped, r_result))

        # 只吞 Exception：BaseException 必须继续穿出（U14 规矩）
        be_led = TicketLedger(Path(tmp) / "led_n6_base.json", clock=clock)

        def _boom_base_save() -> None:
            raise KeyboardInterrupt("构造：账本写盘时 Ctrl-C")

        be_led._save_locked = _boom_base_save
        be_escaped = None
        try:
            be_led.consume("t-be", round_id="r1")
        except KeyboardInterrupt as inner:
            be_escaped = type(inner).__name__
        check("N6 只吞 Exception：KeyboardInterrupt 仍从 consume() 穿出（未被吞掉）",
              be_escaped == "KeyboardInterrupt", be_escaped)


def test_audit_unavailability_contained() -> None:
    """U08-R5-FU2：审计库 ``db.*`` 抛 ``sqlite3.OperationalError`` 时不得整轮被打断。

    **两态可跑**：修复前本节的 check 会失败（异常穿出执行器、decision 不返回），
    修复后全过；两条路径都不改被测逻辑、不联网、不落真实 ``data/``。

    方向（本修复最关键的一点，做反了比不修更糟）：
    - 去重读取（``signal_ticket_has_order``）是"这张票**已经**下过单吗"的**跨进程
      权威**。**读不出答案 -> 不交易**：该票 fail-closed 跳过并披露
      ``AUDIT_UNAVAILABLE``；**绝不**当成"没下过单"放行（那会**重复下单**），
      也不谎报"已消费"（那会**永久丢票**）。
    - 纯审计写入（``log_risk_event`` / ``update_signal_ticket``）失败：局部化 +
      留痕 + 本轮继续（该开的仓照开）。
    - 只吞 ``Exception``：``KeyboardInterrupt`` 必须继续穿出。
    """
    note("审计库不可用收敛（U08-R5-FU2）")
    if not (trader_supports_funnel() and trader_supports_ledger()):
        check("未接线时不冒充已接线（审计库护栏随 funnel/ledger 接线一并交付）",
              not trader_supports_funnel() and not trader_supports_ledger())
        return

    # 注册表常量优先；未应用本修复时退化为字面量，让 check 干净地失败而不是
    # AttributeError（"两态可跑"要求异常不冒充结果）。
    audit_code = getattr(funnel_mod, "DETAIL_AUDIT_UNAVAILABLE", "AUDIT_UNAVAILABLE")

    class _ReadBoomDB:
        """去重读取抛 OperationalError（如 database is locked）。"""

        def signal_ticket_has_order(self, ticket_id: str) -> bool:
            raise sqlite3.OperationalError("database is locked")

        def log_risk_event(self, **_kw: object) -> None:
            pass

        def update_signal_ticket(self, **_kw: object) -> None:
            pass

    class _OneBadTicketDB:
        """只让 t-bad 这张票的读取抛错。"""

        def signal_ticket_has_order(self, ticket_id: str) -> bool:
            if str(ticket_id) == "t-bad":
                raise sqlite3.OperationalError("database is locked")
            return False

        def log_risk_event(self, **_kw: object) -> None:
            pass

        def update_signal_ticket(self, **_kw: object) -> None:
            pass

    class _WriteBoomDB:
        """去重读取正常，纯审计写入抛 OperationalError。"""

        def signal_ticket_has_order(self, ticket_id: str) -> bool:
            return False

        def log_risk_event(self, **_kw: object) -> None:
            raise sqlite3.OperationalError("database is locked")

        def update_signal_ticket(self, **_kw: object) -> None:
            raise sqlite3.OperationalError("database is locked")

    class _InterruptDB:
        """BaseException 通道：读取时 KeyboardInterrupt。"""

        def signal_ticket_has_order(self, ticket_id: str) -> bool:
            raise KeyboardInterrupt("构造：审计库读取时 Ctrl-C")

        def log_risk_event(self, **_kw: object) -> None:
            pass

        def update_signal_ticket(self, **_kw: object) -> None:
            pass

    def _run(**kwargs: object):
        """返回 ``(decision, escaped_exc_name)``：异常不冒充结果。"""
        try:
            return run_strategy_trader(**kwargs), None  # type: ignore[arg-type]
        except Exception as inner:  # noqa: BLE001 - 修复前这里就是穿出点
            return None, type(inner).__name__

    with tempfile.TemporaryDirectory(prefix="mta-u08-smoke-audit-") as tmp:
        clock = lambda: 1_700_000_000_000  # noqa: E731

        # (1) 去重读取不可用 -> fail-closed 跳过 + 如实披露；**不**放行、**不**烧票
        led = TicketLedger(Path(tmp) / "audit_read.json", clock=clock)
        f1 = ScanFunnel(round_id="smoke-audit-read")
        d1, esc1 = _run(tickets=[live_ticket("t-a")], prices={"BTCUSDT": 100.0},
                        funnel=f1, db=_ReadBoomDB(), ledger=led)
        check("FU2 去重读取抛 OperationalError 不再穿出执行器（decision 仍返回）",
              esc1 is None and d1 is not None, (esc1, None if d1 is None else d1.decision))
        check("FU2 读取不可用 -> fail-closed 跳过：0 笔 intent 且披露 AUDIT_UNAVAILABLE",
              d1 is not None and d1.intents == []
              and f1.detail_counts.get(audit_code) == 1
              and "OperationalError" in (d1.reasoning or ""),
              (None if d1 is None else d1.reasoning[:200], f1.as_dict()))
        check("FU2 跳过不等于'没下过单'放行，也不谎报已消费（三种事实不混）",
              f1.detail_counts.get(funnel_mod.DETAIL_TICKET_ALREADY_CONSUMED) is None
              and f1.detail_counts.get(audit_code) == 1, f1.as_dict())
        check("FU2 跳过**不烧票**（账本未消费，审计库恢复后下一轮仍可评估）",
              not led.is_consumed("t-a"))
        check("FU2 未注册自由文本不得冒充原因码（细分码走注册表）",
              audit_code in funnel_mod.DETAIL_REASON
              and audit_code in funnel_mod.DETAIL_NATURE
              and audit_code in funnel_mod.DETAIL_STAGES,
              audit_code)

        # (2) 单票读取不可用不得牵连同轮其余票
        f2 = ScanFunnel(round_id="smoke-audit-partial")
        d2, esc2 = _run(tickets=[live_ticket("t-bad"), live_ticket("t-good")],
                        prices={"BTCUSDT": 100.0}, funnel=f2, db=_OneBadTicketDB())
        check("FU2 坏票跳过、同轮其余票照常开仓（不因一张票读不出而整轮停摆）",
              esc2 is None and d2 is not None and len(d2.intents) == 1
              and f2.detail_counts.get(audit_code) == 1,
              (esc2, None if d2 is None else d2.reasoning[:200]))

        # (3) 纯审计写入失败：局部化 + 留痕 + 已构造的 intent 不丢
        f3 = ScanFunnel(round_id="smoke-audit-write")
        good_eth = dict(live_ticket("t-good"), symbol="ETHUSDT", signal_id="sig-t-good")
        d3, esc3 = _run(tickets=[good_eth, live_ticket("t-hold")],
                        prices={"BTCUSDT": 100.0, "ETHUSDT": 100.0}, funnel=f3,
                        db=_WriteBoomDB(),
                        portfolio={"positions": [{"symbol": "BTCUSDT", "source": "ai"}]})
        check("FU2 纯审计写入抛 OperationalError 不再穿出，已构造的 intent 不丢",
              esc3 is None and d3 is not None and d3.decision == "open"
              and len(d3.intents) == 1,
              (esc3, None if d3 is None else d3.reasoning[:200]))
        check("FU2 审计写入失败如实披露（note 带操作名与底层异常名，不静默吞掉）",
              d3 is not None and "审计写入失败" in (d3.reasoning or "")
              and "OperationalError" in (d3.reasoning or ""),
              None if d3 is None else d3.reasoning[:240])
        check("FU2 写入失败不改变既有跳过语义（仍记 SYMBOL_ALREADY_HELD）",
              f3.detail_counts.get(funnel_mod.DETAIL_SYMBOL_ALREADY_HELD) == 1,
              f3.as_dict())

        # (4) 只吞 Exception：KeyboardInterrupt 必须继续穿出
        ki_escaped = None
        try:
            run_strategy_trader(tickets=[live_ticket("t-ki")],
                                prices={"BTCUSDT": 100.0}, db=_InterruptDB())
        except KeyboardInterrupt as inner:
            ki_escaped = type(inner).__name__
        except Exception:  # noqa: BLE001 - 退化成普通异常 = 护栏吞了 Ctrl-C
            ki_escaped = None
        check("FU2 只吞 Exception：KeyboardInterrupt 仍穿出执行器（未被护栏吞掉）",
              ki_escaped == "KeyboardInterrupt", ki_escaped)


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
    run_case("[9] 账本写盘失败收敛（N6，两态都跑）",
             test_ledger_write_failure_contained)
    run_case("[10] 审计库不可用收敛（FU2，两态都跑）",
             test_audit_unavailability_contained)
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
