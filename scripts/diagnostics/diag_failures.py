"""分析数据库中 llm_calls 和 trade_decisions 里的错误分布。"""
import sqlite3
from pathlib import Path
from collections import Counter

# U10b 归档：本项目根深度已 +1（见 docs/archive/README.md 的归档索引）。
ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "agent_trader.sqlite"

def main():
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row

    print("=" * 80)
    print("【1】trade_decisions 总体决策决策分布统计")
    print("=" * 80)
    total_decisions = con.execute("SELECT COUNT(*) FROM trade_decisions").fetchone()[0]
    decision_types = con.execute("SELECT decision, COUNT(*) as cnt FROM trade_decisions GROUP BY decision").fetchall()
    
    print(f"总决策记录数: {total_decisions}")
    for d in decision_types:
        pct = (d['cnt'] / total_decisions) * 100 if total_decisions else 0
        print(f"  - {d['decision']:10s}: {d['cnt']:4d} 次 ({pct:.2f}%)")

    print("\n" + "=" * 80)
    print("【2】no_trade 降级原因分类统计")
    print("=" * 80)
    no_trades = con.execute(
        "SELECT reasoning FROM trade_decisions WHERE decision = 'no_trade'"
    ).fetchall()
    
    reasons = []
    for nt in no_trades:
        r = nt['reasoning'] or ""
        if "504 Gateway Time-out" in r:
            reasons.append("504 Gateway Time-out (OpenCode Zen 超时)")
        elif "502 Bad Gateway" in r:
            reasons.append("502 Bad Gateway (OpenCode Zen 网关错)")
        elif "WinError 10054" in r or "远程主机强迫关闭" in r:
            reasons.append("WinError 10054 (网络连接被中途重置)")
        elif "401 Unauthorized" in r:
            reasons.append("401 Unauthorized (API 凭证失效)")
        elif "503 Service Unavailable" in r:
            reasons.append("503 Service Unavailable (服务不可用)")
        elif "连续多轮未输出最终JSON" in r:
            reasons.append("连续多轮未输出最终JSON (循环耗尽或一直在调工具)")
        elif "LLM输出解析失败" in r:
            reasons.append("LLM输出解析失败 (Schema 或格式不匹配)")
        elif "未过业务校验" in r or "参数校验未通过" in r:
            reasons.append("业务/参数校验未通过")
        elif "无 ticket 或没有可执行 ticket" in r and "LLM未产出" not in r:
            reasons.append("策略通道：无可用 ticket (正常等待)")
        else:
            # 缩短并做分类
            short_r = r.split("last_error=")[1][:60] if "last_error=" in r else r[:60]
            reasons.append(f"其它: {short_r.strip()}")
            
    counter = Counter(reasons)
    for reason, count in counter.most_common():
        pct = (count / len(no_trades)) * 100 if no_trades else 0
        print(f"  - {reason:<45s}: {count:4d} 次 ({pct:.2f}%)")

    print("\n" + "=" * 80)
    print("【3】llm_calls 接口调用调用状态与错误审计")
    print("=" * 80)
    total_calls = con.execute("SELECT COUNT(*) FROM llm_calls").fetchone()[0]
    statuses = con.execute("SELECT status, COUNT(*) as cnt FROM llm_calls GROUP BY status").fetchall()
    print(f"总调用记录数: {total_calls}")
    for s in statuses:
        pct = (s['cnt'] / total_calls) * 100 if total_calls else 0
        print(f"  - {s['status']:10s}: {s['cnt']:4d} 次 ({pct:.2f}%)")

    print("\n" + "=" * 80)
    print("【4】llm_calls 报错分类统计 (仅限 status = 'error')")
    print("=" * 80)
    errors = con.execute("SELECT error FROM llm_calls WHERE status = 'error'").fetchall()
    err_reasons = []
    for e in errors:
        err = e['error'] or ""
        if "504 Gateway Time-out" in err:
            err_reasons.append("504 Gateway Time-out")
        elif "502 Bad Gateway" in err:
            err_reasons.append("502 Bad Gateway")
        elif "WinError 10054" in err or "远程主机强迫关闭" in err:
            err_reasons.append("WinError 10054 Connection Reset")
        elif "401 Unauthorized" in err:
            err_reasons.append("401 Unauthorized")
        elif "503 Service Unavailable" in err:
            err_reasons.append("503 Service Unavailable")
        elif "timeout" in err.lower():
            err_reasons.append("Httpx ReadTimeout / ConnectTimeout")
        else:
            err_reasons.append(f"其它: {err[:60]}")
            
    err_counter = Counter(err_reasons)
    for err, count in err_counter.most_common():
        pct = (count / len(errors)) * 100 if errors else 0
        print(f"  - {err:<40s}: {count:4d} 次 ({pct:.2f}%)")

    con.close()

if __name__ == "__main__":
    main()
