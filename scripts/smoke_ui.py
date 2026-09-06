"""UI 轮冒烟测试：模式切换 / 手动重置 / 自动复盘 / 事件页 / 中文化。"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta, timezone
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


print("\n[1] manual_reset：测试模式可用，live 拒绝")
from agents.execution import PaperBroker  # noqa: E402
from agents.models import TradeDecision, TradeIntent, RiskVerdict  # noqa: E402
from audit.database import AuditDatabase  # noqa: E402
from core.capital import CapitalManager  # noqa: E402

tmp = Path(tempfile.mkdtemp())
db = AuditDatabase(tmp / "t.sqlite")
broker = PaperBroker(tmp / "b.json", initial_equity=500.0)
cap = CapitalManager(tmp / "cap.json", broker=broker, db=db, initial_equity=500.0)
rc = {"max_leverage": 20, "max_margin_usage": 0.9, "channel_max_margin_usage": 0.9,
      "max_position_notional": 100000, "max_open_positions": 8, "max_risk_per_trade": 1.0,
      "supported_symbols": ["BTCUSDT", "ETHUSDT"]}
oi = TradeIntent(symbol="BTCUSDT", action="open", side="LONG", qty=0.001, leverage=5,
                 stop_loss=59000.0, take_profit=62000.0, invalidation="x")
broker.execute(TradeDecision(decision="open", intents=[oi]),
               RiskVerdict(verdict="approved", adjusted_intents=[oi]), {"BTCUSDT": 60000.0}, rc, db)
check("开仓后持仓非空", len(broker.state["positions"]) == 1)
r = cap.manual_reset({"BTCUSDT": 61000.0})
check("重置成功", r.get("ok") is True and r.get("session_equity") == 500.0, str(r))
check("仓位已清空", len(broker.state["positions"]) == 0)
check("会话权益回到 500", cap.session_equity() == 500.0, str(cap.session_equity()))
events = [dict(e) for e in db.fetch_all("SELECT event_type FROM risk_events WHERE event_type='manual_capital_reset'")]
check("重置事件落库", len(events) == 1)

class FakeLiveBroker(PaperBroker):
    @property
    def mode(self):
        return "live"

live_cap = CapitalManager(tmp / "cap2.json", broker=FakeLiveBroker(tmp / "b2.json", initial_equity=500.0),
                          db=db, initial_equity=500.0)
r2 = live_cap.manual_reset({})
check("live 模式拒绝重置", r2.get("ok") is False and "实盘" in r2.get("error", ""), str(r2))
rej = [dict(e) for e in db.fetch_all("SELECT event_type FROM risk_events WHERE event_type='manual_reset_rejected'")]
check("拒绝事件落库", len(rej) == 1)

print("\n[2] 自动复盘：到点触发 / 未到点跳过 / 幂等 / 补缺口")
from unittest.mock import MagicMock  # noqa: E402
from core.service import TradingService  # noqa: E402


def make_fake_service(review_cfg: dict):
    s = MagicMock()
    s.settings = MagicMock()
    s.settings.raw_settings = {"review": review_cfg}
    s.settings.reviews_dir = tmp / "reviews"
    s.settings.project_root = ROOT
    s.db = AuditDatabase(tmp / f"svc_{abs(id(review_cfg))}_{review_cfg.get('time_utc', 'x').replace(':', '')}.sqlite")
    s.logger = None
    s._maybe_auto_review = TradingService._maybe_auto_review.__get__(s)
    return s


svc = make_fake_service({"auto": True, "time_utc": "00:05", "use_llm": True})
now = datetime.now(timezone.utc)
now_after = now.replace(hour=0, minute=10)  # 00:10 > 00:05
gen = svc._maybe_auto_review(now_after)
check("到点触发生成昨日复盘", len(gen) >= 1, str(gen))
report = tmp / "reviews" / f"{(now_after - timedelta(days=1)).strftime('%Y-%m-%d')}.md"
check("报告文件已生成", report.exists())
gen2 = svc._maybe_auto_review(now_after)
check("幂等：第二次不重复生成", gen2 == [], str(gen2))

now_before = now.replace(hour=0, minute=1)  # 00:01 < 00:05
gen3 = svc._maybe_auto_review(now_before)
check("未到点直接返回空", gen3 == [])

svc_off = make_fake_service({"auto": False})
check("auto=false 不生成", svc_off._maybe_auto_review(now_after) == [])

print("\n[3] .env 模式写入")
from dashboard.app import _read_env_file, _set_env_mode  # noqa: E402

env = tmp / ".env"
env.write_text(
    "# 注释保留\nOPENCODE_API_KEY=abc\nEXECUTION_MODE=demo\n\nBINANCE_DEMO_API_KEY=k\n",
    encoding="utf-8")
_set_env_mode(env, "live")
text = env.read_text(encoding="utf-8")
check("EXECUTION_MODE 已改写", "EXECUTION_MODE=live" in text)
check("其它行保留", "OPENCODE_API_KEY=abc" in text and "# 注释保留" in text)
check("读取正确", _read_env_file(env).get("EXECUTION_MODE") == "live")
env2 = tmp / ".env2"
env2.write_text("OPENCODE_API_KEY=abc\n", encoding="utf-8")
_set_env_mode(env2, "testnet")
check("缺失时追加写入", "EXECUTION_MODE=testnet" in env2.read_text(encoding="utf-8"))

print("\n[4] 事件中文化映射完整")
from dashboard.app import _event_type_cn, _ago, _mode_cn, _price  # noqa: E402
import re  # noqa: E402

types: set[str] = set()
for py in (ROOT / "src").rglob("*.py"):
    if ".venv" in str(py):
        continue
    try:
        types.update(re.findall(r'event_type="([a-z_]+)"', py.read_text(encoding="utf-8")))
    except Exception:
        pass
check(f"扫描到 {len(types)} 个事件类型", len(types) >= 40, str(len(types)))
missing = sorted(t for t in types if _event_type_cn(t) == t)
check("全部事件类型都有中文映射", not missing, f"缺映射：{missing}")

check("_ago 中文化", "前" in _ago(datetime.now(timezone.utc).isoformat()))
check("live 模式标注", _mode_cn("live") == "实盘")
check("价格保留五位有效数字", _price(744.53325581) == "744.53")
check("价格删除无意义尾零", _price("38.00000000") == "38")
check("极小价格不使用科学计数法", _price(0.000012345678) == "0.000012346")
check("高价价格按五位有效数字舍入", _price(118234.5) == "118230")

print("\n[5] 事件页路由（TestClient）")
from fastapi.testclient import TestClient  # noqa: E402
from core.config_loader import load_settings  # noqa: E402
import os  # noqa: E402
os.environ["EXECUTION_MODE"] = "paper"
settings = load_settings(ROOT)
# 隔离状态文件：测试服务不得读写真实 data/ 下的 capital/channel/control
# 状态（曾发生 paper 测试把 baseline=0 覆写进真实 capital_state.json）。
# db/logs/artifacts 均由 project_root 派生，指向临时目录即可整体隔离。
iso_root = tmp / "iso_root"
(iso_root / "data").mkdir(parents=True, exist_ok=True)
settings.project_root = iso_root
real_svc = TradingService(settings)
from dashboard.app import create_app  # noqa: E402
app = create_app(real_svc)
client = TestClient(app)
# 写接口帶上服务端下发的 console token（CSRF 防护，P0-3）
token = str(getattr(app.state, "console_token", ""))
token_headers = {"X-Console-Token": token, "Content-Type": "application/json"}
r_home = client.get("/")
check("首页 200", r_home.status_code == 200)
check("首页含模式切换器", "mode-select" in r_home.text)
check("paper 模式显示重置按钮", "重置资金" in r_home.text)
check("首页不含旧文案", "亏完重启" not in r_home.text)
db2 = real_svc.db
db2.log_risk_event(event_type="manual_capital_reset", severity="warn",
                   details="手动重置：平掉全部仓位，资金回到 500U", action_taken="close_all + reset")
db2.log_risk_event(event_type="decision_downgraded", severity="warn", details="测试", action_taken="no_trade")
r_events = client.get("/events")
check("事件页 200", r_events.status_code == 200)
check("事件页中文显示类型", "手动重置资金" in r_events.text)
check("事件页显示处理列", "处理" in r_events.text)
r_filter = client.get("/events?severity=warn")
check("级别过滤生效", r_filter.status_code == 200 and "手动重置资金" in r_filter.text)
r_api = client.get("/api/mode")
check("/api/mode 返回当前模式", r_api.json().get("current") == "paper", r_api.text[:120])
r_noauth = client.post("/api/capital/reset", headers={"Content-Type": "application/json"})
check("写接口无 token 被拒(403)", r_noauth.status_code == 403, f"got {r_noauth.status_code}")
r_csrf = client.post("/api/capital/reset", headers={"X-Console-Token": token, "Content-Type": "text/plain"})
check("text/plain CSRF 被拒(415)", r_csrf.status_code == 415, f"got {r_csrf.status_code}")
r_ctrl = client.post("/api/control", json={"state": "emergency"}, headers=token_headers)
check("emergency 无 confirm 被拒(400)", r_ctrl.status_code == 400, r_ctrl.text[:120])
r_reset = client.post("/api/capital/reset", headers=token_headers)
check("重置接口 paper 可用", r_reset.status_code == 200 and r_reset.json().get("ok"), r_reset.text[:150])
real_svc.close()

print(f"\n{'=' * 46}\n通过 {ok} / 失败 {fail}\n{'=' * 46}")
sys.exit(1 if fail else 0)
