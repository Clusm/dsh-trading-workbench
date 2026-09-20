"""U04 执行命令分账、UNKNOWN 有界升级与保护指令规划。

本模块承载 U04 的执行层判定策略。它不发起网络请求、不构造 Broker、不读取
``.env``/``data``，也不把任何结论写回共享源文件：查询与提交仍由
``exchange.order_outbox`` / ``exchange.binance_futures`` 负责，本模块只提供
可独立复核的判定、规划与守护函数，供实盘路径与离线验收共用同一套规则。

固化的六条不变量：

* **三分量守恒** ``accepted = filled + working + terminal``，全程 ``Decimal``；
  终态（CANCELED/EXPIRED）后迟到的 fill 只修订事实，不复活订单、不回退状态
  （DC04 / R08）。撤单确认前 ``working`` 绝不提前计入 ``terminal``。
* **普通命令与 Algo 命令分账**：同一张 ``order_outbox`` 表按 ``command_kind``
  显式区分，查单入口与覆盖报告都按种类分开；Algo 命令的"已覆盖"结论绝不能
  由普通订单记录代表（R07）。
* **未知提交只查单、不重发**：永远使用原 ``clientOrderId``/``clientAlgoId``
  查询，禁止生成新 ID 重发；查询预算/时效耗尽转 ``MANUAL_RECOVERY`` 并有界
  升级，同时继续阻断该标的的新开仓（R10）。
* **保护随真实仓位更新**：先挂新、后撤旧；仓位未确认归零前不得撤销最后一道
  保护，也不得把 TP 与 SL 数量相加冒充双倍覆盖。
* **请求成功 != 成交**：只有交易所给出成交数量与可用成交价才算成交事实。
* **健康/传输类故障不得触发全平**，也不得改动 pause/emergency 语义与风险档。

U04 需要的"命令种类/十进制数量"列由 :func:`ensure_execution_schema` 以幂等
``ALTER TABLE ... ADD COLUMN`` 补齐，**不新增迁移版本号**：``schema_version`` 参与
``release_evidence`` 的 GO 判定（数据库迁移版本必须与发布清单一致），而这里只新增
向后兼容的可空列，不应因此改变发布身份；旧行照常可读，未补列的库也能按端点回推
命令种类。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from typing import Any

from core.contracts import (
    NumericInputError,
    OrderQuantity,
    QuantityError,
    apply_fill,
    confirm_cancel,
    decimal_from_value,
    decimal_text,
    request_cancel,
)
from core.contracts.k04_management import REASON_LAST_PROTECTION

STANDARD_ENDPOINT = "/fapi/v1/order"
ALGO_ENDPOINT = "/fapi/v1/algoOrder"

ZERO = Decimal("0")

#: 有界 UNKNOWN：单条命令的查询预算与转人工时效（R10）。
UNKNOWN_QUERY_BUDGET = 5
UNKNOWN_MANUAL_AFTER_MS = 30 * 60 * 1000

#: 撤单确认前的 working 不得计入 terminal；确认后才允许。
REASON_REVISION_ONLY = "TERMINAL_REVISION_ONLY"
REASON_DUPLICATE_FILL = "DUPLICATE_FILL_IGNORED"
REASON_FILL_UNCONFIRMED = "FILL_UNCONFIRMED"
REASON_QUERY_BEFORE_CANCEL = "WORKING_NOT_IN_TERMINAL_BEFORE_CANCEL_CONFIRM"
REASON_NO_REPOST = "NO_REPOST_BY_ORIGINAL_CLIENT_ID"
REASON_BOUNDED_BUDGET = "QUERY_BUDGET_EXHAUSTED"
REASON_BOUNDED_AGE = "UNKNOWN_AGE_EXCEEDED"
REASON_FLAT_NOT_CONFIRMED = "FLAT_NOT_CONFIRMED"
REASON_ALGO_NOT_STANDARD = "ALGO_NOT_COVERED_BY_STANDARD_OUTBOX"
REASON_HEALTH_NO_FLAT_CLOSE = "HEALTH_FAULT_MUST_NOT_TRIGGER_FLAT_CLOSE"
#: fail-closed：故障类未达到"保护确证失败"门槛时，全平一律被拒绝（含未登记的健康类）。
REASON_FLAT_CLOSE_NOT_CONFIRMED = "FLAT_CLOSE_REQUIRES_CONFIRMED_PROTECTION_FAILURE"
#: 终态（CANCELED/EXPIRED）回报里带有已确认成交：成交事实不得被主分类吞掉。
REASON_PARTIAL_FILL_PRESENT = "TERMINAL_PARTIAL_FILL_PRESENT"
#: 撤旧回调未真正执行（如缺少交易所 algoId）：不得记成"已撤销"。
REASON_CANCEL_NOT_EXECUTED = "CANCEL_CALLBACK_NOT_EXECUTED"


class ExecutionCommandError(RuntimeError):
    """执行命令账本/保护规划违反 U04 不变量。"""


class CommandKindConflict(ExecutionCommandError):
    """同一命令的显式种类与端点/载荷互相矛盾。"""


class CrossKindCoverageClaim(ExecutionCommandError):
    """用普通 Outbox 记录代表 Algo 命令覆盖（或反向）的非法声明。"""


class ProtectionSafetyError(ExecutionCommandError):
    """保护指令规划会撤销最后一道保护或改动 pause/emergency 语义。"""


class QuantizationInputError(ValueError):
    """数量入参不是可精确表示的十进制值（拒绝浮点近似）。"""


class QuantityStateConflict(ValueError):
    """交易所回报的数量/状态自相矛盾，调用方必须拒绝而不是猜测。"""


# --------------------------------------------------------------------------- #
# 1. 命令种类：普通订单与 Algo 保护命令分账
# --------------------------------------------------------------------------- #
class CommandKind(str, Enum):
    """执行命令种类；持久化与查单都必须显式带种类。"""

    STANDARD = "STANDARD"
    ALGO = "ALGO"


#: 每种命令唯一合法的查单入口；Algo 命令绝不允许走普通订单查询。
QUERY_API: dict[CommandKind, str] = {
    CommandKind.STANDARD: "query_order",
    CommandKind.ALGO: "query_algo_order",
}
_ENDPOINT_KIND: dict[str, CommandKind] = {
    STANDARD_ENDPOINT: CommandKind.STANDARD,
    ALGO_ENDPOINT: CommandKind.ALGO,
}


def classify_endpoint(endpoint: Any) -> CommandKind:
    """按端点判定命令种类；未知端点直接拒绝，不做"当成普通订单"的兜底。"""
    text = str(endpoint or "").strip()
    if text not in _ENDPOINT_KIND:
        raise ExecutionCommandError(f"未知订单端点，无法判定命令种类: {text!r}")
    return _ENDPOINT_KIND[text]


def _row_get(row: Any, name: str, default: Any = None) -> Any:
    """同时兼容 sqlite3.Row 与 Mapping；缺列返回默认值。"""
    if row is None:
        return default
    try:
        value = row[name]
    except (IndexError, KeyError, TypeError):
        if isinstance(row, Mapping):
            return row.get(name, default)
        return default
    return default if value is None else value


def classify_row(row: Any) -> CommandKind:
    """判定一条 Outbox 记录的命令种类。

    显式 ``command_kind`` 优先；与端点矛盾时拒绝（fail-closed），
    不允许把 Algo 保护命令降级成普通订单来"解释"。
    """
    explicit = str(_row_get(row, "command_kind", "") or "").strip().upper()
    endpoint = _row_get(row, "endpoint", None)
    by_endpoint = classify_endpoint(endpoint) if endpoint else None
    if explicit:
        try:
            kind = CommandKind(explicit)
        except ValueError as exc:
            raise ExecutionCommandError(f"未知 command_kind: {explicit!r}") from exc
        if by_endpoint is not None and by_endpoint is not kind:
            raise CommandKindConflict(
                f"command_kind={kind.value} 与端点 {endpoint!r} 矛盾")
        return kind
    if by_endpoint is not None:
        return by_endpoint
    # 端点缺失（历史行/迁移前）时只按算法单专有字段兜底，绝不默认 STANDARD。
    if _row_get(row, "client_algo_id", None) or _row_get(row, "algo_id", None):
        return CommandKind.ALGO
    raise ExecutionCommandError("记录缺少 command_kind 与 endpoint，无法判定命令种类")


def query_api_for(kind: CommandKind) -> str:
    """返回该种类唯一合法的查单入口名。"""
    return QUERY_API[CommandKind(kind)]


#: 执行命令分账需要的向前兼容列：全部可空/带默认值，旧行读取不受影响。
#: 这些列由 ``ensure_execution_schema`` 幂等补齐，而不是抬升 schema 版本号 ——
#: ``schema_version`` 参与 release_evidence 的 GO 判定（数据库迁移版本必须与发布
#: 清单一致），U04 只新增向后兼容的列，不应因此改变发布身份。
EXECUTION_SCHEMA_COLUMNS: dict[str, dict[str, str]] = {
    "order_outbox": {
        "command_kind": "TEXT",
        "accepted_qty_text": "TEXT",
        "cumulative_filled_qty_text": "TEXT",
        "working_leaves_qty_text": "TEXT",
        "terminal_unfilled_qty_text": "TEXT",
        "applied_fill_ids_json": "TEXT DEFAULT '[]'",
        "quantity_revision": "INTEGER DEFAULT 0",
    },
    "protection_order_mirror": {
        "quantity_text": "TEXT",
        "actual_qty_text": "TEXT",
        "trigger_price_text": "TEXT",
    },
}


def ensure_execution_schema(conn: Any) -> tuple[str, ...]:
    """幂等补齐执行命令分账所需的列，返回本次新增的 ``表.列``。

    * 只做 ``ALTER TABLE ... ADD COLUMN``，不动任何既有列/行，旧库可直接读；
    * 表不存在时跳过（运行期表由各自迁移创建），不在这里建表；
    * 与其它进程并发启动时，重复 ADD COLUMN 会抛 ``duplicate column name``，
      此时复查列是否已存在并视为成功，不掩盖其它错误。

    返回值的语义是"**本次调用真正执行的 DDL**"：并发场景下由别的进程抢先加好的
    列不计入，避免调用方把它当成自己新增的列去做后续动作。``commit()`` 也只在
    本次确实发过 DDL 时执行，不会替调用方提交它自己尚未完成的事务。
    """
    added: list[str] = []
    ddl_issued = False
    for table, columns in EXECUTION_SCHEMA_COLUMNS.items():
        existing = {
            row["name"] if not isinstance(row, Mapping) else row.get("name")
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if not existing:
            continue
        for name, declaration in columns.items():
            if name in existing:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {declaration}")
            except Exception as exc:  # noqa: BLE001 - 并发添加需复查而不是直接失败
                now = {
                    row["name"] if not isinstance(row, Mapping) else row.get("name")
                    for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if name not in now:
                    raise ExecutionCommandError(
                        f"{table}.{name} 补齐失败: {type(exc).__name__} {exc}") from exc
                # 并发进程已加好该列：本次没有执行 DDL，不计入返回值。
                continue
            ddl_issued = True
            added.append(f"{table}.{name}")
    if ddl_issued:
        conn.commit()
    return tuple(added)


# --------------------------------------------------------------------------- #
# 2. Decimal 数量：拒绝浮点近似
# --------------------------------------------------------------------------- #
def parse_qty(value: Any, *, field: str = "qty", allow_negative: bool = False) -> Decimal:
    """把文本/整数/Decimal 转成 Decimal；浮点与布尔一律拒绝。

    执行数量在边界必须显式使用十进制文本：``0.01`` 与 ``0.0047`` 这类
    数量用 float 直传会产生二进制尾差，勾稽结果不再可复核。
    """
    if value is None or isinstance(value, bool):
        raise QuantizationInputError(f"{field} 缺失或布尔值不是数量: {value!r}")
    if isinstance(value, float):
        raise QuantizationInputError(
            f"{field} 禁止浮点直传；请用十进制文本或 decimal_from_float 显式转换")
    try:
        result = decimal_from_value(value)
    except NumericInputError as exc:
        raise QuantizationInputError(f"{field} 不是有效十进制数: {value!r}") from exc
    if not result.is_finite():
        raise QuantizationInputError(f"{field} 不是有限十进制数: {value!r}")
    if not allow_negative and result < 0:
        raise QuantizationInputError(f"{field} 不能为负: {value!r}")
    return result


def qty_tolerance(target: Decimal) -> Decimal:
    """数量比较容差：与保护镜像保持同一口径（绝对 + 相对）。

    **已知放宽（U04 r2 登记）**：被替换的旧实现用步长口径
    ``max(_min_qty(symbol), 1e-8)``，本实现是 ``1e-6`` 相对 + ``1e-9`` 绝对。
    在 2.0 的仓位上，保护数量差 ``2e-6`` 仍会被判为 ``MATCHES_REAL_POSITION``。
    离线无 symbol filters，无法判定该差值是否会被交易所 step-size 撞上；叠加
    交易所 tick/step 的真实约束需要联调数据，故此处不改判据、只登记。
    """
    return max(Decimal("1e-9"), abs(target) * Decimal("0.000001"))


def quantities_equal(left: Decimal, right: Decimal) -> bool:
    """按交易所精度容差比较两个数量，避免尾数漂移造成误判。"""
    return abs(left - right) <= qty_tolerance(max(abs(left), abs(right)))


# --------------------------------------------------------------------------- #
# 3. 交易所回报 -> 三分量；终态迟到成交只修订事实
# --------------------------------------------------------------------------- #
_WORKING_LABELS = frozenset({"NEW", "PENDING_NEW", "PARTIALLY_FILLED", "PARTIALLYFILLED"})
_TERMINAL_UNFILLED_LABELS = {
    "CANCELED": "CANCELED",
    "CANCELLED": "CANCELED",
    "EXPIRED": "EXPIRED",
    "EXPIRED_IN_MATCH": "EXPIRED",
}
_REJECTED_LABELS = frozenset({"REJECTED"})
_FILLED_LABELS = frozenset({"FILLED", "FINISHED"})
_TRIGGERED_LABELS = frozenset({"TRIGGERED"})


@dataclass(frozen=True)
class ExchangeOrderReading:
    """一条交易所订单回报解析结果；数量与状态都由回报驱动，不猜测。"""

    quantity: OrderQuantity
    status_label: str
    reason_codes: tuple[str, ...] = ()
    requested_qty: Decimal | None = None


def _accepted_and_filled_fields(
    payload: Mapping[str, Any], kind: CommandKind,
) -> tuple[Any, Any, str]:
    """按命令种类取原始/成交数量字段；缺失与畸形分开暴露。"""
    if kind is CommandKind.ALGO:
        accepted = payload.get("quantity", payload.get("origQty"))
        filled = payload.get("actualQty", payload.get("executedQty"))
        return accepted, filled, "quantity/actualQty"
    accepted = payload.get("origQty", payload.get("quantity"))
    filled = payload.get("executedQty", payload.get("actualQty"))
    return accepted, filled, "origQty/executedQty"


def read_exchange_order(
    payload: Mapping[str, Any],
    *,
    kind: CommandKind | None = None,
    order_id: str | None = None,
    applied_fill_ids: Sequence[str] = (),
) -> ExchangeOrderReading:
    """把交易所订单回报解析成 K04 三分量 ``OrderQuantity``。

    关键约束：

    * ``REJECTED`` 表示交易所未接受，``accepted`` 必须为 0；若同时带成交数量，
      说明回报自相矛盾，直接拒绝（不得把失败当"无成交"悄悄吞掉）。
    * 状态标签与成交数量矛盾时（如 NEW 却已有成交）以**成交事实**为准升级状态，
      并在 ``reason_codes`` 中记录来源，绝不把成交说成"只是受理"。
    * 终态（CANCELED/EXPIRED）时 working 必须为 0，未成交部分计入 terminal。

    **已知口径不一致（U04 r2 登记）**：非文本 ``origQty`` 抛
    ``QuantizationInputError``，而 ``executedQty`` 不可解析时抛
    ``QuantityStateConflict``（U04/A_r1 P3-3）。:func:`read_submission_result`
    两者都捕获，影响只限直接调用者；统一异常口径会改变已发布的 K04 边界语义，
    故不改、只登记。
    """
    data = dict(payload or {})
    resolved_kind = CommandKind(kind) if kind is not None else (
        CommandKind.ALGO if ("algoId" in data or "algoStatus" in data)
        else CommandKind.STANDARD
    )
    label = str(
        data.get("status") if resolved_kind is CommandKind.STANDARD
        else data.get("algoStatus", data.get("status"))
        or ""
    ).strip().upper()
    raw_accepted, raw_filled, fields = _accepted_and_filled_fields(data, resolved_kind)
    reasons: list[str] = []
    requested: Decimal | None = None
    if raw_accepted is not None and str(raw_accepted).strip() != "":
        requested = parse_qty(raw_accepted, field="accepted_qty")
    try:
        filled = parse_qty(raw_filled, field="filled_qty") if (
            raw_filled is not None and str(raw_filled).strip() != "") else ZERO
    except QuantizationInputError:
        raise QuantityStateConflict(f"回报成交数量不可解析 ({fields}): {raw_filled!r}")

    if label in _REJECTED_LABELS:
        if filled != 0:
            raise QuantityStateConflict("REJECTED 回报携带成交数量，回报自相矛盾")
        quantity = OrderQuantity.checked(
            order_id=str(order_id or data.get("orderId") or data.get("algoId") or ""),
            accepted_qty=ZERO, cumulative_filled_qty=ZERO,
            working_leaves_qty=ZERO, terminal_unfilled_qty=ZERO,
            status="REJECTED", requested_qty=requested,
        )
        return ExchangeOrderReading(quantity, label, ("REJECTED_NOT_ACCEPTED",), requested)

    accepted = requested if requested is not None else ZERO
    if filled > accepted:
        raise QuantityStateConflict(
            f"回报成交 {decimal_text(filled)} 超过受理量 {decimal_text(accepted)}")

    if label in _TERMINAL_UNFILLED_LABELS:
        status = _TERMINAL_UNFILLED_LABELS[label]
        working = ZERO
        terminal = accepted - filled
    elif label in _FILLED_LABELS:
        if accepted != 0 and filled != accepted:
            raise QuantityStateConflict(
                "FILLED 回报的成交数量与受理量不一致，不得按已全成交处理")
        status = "FILLED"
        working = ZERO
        terminal = ZERO
        if accepted == 0:
            # Hedge 模式 closePosition 型 Algo 单不带数量；全平事实由仓位
            # 归零确认，不能凭本行推出"成交了某个数量"。
            reasons.append("ZERO_QUANTITY_CLOSE_POSITION_ORDER")
    else:
        if label and label not in _WORKING_LABELS and label not in _TRIGGERED_LABELS:
            reasons.append(f"STATUS_LABEL_UNRECOGNIZED:{label}")
        if filled == 0:
            status = "NEW"
        elif accepted != 0 and filled < accepted:
            status = "PARTIALLY_FILLED"
        elif accepted != 0:
            status = "FILLED"
        else:
            raise QuantityStateConflict("回报有成交数量但无受理量，无法守恒")
        working = accepted - filled if status != "FILLED" else ZERO
        terminal = ZERO
        if label in _WORKING_LABELS and status != label and label != "PENDING_NEW":
            reasons.append("STATUS_PROMOTED_FROM_FILL_FACT")

    quantity = OrderQuantity.checked(
        order_id=str(order_id or data.get("orderId") or data.get("algoId") or ""),
        accepted_qty=accepted, cumulative_filled_qty=filled,
        working_leaves_qty=working, terminal_unfilled_qty=terminal,
        status=status, requested_qty=requested,
    )
    if applied_fill_ids:
        reasons.append("APPLIED_FILL_IDS_SUPPLIED")
    return ExchangeOrderReading(quantity, label, tuple(reasons), requested)


def quantities_from_order_payload(
    payload: Mapping[str, Any], **kwargs: Any,
) -> OrderQuantity:
    """``read_exchange_order`` 的三分量便捷入口。"""
    return read_exchange_order(payload, **kwargs).quantity


@dataclass(frozen=True)
class FillRevision:
    """迟到成交修订结果；``revision_only`` 为真表示订单终态未被改写。"""

    quantity: OrderQuantity
    fill_id: str
    fill_qty: Decimal
    revision_only: bool
    reason_codes: tuple[str, ...] = ()


def apply_late_fill(
    quantity: OrderQuantity,
    *,
    fill_qty: Any,
    fill_id: str,
    applied_fill_ids: Sequence[str] = (),
) -> FillRevision:
    """应用一笔成交事实（含 CANCELED 后迟到 fill），只修订不改状态。

    * ``fill_id`` 必填：匿名成交无法幂等，重复 ID 抛 ``DuplicateFillError``。
    * 终态订单只修订 ``cumulative_filled_qty`` 与 ``terminal_unfilled_qty``，
      保持原状态，绝不"复活"订单；守恒仍必须成立。
    """
    if not isinstance(fill_id, str) or not fill_id.strip():
        raise ExecutionCommandError("fill_id 必须是显式非空字符串")
    amount = parse_qty(fill_qty, field="fill_qty")
    if amount <= 0:
        raise QuantityStateConflict("成交数量必须为正")
    terminal_status = quantity.status not in ("NEW", "PARTIALLY_FILLED")
    updated = apply_fill(
        quantity, amount, fill_id=fill_id, applied_fill_ids=applied_fill_ids)
    violations = updated.violations()
    if violations:
        raise QuantityError("; ".join(violations))
    reasons = [REASON_REVISION_ONLY] if terminal_status else []
    if terminal_status and updated.status != quantity.status:
        raise ExecutionCommandError("终态订单的迟到成交不得改变订单状态")
    return FillRevision(
        quantity=updated, fill_id=fill_id, fill_qty=amount,
        revision_only=terminal_status, reason_codes=tuple(reasons),
    )


def cancel_then_late_fill_probe(
    quantity: OrderQuantity, *, late_fill_qty: Any, fill_id: str,
) -> tuple[OrderQuantity, OrderQuantity, FillRevision]:
    """撤单确认门禁探针：返回 (请求撤单后, 确认撤单后, 迟到成交修订后)。

    用于证明两件事：确认撤单前 ``terminal`` 仍为 0；确认之后迟到的 fill 仍能
    修订累计成交（终态可修正）。任一步守恒被破坏都会抛 ``QuantityError``。
    """
    working = request_cancel(quantity)
    if working.terminal_unfilled_qty != ZERO:
        raise ExecutionCommandError(REASON_QUERY_BEFORE_CANCEL)
    confirmed = confirm_cancel(working)
    revision = apply_late_fill(
        confirmed, fill_qty=late_fill_qty, fill_id=fill_id,
        applied_fill_ids=(),
    )
    return working, confirmed, revision


# --------------------------------------------------------------------------- #
# 4. UNKNOWN：只查单、不重发，预算/时效耗尽有界升级
# --------------------------------------------------------------------------- #
class UnknownDecision(str, Enum):
    QUERY_DUE = "QUERY_DUE"
    BACKOFF = "BACKOFF"
    MANUAL_RECOVERY = "MANUAL_RECOVERY"
    NOT_UNKNOWN = "NOT_UNKNOWN"


@dataclass(frozen=True)
class UnknownPlan:
    """单条 UNKNOWN 命令的下一步动作；``repost_allowed`` 恒为 False。"""

    command_id: str
    kind: CommandKind
    decision: UnknownDecision
    query_api: str
    attempt_no: int
    budget: int
    blocks_new_open: bool
    alert_required: bool
    escalation: str
    reason_codes: tuple[str, ...] = ()
    # 两条硬不变量：任何分支都不允许重发或换 client ID。
    repost_allowed: bool = False
    new_client_id_allowed: bool = False
    later_requery_allowed: bool = True


def parse_timestamp_ms(value: Any) -> int | None:
    """把 ISO 文本或整数毫秒统一解析为 epoch 毫秒；无法解析返回 None。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(Decimal(text))
    except (ArithmeticError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return int(parsed.timestamp() * 1000)


def _plan_budget_count(value: Any) -> int:
    """把查询计数列解析成非负整数；脏数据按**预算已耗尽**处理（fail-closed）。

    旧实现对 ``'abc'``/``'3.0'`` 直接抛裸 ``ValueError``，会让整条 UNKNOWN 收口
    规划中断——而"解析不了计数"最不该走的方向就是"当作 0 次、无限重试"。因此这里
    返回 :data:`UNKNOWN_QUERY_BUDGET`，把脏行推进 ``MANUAL_RECOVERY`` 并告警。
    """
    if value in (None, ""):
        return 0
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        try:
            parsed = int(Decimal(str(value).strip()))
        except (ArithmeticError, TypeError, ValueError):
            return UNKNOWN_QUERY_BUDGET
    return parsed if parsed >= 0 else UNKNOWN_QUERY_BUDGET


def plan_unknown_reconcile(
    row: Any,
    *,
    now_ms: int,
    budget: int = UNKNOWN_QUERY_BUDGET,
    manual_after_ms: int = UNKNOWN_MANUAL_AFTER_MS,
) -> UnknownPlan:
    """为一条 Outbox 记录规划 UNKNOWN 收口动作（只查单，绝不重发）。

    升级阶梯固定为：``QUERY_DUE`` -> ``BACKOFF`` -> ``MANUAL_RECOVERY``。
    预算或时效任一耗尽即转人工并要求告警，但记录保留、后续仍可有界复查，
    可见的未知敞口持续阻断新开仓（R10）。
    """
    command_id = str(_row_get(row, "client_order_id", _row_get(row, "command_id", "")) or "")
    kind = classify_row(row)
    status = str(_row_get(row, "status", "") or "").upper()
    attempts = _plan_budget_count(_row_get(row, "query_attempt_count", 0))
    unknown_since = parse_timestamp_ms(_row_get(row, "unknown_since", None))
    next_retry = parse_timestamp_ms(_row_get(row, "next_retry_at", None))
    api = query_api_for(kind)
    if status != "UNKNOWN":
        return UnknownPlan(
            command_id=command_id, kind=kind, decision=UnknownDecision.NOT_UNKNOWN,
            query_api=api, attempt_no=attempts, budget=budget,
            blocks_new_open=False, alert_required=False, escalation="",
            reason_codes=("NOT_UNKNOWN",))
    if attempts >= budget or (
        unknown_since is not None and now_ms - unknown_since >= manual_after_ms
    ):
        reasons = []
        if attempts >= budget:
            reasons.append(REASON_BOUNDED_BUDGET)
        if unknown_since is not None and now_ms - unknown_since >= manual_after_ms:
            reasons.append(REASON_BOUNDED_AGE)
        return UnknownPlan(
            command_id=command_id, kind=kind,
            decision=UnknownDecision.MANUAL_RECOVERY, query_api=api,
            attempt_no=attempts, budget=budget, blocks_new_open=True,
            alert_required=True, escalation="MANUAL_RECOVERY_PLUS_ALERT",
            reason_codes=tuple(reasons + [REASON_NO_REPOST]),
        )
    if next_retry is not None and now_ms < next_retry:
        return UnknownPlan(
            command_id=command_id, kind=kind, decision=UnknownDecision.BACKOFF,
            query_api=api, attempt_no=attempts, budget=budget,
            blocks_new_open=True, alert_required=False,
            escalation="BOUNDED_REQUERY", reason_codes=("BACKOFF_ACTIVE",))
    return UnknownPlan(
        command_id=command_id, kind=kind, decision=UnknownDecision.QUERY_DUE,
        query_api=api, attempt_no=attempts, budget=budget, blocks_new_open=True,
        alert_required=False, escalation="BOUNDED_REQUERY",
        reason_codes=(REASON_NO_REPOST,),
    )


@dataclass(frozen=True)
class UnknownReconcileReport:
    """一次 UNKNOWN 收敛结果；``submit_calls`` 必须恒为 0。"""

    queried: int
    resolved: int
    pending: int
    manual_recovery: tuple[str, ...]
    submit_calls: int
    query_calls_by_api: dict[str, int]
    reason_codes: tuple[str, ...] = ()


def execute_unknown_reconcile(
    rows: Iterable[Any],
    *,
    broker: Any,
    outbox: Any,
    now_ms: int,
    budget: int = UNKNOWN_QUERY_BUDGET,
    manual_after_ms: int = UNKNOWN_MANUAL_AFTER_MS,
) -> UnknownReconcileReport:
    """按种类分派查单：普通命令走 ``query_order``，Algo 命令走 ``query_algo_order``。

    本函数只调用查询入口与 ``outbox.acknowledge``；它没有任何提交路径，因此
    "响应丢失后重发"在此结构上不可能发生。查询失败的行不改变状态，交由
    ``order_outbox`` 的退避与人工收口处理。
    """
    queried = 0
    resolved = 0
    pending = 0
    manual: list[str] = []
    calls: dict[str, int] = {}
    for row in rows:
        plan = plan_unknown_reconcile(
            row, now_ms=now_ms, budget=budget, manual_after_ms=manual_after_ms)
        if plan.decision is UnknownDecision.MANUAL_RECOVERY:
            manual.append(plan.command_id)
            continue
        if plan.decision is not UnknownDecision.QUERY_DUE:
            pending += 1
            continue
        api = plan.query_api
        caller = getattr(broker, api, None)
        if caller is None:
            raise ExecutionCommandError(f"Broker 缺少 {api}，无法按种类查单")
        calls[api] = calls.get(api, 0) + 1
        queried += 1
        symbol = str(_row_get(row, "symbol", "") or "")
        try:
            response = caller(symbol, plan.command_id) if api == "query_order" \
                else caller(plan.command_id)
        except Exception:
            pending += 1
            continue
        found = isinstance(response, dict) and bool(
            response.get("algoId") if plan.kind is CommandKind.ALGO
            else response.get("orderId"))
        if found:
            outbox.acknowledge(plan.command_id, response)
            resolved += 1
        else:
            pending += 1
    return UnknownReconcileReport(
        queried=queried, resolved=resolved, pending=pending,
        manual_recovery=tuple(manual), submit_calls=0, query_calls_by_api=calls,
        reason_codes=(REASON_NO_REPOST,),
    )


# --------------------------------------------------------------------------- #
# 5. 分种类覆盖报告：Algo 不得假称被普通 Outbox 覆盖（R07）
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class KindCoverage:
    kind: CommandKind
    total: int
    unknown: int
    active: int
    terminal: int
    oldest_unknown_age_ms: int | None
    #: 已 POST、结果未知（``SUBMITTING``）：保护"是否存在"并不确定，不构成覆盖。
    submitting: int = 0

    @property
    def covered(self) -> bool:
        """该种类下没有未决命令才算"已覆盖"；空集合不构成覆盖。

        ``SUBMITTING`` 属"已发出、结果未知"，与 ``UNKNOWN`` 同样是未决状态；
        词表外状态在 :func:`coverage_report` 里按未决计入 ``unknown``（fail-closed），
        因此不会出现"没见过的状态 = 已覆盖"。
        """
        return self.total > 0 and self.unknown == 0 and self.submitting == 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value, "total": self.total, "unknown": self.unknown,
            "active": self.active, "terminal": self.terminal, "submitting": self.submitting,
            "covered": self.covered,
            "oldest_unknown_age_ms": self.oldest_unknown_age_ms,
        }


