"""以脱离父进程的方式启动 Web 控制台。

为什么需要这个脚本
------------------
直接在终端里 `python scripts/run_web.py` 时，进程与启动它的会话是绑定的：
会话结束，子进程一并被终止。在自动化环境（agent 后台任务、CI、脚本调用）里，
这表现为「服务跑几分钟后静默消失，日志里没有任何错误堆栈」——
因为进程根本没崩溃，是被父会话回收了。

本脚本在 Windows 上用 DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP 让服务进程
脱离父进程独立存活，启动后立即返回。

用法
----
    python scripts/start_web_detached.py                     # 默认 127.0.0.1:8000
    python scripts/start_web_detached.py --proxy http://127.0.0.1:10808
    python scripts/start_web_detached.py --no-proxy           # 不设代理，直连

日志写在 data/web_console.log。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Windows 进程创建标志
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200


def build_env(proxy: str | None) -> dict[str, str]:
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
    return env


def already_listening(port: int) -> bool:
    try:
        import socket
        with socket.create_connection(("127.0.0.1", port), timeout=1.0):
            return True
    except OSError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="脱离父进程启动 Web 控制台")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--proxy", default="http://127.0.0.1:10808",
                        help="HTTP(S) 出口代理，默认 http://127.0.0.1:10808")
    parser.add_argument("--no-proxy", action="store_true", help="不设置代理，直连")
    args = parser.parse_args()

    if already_listening(args.port):
        print(f"端口 {args.port} 已在监听，跳过启动")
        return 0

    log_dir = PROJECT_ROOT / "data"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "web_console.log"

    python_exe = sys.executable
    # 走 run_web_service.py 包装：它在进程内补齐代理环境并把 stdout/stderr
    # 落盘到 data/web_console.log，比直接跑 run_web.py 更适合无人值守。
    wrapper = PROJECT_ROOT / "scripts" / "run_web_service.py"
    entry = wrapper if wrapper.exists() else (PROJECT_ROOT / "scripts" / "run_web.py")
    cmd = [python_exe, "-u", str(entry),
           "--host", args.host, "--port", str(args.port)]

    env = build_env(None if args.no_proxy else args.proxy)

    # 日志以追加方式打开，避免重启丢失历史
    log_file = open(log_path, "a", encoding="utf-8")
    log_file.write(f"\n===== 启动 {time.strftime('%Y-%m-%d %H:%M:%S')} "
                   f"proxy={'none' if args.no_proxy else args.proxy} =====\n")
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
        if already_listening(args.port):
            print(f"服务已启动 PID={proc.pid}  http://{args.host}:{args.port}")
            print(f"日志: {log_path}")
            return 0
        if proc.poll() is not None:
            print(f"进程已退出，返回码 {proc.returncode}，请看日志: {log_path}")
            return 1

    print(f"等待端口超时（PID={proc.pid}），请看日志: {log_path}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
