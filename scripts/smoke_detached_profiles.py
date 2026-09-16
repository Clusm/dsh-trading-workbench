"""脱离启动器的 profile / 端口 / 日志离线回归。

覆盖 ``scripts/start_web_detached.py``：

- ``--profile demo|live`` 决定默认端口（demo=8000 / live=8001）；
- 显式 ``--port`` 覆盖 profile 默认值，且端口只解析一次；
- 子进程命令行始终显式携带 ``--profile`` 与解析后的 ``--port``；
- demo 日志写 ``data/web_console.log``，live 日志写 ``data/live/web_console.log``；
- 端口探活（已监听跳过、启动后确认）使用解析后的实际端口。

用假对象替换 ``subprocess.Popen``、``socket`` 和 ``time``，并把启动器的
``PROJECT_ROOT`` 指向临时目录：不启动真实服务、不联网、不读写项目 ``.env``
或 ``data`` 目录，也不触碰真实端口。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import shutil
import sys
import tempfile
import types
from dataclasses import dataclass
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent
TIMESTAMP = "2026-09-15 00:00:00"
DEFAULT_PROXY = "http://127.0.0.1:10808"
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy")

passed = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed
    if not condition:
        raise AssertionError(f"{name} | {detail}" if detail else name)
    passed += 1
    print(f"  PASS  {name}")


def load_launcher() -> types.ModuleType:
    """按文件路径加载启动器；只执行模块顶层定义，不进入 main()。"""
    spec = importlib.util.spec_from_file_location(
        "start_web_detached_under_test", SCRIPTS_DIR / "start_web_detached.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 scripts/start_web_detached.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


launcher = load_launcher()

wrapper_source = (SCRIPTS_DIR / "run_web_service.py").read_text(encoding="utf-8")
check("旧包装入口按 profile 分离日志",
      '_profile == "live"' in wrapper_source and '"data", "live"' in wrapper_source)


class FakeSocket:
    """替换 socket.create_connection：按脚本序列回答，并记录每次探测地址。"""

    def __init__(self, responses: list[bool]) -> None:
        self._responses = list(responses)
        self.attempts: list[tuple[str, int]] = []

    def create_connection(self, address: tuple[str, int], timeout: float | None = None):
        self.attempts.append((address[0], address[1]))
        listening = self._responses.pop(0) if self._responses else False
        if not listening:
            raise OSError("connection refused (fake socket)")
        return contextlib.nullcontext()


class FakeTime:
    """替换 time：不真实等待；等待循环超出预算立即失败。"""

    def __init__(self) -> None:
        self.sleeps: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        if len(self.sleeps) > 100:
            raise RuntimeError("启动器等待循环未按预期结束")

    def strftime(self, _format: str) -> str:
        return TIMESTAMP


class FakeProc:
    def __init__(self, cmd: list[str], kwargs: dict[str, object],
                 returncode: int | None) -> None:
        self.cmd = cmd
        self.kwargs = kwargs
        self.pid = 4242
        self.returncode = returncode

    def poll(self) -> int | None:
        return self.returncode


class FakePopen:
    """记录 Popen 参数，绝不真正创建进程。"""

    def __init__(self, returncode: int | None = None) -> None:
        self.calls: list[FakeProc] = []
        self._returncode = returncode

    def __call__(self, cmd: list[str], **kwargs: object) -> FakeProc:
        proc = FakeProc(cmd, kwargs, self._returncode)
        self.calls.append(proc)
        return proc


@dataclass
class RunResult:
    """一次启动器调用的离线观测结果。"""

    code: int | None
    stdout: str
    stderr: str
    calls: list[FakeProc]
    attempts: list[tuple[str, int]]
    sleeps: list[float]
    root: Path
    demo_log: str | None
    live_log: str | None
    sentinels: types.SimpleNamespace

    @property
    def cmd(self) -> list[str]:
        return self.calls[0].cmd if self.calls else []

    @property
    def env(self) -> dict[str, str]:
        return self.calls[0].kwargs.get("env", {}) if self.calls else {}

    @property
    def kwargs(self) -> dict[str, object]:
        return self.calls[0].kwargs if self.calls else {}


@contextlib.contextmanager
def proxy_env(**values: str):
    """临时注入代理环境变量，结束后恢复原值。"""
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def flag_value(cmd: list[str], flag: str) -> str | None:
    if flag not in cmd:
        return None
    index = cmd.index(flag)
    return cmd[index + 1] if index + 1 < len(cmd) else None


def run_launcher(args: list[str], *, responses: list[bool],
                 returncode: int | None = None) -> RunResult:
    """在临时项目根里离线执行一次 main()，返回命令、环境、日志与探活观测。"""
    root = Path(tempfile.mkdtemp(prefix="mta-detached-"))
    try:
        (root / "scripts").mkdir()
        (root / "scripts" / "run_web_service.py").write_text("# fixture", encoding="utf-8")

        fake_socket = FakeSocket(responses)
        fake_time = FakeTime()
        fake_popen = FakePopen(returncode)
        fake_subprocess = types.SimpleNamespace(
            Popen=fake_popen, STDOUT=object(), DEVNULL=object())

        originals = (launcher.PROJECT_ROOT, launcher.socket, launcher.time,
                     launcher.subprocess)
        launcher.PROJECT_ROOT = root
        launcher.socket = fake_socket
        launcher.time = fake_time
        launcher.subprocess = fake_subprocess
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    code: int | None = launcher.main(list(args))
                except SystemExit as exc:  # argparse 拒绝非法参数
                    code = exc.code
        finally:
            (launcher.PROJECT_ROOT, launcher.socket, launcher.time,
             launcher.subprocess) = originals

        for proc in fake_popen.calls:  # 关闭启动器打开的日志句柄
            stream = proc.kwargs.get("stdout")
            if hasattr(stream, "close"):
                stream.close()

        demo_log = root / "data" / "web_console.log"
        live_log = root / "data" / "live" / "web_console.log"
        return RunResult(
            code=code,
            stdout=stdout.getvalue(),
            stderr=stderr.getvalue(),
            calls=fake_popen.calls,
            attempts=fake_socket.attempts,
            sleeps=fake_time.sleeps,
            root=root,
            demo_log=demo_log.read_text(encoding="utf-8") if demo_log.is_file() else None,
            live_log=live_log.read_text(encoding="utf-8") if live_log.is_file() else None,
            sentinels=fake_subprocess,
        )
    finally:
        shutil.rmtree(root, ignore_errors=True)


print("\n[1] demo 默认：端口 8000、日志 data/web_console.log")
with proxy_env(HTTP_PROXY="http://stale.invalid:9", http_proxy="http://stale.invalid:9"):
    demo = run_launcher([], responses=[False, True])
check("demo 启动返回 0", demo.code == 0, str(demo.code))
check("命令行显式携带 --profile demo", flag_value(demo.cmd, "--profile") == "demo", str(demo.cmd))
check("demo 默认端口 8000 显式传给子进程", flag_value(demo.cmd, "--port") == "8000")
check("profile/端口参数各出现一次",
      demo.cmd.count("--profile") == 1 and demo.cmd.count("--port") == 1)
check("host 透传回环地址", flag_value(demo.cmd, "--host") == "127.0.0.1")
check("解释器与包装入口正确",
      demo.cmd[0] == sys.executable and Path(demo.cmd[2]).name == "run_web_service.py")
check("探活与启动确认都使用 8000",
      demo.attempts == [("127.0.0.1", 8000)] * 2, str(demo.attempts))
check("demo 日志写入 data/web_console.log", demo.demo_log is not None)
check("demo 不产生 data/live/web_console.log", demo.live_log is None)
demo_header = demo.demo_log or ""
check("日志头标注 profile 与端口", "profile=demo" in demo_header and "port=8000" in demo_header)
check("输出 demo 日志绝对路径",
      str(demo.root / "data" / "web_console.log") in demo.stdout)
check("输出 demo 访问地址", "http://127.0.0.1:8000" in demo.stdout)
check("子进程工作目录为项目根", demo.kwargs.get("cwd") == str(demo.root))
check("stderr 合并进日志、stdin 断开",
      demo.kwargs.get("stderr") is demo.sentinels.STDOUT
      and demo.kwargs.get("stdin") is demo.sentinels.DEVNULL)
check("close_fds 打开", demo.kwargs.get("close_fds") is True)
expected_flags = ((launcher.DETACHED_PROCESS | launcher.CREATE_NEW_PROCESS_GROUP)
                  if os.name == "nt" else 0)
check("创建标志匹配平台", demo.kwargs.get("creationflags") == expected_flags)
check("非 Windows 使用独立会话", demo.kwargs.get("start_new_session") == (os.name != "nt"))
check("代理模式显式关闭直连开关", demo.env.get("MTA_NO_PROXY") == "0")
check("四种代理变量均指向默认出口",
      all(demo.env.get(key) == DEFAULT_PROXY for key in PROXY_KEYS))
check("回环地址排除代理",
      demo.env.get("NO_PROXY") == "127.0.0.1,localhost"
      and demo.env.get("no_proxy") == "127.0.0.1,localhost")
check("子进程无缓冲输出", demo.env.get("PYTHONUNBUFFERED") == "1")
check("端口确认采用 0.5 秒轮询", demo.sleeps == [0.5], str(demo.sleeps))

print("\n[2] live 默认：端口 8001、日志 data/live/web_console.log")
live = run_launcher(
    ["--profile", "live", "--egress-id", "162.248.75.128"],
    responses=[False, True],
)
check("live 启动返回 0", live.code == 0, str(live.code))
check("命令行显式携带 --profile live", flag_value(live.cmd, "--profile") == "live", str(live.cmd))
check("live 默认端口 8001 显式传给子进程", flag_value(live.cmd, "--port") == "8001")
check("探活与启动确认都使用 8001",
      live.attempts == [("127.0.0.1", 8001)] * 2, str(live.attempts))
check("live 日志写入 data/live/web_console.log", live.live_log is not None)
check("live 不写 demo 日志", live.demo_log is None)
live_header = live.live_log or ""
check("live 日志头标注 profile 与端口",
      "profile=live" in live_header and "port=8001" in live_header)
check("输出 live 日志绝对路径",
      str(live.root / "data" / "live" / "web_console.log") in live.stdout)
check("输出 live 访问地址", "http://127.0.0.1:8001" in live.stdout)
check("live 出口标识注入子进程环境",
      live.env.get("MTA_EGRESS_ID") == "162.248.75.128")
check("live 日志标注出口身份",
      "egress_id=162.248.75.128" in (live.live_log or ""))

print("\n[3] 显式 --port 覆盖 profile 默认端口")
override = run_launcher(
    ["--profile", "live", "--port", "9123", "--egress-id", "test-egress"],
    responses=[False, True],
)
check("显式端口透传给子进程", flag_value(override.cmd, "--port") == "9123", str(override.cmd))
check("端口参数唯一且未被追加默认值", override.cmd.count("--port") == 1)
check("探活使用显式端口 9123",
      override.attempts == [("127.0.0.1", 9123)] * 2, str(override.attempts))
check("日志归属仍由 profile 决定",
      override.live_log is not None and override.demo_log is None)

print("\n[4] demo 显式端口不改变日志归属")
demo_port = run_launcher(["--port", "9200"], responses=[False, True])
check("缺省 profile 为 demo", flag_value(demo_port.cmd, "--profile") == "demo")
check("端口 9200 生效", flag_value(demo_port.cmd, "--port") == "9200")
check("日志仍在 data/web_console.log",
      demo_port.demo_log is not None and demo_port.live_log is None)

print("\n[5] 已在监听：按解析后的实际端口跳过启动")
already = run_launcher(
    ["--profile", "live", "--egress-id", "test-egress"], responses=[True])
check("已监听时返回 0", already.code == 0, str(already.code))
check("只探活一次且端口为 8001", already.attempts == [("127.0.0.1", 8001)],
      str(already.attempts))
check("不创建子进程", not already.calls)
check("跳过信息标注 profile 与端口",
      "跳过启动" in already.stdout and "8001" in already.stdout, already.stdout)
check("跳过时不产生日志文件", already.demo_log is None and already.live_log is None)

print("\n[6] 非法 profile 在启动前被 argparse 拒绝")
invalid = run_launcher(["--profile", "paper"], responses=[])
check("非法档位以退出码 2 拒绝", invalid.code == 2, str(invalid.code))
check("不创建子进程", not invalid.calls)
check("不做端口探活", not invalid.attempts)
check("错误信息列出可选档位",
      "invalid choice" in invalid.stderr and "paper" in invalid.stderr, invalid.stderr)

print("\n[7] 子进程提前退出：返回 1 并指向 live 日志")
exited = run_launcher(
    ["--profile", "live", "--egress-id", "test-egress"],
    responses=[False, False], returncode=1,
)
check("子进程退出时启动器返回 1", exited.code == 1, str(exited.code))
check("提示退出码与 live 日志路径",
      "进程已退出" in exited.stdout
      and str(exited.root / "data" / "live" / "web_console.log") in exited.stdout,
      exited.stdout)
check("等待循环探测 live 端口",
      exited.attempts == [("127.0.0.1", 8001)] * 2, str(exited.attempts))
check("子进程退出后立即返回，不空等",
      exited.sleeps == [0.5], str(exited.sleeps))

print("\n[8] --no-proxy：清除代理环境并显式直连")
with proxy_env(HTTP_PROXY="http://stale.invalid:9", http_proxy="http://stale.invalid:9"):
    direct = run_launcher(["--no-proxy"], responses=[False, True])
check("直连开关显式置 1", direct.env.get("MTA_NO_PROXY") == "1")
check("四种代理变量全部清除", all(key not in direct.env for key in PROXY_KEYS))
check("回环地址仍排除代理", direct.env.get("NO_PROXY") == "127.0.0.1,localhost")
check("日志头记录 proxy=none", "proxy=none" in (direct.demo_log or ""))

print("\n[9] live 缺少出口身份在创建进程前 fail-closed")
with proxy_env(MTA_EGRESS_ID=""):
    missing_egress = run_launcher(["--profile", "live"], responses=[])
check("缺出口身份以参数错误拒绝", missing_egress.code == 2, str(missing_egress.code))
check("缺出口身份不探端口也不创建子进程",
      not missing_egress.attempts and not missing_egress.calls)
check("错误信息明确要求 MTA_EGRESS_ID",
      "MTA_EGRESS_ID" in missing_egress.stderr, missing_egress.stderr)

print(f"\n结果: {passed} 通过 / 0 失败")
