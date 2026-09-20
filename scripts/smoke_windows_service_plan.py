"""Windows SCM 托管配置、命令构造和 fail-closed 规则离线回归。

只使用临时 JSON；不调用 ``sc.exe``，不启动服务，不读取 ``.env`` 或交易状态。
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.windows_service import load_windows_service_config  # noqa: E402
from validate_windows_service_plan import (  # noqa: E402
    build_install_commands,
    command_text,
    validate_config,
)
from windows_service_host import _configure_log  # noqa: E402


passed = 0


def check(name: str, condition: bool) -> None:
    global passed
    if not condition:
        raise AssertionError(name)
    passed += 1
    print(f"  PASS  {name}")


def write_config(directory: Path, **updates: object) -> Path:
    config_dir = directory / "config"
    (directory / "scripts").mkdir(parents=True)
    (directory / "data").mkdir(parents=True)
    (directory / ".venv" / "Scripts").mkdir(parents=True)
    (directory / "scripts" / "run_web.py").write_text("# fixture", encoding="utf-8")
    (directory / ".venv" / "Scripts" / "python.exe").write_bytes(b"fixture")
    config_dir.mkdir(exist_ok=True)
    payload = {
        "service_name": "MultiAgentTrader",
        "display_name": "Multi-Agent Trader Web Service",
        "description": "USD-M demo service",
        "python_executable": ".venv/Scripts/python.exe",
        "entrypoint": "scripts/run_web.py",
        "working_directory": ".",
        "host": "127.0.0.1",
        "port": 8000,
        "interval_minutes": None,
        "no_autostart": False,
        "stop_timeout_seconds": 30,
        "restart_delay_seconds": 30,
        "log_path": "data/web_console.log",
    }
    payload.update(updates)
    path = config_dir / "windows_service.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


tmp = Path(tempfile.mkdtemp(prefix="mta-service-plan-"))
try:
    print("\n[1] 正式配置与SCM命令只读构造")
    config = validate_config(ROOT / "config" / "windows_service.json", require_files=True)
    commands = build_install_commands(config)
    rendered = "\n".join(command_text(command) for command in commands)
    check("正式配置通过本机文件存在性校验", config.host == "127.0.0.1")
    check("安装命令使用唯一windows_service_host入口", "windows_service_host.py" in rendered)
    check("安装命令没有foreground降级参数", "--foreground" not in rendered)
    check("安装计划不包含代理或凭据", all(
        token not in rendered.lower()
        for token in ("http_proxy", "https_proxy", "api_key", "api_secret", "token")
    ))

    print("\n[2] 托管边界fail-closed")
    public = write_config(tmp / "public", host="0.0.0.0")
    try:
        load_windows_service_config(public, project_root=public.parents[1])
        public_blocked = False
    except ValueError:
        public_blocked = True
    check("非回环绑定被拒绝", public_blocked)

    no_auto = write_config(tmp / "no-auto", no_autostart=True)
    try:
        load_windows_service_config(no_auto, project_root=no_auto.parents[1])
        no_auto_blocked = False
    except ValueError:
        no_auto_blocked = True
    check("正式配置不得关闭后台循环", no_auto_blocked)

    outside = write_config(tmp / "outside", log_path="logs/service.log")
    try:
        load_windows_service_config(outside, project_root=outside.parents[1])
        outside_blocked = False
    except ValueError:
        outside_blocked = True
    check("日志路径必须位于data目录", outside_blocked)

    sensitive = write_config(tmp / "sensitive", **{"api_" + "secret": "should-not-exist"})
    try:
        load_windows_service_config(sensitive, project_root=sensitive.parents[1])
        sensitive_blocked = False
    except ValueError:
        sensitive_blocked = True
    check("凭据字段被拒绝", sensitive_blocked)

    long_stop = write_config(tmp / "long-stop", stop_timeout_seconds=121)
    try:
        load_windows_service_config(long_stop, project_root=long_stop.parents[1])
        long_stop_blocked = False
    except ValueError:
        long_stop_blocked = True
    check("停机时限上界被校验", long_stop_blocked)

    wrong_entry = write_config(tmp / "wrong-entry", entrypoint="scripts/other.py")
    try:
        load_windows_service_config(wrong_entry, project_root=wrong_entry.parents[1])
        wrong_entry_blocked = False
    except ValueError:
        wrong_entry_blocked = True
    check("非统一Web入口被拒绝", wrong_entry_blocked)

    print("\n[3] 解释器解析（KI02：可注入/可回退）与配置不完整时安装前失败")
    # KI02 回归：配置里的 `.venv/...` 是**相对代码根**的路径。把同一份配置放到
    # 没有 `.venv` 的目录（worktree / 只读发布目录 / CI 检出）时，旧实现直接
    # ValueError 并中止整条离线回归。现在改为"可注入 + 可回退 + 记录来源"：
    # 回退成功但必须能被审查看到（source != "config"）。
    missing = write_config(tmp / "missing")
    interpreter = missing.parents[1] / ".venv" / "Scripts" / "python.exe"
    interpreter.unlink()
    fallback = load_windows_service_config(
        missing, project_root=missing.parents[1], require_files=True)
    check("解释器缺失时回退而不是硬失败（KI02）", fallback.python_executable.is_file())
    check("回退来源被记录（非 config）", fallback.python_executable_from_fallback
          and fallback.python_executable_source != "config")
    injected = load_windows_service_config(
        missing, project_root=missing.parents[1], require_files=True,
        python_executable=sys.executable)
    check("解释器可显式注入", injected.python_executable == Path(sys.executable).resolve()
          and injected.python_executable_source == "argument")
    try:
        load_windows_service_config(missing, project_root=missing.parents[1],
                                    require_files=True, strict_python=True)
        strict_blocked = False
    except ValueError:
        strict_blocked = True
    check("strict 模式下解释器缺失仍然失败（fail-closed 未被削弱）", strict_blocked)
    interpreter.write_bytes(b"fixture")
    declared = load_windows_service_config(
        missing, project_root=missing.parents[1], require_files=True)
    check("声明路径存在时来源为 config", declared.python_executable_source == "config"
          and declared.python_executable == interpreter.resolve())
    broken = load_windows_service_config(
        missing, project_root=missing.parents[1], require_files=False,
        python_executable="/nonexistent/python.exe")
    check("require-files=False 不做存在性解析（发布包语义不变）",
          broken.python_executable == interpreter.resolve())
    (missing.parents[1] / "scripts" / "run_web.py").unlink()
    try:
        load_windows_service_config(missing, project_root=missing.parents[1],
                                    require_files=True)
        entry_blocked = False
    except ValueError:
        entry_blocked = True
    check("入口缺失时require-files失败", entry_blocked)
    (missing.parents[1] / "scripts" / "run_web.py").write_text("# fixture", encoding="utf-8")

    print("\n[4] 日志达到上限时有限轮转")
    rotate_path = write_config(tmp / "rotate", log_max_bytes=1024 * 1024)
    rotate_config = load_windows_service_config(
        rotate_path, project_root=rotate_path.parents[1], require_files=False,
    )
    rotate_config.log_path.write_bytes(b"x" * (rotate_config.log_max_bytes + 1))
    original_stdout, original_stderr = sys.stdout, sys.stderr
    stream = _configure_log(rotate_config)
    sys.stdout, sys.stderr = original_stdout, original_stderr
    stream.close()
    check("超限日志滚动到编号备份", rotate_config.log_path.with_name(
        f"{rotate_config.log_path.name}.1").is_file())
    check("轮转后重新打开当前日志", rotate_config.log_path.is_file()
          and rotate_config.log_path.stat().st_size == 0)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n结果: {passed} 通过 / 0 失败")
