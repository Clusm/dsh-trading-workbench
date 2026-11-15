/**
 * Deterministic case_scorecard. No LLM is involved.
 *
 * R1 supplies the tradable plan; R2 may only change final tradeable/confidence.
 * Confidence is the conservative min(opening, response); evidence strength is
 * the max of the two rounds (the response may add support, never remove data).
 */

import { DEFAULT_CAPS } from './constants.js'
import { evidenceStrength } from './semantics.js'

function num(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && /^[0-9]+(\.[0-9]+)?$/.test(value)) return Number(value)
  return null
}

function rubricScore(analysis, side) {
  const candidates = Array.isArray(analysis?.candidates) ? analysis.candidates : []
  const candidate = candidates.find((item) => item.side_bias === side || item.side_bias === 'BOTH')
  if (!candidate?.rubric) return 0.5
  const r = candidate.rubric
  const value = 0.25 * num(r.trend_alignment)
    + 0.25 * num(r.level_confluence)
    + 0.20 * num(r.momentum)
    + 0.15 * num(r.volatility_fit)
    + 0.15 * num(r.liquidity)
  return Number.isFinite(value) ? Math.max(0, Math.min(1, value)) : 0.5
}

function gradeFor(score) {
  if (score >= 0.80) return 'A'
  if (score >= 0.68) return 'B'
  if (score >= 0.55) return 'C'
  return 'D'
}

function rrFor(plan, fallbackPrice) {
  const entry = num(plan?.entry?.price) ?? num(fallbackPrice)
  const sl = num(plan?.stop_loss?.price)
  const tp = num(plan?.take_profit?.price)
  if (entry === null || sl === null || tp === null || Math.abs(entry - sl) === 0) return null
  return Math.abs(tp - entry) / Math.abs(entry - sl)
}

function stopPctFor(plan, fallbackPrice) {
  const entry = num(plan?.entry?.price) ?? num(fallbackPrice)
  const sl = num(plan?.stop_loss?.price)
  if (entry === null || sl === null || entry === 0) return null
  return Math.abs(entry - sl) / entry * 100
}

function assessSide({ opening, response, analysis, side, caps, primaryPrice, degraded }) {
  const plan = opening?.plan_draft ?? null
  const openingConfidence = num(opening?.confidence) ?? 0
  const responseConfidence = response ? num(response.confidence) : openingConfidence
  const confidence = Math.min(openingConfidence, responseConfidence)
  const openingEvidence = evidenceStrength(opening)
  const responseEvidence = response ? evidenceStrength(response) : openingEvidence
  const evidence = Math.max(openingEvidence, responseEvidence)
  const tradeable = Boolean(opening?.tradeable) && (response ? Boolean(response.tradeable) : true)
  const rr = rrFor(plan, primaryPrice)
  const stopPct = stopPctFor(plan, primaryPrice)
  const reasons = []

  if (!tradeable) reasons.push('NOT_TRADEABLE')
  if (!plan) reasons.push('PLAN_INVALID')
  if (plan && rr !== null && rr < caps.minRr) reasons.push('RR_TOO_LOW')
  if (plan && stopPct !== null && stopPct < caps.slMinPct) reasons.push('STOP_TOO_TIGHT')
  if (plan && stopPct !== null && stopPct > caps.slMaxPct) reasons.push('STOP_TOO_WIDE')
  if (confidence < 0.5) reasons.push('LOW_CONFIDENCE')
  if (evidence < 0.4 || (opening?.evidence ?? []).length < 3) reasons.push('LOW_EVIDENCE')
  if (plan && num(plan.risk_pct) > caps.maxRiskPct) reasons.push('RISK_OVER_CAP')
  if (plan && num(plan.leverage) > caps.maxLeverage) reasons.push('LEVERAGE_OVER_CAP')
  if (response && (response.rebuttal ?? []).length === 0) reasons.push('REBUTTAL_MISSING')

  const gateCode = {
    NOT_TRADEABLE: 'FAIL_NOT_TRADEABLE',
    PLAN_INVALID: 'FAIL_PLAN',
    RR_TOO_LOW: 'FAIL_RR',
    STOP_TOO_TIGHT: 'FAIL_STOP',
    STOP_TOO_WIDE: 'FAIL_STOP',
    LOW_CONFIDENCE: 'FAIL_CONFIDENCE',
    LOW_EVIDENCE: 'FAIL_EVIDENCE',
    RISK_OVER_CAP: 'FAIL_RISK',
    LEVERAGE_OVER_CAP: 'FAIL_LEVERAGE',
    REBUTTAL_MISSING: 'FAIL_REBUTTAL',
  }
  const gate = reasons.length === 0 ? 'PASS' : (gateCode[reasons[0]] ?? `FAIL_${reasons[0]}`)
  return {
    gate,
    gate_reasons: reasons,
    tradeable,
    rubric_score: Number(rubricScore(analysis, side).toFixed(6)),
    grade: gradeFor(rubricScore(analysis, side)),
    rr: rr === null ? null : Number(rr.toFixed(6)),
    stop_distance_pct: stopPct === null ? null : Number(stopPct.toFixed(4)),
    risk_pct: plan ? num(plan.risk_pct) : null,
    leverage: plan ? plan.leverage : null,
    opening_confidence: openingConfidence,
    response_confidence: responseConfidence,
    confidence,
    evidence_strength: Number(evidence.toFixed(6)),
    rebuttal_count: response ? (response.rebuttal ?? []).length : 0,
    plan_round: 1,
    response_used: Boolean(response) && !degraded,
  }
}

export function computeScorecard({ analysis, redOpening, blueOpening, redResponse = null, blueResponse = null, primaryPrice, caps = DEFAULT_CAPS, degraded = false }) {
  const red = assessSide({ opening: redOpening, response: redResponse, analysis, side: 'LONG', caps, primaryPrice, degraded })
  const blue = assessSide({ opening: blueOpening, response: blueResponse, analysis, side: 'SHORT', caps, primaryPrice, degraded })

  const bothPass = red.gate === 'PASS' && blue.gate === 'PASS'
  const tie = bothPass
    && Math.abs(red.rubric_score - blue.rubric_score) < 0.05
    && Math.abs((red.rr ?? 0) - (blue.rr ?? 0)) < 0.2
    && Math.abs(red.evidence_strength - blue.evidence_strength) < 0.1

  let debateQuality
  if (!bothPass && red.gate !== 'PASS' && blue.gate !== 'PASS') debateQuality = 'both_weak'
  else if (!bothPass || tie) debateQuality = 'thin'
  else debateQuality = 'decisive'

  return {
    red,
    blue,
    debate_quality: debateQuality,
    tie,
    degraded: Boolean(degraded),
    computed_at: new Date().toISOString(),
    thresholds: {
      min_rr: caps.minRr,
      min_confidence: 0.5,
      min_evidence_strength: 0.4,
      tie_epsilon: 0.05,
      tie_rr_epsilon: 0.2,
      tie_evidence_epsilon: 0.1,
    },
  }
}

export { rubricScore, rrFor, stopPctFor }
