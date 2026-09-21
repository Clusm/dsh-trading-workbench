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

U14（F06/F12/F22、R13/R14）在此文件上的接线有三条，都是**加法**（不改变既有原因码
与判定顺序；未声明状态根时路径逐字节兼容）：

1. **路径单一事实源**：``data_root`` 改由 :class:`core.runtime_paths.RuntimePaths`
   解析（消费清单里的 ``health_watch_state`` 由 ``planned`` 变 ``migrated``）。
   未声明状态根（无 ``MTA_STATE_ROOT``/``paths.state_root``）时结果与升级前完全相同。
2. **交易证据时间**（R13）：新增探针 :func:`probe_trading_evidence`，只读汇总
   ``release_observations``/``execution_fills``/``protection_reconciliations`` 的
   最新持久化时刻，交给 :func:`core.ops.alert_delivery.classify_outage` 与
   "心跳进程活着" 一起判别，避免把进程存活当交易健康。
3. **两类失联成因分开**：``process_missing``（本机主进程故障，宿主一直在运行）与
   新增的 ``host_down``（证据缺口跨越了宿主重启/断电）由 host uptime 与证据年龄
   的判别式分开给出，两者**不**混为一类；无法证明宿主连续性时给
   ``host_continuity_unknown``（不猜）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# 这三个导入都不触碰任何交易密钥，也不构造交易服务：
# - core.instance_lock 仅用其 PID 存活判定（与实例锁同语义）。
# - dashboard.health 提供时效原因码与阈值口径（与看板同一套）。
# - core.ops.alert_delivery（U14 独占）提供交易证据读取与"主进程故障 vs 主机关机"
#   的成因判别（只读纯函数 + 只读 SQL，不写任何文件）；core.runtime_paths 提供
#   与 U13 一致的路径解析（换代码根不改状态根）。
from core.instance_lock import InstanceLock
from core.ops.alert_delivery import (
    HOST_CONTINUITY_UNKNOWN,
    HOST_DOWN,
    HOST_JUST_BOOTED,
    HOST_REBOOT_OBSERVED,
    MANUAL_ACK_ONLY,
    NO_CONFIRMED_DELIVERY,
    OUTAGE_HEALTHY,
    PROCESS_MISSING,
    TRADING_STALLED,
    TradingEvidence,
    classify_outage,
    host_uptime_seconds,
    trading_evidence_timestamp,
)
from core.runtime_paths import RuntimePaths, resolve_state_base
from dashboard.health import freshness_health_reasons

#: U14 接线标记：下游（含 U14 验收套件）据此判断共享补丁是否已应用，
#: 从而在"作者 worktree"与"集成树"两态下各自断言各自应有的行为。
U14_INTEGRATION = "u14.health_watch.v1"

#: U09 接线标记：发布证据需要「发布观察窗口事实」与「外部告警真实回执」两个只读
#: 探针（:func:`release_observation_window` / :func:`alert_receipt_truth`）。
#: 两者都是**加法**：不改变既有原因码、判定顺序与阈值；未应用时
#: ``core.release_evidence`` 把对应证据块标 ``NO_DATA`` + ``U09_SHARED_PATCH_MISSING``，
#: 不自行复制一份判定实现。
U09_INTEGRATION = "u09.health_watch.v1"

