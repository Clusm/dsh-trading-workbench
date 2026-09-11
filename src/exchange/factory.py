"""执行 Broker 工厂：paper / demo / testnet / live。"""
from __future__ import annotations

import os
import secrets

from agents.execution import PaperBroker
from audit.database import AuditDatabase
from core.config_loader import Settings
from exchange.binance_futures import BinanceFuturesBroker

BASE_URLS = {
    "demo": "https://demo-fapi.binance.com",
    "testnet": "https://testnet.binancefuture.com",
    "live": "https://fapi.binance.com",
}

# 合法执行模式。未知值一律拒绝启动，不再静默降级为 paper——
# 历史故障（2026-09-05）：某个进程读到 EXECUTION_MODE=paper，静默回退纸面
# 执行后触发手动重置，paper 分支把资金基线写成 0 并落盘，导致 demo 账户的
# 会话权益从此等于交易所全部余额（5028U 而非 532U）。纸面与真实账户混跑
# 而不自知，比启动失败危险得多。
VALID_MODES = ("paper", "demo", "testnet", "live")


def _paper_broker(settings: Settings):
    cap_cfg = settings.raw_settings.get("capital", {}) or {}
    broker = PaperBroker(
        settings.project_root / "data/paper_portfolio.json",
        initial_equity=float(cap_cfg.get("initial_equity", 200)),
    )
    broker.require_pipeline_authorization = True
    broker._pipeline_secret = secrets.token_urlsafe(32)
    return broker


def create_broker(settings: Settings, db: AuditDatabase):
    cfg = settings.raw_settings.get("execution", {})
    raw_mode = os.getenv(cfg.get("mode_env", "EXECUTION_MODE"), "").strip().lower()
    mode = raw_mode or str(cfg.get("default_mode", "demo")).strip().lower()
    if mode not in VALID_MODES:
        db.log_risk_event(event_type="broker_unknown_mode", severity="critical",
                          details=f"未知 EXECUTION_MODE={raw_mode or cfg.get('default_mode')} "
                                  f"（合法值：{'/'.join(VALID_MODES)}），拒绝启动",
                          action_taken="refuse to start")
        raise RuntimeError(
            f"未知 EXECUTION_MODE={raw_mode or cfg.get('default_mode')!r}；"
            f"合法值为 {'/'.join(VALID_MODES)}。已拒绝启动，避免以纸面模式误跑真实策略。")
    if mode == "paper":
        db.log_risk_event(event_type="broker_created", severity="info",
                          details="execution broker=paper（显式声明）", action_taken="paper")
        return _paper_broker(settings)
    if mode in ("demo", "testnet", "live"):
        key_env = cfg.get("api_key_env_map", {}).get(mode) or f"BINANCE_{mode.upper()}_API_KEY"
        secret_env = cfg.get("api_secret_env_map", {}).get(mode) or f"BINANCE_{mode.upper()}_API_SECRET"
        api_key = os.getenv(key_env, "").strip()
        secret = os.getenv(secret_env, "").strip()
        if api_key and secret:
            broker = BinanceFuturesBroker(
                api_key=api_key,
                api_secret=secret,
                base_url=cfg.get("base_url_map", BASE_URLS).get(mode, BASE_URLS[mode]),
                db=db,
                environment=mode,
                protection_mirror_enabled=bool(
                    (settings.raw_settings.get("protection_order_mirror", {}) or {}).get(
                        "enabled", False)
                    and (settings.raw_settings.get("protection_order_mirror", {}) or {}).get(
                        "read_enabled", False)),
            )
            broker.require_pipeline_authorization = True
            broker._pipeline_secret = secrets.token_urlsafe(32)
            db.log_risk_event(event_type="broker_created", severity="info",
                              details=f"execution broker={mode} base_url={broker.base_url}",
                              action_taken="connected")
            return broker
        if bool(cfg.get("allow_paper_fallback", False)):
            db.log_risk_event(event_type="broker_fallback_paper", severity="warn",
                              details=f"EXECUTION_MODE={mode} 但缺少 {key_env}/{secret_env}；"
                                      "配置显式允许回退纸面执行",
                              action_taken="paper")
            return _paper_broker(settings)
        db.log_risk_event(event_type="broker_credentials_missing", severity="critical",
                          details=f"EXECUTION_MODE={mode} 但缺少 {key_env}/{secret_env}，拒绝启动",
                          action_taken="refuse to start")
        raise RuntimeError(
            f"EXECUTION_MODE={mode} 缺少 {key_env}/{secret_env}；已拒绝静默回退 paper。")
    raise RuntimeError(f"无法创建 execution broker: {mode}")
