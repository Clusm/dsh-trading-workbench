"""r5 两态证明 + 非空转反证。

1) 两态：直接打印"补列前迁移集合 / 补列后迁移集合 / current_schema_version"，
   断言二者相等（这就是被改断言所依赖的事实）。
2) 非空转反证：人为让补列动作**偷偷插入一行 schema_migrations**，同一条相对式
   判据必须失败——证明改后的断言不是恒真，仍能抓到"U04 真的新增了迁移"。
3) 追加迁移仍通过：链上再多两版（17/18）时，相对式判据不变仍成立。

用法: python probe_two_state.py <tree_dir>
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

TREE = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(TREE / "src"))

from audit.database import AuditDatabase  # noqa: E402
from exchange.protection_commands import ensure_execution_schema  # noqa: E402


def versions(db: AuditDatabase) -> list[int]:
    return [int(r["version"]) for r in db.fetch_all(
        "SELECT version FROM schema_migrations ORDER BY version")]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mta-u04-r5-"))

    print("== [1] 两态证明：补列前 == 补列后（真实原始输出） ==")
    db = AuditDatabase(tmp / "two_state.sqlite")
    before = versions(db)
    sv_before = db.current_schema_version()
    ensure_execution_schema(db.conn)
    after = versions(db)
    sv_after = db.current_schema_version()
    print(f"  补列前 versions   = {before}")
    print(f"  补列后 versions   = {after}")
    print(f"  补列前 schema_ver = {sv_before}")
    print(f"  补列后 schema_ver = {sv_after}")
    ok = (before == after) and (sv_before == sv_after)
    print(f"  前后相等? {ok}  exit_code={0 if ok else 1}")
    db.close()
    if not ok:
        return 1

    print("\n== [2] 非空转反证：偷偷插一行迁移 -> 同一判据必须失败 ==")
    db2 = AuditDatabase(tmp / "tamper.sqlite")
    v_before = versions(db2)
    real = ensure_execution_schema(db2.conn)
    # 模拟"U04 的补列其实是一次迁移"：补列后追加一行 schema_migrations
    db2.conn.execute(
        "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
        (99, "u04_should_not_exist", "2026-01-01T00:00:00+00:00"))
    db2.conn.commit()
    v_after = versions(db2)
    leaked = v_after != v_before
    print(f"  真实补列返回列数 = {len(real)}")
    print(f"  迁移集合是否被改动 = {leaked}（期望 True，说明能抓到）")
    print(f"  判据在此情形下失败退出? {leaked}")
    db2.close()
    if not leaked:
        print("反证失败：相对式判据对本情形不敏感（恒真）")
        return 1

    print("\n== [3] 链上再多两版仍通过 ==")
    db3 = AuditDatabase(tmp / "extra.sqlite")
    top = db3.current_schema_version()
    for extra in (top + 1, top + 2):
        db3.conn.execute(
            "INSERT INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
            (extra, f"synthetic_{extra}", "2026-01-01T00:00:00+00:00"))
    db3.conn.commit()
    b = versions(db3)
    s = db3.current_schema_version()
    ensure_execution_schema(db3.conn)
    a = versions(db3)
    s2 = db3.current_schema_version()
    print(f"  补列前 versions   = {b}")
    print(f"  补列后 versions   = {a}")
    print(f"  current_schema_version {s} -> {s2}")
    ok3 = (b == a) and (s == s2) and b == list(range(1, len(b) + 1))
    print(f"  链良构且集合不变? {ok3}")
    db3.close()
    return 0 if ok3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
