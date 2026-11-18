"""M1.0 workbench service skeleton (read-only public market probe)."""

from .proxyenv import sanitize_proxy_env as _sanitize_proxy_env

# 导入即自愈：NO_PROXY 里的非法条目会让 httpx 在构造阶段崩溃，必须在任何
# 客户端创建之前清理。放在包入口是最稳的位置——测试与服务都会经过这里。
_sanitize_proxy_env()

__all__ = ["__version__"]

__version__ = "0.2.1"