"""只读探测当前USD-M环境的User Data Stream；不下单、不改仓位、不打印凭据。"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from core.config_loader import load_settings  # noqa: E402
from core.net_env import apply_proxy_env  # noqa: E402
from exchange.event_inbox import ExchangeEventInbox  # noqa: E402
from exchange.factory import BASE_URLS  # noqa: E402
from exchange.user_stream import BinanceUserDataStream  # noqa: E402


async def probe(timeout_seconds: float) -> int:
    applied_proxy = apply_proxy_env(ROOT / ".env")
    settings = load_settings(ROOT)
    execution = settings.raw_settings.get("execution", {}) or {}
    mode_env = str(execution.get("mode_env", "EXECUTION_MODE"))
    mode = (os.getenv(mode_env, "").strip().lower()
            or str(execution.get("default_mode", "demo")).strip().lower())
    if mode not in {"demo", "testnet", "live"}:
        print(f"SKIP environment={mode}: 私有流只适用于demo/testnet/live")
        return 2

    key_env = (execution.get("api_key_env_map", {}) or {}).get(mode) or f"BINANCE_{mode.upper()}_API_KEY"
    api_key = os.getenv(str(key_env), "").strip()
    if not api_key:
        print(f"FAIL environment={mode}: API Key未配置")
        return 2

    rest_base = ((execution.get("base_url_map", {}) or {}).get(mode)
                 or BASE_URLS[mode])
    db = AuditDatabase(Path(tempfile.mkdtemp(prefix="mta-stream-probe-")) / "probe.sqlite")
    stream = BinanceUserDataStream(
        api_key=api_key, rest_base_url=str(rest_base), environment=mode,
        inbox=ExchangeEventInbox(db), reconnect_seconds=1,
        # 没有显式项目代理时禁用Windows系统代理自动发现；本机曾残留一个
        # 已失效的注册表代理，trust_env=True会把直连探针误导向该端口。
        trust_env=bool(applied_proxy),
    )
    task = asyncio.create_task(stream.serve())
    deadline = time.monotonic() + max(1.0, timeout_seconds)
    try:
        while time.monotonic() < deadline:
            if stream.state == "live":
                print(f"PASS environment={mode} state=live observe_only=true")
                return 0
            if stream.state == "degraded":
                print(f"FAIL environment={mode} state=degraded error={stream.last_error}")
                return 1
            await asyncio.sleep(0.1)
        print(f"FAIL environment={mode} state={stream.state} error=timeout")
        return 1
    finally:
        stream.stop()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await stream.close()
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(description="探测USD-M私有账户流（只读）")
    parser.add_argument("--timeout", type=float, default=25.0)
    args = parser.parse_args()
    return asyncio.run(probe(args.timeout))


if __name__ == "__main__":
    raise SystemExit(main())
