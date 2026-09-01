"""按轮精确统计「tool 角色消息」里的错误回包，并抽取 httpx.Client 内存地址。

地址相同 => 同一个进程/同一个连接单例 => 服务未重启。
用途: python scripts/diag_tool_errors.py [run_id]
只读。
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "agent_trader.sqlite"
RAW_DIR = ROOT / "data" / "logs"

ADDR_RE = re.compile(r"httpx\.Client object at (0x[0-9A-Fa-f]+)")


def load_raw(call_ids: set[str]) -> dict[str, dict]:
    found: dict[str, dict] = {}
    for f in sorted(RAW_DIR.glob("*/orchestrator_llm_raw.jsonl"), reverse=True):
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            cid = obj.get("call_id")
            if cid in call_ids and cid not in found:
                found[cid] = obj
        if len(found) >= len(call_ids):
            break
    return found


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    run_id = sys.argv[1] if len(sys.argv) > 1 else None
    if not run_id:
        r = con.execute(
            "SELECT run_id FROM llm_calls WHERE run_id IS NOT NULL AND run_id!='' "
            "ORDER BY id DESC LIMIT 1").fetchone()
        run_id = r["run_id"]

    calls = con.execute(
        "SELECT call_id, started_at FROM llm_calls WHERE run_id=? ORDER BY id", (run_id,)
    ).fetchall()
    raws = load_raw({c["call_id"] for c in calls})

    print(f"run_id = {run_id}")
    print("=" * 96)
    all_addrs: set[str] = set()
    for i, c in enumerate(calls, 1):
        obj = raws.get(c["call_id"])
        if not obj:
            print(f"第{i}轮: (jsonl 未命中)")
            continue
        msgs = obj.get("messages") or []
        tool_msgs = [m for m in msgs
                     if isinstance(m, dict) and m.get("role") == "tool"]
        n_url = 0
        n_other_err = 0
        addrs: set[str] = set()
        samples: list[str] = []
        for m in tool_msgs:
            content = str(m.get("content") or "")
            if "Invalid type for url" in content:
                n_url += 1
                for a in ADDR_RE.findall(content):
                    addrs.add(a)
                if len(samples) < 1:
                    samples.append(content[:150])
            elif content.strip().startswith("{") and '"error"' in content:
                n_other_err += 1
                if len(samples) < 1:
                    samples.append(content[:150])
        all_addrs |= addrs
        print(f"第{i}轮  {c['started_at']}  tool回包共 {len(tool_msgs)} 条 | "
              f"TypeError名字遮蔽 {n_url} 条 | 其它error {n_other_err} 条")
        for s in samples:
            print(f"        样例: {s}")
        if addrs:
            print(f"        httpx.Client 地址: {sorted(addrs)}")

    print()
    print("=" * 96)
    print(f"全部轮次出现的 httpx.Client 地址集合: {sorted(all_addrs)}")
    if len(all_addrs) == 1:
        print(">>> 只有一个地址 => 全程同一个 httpx 单例 => 进程未重启，内存里仍是旧代码")
    con.close()


if __name__ == "__main__":
    main()