@dataclass(frozen=True)
class CoverageReport:
    environment: str
    by_kind: dict[str, KindCoverage]

    def kind(self, kind: CommandKind) -> KindCoverage:
        resolved = CommandKind(kind)
        return self.by_kind.get(resolved.value, KindCoverage(
            kind=resolved, total=0, unknown=0, active=0, terminal=0,
            oldest_unknown_age_ms=None))

    @property
    def mixed_kinds(self) -> bool:
        """同一账本里同时存在普通与 Algo 命令：单一种类结论必须分开取。"""
        return sum(1 for item in self.by_kind.values() if item.total > 0) > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "mixed_kinds": self.mixed_kinds,
            "by_kind": {key: item.to_dict() for key, item in sorted(self.by_kind.items())},
        }


#: 覆盖统计的封闭状态词表：词表外的状态一律按"未决"处理（fail-closed）。
_COVERAGE_ACTIVE = frozenset({"ACKNOWLEDGED", "NEW", "PARTIALLY_FILLED"})
_COVERAGE_TERMINAL = frozenset({"FILLED", "CANCELED", "EXPIRED", "REJECTED"})


def coverage_report(
    rows: Iterable[Any], *, environment: str, now_ms: int | None = None,
) -> CoverageReport:
    """按命令种类分别汇总覆盖状态；两类命令从不合并成一个"已覆盖"结论。"""
    buckets: dict[CommandKind, dict[str, Any]] = {
        CommandKind.STANDARD: {"total": 0, "unknown": 0, "active": 0, "terminal": 0,
                               "oldest_unknown_age_ms": None, "submitting": 0},
        CommandKind.ALGO: {"total": 0, "unknown": 0, "active": 0, "terminal": 0,
                           "oldest_unknown_age_ms": None, "submitting": 0},
    }
    reference_ms = now_ms
    for row in rows:
        kind = classify_row(row)
        bucket = buckets[kind]
        bucket["total"] += 1
        status = str(_row_get(row, "status", "") or "").upper()
        if status in _COVERAGE_ACTIVE:
            bucket["active"] += 1
        elif status in _COVERAGE_TERMINAL:
            bucket["terminal"] += 1
        else:
            # UNKNOWN / SUBMITTING / 词表外状态：都按"未决"计入 unknown（fail-closed），
            # 不让"没见过的状态"或"POST 已发出但结果未知"冒充已覆盖。
            bucket["unknown"] += 1
            if status == "SUBMITTING":
                bucket["submitting"] += 1
            since = parse_timestamp_ms(_row_get(row, "unknown_since", None))
            if since is not None:
                moment = reference_ms if reference_ms is not None else since
                age = max(0, moment - since)
                current = bucket["oldest_unknown_age_ms"]
                bucket["oldest_unknown_age_ms"] = age if current is None else max(current, age)
    return CoverageReport(environment=str(environment), by_kind={
        kind.value: KindCoverage(kind=kind, **values)
        for kind, values in buckets.items()
    })


