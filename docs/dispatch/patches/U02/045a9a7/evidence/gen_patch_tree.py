"""把 r2 的修复写进补丁生成树（`_scratch_B/r2/sb_gen`，基线 = 集成 HEAD）。

每个替换都用 ``assert`` 校验命中，避免"改了但没生效"的静默失败。
只用标准库，不联网、不读 .env/data。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "r2" / "sb_gen"


def patch(rel: str, old: str, new: str, *, count: int = 1) -> None:
    path = ROOT / rel
    text = path.read_text(encoding="utf-8")
    found = text.count(old)
    assert found == count, f"{rel}: 期望命中 {count} 次，实际 {found} 次\n---\n{old[:200]}"
    path.write_text(text.replace(old, new), encoding="utf-8")
    print(f"OK {rel}: {old.strip().splitlines()[0][:70]}")


# --------------------------------------------------------------------------- #
# 1) binance_futures.py
# --------------------------------------------------------------------------- #
BF = "src/exchange/binance_futures.py"

# 1a. 删除 patch 02 误插入的 `_open` 行（reservation_key 在 `_open` 内未定义）。
patch(BF, """        if not chunks:
            self._release_exit(reservation_key, reason="NO_CHUNKS")
            return None
""", """        if not chunks:
            return None
""")

# 1b. `_close_or_reduce` 主体缩进进 try/finally。
text = (ROOT / BF).read_text(encoding="utf-8")
head = """        if close_qty <= 0:
            return None
        entry = float(pos.get("entryPrice") or 0.0)
"""
assert text.count(head) == 1, "1b: 未找到 close_qty/entry 锚点"
new_head = """        if close_qty <= 0:
            self._release_exit(reservation_key, reason="NO_QTY_TO_CLOSE")
            return None
        # U02/A_r1 P0-1：占量成功之后的**所有**退出路径（含异常）都必须在 finally
        # 里结算或释放预留。`_submit_standard_order` 在"确定性拒绝"（HTTP 429/418、
        # reduceOnly 被拒、4xx）或 POST 异常后对账失败时会重新抛出，异常被逐 intent
        # 的 except Exception 兜住——若此处不释放，这份预留会把可用量永久占满，
        # 之后每一次退出都只得到误导性的"无仓可平"，该标的再也不能自动退出
        #（F03/R09 的并发 TP 拒绝场景）。
        settled = False
        try:
            entry = float(pos.get("entryPrice") or 0.0)
"""
text = text.replace(head, new_head, 1)

body_start = text.index('            entry = float(pos.get("entryPrice") or 0.0)\n') \
    + len('            entry = float(pos.get("entryPrice") or 0.0)\n')
tail_start = text.index("        self._resync_protection(symbol, intent=intent)\n",
                        body_start)
ret_tail = '"order_count": order_count, "fill_unconfirmed": close_unconfirmed}\n'
ret_end = text.index(ret_tail, tail_start) + len(ret_tail)
assert text.index("        return {\"order_id\": first_cid,", tail_start) < ret_end

def _indent(block: str) -> str:
    return "\n".join(("    " + line) if line.strip() else line
                     for line in block.split("\n"))

body = _indent(text[body_start:tail_start])
tail = text[tail_start:ret_end]
# 尾部注释按新语义重写（旧注释说"交给下一轮重新规划"，但旧代码用稳定键把下一轮
# 吃成了 duplicate_skipped —— 注释与行为相反，这里一并纠正）。
old_tail_comment = """        self._resync_protection(symbol, intent=intent)
        # U02：结算本轮预留。确认成交则按实际扣减，未确认（可能部分成交/响应丢失）
        # 则整笔释放，交给下一轮 UNKNOWN 收口重新规划——绝不让一笔在途退出长期
        # 占着仓量，也绝不重复扣减。
"""
assert tail.startswith(old_tail_comment), f"1b: 尾部注释不匹配:\n{tail[:400]}"
tail_rest = tail[len(old_tail_comment):]
new_tail = """            self._resync_protection(symbol, intent=intent)
            # U02：结算本轮预留。确认成交则按实际扣减；未确认（可能部分成交/
            # 响应丢失）则整笔释放，交给下一轮 UNKNOWN 收口重新规划——释放会让
            # 该 exit_key 恢复可重发，因此"重新规划"这一句话才真正成立。
