/**
 * Deterministic evidence projection and per-role slices.
 *
 * M1.3a does not fetch market data. It accepts an already-produced
 * ai-evidence-view/1-like object (or a md-evidence-like fixture), verifies its
 * identity fields, preserves evidence_hash/age_ms, and gives each agent only
 * the slice its prompt is allowed to see. No role receives the full 8-deep
 * payload by default except analysis.
 */

import { createHash } from 'node:crypto'
import { EVIDENCE_MAX_AGE_MS, EVIDENCE_SLICE_TOKENS } from './constants.js'
import { ChainError } from './errors.js'

function isObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

export function stableStringify(value) {
  if (Array.isArray(value)) return `[${value.map((item) => stableStringify(item)).join(',')}]`
  if (isObject(value)) {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stableStringify(value[key])}`).join(',')}}`
  }
  return JSON.stringify(value)
}

export function hashEvidence(view) {
  const copy = structuredClone(view)
  delete copy.evidence_hash
  delete copy.age_ms
  return `sha256:${createHash('sha256').update(stableStringify(copy)).digest('hex')}`
}

export function estimateTokens(value) {
  return Math.ceil(Buffer.byteLength(JSON.stringify(value), 'utf8') / 4)
}

function baseView(input, opts) {
  const now = typeof opts.now === 'function' ? opts.now() : (opts.now ?? Date.now())
  const producedAt = input.produced_at ?? input.producedAt
  if (typeof producedAt !== 'string' || Number.isNaN(Date.parse(producedAt))) {
    throw new ChainError('EVIDENCE_INVALID', 'evidence.produced_at must be an ISO timestamp')
  }
  const producedMs = Date.parse(producedAt)
  if (producedMs > now) {
    throw new ChainError('EVIDENCE_INVALID', 'evidence.produced_at must not be in the future')
  }
  if (!input.snapshot_id && !input.snapshotId) throw new ChainError('EVIDENCE_INVALID', 'evidence.snapshot_id is required')
  if (!input.cycle_id && !input.cycleId) throw new ChainError('EVIDENCE_INVALID', 'evidence.cycle_id is required')
  if (!isObject(input.symbols)) throw new ChainError('EVIDENCE_INVALID', 'evidence.symbols must be an object')
  const primary = input.primary_symbol ?? input.primarySymbol
  if (typeof primary !== 'string' || !isObject(input.symbols[primary])) {
    throw new ChainError('EVIDENCE_INVALID', 'evidence.primary_symbol must point at symbols[primary]')
  }
  if (!isObject(input.data_health) || typeof input.data_health.verdict !== 'string') {
    throw new ChainError('EVIDENCE_INVALID', 'evidence.data_health.verdict is required')
  }
  // age is server-derived; an explicit value is only a consistency claim.
  const computedAge = now - producedMs
  const explicitAge = Number.isFinite(input.age_ms) ? input.age_ms
    : Number.isFinite(input.ageMs) ? input.ageMs : null
  if (explicitAge !== null && Math.abs(explicitAge - computedAge) > 5_000) {
    throw new ChainError('EVIDENCE_INVALID', 'evidence.age_ms disagrees with produced_at')
  }
  const ageMs = computedAge

  const asOf = input.as_of ?? input.asOf ?? producedAt
  if (typeof asOf !== 'string' || Number.isNaN(Date.parse(asOf))) {
    throw new ChainError('EVIDENCE_INVALID', 'evidence.as_of must be an ISO timestamp')
  }
  if (Date.parse(asOf) > now) {
    throw new ChainError('EVIDENCE_INVALID', 'evidence.as_of must not be in the future')
  }

  const view = {
    schema_version: 'ai-evidence-view/1',
    snapshot_id: input.snapshot_id ?? input.snapshotId,
    cycle_id: input.cycle_id ?? input.cycleId,
    produced_at: producedAt,
    as_of: asOf,
    data_health: structuredClone(input.data_health),
    market: input.market ?? { regime: 'unknown' },
    primary_symbol: primary,
    symbols: structuredClone(input.symbols),
    delivery: input.delivery ?? {
      order_gate_ttl_s: 180,
      price_ttl_s: 15,
      audience: 'operator',
      render: 'md-evidence-prompt/1',
    },
    age_ms: ageMs,
    evidence_hash: null,
  }
  assertNoFutureBars(view, now)
  const computedHash = hashEvidence(view)
  const explicitHash = typeof input.evidence_hash === 'string' ? input.evidence_hash
    : typeof input.evidenceHash === 'string' ? input.evidenceHash : null
  if (explicitHash !== null && explicitHash !== computedHash) {
    throw new ChainError('EVIDENCE_INVALID', 'evidence.evidence_hash does not match canonical content')
  }
  view.evidence_hash = computedHash
  return view
}