def assert_kind_coverage(
    rows: Iterable[Any], *, kind: CommandKind, claimed_covered: bool,
) -> KindCoverage:
    """断言"某类命令已覆盖"；用另一类命令的记录代表覆盖时直接拒绝。

    R07 的具体落地：声明 Algo 命令已覆盖时，必须有 Algo 记录且其中没有
    UNKNOWN；仅有普通订单记录（或反之）不能作为覆盖证据。
    """
    resolved = CommandKind(kind)
    report = coverage_report(rows, environment="")
    actual = report.kind(resolved)
    if not claimed_covered:
        return actual
    if actual.total == 0:
        raise CrossKindCoverageClaim(
            f"{resolved.value} 覆盖声明缺少 {resolved.value} 记录："
            f"不得由其它种类命令代表（{REASON_ALGO_NOT_STANDARD}）")
    if actual.unknown:
        raise CrossKindCoverageClaim(
            f"{resolved.value} 仍有 {actual.unknown} 条未知命令，不得声明已覆盖")
    return actual


# --------------------------------------------------------------------------- #
# 6. "请求成功" != "成交"
# --------------------------------------------------------------------------- #
class SubmissionOutcome(str, Enum):
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    TERMINAL_UNFILLED = "TERMINAL_UNFILLED"
    #: 终态（CANCELED/EXPIRED）但已确认有部分成交：成交事实必须显式可见。
    TERMINAL_PARTIAL = "TERMINAL_PARTIAL"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class SubmissionReading:
    outcome: SubmissionOutcome
    accepted_qty: Decimal
    filled_qty: Decimal
    fill_confirmed: bool
    reason_codes: tuple[str, ...] = ()