""" + _indent(tail_rest)
new_tail = new_tail.replace(
    "            self._settle_exit(reservation_key)\n",
    "            self._settle_exit(reservation_key)\n            settled = True\n", 1)
assert "settled = True" in new_tail
finally_block = """        finally:
            if not settled:
                # 走不完的路径必须释放：占死可用量比"多一次可重发的退出"危险得多。
                try:
                    self._release_exit(reservation_key, reason="EXIT_PATH_ERROR")
                    if self.db:
                        self.db.log_risk_event(
                            event_type="futures_exit_reservation_released",
                            severity="warn",
                            details=(f"{symbol}: 退出路径未走完，已释放本次预留"
                                     "（下一轮可重发，不会占死该标的的可减量）"),
                            action_taken="release reservation")
                except Exception:  # noqa: BLE001 - 兜底回收在下一轮 sweep
                    pass
"""
text = text[:body_start] + body + new_tail + finally_block + text[ret_end:]
(ROOT / BF).write_text(text, encoding="utf-8")
print("OK binance_futures.py: _close_or_reduce -> try/finally")

# 1c. _reserve_exit：TTL + 巡检 + 审计事件。
patch(BF, """    def _reserve_exit(self, symbol: str, pos_side: str, intent: Any,
                      current: float) -> dict[str, Any] | None:
        \"\"\"经 U02 管理预留注册退出；未接入管理组件时保持既有行为。\"\"\"
        manager = getattr(self, "strategy_position_manager", None)
        if manager is None:
            return None
        domain = self._exit_domain_key(symbol, pos_side)
        exit_key = str(getattr(intent, "client_order_id", None)
                       or f"exit:{domain}:{uuid.uuid4().hex[:16]}")
        outcome = manager.request_exit(
            exit_key=exit_key, lifecycle_id=domain, symbol=symbol,
            position_side=pos_side, qty=float(intent.qty), current_qty=current)
""", """    #: 在途退出预留的超时（毫秒）。一次退出提交+等成交是秒级；超过这个时长仍未
    #: 结算的预留只可能是"结算与释放都没跑到"的泄漏，按泄漏回收（U02/A_r1 P0-1）。
    EXIT_RESERVATION_TTL_MS = 900_000

    def _sweep_stale_exit_reservations(self, manager: Any) -> tuple[str, ...]:
        \"\"\"回收超过 TTL 仍在途的退出预留（泄漏兜底），并留下审计事件。\"\"\"
        reclaim = getattr(manager, "reclaim_stale_exits", None)
        if reclaim is None:
            return ()
        try:
            reclaimed = tuple(reclaim(now_ms=int(time.time() * 1000),
                                      ttl_ms=self.EXIT_RESERVATION_TTL_MS))
        except Exception:  # noqa: BLE001 - 巡检失败不得影响本次退出
            return ()
        if reclaimed and self.db:
            self.db.log_risk_event(
                event_type="futures_stale_exit_reservation_reclaimed", severity="warn",
                details=(f"{len(reclaimed)} 笔退出预留超过 "
                         f"{self.EXIT_RESERVATION_TTL_MS}ms 未结算，已按泄漏回收："
                         "上一轮退出既未成交也未释放，现恢复可退出"),
                action_taken="reclaim stale reservation")
        return reclaimed

    def _reserve_exit(self, symbol: str, pos_side: str, intent: Any,
                      current: float) -> dict[str, Any] | None:
        \"\"\"经 U02 管理预留注册退出；未接入管理组件时保持既有行为。\"\"\"
        manager = getattr(self, "strategy_position_manager", None)
        if manager is None:
            return None
        # 先回收上一轮泄漏的在途预留：稳定 key（guardian 的 mta-g-<sha24>）会被
        # `_exit_keys` 记忆，不回收就等于该标的永久不可退出。
        self._sweep_stale_exit_reservations(manager)
        domain = self._exit_domain_key(symbol, pos_side)
        exit_key = str(getattr(intent, "client_order_id", None)
                       or f"exit:{domain}:{uuid.uuid4().hex[:16]}")
        outcome = manager.request_exit(
            exit_key=exit_key, lifecycle_id=domain, symbol=symbol,
            position_side=pos_side, qty=float(intent.qty), current_qty=current,
            now_ms=int(time.time() * 1000),
            in_flight_ttl_ms=self.EXIT_RESERVATION_TTL_MS)
