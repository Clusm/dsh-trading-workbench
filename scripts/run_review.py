"""生成每日复盘报告。用法: python scripts/run_review.py [YYYY-MM-DD]"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from audit.jsonl_logger import JsonlLogger  # noqa: E402
from core.config_loader import load_settings  # noqa: E402
from agents.review import run_review_agent  # noqa: E402
from llm.factory import build_llm_for_agent  # noqa: E402


def main() -> None:
    settings = load_settings(PROJECT_ROOT)
    db = AuditDatabase(settings.db_file)
    logger = JsonlLogger(settings.logs_dir)
    llm, model = build_llm_for_agent(settings, "review_agent")
    date_arg = sys.argv[1] if len(sys.argv) > 1 else None
    report = run_review_agent(db, settings.reviews_dir, review_date=date_arg,
                              llm=llm, model=model, logger=logger)
    print("report:", report)
    db.close()


if __name__ == "__main__":
    main()
