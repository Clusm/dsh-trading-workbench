"""r3 共享适配增量：写进 `_scratch_B/r3/sb_gen`（= r2 生成态的拷贝）。

只用标准库，不联网、不读 .env/data。每个替换都用 ``assert`` 校验命中。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "r3" / "sb_gen"


def patch(rel: str, old: str, new: str, *, count: int = 1) -> None:
    path = ROOT / rel
    text = path.read_text(encoding="utf-8")
    found = text.count(old)
    assert found == count, f"{rel}: 期望命中 {count} 次，实际 {found} 次\n---\n{old[:200]}"
    path.write_text(text.replace(old, new), encoding="utf-8")
    print(f"OK {rel}: {old.strip().splitlines()[0][:70]}")


# --------------------------------------------------------------------------- #
# 1) agents/execution.py：③ 纸面退出的"同轮抑制"必须与声明一致
#    r2 的 release_exit 会**撤销** exit_key，于是同轮同指纹的第二次退出会再次
#    提交（README r2-3 声称"保持抑制"与事实相反）。纸面成交是同步完成的，改用
#    confirm_exit 结算：同一轮内同键仍判重复，跨轮次的键带轮次令牌、照常执行。
# --------------------------------------------------------------------------- #
EX = "src/agents/execution.py"
patch(EX, """        返回非 None 表示本次退出**不执行**：重复退出返回 ``duplicate_skipped``，
        无可减量返回 ``exit_noop``——两者都绝不会被降级成反向开仓（退出方向由
        持仓侧决定，请求量被可减量夹逼）。纸面成交是同步完成的，因此注册后即
        释放预留，把仓量让给后续真正的退出。
        \"\"\"""", """        返回非 None 表示本次退出**不执行**：重复退出返回 ``duplicate_skipped``，
        无可减量返回 ``exit_noop``——两者都绝不会被降级成反向开仓（退出方向由
        持仓侧决定，请求量被可减量夹逼）。

        纸面成交是同步完成的，因此注册后立刻**结算**这份预留
        （``confirm_exit``，而不是 ``release_exit``）：

        * ``release_exit`` 会把该 ``exit_key`` **撤销**，于是**同一轮内**同一笔
          退出再次到达时会重新占量并再次提交（"同轮抑制"名存实亡，U02/A_r2
          NEW-3）；
        * ``confirm_exit`` 把它标为 ``CONSUMED``：同轮内同键请求仍判
          ``duplicate_skipped``，而**跨轮次**用的是带轮次令牌的新键，照常执行
          （A_r1 P1-3 仍成立）。
        * 可用量同样立刻恢复（``CONSUMED`` 不计入在途预留），下一轮由
          ``reconcile_position`` 以权威仓量校正。
        \"\"\"""")

patch(EX, """        manager.release_exit(exit_key, reason="paper_immediate_fill")
        return None""", """        manager.confirm_exit(exit_key, engaged_qty=outcome.submit_qty)
        return None""")

patch(EX, """        \"\"\"纸面退出回退键：**同一轮内同指纹去重，跨轮次各自独立**。

        恒稳的 ``paper-exit:<symbol>:<side>`` 会让同名标的的第二次退出永久被
        幂等吃掉（U02/A_r1 P1-3），因此键必须携带"本轮令牌 + 本次退出指纹"：
        同轮重复的同一笔退出仍被抑制，下一轮的退出（含部分平仓后平剩余）正常
        执行。显式 ``client_order_id`` 优先级不变。
        \"\"\"""", """        \"\"\"纸面退出回退键：**同一轮内同指纹去重，跨轮次各自独立**。

        恒稳的 ``paper-exit:<symbol>:<side>`` 会让同名标的的第二次退出永久被
        幂等吃掉（U02/A_r1 P1-3），因此键必须携带"本轮令牌 + 本次退出指纹"：
        同轮重复的同一笔退出仍被抑制（守卫用 ``confirm_exit`` 结算而不是
        ``release_exit``，见 :meth:`_exit_idempotency_guard`），下一轮的退出
        （含部分平仓后平剩余）正常执行。显式 ``client_order_id`` 优先级不变。
        \"\"\"""")


# --------------------------------------------------------------------------- #
# 2) exchange/binance_futures.py：⑤ 巡检失败必须有运维可见性（A_r2 NEW-5）
# --------------------------------------------------------------------------- #
BF = "src/exchange/binance_futures.py"
patch(BF, """        try:
            reclaimed = tuple(reclaim(now_ms=int(time.time() * 1000),
                                      ttl_ms=self.EXIT_RESERVATION_TTL_MS))
        except Exception:  # noqa: BLE001 - 巡检失败不得影响本次退出
            return ()""", """        try:
            reclaimed = tuple(reclaim(now_ms=int(time.time() * 1000),
                                      ttl_ms=self.EXIT_RESERVATION_TTL_MS))
        except Exception as exc:  # noqa: BLE001 - 巡检失败不得影响本次退出
            # 巡检失败不能静默：本轮不再回收泄漏预留，未过期的同键退出会退化为
            # `duplicate_skipped`（fail-closed，不放行）。"巡检长期失败 -> 该标的
            # 不可退出"这条链必须能被运维看到（U02/A_r2 NEW-5）。
            if self.db:
                try:
                    self.db.log_risk_event(
                        event_type="futures_exit_reservation_sweep_failed",
                        severity="warn",
                        details=(f"退出预留巡检失败（{type(exc).__name__}）：本轮不做"
                                 "泄漏回收，未过期的同键退出将退化为重复退出"),
                        action_taken="skip sweep")
                except Exception:  # noqa: BLE001 - 审计失败不得影响本次退出
                    pass
            return ()""")


# --------------------------------------------------------------------------- #
# 3) dashboard/app.py：新增事件类型的中文映射（伴随修改）
# --------------------------------------------------------------------------- #
patch("src/dashboard/app.py",
      '''        "futures_exit_reservation_released": "退出预留已释放（可重发）",''',
      '''        "futures_exit_reservation_released": "退出预留已释放（可重发）",
        "futures_exit_reservation_sweep_failed": "退出预留巡检失败",''')

print("\nGEN R3 OK")
sys.exit(0)
