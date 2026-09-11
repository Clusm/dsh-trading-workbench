"""AI 决策质量的确定性评价器。

本模块与 ``RiskGuard`` 刻意分离：前者回答“交易论点是否自洽”，后者继续回答
“账户是否承受得起”。初始配置全部为 shadow，因此这里产出的 BLOCK 只表示
“如果晋级为 enforce，本应阻断”，不会改变现有风险档或交易执行。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "decision_policy.yaml"
VALID_MODES = {"off", "shadow", "enforce"}


def load_decision_policy(path: Path | None = None) -> dict[str, Any]:
    """读取独立策略；非法 mode 直接失败，避免拼写错误静默放行。"""
    source = path or DEFAULT_CONFIG_PATH
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    checks = raw.get("checks") or {}
    if not isinstance(checks, dict):
        raise ValueError("decision_policy.checks must be an object")
    for check_id, config in checks.items():
        mode = str((config or {}).get("mode") or "off")
        if mode not in VALID_MODES:
            raise ValueError(f"decision_policy {check_id}.mode={mode!r} is invalid")
    guardian = raw.get("position_guardian") or {}
    guardian_mode = str(guardian.get("mode") or "off")
    if guardian_mode not in VALID_MODES:
        raise ValueError(f"decision_policy position_guardian.mode={guardian_mode!r} is invalid")
    return raw


def _result(check_id: str, mode: str, status: str, reason: str,
            evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "version": 1,
        "mode": mode,
        "status": status,
        "reason": reason,
        "evidence": evidence or {},
    }


def _check_mode(config: dict[str, Any], check_id: str) -> tuple[str, dict[str, Any]]:
    item = ((config.get("checks") or {}).get(check_id) or {})
    return str(item.get("mode") or "off"), item


def evaluate_open_intent(intent: Any, config: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """评价单个 AI open intent；只依赖结构化字段，结果可稳定回放。"""
    if getattr(intent, "action", None) != "open":
        return []
    cfg = config or load_decision_policy()
    ev = getattr(intent, "evidence", None)
    if ev is None:
        return [_result("evidence_contract", "enforce", "BLOCK", "缺少结构化开仓证据")]

    side = str(getattr(intent, "side", "") or "").upper()
    higher_trend = str(getattr(ev, "higher_timeframe_trend", "unknown") or "unknown")
    regime = str(getattr(ev, "market_regime", "unknown") or "unknown")
    results: list[dict[str, Any]] = []

    mode, _ = _check_mode(cfg, "trend_alignment")
    if mode != "off":
        conflicts = ((side == "LONG" and (higher_trend == "down" or regime == "trend_down"))
                     or (side == "SHORT" and (higher_trend == "up" or regime == "trend_up")))
        unknown = higher_trend == "unknown" or regime == "unknown"
        status = "BLOCK" if conflicts else ("WARN" if unknown else "PASS")
        reason = ("方向与高周期/市场状态冲突" if conflicts else
                  "趋势或市场状态未知" if unknown else "方向与市场状态一致")
        results.append(_result("trend_alignment", mode, status, reason, {
            "side": side, "higher_timeframe_trend": higher_trend, "market_regime": regime,
        }))

    mode, rr_cfg = _check_mode(cfg, "risk_reward")
    if mode != "off":
        reported = float(getattr(ev, "risk_reward_ratio", 0.0) or 0.0)
        trigger = float(getattr(ev, "trigger_price", 0.0) or 0.0)
        stop = float(getattr(intent, "stop_loss", 0.0) or 0.0)
        target = float(getattr(intent, "take_profit", 0.0) or 0.0)
        risk = abs(trigger - stop)
        actual = abs(target - trigger) / risk if risk > 0 else 0.0
        minimum = float(rr_cfg.get("minimum") or 0.0)
        tolerance = float(rr_cfg.get("report_tolerance") or 0.0)
        mismatch = abs(actual - reported) > max(tolerance, actual * tolerance)
        status = "BLOCK" if actual < minimum or mismatch else "PASS"
        if actual < minimum:
            reason = f"实际风险收益比 {actual:.3f} 低于决策质量门槛 {minimum:.3f}"
        elif mismatch:
            reason = f"模型报告 RR={reported:.3f} 与价格计算 RR={actual:.3f} 不一致"
        else:
            reason = f"实际风险收益比 {actual:.3f} 通过"
        results.append(_result("risk_reward", mode, status, reason, {
            "reported": reported, "actual": round(actual, 6), "minimum": minimum,
            "trigger_price": trigger, "stop_loss": stop, "take_profit": target,
        }))

    mode, chase_cfg = _check_mode(cfg, "entry_chase")
    if mode != "off":
        chase = float(getattr(ev, "chase_distance_pct", 0.0) or 0.0)
        maximum = float(chase_cfg.get("maximum_pct") or 0.0)
        status = "BLOCK" if chase > maximum else "PASS"
        reason = (f"追价距离 {chase:.3f}% 超过 {maximum:.3f}%" if status == "BLOCK"
                  else f"追价距离 {chase:.3f}% 通过")
        results.append(_result("entry_chase", mode, status, reason, {
            "chase_distance_pct": chase, "maximum_pct": maximum,
        }))

    mode, _ = _check_mode(cfg, "evidence_conflict")
    if mode != "off":
        support = [str(item) for item in (getattr(ev, "supporting_evidence", None) or [])]
        counter = [str(item) for item in (getattr(ev, "counter_evidence", None) or [])]
        status = "BLOCK" if counter and len(counter) >= len(support) else ("WARN" if counter else "PASS")
        reason = ("反证数量不少于支持证据" if status == "BLOCK" else
                  "存在反证，需要在解释中处理" if status == "WARN" else "未报告直接反证")
        results.append(_result("evidence_conflict", mode, status, reason, {
            "support_count": len(support), "counter_count": len(counter),
        }))

    mode, _ = _check_mode(cfg, "invalidation_consistency")
    if mode != "off":
        invalidation = float(getattr(ev, "invalidation_price", 0.0) or 0.0)
        operator = str(getattr(ev, "invalidation_operator", "") or "")
        valid = ((side == "LONG" and operator == "close_below" and invalidation < trigger)
                 or (side == "SHORT" and operator == "close_above" and invalidation > trigger))
        stop_same_side = ((side == "LONG" and stop <= trigger)
                          or (side == "SHORT" and stop >= trigger))
        status = "PASS" if valid and stop_same_side else "BLOCK"
        reason = "结构无效点与方向一致" if status == "PASS" else "结构无效点/止损方向不一致"
        results.append(_result("invalidation_consistency", mode, status, reason, {
            "side": side, "trigger_price": trigger, "invalidation_price": invalidation,
            "operator": operator, "stop_loss": stop,
        }))
    return results


def enforced_issues(results: list[dict[str, Any]]) -> list[str]:
    """只把已明确晋级为 enforce 的 BLOCK 转成执行问题。"""
    return [
        f"decision_policy.{item['check_id']}: {item['reason']}"
        for item in results
        if item.get("mode") == "enforce" and item.get("status") == "BLOCK"
    ]
