"""U06b 影子 v2 固定时点标签：数据模型、状态与口径常量。

本模块只定义**值对象与常量**，不含时钟读取、不含数据库访问、不含网络与 LLM。
所有时点一律由调用方显式传入 ``datetime``（UTC），保证：

* **无未来信息回填**：标签函数是纯函数，只能看到调用方给的样本；
* **可重放**：同一 ``(请求, 样本)`` 必得同一 ``LabelResult``；
* **可测**：不需要冻结系统时钟就能覆盖 ``未成熟`` 分支。

口径边界（与 U06a 的关系）
--------------------------
``src/intel/shadow_status.py`` 是**首批**「影子是什么口径」的唯一权威描述源，并把
``V2_LABELS_STATE`` 记为 ``DEFERRED``。本模块**不修改也不重命名**它的任何常量，
只在研究侧实现被推迟的 v2 固定时点标签，且：

* ``V2_CALIBER_ID`` 与 legacy v1 口径 id **不同字符串**，两套结果不可能被混读；
* 每个结果都带 ``counts_toward_release_gate=False``，且 :func:`gate_isolation_facts`
  用 ``shadow_status.release_gate_role`` 现算门禁角色，不复制门禁判定。

交付后**不改变**首批记录：``F11:v2_labels=DEFERRED`` 仍是首批事实，v2 标签是后置
线交付（见 :data:`POST_RELEASE_SCOPE_NOTE`）。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from intel import shadow_status as _ss

#: 本模块（及其落库行/夹具）的 schema 版本，便于 U07 只消费匹配版本。
SCHEMA_VERSION = "u06b-shadow-v2-labels-v1"

#: v2 固定时点口径：取价时点 = 决策时刻 + horizon 的目标时刻（不是轮转刷新时刻）。
V2_CALIBER_ID = "v2-fixed-target-time"
V2_CALIBER_LABEL = "v2 固定时点标签（目标时刻取价，仅研究参考）"

#: legacy v1 口径 id（**引用** U06a，不复制字面量，避免漂移）。
LEGACY_CALIBER_ID = _ss.LEGACY_CALIBER_ID

#: v2 标签**永不计入**发布门禁分母（200 方向候选 / 30 完整闭合）。
COUNTS_TOWARD_RELEASE_GATE = False

#: 后置线口径说明：交付 v2 不等于首批 F11 已验收。
POST_RELEASE_SCOPE_NOTE = (
    "U06b 为 W4+ 后置线交付：实现 v2 固定时点标签与缺失原因标签，"
    "但**不改变**首批记录 F11:v2_labels=DEFERRED，也**不**把 v2 结果计入"
    "发布门禁（U06a 已把 B/C 定为 research_only）。影子权重讨论须在 U06b/U07 "
    "完成后另行审查，本模块不自动晋升、不给执行权重。"
)

# --------------------------------------------------------------------------- #
# 标签状态与缺失原因
# --------------------------------------------------------------------------- #

#: 到期窗口内取到合格价，得到固定时点标签。
STATE_LABELED = "LABELED"
#: 到期窗口已关闭但拿不到合格价：**不静默丢弃**，按原因留痕。
STATE_MISSING = "MISSING"
#: 目标时刻（含容差窗口）尚未走完：现在不能判定，更不能用未来价回填。
STATE_PENDING = "PENDING"

#: 缺失原因：一一对应 U06a 披露的 legacy v1 无法区分的四类缺口。
REASON_NOT_MATURED = "NOT_MATURED"           # 未成熟（窗口未关闭，属 PENDING）
REASON_NO_PRICE = "NO_PRICE"                 # 缺价：窗口内该标的一条样本都没有
REASON_OVER_TOLERANCE = "OVER_TOLERANCE"     # 超容差：最近样本偏离目标时刻过多
REASON_DATA_GAP = "DATA_GAP"                 # 断流：目标时刻前后都有样本但有长空洞
REASON_CLOCK_UNTRUSTED = "CLOCK_UNTRUSTED"   # F17：时钟回拨/跳变，拒绝出标签

#: 全部缺失原因（含 ``NOT_MATURED``，后者只挂在 ``PENDING`` 上）。
MISSING_REASONS: tuple[str, ...] = (
    REASON_NOT_MATURED,
    REASON_NO_PRICE,
    REASON_OVER_TOLERANCE,
    REASON_DATA_GAP,
    REASON_CLOCK_UNTRUSTED,
)

#: 只有这两个原因代表**已关闭窗口的真实损失**（可与 U06a 的缺失总数勾稽）。
CLOSED_WINDOW_REASONS: tuple[str, ...] = (
    REASON_NO_PRICE,
    REASON_OVER_TOLERANCE,
    REASON_DATA_GAP,
    REASON_CLOCK_UNTRUSTED,
)

#: 默认容差：目标时刻 ±90 秒。legacy v1 的 F11 反例是「5 分钟标签晚 8 分钟」，
#: 即偏差 480 秒 ≫ 90 秒 → 在 v2 下必须落 ``OVER_TOLERANCE`` 而不是被当成合格标签。
DEFAULT_TOLERANCE_SECONDS = 90.0

#: 默认采样间隔预期（秒）：5 分钟网格；用于区分「断流」与「超容差」。
DEFAULT_EXPECTED_INTERVAL_SECONDS = 300.0

#: 可接受的研究成本假设上限，防止把 v2 参数写成荒谬值。
MAX_TOLERANCE_SECONDS = 3600.0


class LeakageError(ValueError):
    """违反「无未来信息回填」时抛出（例如把目标时刻之后的价塞进路径）。"""


# --------------------------------------------------------------------------- #
# 值对象
# --------------------------------------------------------------------------- #


def utcnow() -> datetime:
    """返回带时区的当前 UTC 时刻；仅**入口**调用，纯函数一律显式收时点。"""
    return datetime.now(timezone.utc)


def as_utc(moment: datetime) -> datetime:
    """把 ``datetime`` 规范化成带时区的 UTC；无时区按 UTC 解释。"""
    if not isinstance(moment, datetime):
        raise TypeError(f"需要 datetime，得到 {type(moment).__name__}")
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def parse_ts(text: Any) -> datetime | None:
    """解析 ISO 时间串（含 ``Z`` 后缀）；不可解析返回 ``None``，不猜。"""
    if isinstance(text, datetime):
        return as_utc(text)
    raw = str(text or "").strip()
    if not raw:
        return None
    if raw.endswith(("Z", "z")):
        raw = raw[:-1] + "+00:00"
    try:
        return as_utc(datetime.fromisoformat(raw))
    except ValueError:
        return None


@dataclass(frozen=True)
class PriceSample:
    """一条带时点的价格观测（来自 ``mark_price_samples`` 或离线夹具）。

    Attributes:
        symbol: 标的。
        sampled_at: 观测时刻（UTC）。
        price: 标记价，必须为正。
        source: 观测来源标签（诊断用）。
    """

    symbol: str
    sampled_at: datetime
    price: float
    source: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", str(self.symbol or ""))
        object.__setattr__(self, "sampled_at", as_utc(self.sampled_at))
        object.__setattr__(self, "price", float(self.price))

    @staticmethod
    def from_row(row: Mapping[str, Any]) -> "PriceSample | None":
        """从 ``mark_price_samples`` 行构造；时点/价格非法返回 ``None``。"""
        moment = parse_ts(row.get("sampled_at"))
        try:
            price = float(row.get("mark_price"))
        except (TypeError, ValueError):
            return None
        if moment is None or price <= 0:
            return None
        return PriceSample(symbol=str(row.get("symbol") or ""), sampled_at=moment,
                           price=price, source=str(row.get("source") or ""))


@dataclass(frozen=True)
class ExperimentIdentity:
    """实验版本隔离键：不同版本/发布/配置的样本不得混算（DL08）。

    Attributes:
        experiment_version: 实验版本（口径变更即换版本）。
        release_id: 发布标识。
        config_fingerprint: 配置指纹（参数集哈希）。
        horizon_minutes: 目标视界（分钟）。
    """

    experiment_version: str = "v2-experiment-1"
    release_id: str = ""
    config_fingerprint: str = ""
    horizon_minutes: int = 0

    def key(self) -> tuple[str, str, str, int]:
        """返回可哈希的隔离键；**逐字段**参与，缺一不可。"""
        return (str(self.experiment_version), str(self.release_id),
                str(self.config_fingerprint), int(self.horizon_minutes))

    def label(self) -> str:
        """返回稳定字符串键，供落库/夹具文件名使用。"""
        version, release, fingerprint, horizon = self.key()
        return f"{version}|{release}|{fingerprint}|{horizon}m"


@dataclass(frozen=True)
class LabelRequest:
    """一次固定时点标签请求（决策侧事实，不含任何未来价）。

    Attributes:
        decision_id: 决策 id。
        source: ``shadow_decisions.source``（必须来自 U06a 的组定义）。
        symbol: 标的。
        signal: 信号方向。
        reference_price: 入场参考价（决策时刻的价）。
        decided_at: 决策时刻（UTC）。
        horizon_minutes: 目标视界（分钟）。
        experiment_version/release_id/config_fingerprint: 隔离键三段。
        funding_rate: 该轮资金费率（研究假设，缺省 0）。
    """

    decision_id: str
    source: str
    symbol: str
    signal: str
    reference_price: float
    decided_at: datetime
    horizon_minutes: int
    experiment_version: str = "v2-experiment-1"
    release_id: str = ""
    config_fingerprint: str = ""
    funding_rate: float = 0.0

    def __post_init__(self) -> None:
        object.__setattr__(self, "decided_at", as_utc(self.decided_at))
        object.__setattr__(self, "reference_price", float(self.reference_price))
        object.__setattr__(self, "horizon_minutes", int(self.horizon_minutes))
        object.__setattr__(self, "funding_rate", float(self.funding_rate or 0.0))

    @property
    def identity(self) -> ExperimentIdentity:
        """本请求的实验隔离键。"""
        return ExperimentIdentity(
            experiment_version=self.experiment_version,
            release_id=self.release_id,
            config_fingerprint=self.config_fingerprint,
            horizon_minutes=self.horizon_minutes,
        )

    @property
    def target_at(self) -> datetime:
        """固定目标时刻 = 决策时刻 + horizon（**不是**最近的刷新轮）。"""
        return self.decided_at + timedelta(minutes=self.horizon_minutes)

    @property
    def gate_role(self) -> str:
        """门禁角色，现算自 U06a 事实（未知来源 fail-closed 为 research_only）。"""
        return _ss.release_gate_role(self.source)


@dataclass(frozen=True)
class LabelResult:
    """一条 v2 固定时点标签结果（或缺失/未成熟留痕）。

    任何字段都不含未来信息：``observed_at`` 必落在 ``[target-tol, target+tol]``，
    路径样本必落在 ``[decided_at, target_at]``。
    """

    decision_id: str
    source: str
    symbol: str
    signal: str
    horizon_minutes: int
    target_at: datetime
    state: str
    missing_reason: str | None
    observed_at: datetime | None
    exit_price: float | None
    deviation_seconds: float | None
    tolerance_seconds: float
    gross_return_pct: float | None
    net_return_pct: float | None
    fee_cost_pct: float | None
    slippage_cost_pct: float | None
    funding_cost_pct: float | None
    correct: int | None
    mfe_pct: float | None
    mae_pct: float | None
    path_samples: int
    experiment: ExperimentIdentity
    caliber: str = V2_CALIBER_ID
    counts_toward_release_gate: bool = COUNTS_TOWARD_RELEASE_GATE
    execution_weight: float = _ss.EXECUTION_WEIGHT
    note: str = ""

    @property
    def is_labeled(self) -> bool:
        """``True`` 表示拿到了合格固定时点价。"""
        return self.state == STATE_LABELED

    def to_dict(self) -> dict[str, Any]:
        """转成可直接 JSON 序列化/落库的行（时点用 ISO 毫秒字符串）。"""

        def iso(value: datetime | None) -> str | None:
            return value.isoformat(timespec="milliseconds") if value else None

        return {
            "schema": SCHEMA_VERSION,
            "decision_id": self.decision_id,
            "source": self.source,
            "group": (spec.group if (spec := _ss.cohort_spec(self.source)) else "?"),
            "symbol": self.symbol,
            "signal": self.signal,
            "horizon_minutes": int(self.horizon_minutes),
            "target_at": iso(self.target_at),
            "state": self.state,
            "missing_reason": self.missing_reason,
            "observed_at": iso(self.observed_at),
            "exit_price": self.exit_price,
            "deviation_seconds": self.deviation_seconds,
            "tolerance_seconds": self.tolerance_seconds,
            "gross_return_pct": self.gross_return_pct,
            "net_return_pct": self.net_return_pct,
            "fee_cost_pct": self.fee_cost_pct,
            "slippage_cost_pct": self.slippage_cost_pct,
            "funding_cost_pct": self.funding_cost_pct,
            "correct": self.correct,
            "mfe_pct": self.mfe_pct,
            "mae_pct": self.mae_pct,
            "path_samples": int(self.path_samples),
            "experiment_key": self.experiment.label(),
            "experiment_version": self.experiment.experiment_version,
            "release_id": self.experiment.release_id,
            "config_fingerprint": self.experiment.config_fingerprint,
            "caliber": self.caliber,
            "counts_toward_release_gate": bool(self.counts_toward_release_gate),
            "execution_weight": float(self.execution_weight),
            "gate_role": self.gate_role,
            "note": self.note,
        }

    @property
    def gate_role(self) -> str:
        """门禁角色（现算自 U06a 事实，不复制判定）。"""
        return _ss.release_gate_role(self.source)


def gate_isolation_facts() -> dict[str, Any]:
    """返回 v2 标签与发布门禁的隔离事实（供 UI/审查引用，不含门禁判定）。"""
    return {
        "caliber": V2_CALIBER_ID,
        "legacy_caliber": LEGACY_CALIBER_ID,
        "counts_toward_release_gate": COUNTS_TOWARD_RELEASE_GATE,
        "execution_weight": _ss.EXECUTION_WEIGHT,
        "auto_promotion": False,
        "research_sources": list(_ss.research_sources()),
        "gate_role_by_source": {spec.source: spec.gate_role for spec in _ss.COHORTS},
        "release_gate_authority": "src/core/release_evidence.py (U09 固定并版本化)",
        "shadow_status_v2_state": _ss.V2_LABELS_STATE,
        "note": POST_RELEASE_SCOPE_NOTE,
    }