def read_submission_result(payload: Mapping[str, Any] | None) -> SubmissionReading:
    """区分"交易所受理了请求"与"确实成交"。

    HTTP 200 / 有 ``orderId`` 只代表受理；``fill_confirmed`` 需要终态或部分
    成交状态、正的成交数量与可用成交价三者同时成立。受理不得被当成交。
    """
    data = dict(payload or {})
    reasons: list[str] = []
    if not data:
        return SubmissionReading(
            SubmissionOutcome.UNKNOWN, ZERO, ZERO, False, ("EMPTY_RESPONSE",))
    try:
        quantity = read_exchange_order(data).quantity
    except (QuantityStateConflict, QuantizationInputError, QuantityError) as exc:
        return SubmissionReading(
            SubmissionOutcome.UNKNOWN, ZERO, ZERO, False,
            (f"RESPONSE_INCONSISTENT:{type(exc).__name__}",))
    avg_price = data.get("avgPrice", data.get("price"))
    price_ok = False
    if avg_price not in (None, ""):
        try:
            price_ok = parse_qty(avg_price, field="avg_price") > 0
        except QuantizationInputError:
            price_ok = False
    fill_confirmed = quantity.cumulative_filled_qty > 0 and price_ok
    if quantity.status == "REJECTED":
        outcome = SubmissionOutcome.REJECTED
    elif quantity.status in {"CANCELED", "EXPIRED"}:
        # 终态带成交（撤单前已有部分成交 / 过期前已有部分成交）与终态零成交是
        # **两件事**：只读 outcome 的消费者不能把一笔真实成交看成"什么都没有"。
        if fill_confirmed:
            outcome = SubmissionOutcome.TERMINAL_PARTIAL
            reasons.append(REASON_PARTIAL_FILL_PRESENT)
        else:
            outcome = SubmissionOutcome.TERMINAL_UNFILLED
    elif fill_confirmed and quantity.status == "FILLED":
        outcome = SubmissionOutcome.FILLED
    elif fill_confirmed and quantity.status == "PARTIALLY_FILLED":
        outcome = SubmissionOutcome.PARTIALLY_FILLED
    else:
        # 状态自称 FILLED/PARTIALLY_FILLED 但没有可确认的成交事实（成交量为 0
        # 或成交价不可用）时只能回到"已受理"，不得把请求成功当成交。
        outcome = SubmissionOutcome.ACKNOWLEDGED
        reasons.append(REASON_FILL_UNCONFIRMED)
    if outcome is SubmissionOutcome.ACKNOWLEDGED:
        reasons.append("ACKNOWLEDGED_NOT_FILLED")
    return SubmissionReading(
        outcome=outcome, accepted_qty=quantity.accepted_qty,
        filled_qty=quantity.cumulative_filled_qty,
        fill_confirmed=fill_confirmed, reason_codes=tuple(reasons))


