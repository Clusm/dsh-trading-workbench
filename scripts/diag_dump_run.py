"""dump 指定 run 的每一轮：finish_reason / 调了哪些工具 / 工具回包开头。

用法: python scripts/diag_dump_run.py [run_id]   （缺省=最近一次）
只读。
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "data" / "agent_trader.sqlite"
RAW_DIR = ROOT / "data" / "logs"


def load_raw(call_ids: set[str]) -> dict[str, dict]:
    """按 call_id 从 jsonl 里捞原始记录（逐行扫，扫到全齐为止）。"""
    found: dict[str, dict] = {}
    files = sorted(RAW_DIR.glob("*/orchestrator_llm_raw.jsonl"), reverse=True)
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"  [warn] 读 {f} 失败: {e}")
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            obj = None
            cid = None
            try:
                obj = json.loads(line)
                cid = obj.get("call_id")
            except Exception:
                # 有些行可能是 {call_id: {...}} 结构，或 json 损坏
                continue
            if cid in call_ids and cid not in found:
                found[cid] = obj
        if len(found) >= len(call_ids):
            break
    return found


def summarize_choices(obj: dict) -> None:
    """打印该次调用的模型输出要点。"""
    # 兼容多种落盘结构
    resp = obj.get("response") or obj.get("raw") or obj
    choices = resp.get("choices") if isinstance(resp, dict) else None
    if not choices and isinstance(obj.get("choices"), list):
        choices = obj["choices"]
    if not choices:
        keys = list(obj.keys())[:20]
        print(f"      (无 choices 字段; 顶层 keys={keys})")
        return
    for ch in choices:
        if not isinstance(ch, dict):
            continue
        fr = ch.get("finish_reason")
        msg = ch.get("message") or {}
        content = (msg.get("content") or "") if isinstance(msg, dict) else ""
        tc = msg.get("tool_calls") if isinstance(msg, dict) else None
        print(f"      finish_reason={fr}  content_len={len(content)}  tool_calls={len(tc) if tc else 0}")
        if tc:
            for t in tc:
                fn = t.get("function") or {}
                args = fn.get("arguments") or ""
                print(f"        -> {fn.get('name')}({args[:110]})")
        if content:
            print(f"      content: {content[:260]}")


def main() -> None:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    run_id = sys.argv[1] if len(sys.argv) > 1 else None
    if not run_id:
        r = con.execute(
            "SELECT run_id FROM llm_calls WHERE run_id IS NOT NULL AND run_id != '' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        run_id = r["run_id"] if r else None
    if not run_id:
        print("没有 run 记录")
        return

    calls = con.execute(
        "SELECT call_id, started_at, status, latency_ms, error, prompt_file, completion_file "
        "FROM llm_calls WHERE run_id=? ORDER BY id",
        (run_id,),
    ).fetchall()
    print(f"run_id = {run_id}   共 {len(calls)} 次调用")
    print("=" * 90)

    raws = load_raw({c["call_id"] for c in calls})
    print(f"(jsonl 命中 {len(raws)}/{len(calls)} 条)")
    print()

    for i, c in enumerate(calls, 1):
        print(f"---- 第 {i} 次  {c['started_at']}  {c['status']}  {c['latency_ms']}ms")
        obj = raws.get(c["call_id"])
        if obj is None:
            print("      (jsonl 未命中，改用落盘文件)")
            for label, p in (("prompt", c["prompt_file"]), ("completion", c["completion_file"])):
                if not p:
                    continue
                fp = ROOT / p if not Path(p).is_absolute() else Path(p)
                if fp.exists():
                    try:
                        blob = fp.read_text(encoding="utf-8", errors="replace")
                    except Exception as e:
                        print(f"      {label} 读取失败 {e}")
                        continue
                    # completion 可能是完整响应 json
                    if label == "completion":
                        try:
                            j = json.loads(blob)
                            print(f"      [{label} json]")
                            summarize_choices({"choices": j.get("choices", [])} if isinstance(j, dict) else j)
                            continue
                        except Exception:
                            pass
                    # 否则把工具回包抓出来
                    for kw in ("Invalid type for url", "TypeError", "Server error", "Client error",
                               "ConnectError", "ReadTimeout", "10054"):
                        n = blob.count(kw)
                        if n:
                            print(f"      [{label}] 含 '{kw}' × {n}")
                    print(f"      [{label}] 末尾: {blob[-260:]!r}")
            continue
        summarize_choices(obj)
        # 工具回包
        blob = json.dumps(obj, ensure_ascii=False)
        for kw in ("Invalid type for url", "Server error", "Client error",
                   "ConnectError", "ReadTimeout", "10054", "timeout"):
            n = blob.count(kw)
            if n:
                idx = blob.find(kw)
                print(f"      [回包含 '{kw}' × {n}] {blob[max(0,idx-90):idx+130]}")
        print()

    con.close()


if __name__ == "__main__":
    main()
