"""U01 复盘统计回归：RV01（NULL 不计赢）、RV02（失效需触发事件）、RV03（校准区分
高低估与小样本）、以及 ``query_review_library`` 纯只读无文件写入副作用。

运行：``python tests/integration/test_review_statistics.py``（无需 pytest / 不联网）
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from audit.database import AuditDatabase  # noqa: E402
from agents.calibration import calibration_text, compute_calibration  # noqa: E402
from agents.lessons import LESSONS_FILE, query_review_library  # noqa: E402
from agents.review import (  # noqa: E402
    _build_outcome_stats,
    inspect_invalidation_triggers,
    invalidation_triggered,
)

WIDE_START = "1999-01-01T00:00:00"
WIDE_END = "2999-01-01T00:00:00"
FAILURES: list[str] = []
PASS_COUNT = 0


def check(name: str, condition: bool, detail: object = "") -> None:
    global PASS_COUNT
    if condition:
        PASS_COUNT += 1
        print(f"  PASS  {name}")
        return
    FAILURES.append(f"{name}: {detail}")
    print(f"  FAIL  {name}: {detail}")


def _db() -> AuditDatabase:
    tmp = Path(tempfile.mkdtemp(prefix="mta-u01-review-"))
    return AuditDatabase(tmp / "review.sqlite")


def _outcome(db: AuditDatabase, *, oid: str, symbol: str = "ZZZUSDT", pnl,
             confidence=0.9, invalidation=None, source="ai",
             opened_at="2000-01-01T00:00:00", closed_at="2100-01-01T00:00:00",
             holding_minutes=None, expected_holding_minutes=None) -> None:
    db.log_trade_outcome(
        outcome_id=oid, intent_id=None, decision_id=None, symbol=symbol, side="LONG",
        source=source, confidence=confidence, pnl=pnl, invalidation=invalidation,
        holding_minutes=holding_minutes,
        expected_holding_minutes=expected_holding_minutes,
        opened_at=opened_at, closed_at=closed_at,
    )


def _order(db: AuditDatabase, *, coid: str, symbol: str, side: str, qty: float,
           price: float, created_at: str, raw: dict) -> None:
    db.conn.execute(
        """INSERT INTO orders(client_order_id,symbol,side,order_type,qty,price,status,
           created_at,updated_at,raw_json) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (coid, symbol, side, "MARKET", qty, price, "FILLED", created_at, created_at,
         json.dumps(raw, ensure_ascii=False)),
    )
    db.conn.commit()


# --------------------------------------------------------------------------- #


def test_rv01_null_pnl_not_a_win() -> None:
    print("\n[RV01] pnl 为 NULL 不计赢")
    db = _db()
    _outcome(db, oid="O-NULL", pnl=None, confidence=0.9)
    stats = _build_outcome_stats(db, WIDE_START, WIDE_END)
    check("NULL 盈亏不计赢", stats["wins"] == 0, stats)
    check("NULL 盈亏不计亏", stats["losses"] == 0, stats)
    check("NULL 盈亏单独计数", stats["unknown_pnl"] == 1, stats)
    check("无可判样本时胜率为 None", stats["win_rate"] is None, stats["win_rate"])
    check("净盈亏不含未知样本", stats["total"] == 0.0, stats["total"])
    db.close()

    db = _db()
    _outcome(db, oid="O-WIN", pnl=1.0, confidence=0.9)
    _outcome(db, oid="O-NULL2", pnl=None, confidence=0.9)
    _outcome(db, oid="O-LOSS", pnl=-1.0, confidence=0.9)
    stats = _build_outcome_stats(db, WIDE_START, WIDE_END)
    check("混合样本：wins=1 losses=1 unknown=1",
          (stats["wins"], stats["losses"], stats["unknown_pnl"]) == (1, 1, 1), stats)
    check("胜率分母不含 NULL（1/2）", abs(stats["win_rate"] - 0.5) < 1e-9, stats["win_rate"])
    db.close()

    db = _db()
    for index in range(5):
        _outcome(db, oid=f"O-P{index}", pnl=None, confidence=0.9)
    calib = compute_calibration(db, source="ai")
    check("校准不把 NULL 样本计入桶", calib["n"] == 0, calib["n"])
    check("全 NULL 时整体样本不足", calib["small_sample"] is True, calib)
    db.close()


