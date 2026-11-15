"""网络环境自愈：清理会让 httpx 在构造阶段直接崩溃的代理白名单条目。

真实故障（2026-11-15）：环境里 NO_PROXY 含 IPv6 方括号写法（例如 [::1]）时，
httpx.Client() 在解析代理白名单阶段就抛 InvalidURL，**任何** HTTP 客户端都无法创建——
包括与代理无关的本地调用。表现为“启动即崩”且报错位置误导。

本模块在包被导入时执行一次，把无法解析的条目摘掉并记一条 warning。
"""

from __future__ import annotations

import logging
import os

__all__ = ["sanitize_proxy_env"]

logger = logging.getLogger("workbench.proxyenv")


def sanitize_proxy_env() -> list[str]:
    """删除 NO_PROXY/no_proxy 里 httpx 无法解析的条目，返回被删除的条目。"""

    removed: list[str] = []
    for key in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(key)
        if not raw:
            continue
        kept: list[str] = []
        for item in raw.split(","):
            entry = item.strip()
            if not entry:
                continue
            candidate = entry.lstrip(".")
            if candidate.startswith("[") or "]" in candidate:
                removed.append(entry)
                continue
            kept.append(entry)
        os.environ[key] = ",".join(kept)
    if removed:
        logger.warning(
            "proxy_env_sanitized removed=%s", ",".join(sorted(set(removed)))
        )
    return removed