function timeMs(value) {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value > 1e12 ? value : value * 1000
  }
  if (typeof value === 'string') {
    const parsed = Date.parse(value)
    return Number.isNaN(parsed) ? null : parsed
  }
  return null
}

function assertNoFutureBars(view, now) {
  const checkList = (items, path) => {
    for (const [index, item] of (items ?? []).entries()) {
      if (!isObject(item)) continue
      for (const key of ['t', 'open_time', 'openTime', 'close_time', 'closeTime', 'time', 'timestamp']) {
        const ms = timeMs(item[key])
        if (ms !== null && ms > now) throw new ChainError('EVIDENCE_INVALID', `${path}[${index}].${key} is in the future`)
      }
    }
  }
  for (const [symbol, symbolView] of Object.entries(view.symbols)) {
    if (!isObject(symbolView) || !isObject(symbolView.timeframes)) continue
    for (const [tf, block] of Object.entries(symbolView.timeframes)) {
      if (!isObject(block)) continue
      const last = timeMs(block.last_closed_at)
      if (last !== null && last > now) throw new ChainError('EVIDENCE_INVALID', `symbols.${symbol}.timeframes.${tf}.last_closed_at is in the future`)
      checkList(block.candles, `symbols.${symbol}.timeframes.${tf}.candles`)
    }
    checkList(symbolView.candles, `symbols.${symbol}.candles`)
    checkList(symbolView.bars, `symbols.${symbol}.bars`)
  }
  checkList(view.bars, 'bars')
}

export function buildEvidenceView(input, opts = {}) {
  if (!isObject(input)) throw new ChainError('EVIDENCE_INVALID', 'evidence must be an object')
  return baseView(input, opts)
}

function compactSymbol(symbolView, { keepCandles = 12, keepTimeframes = ['15m', '1h'] } = {}) {
  const out = {}
  for (const key of ['price', 'trend', 'levels', 'derivatives', 'flags', 'structure', 'age_s']) {
    if (symbolView[key] !== undefined) out[key] = structuredClone(symbolView[key])
  }
  if (isObject(symbolView.timeframes)) {
    out.timeframes = {}
    for (const tf of keepTimeframes) {
      if (!isObject(symbolView.timeframes[tf])) continue
      const block = structuredClone(symbolView.timeframes[tf])
      if (Array.isArray(block.candles)) block.candles = block.candles.slice(-keepCandles)
      out.timeframes[tf] = block
    }
  } else if (symbolView.timeframes !== undefined) {
    out.timeframes = structuredClone(symbolView.timeframes)
  }
  return out
}

function compactDecisionSymbol(symbolView) {
  const out = {}
  for (const key of ['price', 'trend', 'levels', 'flags', 'atr_pct_15m', 'derivatives']) {
    if (symbolView[key] !== undefined) out[key] = structuredClone(symbolView[key])
  }
  if (isObject(symbolView.timeframes)) {
    out.timeframes = {}
    for (const tf of ['15m', '1h']) {
      const block = symbolView.timeframes[tf]
      if (!isObject(block)) continue
      out.timeframes[tf] = {
        trend: block.trend,
        rsi14: block.rsi14,
        atr_pct: block.atr_pct,
        ema20: block.ema20,
        ema50: block.ema50,
        range_pos: block.structure?.range_pos,
      }
    }
  }
  return out
}

function compactRiskSymbol(symbolView) {
  const price = isObject(symbolView.price) ? symbolView.price : {}
  return {
    price: {
      last: price.last,
      mark: price.mark,
      bid: price.bid,
      ask: price.ask,
      spread_bps: price.spread_bps,
      age_s: price.age_s,
    },
    atr_pct_15m: symbolView.atr_pct_15m ?? symbolView.timeframes?.['15m']?.atr_pct ?? null,
    flags: structuredClone(symbolView.flags ?? []),
  }
}

function compactExecutionSymbol(symbolView, exchange) {
  const price = isObject(symbolView.price) ? symbolView.price : {}
  return {
    price: {
      last: price.last,
      bid: price.bid,
      ask: price.ask,
      spread_bps: price.spread_bps,
      age_s: price.age_s,
    },
    atr_pct_15m: symbolView.atr_pct_15m ?? symbolView.timeframes?.['15m']?.atr_pct ?? null,
    exchange: exchange ?? symbolView.exchange ?? null,
  }
}

