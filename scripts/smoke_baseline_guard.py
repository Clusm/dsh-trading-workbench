"""资金基线防污染回归测试。

覆盖 2026-09-05 的线上故障：paper 进程把 baseline 写成 0 并落盘，demo 进程
把 0 当合法基线继承，会话权益从此等于交易所全部余额（5028U 而非 532U）。

用法：.venv/Scripts/python.exe scripts/smoke_baseline_guard.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

ok = fail = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name}  {extra}")


from agents.execution import PaperBroker  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402
from core.capital import CapitalManager  # noqa: E402

EXCHANGE_EQ = 5028.5644  # demo 账户自带余额，与会话起始资金无关
INITIAL = 500.0


class FakeExchangeBroker:
    """模拟 demo/testnet broker：mode=demo，可注入权益与接口故障。"""

    def __init__(self, equity_value: float = EXCHANGE_EQ, fail: bool = False,
                 state_error: bool = False):
        self.mode = "demo"
        self._equity = equity_value
        self.fail = fail
        self.state_error = state_error
        self.closed = False

    def equity(self, prices=None, source=None):
        if self.fail:
            return 0.0
        return self._equity

    @property
    def state(self):
        # 接口整体不可用时，state 备用口径同样拿不到数据
        if self.state_error or self.fail:
            return {"cash": 0.0, "initial_equity": 0.0, "positions": [], "error": "boom"}
        return {"cash": self._equity, "initial_equity": self._equity,
                "positions": [], "wallet_balance": self._equity}

    def close_all(self, prices, db):
        self.closed = True

    def reset_capital(self, amount):
        pass


def new_db(tmp: Path, name: str) -> AuditDatabase:
    return AuditDatabase(tmp / f"{name}.sqlite")


def events(db: AuditDatabase, event_type: str) -> list[dict]:
    rows = db.fetch_all(
        "SELECT details FROM risk_events WHERE event_type=? ORDER BY id", (event_type,))
    return [dict(r) for r in rows]


tmp = Path(tempfile.mkdtemp())

print("\n[1] 干净启动：空状态文件 -> 基线 = 交易所权益 - 起始资金")
db = new_db(tmp, "t1")
broker = FakeExchangeBroker()
cap = CapitalManager(tmp / "cap1.json", broker=broker, db=db, initial_equity=INITIAL)
check("基线 = 5028.56 - 500", abs(cap.baseline - (EXCHANGE_EQ - INITIAL)) < 0.01, str(cap.baseline))
check("会话权益回到 500", abs(cap.session_equity() - INITIAL) < 0.01, str(cap.session_equity()))
check("基线已固化", (tmp / "cap1.json").exists())
saved = json.loads((tmp / "cap1.json").read_text(encoding="utf-8"))
check("状态文件记录 mode", saved.get("mode") == "demo", str(saved))
check("状态文件含锚点时间", bool(saved.get("anchored_at")), str(saved))

print("\n[2] 正确基线可复用：baseline=4496.43 对应会话权益 532.14")
db = new_db(tmp, "t2")
state2 = tmp / "cap2.json"
state2.write_text(json.dumps({"initial_equity": INITIAL, "baseline": 4496.4289,
                              "mode": "demo"}), encoding="utf-8")
cap2 = CapitalManager(state2, broker=FakeExchangeBroker(), db=db, initial_equity=INITIAL)
check("沿用既有基线", abs(cap2.baseline - 4496.4289) < 0.01, str(cap2.baseline))
check("会话权益 = 532.14", abs(cap2.session_equity() - 532.1355) < 0.01, str(cap2.session_equity()))
check("未误报基线失效", not events(db, "baseline_implausible"))

print("\n[3] 本次故障：baseline=0（paper 写入）被 demo 继承 -> 必须重锚")
db = new_db(tmp, "t3")
state3 = tmp / "cap3.json"
state3.write_text(json.dumps({"initial_equity": INITIAL, "baseline": 0.0,
                              "mode": "demo"}), encoding="utf-8")
cap3 = CapitalManager(state3, broker=FakeExchangeBroker(), db=db, initial_equity=INITIAL)
check("基线已重锚", abs(cap3.baseline - (EXCHANGE_EQ - INITIAL)) < 0.01, str(cap3.baseline))
check("会话权益回到 500 而非 5028", abs(cap3.session_equity() - INITIAL) < 0.01,
      str(cap3.session_equity()))
imp = events(db, "baseline_implausible")
check("已记录基线失效事件", len(imp) == 1, str(len(imp)))

print("\n[4] 跨执行模式隔离：paper 写入的基线绝不给 demo 用")
db = new_db(tmp, "t4")
state4 = tmp / "cap4.json"
state4.write_text(json.dumps({"initial_equity": INITIAL, "baseline": 0.0,
                              "mode": "paper"}), encoding="utf-8")
cap4 = CapitalManager(state4, broker=FakeExchangeBroker(), db=db, initial_equity=INITIAL)
check("mode 不匹配即重算", abs(cap4.baseline - (EXCHANGE_EQ - INITIAL)) < 0.01, str(cap4.baseline))
check("会话权益 = 500", abs(cap4.session_equity() - INITIAL) < 0.01, str(cap4.session_equity()))

print("\n[5] 起始资金被改（200 -> 500）：基线必须重算")
db = new_db(tmp, "t5")
state5 = tmp / "cap5.json"
state5.write_text(json.dumps({"initial_equity": 200.0, "baseline": 4828.5644,
                              "mode": "demo"}), encoding="utf-8")
cap5 = CapitalManager(state5, broker=FakeExchangeBroker(), db=db, initial_equity=INITIAL)
check("起始资金变更后重锚", abs(cap5.baseline - (EXCHANGE_EQ - INITIAL)) < 0.01, str(cap5.baseline))

print("\n[6] paper 模式：baseline=0 合法，且不影响 demo 基线")
db = new_db(tmp, "t6")
paper_broker = PaperBroker(tmp / "paper6.json", initial_equity=INITIAL)
cap6 = CapitalManager(tmp / "cap6.json", broker=paper_broker, db=db, initial_equity=INITIAL)
check("paper 基线为 0", cap6.baseline == 0.0, str(cap6.baseline))
check("paper 会话权益 = 500", abs(cap6.session_equity() - INITIAL) < 0.01, str(cap6.session_equity()))
saved6 = json.loads((tmp / "cap6.json").read_text(encoding="utf-8"))
check("paper 基线标记 mode=paper", saved6.get("mode") == "paper", str(saved6))
r6 = cap6.manual_reset({})
check("paper 重置成功", r6.get("ok") is True, str(r6))

print("\n[7] 权益接口不可用：拒绝重置，不落盘错误基线")
db = new_db(tmp, "t7")
state7 = tmp / "cap7.json"
state7.write_text(json.dumps({"initial_equity": INITIAL, "baseline": 4496.4289,
                              "mode": "demo"}), encoding="utf-8")
broker7 = FakeExchangeBroker(fail=True)
cap7 = CapitalManager(state7, broker=broker7, db=db, initial_equity=INITIAL)
r7 = cap7.manual_reset({})
check("权益不可用拒绝重置", r7.get("ok") is False, str(r7))
check("未平仓", broker7.closed is False)
after7 = json.loads(state7.read_text(encoding="utf-8"))
check("基线未被写坏", abs(float(after7["baseline"]) - 4496.4289) < 0.01, str(after7))

print("\n[8] 权益不可用时构造：不固化猜测基线")
db = new_db(tmp, "t8")
state8 = tmp / "cap8.json"
cap8 = CapitalManager(state8, broker=FakeExchangeBroker(fail=True), db=db, initial_equity=INITIAL)
check("未落盘错误基线", not state8.exists(), "状态文件不应被创建")
check("会话权益退回起始资金", abs(cap8.session_equity() - INITIAL) < 0.01, str(cap8.session_equity()))

print("\n[9] paper 账本配置值不得被当成交易所权益")
db = new_db(tmp, "t9")
state9 = tmp / "cap9.json"
state9.write_text(json.dumps({"initial_equity": INITIAL, "baseline": 4496.4289,
                              "mode": "demo"}), encoding="utf-8")


class StateOnlyBroker(FakeExchangeBroker):
    """equity() 接口挂掉，但 state 里有真实权益（备用口径）。"""

    def equity(self, prices=None, source=None):
        return 0.0


cap9 = CapitalManager(state9, broker=StateOnlyBroker(), db=db, initial_equity=INITIAL)
check("走 state 备用口径拿到真实权益", abs(cap9.baseline - 4496.4289) < 0.01, str(cap9.baseline))
check("会话权益 = 532.14", abs(cap9.session_equity() - 532.1355) < 0.01, str(cap9.session_equity()))

print("\n[10] 执行模式白名单：未知模式拒绝启动")
import os  # noqa: E402

from core.config_loader import load_settings  # noqa: E402
from exchange.factory import VALID_MODES, create_broker  # noqa: E402

settings = load_settings(ROOT)
dbf = new_db(tmp, "t10")
old_mode = os.environ.get("EXECUTION_MODE")
os.environ["EXECUTION_MODE"] = "mainnet"
try:
    create_broker(settings, dbf)
    check("未知模式抛异常", False, "应当拒绝启动")
except RuntimeError as e:
    check("未知模式抛异常", "未知 EXECUTION_MODE" in str(e), str(e))
except Exception as e:
    check("未知模式抛异常", False, f"{type(e).__name__}: {e}")
check("已记录拒绝事件", len(events(dbf, "broker_unknown_mode")) == 1)

os.environ["EXECUTION_MODE"] = "paper"
dbf2 = new_db(tmp, "t10b")
b = create_broker(settings, dbf2)
check("paper 显式声明可用", getattr(b, "mode", "") == "paper", str(getattr(b, "mode", "")))
check("paper 不再记为降级", not events(dbf2, "broker_fallback_paper"))
if old_mode is None:
    os.environ.pop("EXECUTION_MODE", None)
else:
    os.environ["EXECUTION_MODE"] = old_mode

print("\n[11] 白名单完整")
check("四种合法模式", set(VALID_MODES) == {"paper", "demo", "testnet", "live"}, str(VALID_MODES))

print("\n[12] 快照守卫：基线重锚造成的跳变必须放行")
from unittest.mock import MagicMock  # noqa: E402

from dashboard.app import _baseline_just_reanchored  # noqa: E402

svc = MagicMock()
svc.settings.raw_settings = {"capital": {"state_file": "data/capital_state.json"}}
svc.settings.project_root = tmp
(tmp / "data").mkdir(exist_ok=True)
(tmp / "data" / "capital_state.json").write_text(
    json.dumps({"anchored_at": "2026-09-05T05:13:29+00:00"}), encoding="utf-8")
svc.db.fetch_one.return_value = {"created_at": "2026-09-04T12:00:00+00:00"}
check("重锚晚于最后快照 -> 放行跳变", _baseline_just_reanchored(svc) is True)
svc.db.fetch_one.return_value = {"created_at": "2026-09-05T06:00:00+00:00"}
check("快照晚于重锚 -> 不再豁免", _baseline_just_reanchored(svc) is False)
svc.db.fetch_one.return_value = None
check("无历史快照 -> 视为刚重锚", _baseline_just_reanchored(svc) is True)
(tmp / "data" / "capital_state.json").write_text(json.dumps({}), encoding="utf-8")
check("状态文件无锚点时间 -> 不豁免", _baseline_just_reanchored(svc) is False)

print(f"\n{'=' * 46}\n通过 {ok} / 失败 {fail}\n{'=' * 46}")
sys.exit(1 if fail else 0)