def test_rv02_invalidation_needs_trigger() -> None:
    print("\n[RV02] thesis 失效需真实触发事件，不靠 invalidation 文本")
    db = _db()
    # 有 invalidation 文本（预设证伪条件）但没有触发事件
    _outcome(db, oid="O-L1", pnl=-1.0, invalidation="15m 收盘跌破结构低点即证伪")
    stats = _build_outcome_stats(db, WIDE_START, WIDE_END)
    check("仅有 invalidation 文本不算证伪", stats["invalidated_losses"] == 0, stats)
    check("仅有文本的亏损单单独计数", stats["invalidation_text_only"] == 1, stats)

    # 写入真实证伪触发事件（Guardian 收盘确认）
    db.log_risk_event(
        event_type="guardian_invalidation", severity="critical",
        details="ZZZUSDT LONG: close=744.13, close_below 744.90",
        action_taken="create close intent",
        raw={"symbol": "ZZZUSDT", "state": "INVALIDATED",
             "reason_code": "THESIS_INVALIDATED"},
    )
    db.conn.commit()
    stats2 = _build_outcome_stats(db, WIDE_START, WIDE_END)
    check("有触发事件才算「行情判错」", stats2["invalidated_losses"] == 1, stats2)
    check("触发后不再计入纯文本计数", stats2["invalidation_text_only"] == 0, stats2)

    # 其他 symbol 的触发事件不得串到本仓位
    db2 = _db()
    _outcome(db2, oid="O-L2", symbol="YYYUSDT", pnl=-1.0, invalidation="x")
    db2.log_risk_event(
        event_type="guardian_invalidation", severity="critical", details="other",
        raw={"symbol": "ZZZUSDT", "state": "INVALIDATED",
             "reason_code": "THESIS_INVALIDATED"},
    )
    db2.conn.commit()
    stats3 = _build_outcome_stats(db2, WIDE_START, WIDE_END)
    check("跨 symbol 的触发事件不串用", stats3["invalidated_losses"] == 0, stats3)

    # reason_code 不是 THESIS_INVALIDATED 的 Guardian 事件不算证伪
    db2.log_risk_event(
        event_type="guardian_invalidation", severity="critical", details="review due",
        raw={"symbol": "YYYUSDT", "state": "REVIEW_DUE",
             "reason_code": "EVIDENCE_MISSING"},
    )
    db2.conn.commit()
    stats4 = _build_outcome_stats(db2, WIDE_START, WIDE_END)
    check("非 THESIS_INVALIDATED 的 Guardian 事件不算证伪",
          stats4["invalidated_losses"] == 0, stats4)
    db2.close()
    db.close()

    print("\n[RV02b] 触发事件的时间边界（纯函数）")
    triggers = [{"symbol": "ZZZUSDT", "at": "2026-01-01T12:00:00"}]
    check("生命周期内触发 -> True",
          invalidation_triggered(triggers, symbol="ZZZUSDT",
                                 opened_at="2026-01-01T10:00:00",
                                 closed_at="2026-01-01T14:00:00") is True)
    check("开仓前的触发 -> False",
          invalidation_triggered(triggers, symbol="ZZZUSDT",
                                 opened_at="2026-01-01T13:00:00",
                                 closed_at="2026-01-01T14:00:00") is False)
    check("平仓后的触发 -> False",
          invalidation_triggered(triggers, symbol="ZZZUSDT",
                                 opened_at="2026-01-01T10:00:00",
                                 closed_at="2026-01-01T11:00:00") is False)
    check("无触发事件 -> False", invalidation_triggered([], symbol="ZZZUSDT",
                                                       opened_at=None,
                                                       closed_at="2030-01-01T00:00:00") is False)
    check("触发查询路径可独立调用", callable(inspect_invalidation_triggers))