# --------------------------------------------------------------------------- #
# 7. 保护指令：随真实仓位更新，不撤最后一道保护
# --------------------------------------------------------------------------- #
PROTECTION_TYPES = ("STOP_MARKET", "TAKE_PROFIT_MARKET")


def protection_purpose(order_type: str) -> str:
    text = str(order_type or "").upper()
    return "STOP" if text.startswith("STOP") else (
        "TAKE_PROFIT" if text.startswith("TAKE_PROFIT") else "EXIT")


@dataclass(frozen=True)
class RealPosition:
    """可证明的真实仓位快照；``revision`` 用于拒绝过期授权。"""

    symbol: str
    position_side: str
    qty: Decimal
    revision: int = 0

    @property
    def is_flat(self) -> bool:
        return self.qty == 0

    @property
    def close_side(self) -> str:
        return "SELL" if self.qty > 0 else "BUY"

    @property
    def abs_qty(self) -> Decimal:
        return abs(self.qty)


def real_position_from_snapshot(
    snapshot: Mapping[str, Any], *, revision: int = 0,
) -> RealPosition:
    """把交易所持仓行转换为 ``RealPosition``；数量必须可精确表示。"""
    raw = snapshot.get("positionAmt", snapshot.get("position_amt"))
    qty = parse_qty(raw if raw not in (None, "") else ZERO,
                    field="positionAmt", allow_negative=True)
    explicit = str(snapshot.get("positionSide") or "BOTH").upper()
    return RealPosition(
        symbol=str(snapshot.get("symbol") or "").upper(),
        position_side=explicit if explicit in {"LONG", "SHORT"} else "BOTH",
        qty=qty, revision=int(revision),
    )


@dataclass(frozen=True)
class ProtectionView:
    """一张已在交易所存在的保护单（来自保护镜像或私有流）。"""

    client_id: str
    order_type: str
    qty: Decimal | None
    trigger_price: Decimal
    reduce_only: bool = True
    close_position: bool = False
    algo_id: str | None = None

    @property
    def purpose(self) -> str:
        return protection_purpose(self.order_type)


def protection_view_from_algo(row: Mapping[str, Any]) -> ProtectionView:
    """从 Algo 查询/镜像行构造视图；数量与触发价一律走 Decimal 文本。

    读取优先级：**先文本列**（``quantity_text``/``actual_qty_text``/
    ``trigger_price_text``，由保护镜像的 U04 补丁写入），缺失或非法才回落到
    交易所 REAL/JSON 原值。这样镜像的"精确勾稽"列有真实消费者，而不是只写不读；
    若上游只给了 float（如 ``2.0``），:func:`parse_qty` 会明确拒绝而不是静默舍入。
    """
    def _decimal_or_none(value: Any, field: str) -> Decimal | None:
        if value in (None, ""):
            return None
        return parse_qty(value, field=field)

    def _prefer_text(text_key: str, fallback_keys: tuple[str, ...],
                     field: str) -> Decimal | None:
        for key in (text_key,) + fallback_keys:
            value = _row_get(row, key, None)
            if value in (None, ""):
                continue
            return _decimal_or_none(value, field)
        return None

    qty = _prefer_text("quantity_text", ("quantity",), "quantity")
    trigger = _prefer_text("trigger_price_text",
                           ("triggerPrice", "trigger_price"), "triggerPrice")
    return ProtectionView(
        # 同时兼容交易所/私有流（clientAlgoId）与保护镜像（client_algo_id）两种列名。
        client_id=str(row.get("clientAlgoId") or row.get("client_algo_id")
                      or row.get("client_id") or ""),
        order_type=str(row.get("orderType") or row.get("order_type") or "").upper(),
        qty=qty,
        trigger_price=trigger or ZERO,
        reduce_only=bool(row.get("reduceOnly", row.get("reduce_only", True))),
        close_position=bool(row.get("closePosition", row.get("close_position", False))),
        algo_id=str(row.get("algoId") or row.get("algo_id") or "") or None,
    )


