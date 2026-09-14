"""阶段 0 验证脚本：不依赖任何 key，跑通一轮 mock 多 Agent 决策并全量落库。"""
from __future__ import annotations

import json
import sys
import uuid
import warnings
from pathlib import Path

from langchain_core._api.deprecation import LangChainPendingDeprecationWarning  # noqa: E402
warnings.filterwarnings("ignore", category=LangChainPendingDeprecationWarning)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from audit.jsonl_logger import JsonlLogger  # noqa: E402
from core.agent_recorder import AgentRecorder  # noqa: E402
from core.config_loader import load_settings  # noqa: E402
from core.graph import build_graph  # noqa: E402
from core.trading_scope import scope_from_settings  # noqa: E402
from llm.mock_client import MockLLMClient  # noqa: E402


def main() -> None:
    settings = load_settings(PROJECT_ROOT)
    db = AuditDatabase(settings.db_file)
    logger = JsonlLogger(settings.logs_dir)
    recorder = AgentRecorder(db, logger, settings.artifacts_dir)

    # 阶段0固定 mock；阶段1接入 DeepSeek 后再切换
    llm = MockLLMClient()
    model = "mock"

    graph = build_graph(
        llm=llm,
        model=model,
        db=db,
        logger=logger,
        recorder=recorder,
        risk_config=settings.raw_risk,
        knowledge_dir=settings.knowledge_dir,
        enable_live_data=False,
        trading_scope=scope_from_settings(settings.raw_settings),
    )

    run_id = str(uuid.uuid4())
    decision_id = str(uuid.uuid4())
    final_state = graph.invoke({"run_id": run_id, "decision_id": decision_id})

    decision = final_state.get("decision")
    verdict = final_state.get("risk_verdict")
    if decision is not None:
        db.log_trade_decision(
            decision_id=decision_id,
            run_id=run_id,
            decision=decision.decision,
            reasoning=decision.reasoning,
            raw_json=decision.model_dump_json(),
        )

    print("=== phase0 run ===")
    print("run_id:", run_id)
    print("decision:", decision.model_dump_json() if decision else None)
    print("risk_verdict:", verdict.model_dump_json() if verdict else None)
    print("db_counts:", json.dumps(db.table_counts(), ensure_ascii=False))
    print("db_file:", settings.db_file)
    print("logs_dir:", settings.logs_dir)
    db.close()


if __name__ == "__main__":
    main()
