"""以脱离父进程的方式启动 Web 控制台。

为什么需要这个脚本
------------------
直接在终端里 `python scripts/run_web.py` 时，进程与启动它的会话是绑定的：
会话结束，子进程一并被终止。在自动化环境（agent 后台任务、CI、脚本调用）里，
这表现为「服务跑几分钟后静默消失，日志里没有任何错误堆栈」——
因为进程根本没崩溃，是被父会话回收了。

本脚本在 Windows 上用 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP 让服务进程
脱离父进程独立存活，启动后立即返回。

profile 与端口
--------------
--profile 是默认端口与日志位置的唯一来源：

    profile  默认端口  日志
    demo     8000      data/web_console.log
    live     8001      data/live/web_console.log

显式 --port 优先于 profile 默认值。端口只解析一次，子进程始终收到显式的
--profile 与解析后的 --port，不依赖子进程自身的默认值，避免
「启动器探活 A 端口、子进程监听 B 端口」这类 profile/端口歧义。

用法
----
    python scripts/start_web_detached.py                             # demo，127.0.0.1:8000
    python scripts/start_web_detached.py --profile live --egress-id 162.248.75.128
    python scripts/start_web_detached.py --profile live --port 9001 --egress-id 162.248.75.128
    python scripts/start_web_detached.py --proxy http://127.0.0.1:10808
    python scripts/start_web_detached.py --no-proxy                  # 不设代理，直连
"""
from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# profile 是默认端口的唯一来源，避免启动器与子进程各持一套默认值
PROFILE_DEFAULT_PORTS = {"demo": 8000, "live": 8001}

# Windows 进程创建标志
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def resolve_port(profile: str, port: int | None) -> int:
    """解析实际端口：显式 --port 优先，未指定时由 profile 唯一决定。"""
    if port is not None:
        return port
    return PROFILE_DEFAULT_PORTS[profile]


def log_path_for(project_root: Path, profile: str) -> Path:
    """profile 决定日志文件，demo 与 live 两个实例的日志互不覆盖。"""
    if profile == "live":
        return project_root / "data" / "live" / "web_console.log"
    return project_root / "data" / "web_console.log"


def build_env(proxy: str | None, egress_id: str | None = None) -> dict[str, str]:
    """构造子进程环境。

    代理必须显式写进环境：项目里所有 httpx 调用都靠 trust_env 读环境变量，
    一旦环境里没有（或被注入了不可用的代理），三路情报会静默全灭。
    """
    env = os.environ.copy()
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(key, None)
    if proxy:
        # 显式 0 覆盖 .env 中可能残留的 MTA_NO_PROXY=1；仅删除环境变量会让
        # run_web 的早期加载再次读到文件值，造成“启动器说走代理、实际直连”。
        env["MTA_NO_PROXY"] = "0"
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
    else:
        # run_web_service.py 会兼容旧启动方式而注入默认代理；用进程内开关
        # 让 run_web.py 的 apply_proxy_env() 显式清除它，保证 --no-proxy 真直连。
        env["MTA_NO_PROXY"] = "1"
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["no_proxy"] = "127.0.0.1,localhost"
    env["PYTHONUNBUFFERED"] = "1"
    if egress_id:
        # 公开出口标识进入发布清单，供 418/429 观察窗按出口归因；不是交易凭据。
        env["MTA_EGRESS_ID"] = str(egress_id).strip()
    return env


def already_listening(port: int) -> bool:
    """探测解析后的实际端口；启动前跳过与启动后确认共用同一端口来源。"""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="脱离父进程启动 Web 控制台")
    parser.add_argument("--profile", choices=("demo", "live"), default="demo",
                        help="运行档位：demo=端口 8000 / live=端口 8001（默认 demo）")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=None,
                        help="显式端口；未指定时由 --profile 决定")
    parser.add_argument("--proxy", default="http://127.0.0.1:10808",
                        help="HTTP(S) 出口代理，默认 http://127.0.0.1:10808")
    parser.add_argument("--no-proxy", action="store_true", help="不设置代理，直连")
    parser.add_argument(
        "--egress-id", default=None,
        help="已核验的公网出口标识；live 必填，也可由 MTA_EGRESS_ID 提供",
    )
    args = parser.parse_args(argv)

    port = resolve_port(args.profile, args.port)
    egress_id = str(args.egress_id or os.getenv("MTA_EGRESS_ID", "")).strip()
    if args.profile == "live" and not egress_id:
        parser.error("live 启动必须提供 --egress-id 或 MTA_EGRESS_ID，禁止发布清单使用 unknown")

    if already_listening(port):
        print(f"profile={args.profile} 端口 {port} 已在监听，跳过启动")
        return 0

    log_path = log_path_for(PROJECT_ROOT, args.profile)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    python_exe = sys.executable
    # 走旧版 run_web_service.py 包装，把 stdout/stderr 落盘到 profile 对应的
    # 日志；代理由本启动器的环境和统一 run_web.py 解析。
    wrapper = PROJECT_ROOT / "scripts" / "run_web_service.py"
    entry = wrapper if wrapper.exists() else (PROJECT_ROOT / "scripts" / "run_web.py")
    # 子进程必须显式拿到 --profile 与解析后的 --port，禁止再走子进程默认值
    cmd = [python_exe, "-u", str(entry),
           "--profile", args.profile, "--host", args.host, "--port", str(port)]

    env = build_env(None if args.no_proxy else args.proxy, egress_id or None)

    # 日志以追加方式打开，避免重启丢失历史
    log_file = open(log_path, "a", encoding="utf-8")
    log_file.write(f"\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} "
                   f"profile={args.profile} port={port} "
                   f"proxy={'none' if args.no_proxy else args.proxy} "
                   f"egress_id={egress_id or 'unknown'} =====\n")
    log_file.flush()

    creationflags = 0
    if os.name == "nt":
        creationflags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    proc = subprocess.Popen(
        cmd,
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_file,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        creationflags=creationflags,
        start_new_session=(os.name != "nt"),
        close_fds=True,
    )

    # 等待端口起来，最多 40 秒
    for _ in range(80):
        time.sleep(0.5)
        if already_listening(port):
            print(f"服务已启动 PID={proc.pid}  http://{args.host}:{port}  profile={args.profile}")
            print(f"日志: {log_path}")
            return 0
        if proc.poll() is not None:
            print(f"进程已退出，返回码 {proc.returncode}，请看日志: {log_path}")
            return 1

    print(f"等待端口超时（PID={proc.pid}），请看日志: {log_path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
