"""静态核验开仓执行边界；默认离线回归的一部分。"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
ALLOWED_CALLERS = {Path("core/graph.py"), Path("core/service.py")}
CALL = re.compile(r"\b(?:self\.)?broker\.execute\s*\(")

violations: list[str] = []
for path in SRC.rglob("*.py"):
    relative = path.relative_to(SRC)
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if CALL.search(line) and relative not in ALLOWED_CALLERS:
            violations.append(f"{relative}:{number}: {line.strip()}")

factory = (SRC / "exchange/factory.py").read_text(encoding="utf-8")
graph = (SRC / "core/graph.py").read_text(encoding="utf-8")
models = (SRC / "agents/models.py").read_text(encoding="utf-8")
required = {
    "Broker工厂开启统一管线授权": "require_pipeline_authorization = True" in factory,
    "Broker工厂创建进程内签名密钥": "_pipeline_secret = secrets.token_urlsafe" in factory,
    "RiskGuard之后签署实际执行集合": "sign_execution(" in graph,
    "授权签名不进入审计JSON": "pipeline_signature: str | None = Field(default=None, exclude=True)" in models,
}

for name, passed in required.items():
    print(f"  {'PASS' if passed else 'FAIL'}  {name}")
if violations:
    print("  FAIL  发现 Broker 执行旁路：")
    for violation in violations:
        print(f"        {violation}")
else:
    print("  PASS  src 中 Broker.execute 仅由 graph/service 调用")

failed = [name for name, passed in required.items() if not passed]
raise SystemExit(1 if failed or violations else 0)
