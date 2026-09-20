"""USD-M 成交流水的纯解析、匹配与生命周期聚合规则。

本模块不执行网络请求，也不直接写数据库。REST ``userTrades``、私有流和离线
回放都先转换成 :class:`ExecutionFill`，再复用同一套规则，避免某个入口因为
字段名、排序或浮点误差不同而产生另一套盈亏解释。

几个刻意保守的不变量：

* ``realizedPnl=0`` 是交易所的合法事实，只有字段缺失/非法时才是 ``None``；
* 平仓成交必须同时满足标的、退出方向、持仓侧和生命周期时间边界；
* 部分成交按实际数量 FIFO 分配，永远不使用 98%/105% 经验比例判定平仓；
* 未知手续费或非 USDT 费用不能被填成 0，也不能被手算值覆盖。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_DOWN
import hashlib
import json
import os
import time
from typing import Any, Iterable, Mapping, TYPE_CHECKING

from core.runtime_identity import derive_account_ref

if TYPE_CHECKING:
    from audit.database import AuditDatabase


ZERO = Decimal("0")

#: 账户引用来源：显式声明（U15 派生值）或旧密钥摘要回退。
ACCOUNT_REF_SOURCE_DECLARED = "declared"
ACCOUNT_REF_SOURCE_LEGACY_KEY = "legacy-key-digest"


class InvalidExecutionFill(ValueError):
    """成交缺少真值所需字段，调用方必须拒绝入账。"""


def _decimal(value: Any, field: str, *, required: bool = False) -> Decimal | None:
    if value is None or value == "":
        if required:
            raise InvalidExecutionFill(f"成交缺少 {field}")
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise InvalidExecutionFill(f"成交 {field} 不是有效数字") from exc
    if not result.is_finite():
        raise InvalidExecutionFill(f"成交 {field} 不是有限数字")
    return result


def _integer(value: Any, field: str, *, required: bool = False) -> int | None:
    if value is None or value == "":
        if required:
            raise InvalidExecutionFill(f"成交缺少 {field}")
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise InvalidExecutionFill(f"成交 {field} 不是整数") from exc


def _text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    text = str(value).strip()
    return text or None


def _declared_account_ref(declared_account_ref: str | None) -> str:
    """账户声明来源：显式入参优先，其次 ``MTA_ACCOUNT_REF``（U15 登记项）。"""
    if declared_account_ref is not None:
        return str(declared_account_ref).strip()
    return str(os.getenv("MTA_ACCOUNT_REF") or "").strip()


def account_ref_source(
    api_key: str, environment: str, *, declared_account_ref: str | None = None,
) -> str:
    """账户引用的来源标记，供启动自检区分"已解耦"与"仍在用密钥摘要"。

    这不是"已解耦"的证明，而是一个**可观测**状态：``legacy-key-digest`` 表示该
    账户尚未被显式声明，账本 account_ref 仍会随密钥轮换漂移（U15/B_r1 P1-4）。
    """
    if _declared_account_ref(declared_account_ref):
        return ACCOUNT_REF_SOURCE_DECLARED
    return ACCOUNT_REF_SOURCE_LEGACY_KEY


def account_reference(
    api_key: str, environment: str, *, declared_account_ref: str | None = None,
) -> str:
    """生成不含凭据原文的账户引用，供审计/生命周期/收入幂等键分区使用。

    **优先消费 U15 的派生值**：声明了账户（``declared_account_ref`` 入参或
    ``MTA_ACCOUNT_REF``）时返回 ``<env>:<declared>``——与
    :func:`core.runtime_identity.derive_account_ref`、K01 ``RuntimeIdentity.account_ref``
    逐字一致，且**与密钥轮换解耦**，因此 K03 幂等键
    ``(environment, account_ref, income_type, tran_id)`` 的分区不会在轮换时被切裂
    （规格 §12 L9 / U15/B_r1 P1-4）。

    **未声明账户时保留旧的密钥摘要形态**（``<env>:sha256(api_key)[:16]``）：旧库
    的收入/生命周期/成交行本就按这个值分区，静默切换会把同一账户的历史数据切成
    两个域。因此这里刻意 fail-visible 而非 fail-open：调用方用
    :func:`account_ref_source` 得到 ``legacy-key-digest`` 即知需要迁移，重分区按
    U02 交付报告的迁移建议显式执行（归属 U13）。
    """
    declared = _declared_account_ref(declared_account_ref)
    if declared:
        return derive_account_ref(environment, declared_account_ref=declared).value
    digest = hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()[:16]
    return f"{str(environment or 'unknown').lower()}:{digest}"


def _trade_id(raw: Mapping[str, Any]) -> str | None:
    value = _text(raw.get("trade_id", raw.get("tradeId", raw.get("id", raw.get("t")))))
    # 私有流非成交状态可能用 -1；它不是可用于幂等的交易所成交身份。
    return None if value in {None, "-1", "0"} else value


def _order_id(raw: Mapping[str, Any]) -> str | None:
    return _text(raw.get("order_id", raw.get("orderId", raw.get("i"))))


def _position_side(raw: Mapping[str, Any], default: str = "BOTH") -> str:
    value = str(raw.get("position_side", raw.get("positionSide", raw.get("ps", default))) or default)
    value = value.upper()
    if value not in {"BOTH", "LONG", "SHORT"}:
        raise InvalidExecutionFill(f"不支持的持仓侧: {value}")
    return value


@dataclass(frozen=True)
class ExecutionFill:
    """已经过字段校验的单笔交易所成交。"""

    fill_key: str
    environment: str
    account_ref: str
    symbol: str
    trade_id: str | None
    order_id: str | None
    side: str
    position_side: str
    qty: Decimal
    price: Decimal
    realized_pnl: Decimal | None
    commission: Decimal | None
    commission_asset: str | None
    trade_time_ms: int
    source: str
    raw: dict[str, Any]

    @property
    def realized_pnl_known(self) -> bool:
        return self.realized_pnl is not None

    @property
    def commission_known(self) -> bool:
        return self.commission is not None

    def as_storage_row(self, *, received_at: str) -> dict[str, Any]:
        """转换为 SQLite 可直接绑定的标量，不把 Decimal 交给 sqlite3。"""
        return {
            "fill_key": self.fill_key,
            "environment": self.environment,
            "account_ref": self.account_ref,
            "symbol": self.symbol,
            "trade_id": self.trade_id,
            "order_id": self.order_id,
            "side": self.side,
            "position_side": self.position_side,
            "qty": float(self.qty),
            "price": float(self.price),
            "realized_pnl": float(self.realized_pnl) if self.realized_pnl is not None else None,
            "commission": float(self.commission) if self.commission is not None else None,
            "commission_asset": self.commission_asset,
            "trade_time_ms": self.trade_time_ms,
            "source": self.source,
            "received_at": received_at,
            "raw_json": json.dumps(self.raw, ensure_ascii=False, separators=(",", ":"), default=str),
        }


def normalize_execution_fill(
    raw: Mapping[str, Any], *, environment: str, account_ref: str,
    source: str, default_position_side: str = "BOTH",
) -> ExecutionFill:
    """规范化 Binance REST/WS 成交；非法事实直接抛错而不是静默跳过。"""
    if not isinstance(raw, Mapping):
        raise InvalidExecutionFill("成交必须是对象")
    symbol = str(raw.get("symbol", raw.get("s", "")) or "").upper().strip()
    side = str(raw.get("side", raw.get("S", "")) or "").upper().strip()
    if not symbol:
        raise InvalidExecutionFill("成交缺少 symbol")
    if side not in {"BUY", "SELL"}:
        raise InvalidExecutionFill(f"成交方向无效: {side!r}")
    qty = _decimal(raw.get("qty", raw.get("quantity", raw.get("l", raw.get("z")))), "qty", required=True)
    price = _decimal(raw.get("price", raw.get("p", raw.get("ap"))), "price", required=True)
    trade_time_ms = _integer(
        raw.get("trade_time_ms", raw.get("time", raw.get("T", raw.get("E")))),
        "trade_time_ms", required=True,
    )
    if qty is None or qty <= ZERO:
        raise InvalidExecutionFill("成交 qty 必须大于 0")
    if price is None or price <= ZERO:
        raise InvalidExecutionFill("成交 price 必须大于 0")
    if trade_time_ms is None or trade_time_ms <= 0:
        raise InvalidExecutionFill("成交 trade_time_ms 必须大于 0")

    trade_id = _trade_id(raw)
    order_id = _order_id(raw)
    position_side = _position_side(raw, default_position_side)
    realized_pnl = _decimal(
        raw.get("realized_pnl", raw.get("realizedPnl", raw.get("rp"))),
        "realizedPnl",
    )
    commission = _decimal(
        raw.get("commission", raw.get("n", raw.get("commissionAmount"))),
        "commission",
    )
    commission_asset = _text(
        raw.get("commission_asset", raw.get("commissionAsset", raw.get("N")))
    )
    environment = str(environment or "unknown").lower()
    account_ref = str(account_ref or "unknown")
    source = str(source or "unknown")
    if trade_id is not None:
        fill_key = f"{environment}:{account_ref}:{trade_id}"
    else:
        # 没有 trade_id 的回退身份只用于私有流非标准 payload；把所有经济字段纳入
        # 摘要，确保 REST/WS 同一成交仍可去重，疑似相同但无法证明相同的事件不会合并。
        identity = {
            "environment": environment, "account_ref": account_ref, "symbol": symbol,
            "order_id": order_id, "side": side, "position_side": position_side,
            "qty": str(qty), "price": str(price), "time": trade_time_ms,
            "realized_pnl": str(realized_pnl) if realized_pnl is not None else None,
            "commission": str(commission) if commission is not None else None,
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:32]
        fill_key = f"{environment}:{account_ref}:synthetic:{digest}"
    return ExecutionFill(
        fill_key=fill_key, environment=environment, account_ref=account_ref,
        symbol=symbol, trade_id=trade_id, order_id=order_id, side=side,
        position_side=position_side, qty=qty, price=price,
        realized_pnl=realized_pnl, commission=commission,
        commission_asset=commission_asset, trade_time_ms=trade_time_ms,
        source=source, raw=dict(raw),
    )


def _stable_id(fill: ExecutionFill) -> tuple[int, int, str]:
    try:
        order = int(fill.trade_id or "0")
    except (TypeError, ValueError):
        order = 0
    return fill.trade_time_ms, order, fill.fill_key


def sort_fills(fills: Iterable[ExecutionFill]) -> list[ExecutionFill]:
    """按真实成交时间、交易所稳定ID排序；不依赖接口默认顺序。"""
    return sorted(list(fills), key=_stable_id)


def close_side(position_side: str) -> str:
    side = str(position_side or "").upper()
    if side == "LONG":
        return "SELL"
    if side == "SHORT":
        return "BUY"
    raise ValueError(f"生命周期必须使用 LONG/SHORT 持仓侧: {position_side!r}")


def position_side_matches(fill: ExecutionFill, position_side: str) -> bool:
    """显式持仓侧必须相同；BOTH 只作为单向模式对 LONG/SHORT 的兼容事实。"""
    expected = str(position_side or "").upper()
    return expected in {"LONG", "SHORT"} and fill.position_side in {"BOTH", expected}


def select_close_fills(
    fills: Iterable[ExecutionFill], *, symbol: str, position_side: str,
    opened_at_ms: int, upper_bound_ms: int | None = None,
) -> list[ExecutionFill]:
    """筛选满足完整生命周期边界的平仓成交。

    查询可以向前重叠若干分钟，但 ``opened_at_ms`` 是匹配的硬下界；因此开仓前
    方向相反的成交只能留在流水中，不能被解释为本次平仓。
    """
    symbol = str(symbol or "").upper()
    wanted_side = close_side(position_side)
    selected = []
    for fill in fills:
        if fill.symbol != symbol or fill.side != wanted_side:
            continue
        if not position_side_matches(fill, position_side):
            continue
        if fill.trade_time_ms < int(opened_at_ms):
            continue
        if upper_bound_ms is not None and fill.trade_time_ms > int(upper_bound_ms):
            continue
        selected.append(fill)
    return sort_fills(selected)


@dataclass(frozen=True)
class FillAggregate:
    """一组成交的数量/价格/费用聚合；完整性单独保留。"""

    qty: Decimal
    notional: Decimal
    average_price: Decimal
    realized_pnl: Decimal | None
    commission: Decimal | None
    commission_asset: str | None
    realized_complete: bool
    commission_complete: bool
    trade_ids: tuple[str, ...]


def aggregate_fills(fills: Iterable[ExecutionFill]) -> FillAggregate:
    values = list(fills)
    if not values:
        raise ValueError("不能聚合空成交")
    qty = sum((row.qty for row in values), ZERO)
    notional = sum((row.qty * row.price for row in values), ZERO)
    realized_complete = all(row.realized_pnl is not None for row in values)
    commission_complete = all(row.commission is not None for row in values)
    assets = {str(row.commission_asset or "").upper() for row in values if row.commission is not None}
    same_asset = len(assets) <= 1
    commission_complete = commission_complete and same_asset
    realized = sum((row.realized_pnl or ZERO for row in values), ZERO) if realized_complete else None
    commission = sum((row.commission or ZERO for row in values), ZERO) if commission_complete else None
    return FillAggregate(
        qty=qty, notional=notional,
        average_price=(notional / qty if qty else ZERO),
        realized_pnl=realized, commission=commission,
        commission_asset=next(iter(assets), None),
        realized_complete=realized_complete,
        commission_complete=commission_complete,
        trade_ids=tuple(row.trade_id for row in values if row.trade_id is not None),
    )


@dataclass(frozen=True)
class FillAllocation:
    """某笔成交分配给一个生命周期的实际数量。"""

    fill_key: str
    allocated_qty: Decimal
    role: str


def quantize_step(value: Decimal, step_size: Decimal | None) -> Decimal:
    """按合约 stepSize 向下取整，避免分配出交易所不接受的数量。"""
    if step_size is None or step_size <= ZERO:
        return value
    return (value / step_size).to_integral_value(rounding=ROUND_DOWN) * step_size


def allocate_fifo(
    fills: Iterable[ExecutionFill], remaining_qty: Decimal,
    *, step_size: Decimal | None = None, role: str = "EXIT",
) -> tuple[list[FillAllocation], Decimal]:
    """按时间 FIFO 抵扣生命周期剩余数量，返回分配结果和未抵扣数量。"""
    remaining = max(ZERO, Decimal(remaining_qty))
    allocations: list[FillAllocation] = []
    for fill in sort_fills(fills):
        if remaining <= ZERO:
            break
        allocated = quantize_step(min(fill.qty, remaining), step_size)
        if allocated <= ZERO:
            continue
        allocations.append(FillAllocation(fill_key=fill.fill_key, allocated_qty=allocated, role=role))
        remaining -= allocated
    return allocations, max(ZERO, remaining)


def compute_net_pnl(
    gross_realized: Decimal | None, entry_fee: Decimal | None,
    exit_fee: Decimal | None, funding_cashflow: Decimal | None,
) -> Decimal | None:
    """统一净收益公式；任一关键成本未知就返回 None。"""
    if any(value is None for value in (gross_realized, entry_fee, exit_fee, funding_cashflow)):
        return None
    return gross_realized - entry_fee - exit_fee + funding_cashflow


#: 只有这些收入类型可以进入资金费现金流；COMMISSION/REALIZED_PNL 已由 fill 计入，
#: 再次加进 funding_cashflow 就是重复计费（U01 DL05）。
FUNDING_CASHFLOW_TYPES = frozenset({"FUNDING_FEE", "INSURANCE_CLEAR"})


def aggregate_funding_cashflow(income_rows: Iterable[Mapping[str, Any]]) -> tuple[Decimal, tuple[str, ...]]:
    """把收入行折成资金费现金流，并回带被排除的类型（不静默丢弃）。

    ``COMMISSION``/``REALIZED_PNL`` 属于 fill 侧口径，只做交叉核对，不进入本函数
    返回值；调用方可用第二个返回值把这些行交给 ``exchange.income_reconciliation``
    的会话勾稽去核对，而不是加进 ``compute_net_pnl`` 的 ``funding_cashflow``。
    """
    total = ZERO
    excluded: list[str] = []
    for row in income_rows:
        income_type = str(row.get("incomeType") or row.get("income_type") or "").strip().upper()
        if income_type not in FUNDING_CASHFLOW_TYPES:
            if income_type:
                excluded.append(income_type)
            continue
        raw = row.get("income", row.get("amount"))
        value = _decimal(raw, "income")
        if value is None:
            raise InvalidExecutionFill(f"资金费行缺少 income 金额: {income_type}")
        total += value
    return total, tuple(dict.fromkeys(excluded))


def quantity_is_flat(remaining_qty: Decimal, step_size: Decimal | None) -> bool:
    """使用合约步长判断是否真正归零，不使用经验百分比。"""
    tolerance = (step_size / Decimal("2")) if step_size and step_size > ZERO else Decimal("0.000000001")
    return abs(remaining_qty) <= tolerance


@dataclass(frozen=True)
class LifecycleReconciliation:
    """一次补账对一个生命周期产生的可审计结果。"""

    lifecycle_id: str
    inserted_allocations: int
    allocated_qty: Decimal
    remaining_qty: Decimal
    status: str
    flat_evidence: bool
    actual_closed_at_ms: int | None
    gross_realized: Decimal | None
    exit_fee: Decimal | None
    cost_status: str
    attribution_status: str
    net_pnl: Decimal | None
    allocation_fill_keys: tuple[str, ...]


def _decimal_from_db(value: Any) -> Decimal | None:
    return _decimal(value, "database value")


class TradeReconciliationBook:
    """把规范化成交持久化并按生命周期做幂等分配。

    网络抓取、账户快照和开仓论点都由调用方提供；本类只在数据库锁内维护成交
    消费关系。它不创建订单、不撤保护单，因而补账失败不会改变交易所状态。
    """

    def __init__(self, db: "AuditDatabase", *, environment: str, account_ref: str) -> None:
        self.db = db
        self.environment = str(environment or "unknown").lower()
        self.account_ref = str(account_ref or "unknown")
        self.release_id = str(getattr(db, "release_id", "legacy") or "legacy")

    def ingest(
        self, rows: Iterable[Mapping[str, Any]], *, source: str,
        default_position_side: str = "BOTH", received_at: str | None = None,
    ) -> tuple[list[ExecutionFill], int]:
        """统一接收 REST/WS 成交；任何不完整成交批次整体拒绝入账。"""
        normalized = [
            normalize_execution_fill(
                row, environment=self.environment, account_ref=self.account_ref,
                source=source, default_position_side=default_position_side,
            )
            for row in rows
        ]
        if not normalized:
            return [], 0
        timestamp = received_at or _utc_now_fallback()
        inserted = self.db.insert_execution_fills(
            fill.as_storage_row(received_at=timestamp) for fill in normalized
        )
        return normalized, inserted

    def ensure_lifecycle(
        self, *, symbol: str, position_side: str, entry_qty: Decimal,
        entry_price: Decimal, opened_at_ms: int | None,
        attribution_status: str = "UNKNOWN", cost_status: str = "UNKNOWN",
        entry_release_id: str | None = None, external: bool = False,
        raw: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """确保同一账户/标的/持仓侧只有一个活动生命周期。"""
        symbol = str(symbol or "").upper()
        position_side = str(position_side or "").upper()
        existing = self.db.get_active_position_lifecycle(
            environment=self.environment, account_ref=self.account_ref,
            symbol=symbol, position_side=position_side,
        )
        if existing is not None:
            return dict(existing)
        opened = int(opened_at_ms or int(time.time() * 1000))
        identity = {
            "environment": self.environment, "account_ref": self.account_ref,
            "symbol": symbol, "position_side": position_side, "opened_at_ms": opened,
            "entry_release_id": entry_release_id, "external": bool(external),
        }
        digest = hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:28]
        lifecycle_id = f"mta-lifecycle-{digest}"
        self.db.create_position_lifecycle(
            lifecycle_id=lifecycle_id, environment=self.environment,
            account_ref=self.account_ref, symbol=symbol, position_side=position_side,
            opened_at_ms=opened, entry_qty=float(entry_qty),
            entry_notional=float(entry_qty * entry_price),
            attribution_status=attribution_status, cost_status=cost_status,
            entry_release_id=entry_release_id,
            release_id=getattr(self.db, "release_id", self.release_id),
            external=external, raw=raw,
        )
        result = self.db.fetch_one(
            "SELECT * FROM position_lifecycles WHERE lifecycle_id=?", (lifecycle_id,))
        if result is None:
            raise RuntimeError("生命周期创建后无法读取")
        return dict(result)

    def reconcile(
        self, lifecycle_id: str, fills: Iterable[ExecutionFill], *, position_qty: Decimal | None,
        step_size: Decimal | None = None,
    ) -> LifecycleReconciliation:
        """把新成交按 FIFO 分配到生命周期，并原子更新汇总和消费关系。"""
        lifecycle = self.db.fetch_one(
            "SELECT * FROM position_lifecycles WHERE lifecycle_id=?", (lifecycle_id,))
        if lifecycle is None:
            raise ValueError(f"未知生命周期: {lifecycle_id}")
        rows = list(fills)
        existing_allocations = self.db.fetch_lifecycle_allocations(lifecycle_id)
        existing_keys = {str(row["fill_key"]) for row in existing_allocations}
        entry_qty = _decimal_from_db(lifecycle["entry_qty"]) or ZERO
        allocated_before = sum(
            (_decimal_from_db(row["allocated_qty"]) or ZERO for row in existing_allocations), ZERO)
        remaining_before = max(ZERO, entry_qty - allocated_before)
        candidates = [row for row in rows if row.fill_key not in existing_keys]
        candidate_qty = sum((row.qty for row in candidates), ZERO)
        new_allocations, _ = allocate_fifo(
            candidates, remaining_before, step_size=step_size, role="EXIT")
        inserted_keys: list[str] = []
        now = _utc_now_fallback()
        with self.db.transaction() as conn:
            for allocation in new_allocations:
                cursor = conn.execute(
                    """INSERT OR IGNORE INTO lifecycle_fill_allocations(
                        lifecycle_id,fill_key,allocated_qty,role,allocated_at
                    ) VALUES(?,?,?,?,?)""",
                    (lifecycle_id, allocation.fill_key, float(allocation.allocated_qty),
                     allocation.role, now),
                )
                if cursor.rowcount == 1:
                    inserted_keys.append(allocation.fill_key)

            allocation_rows = conn.execute(
                """SELECT a.allocated_qty,a.fill_key,f.qty,f.price,f.realized_pnl,
                          f.commission,f.commission_asset,f.trade_time_ms
                   FROM lifecycle_fill_allocations a
                   JOIN execution_fills f ON f.fill_key=a.fill_key
                   WHERE a.lifecycle_id=? AND a.role='EXIT'
                   ORDER BY f.trade_time_ms,a.allocation_id""",
                (lifecycle_id,),
            ).fetchall()
            allocated_qty = sum(
                (_decimal_from_db(row["allocated_qty"]) or ZERO for row in allocation_rows), ZERO)
            remaining_qty = max(ZERO, entry_qty - allocated_qty)
            realized_complete = True
            gross_realized = ZERO
            fee_complete = True
            exit_fee = ZERO
            fee_assets: set[str] = set()
            actual_closed_at_ms: int | None = None
            for row in allocation_rows:
                allocated = _decimal_from_db(row["allocated_qty"]) or ZERO
                fill_qty = _decimal_from_db(row["qty"]) or ZERO
                if fill_qty <= ZERO:
                    continue
                ratio = allocated / fill_qty
                realized = _decimal_from_db(row["realized_pnl"])
                if realized is None:
                    realized_complete = False
                else:
                    gross_realized += realized * ratio
                commission = _decimal_from_db(row["commission"])
                asset = str(row["commission_asset"] or "").upper()
                if commission is None or not asset:
                    fee_complete = False
                else:
                    exit_fee += commission * ratio
                    fee_assets.add(asset)
                fill_time = int(row["trade_time_ms"] or 0)
                actual_closed_at_ms = max(actual_closed_at_ms or 0, fill_time)
            if not realized_complete:
                gross_value: Decimal | None = None
            else:
                gross_value = gross_realized
            if not fee_complete:
                exit_fee_value: Decimal | None = None
            else:
                exit_fee_value = exit_fee
            funding = _decimal_from_db(lifecycle["funding_cashflow"])
            entry_fee = _decimal_from_db(lifecycle["entry_fee"])
            fee_assets_ok = bool(fee_assets) and fee_assets <= {"USDT"}
            cost_complete = (entry_fee is not None and exit_fee_value is not None
                             and funding is not None and fee_assets_ok)
            cost_status = "COMPLETE" if cost_complete else (
                "INCOMPLETE" if allocation_rows else str(lifecycle["cost_status"] or "UNKNOWN"))
            net = compute_net_pnl(
                gross_value, entry_fee, exit_fee_value if fee_assets_ok else None, funding)
            step = step_size if step_size and step_size > ZERO else None
            flat_by_fills = quantity_is_flat(remaining_qty, step)
            flat_by_snapshot = position_qty is not None and quantity_is_flat(
                max(ZERO, Decimal(position_qty)), step)
            over_allocation = candidate_qty > remaining_before and not quantity_is_flat(
                candidate_qty - remaining_before, step)
            flat_evidence = flat_by_fills and flat_by_snapshot and not over_allocation
            status = "RECONCILING" if over_allocation else ("CLOSED" if flat_evidence else (
                "OPEN" if allocated_qty > ZERO else str(lifecycle["status"] or "OPEN"))
            )
            conn.execute(
                """UPDATE position_lifecycles SET status=?,closed_at_ms=?,entry_qty=?,
                    exit_qty=?,remaining_qty=?,exit_notional=?,gross_realized=?,
                    exit_fee=?,cost_status=?,recorded_at=? WHERE lifecycle_id=?""",
                (
                    status, actual_closed_at_ms if flat_evidence else None,
                    float(entry_qty), float(allocated_qty), float(remaining_qty),
                    float(sum((_decimal_from_db(row["allocated_qty"]) or ZERO)
                              * (_decimal_from_db(row["price"]) or ZERO)
                              for row in allocation_rows)),
                    float(gross_value) if gross_value is not None else None,
                    float(exit_fee_value) if exit_fee_value is not None else None,
                    cost_status, now, lifecycle_id,
                ),
            )
        # 返回实际已挂接到生命周期的成交键；重复重放时也能让调用方看到完整的
        # 已归属集合，而不是把本次窗口内未分配的开仓成交混入结果。
        allocated_keys = set(existing_keys) | set(inserted_keys)
        selected_keys = tuple(
            row.fill_key for row in sort_fills(rows) if row.fill_key in allocated_keys)
        return LifecycleReconciliation(
            lifecycle_id=lifecycle_id, inserted_allocations=len(inserted_keys),
            allocated_qty=allocated_qty, remaining_qty=remaining_qty, status=status,
            flat_evidence=flat_evidence, actual_closed_at_ms=actual_closed_at_ms,
            gross_realized=gross_value, exit_fee=exit_fee_value,
            cost_status=cost_status, attribution_status=str(lifecycle["attribution_status"] or "UNKNOWN"),
            net_pnl=net, allocation_fill_keys=selected_keys,
        )


def _utc_now_fallback() -> str:
    """避免在纯模块顶层依赖审计对象；运行时生成 UTC ISO 时间。"""
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")
