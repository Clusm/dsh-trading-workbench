"""U15 验收：控制面边界（回环 / 验证写入 / operator-health-risk 分离）。

独立可运行（无 pytest）：

    PYTHONUTF8=1 <venv>/python.exe tests/acceptance/test_control_boundary.py

只验证纯策略函数（不启动看板、不发请求、不写运行目录）。逐项打印
PASS/FAIL，任一失败以退出码 1 结束。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from core.runtime_identity import (  # noqa: E402
    AUTO_ACTOR,
    CONTROL_ACTION_ROLES,
    CONTROL_ROLES,
    CONTROL_STATE_SEVERITY,
    authorize_control_request,
    is_loopback_host,
    verify_control_write,
)

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        print(f"PASS {name}")
    else:
        FAILURES.append(name)
        print(f"FAIL {name} :: {detail}")


def decide(**kwargs):
    base = {"role": "operator", "action": "set_control", "client_host": "127.0.0.1"}
    base.update(kwargs)
    return authorize_control_request(**base)


# --------------------------------------------------------------------------
# 回环
# --------------------------------------------------------------------------


def check_loopback() -> None:
    for host in ("127.0.0.1", "::1", "localhost", "[::1]", "::ffff:127.0.0.1", "LOCALHOST"):
        check(f"loopback-accepts-{host}", is_loopback_host(host))
    for host in ("0.0.0.0", "10.1.2.3", "192.168.1.5", "", None, "127.0.0.1.evil.com",
                 "localhost.evil.com", "example.com", "::", "127.0.0.2"):
        check(f"non-loopback-rejects-{host!r}", not is_loopback_host(host))
    # 非环回来源即便角色合法也必须 403
    decision = decide(role="operator", action="emergency_stop",
                      client_host="203.0.113.9", requested_state="emergency",
                      confirm_secret="emergency")
    check("non-loopback-denied", not decision.allowed
          and decision.reason_codes == ("NON_LOOPBACK_REJECTED",)
          and decision.http_status == 403)
    check("loopback-allows-operator", decide(action="set_control", requested_state="paused",
                                             confirm_manual_reenable=True).allowed)


# --------------------------------------------------------------------------
# R11：operator / health / risk 权限分离
# --------------------------------------------------------------------------


def check_role_separation() -> None:
    check("three-roles-defined", CONTROL_ROLES == ("operator", "health", "risk"))
    check("auto-is-not-a-role", AUTO_ACTOR not in CONTROL_ROLES)
    # health 只读：不得下发控制
    blocked = decide(role="health", action="set_control", requested_state="paused")
    check("health-cannot-set-control", not blocked.allowed
          and blocked.reason_codes == ("ROLE_NOT_PERMITTED",) and blocked.http_status == 403)
    check("health-can-read", decide(role="health", action="read_status").allowed)
    check("health-can-acknowledge",
          decide(role="health", action="acknowledge_incident").allowed)
    check("health-cannot-resolve", not decide(
        role="health", action="resolve_incident").allowed)
    # risk 可紧急停机（更严方向），不得放宽
    check("risk-can-emergency-stop", decide(role="risk", action="emergency_stop",
                                           requested_state="emergency",
                                           confirm_secret="emergency").allowed)
    check("risk-can-tighten", decide(role="risk", action="set_control_severity_up",
                                     requested_state="paused", current_state="normal").allowed)
    blocked_risk = decide(role="risk", action="set_control_severity_down",
                          requested_state="normal", current_state="paused")
    check("risk-cannot-loosen", not blocked_risk.allowed
          and blocked_risk.reason_codes == ("ROLE_NOT_PERMITTED",))
    # operator 放宽需要显式二次确认
    unconfirmed = decide(role="operator", action="set_control_severity_down",
                         requested_state="normal", current_state="paused")
    check("operator-loosen-needs-confirmation", not unconfirmed.allowed
          and "MANUAL_REENABLE_NOT_CONFIRMED" in unconfirmed.reason_codes
          and unconfirmed.http_status == 409)
    confirmed = decide(role="operator", action="set_control_severity_down",
                       requested_state="normal", current_state="paused",
                       confirm_manual_reenable=True)
    check("operator-loosen-with-confirmation", confirmed.allowed)
    # 非环回 + 未授权角色：先判环回（不给角色探测留后门）
    remote_health = decide(role="health", action="set_control", requested_state="paused",
                           client_host="10.0.0.7")
    check("loopback-checked-before-role", remote_health.reason_codes == ("NON_LOOPBACK_REJECTED",))
    # 未知角色 / 未知动作 / 未知状态
    check("unknown-role-denied", decide(role="root", action="set_control").reason_codes
          == ("UNKNOWN_ROLE",))
    check("unknown-action-rejected", decide(action="drop_tables").reason_codes
          == ("UNKNOWN_CONTROL_ACTION",))
    check("unknown-state-rejected", decide(action="set_control",
                                           requested_state="maintenance").reason_codes
          == ("UNKNOWN_CONTROL_STATE",))
    check("missing-state-rejected", decide(action="set_control").reason_codes
          == ("CONTROL_STATE_REQUIRED",))


# --------------------------------------------------------------------------
# 人工禁用 live 优先于自动恢复
# --------------------------------------------------------------------------


def check_manual_disable_beats_auto_recovery() -> None:
    check("severity-ordering-fail-closed",
          CONTROL_STATE_SEVERITY["normal"] < CONTROL_STATE_SEVERITY["close_only"]
          < CONTROL_STATE_SEVERITY["paused"] < CONTROL_STATE_SEVERITY["emergency"])
    for current in ("close_only", "paused", "emergency"):
        auto = decide(role=AUTO_ACTOR, action="set_control", requested_state="normal",
                      current_state=current)
        check(f"auto-cannot-recover-{current}", not auto.allowed
              and auto.reason_codes == ("AUTO_RECOVERY_BLOCKED_BY_MANUAL_DISABLE",)
              and auto.http_status == 409)
    # operator 从 emergency 退出：必须同时给 confirm 与 emergency 二次确认
    partial = decide(role="operator", action="set_control", requested_state="normal",
                     current_state="emergency", confirm_manual_reenable=True)
    check("emergency-exit-needs-second-confirmation", not partial.allowed
          and "EMERGENCY_EXIT_NOT_CONFIRMED" in partial.reason_codes)
    full = decide(role="operator", action="set_control", requested_state="normal",
                  current_state="emergency", confirm_manual_reenable=True,
                  confirm_secret="emergency")
    check("emergency-exit-with-both-confirmations", full.allowed)
    # 进入 emergency 必须带二次确认串（沿用既有看板语义）
    missing_confirm = decide(role="operator", action="emergency_stop",
                             requested_state="emergency", confirm_manual_reenable=True)
    check("emergency-entry-needs-confirmation", not missing_confirm.allowed
          and "EMERGENCY_CONFIRM_REQUIRED" in missing_confirm.reason_codes)
    # 更严方向不需要 confirm
    tighten = decide(role="operator", action="set_control_severity_up",
                     requested_state="paused", current_state="normal")
    check("tightening-needs-no-confirmation", tighten.allowed)


# --------------------------------------------------------------------------
# 验证写入
# --------------------------------------------------------------------------


def check_verified_write() -> None:
    ok = verify_control_write(requested_state="paused", read_back_state="paused")
    check("write-verified-when-readback-matches", ok.ok and ok.reason_codes == ())
    mismatch = verify_control_write(requested_state="paused", read_back_state="normal")
    check("write-unverified-on-mismatch", not mismatch.ok
          and mismatch.reason_codes == ("CONTROL_WRITE_UNVERIFIED",))
    missing = verify_control_write(requested_state="paused", read_back_state=None)
    check("write-unverified-without-readback", not missing.ok
          and missing.reason_codes == ("CONTROL_READBACK_MISSING",))
    failed = verify_control_write(requested_state="paused", read_back_state="paused",
                                 read_back_ok=False)
    check("write-unverified-on-readback-error", not failed.ok
          and failed.reason_codes == ("CONTROL_READBACK_FAILED",))
    check("write-readback-case-sensitive-by-state",
          verify_control_write(requested_state="Paused", read_back_state="paused").ok is False)


def check_action_registry_shape() -> None:
    check("no-role-can-do-unspecified",
          all(role not in CONTROL_ACTION_ROLES["unspecified"] for role in CONTROL_ROLES))
    check("every-action-has-role-set",
          all(isinstance(v, tuple) and v for k, v in CONTROL_ACTION_ROLES.items() if k != "unspecified"))
    check("read-allowed-for-all-roles",
          all(role in CONTROL_ACTION_ROLES["read_status"] for role in CONTROL_ROLES))


def main() -> int:
    check_loopback()
    check_role_separation()
    check_manual_disable_beats_auto_recovery()
    check_verified_write()
    check_action_registry_shape()
    if FAILURES:
        print(f"\nU15 CONTROL BOUNDARY ACCEPTANCE FAILED: {len(FAILURES)} -> "
              + ", ".join(FAILURES))
        return 1
    print("\nU15 CONTROL BOUNDARY ACCEPTANCE PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