function fitBudget(data, budgetTokens) {
  let current = structuredClone(data)
  let truncated = false
  const trimStep = () => {
    let changed = false
    if (isObject(current.symbols)) {
      for (const symbolView of Object.values(current.symbols)) {
        if (isObject(symbolView.timeframes)) {
          for (const tf of Object.keys(symbolView.timeframes)) {
            const block = symbolView.timeframes[tf]
            if (isObject(block) && Array.isArray(block.candles) && block.candles.length > 8) {
              block.candles = block.candles.slice(-8)
              changed = true
            }
          }
        }
      }
    }
    if (!changed && isObject(current.symbols) && Object.keys(current.symbols).length > 1) {
      const primary = current.primary_symbol
      for (const key of Object.keys(current.symbols)) {
        if (key !== primary) {
          delete current.symbols[key]
          changed = true
        }
      }
    }
    if (!changed && isObject(current.symbols)) {
      for (const symbolView of Object.values(current.symbols)) {
        if (isObject(symbolView.timeframes)) {
          for (const key of Object.keys(symbolView.timeframes)) {
            if (!['15m', '1h'].includes(key)) {
              delete symbolView.timeframes[key]
              changed = true
            }
          }
        }
      }
    }
    return changed
  }
  while (estimateTokens(current) > budgetTokens) {
    if (!trimStep()) break
    truncated = true
  }
  return { data: current, truncated, estimatedTokens: estimateTokens(current) }
}

const ROLE_SLICE = {
  analysis: EVIDENCE_SLICE_TOKENS.analysis,
  debate: EVIDENCE_SLICE_TOKENS.debate,
  decision: EVIDENCE_SLICE_TOKENS.decision,
  risk: EVIDENCE_SLICE_TOKENS.risk,
  execution: EVIDENCE_SLICE_TOKENS.execution,
  review: EVIDENCE_SLICE_TOKENS.review,
}

/**
 * Return { data, meta } for a role. data is safe to put in the prompt; meta is
 * for audit/tests and is never treated as evidence by the model.
 */
export function sliceEvidence(view, role, opts = {}) {
  const primary = view.primary_symbol
  const symbolView = view.symbols[primary]
  if (!isObject(symbolView)) throw new ChainError('EVIDENCE_INVALID', `primary symbol ${primary} missing`)
  const budget = ROLE_SLICE[role] ?? ROLE_SLICE.decision
  const base = {
    schema_version: view.schema_version,
    snapshot_id: view.snapshot_id,
    cycle_id: view.cycle_id,
    produced_at: view.produced_at,
    as_of: view.as_of,
    data_health: structuredClone(view.data_health),
    delivery: structuredClone(view.delivery),
    age_ms: view.age_ms,
    evidence_hash: view.evidence_hash,
    primary_symbol: primary,
  }

  let data
  if (role === 'analysis') {
    data = {
      ...base,
      market: structuredClone(view.market),
      universe: structuredClone(view.universe ?? null),
      symbols: structuredClone(view.symbols),
    }
  } else if (role === 'debate') {
    data = { ...base, symbol: compactSymbol(symbolView) }
  } else if (role === 'decision') {
    data = { ...base, symbol: compactDecisionSymbol(symbolView) }
  } else if (role === 'risk') {
    data = { ...base, symbol: compactRiskSymbol(symbolView) }
  } else if (role === 'execution') {
    data = {
      ...base,
      symbol: compactExecutionSymbol(symbolView, opts.exchange),
      reference_price: symbolView.price?.last ?? null,
    }
  } else if (role === 'review') {
    data = { ...base, metrics: structuredClone(view.metrics ?? view.review_metrics ?? {}) }
  } else {
    data = { ...base, symbol: compactDecisionSymbol(symbolView) }
  }

  const fitted = budget > 0 ? fitBudget(data, budget) : { data, truncated: false, estimatedTokens: 0 }
  return {
    data: fitted.data,
    meta: {
      role,
      budget_tokens: budget,
      estimated_tokens: fitted.estimatedTokens,
      truncated: fitted.truncated,
      evidence_hash: view.evidence_hash,
      age_ms: view.age_ms,
      snapshot_id: view.snapshot_id,
      symbol_count: Object.keys(fitted.data.symbols ?? (fitted.data.symbol ? { [primary]: true } : {})).length,
    },
  }
}

export function evidenceAgeErrors(view, maxAgeMs = EVIDENCE_MAX_AGE_MS) {
  const errors = []
  if (!Number.isFinite(view.age_ms) || view.age_ms < 0) {
    errors.push({ path: '$.age_ms', message: 'age_ms must be finite and >= 0', keyword: 'semantic' })
  } else if (view.age_ms > maxAgeMs) {
    errors.push({ path: '$.age_ms', message: `evidence is stale: ${view.age_ms}ms > ${maxAgeMs}ms`, keyword: 'semantic' })
  }
  if (view.data_health?.verdict !== 'GOOD') {
    errors.push({ path: '$.data_health.verdict', message: 'data verdict is not GOOD', keyword: 'semantic' })
  }
  return errors
}

export function evidenceSummary(view) {
  return {
    snapshot_id: view.snapshot_id,
    cycle_id: view.cycle_id,
    evidence_hash: view.evidence_hash,
    age_ms: view.age_ms,
    produced_at: view.produced_at,
    primary_symbol: view.primary_symbol,
    verdict: view.data_health?.verdict ?? null,
  }
}
