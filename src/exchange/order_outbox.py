"""USD-M普通订单Outbox：POST前持久化，UNKNOWN只查询不重发。"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from audit.database import AuditDatabase, utc_now
from decimal import Decimal
from core.contracts import (
    DuplicateFillError,
    OrderQuantity,
    decimal_from_float,
    decimal_text,
)
from exchange.event_models import ExchangeEvent
from exchange.protection_commands import (
    CommandKind,
    ExecutionCommandError,
    apply_late_fill,
    classify_endpoint,
    classify_row,
    ensure_execution_schema,
    quantities_from_order_payload,
)


_ACTIVE = {"SUBMITTING", "UNKNOWN", "ACKNOWLEDGED", "NEW", "PARTIALLY_FILLED"}
_TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}
_RANK = {
    "SUBMITTING": 0, "UNKNOWN": 5, "ACKNOWLEDGED": 10,
    "NEW": 20, "PARTIALLY_FILLED": 30,
    "FILLED": 40, "CANCELED": 40, "EXPIRED": 40,
    "REJECTED": 40, "EXPIRED_IN_MATCH": 40,
}


#: 交易所状态别名 -> OrderQuantity 词表（``EXPIRED_IN_MATCH`` 归入 ``EXPIRED``）。
_STATUS_ALIASES = {"EXPIRED_IN_MATCH": "EXPIRED"}
#: 终态集合（OrderQuantity 词表口径）。
_QUANTITY_TERMINAL = {"FILLED", "CANCELED", "EXPIRED", "REJECTED"}


def _component_status(*, accepted: Decimal, filled: Decimal, stored_status: str) -> str:
    """由三分量推导 ``OrderQuantity`` 状态（终态保留原状态，迟到成交不复活订单）。

    V1 用"白名单外的状态一律当 ``PARTIALLY_FILLED``"兜底，而 ``PARTIALLY_FILLED``
    要求 ``0 < filled < accepted``——于是 ``SUBMITTING``/``ACKNOWLEDGED``/``UNKNOWN``
    这些真实前置状态与 ``filled == 0`` 的第一笔成交全部抛 ``STATUS_FILL_MISMATCH``。
    这里改为**由分量推导**：``filled == 0 -> NEW``、``0 < filled < accepted ->
    PARTIALLY_FILLED``、``filled == accepted -> FILLED``，终态保留原状态。

    **兜底语义：别名未命中时保留原值**（``.get(x, x)`` 而不是 ``.get(x, "")``）。
    终态判定必须看得见订单自己的状态：若兜底成空串，``CANCELED``/``EXPIRED``/
    ``REJECTED``/``FILLED`` 会被当成"未知状态"，"终态保留原状态"这条分支对它们
    **永久失效**——终态行收到迟到成交后会被去终态化成 ``PARTIALLY_FILLED``，
    ``working`` 非零、``terminal`` 归零，该行随即违反 K04 终态不变量。词表外的状态
    不会因此外泄：它们不落在终态集合里，一律落到下面的分量推导分支，返回值恒为
    ``OrderQuantity`` 词表内的状态（未知状态走 fail-safe 推导，不原样透传）。

    **``FILLED`` 必须由分量背书（N6）**：``apply_event`` 的帧序是"先
    ``_update(status=X)`` 再 ``apply_fill_revision(l)``"，因此处理 ``X="FILLED"`` 帧
    时落库的 ``status`` 已经是 ``FILLED``，而本帧的成交**尚未入账**——这是一个中间
    态。若照搬"终态保留原状态"，调用方会走 ``working, terminal = 0, 0`` 分支，于是
    ``accepted(0.02) != filled(0.0105) + 0 + 0`` 触发守恒违规、``QuantityError`` 被
    ``apply_event`` 吞掉：**本帧成交永远丢失、行永久非法且不可自愈**（任何后续修订都
    命中同一分支）。判据只能是分量：``status="FILLED"`` 而 ``filled < accepted`` 时
    它不是终态事实，退回分量推导（此时得到 ``PARTIALLY_FILLED``/``NEW``），由
    ``apply_fill`` 在成交入账后把状态自行推进到 ``FILLED``。``CANCELED``/``EXPIRED``/
    ``REJECTED`` 不受影响——它们的终态不依赖 ``filled``。
    """
    stored = _STATUS_ALIASES.get(str(stored_status or "").upper(),
                                 str(stored_status or "").upper())
    if stored in _QUANTITY_TERMINAL and not (stored == "FILLED" and filled < accepted):
        return stored
    if filled <= 0:
        return "NEW"
    if filled < accepted:
        return "PARTIALLY_FILLED"
    return "FILLED"


class PendingOrderConflict(RuntimeError):
    """同一业务键已有未决开仓命令。"""


class DuplicateOrderSubmission(RuntimeError):
    """同一clientOrderId已持久化，禁止再次POST。"""


class OrderOutbox:
    """持久化普通订单提交状态，消除网络不确定性导致的重复下单。

    调用顺序必须是 ``prepare`` 落库后才能发送 POST。若请求超时或连接断开，订单进入
    ``UNKNOWN``，后续只按 ``clientOrderId`` 查询交易所，不允许重发。状态只能前进，
    终态不会被迟到的 WebSocket 事件回退。
    """
    def __init__(self, db: AuditDatabase, *, environment: str,
                 max_reconcile_per_cycle: int = 5) -> None:
        self.db = db
        self.environment = str(environment)
        # U04：幂等补齐命令种类与十进制数量列（向后兼容列，不抬升 schema 版本号，
        # 因为 schema_version 参与 release_evidence 的 GO 判定）。
        ensure_execution_schema(self.db.conn)
        self.last_event_at: float | None = None
        self.last_reconcile_at: float | None = None
        self.last_error: str | None = None
        self.reconciled_unknown = 0
        self.max_reconcile_per_cycle = max(1, int(max_reconcile_per_cycle))

    @staticmethod
    def business_key(*, symbol: str, side: str, position_side: str | None,
                     purpose: str) -> str:
        """构造同一交易目的的稳定业务键，用于识别未决开仓冲突。"""
        return f"{purpose}:{symbol.upper()}:{(position_side or side).upper()}"

    def prepare(self, *, client_order_id: str, symbol: str, side: str,
                position_side: str | None, order_type: str, purpose: str,
                qty: float, price: float | None, reduce_only: bool,
                endpoint: str, request: dict[str, Any],
                command_kind: str | None = None,
                qty_text: str | None = None) -> None:
        """在交易所 POST 前记录 SUBMITTING；重复 ID 或未决开仓会直接拒绝。

        ``command_kind`` 显式声明普通订单或 Algo 保护命令（R07）：缺省时按端点判定，
        端点未知直接拒绝，不默认成普通订单。``qty_text`` 用十进制文本保存受理量：
        缺省时的 ``str(qty)`` 是 float 的 repr，会把 ``str(0.1+0.2)`` 这类尾差写进
        "精确"列，所以这里统一经 ``decimal_from_float`` 归一（U04/DC04）。
        """
        now = utc_now()
        kind = CommandKind(command_kind) if command_kind else classify_endpoint(endpoint)
        accepted_qty_text = (str(qty_text) if qty_text is not None
                             else decimal_text(decimal_from_float(qty)))
        key = self.business_key(symbol=symbol, side=side,
                                position_side=position_side, purpose=purpose)
        with self.db.lock:
            existing = self.db.conn.execute(
                "SELECT status FROM order_outbox WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            if existing:
                raise DuplicateOrderSubmission(
                    f"clientOrderId {client_order_id} already {existing['status']}")
            if purpose.upper() == "ENTRY":
                unresolved = self.db.conn.execute(
                    "SELECT client_order_id FROM order_outbox WHERE environment=? "
                    "AND status IN ('SUBMITTING','UNKNOWN') LIMIT 1",
                    (self.environment,),
                ).fetchone()
                if unresolved:
                    raise PendingOrderConflict(
                        f"unresolved order {unresolved['client_order_id']} blocks new entry")
            try:
                self.db.conn.execute(
                    """INSERT INTO order_outbox(
                        client_order_id,release_id,environment,business_key,symbol,side,position_side,
                        order_type,purpose,qty,price,reduce_only,endpoint,status,request_json,
                        attempt_count,created_at,updated_at,command_kind,accepted_qty_text,
                        cumulative_filled_qty_text,working_leaves_qty_text,
                        terminal_unfilled_qty_text,applied_fill_ids_json,quantity_revision
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (client_order_id, getattr(self.db, "release_id", "legacy"), self.environment,
                     key, symbol.upper(), side.upper(),
                     (position_side or "BOTH").upper(), order_type.upper(), purpose.upper(),
                     float(qty), price, int(reduce_only), endpoint, "SUBMITTING",
                     json.dumps(request, ensure_ascii=False, separators=(",", ":")),
                     1, now, now, kind.value, accepted_qty_text,
                     "0", accepted_qty_text, "0", "[]", 0),
                )
                self.db.conn.commit()
            except sqlite3.IntegrityError as exc:
                self.db.conn.rollback()
                raise PendingOrderConflict(
                    f"pending entry already exists for {key}") from exc

    def recover_stale_submitting(self) -> int:
        """进程重启后SUBMITTING的POST结果未知，转换为UNKNOWN且绝不重发。"""
        with self.db.lock:
            cursor = self.db.conn.execute(
                "UPDATE order_outbox SET status='UNKNOWN',error_type='ProcessRestart',"
                "unknown_since=COALESCE(unknown_since,?),next_retry_at=?,updated_at=? "
                "WHERE environment=? AND status='SUBMITTING'",
                (utc_now(), utc_now(), utc_now(), self.environment),
            )
            self.db.conn.commit()
            return int(cursor.rowcount)

    def acknowledge(self, client_order_id: str, response: dict[str, Any]) -> None:
        """用交易所同步响应确认订单；响应终态会同时写入解决时间。"""
        status = str(response.get("status") or response.get("algoStatus")
                     or "ACKNOWLEDGED").upper()
        if status not in _ACTIVE | _TERMINAL:
            status = "ACKNOWLEDGED"
        resolved = utc_now() if status in _TERMINAL else None
        self._update(
            client_order_id, status=status,
            exchange_order_id=str(response.get("orderId") or response.get("algoId") or "") or None,
            response=response, error_type=None, resolved_at=resolved)

    def mark_unknown(self, client_order_id: str, exc: BaseException) -> None:
        """将无法确认提交结果的订单标为 UNKNOWN，等待查询或私有流收敛。"""
        now = utc_now()
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE order_outbox SET status='UNKNOWN',error_type=?,"
                "unknown_since=COALESCE(unknown_since,?),next_retry_at=?,updated_at=? "
                "WHERE client_order_id=? AND status NOT IN "
                "('FILLED','CANCELED','EXPIRED','REJECTED','EXPIRED_IN_MATCH')",
                (type(exc).__name__, now, now, now, client_order_id),
            )
            self.db.conn.commit()

    def reject(self, client_order_id: str, exc: BaseException) -> None:
        """记录可确定未被交易所接受的订单失败。

        K04 要求 ``REJECTED`` 的 ``accepted/filled`` 必须为 0：``prepare`` 在 POST 前
        写入的是**请求量**（此时受理未知），既然交易所已明确拒绝，这里必须把
        ``accepted_qty_text`` 清零，否则该行永远无法构造出合法的 ``OrderQuantity``
        （``REJECTED_WITH_ACCEPTED``），后续任何成交/对账路径都会炸。
        """
        self._update(client_order_id, status="REJECTED",
                     error_type=type(exc).__name__, resolved_at=utc_now(),
                     zero_quantities=True)

    def apply_exchange_quantities(
        self, client_order_id: str, *, accepted_qty: str, filled_qty: str,
        working_leaves_qty: str, terminal_unfilled_qty: str, revision: int,
    ) -> None:
        """按交易所回报落库三分量（十进制文本），不改变命令种类与端点。"""
        with self.db.lock:
            self.db.conn.execute(
                "UPDATE order_outbox SET accepted_qty_text=?,cumulative_filled_qty_text=?,"
                "working_leaves_qty_text=?,terminal_unfilled_qty_text=?,quantity_revision=?,"
                "updated_at=? WHERE client_order_id=?",
                (str(accepted_qty), str(filled_qty), str(working_leaves_qty),
                 str(terminal_unfilled_qty), int(revision), utc_now(), client_order_id),
            )
            self.db.conn.commit()

    def apply_fill_revision(
        self, client_order_id: str, *, fill_qty: str, fill_id: str,
    ) -> dict[str, Any] | None:
        """应用一笔成交事实并持久化修订（含 CANCELED 后的迟到成交）。

        fill 级幂等由 ``applied_fill_ids_json`` 保证：同一 ``fill_id`` 重复投递抛
        ``DuplicateFillError``；终态订单只修订累计成交与终态余量，状态不变。缺少
        三分量记录的旧行返回 ``None``，由调用方先落一次交易所回报。

        **状态由分量推导，不靠"白名单之外的都当 PARTIALLY_FILLED"兜底**：V1 的兜底
        把 ``SUBMITTING``/``ACKNOWLEDGED``/``UNKNOWN`` 这些真实前置状态（以及
        ``filled == "0"`` 的 ``PARTIALLY_FILLED``）都强行映射成 ``PARTIALLY_FILLED``，
        而该状态要求 ``0 < filled < accepted``，于是**第一笔成交永远抛
        ``STATUS_FILL_MISMATCH``**。
        """
        with self.db.lock:
            row = self.db.conn.execute(
                "SELECT * FROM order_outbox WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            if not row:
                return None
            keys = set(row.keys())
            if "accepted_qty_text" not in keys or not row["accepted_qty_text"]:
                return None
            applied = json.loads(str(row["applied_fill_ids_json"] or "[]"))
            stored_status = str(row["status"] or "SUBMITTING").upper()
            accepted = Decimal(str(row["accepted_qty_text"]))
            filled = Decimal(str(row["cumulative_filled_qty_text"] or "0"))
            status = _component_status(
                accepted=accepted, filled=filled, stored_status=stored_status)
            if status in _QUANTITY_TERMINAL and status != "FILLED":
                # 终态：working 必须为 0，余量 = 受理 - 累计成交（K04 终态语义）。
                working, terminal = Decimal(0), accepted - filled
            elif status == "FILLED":
                working, terminal = Decimal(0), Decimal(0)
            else:
                # 工作态：余量随成交推进，终态余量为 0（与 apply_fill 同口径）。
                working, terminal = accepted - filled, Decimal(0)
            quantity = OrderQuantity.checked(
                order_id=str(row["exchange_order_id"] or client_order_id),
                accepted_qty=accepted,
                cumulative_filled_qty=filled,
                working_leaves_qty=working,
                terminal_unfilled_qty=terminal,
                status=status,
                revision=int(row["quantity_revision"] or 0),
            )
            revision = apply_late_fill(
                quantity, fill_qty=fill_qty, fill_id=fill_id,
                applied_fill_ids=tuple(applied),
            )
            updated = revision.quantity
            self.db.conn.execute(
                "UPDATE order_outbox SET cumulative_filled_qty_text=?,"
                "working_leaves_qty_text=?,terminal_unfilled_qty_text=?,quantity_revision=?,"
                "applied_fill_ids_json=?,updated_at=? WHERE client_order_id=?",
                (decimal_text(updated.cumulative_filled_qty),
                 decimal_text(updated.working_leaves_qty),
                 decimal_text(updated.terminal_unfilled_qty), int(updated.revision),
                 json.dumps(list(applied) + [fill_id]), utc_now(), client_order_id),
            )
            self.db.conn.commit()
        if updated.status != quantity.status:
            self._update(client_order_id, status=updated.status)
        return {
            "client_order_id": client_order_id,
            "status": updated.status,
            "fill_id": fill_id,
            "revision_only": revision.revision_only,
            "cumulative_filled_qty": decimal_text(updated.cumulative_filled_qty),
            "terminal_unfilled_qty": decimal_text(updated.terminal_unfilled_qty),
        }

    def _update(self, client_order_id: str, *, status: str,
                exchange_order_id: str | None = None,
                response: dict[str, Any] | None = None,
                error_type: str | None = None,
                resolved_at: str | None = None,
                zero_quantities: bool = False) -> None:
        """状态迁移 + 终态三分量同步。

        **落库的 ``status`` 必须落在 ``OrderQuantity`` 词表内（N7）**：私有流会把
        交易所别名 ``EXPIRED_IN_MATCH`` 原样送进来，而该串不在词表里——直接落库会让
        整行**永久无法重水化**（任何按行读取的对账/审计重放都抛 ``ValidationError``），
        且因为它不在 ``_QUANTITY_TERMINAL``，下面的终态同步分支不执行，留下一行
        ``working = accepted`` 的"终态"行（违反 K04）。这里沿用
        ``_STATUS_ALIASES`` 先归一（``EXPIRED_IN_MATCH -> EXPIRED``），与
        ``_component_status``/``apply_fill_revision`` 的返回值口径一致；顺带把
        **历史遗留**的别名行也归一，使旧行在下一个事件到达时自愈。
        """
        with self.db.lock:
            previous = self.db.conn.execute(
                "SELECT * FROM order_outbox WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            if not previous:
                return
            alias = _STATUS_ALIASES.get(str(status).upper())
            if alias:
                status = alias
            previous_raw = str(previous["status"] or "SUBMITTING")
            previous_status = _STATUS_ALIASES.get(previous_raw.upper(), previous_raw)
            if (previous_status in _TERMINAL and status != previous_status) or (
                    previous_status not in _TERMINAL and
                    _RANK.get(status, 0) < _RANK.get(previous_status, 0)):
                return
            # 状态转到终态时同步三分量：终态的 working 必须为 0、余量等于
            # accepted - filled（K04 终态语义）。不同步的话，一行 CANCELED/EXPIRED
            # 会带着 working == accepted 永久失配，之后任何成交修订都会因
            # VIOLATION_WORKING_WHILE_TERMINAL 抛错而**再也修不回来**。
            quantity_texts: dict[str, str] | None = None
            if "accepted_qty_text" in set(previous.keys()):
                accepted_raw = previous["accepted_qty_text"]
                if zero_quantities:
                    quantity_texts = {
                        "accepted": "0", "filled": "0", "working": "0", "terminal": "0"}
                elif accepted_raw not in (None, ""):
                    try:
                        accepted = Decimal(str(accepted_raw))
                        filled = Decimal(str(previous["cumulative_filled_qty_text"] or "0"))
                        if status in _QUANTITY_TERMINAL and filled <= accepted:
                            quantity_texts = {
                                "accepted": str(accepted), "filled": str(filled),
                                "working": "0", "terminal": str(accepted - filled)}
                    except (ArithmeticError, TypeError, ValueError):
                        quantity_texts = None
            self.db.conn.execute(
                """UPDATE order_outbox SET status=?,
                    exchange_order_id=COALESCE(?,exchange_order_id),
                    response_json=COALESCE(?,response_json),error_type=?,
                    updated_at=?,resolved_at=COALESCE(?,resolved_at),
                    next_retry_at=CASE WHEN ? IS NOT NULL THEN NULL ELSE next_retry_at END,
                    last_query_error=CASE WHEN ? IS NOT NULL THEN NULL ELSE last_query_error END,
                    accepted_qty_text=COALESCE(?,accepted_qty_text),
                    cumulative_filled_qty_text=COALESCE(?,cumulative_filled_qty_text),
                    working_leaves_qty_text=COALESCE(?,working_leaves_qty_text),
                    terminal_unfilled_qty_text=COALESCE(?,terminal_unfilled_qty_text)
                    WHERE client_order_id=?""",
                (status, exchange_order_id,
                 json.dumps(response, ensure_ascii=False, separators=(",", ":"))
                 if response is not None else None,
                 error_type, utc_now(), resolved_at, resolved_at, resolved_at,
                 (quantity_texts or {}).get("accepted"),
                 (quantity_texts or {}).get("filled"),
                 (quantity_texts or {}).get("working"),
                 (quantity_texts or {}).get("terminal"),
                 client_order_id),
            )
            self.db.conn.commit()

    def apply_event(self, event: ExchangeEvent) -> bool:
        """应用匹配本环境 Outbox 的订单私有流事件；无关事件返回 False。"""
        if event.event_type != "ORDER_TRADE_UPDATE":
            return False
        order = event.payload.get("o") if isinstance(event.payload.get("o"), dict) else {}
        client_id = str(order.get("c") or "")
        if not client_id:
            return False
        row = self.db.fetch_one(
            "SELECT status FROM order_outbox WHERE client_order_id=? AND environment=?",
            (client_id, self.environment),
        )
        if not row:
            return False
        status = str(order.get("X") or "UNKNOWN").upper()
        if status not in _ACTIVE | _TERMINAL:
            status = str(row["status"])
        resolved = utc_now() if status in _TERMINAL else None
        self._update(client_id, status=status,
                     exchange_order_id=str(order.get("i") or "") or None,
                     response=order, error_type=None, resolved_at=resolved)
        # U04 接线：私有流的每一笔成交（``l`` 本次成交量、``t`` 成交 ID）落到三分量
        # 与 ``applied_fill_ids_json``。这是 ``apply_fill_revision`` 的**唯一生产调用
        # 点**——没有它，DC04 的"重启后按 fill_id 重放不二次计账"只是内存里的证明。
        last_fill_qty = order.get("l")
        trade_id = str(order.get("t") or "")
        if last_fill_qty not in (None, "", 0, "0") and trade_id:
            try:
                self.apply_fill_revision(
                    client_id, fill_qty=str(last_fill_qty), fill_id=trade_id)
            except DuplicateFillError:
                # 重复投递是 at-least-once 的正常形态，不是故障（幂等生效）。
                pass
            except Exception as exc:  # noqa: BLE001 - 入账失败不得中断事件流
                self.last_error = type(exc).__name__
                if self.db:
                    self.db.log_risk_event(
                        event_type="futures_order_error", severity="warn",
                        details=(f"{client_id}: 私有流成交未能落库 "
                                 f"{type(exc).__name__} {exc}"),
                        action_taken="fill revision skipped")
        self.last_event_at = time.time()
        self.last_error = None
        return True

    def reconcile_unknown(self, broker: Any) -> int:
        """限量查询到期 UNKNOWN；只查询、不重发，失败按指数退避。"""
        now = utc_now()
        rows = self.db.fetch_all(
            "SELECT client_order_id,symbol,endpoint,query_attempt_count FROM order_outbox "
            "WHERE environment=? AND status='UNKNOWN' "
            "AND (next_retry_at IS NULL OR next_retry_at<=?) "
            "ORDER BY COALESCE(unknown_since,updated_at) LIMIT ?",
            (self.environment, now, self.max_reconcile_per_cycle),
        )
        resolved = 0
        for row in rows:
            try:
                client_id = str(row["client_order_id"])
                if str(row["endpoint"]).endswith("/algoOrder"):
                    response = broker.query_algo_order(client_id)
                    found = isinstance(response, dict) and response.get("algoId")
                else:
                    response = broker.query_order(str(row["symbol"]), client_id)
                    found = isinstance(response, dict) and response.get("orderId")
                if found:
                    self.acknowledge(str(row["client_order_id"]), response)
                    # U04 接线：把交易所回报的三分量落到十进制文本列，供重启后
                    # 重放/续算（``apply_fill_revision`` 依赖这套列）。畸形或种类
                    # 不匹配的回报只跳过落库，不影响本次收口结果。
                    try:
                        reading = quantities_from_order_payload(
                            dict(response), kind=classify_row(row))
                        self.apply_exchange_quantities(
                            client_id,
                            accepted_qty=decimal_text(reading.accepted_qty),
                            filled_qty=decimal_text(reading.cumulative_filled_qty),
                            working_leaves_qty=decimal_text(reading.working_leaves_qty),
                            terminal_unfilled_qty=decimal_text(reading.terminal_unfilled_qty),
                            revision=reading.revision)
                    except Exception as exc:  # noqa: BLE001 - 只跳过数量落库
                        # 注意：必须在这里吞掉，否则会被外层 except 当成"查单失败"
                        # 记入退避与重试计数，把一个落库问题伪装成网络问题。
                        if self.db:
                            self.db.log_risk_event(
                                event_type="futures_order_error", severity="warn",
                                details=(f"{client_id}: 交易所三分量未能落库 "
                                         f"{type(exc).__name__} {exc}"),
                                action_taken="quantity persist skipped")
                    resolved += 1
                else:
                    raise LookupError("交易所查询未返回订单标识")
            except Exception as exc:
                self.last_error = type(exc).__name__
                attempts = int(row["query_attempt_count"] or 0) + 1
                delay = min(1800, 30 * (2 ** min(attempts - 1, 6)))
                retry_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
                with self.db.lock:
                    self.db.conn.execute(
                        "UPDATE order_outbox SET query_attempt_count=?,last_query_at=?,"
                        "next_retry_at=?,last_query_error=?,updated_at=? "
                        "WHERE client_order_id=? AND status='UNKNOWN'",
                        (attempts, utc_now(), retry_at,
                         f"{type(exc).__name__}: {str(exc)[:200]}", utc_now(),
                         row["client_order_id"]),
                    )
                    self.db.conn.commit()
        self.reconciled_unknown += resolved
        self.last_reconcile_at = time.time()
        if not rows or resolved == len(rows):
            self.last_error = None
        return resolved

    def manual_resolve_unknown(self, client_order_id: str, *, status: str,
                               operator: str, evidence: str) -> None:
        """以人工核验依据收口长期 UNKNOWN；禁止把不确定订单标成 FILLED。"""
        terminal = str(status).upper()
        if terminal not in {"CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}:
            raise ValueError("人工收口只允许无成交类终态")
        if not str(operator).strip() or not str(evidence).strip():
            raise ValueError("人工收口必须记录操作者和交易所核验证据")
        with self.db.lock:
            row = self.db.conn.execute(
                "SELECT status FROM order_outbox WHERE client_order_id=? AND environment=?",
                (client_order_id, self.environment),
            ).fetchone()
            if not row or str(row["status"]) != "UNKNOWN":
                raise ValueError("只能收口当前环境中的 UNKNOWN 订单")
            self.db.conn.execute(
                "UPDATE order_outbox SET status=?,resolution_note=?,resolved_by=?,"
                "resolved_at=?,updated_at=?,next_retry_at=NULL WHERE client_order_id=?",
                (terminal, str(evidence).strip(), str(operator).strip(), utc_now(),
                 utc_now(), client_order_id),
            )
            self.db.conn.commit()

    def status(self) -> dict[str, Any]:
        """返回当前环境的状态计数和最近一次对账信息。"""
        counts = {str(row["status"]): int(row["count"]) for row in self.db.fetch_all(
            "SELECT status,COUNT(*) AS count FROM order_outbox "
            "WHERE environment=? GROUP BY status", (self.environment,))}
        oldest = self.db.fetch_one(
            "SELECT MIN(COALESCE(unknown_since,updated_at)) AS ts FROM order_outbox "
            "WHERE environment=? AND status='UNKNOWN'", (self.environment,))
        oldest_age = None
        if oldest and oldest["ts"]:
            try:
                created = datetime.fromisoformat(str(oldest["ts"]).replace("Z", "+00:00"))
                oldest_age = max(0.0, (datetime.now(timezone.utc) - created).total_seconds())
            except (TypeError, ValueError):
                oldest_age = None
        due = self.db.fetch_one(
            "SELECT COUNT(*) AS count FROM order_outbox WHERE environment=? "
            "AND status='UNKNOWN' AND (next_retry_at IS NULL OR next_retry_at<=?)",
            (self.environment, utc_now()))
        return {
            "enabled": True,
            "environment": self.environment,
            "last_event_at": self.last_event_at,
            "last_reconcile_at": self.last_reconcile_at,
            "last_error": self.last_error,
            "unknown_orders": int(counts.get("UNKNOWN", 0)),
            "oldest_unknown_age_seconds": oldest_age,
            "retry_due": int(due["count"] if due else 0),
            "submitting_orders": int(counts.get("SUBMITTING", 0)),
            "reconciled_unknown": self.reconciled_unknown,
            "status_counts": counts,
        }
