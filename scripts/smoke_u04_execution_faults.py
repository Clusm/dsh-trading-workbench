"""U04 执行故障离线冒烟（统一回归入口登记的包装器）。

全程离线：不联网、不读真实 ``.env``/``data/``、不构造真实 Broker、不下单/撤单。
测试主体在 ``tests/acceptance/test_execution_faults.py``（本项目不使用 pytest，
该文件本身即可直接运行），本脚本额外做一次**负向对照**：把 golden fixture 的一处
期望值改成错误值后重跑，必须失败退出。防止"测试恒真"把覆盖当成通过。
"""
from __future__ import annotations

import importlib.util
import json
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
ACCEPTANCE = ROOT / "tests" / "acceptance" / "test_execution_faults.py"
FIXTURES = ROOT / "tests" / "fixtures" / "execution"


def _load_acceptance() -> Any:
    sys.path.insert(0, str(ROOT / "src"))
    spec = importlib.util.spec_from_file_location("u04_acceptance", ACCEPTANCE)
    assert spec and spec.loader, f"无法加载验收测试: {ACCEPTANCE}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # 只执行模块级导入，不自动调用 main()
    return module


def _tamper(fixture_name: str, key_path: list[str], value: Any) -> dict[str, Any]:
    """复制夹具到临时目录并篡改一处 golden 期望，返回篡改后的数据。"""
    source = FIXTURES / fixture_name
    data = json.loads(source.read_text(encoding="utf-8"))
    node: Any = data
    for key in key_path[:-1]:
        node = node[key]
    node[key_path[-1]] = value
    return data


def main() -> int:
    module = _load_acceptance()
    print("=== [1/2] 正常路径：U04 执行故障验收必须全部通过 ===", flush=True)
    exit_code = module.main()
    if exit_code != 0:
        print("U04 执行故障验收失败", file=sys.stderr)
        return 1

    print("\n=== [2/2] 负向对照：篡改 golden 后期望必须失败 ===", flush=True)
    workdir = Path(tempfile.mkdtemp(prefix="mta-u04-neg-"))
    try:
        for name in ("dc04_partial_fill_conservation.json",
                     "dc13_unknown_no_resubmit.json",
                     "f03_protection_follows_position.json",
                     "f21_algo_standard_separation.json"):
            shutil.copy2(FIXTURES / name, workdir / name)
        # 把"迟到成交后的终态余量"改成错误值：若测试仍通过，说明断言没有真正绑定 golden。
        tampered = _tamper("dc04_partial_fill_conservation.json",
                           ["derived_golden", "after_late_fill", "terminal_unfilled"],
                           "0.0007")
        (workdir / "dc04_partial_fill_conservation.json").write_text(
            json.dumps(tampered, ensure_ascii=False, indent=2), encoding="utf-8")
        original_fixtures = module.FIXTURES
        original_stdout = sys.stdout
        try:
            module.FIXTURES = workdir
            sys.stdout = open(__import__("os").devnull, "w", encoding="utf-8")
            tampered_code = module.main()
        finally:
            sys.stdout.close()
            sys.stdout = original_stdout
            module.FIXTURES = original_fixtures
        if tampered_code == 0:
            print("负向对照失败：篡改 golden 后期望仍未失败", file=sys.stderr)
            return 1
        if module.PASSED <= 0 or not module.FAILURES:
            print("负向对照失败：未收集到失败项", file=sys.stderr)
            return 1
        print(f"  PASS  篡改 golden 后验收失败退出（exit={tampered_code}，"
              f"failed_checks={len(module.FAILURES)}）")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    print("\nU04 EXECUTION FAULTS SMOKE OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
