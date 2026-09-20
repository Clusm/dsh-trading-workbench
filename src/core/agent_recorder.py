"""统一记录每个 Agent 的运行、输出工件与 JSONL 日志。"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from audit.database import AuditDatabase, utc_now
from audit.jsonl_logger import JsonlLogger
from core.decision_trace import (
    DeadlineBudget,
    DecisionReason,
    DecisionTrace,
    TraceSummary,
)


class AgentRecorder:
    """将 Agent 运行结果同时写入 SQLite、JSONL 和可检查的结构化工件。"""
    def __init__(self, db: AuditDatabase, logger: JsonlLogger, artifacts_dir: Path):
        self.db = db
        self.logger = logger
        self.artifacts_dir = Path(artifacts_dir)
        # 发布批次由 Service 在 broker 建立后写入数据库上下文；这里仅保存一个
        # 兼容旧调用的快照，数据库本身仍会以当前上下文作为默认值。
        self.release_id = str(getattr(db, "release_id", "legacy") or "legacy")

    def save_artifact(self, agent_name: str, artifact_type: str, obj: BaseModel | dict,
                      run_id: str | None = None) -> str:
        payload = obj.model_dump() if isinstance(obj, BaseModel) else obj
        artifact_id = str(uuid.uuid4())
        p = self.artifacts_dir / f"{artifact_id}.json"
        p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        sha = hashlib.sha256(p.read_bytes()).hexdigest()
        self.db.log_artifact(
            artifact_id=artifact_id,
            run_id=run_id,
            agent_name=agent_name,
            artifact_type=artifact_type,
            created_at=utc_now(),
            json_path=str(p),
            sha256=sha,
            release_id=getattr(self.db, "release_id", self.release_id),
        )
        return str(p)

    def record(self, agent_name: str, model: str | None, role: str | None,
               input_summary: str, output: BaseModel | dict | None,
               error: Exception | None = None, run_id: str | None = None) -> dict[str, Any]:
        started = time.monotonic()
        started_at = utc_now()
        artifact_path = None
        output_summary = ""
        status = "ok"
        err_text = None
        if error is None:
            try:
                artifact_path = self.save_artifact(agent_name, "agent_output", output or {}, run_id)
                if isinstance(output, BaseModel):
                    output_summary = output.model_dump_json()[:500]
                else:
                    output_summary = json.dumps(output, ensure_ascii=False)[:500]
            except Exception as e:  # 记录失败不能影响主流程
                status = "audit_error"
                err_text = str(e)
        else:
            status = "error"
            err_text = str(error)

        latency_ms = int((time.monotonic() - started) * 1000)
        agent_run_id = str(uuid.uuid4())
        self.db.register_agent(agent_name, role, model)
        self.db.log_agent_run(
            run_id=agent_run_id,
            agent_name=agent_name,
            started_at=started_at,
            ended_at=utc_now(),
            status=status,
            model=model,
            input_summary=input_summary[:500],
            output_summary=output_summary,
            latency_ms=latency_ms,
            error=err_text,
            release_id=getattr(self.db, "release_id", self.release_id),
        )
        self.logger.log_agent(agent_name, {
            "cycle_run_id": run_id,
            "agent_run_id": agent_run_id,
            "status": status,
            "input_summary": input_summary[:500],
            "output_summary": output_summary,
            "artifact": artifact_path,
            "error": err_text,
        })
        return {"status": status, "artifact": artifact_path, "output": output}

    # ------------------------------------------------------------ U05 轨迹

    def begin_decision_trace(self, cycle_id: str, *,
                             timeout_seconds: float | None = None) -> DecisionTrace:
        """为本轮决策开一条轨迹：所有模型尝试共用**同一个绝对 deadline**。

        调用方在整轮开始时创建一次，再把 ``budget.shared().deadline`` 交给主/备/
        重试与工具轮，避免每次尝试各自 ``now + timeout`` 推导（U05/DL07、R22）。
        """
        budget = (None if timeout_seconds is None
                  else DeadlineBudget.start(timeout_seconds, label=str(cycle_id)))
        return DecisionTrace(cycle_id=str(cycle_id), budget=budget)

    def record_decision_trace(self, trace: DecisionTrace, *,
                              run_id: str | None = None,
                              agent_name: str = "decision_cycle",
                              reason: DecisionReason | None = None) -> dict[str, Any]:
        """把轨迹落审计：**四类计数分开** + 分阶段 p95/p99 + 逐轮结局。

        落库口径：轨迹与分位写进工件 JSON（``save_artifact``），摘要写进
        ``agent_runs``（``log_decision_trace_summary``）——**不新增表、不推进
        schema 版本**，因此不产生迁移伴随修改。

        ``latency_ms`` 落的是**真实耗时**（``trace.elapsed_ms()``，整轮墙钟跨度），
        没测到就是 NULL——它绝不能借给 ``failed_rounds`` 用（B-r1 P1-1：那会把
        "失败轮次个数"当成毫秒混进 ``review.py`` 的 ``AVG(latency_ms)`` 每日复盘）。

        **两条落库路径都被保护**：工件与 ``agent_runs`` 任一处失败只标
        ``audit_error``、不影响决策主流程；旧实现只把 ``save_artifact`` 包进
        ``try``，DB 写入异常会直接抛给调用方（B-r1 P2-5），与 docstring 不符。
        """
        summary: TraceSummary = trace.summary()
        latency_ms = trace.elapsed_ms()
        payload = {
            "schema_version": "u05-decision-trace-1",
            "trace": trace.as_dict(),
            "summary": summary.as_dict(),
            "reason": None if reason is None else reason.as_dict(),
        }
        audit_error: str | None = None
        artifact_path: str | None = None
        try:
            artifact_path = self.save_artifact(agent_name, "decision_trace", payload, run_id)
        except Exception as e:  # noqa: BLE001 - 审计失败不影响主流程
            audit_error = f"artifact: {type(e).__name__}: {e}"
        else:
            try:
                self.db.log_decision_trace_summary(
                    run_id=str(run_id or trace.cycle_id),
                    cycle_id=trace.cycle_id,
                    summary=summary.as_dict(),
                    latency={name: item.as_dict() for name, item in trace.latency().items()},
                    outcomes=[item.as_dict() for item in trace.outcomes],
                    reason=None if reason is None else reason.as_dict(),
                    artifact_path=artifact_path,
                    latency_ms=latency_ms,
                    release_id=getattr(self.db, "release_id", self.release_id),
                )
            except Exception as e:  # noqa: BLE001 - 同上：DB 写入失败同样不得外抛
                audit_error = f"agent_runs: {type(e).__name__}: {e}"
        failed_rounds = summary.failed_rounds
        status = "audit_error" if audit_error else ("ok" if failed_rounds == 0 else "degraded")
        self.logger.log_agent(agent_name, {
            "cycle_run_id": run_id,
            "status": status,
            "latency_ms": latency_ms,
            "summary": summary.as_dict(),
            "artifact": artifact_path,
            "error": audit_error,
        })
        return {"status": status, "artifact": artifact_path,
                "latency_ms": latency_ms,
                "error": audit_error,
                "summary": summary.as_dict()}
