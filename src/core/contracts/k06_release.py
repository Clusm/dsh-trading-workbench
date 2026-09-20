"""K06 ReleaseEvidence 五组状态契约。

只输出 engineering / accounting / runtime_observation / operations /
strategy_evaluation 五组 ``PASS|FAIL|NO_DATA`` 与阻断项清单；这里不建立
第二套 GO/NO_GO 判断器，最终门禁仍由既有发布证据计算器与发布审查决定。
NO_DATA 绝不等于 PASS；缺证据必须列出具体阻断项。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from .common import SCHEMA_VERSION, canonical_hash

RELEASE_GROUPS = (
    "engineering",
    "accounting",
    "runtime_observation",
    "operations",
    "strategy_evaluation",
)

CODE_MISSING_EVIDENCE = "MISSING_EVIDENCE"
CODE_PASS_WITHOUT_EVIDENCE = "PASS_WITHOUT_EVIDENCE"
CODE_GROUP_FAILED = "GROUP_FAILED"
CODE_GROUP_NO_DATA = "NO_DATA"
CODE_WINDOW_INVALID = "WINDOW_INVALID"
CODE_WINDOW_MISSING = "WINDOW_MISSING"
CODE_EVIDENCE_HASH_MISSING = "EVIDENCE_HASH_MISSING"
CODE_IDENTITY_REF_MISSING = "IDENTITY_REF_MISSING"
CODE_IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
CODE_DECLARED_STATUS_UNKNOWN = "DECLARED_STATUS_UNKNOWN"


class BlockingItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    group: str
    code: str
    detail: str
    severity: Literal["BLOCKING", "WARNING"] = "BLOCKING"


class ReleaseGroupEvidence(BaseModel):
    """单组状态；组与组独立，一组 FAIL 不影响其余组的计算。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    group: str
    status: Literal["PASS", "FAIL", "NO_DATA"]
    evidence_hash: str | None = None
    window_start_ms: int | None = None
    window_end_ms: int | None = None
    identity_ref: str | None = None
    reason_codes: tuple[str, ...] = ()


