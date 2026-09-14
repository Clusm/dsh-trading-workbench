"""L01 离线回归：验证业务开仓范围不会污染存量管理范围。"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from agents.models import TradeIntent  # noqa: E402
from core.action_space import ActionSpace  # noqa: E402
from core.config_loader import load_settings  # noqa: E402
from core.trading_scope import scope_from_settings  # noqa: E402
from agents.execution import PaperBroker  # noqa: E402


def _open(symbol: str) -> TradeIntent:
    return TradeIntent(
        symbol=symbol, action="open", side="LONG", qty=1, leverage=2,
        stop_loss=90, take_profit=110, confidence=0.6,
    )


def main() -> None:
    settings = load_settings(ROOT)
    scope = scope_from_settings(settings.raw_settings)
    assert len(scope.symbols) == 20, scope.as_dict()
    assert scope.allows_open(scope.symbols[0])
    assert not scope.allows_open("BCHUSDT")
    assert scope.allowed_open_symbols([scope.symbols[0], "BCHUSDT"]) == [scope.symbols[0]]
    assert scope.candidate_symbols(["BCHUSDT", scope.symbols[1], scope.symbols[1]]) == [scope.symbols[1]]
    # 白名单外的历史仓位/挂单仍然属于管理集合，不能被开仓白名单删除。
    assert scope.management_symbols(
        positions=[{"symbol": "BCHUSDT"}],
        pending_orders=[{"symbol": scope.symbols[0]}],
    ) == ["BCHUSDT", scope.symbols[0]]

    config = {
        "supported_symbols": list(scope.symbols) + ["BCHUSDT"],
        "allowed_open_symbols": list(scope.symbols),
        "max_open_positions": 8,
    }
    space = ActionSpace.from_context(
        risk_config=config,
        portfolio={"positions": [{"symbol": "BCHUSDT", "side": "LONG"}]},
        tradeable_symbols=list(scope.symbols),
    )
    assert not space.check_intent(_open(scope.symbols[0]), {scope.symbols[0]: 100})
    assert any("业务开仓白名单" in item for item in space.check_intent(
        _open("BCHUSDT"), {"BCHUSDT": 100}))
    close = TradeIntent(symbol="BCHUSDT", action="close", side="LONG", qty=1)
    assert not space.check_intent(close, {"BCHUSDT": 100})

    class IncidentDB:
        def __init__(self) -> None:
            self.events: list[str] = []

        def log_risk_event(self, *, event_type: str, **_: object) -> None:
            self.events.append(event_type)

    # 直接绕过 risk.py 调用 PaperBroker 也不能扩大开仓范围。
    with tempfile.TemporaryDirectory() as temp_dir:
        broker = PaperBroker(Path(temp_dir) / "paper.json")
        broker.trading_scope = scope
        incident_db = IncidentDB()
        assert broker._open(_open("BCHUSDT"), 100, "ai", None, {}, incident_db) is None
        assert incident_db.events == ["paper_scope_open_blocked"]

    print(f"PASS trading scope: {len(scope.symbols)} symbols, hash={scope.scope_hash[:12]}")


if __name__ == "__main__":
    main()
