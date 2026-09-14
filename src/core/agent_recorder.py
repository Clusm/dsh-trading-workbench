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
