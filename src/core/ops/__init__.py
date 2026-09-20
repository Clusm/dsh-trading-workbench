"""U13 运维子包：进程托管（supervision）与恢复（recovery）。

两个模块都是**纯策略/纯文件**库：不启动真实服务、不发送真实告警、不联网、
不下单。真正宿主上的托管与告警验收归 U12 OPS（需 D01/D02 决策）。
"""
from __future__ import annotations

__all__ = ["recovery", "supervision"]
