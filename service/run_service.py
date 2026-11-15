#!/usr/bin/env python
"""服务启动入口（M1.1）。

用法：
    python run_service.py --port 8791
    python run_service.py --host 127.0.0.1 --port 8791 --data-dir ../data

启动前做五道本地检查（不联网）：
1. 配置合法（loopback、端口、探针参数、auth mode）；
2. demo-only 守卫（行情基址必须是 demo 域名）；
3. 鉴权 token：env ``WORKBENCH_API_TOKEN`` > ``data/.api-token`` > 首次生成；
4. 单实例锁（``data/service.lock``：pid/start_at + OS 文件锁 + 存活校验，t12 F-05）；
5. 端口可用（占用则退出码 4，docs/01 §10.4 不自动换端口）。

退出码：0 正常；2 配置/守卫/鉴权失败；3 已有实例在运行；4 端口占用。
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import platform
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import uvicorn

from workbench_service.app import configure_logging, create_app
from workbench_service.config import (
    AUTH_MODE_BEARER,
    AUTH_MODE_DEV_OPEN,
    SERVICE_NAME,
    SERVICE_VERSION,
    AuthTokenError,
    Settings,
    resolve_runtime_auth,
)
from workbench_service.guard import evaluate_demo_guard
from workbench_service.security import install_health_auth_view

logger = logging.getLogger("workbench.run")

EXIT_CONFIG = 2
EXIT_ALREADY_RUNNING = 3
EXIT_PORT_IN_USE = 4

LOCK_FILENAME = "service.lock"
_WINDOWS_STILL_ACTIVE = 259
#: Windows 字节锁偏移：锁在 1 MiB 处，避免锁住 JSON 内容导致其它进程无法读取 holder 信息。
_WINDOWS_LOCK_OFFSET = 1024 * 1024


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _pid_alive(pid: int) -> bool:
    """判断进程是否存活（Windows 用 OpenProcess，POSIX 用 signal 0）。"""

    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        process_query_limited_information = 0x1000
        handle = kernel32.OpenProcess(process_query_limited_information, 0, int(pid))
        if not handle:
            return False
        try:
            exit_code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return exit_code.value == _WINDOWS_STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _try_os_lock(fh: Any) -> bool:
    """尝试对文件加非阻塞独占锁；进程崩溃时由 OS 自动释放。"""

    if os.name == "nt":
        import msvcrt

        try:
            fh.seek(_WINDOWS_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False

    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _release_os_lock(fh: Any) -> None:
    try:
        if os.name == "nt":
            import msvcrt

            fh.seek(_WINDOWS_LOCK_OFFSET)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


def _read_lock_payload(fh: Any) -> dict[str, Any]:
    try:
        fh.seek(0)
        raw = fh.read(4096)
        text = raw.decode("utf-8", errors="replace").strip()
        if not text:
            return {}
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class InstanceLock:
    """``data/service.lock`` 单实例锁。

    - 内容：``{service, version, pid, started_at, host, port}``，退出时追加 ``released_at``；
    - 互斥：Windows ``msvcrt.locking`` / POSIX ``fcntl.flock``，进程死亡由 OS 释放，
      不会因为崩溃留下“永远锁死”的文件；
    - 诊断：获取失败时返回 holder 的 pid/start_at 与存活判定。
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._fh: Any = None

    def acquire(self, *, host: str = "", port: int = 0) -> tuple[bool, dict[str, Any]]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+b")
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"0")
                fh.flush()

            # 先在加锁前读取 holder 信息：失败时另一个句柄持锁，读锁区可能被拒绝。
            existing = _read_lock_payload(fh)
            if not _try_os_lock(fh):
                fh.close()
                return False, existing

            payload = {
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "pid": os.getpid(),
                "started_at": _utc_now_iso(),
                "host": host,
                "port": port,
            }
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
            self._fh = fh
            return True, payload
        except Exception:
            fh.close()
            raise

    def release(self) -> None:
        fh = self._fh
        if fh is None:
            return
        self._fh = None
        try:
            payload = {
                "service": SERVICE_NAME,
                "version": SERVICE_VERSION,
                "pid": os.getpid(),
                "started_at": _read_lock_payload(fh).get("started_at"),
                "released_at": _utc_now_iso(),
            }
            fh.seek(0)
            fh.truncate()
            fh.write(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        except OSError:
            pass
        finally:
            _release_os_lock(fh)
            fh.close()


def _port_is_free(host: str, port: int) -> bool:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        try:
            sock.bind((host, port))
        except OSError:
            return False
    return True


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run_service.py",
        description="trading-workbench M1.0 service (health/ready + read-only market probe)",
    )
    parser.add_argument("--host", default=None, help="loopback host (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="port (default 8791)")
    parser.add_argument("--data-dir", default=None, help="runtime data dir (default trading-workbench/data)")
    parser.add_argument(
        "--log-level",
        default=None,
        choices=["debug", "info", "warning", "error", "critical"],
        help="log level (default info)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)

    bootstrap_level = (args.log_level or os.environ.get("WORKBENCH_LOG_LEVEL") or "info").lower()
    configure_logging(bootstrap_level)

    try:
        settings = Settings.from_env(
            host=args.host,
            port=args.port,
            data_dir=args.data_dir,
            log_level=args.log_level,
            default_auth_mode=AUTH_MODE_BEARER,
        )
    except ValueError as exc:
        logger.critical("config_invalid error=%s", exc)
        return EXIT_CONFIG

    logger.info(
        "bootstrap service=%s version=%s schema=%s python=%s platform=%s",
        SERVICE_NAME,
        SERVICE_VERSION,
        settings.schema_version,
        sys.version.split()[0],
        platform.platform(terse=True),
    )
    logger.info(
        "config host=%s port=%s data_dir=%s db=%s auth=%s token_ready=%s probe=symbol:%s interval:%s limit:%s timeout:%ss",
        settings.host,
        settings.port,
        settings.data_dir,
        settings.db_path,
        settings.auth_mode,
        settings.token_ready,
        settings.probe_symbol,
        settings.probe_interval,
        settings.probe_limit,
        settings.probe_timeout_s,
    )

    log_path = configure_logging(settings.log_level, log_dir=settings.logs_dir)
    if log_path is not None:
        logger.info("log_file path=%s max_bytes=10485760 backups=5", log_path)

    guard = evaluate_demo_guard(settings.demo_base_url)
    if not guard.ok:
        logger.critical(
            "demo_guard_failed reason=%s detail=%s base_url=%s",
            guard.reason_code,
            guard.detail,
            guard.base_url,
        )
        return EXIT_CONFIG
    logger.info("demo_guard_ok reason=%s base_url=%s", guard.reason_code, guard.base_url)

    # 注意：``resolve_runtime_auth`` 会读取/首次生成 ``data/.api-token``，
    # 必须放在单实例锁之后，避免两个进程并发启动时同时生成 token（t22 M01）。
    lock = InstanceLock(settings.data_dir / LOCK_FILENAME)
    acquired, lock_payload = lock.acquire(host=settings.host, port=settings.port)
    if not acquired:
        holder_pid = lock_payload.get("pid")
        holder_alive = _pid_alive(int(holder_pid)) if isinstance(holder_pid, int) else None
        logger.error(
            "E_ALREADY_RUNNING lock=%s holder_pid=%s holder_started_at=%s holder_alive=%s",
            lock.path,
            holder_pid,
            lock_payload.get("started_at"),
            holder_alive,
        )
        return EXIT_ALREADY_RUNNING
    logger.info(
        "instance_lock_acquired path=%s pid=%s started_at=%s",
        lock.path,
        os.getpid(),
        lock_payload.get("started_at"),
    )

    try:
        if not _port_is_free(settings.host, settings.port):
            logger.error(
                "E_PORT_IN_USE host=%s port=%s (docs/01 §10.4: do not auto-switch ports)",
                settings.host,
                settings.port,
            )
            return EXIT_PORT_IN_USE

        # 鉴权解析（可能首次生成 data/.api-token）在实例锁内进行 → 并发启动被串行化。
        try:
            settings = resolve_runtime_auth(settings)
        except (AuthTokenError, ValueError, OSError) as exc:
            logger.critical("auth_config_failed error=%s", exc)
            return EXIT_CONFIG
        if settings.auth_mode == AUTH_MODE_BEARER:
            logger.info(
                "auth_ready mode=bearer token_ready=%s source=%s path=%s",
                settings.token_ready,
                settings.auth_token_source,
                settings.api_token_path,
            )
        else:
            logger.warning(
                "auth_ready mode=dev-open (explicit WORKBENCH_AUTH_MODE=dev-open); "
                "loopback-only, no bearer required"
            )

        app = create_app(settings)
        if not install_health_auth_view(app):
            # /health 缺 auth/lock 投影意味着鉴权面装配失败，必须拒绝启动。
            logger.critical(
                "health_auth_projection_failed: /health route missing; refusing to start"
            )
            return EXIT_CONFIG
        logger.info(
            "listening url=http://%s:%s (loopback only) venue=demo signed=demo-only",
            settings.host,
            settings.port,
        )
        uvicorn.run(
            app,
            host=settings.host,
            port=settings.port,
            log_level=settings.log_level,
            access_log=False,
            server_header=False,
            proxy_headers=False,
        )
        return 0
    finally:
        lock.release()
        logger.info("instance_lock_released path=%s", lock.path)


if __name__ == "__main__":
    raise SystemExit(main())
