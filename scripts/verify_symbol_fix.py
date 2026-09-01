"""端到端验证：真实拉取币安行情，对比核心池过滤前后的采集标的与字符数。

同时验证 extra_symbols（当前持仓）会被强制纳入采集。

运行：python scripts/verify_symbol_fix.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import httpx  # noqa: E402

# 沙箱里 env 前缀赋值会被覆盖，必须显式注入代理
_orig_init = httpx.Client.__init__


def _patched(self, *a, **kw):
    kw["trust_env"] = False
    kw["proxy"] = "http://127.0.0.1:10808"
    _orig_init(self, *a, **kw)


httpx.Client.__init__ = _patched

import yaml  # noqa: E402

from data import binance_public as bp  # noqa: E402

CORE = yaml.safe_load(open("config/settings.yaml", encoding="utf-8"))["market_universe"]["core_symbols"]
NOISE = {"SOXLUSDT", "XAGUSDT", "XAUUSDT", "SKHYNIXUSDT", "SNDKUSDT", "SPCXUSDT"}
ANALYZE_TOP = 10


def report(tag: str, brief, core: list[str] | None) -> None:
    syms = [m["symbol"] for m in brief.metrics]
    jm = json.dumps(brief.metrics, ensure_ascii=False)
    noise_hit = sorted(set(syms) & NOISE)
    in_core = [s for s in syms if s in CORE]
    print(f"\n--- {tag} ---")
    print(f"  采集标的({len(syms)}): {' '.join(syms)}")
    print(f"  metrics 字符数 : {len(jm)}")
    print(f"  regime         : {brief.regime}")
    print(f"  errors         : {len(brief.errors)}")
    print(f"  命中核心池     : {len(in_core)}/{len(syms)}")
    print(f"  噪声标的       : {len(noise_hit)}" + (f"  {noise_hit}" if noise_hit else "  （无）"))
    return jm, syms


print("=" * 70)
print(f"核心池 {len(CORE)} 只：{' '.join(CORE)}")
print("=" * 70)

print("\n[1/3] 修复前：不传 core_symbols（全市场按成交额盲选）")
t = time.time()
old = bp.build_market_brief(top_n=20, analyze_top=ANALYZE_TOP)
old_jm, old_syms = report(f"修复前（{time.time()-t:.1f}s）", old, None)

print("\n[2/3] 修复后：传入核心池白名单")
t = time.time()
new = bp.build_market_brief(top_n=20, analyze_top=ANALYZE_TOP, core_symbols=CORE)
new_jm, new_syms = report(f"修复后（{time.time()-t:.1f}s）", new, CORE)

print("\n[3/3] 持仓强制纳入：指定成交额最低的 DOTUSDT 为持仓，analyze_top=3")
t = time.time()
held = bp.build_market_brief(top_n=20, analyze_top=3, core_symbols=CORE, extra_symbols=["DOTUSDT"])
held_syms = [m["symbol"] for m in held.metrics]
print(f"  analyze_top=3 采集到: {' '.join(held_syms)}  ({time.time()-t:.1f}s)")
held_ok = "DOTUSDT" in held_syms
print(f"  持仓 DOTUSDT 是否被保留: {'是' if held_ok else '否  <-- 失败'}")

print("\n" + "=" * 70)
print("结论")
print("=" * 70)
old_noise = len(set(old_syms) & NOISE)
new_noise = len(set(new_syms) & NOISE)
saved = len(old_jm) - len(new_jm)
print(f"  噪声标的      : 修复前 {old_noise}/{len(old_syms)}  ->  修复后 {new_noise}/{len(new_syms)}")
print(f"  核心池命中    : 修复前 {len([s for s in old_syms if s in CORE])}/{len(old_syms)}"
      f"  ->  修复后 {len([s for s in new_syms if s in CORE])}/{len(new_syms)}")
print(f"  metrics 字符  : {len(old_jm)} -> {len(new_jm)}")
print(f"  regime        : {old.regime} -> {new.regime}")

ok = True
if new_noise != 0:
    print("  [FAIL] 修复后仍有噪声标的")
    ok = False
if not set(new_syms) <= set(CORE):
    print(f"  [FAIL] 修复后仍有核心池外标的: {sorted(set(new_syms) - set(CORE))}")
    ok = False
if not held_ok:
    print("  [FAIL] 持仓标的未被强制纳入")
    ok = False
if len(new_syms) < 5:
    print(f"  [FAIL] 修复后样本量过少({len(new_syms)})，regime 判定会变脆")
    ok = False

print("\n  => " + ("全部通过：噪声归零、标的 100% 来自核心池、持仓强制保留" if ok else "存在问题，见上"))
sys.exit(0 if ok else 1)
