"""跨进程公共行情预算回归；只操作临时状态文件，不连接币安。"""
from __future__ import annotations

import multiprocessing
import sys
import tempfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _worker(state_file: str, gate: Any, output: Any) -> None:
    """两个独立 Python 进程竞争同一个五分钟预算槽位。"""
    from data.rate_limit import (
        PublicMarketBudgetDeferred,
        configure_rate_limit_budget,
        configure_rate_limit_state,
        reserve_request,
    )

    configure_rate_limit_budget(
        weight_limit_1m=2400,
        local_budget_1m=600,
        data_window_seconds=300,
        data_request_budget_5m=1,
    )
    configure_rate_limit_state(Path(state_file))
    gate.wait(timeout=10)
    try:
        reserve_request(
            "/futures/data/topLongShortAccountRatio",
            weight=1,
            priority="background",
            endpoint_group="futures_data_5m",
            source="multiprocess_smoke",
            environment="demo",
        )
        output.put("accepted")
    except PublicMarketBudgetDeferred:
        output.put("deferred")
    except Exception as exc:  # pragma: no cover - failure is reported to parent
        output.put(f"error:{type(exc).__name__}")


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


def main() -> int:
    ctx = multiprocessing.get_context("spawn")
    with tempfile.TemporaryDirectory(prefix="mta-rate-limit-mp-") as temp_dir:
        state_file = str(Path(temp_dir) / "public_rate_limit_state.json")
        gate = ctx.Barrier(2)
        output = ctx.Queue()
        workers = [ctx.Process(target=_worker, args=(state_file, gate, output))
                   for _ in range(2)]
        for worker in workers:
            worker.start()
        results = [output.get(timeout=20) for _ in workers]
        for worker in workers:
            worker.join(timeout=10)

        check("两个进程均正常退出", all(worker.exitcode == 0 for worker in workers))
        check("共享一个预算槽位", sorted(results) == ["accepted", "deferred"])

        payload = Path(state_file).read_text(encoding="utf-8")
        check("请求遥测已持久化", '"group": "futures_data_5m"' in payload)

    print("\n结果: 3 通过 / 0 失败")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
