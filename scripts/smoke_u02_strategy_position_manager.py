"""U02 策略仓管理 / 有界退出授权 离线冒烟（统一回归入口登记的包装器）。

全程离线：不联网、不读真实 ``.env``/``data/``、不构造真实 Broker、不下单撤单。
测试主体在 ``tests/integration/test_strategy_position_manager.py``（本项目不使用
pytest，该文件本身即可直接运行），本脚本额外做两次**负向对照**，防止"测试恒真"
把覆盖当成通过：

* [N1] 把 ``account_ref`` 打回旧的 ``sha256(api_key)[:16]`` 漂移形态
  （U15/B_r1 P1-4 的复现形态），必须让验收失败；
* [N2] 让重复退出"变成开仓"（``opens_position=True``），必须让验收失败。

两者都只改内存中的替身行为，不写任何仓库文件。
"""
from __future__ import annotations

import importlib.util
import sys
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = ROOT / "tests" / "integration" / "test_strategy_position_manager.py"


def _load_acceptance(name: str) -> Any:
    sys.path.insert(0, str(ROOT / "src"))
    sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location(name, ACCEPTANCE)
    assert spec and spec.loader, f"无法加载验收测试: {ACCEPTANCE}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # 只执行模块级导入，不自动调用 main()
    return module


def _run(module: Any) -> int:
    try:
        return int(module.main())
    except SystemExit as exc:  # pragma: no cover - main() 直接抛 SystemExit 时
        return int(exc.code or 0)


def _negative_account_ref_drift() -> int:
    module = _load_acceptance("u02_neg_drift")
    # 旧账本侧派生形态：账户引用随密钥漂移，与运行代派生值不一致。
    module.ledger_account_ref = lambda generation: f"demo:8c9c2ba2efcdc984"
    return _run(module)


def _negative_duplicate_exit_opens_position() -> int:
    module = _load_acceptance("u02_neg_reverse")
    original = module.StrategyPositionManager.request_exit

    def lax(self: Any, **kwargs: Any) -> Any:
        outcome = original(self, **kwargs)
        if outcome.duplicate:
            # 复现"重复退出被做成反向开仓"这一被禁止的形态。
            return type(outcome)(
                exit_key=outcome.exit_key, accepted=True, duplicate=True,
                action="OPEN", side="BUY", submit_qty=Decimal("0.01"),
                reduce_only=False, reason_codes=(), opens_position=True)
        return outcome

    module.StrategyPositionManager.request_exit = lax
    return _run(module)


def main() -> int:
    print("=== [1/3] 正常路径：U02 策略仓管理验收必须全部通过 ===", flush=True)
    if _run(_load_acceptance("u02_positive")) != 0:
        print("U02 策略仓管理验收失败", file=sys.stderr)
        return 1

    print("\n=== [2/3] 负向对照 N1：account_ref 漂移必须被检出 ===", flush=True)
    if _negative_account_ref_drift() == 0:
        print("负向对照 N1 未生效：account_ref 漂移未被检出", file=sys.stderr)
        return 1
    print("N1 OK：account_ref 漂移被验收拒绝")

    print("\n=== [3/3] 负向对照 N2：重复退出反向开仓必须被检出 ===", flush=True)
    if _negative_duplicate_exit_opens_position() == 0:
        print("负向对照 N2 未生效：重复退出反向开仓未被检出", file=sys.stderr)
        return 1
    print("N2 OK：重复退出反向开仓被验收拒绝")

    print("\nU02 SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
