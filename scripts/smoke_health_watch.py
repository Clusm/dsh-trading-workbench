"""O-01 独立只读健康监控回归；全程使用项目内临时目录，不联网、不读真实密钥。

覆盖四类异常的判定，并证明：
  * 未配置外部告警通道时结果明确为 unverified（不假装已接入）；
  * 监控进程严格只读（运行前后目标目录无新增/修改文件，数据库无 -wal/-shm 副作用）。

遵循现有 smoke 风格：顶层脚本、check(name, cond, extra)、结尾 PASS/FAIL 计数、失败退出码非 0。
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "scripts"))

from core.health_watch import HealthWatchConfig, evaluate_health  # noqa: E402

ok = fail = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global ok, fail
    if cond:
        ok += 1
        print(f"  PASS  {name}")
    else:
        fail += 1
        print(f"  FAIL  {name}  {extra}")


TMP = ROOT / "_smoke_health_watch_tmp"
import atexit
atexit.register(lambda: shutil.rmtree(TMP, ignore_errors=True))  # 用完清理项目内临时目录


def _iso(offset_seconds: float = 0.0) -> str:
    return (datetime.now(timezone.utc).timestamp() + offset_seconds)


def _iso_str(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _make_settings(stability: dict | None = None) -> dict:
    return {
        "stability": stability or {
            "startup_grace_seconds": 120,
            "fast_cycle_stale_seconds": 120,
            "protection_reconcile_stale_seconds": 120,
        },
        "paths": {"data_dir": "data", "db_file": "data/agent_trader.sqlite"},
        "execution": {"default_mode": "live"},
    }


def _write_settings(project: Path, settings: dict) -> None:
    (project / "config").mkdir(parents=True, exist_ok=True)
    (project / "config" / "settings.yaml").write_text(
        json.dumps(settings), encoding="utf-8")


def _fresh_db(data_live: Path) -> sqlite3.Connection:
    data_live.mkdir(parents=True, exist_ok=True)
    db_path = data_live / "agent_trader.sqlite"
    if db_path.exists():
        db_path.unlink()
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=DELETE")  # 不产生 WAL，便于验证无 -wal/-shm 副作用
    conn.execute(
        "CREATE TABLE release_observations ("
        "observation_id TEXT PRIMARY KEY, release_id TEXT, environment TEXT,"
        "account_ref TEXT, session_id TEXT, observed_at TEXT, observed_minute TEXT,"
        "health_state TEXT, component_status_json TEXT, config_hash TEXT,"
        "rate_limit_json TEXT, coverage_json TEXT, entry_readiness_json TEXT,"
        "raw_json TEXT)"
    )
    conn.execute(
        "CREATE TABLE protection_reconciliations ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, checked_at TEXT, environment TEXT,"
        "matched INTEGER, positions INTEGER, open_algos INTEGER, orphan_orders INTEGER,"
        "missing_protection INTEGER, direction_mismatch INTEGER, qty_mismatch INTEGER,"
        "unsafe_orders INTEGER, details_json TEXT)"
    )
    conn.commit()
    return conn


def _insert_observation(conn, *, observed_at, fast_cycle, environment="live"):
    conn.execute(
        "INSERT INTO release_observations VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("obs1", "rel1", environment, "live:acc", "sess1",
         _iso_str(observed_at), "2026-01-01T00:00:00+00:00", "healthy",
         json.dumps({"fast_cycle": fast_cycle, "slow_cycle": "healthy",
                     "service": "healthy", "persistence": "healthy"}),
         "h", "{}", "{}", json.dumps({"new_entry_allowed": True, "blocking_reasons": []}),
         "{}"),
    )
    conn.commit()


def _insert_protection(conn, *, checked_at, matched, environment="live"):
    conn.execute(
        "INSERT INTO protection_reconciliations "
        "(checked_at, environment, matched, positions, open_algos, orphan_orders,"
        " missing_protection, direction_mismatch, qty_mismatch, unsafe_orders, details_json)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (_iso_str(checked_at), environment, int(matched), 0, 0, 0, 0, 0, 0, 0,
         json.dumps({})),
    )
    conn.commit()


def _write_lock(data_live: Path, *, pid: int | None, started_at: float | None = None):
    if pid is None:
        if data_live.joinpath("trading_service.lock").exists():
            data_live.joinpath("trading_service.lock").unlink()
        return
    data_live.joinpath("trading_service.lock").write_text(
        json.dumps({"pid": pid, "started_at": _iso_str(
            time.time() if started_at is None else started_at)}),
        encoding="utf-8")


def _base_config(project: Path, **overrides) -> HealthWatchConfig:
    cfg = HealthWatchConfig(
        project_root=project, profile="live", environment="live",
        startup_grace_seconds=120, fast_cycle_stale_seconds=120,
        protection_reconcile_stale_seconds=120, observation_stale_seconds=300,
        alert_channel="unverified", alert_channel_status="unverified",
        alert_channel_note="未验证：未配置已批准的外部告警通道，无人值守告警尚未完成验证",
    )
    for k, v in overrides.items():
        setattr(cfg, k, v)
    return cfg


def _snapshot(data_root: Path) -> dict:
    snap: dict = {}
    for p in data_root.rglob("*"):
        if p.is_file():
            st = p.stat()
            snap[p.relative_to(data_root).as_posix()] = (st.st_mtime_ns, st.st_size)
    return snap


def main() -> None:
    if TMP.exists():
        shutil.rmtree(TMP)
    project = TMP / "project"
    data_live = project / "data" / "live"
    _write_settings(project, _make_settings())

    now = time.time()

    # --- 1. 托管进程消失 -----------------------------------------------------
    conn = _fresh_db(data_live)
    _insert_observation(conn, observed_at=now, fast_cycle="healthy")
    _insert_protection(conn, checked_at=now, matched=1)
    conn.close()
    _write_lock(data_live, pid=999999)  # 不存在的 PID -> 进程消失
    res = evaluate_health(_base_config(project))
    check("进程消失(死PID) 触发 process_missing",
          "process_missing" in res.reasons, str(res.reasons))

    _write_lock(data_live, pid=None)  # 锁文件缺失 -> 进程消失
    res = evaluate_health(_base_config(project))
    check("进程消失(无锁) 触发 process_missing",
          "process_missing" in res.reasons, str(res.reasons))

    data_live.joinpath("trading_service.lock").write_text("{broken", encoding="utf-8")
    res = evaluate_health(_base_config(project))
    check("锁文件损坏时不误报健康",
          "process_unverified" in res.reasons, str(res.reasons))

    # --- 2. 快循环长期无完成 ------------------------------------------------
    conn = _fresh_db(data_live)
    _insert_observation(conn, observed_at=now, fast_cycle="stale")
    _insert_protection(conn, checked_at=now, matched=1)
    conn.close()
    _write_lock(data_live, pid=os.getpid())  # 本进程存活，排除 process_missing
    res = evaluate_health(_base_config(project))
    check("快循环过期(stale) 触发 fast_cycle_stale",
          "fast_cycle_stale" in res.reasons, str(res.reasons))
    check("快循环过期时未误报进程消失",
          "process_missing" not in res.reasons, str(res.reasons))

    conn = _fresh_db(data_live)
    _insert_observation(conn, observed_at=now, fast_cycle="missing")
    _insert_protection(conn, checked_at=now, matched=1)
    conn.close()
    _write_lock(data_live, pid=os.getpid())
    res = evaluate_health(_base_config(project))
    check("快循环从未完成(missing) 触发 fast_cycle_never_completed",
          "fast_cycle_never_completed" in res.reasons, str(res.reasons))

    conn = _fresh_db(data_live)
    _insert_observation(conn, observed_at=now, fast_cycle="degraded")
    _insert_protection(conn, checked_at=now, matched=1)
    conn.close()
    _write_lock(data_live, pid=os.getpid())
    res = evaluate_health(_base_config(project))
    check("快循环错误(degraded) 触发 fast_cycle_error",
          "fast_cycle_error" in res.reasons, str(res.reasons))

    # --- 3. 数据过期（保护单对账 / 证据流过期） -----------------------------
    conn = _fresh_db(data_live)
    _insert_observation(conn, observed_at=now, fast_cycle="healthy")
    _insert_protection(conn, checked_at=now - 1000, matched=0)  # 旧且不匹配
    conn.close()
    _write_lock(data_live, pid=os.getpid())
    res = evaluate_health(_base_config(project))
    check("保护单对账过期 触发 protection_reconcile_stale",
          "protection_reconcile_stale" in res.reasons, str(res.reasons))
    check("保护不匹配同时触发 protection_mismatch",
          "protection_mismatch" in res.reasons, str(res.reasons))

    conn = _fresh_db(data_live)
    _insert_observation(conn, observed_at=now, fast_cycle="healthy")
    # 不插入 protection_reconciliations 行 -> 从未对账
    conn.close()
    _write_lock(data_live, pid=os.getpid(), started_at=now - 1000)
    res = evaluate_health(_base_config(project))
    check("保护单从未对账 触发 protection_reconcile_never_completed",
          "protection_reconcile_never_completed" in res.reasons, str(res.reasons))

    conn = _fresh_db(data_live)
    _insert_observation(conn, observed_at=now - 1000, fast_cycle="healthy")  # 证据流过期
    _insert_protection(conn, checked_at=now, matched=1)
    conn.close()
    _write_lock(data_live, pid=os.getpid())
    res = evaluate_health(_base_config(project))
    check("证据流过期 触发 observation_stale",
          "observation_stale" in res.reasons, str(res.reasons))

    conn = _fresh_db(data_live)
    conn.close()  # 刚启动，首条观察和首次保护对账都尚未写入
    _write_lock(data_live, pid=os.getpid(), started_at=now)
    res = evaluate_health(_base_config(project), now=now + 10)
    check("启动宽限内空证据库不误报 observation_stale",
          "observation_stale" not in res.reasons, str(res.reasons))
    check("启动宽限内未完成保护对账不误报",
          "protection_reconcile_never_completed" not in res.reasons, str(res.reasons))

    _write_lock(data_live, pid=os.getpid(), started_at=now - 1000)
    res = evaluate_health(_base_config(project), now=now)
    check("启动宽限结束后空证据库 fail-closed",
          {"observation_stale", "protection_reconcile_never_completed"}.issubset(res.reasons),
          str(res.reasons))

    conn = _fresh_db(data_live)
    _insert_observation(conn, observed_at=now, fast_cycle="healthy", environment="demo")
    _insert_protection(conn, checked_at=now, matched=1, environment="live")
    conn.close()
    _write_lock(data_live, pid=os.getpid(), started_at=now - 1000)
    res = evaluate_health(_base_config(project), now=now)
    check("不同环境的证据不能串入 live 健康判定",
          "observation_stale" in res.reasons, str(res.reasons))

    # --- 4. 磁盘不可用 ------------------------------------------------------
    conn = _fresh_db(data_live)
    conn.close()
    _write_lock(data_live, pid=os.getpid())
    bad_cfg = _base_config(project)
    bad_cfg.profile = "live"
    # 指向不存在的数据目录
    bad_cfg.project_root = project
    bad_cfg_styled = HealthWatchConfig(
        project_root=project / "nonexistent_extra", profile="live", environment="live")
    res = evaluate_health(bad_cfg_styled)
    check("数据目录不可访问 触发 disk_unavailable",
          "disk_unavailable" in res.reasons, str(res.reasons))

    # --- 5. 告警通道未验证（关键约束） --------------------------------------
    conn = _fresh_db(data_live)
    _insert_observation(conn, observed_at=now, fast_cycle="healthy")
    _insert_protection(conn, checked_at=now, matched=1)
    conn.close()
    _write_lock(data_live, pid=os.getpid())
    res = evaluate_health(_base_config(project))
    check("健康且无外部通道时 alert_channel_status=unverified",
          res.alert_channel_status == "unverified", res.alert_channel_status)
    check("未验证时明确否认无人值守告警已完成",
          "尚未完成" in (res.alert_channel_note or ""), res.alert_channel_note or "")
    check("未验证说明文案含『未验证』",
          "未验证" in (res.alert_channel_note or ""), res.alert_channel_note or "")
    # 复用 stability 阈值：从 settings 文件读取
    cfg2 = HealthWatchConfig.from_settings(project, profile="live")
    check("from_settings 复用 stability.fast_cycle_stale_seconds=120",
          cfg2.fast_cycle_stale_seconds == 120, str(cfg2.fast_cycle_stale_seconds))
    # 真实项目配置是 YAML 而非 JSON；直接调用 from_settings 也必须正确解析。
    (project / "config" / "settings.yaml").write_text(
        "stability:\n  fast_cycle_stale_seconds: 77\n"
        "execution:\n  default_mode: demo\n",
        encoding="utf-8",
    )
    cfg_yaml = HealthWatchConfig.from_settings(project, profile="live")
    check("from_settings 可直接解析 YAML",
          cfg_yaml.fast_cycle_stale_seconds == 77, str(cfg_yaml.fast_cycle_stale_seconds))

    # --- 6. 只读性证明 ------------------------------------------------------
    conn = _fresh_db(data_live)
    _insert_observation(conn, observed_at=now, fast_cycle="healthy")
    _insert_protection(conn, checked_at=now, matched=1)
    conn.close()
    _write_lock(data_live, pid=os.getpid())
    cfg = _base_config(project)
    snap_before = _snapshot(data_live)
    res = evaluate_health(cfg)
    check("健康场景判定 healthy=true", res.healthy is True, str(res.reasons))
    # 通过 CLI 子进程再跑一次（健康但告警未验证 -> 退出码 2），验证端到端只读
    cli = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "health_watch.py"),
         "--project-root", str(project), "--profile", "live", "--once"],
        cwd=ROOT, capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "MTA_NO_PROXY": "1", "PYTHONUTF8": "1",
             "PYTHONIOENCODING": "utf-8"},
    )
    check("CLI 健康且未验证时退出码=2（不声称无人值守完成）",
          cli.returncode == 2, f"rc={cli.returncode} stderr={cli.stderr[-200:]}")
    try:
        cli_payload = json.loads(cli.stdout.strip().splitlines()[-1])
        check("CLI 输出机器可读 JSON 且 healthy=true",
              cli_payload.get("healthy") is True, cli.stdout[-200:])
        check("CLI 输出 read_only=true",
              cli_payload.get("read_only") is True, cli.stdout[-200:])
    except Exception as exc:
        check("CLI 输出可解析为 JSON", False, str(exc))
    snap_after = _snapshot(data_live)
    check("监控运行后目标目录无新增/修改文件",
          snap_before == snap_after,
          f"before={len(snap_before)} after={len(snap_after)}")
    sidecars = list(data_live.rglob("*-wal")) + list(data_live.rglob("*-shm")) + \
        list(data_live.rglob("*-journal"))
    check("数据库未产生 -wal/-shm/-journal 副作用",
          not sidecars, str([str(p) for p in sidecars]))

    # --- 7. WAL 最新提交可见 ------------------------------------------------
    # immutable=1 会跳过 WAL，只看到 checkpoint 前的旧行；监控运行中的服务必须读取
    # mode=ro 快照，否则会把健康实例误报为 observation_stale。
    wal_root = project / "wal_case" / "data" / "live"
    wal_root.mkdir(parents=True, exist_ok=True)
    wal_db = wal_root / "agent_trader.sqlite"
    writer = sqlite3.connect(str(wal_db))
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE release_observations ("
                   "observation_id TEXT PRIMARY KEY, observed_at TEXT, "
                   "component_status_json TEXT, entry_readiness_json TEXT, environment TEXT)")
    old_at = now - 1000
    writer.execute("INSERT INTO release_observations VALUES (?,?,?,?,?)",
                   ("old", _iso_str(old_at), json.dumps({"fast_cycle": "healthy"}), "{}", "live"))
    writer.commit()
    writer.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    latest_at = now
    writer.execute("INSERT INTO release_observations VALUES (?,?,?,?,?)",
                   ("latest", _iso_str(latest_at), json.dumps({"fast_cycle": "healthy"}), "{}", "live"))
    writer.commit()  # 最新行只在 WAL 中，writer 保持打开
    from core.health_watch import open_read_only
    reader = open_read_only(wal_db)
    try:
        latest = reader.execute(
            "SELECT observed_at FROM release_observations ORDER BY observed_at DESC LIMIT 1"
        ).fetchone()[0]
    finally:
        reader.close()
        writer.close()
    check("只读连接可见 WAL 中最新提交", latest == _iso_str(latest_at), str(latest))

    print(f"\nHEALTH_WATCH_SMOKE ok={ok} fail={fail}")
    if fail:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
