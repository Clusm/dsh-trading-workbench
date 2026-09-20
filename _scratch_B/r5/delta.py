"""全量离线 delta：按同一份清单在树里逐脚本跑，记录 rc（不 check=True 早退）。

用法:
  python delta.py <tree_dir> <out_json> [manifest_source_tree]
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

TREE = Path(sys.argv[1]).resolve()
OUT = Path(sys.argv[2]).resolve()
MANIFEST_TREE = Path(sys.argv[3]).resolve() if len(sys.argv) > 3 else TREE

source = (MANIFEST_TREE / "scripts" / "run_offline_regression.py").read_text(
    encoding="utf-8")
block = re.search(r"SCRIPTS = \((.*?)\n\)", source, re.S).group(1)
scripts = re.findall(r'"([^"]+)"', block)

env = os.environ.copy()
env["EXECUTION_MODE"] = "paper"
env.pop("MTA_PROFILE", None)
env["MTA_NO_PROXY"] = "1"
env["PYTHONUTF8"] = "1"
env["PYTHONIOENCODING"] = "utf-8"
for name in tuple(env):
    upper = name.upper()
    if ("BINANCE" in upper or "SENSENOVA" in upper) and (
            "KEY" in upper or "SECRET" in upper or "TOKEN" in upper):
        env.pop(name, None)

results: dict[str, int] = {}
for script in scripts:
    proc = subprocess.run([sys.executable, script], cwd=TREE, env=env,
                          capture_output=True, text=True, encoding="utf-8",
                          errors="replace")
    results[script] = proc.returncode
    flag = "OK  " if proc.returncode == 0 else "FAIL"
    print(f"{flag} rc={proc.returncode:<3} {script}", flush=True)
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-3:]
        for line in tail:
            print(f"      | {line}", flush=True)

OUT.write_text(json.dumps({"tree": str(TREE), "scripts": scripts,
                           "rc": results}, ensure_ascii=False, indent=2),
               encoding="utf-8")
failed = sorted(k for k, v in results.items() if v != 0)
print(f"\nmanifest={len(scripts)} failed={len(failed)}")
for name in failed:
    print(f"  FAILED {name}")
