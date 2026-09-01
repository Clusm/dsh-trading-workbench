# -*- coding: utf-8 -*-
"""核对总览页资金数字：页面口径 vs 交易所原始账户数据"""
import sys, io, json, warnings, urllib.request
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from langchain_core._api.deprecation import LangChainPendingDeprecationWarning
warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)

from core.config_loader import load_settings
from audit.database import AuditDatabase
from exchange.factory import create_broker
from data.market_query import get_live_prices
import data.rate_limit as rl

s = load_settings(Path("."))
db = AuditDatabase(s.db_file)
b = create_broker(s, db)

# 1) 交易所原始数据
acct = b.account(force=True)
pos = b.positions(force=True)
wallet = float(acct.get("totalWalletBalance") or 0)
margin_bal = float(acct.get("totalMarginBalance") or 0)
exch_upnl = float(acct.get("totalUnrealizedProfit") or 0)
avail = float(acct.get("availableBalance") or 0)
print("=== 交易所账户原始数据 (demo) ===")
print(f"totalWalletBalance   = {wallet:.4f}")
print(f"totalMarginBalance   = {margin_bal:.4f}")
print(f"totalUnrealizedProfit= {exch_upnl:.4f}")
print(f"availableBalance     = {avail:.4f}")
print(f"账户内部自洽: wallet+upnl==marginBal ? {abs(wallet+exch_upnl-margin_bal)<0.001}")
for p in pos:
    print(f"  {p['symbol']}: qty={p.get('positionAmt')} entry={p.get('entryPrice')} mark={p.get('markPrice')} upnl={p.get('unRealizedProfit')} isolatedMargin={p.get('isolatedMargin')}")

# 2) baseline 与会话权益口径
baseline = s and json.loads(Path("data/capital_state.json").read_text(encoding="utf-8")) if Path("data/capital_state.json").exists() else {}
print("\n=== capital_state.json ===")
print(baseline)
bl = float(baseline.get("baseline", 0))
session_eq = margin_bal - bl
print(f"session_equity = totalMarginBalance({margin_bal:.4f}) - baseline({bl:.4f}) = {session_eq:.4f}")

# 3) live ticker 价格算的浮盈（页面新口径）
live = get_live_prices([str(p['symbol']) for p in pos], ttl_seconds=0)
print(f"\n=== 实时 ticker 价格 ===")
print(live)
live_upnl = 0.0
for p in pos:
    sym = str(p['symbol']); qty = abs(float(p.get('positionAmt') or 0)); entry = float(p.get('entryPrice') or 0)
    lp = live.get(sym, 0)
    u = (lp - entry) * qty if float(p.get('positionAmt')) > 0 else (entry - lp) * qty
    live_upnl += u
    print(f"  {sym}: live={lp} entry={entry} qty={qty} live_upnl={u:.4f} vs 交易所upnl={p.get('unRealizedProfit')}")

# 4) 页面 /api/status 口径
api = json.loads(urllib.request.urlopen("http://127.0.0.1:8000/api/status", timeout=30).read())
c = api.get("capital") or {}
print("\n=== 页面 /api/status 显示 ===")
print(f"session_equity={c.get('session_equity')}  pnl={c.get('pnl')}  pnl_pct={c.get('pnl_pct')}")
print(f"unrealized_pnl={api.get('unrealized_pnl')}  realized_pnl={api.get('realized_pnl')}")
for p in api.get("positions") or []:
    print(f"  {p['symbol']}: last={p.get('last_price')} upnl={p.get('upnl'):.4f} upnl_pct={p.get('upnl_pct'):.4f} margin={p.get('margin')}")

# 5) 一致性检验
u_api = float(api.get("unrealized_pnl") or 0)
r_api = float(api.get("realized_pnl") or 0)
pnl_api = float(c.get("pnl") or 0)
print("\n=== 一致性检验 ===")
print(f"[1] 页面身份式: unrealized({u_api:.4f}) + realized({r_api:.4f}) = {u_api+r_api:.4f} vs 总盈亏({pnl_api:.4f})  -> {'OK' if abs(u_api+r_api-pnl_api)<0.01 else 'MISMATCH'}")
print(f"[2] 总盈亏 vs 交易所口径(session_eq-500): {session_eq-500:.4f}  -> {'OK' if abs(session_eq-500-pnl_api)<0.01 else 'MISMATCH'}")
print(f"[3] 页面浮盈 vs 交易所totalUnrealizedProfit: {exch_upnl:.4f}  -> {'OK(应接近，实时价与标记价有小差)' if abs(u_api-exch_upnl)<0.5 else '注意差距较大'}")
print(f"[4] 页面浮盈 vs 实时价计算: {live_upnl:.4f}  -> {'OK' if abs(u_api-live_upnl)<0.01 else 'MISMATCH'}")
db.close()
