"""自动复盘队列不阻塞交易调度线程的回归（全离线）。"""
from __future__ import annotations

import queue
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from audit.database import AuditDatabase  # noqa: E402
from core.service import TradingService  # noqa: E402


def check(name: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"{name}: {detail}")
    print(f"  PASS  {name}")


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="mta-review-worker-"))
    service = TradingService.__new__(TradingService)
    service.settings = SimpleNamespace(
        raw_settings={"review": {
            "auto": True, "time_utc": "00:05", "use_llm": False,
            "total_timeout_seconds": 10,
        }},
        reviews_dir=tmp / "reviews",
    )
    service.db = AuditDatabase(tmp / "review.sqlite")
    service.logger = None
    service._stop_event = threading.Event()
    service._review_thread = None
    service._review_queue = queue.Queue()
    service._review_pending = set()
    service._review_lock = threading.Lock()

    print("\n[1] 到点只发现并入队，不在调度调用栈同步生成")
    now = datetime.now(timezone.utc).replace(hour=0, minute=10, second=0, microsecond=0)
    started = time.monotonic()
    queued = TradingService._maybe_auto_review(service, now)
    elapsed = time.monotonic() - started
    check("到点返回日期队列", len(queued) == 7, queued)
    check("发现调用快速返回", elapsed < 0.5, elapsed)
    check("worker已启动且队列可见", service._review_thread is not None and
          service._review_thread.is_alive() and service._review_queue.qsize() >= 1)

    print("\n[2] worker串行完成并可审计，服务停止可收敛")
    service._review_queue.join()
    reports = list((tmp / "reviews").glob("*.md"))
    rows = service.db.fetch_all("SELECT review_date FROM daily_reviews")
    check("7天缺口全部生成", len(reports) == 7 and len(rows) == 7,
          {"reports": len(reports), "rows": len(rows)})
    service._stop_event.set()
    service._review_thread.join(timeout=2.0)
    check("worker停止后无遗留线程", not service._review_thread.is_alive())

    print("\n[3] 未到点不创建任务")
    before = service._maybe_auto_review(
        now.replace(hour=0, minute=1),
    )
    check("未到点返回空", before == [], before)
    print("\n结果：PASS 6 / FAIL 0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
