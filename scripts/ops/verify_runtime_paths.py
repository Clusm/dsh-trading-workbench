"""只读核验 RuntimePaths：全消费者清单、shared_egress_root 唯一性、换代码根不改锁。

默认对 **当前仓库根** 做纯路径解析：不创建目录、不读任何状态文件内容、不读
``.env``/密钥、不联网、不启动服务。需要模拟"换了代码目录"时用 ``--other-code-root``
指向一个 **只用于解析** 的路径即可（同样不落盘）。

用途：
- 部署前确认 live 状态根与账户绑定（``--environment live``）；
- 事故复盘时确认"两个控制台是否指向同一状态根/同一把锁"（F06/F18/R12）；
- 迁移前看清旧位置 → 规范位置要复制什么（F18 可回滚迁移）。

退出码：0 = 全部检查通过；2 = 存在失败项（fail-closed）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from core.runtime_paths import (  # noqa: E402
    CONSUMER_INVENTORY,
    SHARED_EGRESS_CONSUMERS,
    RuntimePaths,
    fingerprint_disposition,
)

VALID_STATUS = {"migrated", "aligned", "compat", "planned"}


def build_report(code_root: Path, *, profile: str | None, state_root: str | None,
                 environment: str | None) -> dict:
    paths = RuntimePaths.resolve(code_root, profile=profile, state_root=state_root)
    failures: list[str] = []

    keys = [entry.key for entry in CONSUMER_INVENTORY]
    if len(keys) != len(set(keys)):
        failures.append("CONSUMER_KEY_DUPLICATE")
    if not all(entry.status in VALID_STATUS for entry in CONSUMER_INVENTORY):
        failures.append("CONSUMER_STATUS_INVALID")
    if not all(entry.owner and entry.note for entry in CONSUMER_INVENTORY):
        failures.append("CONSUMER_UNOWNED")
    escapes = paths.escape_check()
    if escapes:
        failures.append("PATH_ESCAPE")
    if not paths.shared_egress_root_is_unique():
        failures.append("SHARED_EGRESS_ROOT_NOT_UNIQUE")
    if paths.unresolved_consumers():
        # 显式登记项不算失败（登记过即可），但必须出现在报告里。
        pass

    reasons = paths.validate(environment) if environment else ()
    if environment and reasons:
        failures.append("ENVIRONMENT_STATE_ROOT_MISMATCH")

    # 换代码根不改锁：**显式声明状态根**时，另一个代码根必须得到同一把锁。
    # 未声明状态根（旧布局）时锁随代码根是升级前的既有行为，如实报告而非判失败。
    other = RuntimePaths.resolve(Path(str(code_root) + "__other_code_root__"),
                                 profile=profile, state_root=state_root)
    lock_stable = (other.lock_file == paths.lock_file
                   and other.lock_scope() == paths.lock_scope())
    if paths.state_base_explicit and not lock_stable:
        failures.append("LOCK_SCOPE_FOLLOWS_CODE_ROOT")

    return {
        "code_root": str(paths.code_root),
        "state_base": str(paths.state_base),
        "state_base_declared": paths.state_base_declared,
        "state_base_source": paths.state_base_source,
        "state_base_explicit": paths.state_base_explicit,
        "state_root": str(paths.state_root),
        "profile": paths.profile,
        "profile_declared": paths.profile_declared,
        "environment": environment,
        "environment_state_root_reasons": list(reasons),
        "shared_egress_root": str(paths.shared_egress_root),
        "shared_egress_root_unique": paths.shared_egress_root_is_unique(),
        "shared_egress_consumers": list(SHARED_EGRESS_CONSUMERS),
        "shared_egress_paths": {
            "rate_limit_state": str(paths.rate_limit_state),
            "rate_limit_state_effective": str(paths.shared_egress_state_file()),
            "coordination_dir": str(paths.coordination_dir),
        },
        "lock_file": str(paths.lock_file),
        "lock_scope": paths.lock_scope(),
        "lock_stable_across_code_roots": lock_stable,
        "lock_scope_vs_other_code_root": {
            "code_root": str(other.code_root),
            "lock_file": str(other.lock_file),
            "lock_scope": other.lock_scope(),
            "same_lock": other.lock_file == paths.lock_file,
        },
        "run_registry_file": str(paths.run_registry_file),
        "consumers": paths.consumer_report(),
        "consumers_planned": paths.unresolved_consumers(),
        "path_escapes": escapes,
        "compatibility_migration_plan": paths.compatibility_migration_plan(),
        "fingerprint_disposition": fingerprint_disposition(),
        "failures": failures,
        "ok": not failures,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="只读核验 RuntimePaths（不落盘）")
    parser.add_argument("--code-root", type=Path, default=ROOT)
    parser.add_argument("--state-root", default=None,
                        help="显式状态根（MTA_STATE_ROOT 的等价注入点）")
    parser.add_argument("--profile", choices=("demo", "live"), default=None)
    parser.add_argument("--environment", default=None,
                        help="做 environment↔状态根一致性判定（如 live）")
    parser.add_argument("--json", action="store_true", help="只打印 JSON")
    parser.add_argument("--other-code-root", type=Path, default=None,
                        help="额外报告：另一个代码根解析出的锁是否相同")
    args = parser.parse_args()
    report = build_report(args.code_root, profile=args.profile,
                          state_root=args.state_root, environment=args.environment)
    if args.other_code_root is not None:
        other = RuntimePaths.resolve(args.other_code_root, profile=args.profile,
                                     state_root=args.state_root)
        report["other_code_root"] = {
            "code_root": str(other.code_root),
            "state_base": str(other.state_base),
            "lock_file": str(other.lock_file),
            "lock_scope": other.lock_scope(),
            "same_lock_as_primary": other.lock_file == Path(report["lock_file"]),
        }
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["ok"] else 2
    print("RUNTIME PATHS VERIFY")
    print(f"  code_root            = {report['code_root']}")
    print(f"  state_base           = {report['state_base']} "
          f"(declared={report['state_base_declared']} "
          f"source={report['state_base_source']})")
    print(f"  state_root           = {report['state_root']} profile={report['profile']}")
    print(f"  shared_egress_root   = {report['shared_egress_root']} "
          f"unique={report['shared_egress_root_unique']}")
    print(f"  lock_file            = {report['lock_file']} scope={report['lock_scope']}")
    print(f"  lock stable across code roots = {report['lock_stable_across_code_roots']}")
    print(f"  run_registry_file    = {report['run_registry_file']}")
    print(f"  consumers            = {len(report['consumers'])} "
          f"(planned={len(report['consumers_planned'])})")
    for row in report["consumers"]:
        print(f"    - [{row['status']:8}] {row['key']} -> {row['resolved']}")
    if report["environment_state_root_reasons"]:
        print(f"  environment reasons  = {report['environment_state_root_reasons']}")
    print("\n  compatibility migration plan:")
    for row in report["compatibility_migration_plan"]:
        print(f"    - [{row['action']:12}] {row['key']}")
    if report["failures"]:
        print(f"\nRUNTIME PATHS VERIFY FAILED: {', '.join(report['failures'])}")
        return 2
    print("\nRUNTIME PATHS VERIFY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
