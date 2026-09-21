"""独立健康观察进程 CLI 入口（O-01）。

只做只读检查，不持交易私钥，不写任何状态文件。可单次运行或按间隔循环运行，
每次输出机器可读 JSON 到标准输出；退出码区分：

  * 0  健康（且无异常，且告警通道已验证）
  * 1  异常（发现至少一类健康问题）
  * 2  未验证（健康，但外部告警通道未配置/未验证 —— 无人值守告警尚未完成）

用法示例：
  .venv/Scripts/python.exe scripts/health_watch.py --once
  .venv/Scripts/python.exe scripts/health_watch.py --interval-seconds 30 --count 10
  .venv/Scripts/python.exe scripts/health_watch.py --profile live --state-root D:/state --once

U14（F06/F12/F22）：``--state-root`` 是状态根的显式注入点（与
``MTA_STATE_ROOT`` / ``paths.state_root`` 同一口径，均由
``core.runtime_paths.resolve_state_base`` 判定），换代码目录部署时用它可以
盯住**同一份**状态。输出 JSON 追加 ``trading_evidence_at`` / ``outage_kind`` /
``state_root_source`` 等字段；退出码语义不变（0 健康且已验证 / 1 发现异常 /
2 健康但外部告警通道未验证）。
"""
from __future__ import annotations

import argparse
import inspect
import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from core.health_watch import (  # noqa: E402
    HealthWatchConfig,
    evaluate_health,
)


def _load_settings_yaml(project_root: Path) -> dict:
    try:
        import yaml  # 仅离线解析配置，不触发任何网络或密钥读取
        text = (project_root / "config" / "settings.yaml").read_text(encoding="utf-8")
        return yaml.safe_load(text) or {}
    except Exception:
        return {}


def _from_settings_supports_state_root() -> bool:
    """探测 ``from_settings`` 是否已接受 ``state_root``（共享补丁可能未同时应用）。"""
    try:
        return "state_root" in inspect.signature(
            HealthWatchConfig.from_settings).parameters
    except (TypeError, ValueError):  # pragma: no cover - 内建/不可解析
        return False


def _build_config(args: argparse.Namespace) -> HealthWatchConfig:
    project_root = Path(args.project_root).resolve()
    profile = args.profile if args.profile else os.getenv("MTA_PROFILE")
    settings = _load_settings_yaml(project_root)
    common = {
        "profile": profile,
        "observation_stale_seconds": (
            float(args.observation_stale_seconds)
            if args.observation_stale_seconds is not None else None),
        "raw_settings": settings,
    }
    if _from_settings_supports_state_root():
        return HealthWatchConfig.from_settings(
            project_root, state_root=args.state_root, **common)
    # 核心补丁未同时应用：显式拒绝，而不是静默忽略 --state-root（静默忽略会让
    # 操作者以为盯住了指定状态根，实际盯的是旧布局）。
    if args.state_root:
        raise SystemExit(
            "--state-root 需要 src/core/health_watch.py 的 U14 适配补丁同时应用")
    return HealthWatchConfig.from_settings(project_root, **common)


def _run_once(config: HealthWatchConfig) -> tuple[dict, bool]:
    result = evaluate_health(config)
    payload = result.to_dict()
    print(json.dumps(payload, ensure_ascii=False))
    sys.stdout.flush()
    return payload, result.healthy


def main() -> int:
    parser = argparse.ArgumentParser(description="独立只读健康观察进程")
    parser.add_argument("--project-root", default=str(ROOT),
                        help="项目根目录（默认：仓库根）")
    parser.add_argument("--profile", default=None,
                        choices=[None, "demo", "live"],
                        help="运行剖面；默认读 MTA_PROFILE")
    parser.add_argument("--state-root", default=None,
                        help="显式状态根（等价于 MTA_STATE_ROOT）；换代码目录部署时"
                             "用它盯住同一份状态（U14/U13 同一解析口径）")
    parser.add_argument("--once", action="store_true",
                        help="单次检查后退出（默认行为）")
    parser.add_argument("--interval-seconds", type=float, default=None,
                        help="循环检查间隔（秒）；与 --count 配合")
    parser.add_argument("--count", type=int, default=None,
                        help="循环检查次数；缺省则一直运行")
    parser.add_argument("--observation-stale-seconds", type=float, default=None,
                        help="证据流过期阈值覆盖（秒）")
    args = parser.parse_args()

    config = _build_config(args)
    interval = args.interval_seconds if args.interval_seconds is not None else None
    count = args.count
    loops = 0
    found_anomaly = False
    while True:
        payload, healthy = _run_once(config)
        if not healthy:
            found_anomaly = True
        loops += 1
        if interval is None or (count is not None and loops >= count):
            break
        time.sleep(interval)

    # 退出码：异常优先(1)；健康但未验证告警(2)；健康且已验证(0)。
    if found_anomaly:
        return 1
    if config.alert_channel_status != "verified":
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