""")

# --------------------------------------------------------------------------- #
# 2) agents/execution.py：纸面回退键区分退出轮次
# --------------------------------------------------------------------------- #
EX = "src/agents/execution.py"
patch(EX, """        intents = verdict.adjusted_intents if verdict.adjusted_intents else decision.intents
        # U02/DL03：管理动作（退出/减仓/调整）优先提交；纸面路径与实盘同序，
        # 避免离线验证与真实执行的提交次序不一致。
        from core.decision_pipeline import priority_order
        manager = getattr(self, "strategy_position_manager", None)
        for intent in priority_order(intents):""", """        intents = verdict.adjusted_intents if verdict.adjusted_intents else decision.intents
        # U02/DL03：管理动作（退出/减仓/调整）优先提交；纸面路径与实盘同序，
        # 避免离线验证与真实执行的提交次序不一致。
        from core.decision_pipeline import priority_order
        manager = getattr(self, "strategy_position_manager", None)
        # U02/A_r1 P1-3：纸面回退退出键必须带"本轮令牌"。用恒稳键会让同名标的的
        # 第二次退出（含"先部分平、再平剩余"）永久被幂等吃掉、不再执行。
        self._paper_exit_round = self._paper_exit_round_token()
        for intent in priority_order(intents):""")

patch(EX, """        symbol = str(intent.symbol)
        _, pos = existing if existing else ("", {})
        position_side = str(pos.get("side") or getattr(intent, "side", "") or "")
        exit_key = str(getattr(intent, "client_order_id", None)
                       or f"paper-exit:{symbol}:{position_side}")""", """        symbol = str(intent.symbol)
        _, pos = existing if existing else ("", {})
        position_side = str(pos.get("side") or getattr(intent, "side", "") or "")
        exit_key = str(getattr(intent, "client_order_id", None)
                       or self._paper_exit_fallback_key(intent, symbol, position_side))""")

patch(EX, """    def _margin_available(self, book: dict[str, Any], risk_config: dict[str, Any]) -> float:""", """    def _paper_exit_round_token(self) -> str:
        \"\"\"本轮执行令牌：每调用一次 ``execute()`` 递增一次。\"\"\"
        self._paper_exit_seq = int(getattr(self, "_paper_exit_seq", 0)) + 1
        return f"r{self._paper_exit_seq}"

    def _paper_exit_fallback_key(self, intent: Any, symbol: str,
                                 position_side: str) -> str:
        \"\"\"纸面退出回退键：**同一轮内同指纹去重，跨轮次各自独立**。

        恒稳的 ``paper-exit:<symbol>:<side>`` 会让同名标的的第二次退出永久被
        幂等吃掉（U02/A_r1 P1-3），因此键必须携带"本轮令牌 + 本次退出指纹"：
        同轮重复的同一笔退出仍被抑制，下一轮的退出（含部分平仓后平剩余）正常
        执行。显式 ``client_order_id`` 优先级不变。
        \"\"\"
        token = str(getattr(self, "_paper_exit_round", "") or "r0")
        explicit = str(getattr(intent, "signal_id", None)
                       or getattr(intent, "ticket_id", None)
                       or getattr(intent, "strategy_id", None) or "na")
        fingerprint = (f"{str(getattr(intent, 'action', '') or '')}:"
                       f"{float(getattr(intent, 'qty', 0) or 0):.12g}")
        return (f"paper-exit:{symbol}:{position_side}:{token}:"
                f"{explicit}:{fingerprint}")

    def _margin_available(self, book: dict[str, Any], risk_config: dict[str, Any]) -> float:""")

# --------------------------------------------------------------------------- #
# 3) decision_pipeline.py：enforce=True 必须 fail-closed（不能静默关掉全部退出）
# --------------------------------------------------------------------------- #
DP = "src/core/decision_pipeline.py"
patch(DP, """def filter_unbound_management(
    decision: Any, verdict: Any, db: Any, *,
    bound_authorization_ids: Any = (), enforce: bool = False,
) -> tuple[Any, Any, list[str]]:
    \"\"\"非 OPEN 意图必须携带**已绑定**的管理授权引用，否则 ``NEEDS_AUTH``。

    ``enforce=False``（默认）只做可观测：把未绑定的管理意图记入风险事件与返回
    值，但**不改变执行集合**——未接入 U02 授权服务的既有调用方行为完全不变。
    ``enforce=True`` 时把未绑定的管理意图从原始与风控调整集合中一并摘除，关键点
    是**摘除而不是降级**：被摘除的意图绝不会被重新解释成开仓（摘除后若已无任何
    意图，决策降级为 ``no_trade`` 而非 ``open``）。
    \"\"\"
    bound = {str(item) for item in (bound_authorization_ids or ())}
    raw = list(decision.intents)
    adjusted = list(verdict.adjusted_intents)