@dataclass(frozen=True)
class ProtectionAction:
    action: str  # PLACE | KEEP | CANCEL
    order_type: str
    client_id: str | None
    trigger_price: Decimal | None
    qty: Decimal | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action, "order_type": self.order_type,
            "client_id": self.client_id,
            "trigger_price": decimal_text(self.trigger_price) if (
                self.trigger_price is not None) else None,
            "qty": decimal_text(self.qty) if self.qty is not None else None,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class ProtectionPlan:
    """一次保护刷新的有序动作计划；PLACE 必须排在 CANCEL 之前执行。"""

    symbol: str
    position_side: str
    target_qty: Decimal
    flat_confirmed: bool
    actions: tuple[ProtectionAction, ...]
    blocked_reason: str = ""
    reason_codes: tuple[str, ...] = ()

    @property
    def places(self) -> tuple[ProtectionAction, ...]:
        return tuple(a for a in self.actions if a.action == "PLACE")

    @property
    def cancels(self) -> tuple[ProtectionAction, ...]:
        return tuple(a for a in self.actions if a.action == "CANCEL")

    @property
    def keeps(self) -> tuple[ProtectionAction, ...]:
        return tuple(a for a in self.actions if a.action == "KEEP")

    @property
    def requires_place_first(self) -> bool:
        return bool(self.cancels)

    def ordered(self) -> tuple[ProtectionAction, ...]:
        """执行顺序：先 PLACE、再 KEEP、最后 CANCEL（撤旧前必须已有新保护）。"""
        rank = {"PLACE": 0, "KEEP": 1, "CANCEL": 2}
        return tuple(sorted(self.actions, key=lambda a: rank.get(a.action, 9)))

    @property
    def safe_to_execute(self) -> bool:
        return bool(self.actions) and not self.blocked_reason

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol, "position_side": self.position_side,
            "target_qty": decimal_text(self.target_qty),
            "flat_confirmed": self.flat_confirmed,
            "blocked_reason": self.blocked_reason,
            "reason_codes": list(self.reason_codes),
            "actions": [a.to_dict() for a in self.ordered()],
        }


def plan_protection_refresh(
    *,
    position: RealPosition,
    protections: Sequence[ProtectionView] = (),
    desired_sl: Decimal | Any | None = None,
    desired_tp: Decimal | Any | None = None,
    flat_confirmed: bool = False,
) -> ProtectionPlan:
    """按真实仓位规划保护刷新，保证"先挂新、后撤旧"且不撤最后一道保护。

    * 有仓：以 ``abs(position.qty)`` 为目标数量；同类型同方向同数量的有效保护
      保留（KEEP），价格可变（PLACE 新的 + CANCEL 旧的），数量或方向错配的
      一律视为过期（CANCEL）。
    * 无仓但**未确认归零**：不产生任何 CANCEL（``FLAT_NOT_CONFIRMED``）。
    * 无仓且已确认归零：清理遗留保护（CANCEL）。
    * 若一次刷新会把该仓位的保护清零（没有任何 KEEP/PLACE 留下），则拒绝全部
      CANCEL 并报 ``CANNOT_REMOVE_LAST_PROTECTION``。

    ``flat_confirmed`` 按**严格布尔**判定（``is True``，N3）：签名声明 ``bool``，
    传入 ``1``/``"false"``/``"true"``/``[0]`` 这类非布尔真值一律按"未确认"处理
    （fail-closed，与 :func:`can_cancel_protection` 同口径）——"已确认归零"会直接
    授权清理最后一道保护，这个判断不允许靠真值转换蒙对。
    """
    target = position.abs_qty
    reasons: list[str] = []
    actions: list[ProtectionAction] = []
    existing = [item for item in protections if item.order_type]

    if position.is_flat:
        if flat_confirmed is not True:
            return ProtectionPlan(
                symbol=position.symbol, position_side=position.position_side,
                target_qty=ZERO, flat_confirmed=False, actions=(),
                blocked_reason=REASON_FLAT_NOT_CONFIRMED,
                reason_codes=(REASON_FLAT_NOT_CONFIRMED,),
            )
        cancels = tuple(
            ProtectionAction("CANCEL", item.order_type, item.client_id,
                             item.trigger_price, item.qty, "FLAT_CLEANUP")
            for item in existing
        )
        return ProtectionPlan(
            symbol=position.symbol, position_side=position.position_side,
            target_qty=ZERO, flat_confirmed=True, actions=cancels,
            reason_codes=("FLAT_CONFIRMED_CLEANUP",),
        )

    def _desired(value: Any) -> Decimal | None:
        if value in (None, ""):
            return None
        return parse_qty(value, field="trigger_price")

    wanted: dict[str, Decimal | None] = {
        "STOP_MARKET": _desired(desired_sl),
        "TAKE_PROFIT_MARKET": _desired(desired_tp),
    }
    for order_type, price in wanted.items():
        if price is None:
            # 未显式给定则沿用现有同类型保护价；两者都没有时该侧无目标。
            same = [item for item in existing if item.order_type == order_type]
            if same:
                wanted[order_type] = same[0].trigger_price
        if wanted[order_type] is None:
            reasons.append(f"{protection_purpose(order_type)}_PRICE_UNKNOWN")

    cancel_candidates: list[ProtectionAction] = []
    for order_type, price in wanted.items():
        if price is None:
            continue
        same_type = [item for item in existing if item.order_type == order_type]
        kept = False
        for item in same_type:
            # closePosition 型保护在触发时按 positionSide 全平，数量天然跟随
            # 真实仓位；数量型保护必须 reduceOnly 且数量与仓位一致才算有效。
            qty_ok = item.close_position or (
                item.qty is not None and quantities_equal(item.qty, target))
            price_ok = quantities_equal(item.trigger_price, price)
            has_reduce_semantics = item.reduce_only or item.close_position
            if has_reduce_semantics and qty_ok and price_ok and not kept:
                actions.append(ProtectionAction(
                    "KEEP", order_type, item.client_id, item.trigger_price,
                    item.qty, "MATCHES_REAL_POSITION"))
                kept = True
                continue
            cancel_candidates.append(ProtectionAction(
                "CANCEL", order_type, item.client_id, item.trigger_price,
                item.qty, "SUPERSEDED_BY_REAL_POSITION"))
        if not kept:
            actions.append(ProtectionAction(
                "PLACE", order_type, None, price, target, "MISSING_OR_MISMATCHED"))
    # 目标里没有、交易所却存在的保护单：保留而不是顺手撤销（撤单需要明确授权）。
    for item in existing:
        if item.order_type not in PROTECTION_TYPES:
            continue
        if not any(a.client_id == item.client_id for a in actions + cancel_candidates):
            actions.append(ProtectionAction(
                "KEEP", item.order_type, item.client_id, item.trigger_price,
                item.qty, "NOT_IN_REFRESH_SCOPE"))

    surviving = [a for a in actions if a.action in {"KEEP", "PLACE"}]
    # 已知不可达（U04 r2 登记，非缺陷隐瞒）：``cancel_candidates`` 只在
    # ``price is not None`` 的迭代里追加，而同一迭代末尾必然追加 PLACE 或 KEEP，
    # 因此 ``surviving`` 永不为空（穷举 513648 组合未命中，见 U04/A_r1 P2-A4）。
    # 保留它是**未来回归护栏**：一旦有人改成"先收集 CANCEL 再决定 PLACE"，
    # 这里会立即拦住"撤掉最后一道保护"。运行期的实际保护由
    # 「PLACE 先于 CANCEL 执行 + can_cancel_protection 只认 flat_confirmed」提供。
    if cancel_candidates and not surviving:
        reasons.append(REASON_LAST_PROTECTION)
        return ProtectionPlan(
            symbol=position.symbol, position_side=position.position_side,
            target_qty=target, flat_confirmed=flat_confirmed,
            actions=tuple(actions),
            blocked_reason=REASON_LAST_PROTECTION,
            reason_codes=tuple(reasons),
        )
    if not surviving and position.abs_qty > 0:
        reasons.append("NO_PROTECTION_PLANNED_FOR_OPEN_POSITION")
    actions.extend(cancel_candidates)
    plan = ProtectionPlan(
        symbol=position.symbol, position_side=position.position_side,
        target_qty=target, flat_confirmed=flat_confirmed,
        actions=tuple(actions), reason_codes=tuple(reasons),
    )
    _assert_no_cancel_before_place(plan)
    return plan


def _assert_no_cancel_before_place(plan: ProtectionPlan) -> None:
    """静态守护：计划自身的动作顺序必须是"先挂新、后撤旧"。

    校验 ``actions`` 的原始构造顺序（而非 ``ordered()`` 的排序结果），这样
    未来若有人在循环里提前追加 CANCEL，会在规划阶段就失败而不是留下裸仓。

    **已知结构性不可达（U04 r2 登记）**：当前所有 CANCEL 都被追加在 PLACE 之后，
    因此本函数不会在运行期触发（U04/A_r1 P3-8）。它同样属于未来回归护栏，
    运行期真正的时序保证在 :func:`execute_protection_plan`。
    """
    if not plan.places or not plan.cancels:
        return
    kinds = [action.action for action in plan.actions]
    first_cancel = kinds.index("CANCEL")
    last_place = max(index for index, kind in enumerate(kinds) if kind == "PLACE")
    if first_cancel < last_place:
        raise ProtectionSafetyError("撤旧前必须先挂新保护")


