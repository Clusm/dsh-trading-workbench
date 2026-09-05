"""网络出口配置：从 .env 读取代理设置并覆盖进程环境变量。

## 为什么需要它

全项目的 httpx 调用（20+ 处）都依赖 `trust_env=True` 的隐式行为，没有任何代码级
代理配置，`.env` 里也没有 proxy 项。这意味着出口完全由「启动这个进程时环境变量
恰好是什么」决定——换个终端、被外层工具注入、或直接双击启动，走的可能就是完全不同
的出口。

实测后果：出口不通时，币安行情 / 持仓同步 / Yahoo / CoinGecko 会集体超时，
表现为 502、持仓加载失败、情报全降级，而且只在运行时才暴露。

曾出现过三次真实故障：
  - 沙箱注入 55625 → Yahoo 7/7 全 403、CoinGecko 与币安全超时（三路情报全灭）
  - 沙箱注入 61068 → 服务持续 `ProxyError 502`，50 分钟内反复崩溃重启后彻底挂掉
  - 直连 → ConnectTimeout（国内网络下币安不可达）

## 为什么用 os.environ 覆盖而不是逐个传 proxy=

`httpx.Client(trust_env=True)` 在**每次创建 client 时**才解析代理，
所以在进程最早期覆盖 `os.environ` 就能让所有现存调用点一次性生效，
不必改动 20+ 处代码，也不会漏掉将来新增的调用点。

已验证：进程内 `os.environ[...] = ...` 之后，
`urllib.request.getproxies()` 立刻返回新值，httpx 随之走新出口。
（注意：`env VAR=... python x.py` 的命令行前缀赋值在外层已注入同名变量时
可能不生效，所以必须在**进程内**覆盖。）
"""
from __future__ import annotations

import os
from pathlib import Path

# 同时写大小写：urllib 在 Windows 上大小写不敏感，在 Linux 上只读大写
PROXY_KEYS = ("HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY")


def _read_env_file(path: Path) -> dict[str, str]:
    """极简 .env 解析，只取需要的键，不引入 dotenv 依赖。"""
    out: dict[str, str] = {}
    if not path.exists():
        return out
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        if not key:
            continue
        out[key] = val.strip().strip('"').strip("'")
    return out


def apply_proxy_env(env_path: str | Path | None = None) -> dict[str, str]:
    """把 .env 里的代理配置写进 os.environ，返回实际生效的键值对。

    必须在任何 httpx 调用之前执行（建议放在入口脚本 import 业务模块之前）。
    .env 里没配代理时不做任何事，保持原有行为。
    """
    path = Path(env_path) if env_path else Path(__file__).resolve().parents[2] / ".env"
    values = _read_env_file(path)

    # 直连开关：环境变量优先，其次 .env。
    # 必须同时读 .env，不能只看 os.environ——本函数通常早于 load_dotenv()
    # 执行（run_web.py 中位于 import 业务模块之前），此时 .env 尚未被 dotenv
    # 载入进程，只看环境变量会让 .env 里的开关静默失效。
    no_proxy_flag = (
        os.getenv("MTA_NO_PROXY", "").strip()
        or values.get("MTA_NO_PROXY", "")
        or values.get("mta_no_proxy", "")
    )
    if no_proxy_flag.strip().lower() in {"1", "true", "yes", "on"}:
        # 为什么要显式写入 NO_PROXY='*' 而不是把代理变量全删掉：
        # Windows 上 urllib.request.getproxies() 的实现是
        #   getproxies_environment() or getproxies_registry()
        # 环境变量一旦为空，就会**回退去读「系统代理」注册表项**，
        # httpx(trust_env=True) 因此仍会走那个可能已经死掉的代理 → 10061 拒绝连接。
        # 写入 NO_PROXY='*' 能让 environment 分支返回非空（{'no':'*'}），
        # 从而阻断注册表回退，并让 httpx 对所有主机绕过代理。
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            # 保留为空值而不是删除：load_settings() 随后会调用load_dotenv，
            # dotenv只填充不存在的键。若这里删除，.env里的旧代理会被再次注入，
            # 使同一进程出现“REST直连、WebSocket却走坏代理”的分裂出口。
            os.environ[key] = ""
            os.environ[key.lower()] = ""
        os.environ["NO_PROXY"] = "*"
        os.environ["no_proxy"] = "*"
        return {}

    applied: dict[str, str] = {}
    for key in PROXY_KEYS:
        val = values.get(key) or values.get(key.lower())
        if not val:
            continue
        os.environ[key] = val
        os.environ[key.lower()] = val
        applied[key] = val
    return applied
