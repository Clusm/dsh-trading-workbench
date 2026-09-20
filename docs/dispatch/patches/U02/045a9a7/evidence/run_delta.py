"""全量离线回归 delta：逐条非 fail-fast 跑该树自己的 SCRIPTS 清单，输出退出码。

用法：python run_delta.py <tree>
环境与 ``scripts/run_offline_regression.py:main()`` 同口径（paper、无代理、UTF-8，
剔除 MTA_PROFILE 与 *KEY/SECRET/TOKEN*）。
"""
from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

PYTHON = r"C:/Users/Tao/Desktop/币安/multi-agent-trader/.venv/Scripts/python.exe"


def scripts_of(tree: Path) -> list[str]:
    src = (tree / "scripts" / "run_offline_regression.py").read_text(encoding="utf-8")
    module = ast.parse(src)
    for node in module.body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "SCRIPTS" for t in node.targets):
            return [item.value for item in node.value.elts]
    raise SystemExit("SCRIPTS not found")


def env_for() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k != "MTA_PROFILE"
           and not any(tag in k.upper()
                       for tag in ("BINANCE", "SENSENOVA", "API_KEY", "API_SECRET",
                                   "TOKEN"))}
    env.update({"EXECUTION_MODE": "paper", "MTA_NO_PROXY": "1",
                "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"})
    return env


def main() -> int:
    tree = Path(sys.argv[1]).resolve()
    names = scripts_of(tree)
    failed: list[str] = []
    tails: list[str] = []
    for name in names:
        proc = subprocess.run([PYTHON, name], cwd=tree, env=env_for(),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=1800)
        if proc.returncode != 0:
            failed.append(name)
            tails.append(f"### {name} rc={proc.returncode}\n"
                         + "\n".join(((proc.stdout or "") + (proc.stderr or "")
                                      ).strip().splitlines()[-6:]))
        print(f"{proc.returncode:>3} {name}", flush=True)
    print(f"\nmanifest_entries={len(names)} FAILED={len(failed)} {failed}")
    for tail in tails:
        print(tail)
    return 0


if __name__ == "__main__":
    sys.exit(main())
