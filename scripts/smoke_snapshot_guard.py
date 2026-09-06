"""验证权益快照的脏读拦截闸门（不联网：mock 掉数据库）。

背景：交易所偶发脏读把余额读成几千 U（实测 4998.05 / 999.58，而起始资金只有 500 U），
一条脏快照就会永久毁掉「最高 / 最低 / 最大回撤」这类区间统计，只能事后人工清理。
现在改为写入前按「相对上一条快照的突变幅度」拦截。

覆盖点：
1. 正常权益照常写入
2. 单次脏读（4998.05）被拦下，且不污染历史
3. 连续脏读全部被拦（脏值始终与「最后一条有效快照」比较，不会自我采信）
4. 脏读结束后恢复正常写入
5. 首次运行无历史快照时不拦截（没有比较基准）
6. 被拦时会留下 snapshot_rejected 事件，且该事件做了节流不会刷屏
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from dashboard.app import _maybe_snapshot  # noqa: E402

PASS, FAIL = 0, 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name} {extra}")


class FakeDB:
    """只实现 _maybe_snapshot 用到的两个方法。"""

    def __init__(self) -> None:
        self.rows: list[float] = []
        self.events: list[str] = []

    def fetch_one(self, sql, params=()):
        # 返回最后一条已写入快照的 equity，模拟 ORDER BY id DESC LIMIT 1
        return (self.rows[-1],) if self.rows else None

    def log_portfolio_snapshot(self, **kw) -> None:
        self.rows.append(float(kw["equity"]))

    def log_risk_event(self, *, event_type, severity, details, action_taken) -> None:
        self.events.append(event_type)


class FakeCapital:
    initial_equity = 500.0


class FakeService:
    def __init__(self) -> None:
        self.db = FakeDB()
        self.capital = FakeCapital()


def snap(service, equity) -> None:
    """绕过 60 秒节流，强制走一次判定。"""
    import dashboard.app as app

    app._last_snapshot_mono = 0.0
    _maybe_snapshot(service, equity, 0.0, [])


def main() -> int:
    global PASS, FAIL
    print(f"\n{'=' * 68}\n权益快照脏读拦截\n{'=' * 68}")

    print("\n[1] 正常权益照常写入")
    s = FakeService()
    snap(s, 500.0)
    check("500.00 被记录", s.db.rows == [500.0], str(s.db.rows))
    snap(s, 501.2)
    snap(s, 499.4)
    check("小幅波动全部记录", s.db.rows == [500.0, 501.2, 499.4], str(s.db.rows))
    check("无 risk_event", not s.db.events, str(s.db.events))

    print("\n[2] 单次脏读被拦下（历史实测值 4998.05）")
    s = FakeService()
    snap(s, 500.0)
    snap(s, 4998.05)
    check("脏值未写入", s.db.rows == [500.0], str(s.db.rows))
    check("留下 snapshot_rejected 事件", "snapshot_rejected" in s.db.events, str(s.db.events))

    print("\n[3] 连续脏读全部被拦（不与脏值自我采信）")
    s = FakeService()
    snap(s, 500.0)
    for _ in range(4):
        snap(s, 4998.05)
    check("4 次脏读全部拦下", s.db.rows == [500.0], str(s.db.rows))

    print("\n[4] 脏读结束后恢复正常")
    s = FakeService()
    snap(s, 500.0)
    snap(s, 4998.05)
    snap(s, 500.3)
    check("恢复后正常写入", s.db.rows == [500.0, 500.3], str(s.db.rows))

    print("\n[5] 首次运行无历史快照时不拦截")
    s = FakeService()
    snap(s, 500.0)
    check("首个快照被记录", s.db.rows == [500.0], str(s.db.rows))

    print("\n[6] 单点尖峰（999.58）被拦下")
    s = FakeService()
    snap(s, 499.97)
    snap(s, 999.58)
    check("尖峰未写入", s.db.rows == [499.97], str(s.db.rows))

    print("\n[7] 告警事件有节流，不会每周期刷屏")
    s = FakeService()
    snap(s, 500.0)
    import dashboard.app as app

    app._last_bad_snapshot_log_mono = 0.0
    for i in range(5):
        app._last_snapshot_mono = 0.0
        # 每次都造一个不同的脏值，确保都触发判定
        _maybe_snapshot(s, 500.0 * (2.0 + i), 0.0, [])
    check("5 次脏读只记 1 次事件", s.db.events.count("snapshot_rejected") == 1,
          str(s.db.events))

    print("\n[8] 边界：恰好 60% 跳变放行，略超则拦")
    s = FakeService()
    snap(s, 500.0)
    snap(s, 799.0)  # +59.8%
    check("59.8% 跳变放行", len(s.db.rows) == 2, str(s.db.rows))
    s2 = FakeService()
    snap(s2, 500.0)
    snap(s2, 805.0)  # +61%
    check("61% 跳变拦截", s2.db.rows == [500.0], str(s2.db.rows))

    print(f"\n{'=' * 68}")
    print(f"结果：PASS {PASS} / FAIL {FAIL}")
    print("=" * 68)
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
