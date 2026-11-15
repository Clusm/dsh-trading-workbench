"""硬风控包（R00–R25 + 审批令牌 + 控制态）。"""

from __future__ import annotations

from .approval import ApprovalError, ApprovalManager
from .config import RULE_SPECS, RiskLimits, rule_table
from .models import (
    OrderPlan,
    PlanValidationError,
    PositionView,
    RiskContext,
    RiskVerdict,
    RuleViolation,
    SymbolFiltersView,
)
from .rules import BLOCKING_CODES, evaluate
from .service import RiskService
from .state import CONTROL_STATES, ControlSnapshot, RiskStateError, RiskStateStore

__all__ = [
    "RiskLimits",
    "RULE_SPECS",
    "rule_table",
    "OrderPlan",
    "PlanValidationError",
    "PositionView",
    "RiskContext",
    "RiskVerdict",
    "RuleViolation",
    "SymbolFiltersView",
    "BLOCKING_CODES",
    "evaluate",
    "RiskService",
    "ApprovalManager",
    "ApprovalError",
    "RiskStateStore",
    "RiskStateError",
    "ControlSnapshot",
    "CONTROL_STATES",
]
