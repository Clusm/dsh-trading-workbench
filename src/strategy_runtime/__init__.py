"""交易系统内置的、命名空间隔离的确定性策略计算运行时。

这里仅包含已通过策略卡验证的指标、特征和信号函数；它不访问网络、账户、
订单或项目外文件。具体来源和可复核哈希见同目录 ``PROVENANCE.md``。
"""
from strategy_runtime.features import build_features, clear_feature_cache
from strategy_runtime.signals import REGISTRY, generate_signals

__all__ = ["REGISTRY", "build_features", "clear_feature_cache", "generate_signals"]
