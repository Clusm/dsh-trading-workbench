"""独立只读健康监控：纯判定 + 只读探针（O-01 核心逻辑，不持有交易私钥）。

本模块是「独立健康观察进程」的核心。它不构造 ``TradingService``、不导入任何交易所 /
Broker / LLM 客户端、不读取也不使用 ``BINANCE_*`` 等交易密钥，不写任何状态文件或 JSON。
所有判定都基于两类只读探针：

1. 单实例锁 ``data/trading_service.lock`` 的 PID 存活（复用 ``core.instance_lock`` 的同一
   存活判定：只有 PID 存活时才认为托管进程还在）。
2. 审计库 ``release_observations`` / ``protection_reconciliations`` 的最新证据
   （用 ``mode=ro`` 打开并强制 ``query_only``，能读取运行中 WAL 的最新提交）。

时效原因码严格复用 ``dashboard.health.freshness_health_reasons`` 的命名
（``fast_cycle_error`` / ``fast_cycle_stale`` / ``fast_cycle_never_completed`` /
``protection_reconcile_stale`` / ``protection_reconcile_never_completed`` /
``protection_reconcile_error``），阈值来自 ``config/settings.yaml`` 的 ``stability`` 段。

新增的两类异常（进程消失、磁盘不可用）使用 ``process_missing`` / ``disk_unavailable``；
监控证据流自身过期（服务挂起但未释放锁）用 ``observation_stale``，属于「数据过期」类别。

告警通道：仅复用项目里「已批准」的外部通道；未配置时明确为 ``unverified``，绝不假装
无人值守告警已完成。
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 这两个导入都不触碰任何交易密钥，也不构造交易服务：
# - core.instance_lock 仅用其 PID 存活判定（与实例锁同语义）。
# - dashboard.health 提供时效原因码与阈值口径（与看板同一套）。
from core.instance_lock import InstanceLock
from dashboard.health import freshness_health_reasons


def _json_loads(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    try:
        loaded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(loaded) if isinstance(loaded, dict) else {}


def _parse_iso(value: Any) -> float | None:
    """解析 ISO 时间并返回 UTC 时间戳；坏值返回 None。"""
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).timestamp()


@dataclass
class HealthWatchConfig:
    """监控配置：全部来自 ``config/settings.yaml``，不引入任何密钥字段。"""

    project_root: Path
    profile: str | None = None  # None / "demo" / "live"
    startup_grace_seconds: float = 120.0
    fast_cycle_stale_seconds: float = 120.0
    protection_reconcile_stale_seconds: float = 120.0
    # 证据流（release_observations）超过该秒数无新写入即视为过期；覆盖
    # 「服务进程存活但停止写入证据」的场景。默认 300s（快循环 30s 的 10 倍余量）。
    observation_stale_seconds: float = 300.0
    environment: str = "demo"
    alert_channel: str = "unverified"
    alert_channel_status: str = "unverified"
    alert_channel_note: str = ""

    @property
    def data_root(self) -> Path:
        base = self.project_root / "data"
        if self.profile == "live":
            return base / "live"
        return base

    @property
    def db_file(self) -> Path:
        return self.data_root / "agent_trader.sqlite"

    @property
    def lock_file(self) -> Path:
        return self.data_root / "trading_service.lock"

    @classmethod
    def from_settings(
        cls, project_root: Path, *, profile: str | None = None,
        observation_stale_seconds: float | None = None,
        raw_settings: dict[str, Any] | None = None,
    ) -> "HealthWatchConfig":
        """从 ``config/settings.yaml`` 只读构建；不调用 load_settings（避免 ensure_dirs 写盘）。"""
        root = Path(project_root)
        settings = dict(raw_settings or {})
        if not settings:
            try:
                text = (root / "config" / "settings.yaml").read_text(encoding="utf-8")
                try:
                    import yaml

                    loaded = yaml.safe_load(text)
                except ImportError:
                    loaded = json.loads(text)
                settings = dict(loaded) if isinstance(loaded, dict) else {}
            except Exception:
                settings = {}
        stability = settings.get("stability", {}) or {}
        resolved_profile = profile if profile is not None else (
            "live" if str(settings.get("profile")) == "live" else None)
        environment = resolved_profile or str(settings.get("execution", {}).get("default_mode", "demo") or "demo")
        channel, status, note = resolve_alert_channel(settings)
        return cls(
            project_root=root,
            profile=resolved_profile,
            startup_grace_seconds=float(stability.get("startup_grace_seconds", 120)),
            fast_cycle_stale_seconds=float(stability.get("fast_cycle_stale_seconds", 120)),
            protection_reconcile_stale_seconds=float(
                stability.get("protection_reconcile_stale_seconds", 120)),
            observation_stale_seconds=(
                float(observation_stale_seconds)
                if observation_stale_seconds is not None else 300.0),
            environment=str(environment).lower(),
            alert_channel=channel,
            alert_channel_status=status,
            alert_channel_note=note,
        )


def resolve_alert_channel(raw_settings: dict[str, Any]) -> tuple[str, str, str]:
    """返回 ``(channel, status, note)``。

    仅当 settings 中明确声明「已批准」的外部通道时才视为 verified；否则一律
    ``unverified``，并明确说明无人值守告警尚未完成验证。绝不自行发明 webhook / 邮箱。
    """
    alerting = (raw_settings or {}).get("alerting")
    if isinstance(alerting, dict) and alerting.get("verified") and alerting.get("provider"):
        provider = str(alerting["provider"])
        return provider, "verified", f"已批准外部告警通道: {provider}"
    return (
        "unverified",
        "unverified",
        "未验证：未配置已批准的外部告警通道，无人值守告警尚未完成验证",
    )


def open_read_only(db_file: Path) -> sqlite3.Connection:
    """以 ``mode=ro`` 只读打开数据库，并能看到运行中 WAL 的最新提交。

    不能使用 ``immutable=1``：它会跳过 WAL，在服务运行时只能看到较旧的主库页，
    从而把健康实例误报为 ``observation_stale``。``mode=ro`` 禁止写入，
    ``PRAGMA query_only=ON`` 再做一层保护；活跃服务已持有 WAL/SHM，读者只参与现有快照。
    """
    resolved = Path(db_file).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"database not found: {resolved}")
    connection = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


# ---------------------------------------------------------------------------
# 只读探针（都不写任何文件）
# ---------------------------------------------------------------------------

def probe_instance_lock(lock_file: Path) -> dict[str, Any]:
    """读取单实例锁的 PID 存活；复用 InstanceLock 的同一判定。"""
    if not lock_file.is_file():
        return {"present": False, "pid": None, "alive": None, "started_at": None}
    try:
        data = json.loads(lock_file.read_text(encoding="utf-8"))
        pid = int(data.get("pid") or 0)
    except Exception:
        return {"present": True, "pid": None, "alive": None,
                "started_at": None, "error": "lock_unreadable"}
    alive = InstanceLock._pid_alive(pid) if pid > 0 else False
    return {"present": True, "pid": pid, "alive": bool(alive),
            "started_at": _parse_iso(data.get("started_at"))}


def probe_disk(data_root: Path, db_file: Path) -> dict[str, Any]:
    """磁盘不可用：数据目录不可访问，或数据库无法以只读方式打开。"""
    try:
        if not data_root.exists():
            return {"available": False, "detail": "data directory missing"}
        if db_file.exists():
            conn = open_read_only(db_file)
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()
        return {"available": True, "detail": "ok"}
    except Exception as exc:
        return {"available": False, "detail": f"{type(exc).__name__}: {exc}"}


def probe_latest_observation(
    conn: sqlite3.Connection, environment: str | None = None,
) -> dict[str, Any] | None:
    """读取最新一条运行证据（observed_at / 快循环状态 / 就绪快照）。"""
    try:
        if environment:
            row = conn.execute(
                "SELECT observed_at, component_status_json, entry_readiness_json, environment "
                "FROM release_observations WHERE environment=? "
                "ORDER BY observed_at DESC LIMIT 1",
                (str(environment).lower(),),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT observed_at, component_status_json, entry_readiness_json, environment "
                "FROM release_observations ORDER BY observed_at DESC LIMIT 1"
            ).fetchone()
    except Exception:
        return None
    if not row:
        return None
    component = _json_loads(row["component_status_json"])
    readiness = _json_loads(row["entry_readiness_json"])
    return {
        "observed_at": _parse_iso(row["observed_at"]),
        "fast_cycle": str(component.get("fast_cycle") or ""),
        "component_status": component,
        "entry_readiness": readiness,
        "environment": str(row["environment"] or ""),
    }


def probe_protection(conn: sqlite3.Connection, environment: str) -> dict[str, Any]:
    """读取最新一次保护单对账事实（时间戳 / 是否匹配 / 错误）。"""
    try:
        row = conn.execute(
            "SELECT checked_at, matched, details_json FROM protection_reconciliations "
            "WHERE environment=? ORDER BY id DESC LIMIT 1",
            (environment,),
        ).fetchone()
    except Exception:
        return {"last_reconcile_at": None, "matched": None, "error": None}
    if not row:
        return {"last_reconcile_at": None, "matched": None, "error": None}
    details = _json_loads(row["details_json"])
    err = details.get("error") or details.get("exception")
    return {
        "last_reconcile_at": _parse_iso(row["checked_at"]),
        "matched": bool(int(row["matched"] or 0)),
        "error": str(err) if err else None,
    }


# ---------------------------------------------------------------------------
# 原因码派生（严格复用 dashboard.health 命名 + stability 阈值）
# ---------------------------------------------------------------------------

def _fast_cycle_reasons(obs: dict[str, Any] | None) -> list[str]:
    """快循环长期无完成：直接复用 service 已用 stability 阈值算出的 verdict。

    原因码与 ``dashboard.health.freshness_health_reasons`` 完全一致：
    ``fast_cycle_never_completed`` / ``fast_cycle_stale`` / ``fast_cycle_error``。
    """
    if obs is None:
        return []
    state = obs.get("fast_cycle", "")
    if state == "missing":
        return ["fast_cycle_never_completed"]
    if state == "stale":
        return ["fast_cycle_stale"]
    if state == "degraded":
        return ["fast_cycle_error"]
    return []


def _protection_reasons(conn: sqlite3.Connection, environment: str,
                         config: HealthWatchConfig, now: float,
                         service_started_at: float | None = None) -> list[str]:
    """数据过期（保护单对账）：复用 ``freshness_health_reasons`` 与
    ``protection_reconcile_stale_seconds`` 阈值。paper 环境无交易所镜像，恒为已验证，
    不触发（与 service._mirror_fact 对 paper 返回 True 一致）。
    """
    if environment == "paper":
        return []
    protection = probe_protection(conn, environment)
    stability = {
        "startup_grace_seconds": config.startup_grace_seconds,
        "fast_cycle_stale_seconds": config.fast_cycle_stale_seconds,
        "protection_reconcile_stale_seconds": config.protection_reconcile_stale_seconds,
    }
    protection_dict = {
        "enabled": True,
        "read_enabled": True,
        "last_reconcile_at": protection["last_reconcile_at"],
        "last_error": protection["error"],
    }
    # 有可验证的进程启动时间时复用 Dashboard 的启动宽限；只有锁文件缺失/损坏时
    # 才按长期运行处理。这样刚启动、首轮保护对账尚未完成时不会误发故障告警。
    started_at = service_started_at
    if started_at is None:
        started_at = now - max(3600.0, 10.0 * config.startup_grace_seconds)
    runtime = {"started_at": started_at}
    reasons = freshness_health_reasons(runtime, protection_dict, stability, now=now)
    return [r for r in reasons if r.startswith("protection_reconcile_")]


# ---------------------------------------------------------------------------
# 顶层判定
# ---------------------------------------------------------------------------

@dataclass
class HealthResult:
    healthy: bool
    reasons: list[str] = field(default_factory=list)
    categories: dict[str, list[str]] = field(default_factory=dict)
    process: dict[str, Any] = field(default_factory=dict)
    observed_at: float | None = None
    disk: dict[str, Any] = field(default_factory=dict)
    environment: str = "demo"
    profile: str | None = None
    alert_channel: str = "unverified"
    alert_channel_status: str = "unverified"
    alert_channel_note: str = ""
    read_only: bool = True
    generated_at: float = field(default_factory=lambda: time.time())

    def to_dict(self) -> dict[str, Any]:
        return {
            "healthy": self.healthy,
            "reasons": list(self.reasons),
            "categories": {k: list(v) for k, v in self.categories.items()},
            "process": self.process,
            "observed_at": self.observed_at,
            "disk": self.disk,
            "environment": self.environment,
            "profile": self.profile,
            "alert_channel": self.alert_channel,
            "alert_channel_status": self.alert_channel_status,
            "alert_channel_note": self.alert_channel_note,
            "read_only": self.read_only,
            "generated_at": self.generated_at,
        }


def evaluate_health(config: HealthWatchConfig, *, now: float | None = None) -> HealthResult:
    """执行一次只读健康评估，返回结构化结果。不写任何文件。"""
    now = float(now if now is not None else time.time())
    categories: dict[str, list[str]] = {}
    observed_at: float | None = None

    disk = probe_disk(config.data_root, config.db_file)
    if not disk["available"]:
        categories["disk_unavailable"] = ["disk_unavailable"]

    proc = probe_instance_lock(config.lock_file)
    if not proc["present"] or proc.get("alive") is False:
        categories["process_missing"] = ["process_missing"]
    elif proc.get("alive") is not True:
        # 锁损坏或 PID 无法解析时不能把进程状态当成健康；与确知进程消失区分原因码。
        categories["process_unverified"] = ["process_unverified"]

    if disk["available"]:
        try:
            conn = open_read_only(config.db_file)
        except Exception as exc:
            conn = None
            categories.setdefault("disk_unavailable", []).append("disk_unavailable")
            disk = {"available": False, "detail": f"{type(exc).__name__}: {exc}"}
        if conn is not None:
            try:
                obs = probe_latest_observation(conn, config.environment)
                if obs is not None:
                    observed_at = obs.get("observed_at")

                started_at = proc.get("started_at") if proc.get("alive") is True else None
                startup_age = (
                    now - float(started_at)
                    if isinstance(started_at, (int, float)) else None
                )
                in_startup_grace = bool(
                    startup_age is not None
                    and 0.0 <= startup_age <= max(0.0, config.startup_grace_seconds)
                )

                fc = _fast_cycle_reasons(obs)
                if fc:
                    categories["fast_cycle"] = fc

                protection = probe_protection(conn, config.environment)
                de: list[str] = list(
                    _protection_reasons(
                        conn, config.environment, config, now,
                        service_started_at=(float(started_at) if started_at is not None else None),
                    ))
                # 证据流自身过期（独立探针）：服务挂起但未释放锁也会落入此分支。
                if obs is None and not in_startup_grace:
                    de.append("observation_stale")
                elif observed_at is not None and (now - observed_at) > config.observation_stale_seconds:
                    de.append("observation_stale")
                if de:
                    categories["data_expired"] = de
                if config.environment != "paper" and protection.get("matched") is False:
                    categories["protection"] = ["protection_mismatch"]
            finally:
                conn.close()

    reasons: list[str] = []
    for codes in categories.values():
        reasons.extend(codes)
    reasons = list(dict.fromkeys(reasons))

    return HealthResult(
        healthy=len(reasons) == 0,
        reasons=reasons,
        categories=categories,
        process=proc,
        observed_at=observed_at,
        disk=disk,
        environment=config.environment,
        profile=config.profile,
        alert_channel=config.alert_channel,
        alert_channel_status=config.alert_channel_status,
        alert_channel_note=config.alert_channel_note,
    )
