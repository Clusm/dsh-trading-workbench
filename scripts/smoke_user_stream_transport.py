"""User Data Stream环境路由与安全默认值回归；不连接网络。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
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

print("\n[2] 配置默认关闭且仅观察")
settings = yaml.safe_load((ROOT / "config" / "settings.yaml").read_text(encoding="utf-8"))
cfg = settings["exchange_stream"]
check("默认不随服务启动", cfg["enabled"] is False)
check("默认observe_only", cfg["observe_only"] is True)
check("提前30分钟续期", cfg["keepalive_minutes"] == 30)

import asyncio
asyncio.run(live.close())
asyncio.run(demo.close())
asyncio.run(testnet.close())
db.close()
print("\n结果: 6 通过 / 0 失败")
