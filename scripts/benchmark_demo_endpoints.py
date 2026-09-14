"""只读测量本地控制台与可选 Binance USD-M 公共接口延迟。

这是显式在线诊断脚本，不进入默认离线回归。它不读取密钥、不调用签名接口，也不修改
订单或状态；用于把“页面感觉慢”变成可比较的 P50/P95/最大值和失败率证据。
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from data.binance_transport import request_json
from data.rate_limit import configure_rate_limit_state


def percentile(values: list[float], quantile: float) -> float | None:
    """使用线性插值计算百分位；空样本返回 None。"""
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def measure(client: httpx.Client, url: str, samples: int) -> dict[str, Any]:
    """顺序 GET，避免压测本身改变服务并发状态。"""
    latencies: list[float] = []
    errors: list[str] = []
    for _ in range(samples):
        started = time.perf_counter()
        try:
            response = client.get(url)
            response.raise_for_status()
            # 强制读取完整响应体，测量包含 JSON 下载而非仅响应头。
            _ = response.content
            latencies.append((time.perf_counter() - started) * 1000.0)
        except Exception as exc:
            errors.append(type(exc).__name__)
    return {
        "url": url,
        "samples_requested": samples,
        "samples_ok": len(latencies),
        "failures": len(errors),
        "error_types": sorted(set(errors)),
        "p50_ms": round(percentile(latencies, 0.50) or 0.0, 3) if latencies else None,
        "p95_ms": round(percentile(latencies, 0.95) or 0.0, 3) if latencies else None,
        "max_ms": round(max(latencies), 3) if latencies else None,
    }


def measure_binance_public(client: httpx.Client, samples: int) -> dict[str, Any]:
    """通过统一网关测量公共接口，避免诊断工具绕过全局418熔断。"""
    url = "https://fapi.binance.com/fapi/v1/time"
    latencies: list[float] = []
    errors: list[str] = []
    for _ in range(samples):
        started = time.perf_counter()
        try:
            request_json(client, "/fapi/v1/time", attempts=1, priority="background")
            latencies.append((time.perf_counter() - started) * 1000.0)
        except Exception as exc:
            errors.append(type(exc).__name__)
    return {
        "url": url,
        "transport": "governed_public_gateway",
        "samples_requested": samples,
        "samples_ok": len(latencies),
        "failures": len(errors),
        "error_types": sorted(set(errors)),
        "p50_ms": round(percentile(latencies, 0.50) or 0.0, 3) if latencies else None,
        "p95_ms": round(percentile(latencies, 0.95) or 0.0, 3) if latencies else None,
        "max_ms": round(max(latencies), 3) if latencies else None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="只读测量 Demo 控制台接口 P95")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--samples", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--include-binance-public", action="store_true")
    args = parser.parse_args()
    if args.samples < 2 or args.samples > 200:
        parser.error("--samples 必须在 2 到 200 之间")

    base = str(args.base_url).rstrip("/")
    # 独立维护进程也读取服务的跨重启冷却文件，不能成为第二个无状态出口。
    configure_rate_limit_state(ROOT / "data" / "public_rate_limit_state.json")
    urls = [f"{base}/api/health", f"{base}/api/status", f"{base}/api/intel"]
    with httpx.Client(timeout=args.timeout, follow_redirects=False) as client:
        results = [measure(client, url, args.samples) for url in urls]
        if args.include_binance_public:
            results.append(measure_binance_public(client, args.samples))
    report = {
        "read_only": True,
        "samples_per_endpoint": args.samples,
        "results": results,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(item["failures"] == 0 for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
