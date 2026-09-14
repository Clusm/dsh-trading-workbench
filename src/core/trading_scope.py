"""统一的数字货币合约交易范围。

本模块把“能不能作为新开仓标的”从提示词、策略扫描和交易所过滤器中抽出来，
成为一个小而明确的领域对象。交易所的完整 instrument 列表只负责说明品种是否
存在及其精度；业务上的开仓白名单则只来自 ``market_universe.core_symbols``。

这两个集合不能混用：

* ``instrument_symbols``：交易所当前可识别的合约，允许用于持仓管理和精度校验；
* ``open_symbols``：项目明确批准的开仓品种，候选池、AI schema、策略和最终下单
  闸门都必须收敛到这里；
* ``management_symbols``：当前持仓/挂单的并集，即使某个品种后来离开开仓白名单，
  也仍然必须能够平仓、减仓和撤改单。

这里不改变风险参数、仓位上限或杠杆档位，只修复“数据源候选范围”和“最终开仓
授权范围”不一致的问题。
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import re
from typing import Any, Iterable, Mapping


_SYMBOL_RE = re.compile(r"^[A-Z0-9]+USDT$")


def normalize_symbol(value: Any) -> str:
    """标准化 symbol；不负责把裸币名自动拼成 USDT，避免悄悄放宽范围。"""
    return str(value or "").strip().upper()


def _normalize_configured_symbols(symbols: Iterable[Any]) -> tuple[str, ...]:
    """校验配置白名单，并保持配置顺序供提示词和扫描器稳定使用。"""
    normalized: list[str] = []
    seen: set[str] = set()
    for raw in symbols:
        symbol = normalize_symbol(raw)
        if not symbol or not _SYMBOL_RE.fullmatch(symbol):
            raise ValueError(f"交易范围包含非法合约 symbol: {raw!r}")
        if symbol in seen:
            raise ValueError(f"交易范围包含重复合约 symbol: {symbol}")
        seen.add(symbol)
        normalized.append(symbol)
    if not normalized:
        raise ValueError("交易范围不能为空；缺少开仓白名单时必须 fail closed")
    return tuple(normalized)


def _ordered_unique(values: Iterable[Any]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        symbol = normalize_symbol(raw)
        if symbol and symbol not in seen:
            seen.add(symbol)
            result.append(symbol)
    return result


@dataclass(frozen=True)
class TradingScope:
    """当前发布配置下的开仓范围及其稳定指纹。"""

    open_symbols: tuple[str, ...]
    scope_hash: str

    @classmethod
    def from_symbols(cls, symbols: Iterable[Any]) -> "TradingScope":
        configured = _normalize_configured_symbols(symbols)
        # 指纹按排序后的集合计算：调整 YAML 的展示顺序不会伪造一次范围变更，
        # 但增加、删除或修改任何开仓品种都会生成新的授权边界。
        digest_input = json.dumps(sorted(configured), ensure_ascii=False, separators=(",", ":"))
        digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
        return cls(open_symbols=configured, scope_hash=digest)

    @property
    def symbols(self) -> tuple[str, ...]:
        """兼容调用方的只读别名。"""
        return self.open_symbols

    def allows_open(self, symbol: Any, instruments: Iterable[Any] | None = None) -> bool:
        """判断某 symbol 是否同时满足业务白名单和可用 instrument 条件。"""
        normalized = normalize_symbol(symbol)
        if normalized not in self.open_symbols:
            return False
        if instruments is None:
            return True
        available = {normalize_symbol(item) for item in instruments if normalize_symbol(item)}
        return not available or normalized in available

    def allowed_open_symbols(self, instruments: Iterable[Any] | None = None) -> list[str]:
        """返回可新开仓的有序交集；instrument 未加载时不把范围误缩成空集。"""
        if instruments is None:
            return list(self.open_symbols)
        available = {normalize_symbol(item) for item in instruments if normalize_symbol(item)}
        if not available:
            return list(self.open_symbols)
        return [symbol for symbol in self.open_symbols if symbol in available]

    def candidate_symbols(self, ranked: Iterable[Any] | None = None,
                          instruments: Iterable[Any] | None = None) -> list[str]:
        """把任意市场排名裁剪到开仓范围，保持排名顺序并去重。"""
        allowed = set(self.allowed_open_symbols(instruments))
        ordered = _ordered_unique(ranked if ranked is not None else self.open_symbols)
        return [symbol for symbol in ordered if symbol in allowed]

    def management_symbols(self, positions: Iterable[Mapping[str, Any]] | None = None,
                           pending_orders: Iterable[Mapping[str, Any]] | None = None) -> list[str]:
        """收集持仓和挂单 symbol，供行情订阅与平仓管理使用。"""
        values: list[Any] = []
        for item in list(positions or []) + list(pending_orders or []):
            if isinstance(item, Mapping):
                values.append(item.get("symbol"))
        return _ordered_unique(values)

    def as_dict(self) -> dict[str, Any]:
        """用于日志、运行快照和发布验收报告。"""
        return {"open_symbols": list(self.open_symbols), "scope_hash": self.scope_hash}


def scope_from_settings(raw_settings: Mapping[str, Any]) -> TradingScope:
    """从已加载的 settings 构造范围；配置缺失时直接失败而不是全市场回退。"""
    section = raw_settings.get("market_universe", {}) if isinstance(raw_settings, Mapping) else {}
    symbols = section.get("core_symbols") if isinstance(section, Mapping) else None
    return TradingScope.from_symbols(symbols or [])