def evaluate_group(
    group: str,
    evidence: Mapping[str, Any] | None,
    *,
    identity_ref: str | None = None,
) -> tuple[ReleaseGroupEvidence, tuple[BlockingItem, ...]]:
    """评估单组；PASS 必须同时具备证据 hash、有效窗口与身份引用。"""
    if group not in RELEASE_GROUPS:
        raise ValueError(f"unknown release group: {group}")
    if evidence is None:
        return (
            ReleaseGroupEvidence(group=group, status="NO_DATA", reason_codes=(CODE_MISSING_EVIDENCE,)),
            (BlockingItem(
                group=group,
                code=CODE_MISSING_EVIDENCE,
                detail="no evidence payload supplied for group",
            ),),
        )
    payload = dict(evidence)
    declared = str(payload.get("status") or "NO_DATA").upper()
    evidence_hash = payload.get("evidence_hash")
    window_start = payload.get("window_start_ms")
    window_end = payload.get("window_end_ms")
    group_identity = payload.get("identity_ref")
    if declared == "PASS":
        missing: list[str] = []
        if not evidence_hash:
            missing.append(CODE_EVIDENCE_HASH_MISSING)
        if window_start is None or window_end is None:
            missing.append(CODE_WINDOW_MISSING)
        if not group_identity:
            missing.append(CODE_IDENTITY_REF_MISSING)
        if missing:
            blockers = [BlockingItem(
                group=group,
                code=CODE_PASS_WITHOUT_EVIDENCE,
                detail="declared PASS but evidence incomplete: " + ", ".join(missing),
            )]
            blockers.extend(
                BlockingItem(group=group, code=code, detail=f"required for PASS: {code}")
                for code in missing
            )
            return (
                ReleaseGroupEvidence(
                    group=group,
                    status="FAIL",
                    evidence_hash=str(evidence_hash) if evidence_hash else None,
                    window_start_ms=int(window_start) if window_start is not None else None,
                    window_end_ms=int(window_end) if window_end is not None else None,
                    identity_ref=str(group_identity) if group_identity else None,
                    reason_codes=tuple([CODE_PASS_WITHOUT_EVIDENCE, *missing]),
                ),
                tuple(blockers),
            )
        reasons: list[str] = []
        if int(window_end) <= int(window_start):
            reasons.append(CODE_WINDOW_INVALID)
        if identity_ref and str(group_identity) != identity_ref:
            reasons.append(CODE_IDENTITY_MISMATCH)
        if reasons:
            blockers = tuple(
                BlockingItem(group=group, code=code, detail=f"PASS rejected: {code}")
                for code in reasons
            )
            return (
                ReleaseGroupEvidence(
                    group=group,
                    status="FAIL",
                    evidence_hash=str(evidence_hash),
                    window_start_ms=int(window_start),
                    window_end_ms=int(window_end),
                    identity_ref=str(group_identity),
                    reason_codes=tuple(reasons),
                ),
                blockers,
            )
        return (
            ReleaseGroupEvidence(
                group=group,
                status="PASS",
                evidence_hash=str(evidence_hash),
                window_start_ms=int(window_start),
                window_end_ms=int(window_end),
                identity_ref=str(group_identity),
            ),
            (),
        )
    if declared == "FAIL":
        return (
            ReleaseGroupEvidence(
                group=group,
                status="FAIL",
                evidence_hash=str(evidence_hash) if evidence_hash else None,
                window_start_ms=int(window_start) if window_start is not None else None,
                window_end_ms=int(window_end) if window_end is not None else None,
                identity_ref=str(group_identity) if group_identity else None,
                reason_codes=(CODE_GROUP_FAILED,),
            ),
            (BlockingItem(
                group=group,
                code=CODE_GROUP_FAILED,
                detail=str(payload.get("detail") or "group reported failure"),
            ),),
        )
    if declared == "NO_DATA":
        return (
            ReleaseGroupEvidence(
                group=group,
                status="NO_DATA",
                evidence_hash=str(evidence_hash) if evidence_hash else None,
                window_start_ms=int(window_start) if window_start is not None else None,
                window_end_ms=int(window_end) if window_end is not None else None,
                identity_ref=str(group_identity) if group_identity else None,
                reason_codes=(CODE_GROUP_NO_DATA,),
            ),
            (BlockingItem(
                group=group,
                code=CODE_GROUP_NO_DATA,
                detail=str(payload.get("detail") or "no data available for group"),
            ),),
        )
    return (
        ReleaseGroupEvidence(group=group, status="NO_DATA", reason_codes=(CODE_DECLARED_STATUS_UNKNOWN,)),
        (BlockingItem(
            group=group,
            code=CODE_DECLARED_STATUS_UNKNOWN,
            detail=f"unrecognised declared status: {declared}",
        ),),
    )


class ReleaseEvidenceReport(BaseModel):
    """五组状态 + 阻断项；模型自身不携带任何整体 GO 判定字段。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = SCHEMA_VERSION
    groups: tuple[ReleaseGroupEvidence, ...] = ()
    blocking_items: tuple[BlockingItem, ...] = ()
    report_hash: str = ""

    def group_status(self, group: str) -> str | None:
        for item in self.groups:
            if item.group == group:
                return item.status
        return None


def build_release_report(
    groups: Mapping[str, Mapping[str, Any] | None],
    *,
    identity_ref: str | None = None,
) -> ReleaseEvidenceReport:
    """逐组独立评估并汇总阻断项；不产生第二套 GO 判断。"""
    evaluated: list[ReleaseGroupEvidence] = []
    blockers: list[BlockingItem] = []
    for group in RELEASE_GROUPS:
        status, group_blockers = evaluate_group(
            group, groups.get(group), identity_ref=identity_ref,
        )
        evaluated.append(status)
        blockers.extend(group_blockers)
    report = ReleaseEvidenceReport(
        groups=tuple(evaluated),
        blocking_items=tuple(blockers),
        report_hash="",
    )
    payload = report.model_dump(mode="json")
    payload.pop("report_hash", None)
    return report.model_copy(update={"report_hash": canonical_hash(payload)})


class K06SchemaEnvelope(BaseModel):
    """schema 导出与冒烟校验入口；不是运行期数据结构。"""

    model_config = ConfigDict(extra="forbid")

    group_evidence: ReleaseGroupEvidence
    blocking_item: BlockingItem
    report: ReleaseEvidenceReport
