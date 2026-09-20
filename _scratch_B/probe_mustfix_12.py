"""MUST FIX 1/2 修复前后对照（对同一组输入跑旧/新模块）。"""
from __future__ import annotations
import sys
from pathlib import Path
SRC = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(SRC))
from exchange.protection_commands import (  # noqa: E402
    ProtectionSafetyError, can_cancel_protection, guard_fault_action,
    plan_protection_refresh, protection_view_from_algo, real_position_from_snapshot,
)
print(f"=== module src: {SRC} ===")

# ---- MUST FIX 1 ----
one_sl = [protection_view_from_algo({
    "clientAlgoId": "SL_ONLY", "algoId": "1", "orderType": "STOP_MARKET",
    "closePosition": True, "quantity": None, "triggerPrice": "95.00"})]
for label, snap in (("positionAmt='0.0'", {"symbol": "Z", "positionAmt": "0.0"}),
                    ("positionAmt='0.00000000'", {"symbol": "Z", "positionAmt": "0.00000000"}),
                    ("positionAmt='2.0'（有仓）", {"symbol": "Z", "positionAmt": "2.0"})):
    pos = real_position_from_snapshot(snap)
    allowed, reason = can_cancel_protection(
        position=pos, protections=one_sl, target_client_id="SL_ONLY")
    print(f"[M1] {label:28s} flat_confirmed=False 仅剩一张保护 -> "
          f"({allowed}, {reason!r})")
plan = plan_protection_refresh(position=real_position_from_snapshot(
    {"symbol": "Z", "positionAmt": "0.0"}), protections=one_sl, flat_confirmed=False)
print(f"[M1] 同情形 plan_protection_refresh -> blocked={plan.blocked_reason!r} "
      f"cancels={len(plan.cancels)}  # 旧模块两处口径互相矛盾")

# ---- MUST FIX 2 ----
for fault in ("HEALTH_DEGRADED", "WS_DISCONNECT", "QUERY_FAILED", "RATE_LIMITED",
              "PROTECTION_MIRROR_STALE", "ALGO_ORDER_QUERY_FAILED",
              "ProtectionOrderHealthCheck", "SOMETHING_NEW",
              "HTTP_503", "TIMEOUT", "INSUFFICIENT_MARGIN", "PROTECTION_FAILED"):
    try:
        p = guard_fault_action(fault, "CLOSE_ALL")
        print(f"[M2] guard_fault_action({fault!r}, 'CLOSE_ALL') -> ALLOWED "
              f"class={p.fault_class.value} triggers_flat_close={p.triggers_flat_close}")
    except ProtectionSafetyError as exc:
        print(f"[M2] guard_fault_action({fault!r}, 'CLOSE_ALL') -> BLOCKED ({exc})")