@dataclass(frozen=True)
class PlanExecution:
    """保护计划执行结果。

    * ``cancel_skipped``：因新保护未挂上（或有 PLACE 失败）而**整体**未撤旧；
    * ``cancel_failed``：撤旧回调明确返回**未执行/失败**的 client_id 列表。
      这些单子**不能**记进 ``canceled``——否则账本会宣称"旧保护已撤"，而交易所
      侧旧保护仍在（重复保护 + 过期触发器仍会触发，覆盖报告也会误判已终结）。
    """

    placed: tuple[str, ...]
    kept: tuple[str, ...]
    canceled: tuple[str, ...]
    cancel_skipped: bool
    failed_place: str | None = None
    reason_codes: tuple[str, ...] = ()
    cancel_failed: tuple[str, ...] = ()
    cancel_unexecuted_reason: str = ""


def execute_protection_plan(
    plan: ProtectionPlan,
    *,
    place: Any,
    cancel: Any,
) -> PlanExecution:
    """按序执行保护计划：任一 PLACE 失败即**停止**，绝不继续撤旧保护。

    这是"不得撤销最后一道保护"在时序上的保证：撤旧永远发生在新保护确认挂上
    之后；新保护没挂上时宁可留下重复保护，也不留下裸仓。

    回调契约：``cancel`` 必须返回**真值**才算"已撤销"。返回 ``False``/``None``
    （例如只知 ``clientAlgoId``、匹配不到交易所 ``algoId`` 而无法发出 DELETE）
    表示未执行，会记入 ``cancel_failed`` 而不是 ``canceled``。用"真值才算成功"
    而非"假值才算失败"，是为了让**忘记返回**的回调落在安全一侧。
    """
    if not plan.safe_to_execute:
        return PlanExecution((), (), (), True, None,
                             (plan.blocked_reason or "PLAN_NOT_EXECUTABLE",))
    placed: list[str] = []
    kept: list[str] = []
    canceled: list[str] = []
    failed_cancels: list[str] = []
    cancel_reason = ""
    for action in plan.ordered():
        if action.action == "KEEP":
            kept.append(str(action.client_id))
        elif action.action == "PLACE":
            try:
                placed_id = place(action)
            except Exception as exc:  # noqa: BLE001 - 失败必须停止后续撤旧
                return PlanExecution(
                    tuple(placed), tuple(kept), tuple(canceled), True,
                    action.order_type, (f"PLACE_FAILED:{type(exc).__name__}",),
                    tuple(failed_cancels), cancel_reason)
            placed.append(str(placed_id or action.order_type))
        elif action.action == "CANCEL":
            outcome = cancel(action)
            if not outcome:
                failed_cancels.append(str(action.client_id))
                cancel_reason = cancel_reason or REASON_CANCEL_NOT_EXECUTED
                continue
            canceled.append(str(action.client_id))
    return PlanExecution(tuple(placed), tuple(kept), tuple(canceled), False,
                         None, (), tuple(failed_cancels), cancel_reason)


def can_cancel_protection(
    *, position: RealPosition, protections: Sequence[ProtectionView],
    target_client_id: str, flat_confirmed: bool = False,
) -> tuple[bool, str]:
    """判定单独撤销一张保护单是否安全；返回 (是否允许, reason code)。

    ``position.is_flat`` 只是"快照数量为 0"，**不等于**已确认归零（快照竞态、
    挂单未成交、镜像滞后都会出现 ``positionAmt==0``）。因此这里只认
    ``flat_confirmed``，与 :func:`plan_protection_refresh` 同口径：未确认归零时
    绝不清理最后一道保护，且 reason 不谎称 ``FLAT_CONFIRMED_CLEANUP``。

    ``flat_confirmed`` 按**严格布尔**判定（``is True``，N3）：签名声明 ``bool``，
    ``"false"``/``1``/``[0]`` 这类非布尔真值一律按"未确认"处理（fail-closed）。
    选择"降级为未确认"而不是"抛异常"是有意的——本函数的返回契约是
    ``(bool, reason)``，非布尔入参只可能是调用方违约，降级到保守分支既不放行
    危险动作，也不会在撤单路径上引入新的异常面。
    """
    if flat_confirmed is True:
        return True, "FLAT_CONFIRMED_CLEANUP"
    remaining = [
        item for item in protections
        if item.client_id != target_client_id
        and (item.reduce_only or item.close_position)
    ]
    if not remaining:
        if position.is_flat:
            # 快照为零但未经确认：既不能撤最后一道保护，也不能报告"已确认归零"。
            return False, REASON_FLAT_NOT_CONFIRMED
        return False, REASON_LAST_PROTECTION
    return True, ""


# --------------------------------------------------------------------------- #
# 8. 执行故障分类：健康故障不得触发全平，也不改 pause/emergency
# --------------------------------------------------------------------------- #
class FaultClass(str, Enum):
    SUBMIT_UNCERTAIN = "SUBMIT_UNCERTAIN"
    QUERY_UNCERTAIN = "QUERY_UNCERTAIN"
    TRANSPORT = "TRANSPORT"
    RATE_LIMIT = "RATE_LIMIT"
    HEALTH = "HEALTH"
    REJECTION = "REJECTION"
    #: 已确证的保护挂单失败：不是观测故障，必须按 P0 兜底处理（不得静默留守裸仓）。
    PROTECTION = "PROTECTION"
    #: 无法归类的故障：既不能当健康故障（会放过保护缺口），也不能当已确证故障，
    #: 只做保守告警与人工复核，风险动作由调用方按 P0 策略决定。
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class FaultActionPlan:
    fault: str
    fault_class: FaultClass
    action: str
    blocks_new_open: bool
    alert_required: bool
    reason_codes: tuple[str, ...] = ()
    # 三条硬不变量：健康/传输故障均不改变仓、不改风险档、不动 pause/emergency。
    triggers_flat_close: bool = False
    changes_risk_profile: bool = False
    touches_pause_state: bool = False
    touches_emergency_state: bool = False


_HEALTH_LIKE = frozenset({
    FaultClass.TRANSPORT, FaultClass.HEALTH, FaultClass.RATE_LIMIT,
    FaultClass.QUERY_UNCERTAIN,
})
_FAULT_TABLE: dict[str, tuple[FaultClass, str]] = {
    "TIMEOUT": (FaultClass.SUBMIT_UNCERTAIN, "QUERY_BY_ORIGINAL_ID"),
    "RESPONSE_LOST": (FaultClass.SUBMIT_UNCERTAIN, "QUERY_BY_ORIGINAL_ID"),
    "HTTP_503": (FaultClass.SUBMIT_UNCERTAIN, "QUERY_BY_ORIGINAL_ID"),
    "HTTP_5XX": (FaultClass.SUBMIT_UNCERTAIN, "QUERY_BY_ORIGINAL_ID"),
    "CONNECTION_RESET": (FaultClass.TRANSPORT, "QUERY_BY_ORIGINAL_ID"),
    "WS_DISCONNECT": (FaultClass.TRANSPORT, "SKIP_CYCLE_AND_RESUBSCRIBE"),
    "QUERY_FAILED": (FaultClass.QUERY_UNCERTAIN, "BACKOFF_AND_REQUERY"),
    "UNKNOWN_BUDGET_EXHAUSTED": (FaultClass.QUERY_UNCERTAIN, "MANUAL_RECOVERY"),
    "RATE_LIMITED": (FaultClass.RATE_LIMIT, "BACKOFF_AND_SKIP_ENTRY"),
    "HEALTH_DEGRADED": (FaultClass.HEALTH, "ALERT_ONLY"),
    "HEARTBEAT_STALE": (FaultClass.HEALTH, "ALERT_ONLY"),
    "REJECTED": (FaultClass.REJECTION, "NO_RETRY"),
    "INSUFFICIENT_MARGIN": (FaultClass.REJECTION, "NO_RETRY"),
    "PROTECTION_FAILED": (FaultClass.PROTECTION, "ALERT_AND_RETRY_PROTECTION"),
    "PLACE_FAILED": (FaultClass.PROTECTION, "ALERT_AND_RETRY_PROTECTION"),
    "UNKNOWN_FAULT": (FaultClass.UNKNOWN, "ALERT_AND_MANUAL_REVIEW"),
}