#: 失联成因 → ``HealthResult.categories`` 里的类别名。
#: 类别名与原因码沿用既有小写风格（``process_missing`` / ``disk_unavailable``）；
#: ``PROCESS_MISSING`` 与 ``OUTAGE_HEALTHY`` 映射到既有语义（前者已由
#: ``process_missing`` 表达、后者表示"无新增异常"），因此**不新增**原因码，
#: 既有 smoke 的期望集合保持不变。
OUTAGE_CATEGORY_NAMES: dict[str, str] = {
    PROCESS_MISSING: "",
    OUTAGE_HEALTHY: "",
    HOST_DOWN: "host_down",
    TRADING_STALLED: "trading_stalled",
    HOST_CONTINUITY_UNKNOWN: HOST_CONTINUITY_UNKNOWN,
    HOST_JUST_BOOTED: "host_booted",
}


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
    #: 显式声明的状态基座（``MTA_STATE_ROOT`` / ``paths.state_root`` 解析结果）。
    #: 空串 = 未声明 → 退回 ``<project_root>/data``（升级前的行为，逐字节兼容）。
    state_root_declared: str = ""

    @property
    def state_root_source(self) -> str:
        """``declared``（换代码根不改状态根）或 ``fallback``（旧布局）。"""
        return "declared" if self.state_root_declared.strip() else "fallback"

    @property
    def paths(self) -> RuntimePaths:
        """RuntimePaths 单一事实源（U13/U14 共用同一解析口径）。

        ``env={}``：本类**不**隐式读 ``os.environ``；环境注入只在
        :meth:`from_settings`（真实入口）里发生一次，避免直接构造
        ``HealthWatchConfig(...)`` 的调用方被环境变量悄悄改变行为。
        """
        return RuntimePaths.resolve(
            self.project_root, profile=self.profile,
            state_root=(self.state_root_declared or None), env={})

    @property
    def data_root(self) -> Path:
        """状态根：由 RuntimePaths 解析，未声明时 == 旧的 ``<project_root>/data[/live]``。"""
        return self.paths.state_root

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
        state_root: str | None = None,
    ) -> "HealthWatchConfig":
        """从 ``config/settings.yaml`` 只读构建；不调用 load_settings（避免 ensure_dirs 写盘）。

        ``state_root`` 是显式注入点（``--state-root`` / ``paths.state_root`` /
        ``MTA_STATE_ROOT`` 三者的统一解析见 :func:`resolve_state_base`）。
        """
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
        # 状态根解析：显式形参 > settings.paths.state_root > 环境变量 > 旧布局。
        # 判定委托 resolve_state_base（U13 的唯一口径），这里只把"是否显式声明"
        # 转成 state_root_declared，供 state_root_source 如实报告。
        settings_declared = str((settings.get("paths") or {}).get("state_root") or "")
        declared_input = state_root if state_root is not None else (settings_declared or None)
        _base, declared_value, origin = resolve_state_base(
            root, declared=declared_input, env=os.environ)
        declared = declared_value if origin == "declared" else ""
        return cls(
            project_root=root,
            profile=resolved_profile,
            state_root_declared=declared,
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


def probe_trading_evidence(
    conn: sqlite3.Connection | None, environment: str,
) -> TradingEvidence:
    """只读读取"交易证据时间"（R13：心跳必须携带交易证据时间）。

    只发 ``SELECT MAX(...)``，不写任何东西。读不到（表缺失、主库损坏 F22）时
    返回 ``available=False``——调用方据此把"交易证据缺失"如实报出来，而不是
    把"心跳进程活着"当成交易健康。
    """
    if conn is None:
        return TradingEvidence(False, None, {}, ("TRADING_DB_UNAVAILABLE",))
    return trading_evidence_timestamp(conn, environment=environment)


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
# U09 只读探针：发布观察窗口事实 / 外部告警真实回执（都不写任何文件）
# ---------------------------------------------------------------------------

RELEASE_WINDOW_SINGLE_SESSION = "SINGLE_SESSION"
RELEASE_WINDOW_RESTART_SAME_ARTIFACT = "RESTART_SAME_ARTIFACT"
RELEASE_WINDOW_CONFIG_CHANGED = "CONFIG_CHANGED_WITHIN_WINDOW"
RELEASE_WINDOW_NO_DATA = "NO_DATA"
CODE_OBSERVATION_TABLE_UNAVAILABLE = "OBSERVATION_TABLE_UNAVAILABLE"
CODE_ALERT_MIRROR_TABLE_MISSING = "ALERT_MIRROR_TABLE_MISSING"


def _table_names(conn: sqlite3.Connection) -> set[str]:
    try:
        return {str(row[0]) for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    except Exception:
        return set()


def release_observation_window(
    conn: sqlite3.Connection, *, release_id: str, environment: str,
    account_ref: str | None = None, declared_start: Any = None, declared_end: Any = None,
) -> dict[str, Any]:
    """返回发布观察窗口的**事实**（U09 只读探针）。

    职责边界：本函数只报事实，**不替调用方决定窗口边界**、不写任何状态、
    也绝不把"第一条/最后一条观察"当成窗口起点（规格 §12 明令禁止自动截断）。
    ``declared_start``/``declared_end`` 是调用方声明的窗口，用来计算
    ``head_gap_minutes``/``tail_gap_minutes``（**完全没有观察行**的断档分钟）；
    与"有观察行但不健康"是两件事，不得混为一谈。

    ``session_id`` 与 ``config_hash`` 用于区分「同一工件的进程重启」与
    「不同 RC/配置的换版」：多 session + 单一 config_hash 只是重启，不是换版
    （F24）。

    Returns:
        含 ``observed_first``/``observed_last``/``session_count``/``config_hashes``/
        ``continuity``/``head_gap_minutes``/``tail_gap_minutes``/``foreign_release_rows``
        的字典；证据不可用时 ``state=NO_DATA``（不猜、不回落）。
    """
    facts: dict[str, Any] = {
        "state": "NO_DATA",
        "release_id": str(release_id),
        "environment": str(environment).lower(),
        "observed_first": None,
        "observed_last": None,
        "observation_rows": 0,
        "observed_minute_count": 0,
        "healthy_minute_count": 0,
        "sessions": [],
        "session_count": 0,
        "config_hashes": [],
        "continuity": RELEASE_WINDOW_NO_DATA,
        "declared_start": declared_start,
        "declared_end": declared_end,
        "declared": bool(declared_start and declared_end),
        "head_gap_minutes": None,
        "tail_gap_minutes": None,
        "foreign_release_rows": 0,
        "window_derived_from_data": False,
        "reason_codes": [],
    }
    tables = _table_names(conn)
    if "release_observations" not in tables:
        facts["reason_codes"] = [CODE_OBSERVATION_TABLE_UNAVAILABLE]
        return facts
    clauses = ["release_id=?", "environment=?"]
    params: list[Any] = [str(release_id), str(environment).lower()]
    if account_ref:
        clauses.append("account_ref=?")
        params.append(str(account_ref))
    try:
        rows = conn.execute(
            "SELECT session_id, observed_at, observed_minute, health_state, config_hash "
            "FROM release_observations WHERE " + " AND ".join(clauses)
            + " ORDER BY observed_at", tuple(params)).fetchall()
    except Exception:
        facts["reason_codes"] = [CODE_OBSERVATION_TABLE_UNAVAILABLE]
        return facts

    stamps = [stamp for stamp in (_parse_iso(row["observed_at"]) for row in rows)
              if stamp is not None]
    sessions = sorted({str(row["session_id"]) for row in rows if str(row["session_id"] or "")})
    config_hashes = sorted({str(row["config_hash"]) for row in rows
                            if str(row["config_hash"] or "")})
    healthy_minutes = {str(row["observed_minute"]) for row in rows
                       if str(row["health_state"] or "").lower() == "healthy"}
    facts.update({
        "observation_rows": len(rows),
        "observed_first": min(stamps) if stamps else None,
        "observed_last": max(stamps) if stamps else None,
        "observed_minute_count": len({str(row["observed_minute"]) for row in rows}),
        "healthy_minute_count": len(healthy_minutes),
        "sessions": sessions,
        "session_count": len(sessions),
        "config_hashes": config_hashes,
    })
    if not rows:
        facts["reason_codes"] = ["NO_OBSERVATION_ROWS"]
        return facts
    if len(config_hashes) > 1:
        facts["continuity"] = RELEASE_WINDOW_CONFIG_CHANGED
        facts["reason_codes"] = ["MIXED_CONFIG_SAMPLES"]
    elif len(sessions) > 1:
        # 多 session 只说明进程重启过；工件/配置没变就不是换版。
        facts["continuity"] = RELEASE_WINDOW_RESTART_SAME_ARTIFACT
    else:
        facts["continuity"] = RELEASE_WINDOW_SINGLE_SESSION
    facts["state"] = "FAIL" if facts["continuity"] == RELEASE_WINDOW_CONFIG_CHANGED else "PASS"

    start = _parse_iso(declared_start)
    end = _parse_iso(declared_end)
    if start is not None and end is not None and stamps:
        first, last = min(stamps), max(stamps)
        facts["head_gap_minutes"] = max(0, int((first - start) // 60))
        facts["tail_gap_minutes"] = max(0, int((end - last) // 60))
        try:
            foreign = conn.execute(
                "SELECT COUNT(*) FROM release_observations "
                "WHERE environment=? AND observed_at>=? AND observed_at<=? "
                "AND release_id<>?",
                (str(environment).lower(), str(declared_start), str(declared_end),
                 str(release_id))).fetchone()[0]
            facts["foreign_release_rows"] = int(foreign or 0)
        except Exception:
            facts["foreign_release_rows"] = 0
    return facts


def alert_receipt_truth(
    conn: sqlite3.Connection, *, environment: str, account_ref: str | None = None,
) -> dict[str, Any]:
    """按**真实回执**统计外部告警送达真相（U09 只读探针，F12/K05）。

    判定只认证据：``delivered_at_ms`` **且** ``device_receipt`` 才算送达；
    供应商 ``provider_receipt``／``ACCEPTED`` 只是"上游接受了"，人工 ``ack_at_ms``
    只是"有人看到了"——两者都**不是**送达，更不是恢复。原因码复用 U14
    ``core.ops.alert_delivery`` 的 ``MANUAL_ACK_ONLY`` / ``NO_CONFIRMED_DELIVERY``，
    不在本层另造一套。
    """
    facts: dict[str, Any] = {
        "state": "NO_DATA",
        "environment": str(environment).lower(),
        "tracked_incidents": 0,
        "delivered_confirmed": 0,
        "ack_without_delivery": [],
        "provider_accepted_only": 0,
        "pending": 0,
        "ack_is_not_delivery": True,
        "reason_codes": [],
    }
    if "alert_deliveries" not in _table_names(conn):
        facts["reason_codes"] = [CODE_ALERT_MIRROR_TABLE_MISSING]
        return facts
    try:
        rows = conn.execute(
            "SELECT incident_key, attempt, state, state_rank, provider_receipt, "
            "device_receipt, delivered_at_ms, ack_at_ms FROM alert_deliveries "
            "WHERE environment=? ORDER BY incident_key, state_rank, attempt",
            (str(environment).lower(),)).fetchall()
    except Exception:
        facts["reason_codes"] = [CODE_ALERT_MIRROR_TABLE_MISSING]
        return facts
    if not rows:
        facts["reason_codes"] = ["NO_ALERT_ROWS"]
        return facts

    latest: dict[str, Any] = {}
    for row in rows:
        key = str(row["incident_key"])
        current = latest.get(key)
        rank = (int(row["state_rank"] or 0), int(row["attempt"] or 0))
        if current is None or rank >= current[0]:
            latest[key] = (rank, row)

    ack_only: list[dict[str, Any]] = []
    provider_only = 0
    delivered = 0
    pending = 0
    for key, (_rank, row) in latest.items():
        state = str(row["state"] or "").upper()
        has_device_receipt = bool(str(row["device_receipt"] or "").strip())
        has_delivery = row["delivered_at_ms"] is not None
        is_delivered = bool(has_delivery and has_device_receipt)
        if is_delivered:
            delivered += 1
            continue
        if row["ack_at_ms"] is not None or state == "ACKNOWLEDGED":
            # 已 ACK：人工确认可审计，但绝不等于送达/恢复。
            ack_only.append({
                "incident_key": key, "state": state,
                "ack_at_ms": row["ack_at_ms"],
                "provider_receipt": str(row["provider_receipt"] or "") or None,
                "delivered_at_ms": row["delivered_at_ms"],
                "device_receipt": None,
            })
            continue
        if str(row["provider_receipt"] or "").strip() or state in {"SUBMITTED", "ACCEPTED"}:
            # 上游接受但无设备回执：按"未送达"计。
            provider_only += 1
            continue
        pending += 1
    facts.update({
        "tracked_incidents": len(latest),
        "delivered_confirmed": delivered,
        "ack_without_delivery": ack_only[:20],
        "provider_accepted_only": provider_only,
        "pending": pending,
    })
    reason_codes: list[str] = []
    if ack_only:
        reason_codes.append(MANUAL_ACK_ONLY)
    if provider_only or pending:
        reason_codes.append(NO_CONFIRMED_DELIVERY)
    facts["reason_codes"] = reason_codes
    facts["state"] = "FAIL" if reason_codes else "PASS"
    return facts


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
    # ---- U14 新增（全部为**加法**字段，既有消费者不受影响）----
    #: 交易证据时间（POSIX 秒）：交易路径最后一条**持久化**证据的时刻。
    trading_evidence_at: float | None = None
    trading_evidence_age_seconds: float | None = None
    trading_evidence_sources: dict[str, Any] = field(default_factory=dict)
    #: 失联成因（``HEALTHY``/``PROCESS_MISSING``/``HOST_DOWN``/
    #: ``TRADING_STALLED``/``HOST_CONTINUITY_UNKNOWN``/``HOST_JUST_BOOTED``）。
    outage_kind: str = ""
    outage_reason_codes: list[str] = field(default_factory=list)
    host_uptime_seconds: float | None = None
    state_root: str = ""
    state_root_source: str = "fallback"
    u14_integration: str = U14_INTEGRATION

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
            "trading_evidence_at": self.trading_evidence_at,
            "trading_evidence_age_seconds": self.trading_evidence_age_seconds,
            "trading_evidence_sources": dict(self.trading_evidence_sources),
            "outage_kind": self.outage_kind,
            "outage_reason_codes": list(self.outage_reason_codes),
            "host_uptime_seconds": self.host_uptime_seconds,
            "state_root": self.state_root,
            "state_root_source": self.state_root_source,
            "u14_integration": self.u14_integration,
        }


def evaluate_health(config: HealthWatchConfig, *, now: float | None = None) -> HealthResult:
    """执行一次只读健康评估，返回结构化结果。不写任何文件。"""
    now = float(now if now is not None else time.time())
    categories: dict[str, list[str]] = {}
    observed_at: float | None = None
    evidence = TradingEvidence(False, None, {}, ("TRADING_DB_UNAVAILABLE",))

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
                # R13：交易证据时间必须独立于"心跳/进程存活"被读出来。
                evidence = probe_trading_evidence(conn, config.environment)
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

    # ---- U14：把"本机主进程故障"与"主机关机"按成因分开（F06/F12/F22）----
    # 判别式只看宿主连续运行时长与交易证据年龄（core.ops.alert_delivery 的纯函数）。
    up = host_uptime_seconds()
    evidence_at_seconds = None if evidence.at_ms is None else evidence.at_ms / 1000.0
    outage = classify_outage(
        now_seconds=now,
        process_alive=(True if proc.get("alive") is True else
                       (False if (proc["present"] and proc.get("alive") is False) else None)),
        trading_evidence_at_seconds=evidence_at_seconds,
        host_uptime_seconds=up,
        startup_grace_seconds=config.startup_grace_seconds,
        trading_evidence_max_age_seconds=config.fast_cycle_stale_seconds,
    )
    category = OUTAGE_CATEGORY_NAMES.get(outage.kind, "")
    if outage.kind == HOST_DOWN:
        # 证据缺口跨越了一次宿主重启：**不得**再同时报 process_missing（两类成因不混）。
        categories.pop("process_missing", None)
        categories[category] = [category]
    elif outage.kind == TRADING_STALLED:
        categories[category] = [category]
    elif outage.kind == HOST_CONTINUITY_UNKNOWN:
        # 进程确已消失这一条仍成立；只是"宿主是否也停过"不可证明，如实标出。
        categories.setdefault(category, []).append(HOST_CONTINUITY_UNKNOWN)
    elif outage.kind == HOST_JUST_BOOTED:
        reboot_codes = [code for code in outage.reason_codes if code == HOST_REBOOT_OBSERVED]
        if reboot_codes:
            categories[category] = reboot_codes

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
        trading_evidence_at=evidence_at_seconds,
        trading_evidence_age_seconds=outage.trading_evidence_age_seconds,
        trading_evidence_sources=dict(evidence.sources),
        outage_kind=outage.kind,
        outage_reason_codes=list(outage.reason_codes),
        host_uptime_seconds=up,
        state_root=str(config.data_root),
        state_root_source=config.state_root_source,
    )
