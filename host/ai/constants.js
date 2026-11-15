/**
 * M1.3a AI chain constants.
 *
 * These values mirror docs/02-AI决策链.md. They are deliberately data, not
 * behaviour: tests may override timeouts/budgets, but the role -> effort map
 * is fixed and must not change per cycle.
 */

export const CHAIN_VERSION = 'ai-chain/1'
export const EVIDENCE_SCHEMA = 'ai-evidence-view/1'
export const OUTPUT_SCHEMAS = Object.freeze({
  analysis: 'analysis/1',
  debate_case: 'debate_case/1',
  decision: 'decision/1',
  risk_advice: 'risk_advice/1',
  execution_entry: 'execution_entry/1',
  execution_manage: 'execution_manage/1',
  review_report: 'review_report/1',
})

export const DEBATE_MODES = Object.freeze({
  SYMMETRIC_TWO_ROUND: 'symmetric_two_round',
  INDEPENDENT: 'independent',
})
export const DEFAULT_DEBATE_MODE = DEBATE_MODES.SYMMETRIC_TWO_ROUND

export const AGENT_IDS = Object.freeze({
  analysis: 'analysis',
  red: 'red_team',
  blue: 'blue_team',
  decision: 'decision',
  risk: 'risk',
  execution: 'execution',
  review: 'review',
})

/** Role key -> provider effort. Fixed for the whole cycle. */
export const ROLE_EFFORT = Object.freeze({
  analysis: 'high',
  red_opening: 'high',
  blue_opening: 'high',
  red_response: 'high',
  blue_response: 'high',
  decision: 'max',
  risk: 'max',
  execution_entry: 'low',
  execution_manage: 'low',
  review: 'high',
})

/** Prompt-facing role key -> audit role / protocol round. */
export const ROLE_AUDIT = Object.freeze({
  analysis: { role: AGENT_IDS.analysis, round: 1, schema: OUTPUT_SCHEMAS.analysis },
  red_opening: { role: AGENT_IDS.red, round: 1, schema: OUTPUT_SCHEMAS.debate_case },
  blue_opening: { role: AGENT_IDS.blue, round: 1, schema: OUTPUT_SCHEMAS.debate_case },
  red_response: { role: AGENT_IDS.red, round: 2, schema: OUTPUT_SCHEMAS.debate_case },
  blue_response: { role: AGENT_IDS.blue, round: 2, schema: OUTPUT_SCHEMAS.debate_case },
  decision: { role: AGENT_IDS.decision, round: 1, schema: OUTPUT_SCHEMAS.decision },
  risk: { role: AGENT_IDS.risk, round: 1, schema: OUTPUT_SCHEMAS.risk_advice },
  execution_entry: { role: AGENT_IDS.execution, round: 1, schema: OUTPUT_SCHEMAS.execution_entry },
  execution_manage: { role: AGENT_IDS.execution, round: 1, schema: OUTPUT_SCHEMAS.execution_manage },
  review: { role: AGENT_IDS.review, round: 1, schema: OUTPUT_SCHEMAS.review_report },
})

/** Per-call timeouts (M1.4f). Chain deadline 340s; serial stage wall-clock is
 *  310s (analysis 70 + R1 60 + R2 55 + decision 45 + risk 45 + execution 35),
 *  leaving room for one retry inside the deadline.
 *
 *  M1.4f evidence (24 auto rounds, 2026-11-11 overnight): the dominant failure
 *  became DEBATE_INCOMPLETE (9/24) and every single one was a *timeout*, not a
 *  truncation — red_opening 15x and blue_opening 12x hit the 40s ceiling while
 *  the successful calls took 26-41s and produced only ~2-3.6k tokens. Under
 *  provider load the effective rate drops to ~80 tok/s (analysis: 3,324 tokens
 *  in 40.7s), so 40s was below the p50 for opening roles.
 *
 *  History: risk 1600@14s timed out in 43/49 calls (RISK_UNAVAILABLE x26);
 *  execution 2000@20s truncated 7/16 (EXECUTION_PLAN_INVALID); decision
 *  4500@30s timed out 15x. Those were raised in M1.4c; openings are raised here. */
export const ROLE_TIMEOUT_MS = Object.freeze({
  analysis: 70_000,
  red_opening: 60_000,
  blue_opening: 60_000,
  red_response: 55_000,
  blue_response: 55_000,
  decision: 45_000,
  risk: 45_000,
  execution_entry: 35_000,
  execution_manage: 25_000,
  review: 150_000,
})

/** Hard upper bound for one role call. Retry escalation never exceeds this. */
export const MAX_OUTPUT_TOKENS = 10_000

/** Per-call output ceilings. Measured generation rate is ~130 tok/s, and
 *  effort=max roles (decision/risk) also pay reasoning time. Sized from the
 *  observed distribution (p90 + headroom), not from a round number. */
export const ROLE_MAX_TOKENS = Object.freeze({
  analysis: 8_000,
  red_opening: 4_000,
  blue_opening: 4_000,
  red_response: 4_600,
  blue_response: 4_600,
  decision: 4_000,
  risk: 3_000,
  execution_entry: 3_200,
  execution_manage: 2_400,
  review: 3_000,
})

/** Evidence slice token budgets from 02 §4.4 (two-round: 14.4k total). */
export const EVIDENCE_SLICE_TOKENS = Object.freeze({
  analysis: 3_500,
  debate: 1_800,
  decision: 1_200,
  risk: 600,
  execution: 400,
  review: 0,
})

