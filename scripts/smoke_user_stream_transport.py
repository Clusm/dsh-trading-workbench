"""User Data Stream环境路由与安全默认值回归；不连接网络。"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

import httpx
import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from data import rate_limit  # noqa: E402
from exchange.event_inbox import ExchangeEventInbox  # noqa: E402
from exchange.user_stream import BinanceUserDataStream  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


db = AuditDatabase(Path(tempfile.mkdtemp()) / "transport.sqlite")
inbox = ExchangeEventInbox(db)


def build(environment: str, rest: str) -> BinanceUserDataStream:
    return BinanceUserDataStream(api_key="test-key", rest_base_url=rest,
                                 environment=environment, inbox=inbox)


print("\n[1] 三个执行环境使用隔离的私有流主机")
live = build("live", "https://fapi.binance.com")
demo = build("demo", "https://demo-fapi.binance.com")
testnet = build("testnet", "https://testnet.binancefuture.com")
check("live使用/private且限制事件", live.connection_url("secret").startswith(
    "wss://fstream.binance.com/private/ws?listenKey=secret&events="))
check("demo不混用生产主机", demo.connection_url("secret") ==
      "wss://fstream.binancefuture.com/ws/secret")
check("testnet不混用生产主机", testnet.connection_url("secret") ==
      "wss://stream.binancefuture.com/ws/secret")

print("\n[2] 仅生产 listenKey 响应计入共享出口权重")
rate_limit._reset_for_test()
live._observe_rest_response(httpx.Response(
    200, headers={"X-MBX-USED-WEIGHT-1M": "17"},
))
demo._observe_rest_response(httpx.Response(
    200, headers={"X-MBX-USED-WEIGHT-1M": "999"},
))
limit_status = rate_limit.rate_limit_status()
check("生产响应已观测", limit_status["requests"] == 1 and
      limit_status["observed_weight_1m"] == 17)

print("\n[3] 配置已进入仅观察期")
settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
cfg = settings["exchange_stream"]
check("随服务启动", cfg["enabled"] is True)
check("默认observe_only", cfg["observe_only"] is True)
check("提前30分钟续期", cfg["keepalive_minutes"] == 30)
check("事件队列有界", cfg["queue_size"] == 5000)

print("\n[4] 健康状态脱敏且队列溢出可见")
bounded = BinanceUserDataStream(
    api_key="must-not-leak", rest_base_url="https://demo-fapi.binance.com",
    environment="demo", inbox=inbox, queue_size=1,
)
bounded._enqueue_payload({"e": "ACCOUNT_UPDATE", "E": 1})
try:
    bounded._enqueue_payload({"e": "ACCOUNT_UPDATE", "E": 2})
except RuntimeError:
    pass
status = bounded.status()
check("队列严格限制容量", status["queue_depth"] == 1 and status["queue_capacity"] == 1)
check("溢出进入degraded并要求对账", status["state"] == "degraded" and
      status["reconcile_required"] is True and status["queue_overflows"] == 1)
check("状态不泄露API Key", "must-not-leak" not in str(status))

print("\n[5] 单消费者把事件串行写入幂等收件箱")
live._enqueue_payload({"e": "ACCOUNT_UPDATE", "E": 11, "T": 12, "a": {"P": []}})
live._enqueue_payload({"e": "listenKeyExpired", "E": 13, "listenKey": "hidden"})
live.stop()
asyncio.run(live._consume_inbox())
persisted = live.status()
check("事件已持久化且队列清空", persisted["events_inserted"] == 2 and
      persisted["queue_depth"] == 0)
check("记录DB写延迟", persisted["last_db_write_latency_ms"] is not None)
control = db.fetch_one(
    "SELECT status, payload_json FROM exchange_events WHERE event_type='listenKeyExpired'")
check("过期控制帧已处理且listenKey脱敏", control["status"] == "APPLIED" and
      "hidden" not in control["payload_json"])

asyncio.run(live.close())
asyncio.run(demo.close())
asyncio.run(testnet.close())
asyncio.run(bounded.close())
db.close()
print("\n结果: 14 通过 / 0 失败")