def classify_execution_fault(fault: Any) -> FaultActionPlan:
    """把执行故障映射为保守动作；未知故障一律 fail-closed 到人工/告警。"""
    name = fault if isinstance(fault, str) else type(fault).__name__
    text = str(name).strip().upper()
    if not text or text in {"EXCEPTION", "EXC"}:
        text = "UNKNOWN_FAULT"
    if "TIMEOUT" in text or "TIMEDOUT" in text:
        text = "TIMEOUT"
    elif "503" in text:
        text = "HTTP_503"
    elif text.startswith("HTTP5") or "HTTP_5" in text:
        text = "HTTP_5XX"
    elif "WS" in text and "DISCONNECT" in text:
        text = "WS_DISCONNECT"
    elif text not in _FAULT_TABLE and "CONNECTION" in text:
        text = "CONNECTION_RESET"
    if text in _FAULT_TABLE:
        fault_class, action = _FAULT_TABLE[text]
        known = True
    else:
        # 未归类故障不得默认成健康故障：否则"保护确实失败"这类确定性故障会被
        # 当成观测故障而放过裸仓风险（P0）。默认只告警 + 人工复核，由调用方
        # 按 P0 策略决定风险动作。
        fault_class, action, known = FaultClass.UNKNOWN, "ALERT_AND_MANUAL_REVIEW", False
    reasons = [REASON_HEALTH_NO_FLAT_CLOSE] if fault_class in _HEALTH_LIKE else []
    if not known:
        reasons.append("FAULT_UNRECOGNIZED_FAIL_CLOSED")
    return FaultActionPlan(
        fault=text, fault_class=fault_class, action=action,
        blocks_new_open=fault_class in _HEALTH_LIKE or fault_class in {
            FaultClass.SUBMIT_UNCERTAIN},
        alert_required=fault_class is not FaultClass.REJECTION,
        reason_codes=tuple(reasons),
    )


FLAT_CLOSE_ACTIONS = frozenset({
    "CLOSE_ALL", "FLATTEN", "CANCEL_ALL_PROTECTION", "EMERGENCY_CLOSE",
})

#: 允许驱动仓位动作的故障类**白名单**：只有"已确证的保护挂单失败"。
#: 其余（含观测故障、提交结果未知、未归类 ``UNKNOWN``）一律 fail-closed：不得
#: 由故障分类去触发全平。用白名单而非"枚举健康类"，是因为健康类名永远枚举不全
#: （``PROTECTION_MIRROR_STALE``/``ALGO_ORDER_QUERY_FAILED`` 等都属观测故障却
#: 不在登记表内）；枚举不完的一侧必须落在"拒绝"上。
FLAT_CLOSE_AUTHORIZED: frozenset[FaultClass] = frozenset({FaultClass.PROTECTION})

#: "观测故障"集合：这类故障下无法证明当前仓位无保护，因此**推迟兜底平仓**
#: （只告警、下轮重试）。方向与 ``FLAT_CLOSE_AUTHORIZED`` 相反——这里是"别平"，
#: 那里是"别乱平"；两者都必须避免用枚举不完的表做默认放行。
_OBSERVATION_FAULTS: frozenset[FaultClass] = _HEALTH_LIKE


def defers_flat_close(sync_reason: Any) -> bool:
    """保护同步失败类别是否属于**已确证的观测故障**（据此推迟兜底平仓）。

    空/`None` 一律返回 ``False``：没有可归类的观测故障证据时必须照常兜底平仓，
    绝不允许"证据缺失"变成"保留裸仓"。
    """
    text = str(sync_reason or "").strip()
    if not text:
        return False
    return classify_execution_fault(text).fault_class in _OBSERVATION_FAULTS


class ProtectionSyncReasons:
    """按标的保存"最近一轮"保护同步的失败类别。

    两个必须成立的属性（V1 曾把状态做成跨标的、成功路径不复位的进程级标量）：

    * **按标的隔离**：A 标的的 ``QUERY_FAILED`` 不得抑制 B 标的的兜底平仓；
    * **每轮复位**：``begin()`` 在每个同步轮次入口清空该标的的旧值，因此
      "上几轮查询失败、本轮查询成功"不会留下残余 ``QUERY_FAILED``。
    """

    def __init__(self) -> None:
        self._by_symbol: dict[str, str] = {}

    @staticmethod
    def _key(symbol: Any) -> str:
        return str(symbol or "").upper()

    def begin(self, symbol: Any) -> None:
        """同步轮次入口：先复位该标的，任何提前 return 都不会留下残余类别。"""
        self._by_symbol.pop(self._key(symbol), None)

    def mark(self, symbol: Any, reason: Any) -> None:
        text = str(reason or "").strip()
        if text:
            self._by_symbol[self._key(symbol)] = text
        else:
            self._by_symbol.pop(self._key(symbol), None)

    def get(self, symbol: Any) -> str:
        return self._by_symbol.get(self._key(symbol), "")

    def defers(self, symbol: Any) -> bool:
        return defers_flat_close(self.get(symbol))

    def as_dict(self) -> dict[str, str]:
        return dict(self._by_symbol)


def guard_fault_action(fault: Any, proposed_action: str) -> FaultActionPlan:
    """阻止把健康/传输故障升级成全平；返回保守计划，非法动作直接抛错。

    fail-closed：只要故障类不在 :data:`FLAT_CLOSE_AUTHORIZED`（即"保护确证失败"）
    之内，任何 ``FLAT_CLOSE_ACTIONS`` 提议都被拒绝——**包括**未登记的健康类
    （落入 ``UNKNOWN``）与"提交结果未知"的 ``SUBMIT_UNCERTAIN``（此时订单可能已
    在交易所存活，全平会二次平仓甚至反手开仓）。
    """
    plan = classify_execution_fault(fault)
    action = str(proposed_action or "").strip().upper()
    if action in FLAT_CLOSE_ACTIONS and plan.fault_class not in FLAT_CLOSE_AUTHORIZED:
        reason = (REASON_HEALTH_NO_FLAT_CLOSE
                  if plan.fault_class in _HEALTH_LIKE
                  else REASON_FLAT_CLOSE_NOT_CONFIRMED)
        raise ProtectionSafetyError(
            f"{plan.fault} 属 {plan.fault_class.value}，不得触发全平动作 {action}"
            f"（{reason}）")
    if plan.triggers_flat_close or plan.touches_pause_state or \
            plan.touches_emergency_state or plan.changes_risk_profile:
        raise ProtectionSafetyError("故障计划不得改变仓、风险档或 pause/emergency 语义")
    if action in FLAT_CLOSE_ACTIONS:
        # 唯一被授权的兜底平仓（保护确证失败）：审计计划必须**如实记录**确实发生了
        # 仓位动作，否则计划对象不能作为"是否平仓"的证据（V1 的 triggers_flat_close
        # 恒为 False，等于放行全平却在审计里否认）。
        return replace(plan, action=action, triggers_flat_close=True)
    return plan


STATE_FLAG_KEYS = ("paused", "emergency", "kill_switch", "live_enabled")


def execution_state_flags(state: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
    """抽取 pause/emergency 等控制位快照，用于证明执行故障未改动其语义。"""
    return tuple((key, state.get(key)) for key in STATE_FLAG_KEYS)


def guard_state_flags_unchanged(
    before: Mapping[str, Any], after: Mapping[str, Any],
) -> None:
    """执行路径不得改动 pause/emergency/live 语义；变化即抛错。"""
    left = execution_state_flags(before)
    right = execution_state_flags(after)
    if left != right:
        changed = [key for (key, old), (_, new) in zip(left, right) if old != new]
        raise ProtectionSafetyError(f"执行故障处理改动了控制位: {changed}")


__all__ = [
    "ALGO_ENDPOINT",
    "STANDARD_ENDPOINT",
    "CommandKind",
    "CommandKindConflict",
    "CoverageReport",
    "CrossKindCoverageClaim",
    "ExchangeOrderReading",
    "ExecutionCommandError",
    "FaultActionPlan",
    "FaultClass",
    "FillRevision",
    "KindCoverage",
    "FLAT_CLOSE_ACTIONS",
    "FLAT_CLOSE_AUTHORIZED",
    "PlanExecution",
    "ProtectionAction",
    "ProtectionPlan",
    "ProtectionSafetyError",
    "ProtectionSyncReasons",
    "ProtectionView",
    "QuantizationInputError",
    "QuantityStateConflict",
    "RealPosition",
    "SubmissionOutcome",
    "SubmissionReading",
    "UnknownDecision",
    "UnknownPlan",
    "UnknownReconcileReport",
    "apply_late_fill",
    "assert_kind_coverage",
    "can_cancel_protection",
    "cancel_then_late_fill_probe",
    "classify_endpoint",
    "classify_execution_fault",
    "classify_row",
    "coverage_report",
    "defers_flat_close",
    "execute_protection_plan",
    "execute_unknown_reconcile",
    "ensure_execution_schema",
    "execution_state_flags",
    "guard_fault_action",
    "guard_state_flags_unchanged",
    "parse_qty",
    "parse_timestamp_ms",
    "plan_protection_refresh",
    "plan_unknown_reconcile",
    "protection_purpose",
    "protection_view_from_algo",
    "qty_tolerance",
    "quantities_equal",
    "quantities_from_order_payload",
    "query_api_for",
    "read_exchange_order",
    "read_submission_result",
    "real_position_from_snapshot",
]