/**
 * 提示词版本：写入每次调用审计与每轮快照，用来区分"同一份代码、不同提示"。
 *
 * T16/T17 bump：analysis/red/blue/decision 的提示新增了 entry.zone_low/zone_high
 * 与 valid_until 的硬要求（并把取值依据放进 payload.plan_basis）。提示文本变了，
 * 版本号必须跟着变，否则账本里无法区分"硬化前/硬化后"的样本——T16 的 enforce
 * 晋级条件 a 正是依赖这个字段。
 *   analysis  v1 → v2（新增 trigger.valid_until / invalidation.price 的硬要求）
 *   red/blue  v2 → v3（新增 zone/valid_until 硬要求 + plan_basis 依据；R2 不得重推）
 *   decision  v2 → v3（新增 plan.zone/valid_until 必须原样复制）
 * 其余角色提示文本未改，版本保持不变。
 */
export const PROMPT_VERSIONS = Object.freeze({
  analysis: 'analysis.v2.zh2',
  red_opening: 'red.v3.zh2',
  blue_opening: 'blue.v3.zh2',
  red_response: 'red.v3.zh2',
  blue_response: 'blue.v3.zh2',
  decision: 'decision.v3.zh2',
  risk: 'risk.v1.zh1',
  execution_entry: 'execution.v1.zh1',
  execution_manage: 'execution.v1.zh1',
  review: 'review.v1.zh1',
})

export const TEMPERATURES = Object.freeze({
  analysis: 0.2,
  red_opening: 0.2,
  blue_opening: 0.2,
  red_response: 0.2,
  blue_response: 0.2,
  decision: 0.1,
  risk: 0.1,
  execution_entry: 0,
  execution_manage: 0,
  review: 0.2,
})

/** Hard risk caps used only for local scorecard/semantic checks. The service
 * remains authoritative; M1.3a never writes a DB or calls the execution API. */
export const DEFAULT_CAPS = Object.freeze({
  maxRiskPct: 1.0,
  strongMaxRiskPct: 2.0,
  maxLeverage: 10,
  strongMaxLeverage: 15,
  minRr: 1.2,
  slMinPct: 0.2,
  slMaxPct: 5.0,
  maxNotionalMultiple: 2.0,
  maxOpenPositions: 1,
})

/** §8·D 执行前确定性闸门。off=不评估；observe=只记录不拦截；enforce=拦截。
 * 默认 observe 的理由：模型尚未被要求输出 valid_until 与入场区间，直接 enforce
 * 会让所有轮次都判 PLAN_INVALID 从而彻底停止交易——那等于用一个新缺口换旧缺口。
 * 提示层硬化 + 新样本确认后才切 enforce（由配置/调用方显式指定）。
 * 注意：observe 下闸门结论必须出现在每轮结果里，不允许“评估了但没人看见”。 */
export const EXECUTION_GATE_MODES = Object.freeze({ OFF: 'off', OBSERVE: 'observe', ENFORCE: 'enforce' })
export const DEFAULT_EXECUTION_GATE_MODE = EXECUTION_GATE_MODES.OBSERVE

/**
 * §8·E 链路变体（固定枚举，不是任意流程编排器）。
 *
 * baseline = 现行八角色链路；lean = 去掉 analysis 与 decision 两个 LLM 角色调用，
 * 其余角色（R1/R2 辩论、风控、执行）与全部确定性校验（scorecard、plan-copy 语义、
 * evaluateEntryPlan 执行闸门、P1/P2）一字不改。lean 的"决策"由 scorecard 确定性
 * 推出，用的仍是同一套 decision 语义校验，因此不能用"少校验"换时间。
 *
 * 只允许这两个值：任何其他值在配置解析处直接失败（CHAIN_VARIANT_UNKNOWN），
 * 不做"按数组编排角色"的通用入口。
 */
export const CHAIN_VARIANTS = Object.freeze({ BASELINE: 'baseline', LEAN: 'lean' })
export const DEFAULT_CHAIN_VARIANT = CHAIN_VARIANTS.BASELINE

/** 变体 → 实际会发起的 LLM 角色（顺序即调用顺序）。 */
export const VARIANT_ROLES = Object.freeze({
  baseline: Object.freeze(['analysis', 'red_opening', 'blue_opening', 'red_response', 'blue_response', 'decision', 'risk', 'execution_entry']),
  lean: Object.freeze(['red_opening', 'blue_opening', 'red_response', 'blue_response', 'risk', 'execution_entry']),
})

export const CHAIN_DEADLINE_MS = 340_000
export const EVIDENCE_MAX_AGE_MS = 180_000
export const ENTRY_SNAPSHOT_MAX_AGE_MS = 15_000
export const MAX_CALLS = 14

/** 01 §5.3 phase enum for the running-progress object (docs/02 §8.3). */
export const ROLE_PHASE = Object.freeze({
  analysis: 'ANALYZING',
  red_opening: 'DEBATING',
  blue_opening: 'DEBATING',
  red_response: 'DEBATING2',
  blue_response: 'DEBATING2',
  decision: 'DECIDING',
  risk: 'RISK_AI',
  execution_entry: 'EXEC_PLANNING',
  execution_manage: 'EXEC_PLANNING',
  review: 'REVIEW',
})
export const MAX_ATTEMPTS_PER_CALL = 2
