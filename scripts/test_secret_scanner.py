"""密钥扫描器的离线回归；测试值通过拼接构造，避免进入扫描器源码。"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scan_tracked_secrets import find_secret_hits  # noqa: E402


def check(name: str, condition: bool) -> None:
    if not condition:
        raise AssertionError(name)
    print(f"  PASS  {name}")


print("\n[1] 示例配置和环境变量名不误报")
check("空值与环境变量名通过", not find_secret_hits(
    "SENSENOVA_API_KEY=\napi_secret: BINANCE_DEMO_API_SECRET\n"
))
check("短测试值通过", not find_secret_hits("api_key='test-key'\n"))

print("\n[2] 高风险凭据只报告位置，不返回匹配值")
provider_token = "sk-" + ("x" * 24)
hits = find_secret_hits("api_key='" + provider_token + "'\n")
check("provider token被发现", any(hit.rule == "provider-token" for hit in hits))
check("赋值凭据被发现", any(hit.rule == "secret-assignment" for hit in hits))
check("结果不含匹配值", all(provider_token not in repr(hit) for hit in hits))

print("\n[3] 私钥和Bearer凭据被发现")
bearer = "Bearer " + ("y" * 30)
private_key_marker = "-----BEGIN " + "PRIVATE KEY-----"
hits = find_secret_hits(private_key_marker + "\n" + bearer + "\n")
check("私钥标记被发现", any(hit.rule == "private-key" for hit in hits))
check("Bearer凭据被发现", any(hit.rule == "bearer-token" for hit in hits))

print("\n结果：PASS 7 / FAIL 0")
