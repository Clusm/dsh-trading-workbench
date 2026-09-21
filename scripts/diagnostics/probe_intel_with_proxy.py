"""临时探针：强制所有 httpx 出口走指定代理，对比三路情报采集的耗时与完整度。

用法:
    python scripts/probe_intel_with_proxy.py 10808     # 走 10808
    python scripts/probe_intel_with_proxy.py none      # 走环境变量(沙箱 55625)
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, "src")

from dotenv import load_dotenv

load_dotenv()

PROXY = sys.argv[1] if len(sys.argv) > 1 else "10808"


def install(proxy: str) -> None:
    """monkey-patch httpx.Client / httpx.get，强制走指定代理。"""
    import httpx

    orig_init = httpx.Client.__init__

    def patched_init(self, *args, **kwargs):
        kwargs["trust_env"] = False
        if proxy != "none":
            kwargs["proxy"] = f"http://127.0.0.1:{proxy}"
        orig_init(self, *args, **kwargs)

    httpx.Client.__init__ = patched_init


install(PROXY)
print(f"=== 代理模式: {PROXY} ===", flush=True)

from data import macro_public as mp  # noqa: E402
from data import onchain_public as op  # noqa: E402
from data import binance_public as bp  # noqa: E402

# ---------- 宏观 ----------
t = time.time()
try:
    mb = mp.build_macro_brief(tavily_api_key=os.getenv("TAVILY_API_KEY"))
    ok = len(mb.notes)
    print(f"[宏观] {time.time()-t:6.1f}s  行情项={ok}/7  errors={len(mb.errors)}  bias={mb.market_bias}", flush=True)
    for n in mb.notes:
        print(f"        - {n}", flush=True)
    for e in mb.errors:
        print(f"        ! {e[:80]}", flush=True)
except Exception as e:
    print(f"[宏观] FAIL {type(e).__name__}: {e}", flush=True)

# ---------- 链上 ----------
t = time.time()
try:
    ob = op.build_onchain_brief(etherscan_api_key=os.getenv("ETHERSCAN_API_KEY") or None)
    d = ob.model_dump()
    filled = {k: v for k, v in d.items() if v not in (None, [], {}, "")}
    print(f"[链上] {time.time()-t:6.1f}s  errors={len(ob.errors)}  非空字段={len(filled)}/{len(d)}", flush=True)
    for k, v in filled.items():
        s = str(v)
        print(f"        - {k}: {s[:150]}", flush=True)
    for e in ob.errors:
        print(f"        ! {e[:80]}", flush=True)
except Exception as e:
    print(f"[链上] FAIL {type(e).__name__}: {e}", flush=True)

# ---------- 市场 ----------
t = time.time()
try:
    mkt = bp.build_market_brief(top_n=40, analyze_top=10)
    d = mkt.model_dump()
    metrics = d.get("metrics") or []
    print(f"[市场] {time.time()-t:6.1f}s  metrics={len(metrics)}  errors={len(mkt.errors)}  regime={d.get('regime')}", flush=True)
    if metrics:
        m0 = metrics[0]
        nulls = [k for k, v in m0.items() if v is None]
        print(f"        单标的字段={len(m0)}  空字段={len(nulls)} {nulls}", flush=True)
    for e in mkt.errors[:10]:
        print(f"        ! {e[:80]}", flush=True)
except Exception as e:
    print(f"[市场] FAIL {type(e).__name__}: {e}", flush=True)