""", """#: 意图模型无法携带管理授权引用时，``enforce=True`` 一律拒绝启用。
ENFORCEMENT_UNSUPPORTED = "MANAGEMENT_ENFORCEMENT_UNSUPPORTED"


def authorization_ref_supported(intents: Any) -> bool:
    \"\"\"意图模型是否真的能携带管理授权引用（而不是被 pydantic 静默丢弃）。

    ``TradeIntent`` 目前**没有** ``management_authorization_id`` 字段，且没有
    ``model_config``（extra 默认 ``ignore``）——调用方即使传了也会被静默扔掉。
    因此 ``filter_unbound_management(enforce=True)`` 会把**所有**管理意图判为
    未绑定并全部摘除，等价于"翻一个布尔就关掉全部退出"（U02/A_r1 P2-6）。
    \"\"\"
    for item in (intents or ()):
        model_fields = getattr(type(item), "model_fields", None)
        if model_fields is not None and any(
                name in model_fields
                for name in ("management_authorization_id", "management_id")):
            return True
        if hasattr(item, "management_authorization_id") or hasattr(item, "management_id"):
            return True
    return False


def filter_unbound_management(
    decision: Any, verdict: Any, db: Any, *,
    bound_authorization_ids: Any = (), enforce: bool = False,
) -> tuple[Any, Any, list[str]]:
    \"\"\"非 OPEN 意图必须携带**已绑定**的管理授权引用，否则 ``NEEDS_AUTH``。

    ``enforce=False``（默认）只做可观测：把未绑定的管理意图记入风险事件与返回
    值，但**不改变执行集合**——未接入 U02 授权服务的既有调用方行为完全不变。
    ``enforce=True`` 时把未绑定的管理意图从原始与风控调整集合中一并摘除，关键点
    是**摘除而不是降级**：被摘除的意图绝不会被重新解释成开仓（摘除后若已无任何
    意图，决策降级为 ``no_trade`` 而非 ``open``）。

    **fail-closed 前置条件**：意图模型必须真的能携带授权引用
    （:func:`authorization_ref_supported`）。否则 ``enforce=True`` 会把全部退出
    静默摘掉——那是"一开就全停"的雷，而不是安全开关，因此在摘除任何意图之前
    直接抛 :class:`ManagementEnforcementError`。

    本函数当前**没有生产调用方**（属 U02 的接入面，交由后续任务接线）：默认
    ``enforce=False`` 不改变任何既有行为。
    \"\"\"
    bound = {str(item) for item in (bound_authorization_ids or ())}
    raw = list(decision.intents)
    adjusted = list(verdict.adjusted_intents)
    if enforce and not authorization_ref_supported(raw + adjusted):
        raise ManagementEnforcementError([ENFORCEMENT_UNSUPPORTED])
""")

patch(DP, """#: 非 OPEN 意图缺少已绑定管理授权引用时的原因码（K04 §6）。
NEEDS_AUTH = "NEEDS_AUTH\"""", """#: 非 OPEN 意图缺少已绑定管理授权引用时的原因码（K04 §6）。
NEEDS_AUTH = "NEEDS_AUTH\"


class ManagementEnforcementError(RuntimeError):
    \"\"\"``enforce=True`` 在没有可绑定字段的意图模型上被启用（fail-closed）。\"\"\"

    def __init__(self, reason_codes: Any) -> None:
        self.reason_codes = tuple(str(item) for item in reason_codes)
        super().__init__("; ".join(self.reason_codes))
""")

# --------------------------------------------------------------------------- #
# 4) dashboard/app.py：新增事件类型的中文映射（伴随修改）
# --------------------------------------------------------------------------- #
patch("src/dashboard/app.py", """        "futures_duplicate_exit_skipped": "重复退出已幂等忽略",""",
      """        "futures_duplicate_exit_skipped": "重复退出已幂等忽略",
        "futures_stale_exit_reservation_reclaimed": "超时退出预留已回收",
        "futures_exit_reservation_released": "退出预留已释放（可重发）",""")

print("\nGEN OK")
sys.exit(0)
