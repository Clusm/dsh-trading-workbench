/**
 * RoundView skeleton consumed by M1.4. Keep this DTO presentation-safe:
 * no API tokens, no Demo credentials, no full prompts. AI call audit entries
 * contain hashes/usage/latency only, as emitted by the chain.
 */

import { CHAIN_DEADLINE_MS } from './ai/constants.js'
import { attributeRound, firstFailedCall, stopKindOf } from './attribution.js'
import { currentBuild } from './build.js'

export const ROLE_LABELS = {
  analysis: '分析',
  red_opening: '红队·开场',
  blue_opening: '蓝队·开场',
  red_response: '红队·回应',
  blue_response: '蓝队·回应',
  decision: '决策',
  risk: '风控',
  execution_entry: '操盘手',
  review: '复盘',
}

export function redactP1(p1) {
  if (!p1 || typeof p1 !== 'object') return null
  return {
    verdict: p1.verdict ?? null,
    code: p1.code ?? null,
    detail: p1.detail ?? null,
    approved_qty: p1.approved_qty ?? null,
    leverage: p1.leverage ?? null,
    approval: p1.approval
      ? {
          jti: p1.approval.jti ?? null,
          plan_hash: p1.approval.plan_hash ?? null,
          approved_qty: p1.approval.approved_qty ?? null,
          expires_at_ms: p1.approval.expires_at_ms ?? null,
        }
      : null,
  }
}

export function redactP2(p2) {
  if (!p2 || typeof p2 !== 'object') return null
  return {
    ok: p2.ok ?? null,
    code: p2.code ?? null,
    detail: p2.detail ?? null,
    status: p2.status ?? null,
    order_id: p2.order_id ?? null,
    client_order_id: p2.client_order_id ?? null,
  }
}

