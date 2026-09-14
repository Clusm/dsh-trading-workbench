"""情报 Agent 的 LLM 摘要辅助：用 flash 模型把结构化数据归纳成简报文字。"""
from __future__ import annotations

import time
import uuid
from typing import Any

from audit.database import AuditDatabase, utc_now
from audit.jsonl_logger import JsonlLogger
from llm.base import LLMClient, call_complete


def summarize(agent_name: str, llm: LLMClient | None, model: str | None,
              role_prompt: str, payload: dict[str, Any], fallback: str,
              db: AuditDatabase, logger: JsonlLogger, run_id: str | None = None,
              deadline: float | None = None,
              purpose: str = "intel") -> str:
    if llm is None:
        return fallback
    call_id = str(uuid.uuid4())
    user = str(payload)
    started = time.monotonic()
    try:
        resp = call_complete(llm, role_prompt, user, deadline=deadline, purpose=purpose)
        status = "ok"
        text = resp.content.strip()[:600]
        error = None
    except Exception as e:
        status = "error"
        text = fallback
        error = e
        resp = None
    latency = int((time.monotonic() - started) * 1000)
    prompt_file = logger.log_llm(agent_name, {"ts": utc_now(), "call_id": call_id,
                                              "system": role_prompt, "user": user}).as_posix()
    completion_file = logger.log_llm(agent_name, {"ts": utc_now(), "call_id": call_id,
                                                  "completion": text}).as_posix()
    db.log_llm_call(call_id=call_id, run_id=run_id, agent_name=agent_name, model=model or "unknown",
                    started_at=utc_now(), ended_at=utc_now(),
                    prompt_tokens=resp.prompt_tokens if resp else 0,
                    completion_tokens=resp.completion_tokens if resp else 0,
                    cost_estimate=resp.cost_estimate if resp else 0.0,
                    latency_ms=latency, status=status, error=str(error) if error else None,
                    prompt_file=prompt_file, completion_file=completion_file)
    return text if text else fallback
