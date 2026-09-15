# -*- coding: utf-8 -*-
"""只读诊断：交易所 Algo 保护单 vs 实际持仓对账。

输出两部分：
  [A] 孤儿保护单 —— 有 Algo 单但已无持仓（sync_protections 的扫描盲区，会单调累积）
  [B] 持仓保护完整性 —— 每个持仓是否 SL/TP 齐全（裸仓检测）

注意：必须先调用 apply_proxy_env() 接管出口。进程环境可能被外层工具注入坏代理，
不接管会全部 502，误判成"系统故障"（本机实测：54312 是 sandbox-cli 的坏代理，
10808 是 sing-box 的可用代理）。

用法：
    .venv/Scripts/python.exe scripts/diag_algo_orphans.py
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))
from core.net_env import apply_proxy_env

apply_proxy_env()
from core.config_loader import load_settings
from exchange.factory import create_broker
from audit.database import AuditDatabase

st = load_settings(ROOT)
db = AuditDatabase(st.db_file)


def retry(fn, n=8, wait=3.0, what=""):
    last = None
    for i in range(n):
        try:
            return fn()
        except Exception as e:
            last = e
            print(f"   [retry {i+1}] {what}: {type(e).__name__}")
            time.sleep(wait)
    raise last

bk = retry(lambda: create_broker(st, db), what="broker")
pos = retry(lambda: [p for p in bk.positions() if abs(float(p.get("positionAmt") or 0)) > 0], what="pos")
algos = retry(lambda: bk._open_algos(None), what="algos")

have = {p["symbol"] for p in pos}
by_sym = {}
for a in algos:
    by_sym.setdefault(a.get("symbol"), []).append(a)

print(f"持仓 {len(have)} 个: {sorted(have)}")
print(f"Algo 单 {len(algos)} 个，覆盖 {len(by_sym)} 个 symbol\n")
print("=== [A] 孤儿保护单：有 Algo 单但已无持仓 ===")
orphan_syms = [s for s in by_sym if s not in have]
if not orphan_syms:
    print("  无")
for s in sorted(orphan_syms):
    for a in by_sym[s]:
        print(f"  {s:12s} algoId={a.get('algoId')} {a.get('orderType'):22s} "
              f"trigger={a.get('triggerPrice'):<12} qty={a.get('quantity')}")

print("\n=== [B] 持仓保护完整性 ===")
for p in sorted(pos, key=lambda x: x["symbol"]):
    s = p["symbol"]
    types = {a.get("orderType") for a in by_sym.get(s, [])}
    ok = "STOP_MARKET" in types and "TAKE_PROFIT_MARKET" in types
    print(f"  {s:12s} amt={p['positionAmt']:>10s}  SL={'Y' if 'STOP_MARKET' in types else 'N'} "
          f"TP={'Y' if 'TAKE_PROFIT_MARKET' in types else 'N'}  {'OK' if ok else '<<< 保护缺失'}")