def test_rv03_calibration_direction() -> None:
    print("\n[RV03] 校准区分过度自信 / 信心不足 / 小样本")
    db = _db()
    # 高置信桶：conf 0.9 × 3 全亏 -> 胜率 0，中点 0.93 -> 过自信
    for index in range(3):
        _outcome(db, oid=f"O-HI{index}", pnl=-1.0, confidence=0.9)
    # 中置信桶：conf 0.5 × 3 全赢 -> 胜率 1.0，中点 0.475 -> 信心不足
    for index in range(3):
        _outcome(db, oid=f"O-MID{index}", pnl=1.0, confidence=0.5)
    # 小样本桶：conf 0.3 × 2 -> 样本不足
    for index in range(2):
        _outcome(db, oid=f"O-SM{index}", pnl=1.0, confidence=0.3)

    stats = compute_calibration(db, source="ai")
    high = next(b for b in stats["buckets"] if b["bucket"] == ">=0.85")
    mid = next(b for b in stats["buckets"] if b["bucket"] == "0.40-0.55")
    small = next(b for b in stats["buckets"] if b["bucket"] == "<0.40")

    check("过自信桶方向 = OVERCONFIDENT", high["direction"] == "OVERCONFIDENT", high)
    check("信心不足桶方向 = UNDERCONFIDENT", mid["direction"] == "UNDERCONFIDENT", mid)
    check("小样本桶方向 = INSUFFICIENT", small["direction"] == "INSUFFICIENT", small)
    check("小样本桶 calibrated 仍为 None（兼容字段）", small["calibrated"] is None, small)
    check("过自信桶 calibrated=False 兼容", high["calibrated"] is False, high)
    check("充足样本数 = 6", stats["sufficient_n"] == 6, stats["sufficient_n"])
    check("小样本不参与整体加权",
          abs(stats["miscalibration"] - (0.93 * 3 + 0.525 * 3) / 6) < 1e-6,
          stats["miscalibration"])
    check("整体方向 = OVERCONFIDENT", stats["direction"] == "OVERCONFIDENT", stats)
    check("整体 overconfident=True", stats["overconfident"] is True, stats)
    check("整体 underconfident=False", stats["underconfident"] is False, stats)
    check("有符号失真度为负（过自信）", stats["signed_miscalibration"] < 0,
          stats["signed_miscalibration"])
    check("小样本桶 n 与 wins/losses/flat 分离",
          small["n"] == 2 and small["wins"] == 2 and small["losses"] == 0, small)
    text = calibration_text(stats)
    check("报告文本标注过自信桶", "过自信" in text and "信心不足" in text, text)
    db.close()

    print("\n[RV03b] 全部小样本时不编造校准结论")
    db = _db()
    for index in range(2):
        _outcome(db, oid=f"O-S{index}", pnl=1.0, confidence=0.9)
    stats = compute_calibration(db, source="ai")
    check("small_sample=True", stats["small_sample"] is True, stats)
    check("miscalibration 为 None（不给伪精度）", stats["miscalibration"] is None, stats)
    check("direction = INSUFFICIENT", stats["direction"] == "INSUFFICIENT", stats)
    check("overconfident=False", stats["overconfident"] is False, stats)
    check("文本提示样本不足", "样本不足" in calibration_text(stats),
          calibration_text(stats))
    db.close()


def test_query_review_library_read_only() -> None:
    print("\n[RV] query_review_library 纯只读、无文件写入副作用")
    db = _db()
    _order(db, coid="C1", symbol="ZZZUSDT", side="BUY", qty=1.0, price=100.0,
           created_at="2026-01-01T00:00:00",
           raw={"action": "open", "leverage": 5})
    _order(db, coid="C2", symbol="ZZZUSDT", side="SELL", qty=1.0, price=99.0,
           created_at="2026-01-01T00:30:00",
           raw={"action": "close", "net_pnl": -0.5, "leverage": 5,
                "commission": -0.25})

    before = LESSONS_FILE.exists()
    writes: list[str] = []
    real_write_text = Path.write_text

    def _forbid(self, *args, **kwargs):
        writes.append(str(self))
        raise AssertionError(f"unexpected write to {self}")

    with mock.patch.object(Path, "write_text", _forbid):
        result = query_review_library(db, limit=8)
    check("无任何 write_text 调用", writes == [], writes)
    check("返回亏损案例", result["count"] >= 1, result)
    check("标注为只读结果", result.get("read_only") is True, result)
    check("查询前后文件存在性不变", LESSONS_FILE.exists() == before,
          (before, LESSONS_FILE.exists()))
    check("返回值不含 library_file 写入路径", "library_file" not in result, list(result))
    db.close()

    # 明确调用写入口时才写文件（临时重定向，不碰真实 knowledge 目录）
    db = _db()
    _order(db, coid="C3", symbol="ZZZUSDT", side="BUY", qty=1.0, price=100.0,
           created_at="2026-01-01T00:00:00", raw={"action": "open"})
    _order(db, coid="C4", symbol="ZZZUSDT", side="SELL", qty=1.0, price=99.0,
           created_at="2026-01-01T01:00:00",
           raw={"action": "close", "net_pnl": -0.5})
    tmp_file = Path(tempfile.mkdtemp(prefix="mta-lessons-")) / "review_lessons.json"
    with mock.patch("agents.lessons.LESSONS_FILE", tmp_file):
        from agents.lessons import build_review_library
        built = build_review_library(db, limit=8)
    check("build_review_library 才落盘", tmp_file.exists() and "library_file" in built,
          built.get("library_file"))
    db.close()


def main() -> int:
    test_rv01_null_pnl_not_a_win()
    test_rv02_invalidation_needs_trigger()
    test_rv03_calibration_direction()
    test_query_review_library_read_only()
    print(f"\nU01 review statistics: PASS={PASS_COUNT} FAIL={len(FAILURES)}")
    for item in FAILURES:
        print(f"  - {item}")
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
