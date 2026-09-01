"""执行 Broker 工厂：paper / demo / testnet / live。"""
from __future__ import annotations

import os

from agents.execution import PaperBroker
from audit.database import AuditDatabase
from core.config_loader import Settings
from exchange.binance_futures import BinanceFuturesBroker

BASE_URLS = {
    "demo": "https://demo-fapi.binance.com",
    "testnet": "https://testnet.binancefuture.com",
    "live": "https://fapi.binance.com",
}


def create_broker(settings: Settings, db: AuditDatabase):
    cfg = settings.raw_settings.get("execution", {})
    mode = os.getenv(cfg.get("mode_env", "EXECUTION_MODE"), "").strip().lower() or cfg.get("default_mode", "demo")
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
            )
            db.log_risk_event(event_type="broker_created", severity="info",
                              details=f"execution broker={mode} base_url={broker.base_url}",
                              action_taken="connected")
            return broker
        db.log_risk_event(event_type="broker_fallback_paper", severity="warn",
                          details=f"EXECUTION_MODE={mode} 但缺少 {key_env}/{secret_env}，回退纸面执行",
                          action_taken="paper")
    else:
        db.log_risk_event(event_type="broker_fallback_paper", severity="warn",
                          details=f"未知 EXECUTION_MODE={mode}，回退纸面执行", action_taken="paper")
    return PaperBroker(settings.project_root / "data/paper_portfolio.json")
