"""私有流在Web生命周期中的只观察接入回归；全程使用假流，不连接网络。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from core.config_loader import Settings  # noqa: E402
from core.entry_readiness import evaluate_entry_readiness  # noqa: E402
from core.trading_scope import TradingScope  # noqa: E402
from data import rate_limit  # noqa: E402
import dashboard.app as dashboard_app  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


class FakePriceStream:
    def __init__(self, _provider, **_kwargs):
        self.active = False
        self._stop = False

    async def serve(self):
        while not self._stop:
            await asyncio.sleep(0.01)

    def stop(self):
        self._stop = True

    def status_text(self):
        return "测试行情流"


class FakeUserStream:
    instances: list["FakeUserStream"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.state = "disabled"
        self.closed = False
        self.stopped = False
        self.__class__.instances.append(self)

    async def serve(self):
        self.state = "live"
        while not self.stopped:
            await asyncio.sleep(0.01)

    def stop(self):
        self.stopped = True

    async def close(self):
        self.closed = True

    def status(self):
        return {
            "enabled": True, "observe_only": True, "environment": "demo",
            "state": self.state, "active": self.state == "live",
            "last_error": None, "events_received": 0, "events_inserted": 0,
        }


def test_settings(root: Path, *, observe_only: bool) -> Settings:
    """手工构造测试配置。

    刻意不调用 ``load_settings(ROOT)``：那会读取仓库真实 ``.env``/``.env.local``
    并把机器级凭据写进进程环境。这里只给本用例需要的最小配置，所有路径都落在
    调用方给的临时目录内。
    """
    return Settings(
        project_root=root,
        raw_settings={
            "stage": "test",
            "paths": {
                "data_dir": "data", "db_file": "data/test.sqlite",
                "artifacts_dir": "data/artifacts", "logs_dir": "data/logs",
                "reviews_dir": "data/reviews", "knowledge_dir": "knowledge",
            },
            "market_universe": {"core_symbols": ["BTCUSDT"]},
            "exchange_stream": {
                "enabled": True, "observe_only": observe_only,
                "keepalive_minutes": 30, "planned_reconnect_hours": 23,
                "reconnect_seconds": 5, "queue_size": 5000,
            },
            "account_mirror": {
                "enabled": False, "shadow_only": True, "rest_reconcile_seconds": 60,
            },
            "order_mirror": {
                "enabled": False, "shadow_only": True,
            },
            "exchange_safety_monitor": {
                "enabled": False, "observe_only": True,
            },
        },
        raw_agents={},
        raw_risk={},
    )


class FakeService:
    def __init__(self, *, observe_only: bool = True):
        root = Path(tempfile.mkdtemp(prefix="mta-user-stream-"))
        self.settings = test_settings(root, observe_only=observe_only)
        self.settings.ensure_dirs()
        self.trading_scope = TradingScope.from_symbols(["BTCUSDT"])
        self.db = AuditDatabase(self.settings.db_file)
        self.broker = SimpleNamespace(
            api_key="must-not-leak", base_url="https://demo-fapi.binance.com",
            environment="demo", mode="demo", _trust_env=False,
            state={"equity": 500.0, "positions": []},
        )
        self.control = SimpleNamespace(state="normal")
        self.capital = SimpleNamespace(initial_equity=500.0)
        self.quota = SimpleNamespace(status=lambda: {})
        self._startup_safety = {"ready": True, "reason": "", "checked_at": "test"}

    def latest(self):
        return {
            "startup_safety": self._startup_safety,
            "account_risk": {"allow_new": True, "mode": "audit_only"},
            "capital_state": {"initial_equity": 500.0, "session_equity": 500.0},
            "channel_state": {"strategy": {}, "ai": {}}, "ts": time.time(),
        }

    def cycle_running(self):
        return False

    def entry_readiness(self):
        """复用真实开仓闸门判定，只把事实换成进程内的假账户。

        看板 /api/ready 的最终原因码完全取自这个快照，所以这里必须走真正的
        ``evaluate_entry_readiness``，否则断言的是替身而不是闸门。
        """
        market_rate = rate_limit.rate_limit_status()
        window = market_rate.get("data_endpoint_window") or {}
        return evaluate_entry_readiness(
            {
                "service_accepting": True,
                "control_state": self.control.state,
                "equity_available": True,
                "account_risk_allow_new": True,
                "startup_safety_ready": bool(self._startup_safety.get("ready")),
                "order_submission_ready": True,
                "market_cooldown_active": bool(market_rate.get("active")),
                "data_budget_available": (
                    int(window.get("requests", 0) or 0) < int(window.get("budget", 1) or 1)),
                "rate_coordination_available": (
                    market_rate.get("coordination_available") is True),
                # demo 影子镜像全部关闭：没有未证实的镜像事实，也没有未知订单。
                "order_mirror_ok": True,
                "protection_mirror_ok": True,
                "unknown_orders": 0,
                "submitting_orders": 0,
                "persistence_available": True,
                "scope_hash": self.trading_scope.scope_hash,
            },
            scope=self.trading_scope,
            runtime_instance_id="smoke-user-stream",
            environment="demo",
        ).as_dict()


dashboard_app.PriceStream = FakePriceStream
dashboard_app.BinanceUserDataStream = FakeUserStream

print("\n[1] observe_only私有流随Web生命周期启动和停止")
service = FakeService()
check("测试配置指向临时目录而非仓库根", service.settings.project_root != ROOT)
check("测试数据库落在临时目录内",
      str(service.settings.db_file).startswith(str(service.settings.project_root)))
app = dashboard_app.create_app(service)
with TestClient(app) as client:
    for _ in range(20):
        health = client.get("/api/health").json()
        if health["exchange_stream"]["active"]:
            break
        time.sleep(0.01)
    check("健康接口报告只观察流在线", health["exchange_stream"]["active"] is True)
    check("流异常不改变主健康口径", health["ok"] is True)
    encoded = str(health)
    check("健康信息不泄露API Key", "must-not-leak" not in encoded)
    ready = client.get("/api/ready")
    check("全部现有开仓前置条件满足时ready=200", ready.status_code == 200 and
          ready.json()["ready"] is True)
    rate_limit._reset_for_test()
    rate_limit.note_418(30)
    cooldown_ready = client.get("/api/ready")
    check("公共行情418冷却时ready禁止新开仓",
          cooldown_ready.status_code == 503 and
          "MARKET_COOLDOWN" in cooldown_ready.json()["reasons"])
    check("冷却只影响新开仓门，不影响健康接口",
          client.get("/api/health").json()["ok"] is True)
    rate_limit._reset_for_test()
    service.control.state = "close_only"
    blocked_ready = client.get("/api/ready")
    check("控制状态非normal时ready=503", blocked_ready.status_code == 503 and
          "CONTROL_NOT_NORMAL" in blocked_ready.json()["reasons"])
instance = FakeUserStream.instances[-1]
check("有界队列配置传入流", instance.kwargs["queue_size"] == 5000)
check("生命周期停止流", instance.stopped is True)
check("生命周期关闭客户端", instance.closed is True)
service.db.close()

print("\n[2] 非observe_only配置被硬阻断")
blocked_service = FakeService(observe_only=False)
blocked_app = dashboard_app.create_app(blocked_service)
with TestClient(blocked_app) as client:
    blocked = client.get("/api/health").json()["exchange_stream"]
    check("执行型私有流不启动", blocked["state"] == "blocked")
    check("阻断原因明确", blocked["reason"] == "observe_only_required")
blocked_service.db.close()

print("\n结果: 14 通过 / 0 失败")
