"""取证：最近 N 小时「LLM未产出有效决策」触发情况 + 最近一次 run 的工具回包摘要。

只读，不修改任何数据。
"""
import json
import sqlite3
import sys
from pathlib import Path

# U10b 归档：本项目根深度已 +1（见 docs/archive/README.md 的归档索引）。
ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "agent_trader.sqlite"
RAW_DIR = ROOT / "data" / "logs"

HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 3.0


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    print("=" * 78)
    print(f"【1】最近 {HOURS} 小时内的 no_trade 决策（reasoning 含『LLM未产出』）")
    print("=" * 78)
    rows = con.execute(
        """
        SELECT id, created_at, run_id, substr(reasoning, 1, 90) AS r
        FROM trade_decisions
        WHERE decision = 'no_trade'
          AND reasoning LIKE '%LLM未产出有效决策%'
          AND created_at >= datetime('now', ?)
        ORDER BY id DESC
        """,
        (f"-{HOURS} hours",),
    ).fetchall()
    print(f"命中 {len(rows)} 条")
    for r in rows:
        print(f"  #{r['id']}  {r['created_at']}  run={r['run_id']}")
        print(f"        {r['r']}")

    print()
    print("=" * 78)
    print("【2】最近 5 次 orchestrator 主循环（llm_calls）")
    print("=" * 78)
    runs = con.execute(
        """
        SELECT run_id, MAX(started_at) AS t, COUNT(*) AS n,
               SUM(CASE WHEN status='ok' THEN 1 ELSE 0 END) AS ok
        FROM llm_calls
        WHERE run_id IS NOT NULL AND run_id != ''
        GROUP BY run_id
        ORDER BY t DESC
        LIMIT 5
        """
    ).fetchall()
    for r in runs:
        print(f"  {r['t']}  run={r['run_id']}  调用 {r['n']} 次 (ok {r['ok']})")

    if not runs:
        con.close()
        return

    latest_run = runs[0]["run_id"]
    print()
    print("=" * 78)
    print(f"【3】最近一次 run={latest_run} 的逐轮明细")
    print("=" * 78)
    calls = con.execute(
        """
        SELECT call_id, started_at, agent_name, status, latency_ms, error
        FROM llm_calls
        WHERE run_id = ?
        ORDER BY id
        """,
        (latest_run,),
    ).fetchall()
    for i, c in enumerate(calls, 1):
        print(f"  #{i}  {c['started_at']}  {c['agent_name']}  {c['status']}  {c['latency_ms']}ms  call_id={c['call_id']}")
        if c["error"]:
            print(f"      error: {c['error'][:160]}")

    # 从原始 jsonl 里按 call_id 捞工具回包
    print()
    print("=" * 78)
    print("【4】该 run 各轮「工具回包」内容（判断是否仍在报 TypeError 名字遮蔽）")
    print("=" * 78)
    wanted = {c["call_id"] for c in calls}
    found: dict[str, list] = {}
    for day_dir in sorted(RAW_DIR.glob("2026-*"), reverse=True):
        f = day_dir / "orchestrator_llm_raw.jsonl"
        if not f.exists():
            continue
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            cid = obj.get("call_id") or obj.get("meta", {}).get("call_id")
            if cid in wanted:
                found.setdefault(cid, []).append(obj)
        if len(found) >= len(wanted):
            break

    for i, c in enumerate(calls, 1):
        objs = found.get(c["call_id"], [])
        if not objs:
            print(f"  第{i}次调用: (jsonl 未找到 call_id={c['call_id']})")
            continue
        hits = 0
        others = 0
        for obj in objs:
            blob = json.dumps(obj, ensure_ascii=False)
            hits += blob.count("Invalid type for url")
            # 其它 error 回包
            for m in ("TypeError", "ConnectError", "ReadTimeout", "502", "WinError 10054"):
                if m != "TypeError":
                    others += blob.count(m)
            for key in ("tool_results", "tools_result", "tool_messages"):
                tr = obj.get(key)
                if not tr:
                    continue
                for item in tr:
                    content = ""
                    if isinstance(item, dict):
                        content = str(item.get("content") or item.get("output") or item.get("text") or "")
                    else:
                        content = str(item)
                    if "Invalid type for url" in content:
                        print(f"  第{i}次调用  <<< TypeError 名字遮蔽（旧代码仍在跑）")
                        print(f"      {content[:220]}")
        if hits == 0:
            print(f"  第{i}次调用: 工具回包未发现 TypeError 名字遮蔽 (其它网络类报错 {others} 处)")

    con.close()


if __name__ == "__main__":
    main()