function num(value) {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function clipText(value, max) {
  if (typeof value !== 'string') return null
  const text = sanitizeDetail(value)
  if (!text) return null
  return text.length > max ? `${text.slice(0, max - 1)}…` : text
}

/**
 * F4 (§8·F / t6): "允许入场范围" has to be checkable in the UI. The plan already
 * carries entry.zone_low/zone_high and a top-level valid_until (08·D2); dropping
 * them here left the cockpit with one entry price and no range. They are display
 * facts only — nothing is added to the Service contract, and a missing zone stays
 * null instead of being widened to "any price".
 */
function summarizeDecisionPlan(plan) {
  if (!plan || typeof plan !== 'object' || Array.isArray(plan)) return null
  return {
    entry_style: typeof plan.entry?.style === 'string' ? plan.entry.style : null,
    entry_price: num(plan.entry?.price),
    entry_zone_low: num(plan.entry?.zone_low),
    entry_zone_high: num(plan.entry?.zone_high),
    valid_until: (() => {
      const raw = plan.valid_until ?? plan.entry?.valid_until ?? null
      if (typeof raw !== 'string') return null
      const text = raw.trim()
      return text ? text.slice(0, 40) : null
    })(),
    stop_loss: num(plan.stop_loss?.price),
    take_profit: num(plan.take_profit?.price),
    risk_pct: num(plan.risk_pct),
    leverage: num(plan.leverage),
    rr: num(plan.rr_to_target ?? plan.take_profit?.rr),
    time_stop_hours: num(plan.time_stop_hours),
  }
}

/**
 * G2 (§8·F / t6): "which model actually ran this round" has exactly one source —
 * the routeSnapshot the Host froze for that round and persisted with it. /health
 * and /state are process/load-time defaults and must never be presented as the
 * executed route. Returns null when the record predates the snapshot.
 */
function summarizeRoute(record) {
  const snapshot = record?.routeSnapshot
  if (!snapshot || typeof snapshot !== 'object' || Array.isArray(snapshot)) return null
  const route = (value) => (value && typeof value === 'object'
    ? {
        provider: typeof value.provider === 'string' ? value.provider : null,
        model: typeof value.model === 'string' ? value.model : null,
        effort: value.effectiveEffort ?? value.requestedEffort ?? null,
        effort_source: value.effortSource ?? null,
      }
    : null)
  const roles = snapshot.roles && typeof snapshot.roles === 'object' ? snapshot.roles : {}
  const roleMap = {}
  for (const [role, value] of Object.entries(roles)) roleMap[role] = route(value)
  const defaultRoute = route(snapshot.default)
  const anyRole = Object.values(roleMap).some(Boolean)
  if (!defaultRoute && !anyRole && snapshot.unavailable !== true && !Array.isArray(snapshot.errors)) return null
  return {
    default: defaultRoute,
    roles: anyRole ? roleMap : null,
    config_revision: num(snapshot.configRevision),
    frozen_at: typeof snapshot.resolvedAt === 'string' ? snapshot.resolvedAt : null,
    catalog_fingerprint: snapshot.catalogFingerprint ?? null,
    unavailable: snapshot.unavailable === true,
    errors: Array.isArray(snapshot.errors) ? snapshot.errors.slice(0, 5) : null,
  }
}

function summarizeCaseComparison(comparison) {
  if (!comparison || typeof comparison !== 'object') return null
  const side = (value) => (value && typeof value === 'object'
    ? {
        strength: clipText(value.strength, 32),
        decisive_evidence: clipText(value.decisive_evidence, 220),
        fatal_flaw: clipText(value.fatal_flaw, 220),
      }
    : null)
  const red = side(comparison.red)
  const blue = side(comparison.blue)
  const decisive = clipText(comparison.decisive_factor, 220)
  const risk = clipText(comparison.opposing_risk, 220)
  if (!red && !blue && !decisive && !risk) return null
  return { red, blue, decisive_factor: decisive, opposing_risk: risk }
}

function summarizeDecision(decision) {
  if (!decision) return null
  return {
    action: decision.action ?? null,
    selected_case: decision.selected_case ?? null,
    symbol: decision.symbol ?? null,
    side: decision.side ?? null,
    confidence: decision.confidence ?? null,
    score_override: decision.score_override ?? null,
    no_trade_reason: decision.no_trade_reason ?? null,
    // M1.4: the "why" behind the action used to be dropped here, so the UI could
    // only show action+confidence. Everything below is display text, capped, and
    // passes the same secret scrubber as reasonDetail.
    thesis: clipText(decision.thesis, 400),
    case_comparison: summarizeCaseComparison(decision.case_comparison),
    plan: summarizeDecisionPlan(decision.plan),
    invalidation: (() => {
      const inv = decision.invalidation
      if (!inv || typeof inv !== 'object') return null
      const priceEvent = clipText(inv.price_event ?? inv.condition, 220)
      const time = clipText(inv.time ?? inv.review_at, 80)
      return priceEvent || time ? { price_event: priceEvent, time } : null
    })(),
    warnings: safeNotes(decision.warnings, 3),
  }
}

function summarizeRisk(risk, riskVerdict) {
  const verdict = riskVerdict && typeof riskVerdict === 'object' ? riskVerdict : null
  if (!risk && !verdict) return null
  return {
    recommendation: risk?.recommendation ?? null,
    size_multiplier: risk?.size_multiplier ?? null,
    max_risk_pct: risk?.max_risk_pct ?? null,
    max_leverage: risk?.max_leverage ?? null,
    concerns: Array.isArray(risk?.concerns) ? risk.concerns.slice(0, 5) : [],
    // P1 RiskVerdict.as_dict() rule hits: {rule, action, detail}. Keep null when
    // the service verdict never reached the Host (missing, not "no hits").
    violations: Array.isArray(verdict?.violations)
      ? verdict.violations
        .filter((item) => item && typeof item === 'object')
        .map((item) => ({
          rule: item.rule ?? null,
          action: item.action ?? null,
          detail: item.detail ?? null,
        }))
      : null,
    blocking_rule: verdict?.blocking_rule ?? null,
  }
}

function summarizeCaseScore(caseScore) {
  if (!caseScore || typeof caseScore !== 'object') return null
  return {
    grade: caseScore.grade ?? null,
    gate: caseScore.gate ?? null,
    gate_reasons: Array.isArray(caseScore.gate_reasons) ? caseScore.gate_reasons.slice() : null,
    // M1.4: the numbers behind the gate. Without them "FAIL_CONFIDENCE" is a
    // verdict with no evidence, and the two sides cannot be compared.
    rr: num(caseScore.rr),
    stop_distance_pct: num(caseScore.stop_distance_pct),
    confidence: num(caseScore.confidence),
    opening_confidence: num(caseScore.opening_confidence),
    response_confidence: num(caseScore.response_confidence),
    rubric_score: num(caseScore.rubric_score),
    evidence_strength: num(caseScore.evidence_strength),
    risk_pct: num(caseScore.risk_pct),
    leverage: num(caseScore.leverage),
    rebuttal_count: num(caseScore.rebuttal_count),
  }
}

/**
 * Project the deterministic chain.js scorecard. `grade`/`gate_reasons` at the
 * top level follow the decision's selected case (red/blue); both sides stay
 * available under `red`/`blue`. No selection or no scorecard -> null, never a
 * fabricated grade or empty reason list.
 */
function summarizeScorecard(scorecard, decision) {
  if (!scorecard || typeof scorecard !== 'object') return null
  const red = summarizeCaseScore(scorecard.red)
  const blue = summarizeCaseScore(scorecard.blue)
  const selectedCase = decision?.selected_case === 'red' || decision?.selected_case === 'blue'
    ? decision.selected_case
    : null
  const selected = selectedCase === 'red' ? red : selectedCase === 'blue' ? blue : null
  return {
    grade: selected?.grade ?? null,
    gate: selected?.gate ?? null,
    gate_reasons: selected?.gate_reasons ?? null,
    debate_quality: scorecard.debate_quality ?? null,
    tie: typeof scorecard.tie === 'boolean' ? scorecard.tie : null,
    degraded: typeof scorecard.degraded === 'boolean' ? scorecard.degraded : null,
    red,
    blue,
  }
}

function summarizeExecution(execution, p1, p2) {
  if (!execution) return null
  return {
    executable: execution.executable ?? null,
    blocked_reason: execution.blocked_reason ?? null,
    symbol: execution.symbol ?? null,
    side: execution.side ?? null,
    order_plan: execution.order_plan ?? null,
    // Same sanitizer as reasonDetail: the public DTO never carries a raw
    // token/Bearer/api_key/sk- string, even when the client falls back to it.
    rationale: typeof execution.rationale === 'string' ? sanitizeDetail(execution.rationale) : null,
    p1: redactP1(p1),
    p2: redactP2(p2),
  }
}

// ---- safe reason detail (t83) ---------------------------------------------
// A round's reasonCode can carry a blocking code (EXECUTION_BLOCKED:OTHER); the
// explanation lives in execution.rationale or result.detail. This projection
// keeps a short display summary only: no prompt text, no tokens, no credentials,
// no stack traces. Unknown object shapes are ignored rather than dumped.
const MAX_REASON_DETAIL = 400
const SECRET_PATTERNS = [
  /\bauthorization\s*[:=]\s*(?:bearer\s+)?\S+/gi,
  /\bbearer\s+\S+/gi,
  /\b(api[_-]?key|secret|password|passphrase|token)\b\s*[:=]\s*\S+/gi,
  /\bsk-[A-Za-z0-9_-]{8,}/g,
]

function sanitizeDetail(value) {
  // Missing values must stay null: `String(null)` used to leak the literal
  // strings "null"/"undefined" into the UI as if they were explanations.
  if (value === null || value === undefined) return null
  let text = String(value)
  for (const pattern of SECRET_PATTERNS) text = text.replace(pattern, '[redacted]')
  text = text.replace(/\s+/g, ' ').trim()
  if (!text) return null
  return text.length > MAX_REASON_DETAIL ? `${text.slice(0, MAX_REASON_DETAIL - 1)}…` : text
}

function detailFrom(value, depth = 0) {
  if (typeof value === 'string') return sanitizeDetail(value)
  if (Array.isArray(value)) {
    const parts = value.slice(0, 6).map((item) => detailFrom(item, depth + 1)).filter(Boolean)
    return parts.length > 0 ? sanitizeDetail(parts.join('; ')) : null
  }
  if (value && typeof value === 'object' && depth < 2) {
    const parts = []
    for (const key of ['message', 'reason', 'detail', 'blocked_reason', 'code']) {
      const piece = value[key]
      if (typeof piece === 'string' && piece.trim()) parts.push(piece)
    }
    // t86 NO_CANDIDATE detail: {reason, confidence, regime, notes[], warnings[]}.
    for (const key of ['notes', 'warnings']) {
      if (Array.isArray(value[key])) {
        for (const entry of value[key].slice(0, 3)) {
          if (typeof entry === 'string' && entry.trim()) parts.push(entry)
        }
      }
    }
    return parts.length > 0 ? detailFrom(parts.join(' · '), depth + 1) : null
  }
  return null
}

function safeNotes(value, limit = 3) {
  if (!Array.isArray(value)) return []
  const notes = []
  for (const entry of value) {
    const text = sanitizeDetail(entry)
    if (text && !notes.includes(text)) notes.push(text)
    if (notes.length >= limit) break
  }
  return notes
}

// Analysis summary only carries display text (regime/notes/warnings); prompts,
// tokens and credentials never appear here.
export function summarizeAnalysis(analysis) {
  if (!analysis || typeof analysis !== 'object' || Array.isArray(analysis)) return null
  const read = analysis.market_read && typeof analysis.market_read === 'object' && !Array.isArray(analysis.market_read)
    ? analysis.market_read
    : null
  const notes = safeNotes(read?.notes, 3)
  const warnings = safeNotes(analysis.warnings, 3)
  const standAside = analysis.stand_aside === true ? true : analysis.stand_aside === false ? false : null
  const standAsideReason = sanitizeDetail(analysis.stand_aside_reason)
  const candidates = Array.isArray(analysis.candidates)
    ? analysis.candidates
      .slice(0, 2)
      .filter((item) => item && typeof item === 'object')
      .map((item) => ({
        symbol: typeof item.symbol === 'string' ? item.symbol : null,
        side_bias: typeof item.side_bias === 'string' ? item.side_bias : null,
        setup_type: typeof item.setup_type === 'string' ? item.setup_type : null,
        confidence: num(item.confidence),
      }))
    : []
  if (standAside === null && !read && warnings.length === 0 && !standAsideReason && candidates.length === 0) return null
  return {
    stand_aside: standAside,
    stand_aside_reason: standAsideReason,
    candidate_count: Array.isArray(analysis.candidates) ? analysis.candidates.length : null,
    candidates,
    market_read: read
      ? { regime: typeof read.regime === 'string' ? sanitizeDetail(read.regime) : null, notes }
      : null,
    warnings,
  }
}

/**
 * Per-round chain rollup (M1.4). The waterfall already lists every call, but a
 * reader cannot tell at a glance how many calls retried, how many were cut off
 * by the output ceiling, or how much of the time budget was consumed.
 */
export function summarizeChain(result, record, { stopKind = null } = {}) {
  const calls = Array.isArray(result?.calls) ? result.calls : []
  const attempted = calls.filter((call) => call && typeof call === 'object')
  const counted = attempted.filter((call) => Number.isFinite(record?.durationMs) || call.call_id)
  const retries = attempted.filter((call) => Number(call.attempt) > 1).length
  const truncated = attempted.filter((call) => call.finish_kind === 'max_tokens').length
  const timeouts = attempted.filter((call) => call.status === 'timeout').length
  const invalid = attempted.filter((call) => call.status === 'invalid').length
  const repaired = attempted.filter((call) => call.repaired === true).length
  const tokensIn = attempted.reduce((sum, call) => sum + (num(call.input_tokens) ?? 0), 0)
  const tokensOut = attempted.reduce((sum, call) => sum + (num(call.output_tokens) ?? 0), 0)
  let slowest = null
  for (const call of attempted) {
    const latency = num(call.latency_ms)
    if (latency !== null && (slowest === null || latency > slowest.latency_ms)) {
      slowest = { role_key: call.role_key ?? call.role ?? null, latency_ms: latency }
    }
  }
  const reached = attempted.filter((call) => call.schema_ok === true).map((call) => call.role_key ?? call.role)
  // A degraded round legitimately never asks for R2, so those stages are not
  // "unreached" — otherwise every fallback round would look like it broke early.
  const degraded = result?.degraded === true || result?.mode === 'opening_only'
  const stages = [
    'analysis', 'red_opening', 'blue_opening',
    ...(degraded ? [] : ['red_response', 'blue_response']),
    'decision', 'risk', 'execution_entry',
  ]
  let stoppedAt = null
  for (let index = 0; index < stages.length; index += 1) {
    if (!reached.includes(stages[index])) { stoppedAt = stages[index]; break }
  }
  const durationMs = num(record?.durationMs)
  // 05 N01 / 01 §5.1: "stopped_at" is redefined to mean "the first stage that
  // never produced". A legal terminal (no trade / gate reject / skip / trade)
  // must not be reported as an early stop, otherwise every NO_TRADE round looks
  // like it broke. Only stop_kind='failed' carries it.
  const failedCall = firstFailedCall(attempted)
  return {
    calls: counted.length,
    retries,
    repaired,
    truncated,
    timeouts,
    invalid,
    tokens_in: tokensIn || null,
    tokens_out: tokensOut || null,
    elapsed_ms: durationMs,
    deadline_ms: CHAIN_DEADLINE_MS,
    slowest,
    stop_kind: stopKind,
    stop_role_key: failedCall?.role_key ?? failedCall?.role ?? null,
    stop_call_id: failedCall?.call_id ?? null,
    stop_error_code: failedCall?.error_code ?? null,
    stopped_at: stopKind === 'failed' ? stoppedAt : null,
  }
}

// Up to three display strings from the t86 detail object or the analysis output.
// `reason`/`confidence`/`regime` stay in reasonDetail, not in this list.
export function summarizeReasonNotes(record, analysis) {
  const notes = []
  const push = (value) => {
    const text = sanitizeDetail(value)
    if (text && !notes.includes(text)) notes.push(text)
  }
  const detail = record?.result?.detail
  if (detail && typeof detail === 'object' && !Array.isArray(detail)) {
    safeNotes(detail.notes).forEach(push)
    safeNotes(detail.warnings).forEach(push)
  }
  if (analysis) {
    safeNotes(analysis.market_read?.notes).forEach(push)
    safeNotes(analysis.warnings).forEach(push)
  }
  return notes.slice(0, 3)
}

/**
 * 03 A6: structured Host failure details ({kind, role, error_code, timeout_ms}).
 * The Client renders the Chinese sentence; `reasonRaw` is folded as "原文".
 */
const REASON_PARAM_KEYS = ['kind', 'role', 'error_code', 'timeout_ms']

export function summarizeReasonParams(record) {
  const detail = record?.result?.detail
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return null
  const params = {}
  for (const key of REASON_PARAM_KEYS) {
    const value = detail[key]
    if (typeof value === 'string' && value.trim()) params[key] = value.trim().slice(0, 80)
    else if (typeof value === 'number' && Number.isFinite(value)) params[key] = value
  }
  return Object.keys(params).length > 0 ? params : null
}

export function summarizeReasonRaw(record) {
  const detail = record?.result?.detail
  if (!detail || typeof detail !== 'object' || Array.isArray(detail)) return null
  return typeof detail.raw === 'string' ? sanitizeDetail(detail.raw) : null
}

export function summarizeReasonDetail(record, execution = null, analysis = null) {
  const result = record?.result ?? null
  // A structured detail already has a code + params; never surface the Host's
  // English engineering sentence as the display detail.
  const structured = summarizeReasonParams(record) !== null
  const candidates = [
    execution?.rationale,
    structured ? null : result?.detail,
    analysis?.stand_aside_reason,
  ]
  if (!structured) candidates.push(record?.error?.message, record?.errorStatus?.message)
  for (const candidate of candidates) {
    const text = detailFrom(candidate)
    if (text) return text
  }
  return null
}

function sanitizeValidationErrors(value) {
  if (!Array.isArray(value)) return []
  return value
    .slice(0, 10)
    .filter((item) => item && typeof item === 'object')
    .map((item) => ({
      path: typeof item.path === 'string' ? clipText(item.path, 120) : null,
      message: clipText(item.message, 200),
      keyword: typeof item.keyword === 'string' ? item.keyword.slice(0, 60) : null,
    }))
}

export function buildNodes(calls, evidenceSnapshotId = null) {
  if (!Array.isArray(calls)) return []
  return calls.map((call) => ({
    id: call.call_id ?? null,
    evidence_snapshot_id: evidenceSnapshotId ?? call.evidence_snapshot_id ?? null,
    role: call.role ?? null,
    role_key: call.role_key ?? null,
    label: ROLE_LABELS[call.role_key] ?? call.role ?? 'agent',
    stage: call.role_key ?? call.role ?? null,
    round: call.round ?? 1,
    attempt: call.attempt ?? 1,
    status: call.schema_ok === true ? 'ok' : 'error',
    schema_ok: call.schema_ok === true,
    repaired: call.attempt > 1,
    latency_ms: call.latency_ms ?? null,
    input_tokens: call.input_tokens ?? null,
    output_tokens: call.output_tokens ?? null,
    finish_kind: call.finish_kind ?? null,
    // 05 N01 / 01 §5.2: the per-call error was already in the audit but never
    // projected, so "which call broke" could not be read without the raw JSON.
    error: call.validation_errors?.[0]?.message ?? null,
    error_code: typeof call.error_code === 'string' ? call.error_code : null,
    error_message: clipText(call.error_message, 400),
    validation_errors: sanitizeValidationErrors(call.validation_errors),
    started_at_ms: num(call.started_at_ms),
    ended_at_ms: num(call.ended_at_ms),
    input_slice: call.input_slice && typeof call.input_slice === 'object' ? call.input_slice : null,
  }))
}

function roundEvidence(record) {
  const resultSummary = record?.result?.evidence
  const ref = record?.evidence
  const summary = resultSummary && typeof resultSummary === 'object' ? resultSummary : {}
  const service = ref && typeof ref === 'object' ? ref : {}
  const snapshotId = service.snapshot_id ?? summary.snapshot_id ?? null
  const serviceSha = service.sha256 ?? service.evidence_hash ?? null
  const viewHash = summary.evidence_hash ?? null
  if (!snapshotId && !serviceSha && !summary.snapshot_id) return null
  return {
    ...summary,
    ...service,
    snapshot_id: snapshotId,
    evidence_id: snapshotId,
    cycle_id: service.cycle_id ?? summary.cycle_id ?? record?.cycleId ?? null,
    // t57: the AI view hash is not the service md-evidence hash. The public
    // RoundView hash must be the service ref sha256; keep the derivation
    // view hash only under an explicit name for traceability.
    evidence_hash: serviceSha ?? viewHash,
    service_sha256: serviceSha,
    view_evidence_hash: viewHash !== serviceSha ? viewHash : null,
    verdict: service.verdict ?? summary.verdict ?? null,
  }
}

// ---- 05 N01: debate projection (01 §5.1 / D2) -----------------------------
// The two sides' arguments live in result.outputs and used to be dropped by the
// DTO, so the UI could only show "red vs blue" numbers, never the reasoning.
const STAGE_ORDER = [
  ['analysis', '分析'],
  ['red_opening', '红队·开场'],
  ['blue_opening', '蓝队·开场'],
  ['red_response', '红队·回应'],
  ['blue_response', '蓝队·回应'],
  ['decision', '决策'],
  ['risk', '风控'],
  ['execution_entry', '操盘手'],
]

function summarizePlanDraft(plan) {
  if (!plan || typeof plan !== 'object' || Array.isArray(plan)) return null
  const entry = plan.entry && typeof plan.entry === 'object' ? plan.entry : plan
  return {
    entry_style: typeof entry.style === 'string' ? entry.style : null,
    entry_price: num(entry.price),
    stop_loss: num(plan.stop_loss?.price),
    take_profit: num(plan.take_profit?.price),
    rr: num(plan.rr_to_target ?? plan.take_profit?.rr),
  }
}

function summarizeCase(caseOutput, { side, callId }) {
  if (!caseOutput || typeof caseOutput !== 'object' || Array.isArray(caseOutput)) return null
  const evidence = Array.isArray(caseOutput.evidence)
    ? caseOutput.evidence.slice(0, 5).filter((item) => item && typeof item === 'object').map((item) => ({
        ref: clipText(item.ref, 120),
        claim: clipText(item.claim, 180),
        weight: typeof item.weight === 'string' ? item.weight : null,
      }))
    : []
  const rebuttal = Array.isArray(caseOutput.rebuttal)
    ? caseOutput.rebuttal.slice(0, 3).filter((item) => item && typeof item === 'object').map((item) => ({
        target_claim: clipText(item.target_claim, 180),
        counter: clipText(item.counter, 240),
      }))
    : []
  return {
    side,
    call_id: callId ?? null,
    symbol: typeof caseOutput.symbol === 'string' ? caseOutput.symbol : null,
    tradeable: typeof caseOutput.tradeable === 'boolean' ? caseOutput.tradeable : null,
    confidence: num(caseOutput.confidence),
    thesis: clipText(caseOutput.thesis, 300),
    evidence,
    rebuttal,
    opponent_steelman: clipText(caseOutput.opponent_steelman, 300),
    no_case_reason: typeof caseOutput.no_case_reason === 'string' ? caseOutput.no_case_reason : null,
    plan_draft: summarizePlanDraft(caseOutput.plan_draft),
  }
}

function callIdForRole(calls, roleKey) {
  const hit = calls.find((call) => call && call.role_key === roleKey)
    ?? calls.find((call) => call && call.role === roleKey)
  return hit?.call_id ?? null
}

export function summarizeDebate(result) {
  if (!result || typeof result !== 'object') return null
  const outputs = result.outputs ?? {}
  const calls = Array.isArray(result.calls) ? result.calls : []
  const hasAny = ['redOpening', 'blueOpening', 'redResponse', 'blueResponse'].some((key) => outputs[key])
  const degraded = result.degraded === true || result.mode === 'opening_only'
  // Nothing to show is not a card: an analysis-only round never reached R1.
  if (!hasAny) return null
  const pseudo = calls.find((call) => call && Number(call.attempt) === 0 && call.role_key === 'debate_round2')
  const decision = outputs.decision && typeof outputs.decision === 'object' ? outputs.decision : null
  const selected = decision?.selected_case === 'red' || decision?.selected_case === 'blue'
    ? decision.selected_case
    : null
  return {
    mode: typeof result.debateMode === 'string' ? result.debateMode : null,
    requested_mode: typeof result.debateMode === 'string' ? result.debateMode : null,
    degraded,
    degrade_reason: degraded
      ? (pseudo?.validation_errors?.[0]?.keyword ?? pseudo?.validation_errors?.[0]?.message ?? 'R2_DEGRADED')
      : null,
    rounds: {
      r1: {
        red: summarizeCase(outputs.redOpening, { side: 'red', callId: callIdForRole(calls, 'red_opening') }),
        blue: summarizeCase(outputs.blueOpening, { side: 'blue', callId: callIdForRole(calls, 'blue_opening') }),
      },
      r2: {
        red: summarizeCase(outputs.redResponse, { side: 'red', callId: callIdForRole(calls, 'red_response') }),
        blue: summarizeCase(outputs.blueResponse, { side: 'blue', callId: callIdForRole(calls, 'blue_response') }),
      },
    },
    adopted: selected
      ? {
          side: selected,
          call_id: decision?.case_comparison ? (callIdForRole(calls, selected === 'red' ? 'red_response' : 'blue_response')
            ?? callIdForRole(calls, selected === 'red' ? 'red_opening' : 'blue_opening')) : null,
          decisive_factor: clipText(decision?.case_comparison?.decisive_factor, 220),
          opposing_risk: clipText(decision?.case_comparison?.opposing_risk, 220),
        }
      : null,
  }
}


/**
 * T15 — the deterministic gate verdict (ai/semantics.js#evaluateEntryPlan) is
 * the answer to "why didn't this round execute?". round-view receives the
 * Host's whitelisted projection (integration.js#gateAuditOf); this local copy
 * of the whitelist exists only because round-view must not import integration
 * (integration imports round-view — a cycle).
 */
function summarizeGate(gate) {
  if (!gate || typeof gate !== 'object' || Array.isArray(gate)) return null
  const quote = gate.quote && typeof gate.quote === 'object'
    ? {
      last: num(gate.quote.last),
      age_ms: num(gate.quote.age_ms),
      // Only two provenances are allowed; anything else is "unknown", never a price.
      source: gate.quote.source === 'pre_submit' || gate.quote.source === 'evidence_build'
        ? gate.quote.source
        : null,
    }
    : null
  return {
    ok: gate.ok === true,
    code: typeof gate.code === 'string' ? gate.code : null,
    sub_code: typeof gate.sub_code === 'string' ? gate.sub_code : null,
    detail: typeof gate.detail === 'string' ? gate.detail : null,
    checked_at: num(gate.checked_at),
    mode: typeof gate.mode === 'string' ? gate.mode : null,
    enforced: gate.enforced === true,
    quote,
  }
}

/** Per-stage rollup (01 §5.1 stages[]): who ran, how often, how long, what broke. */
export function summarizeStages(result, gate = null) {
  const calls = Array.isArray(result?.calls) ? result.calls : []
  return STAGE_ORDER.map(([stageKey, label]) => {
    const stageCalls = calls.filter((call) => call && (call.role_key === stageKey || (!call.role_key && call.role === stageKey)))
    const real = stageCalls.filter((call) => Number(call.attempt) !== 0)
    const attempts = new Set(real.map((call) => Number(call.attempt) || 1)).size
    const failedCall = firstFailedCall(real)
    const started = real.map((call) => num(call.started_at_ms)).filter((value) => value !== null)
    const ended = real.map((call) => num(call.ended_at_ms)).filter((value) => value !== null)
    const latency = real.reduce((sum, call) => sum + (num(call.latency_ms) ?? 0), 0)
    const tokensIn = real.reduce((sum, call) => sum + (num(call.input_tokens) ?? 0), 0)
    const tokensOut = real.reduce((sum, call) => sum + (num(call.output_tokens) ?? 0), 0)
    // T15: a stage that ran and then got blocked by the deterministic gate is
    // NOT 'ok' (the plan was not submitted) and NOT 'pending' (it did run). It is
    // 'blocked', and the reason comes from the gate detail — round-view does not
    // write a second explanation.
    const gateBlocked = stageKey === 'execution_entry' && gate && gate.ok === false
    return {
      stage_key: stageKey,
      label,
      status: gateBlocked ? 'blocked' : (real.length === 0 ? 'pending' : failedCall ? 'error' : 'ok'),
      attempts,
      started_at_ms: started.length > 0 ? Math.min(...started) : null,
      finished_at_ms: ended.length > 0 ? Math.max(...ended) : null,
      latency_ms: real.length > 0 ? latency : null,
      tokens: real.length > 0 ? { in: tokensIn, out: tokensOut } : null,
      error_code: failedCall?.error_code ?? (gateBlocked ? gate.code : null),
      blocked_by_gate: gateBlocked ? { code: gate.code ?? null, sub_code: gate.sub_code ?? null, detail: gate.detail ?? null, mode: gate.mode ?? null, enforced: gate.enforced === true } : null,
      calls: real.map((call) => call.call_id ?? null).filter(Boolean),
    }
  })
}

export function toRoundView(record) {
  const result = record?.result ?? null
  const outputs = result?.outputs ?? {}
  const evidence = roundEvidence(record)
  const evidenceSnapshotId = evidence?.snapshot_id ?? null
  const calls = Array.isArray(result?.calls)
    ? result.calls.map((call) => ({
        ...call,
        call_id: call.call_id ?? null,
        evidence_snapshot_id: evidenceSnapshotId ?? call.evidence_snapshot_id ?? null,
      }))
    : []
  // T15: the Host projects the gate into record.gateAudit (whitelist); older
  // records only have outputs.gate. Both go through the same whitelist here.
  const gateAudit = record?.gateAudit && typeof record.gateAudit === 'object' ? record.gateAudit : null
  const gate = summarizeGate(gateAudit?.gate ?? outputs.gate)
  const execution = summarizeExecution(outputs.execution, outputs.p1, outputs.p2)
  const analysis = summarizeAnalysis(outputs.analysis)
  const reasonCode = record?.reasonCode ?? result?.reasonCode ?? null
  const status = record?.status ?? 'unknown'
  const outcome = record?.outcome ?? result?.outcome ?? null
  const stopKind = stopKindOf({ reasonCode, status, outcome })
  // The chain already attaches attribution (05 N01); older records and test
  // fixtures do not, so the DTO derives the same thing from the same table.
  const attribution = result?.attribution ?? attributeRound({ reasonCode, calls: result?.calls ?? [], status })
  return {
    roundId: record?.roundId ?? null,
    cycleId: record?.cycleId ?? null,
    trigger: record?.trigger ?? 'manual',
    status,
    outcome,
    reasonCode,
    reasonDetail: summarizeReasonDetail(record, execution, analysis),
    reasonParams: summarizeReasonParams(record),
    reasonRaw: summarizeReasonRaw(record),
    reasonNotes: summarizeReasonNotes(record, analysis),
    headline: record?.headline ?? null,
    startedAt: record?.startedAt ?? null,
    finishedAt: record?.finishedAt ?? null,
    durationMs: record?.durationMs ?? null,
    symbol: record?.symbol ?? null,
    mode: result?.mode ?? (result?.degraded ? 'opening_only' : null),
    debateMode: result?.debateMode ?? null,
    degraded: result?.degraded ?? false,
    freshness: record?.freshness ?? null,
    decision: summarizeDecision(outputs.decision),
    // G2: the executed route of THIS round (frozen by the Host, persisted with
    // the record). null on records that predate the snapshot — never filled in
    // from /health or /state.
    route: summarizeRoute(record),
    // T15: "why didn't it execute" is data, not prose invented by the UI.
    gate,
    // T15: the pre-submit re-check (same evaluateEntryPlan, no re-quote). Kept
    // separate from `gate` on purpose: one is evidence_build, one is advisory.
    preSubmit: record?.preSubmitGate && typeof record.preSubmitGate === 'object'
      ? {
        ...summarizeGate(record.preSubmitGate),
        advisory_only: record.preSubmitGate.advisory_only === true,
        requote: record.preSubmitGate.requote ?? null,
      }
      : null,
    planGaps: gateAudit?.plan_gaps ?? null,
    failureCategory: gateAudit?.failure_category
      ?? (typeof result?.failureCategory === 'string' ? result.failureCategory : null),
    analysis,
    debate: summarizeDebate(result),
    attribution,
    stages: summarizeStages(result, gate),
    build: currentBuild(),
    chain: summarizeChain(result, record, { stopKind }),
    scorecard: summarizeScorecard(result?.scorecard, outputs.decision),
    risk: summarizeRisk(outputs.risk, record?.riskVerdict ?? null),
    execution,
    nodes: buildNodes(result?.calls, evidenceSnapshotId),
    aiCalls: calls,
    evidence,
    gaps: record?.gaps ?? [],
    service: record?.service ?? null,
    error: record?.error ?? null,
  }
}

export function toRoundSummary(record) {
  const view = toRoundView(record)
  return {
    roundId: view.roundId,
    cycleId: view.cycleId,
    trigger: view.trigger,
    route: view.route,
    status: view.status,
    outcome: view.outcome,
    reasonCode: view.reasonCode,
    reasonDetail: view.reasonDetail,
    headline: view.headline,
    startedAt: view.startedAt,
    finishedAt: view.finishedAt,
    durationMs: view.durationMs,
    symbol: view.symbol,
    // T15: the list must be able to say "blocked by the gate" without fetching
    // the full round detail.
    blockedByGate: Boolean(view.gate && view.gate.ok === false),
    gateCode: view.gate?.code ?? null,
    mode: view.mode,
    debateMode: view.debateMode,
    degraded: view.degraded,
  }
}
