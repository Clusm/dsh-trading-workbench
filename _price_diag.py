# -*- coding: utf-8 -*-
"""诊断：直接调用 broker.positions() 与 get_live_prices，比对 /api/status 的价格"""
import sys, io, time, warnings
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))
from langchain_core._api.deprecation import LangChainPendingDeprecationWarning
warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)

from core.config_loader import load_settings
from audit.database import AuditDatabase
from exchange.factory import create_broker
from data.market_query import get_live_prices

s = load_settings(Path("."))
db = AuditDatabase(s.db_file)
b = create_broker(s, db)

for i in range(3):
    t0 = time.time()
    pos = b.positions(force=True)
    dt = time.time() - t0
    print(f"[{i}] positions(force=True) {dt:.1f}s: " + "; ".join(
        f"{p['symbol']} mark={p.get('markPrice')} upnl={p.get('unRealizedProfit')}" for p in pos) or "(no positions)")
    time.sleep(2)

lp = get_live_prices(["SOLUSDT"], ttl_seconds=0)
print("get_live_prices:", lp)
db.close()
