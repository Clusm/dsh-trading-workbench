"""校验 Windows SCM 托管配置并输出安装命令。

默认只读。脚本不调用 ``sc.exe``，不启动/停止进程，不读取 ``.env``，也不接触数据库、
账户或交易所。真正安装前先在人工维护的管理员终端复制 ``--print-install`` 输出，
完成安装后再用 SCM 自带命令核对服务状态。
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.windows_service import (  # noqa: E402
    WindowsServiceConfig,
    load_windows_service_config,
)


def build_install_commands(config: WindowsServiceConfig) -> list[list[str]]:
    """生成需由管理员人工执行的 SCM 命令参数，不执行外部命令。"""
    create = [
        "sc.exe", "create", config.service_name,
        f"binPath= {config.scm_bin_path()}",
        f"DisplayName= {config.display_name}",
        "start= auto",
    ]
    description = ["sc.exe", "description", config.service_name, config.description]
    failure = [
        "sc.exe", "failure", config.service_name,
        "reset=", "86400",
        "actions=", f"restart/{config.restart_delay_seconds * 1000}/restart/"
        f"{config.restart_delay_seconds * 2000}/restart/{config.restart_delay_seconds * 4000}",
    ]
    return [create, description, failure]


def command_text(command: list[str]) -> str:
    """以 Windows 命令行规则安全引用一条待执行命令。"""
    return subprocess.list2cmdline(command)


def validate_config(path: Path, *, require_files: bool = False) -> WindowsServiceConfig:
    """公开给冒烟脚本复用的统一配置校验入口。"""
    return load_windows_service_config(path, project_root=ROOT, require_files=require_files)


def main() -> int:
    parser = argparse.ArgumentParser(description="只读校验 Windows SCM 托管计划")
    parser.add_argument(
        "--config", type=Path, default=ROOT / "config" / "windows_service.json",
    )
    parser.add_argument("--print-install", action="store_true",
                        help="在校验通过后打印待人工执行的 sc.exe 命令")
    parser.add_argument("--require-files", action="store_true",
                        help="额外确认本机解释器、入口和工作目录存在")
    args = parser.parse_args()
    try:
        config = validate_config(args.config, require_files=args.require_files)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"WINDOWS SERVICE PLAN FAIL: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(json.dumps({
        "ok": True,
        "service_name": config.service_name,
        "host": config.host,
        "port": config.port,
        "entrypoint": str(config.entrypoint),
        "stop_timeout_seconds": config.stop_timeout_seconds,
        "restart_delay_seconds": config.restart_delay_seconds,
        "uses_scm": True,
        "mutated": False,
    }, ensure_ascii=False, indent=2))
    if args.print_install:
        print("\n# 下面命令仅供管理员人工执行；本脚本不会自动调用 sc.exe")
        for command in build_install_commands(config):
            print(command_text(command))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
