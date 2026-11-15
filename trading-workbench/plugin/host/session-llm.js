/**
 * Session-local LLM fixture ("会话内 LLM") — a deterministic stand-in for
 * ctx.llm.stream during testing.
 *
 * Why this exists: the 12 real end-to-end rounds on 2026-11-09 produced 8
 * failures, 6 of them in the *first* analysis call, caused by the external
 * provider (MissingSessionID, max-tokens truncation, 28s timeout, off-schema
 * JSON). Those are model-side failures, not Host/Service defects. Injecting a
 * session-local LLM removes that variable so the integration can be tested
 * repeatably, offline, without an API key and without any order.
 *
 * The responses below are authored in-conversation against the seven frozen
 * role schemas (docs/02-AI决策链.md). They are data, not a model: no network,
 * no tools, no Session, no provider key. Production keeps using ctx.llm unless
 * WORKBENCH_LLM_MODE=session_fixture is set explicitly.
 *
 * Scripts:
 *  - 'no_trade' (default): analysis stands aside -> NO_CANDIDATE. Exercises
 *    cycle creation, evidence, audit persistence, finalize and RoundView.
 *    Safe against any live evidence because it does not depend on prices.
 *  - 'full_chain': 8 calls (analysis + R1x2 + R2x2 + decision + risk +
 *    execution). Prices are the fixed offline-evidence values; use it with the
 *    frozen evidence fixture in tests, not against a live market snapshot.
 */

export const SESSION_LLM_TRANSPORT = 'session-llm-fixture/1'
export const SESSION_LLM_SCRIPTS = Object.freeze(['no_trade', 'full_chain'])

const FIXED_PRICE = 68234.5

const RED_PLAN = Object.freeze({
  entry: { style: 'MARKET', price: FIXED_PRICE, zone_low: 68100.0, zone_high: 68250.0 },
  stop_loss: { price: 67410.2, distance_pct: 1.21, basis: 'structure' },
  take_profit: { price: 69600.0, rr: 1.66 },
  risk_pct: 0.8,
  leverage: 3,
  time_stop_hours: 24,
  rr_to_target: 1.66,
})

const BLUE_PLAN = Object.freeze({
  entry: { style: 'MARKET', price: FIXED_PRICE },
  stop_loss: { price: 69050.0, distance_pct: 1.20, basis: 'structure' },
  take_profit: { price: 67400.0, rr: 1.02 },
  risk_pct: 0.8,
  leverage: 3,
  time_stop_hours: 24,
  rr_to_target: 1.02,
})

function analysisOutput(cycleId, { standAside }) {
  const base = {
    schema_version: 'analysis/1',
    cycle_id: cycleId,
    agent: 'analysis',
    mode: 'FULL',
    market_read: {
      regime: 'trend_up',
      breadth: { up: 14, down: 6, median_pct_24h: 0.8 },
      volatility: { state: 'normal', btc_atr_pct_1h: 0.42 },
      derivatives: { funding_state: 'crowded_long', oi_state: 'rising' },
      notes: ['资金费偏高，但价格未出现放量滞涨。'],
    },
    symbols_analyzed: ['BTCUSDT', 'ETHUSDT'],
    candidates: [],
    primary_symbol: null,
    stand_aside: true,
    stand_aside_reason: 'NO_CANDIDATE',
    confidence: 0.7,
    evidence_refs: ['symbols.BTCUSDT.price.last', 'symbols.BTCUSDT.timeframes.15m.rsi14'],
    warnings: [],
  }
  if (standAside) return base
  return {
    ...base,
    candidates: [
      {
        symbol: 'BTCUSDT',
        side_bias: 'BOTH',
        setup_type: 'retest',
        thesis: '15m 回踩 EMA20 未破，1h 结构仍为 HL；空头只有阻力与资金费。',
        trigger: { description: '15m 回踩 68100–68200 不破并重新站上 EMA20', price: 68100.0, valid_until: '2026-11-09T03:45:20Z' },
        invalidation: { price: 67410.2, condition: '15m 收盘跌破 EMA20 且 1h 转为 LH/LL' },
        rubric: { trend_alignment: 0.9, level_confluence: 0.8, momentum: 0.7, volatility_fit: 0.8, liquidity: 0.9 },
        key_evidence_refs: ['symbols.BTCUSDT.price.last', 'symbols.BTCUSDT.timeframes.15m.rsi14'],
        flags: ['funding_extreme'],
      },
    ],
    primary_symbol: 'BTCUSDT',
    stand_aside: false,
    stand_aside_reason: null,
  }
}

function debateOutput(cycleId, payload, { side, round, plan }) {
  const isRed = side === 'LONG'
  const respondsTo = payload.task?.responds_to ?? null
  return {
    schema_version: 'debate_case/1',
    cycle_id: cycleId,
    agent: isRed ? 'red_team' : 'blue_team',
    symbol: payload.task?.symbol ?? 'BTCUSDT',
    side,
    round,
    responds_to: round === 2 ? respondsTo : null,
    rebuttal: round === 2
      ? [
          { target_claim: isRed ? '资金费拥挤，多头拥挤' : '15m/1h 趋势向上，回踩可延续', counter: isRed ? '拥挤不等于反转，价格未放量滞涨。' : '趋势仍在，但双阻力未被突破。' },
        ]
      : [],
    tradeable: Boolean(plan),
    confidence: isRed ? (round === 2 ? 0.70 : 0.68) : (round === 2 ? 0.52 : 0.55),
    thesis: isRed ? '15m 回踩 EMA20 未破，1h 趋势向上，回踩做多。' : '双阻力 + 资金费拥挤，冲高失败做空；但逆 1h 趋势。',
    evidence: [
      { ref: 'symbols.BTCUSDT.timeframes.15m.rsi14', claim: '15m RSI 61，未过热', weight: 'high' },
      { ref: 'symbols.BTCUSDT.price.last', claim: '现价位于 EMA20 上方', weight: 'high' },
      { ref: 'symbols.BTCUSDT.levels.resistances', claim: '上方 68820/69100 双阻力', weight: 'medium' },
    ],
    trigger: { type: isRed ? 'retest' : 'rejection', description: isRed ? '回踩 68100–68200 不破' : '冲高 68820 失败并跌回 68600', price: isRed ? 68100.0 : 68820.0, valid_until: '2026-11-09T03:45:20Z' },
    plan_draft: plan,
    invalidation: { price: isRed ? 67410.2 : 69050.0, condition: isRed ? '15m 收盘跌破 EMA20' : '15m 收盘站上 69050' },
    opponent_steelman: isRed ? '空头有资金费与双阻力，但缺少结构破坏。' : '多头有趋势与 EMA20，但双阻力与资金费仍在。',
    no_case_reason: null,
    warnings: [],
  }
}

function decisionOutput(cycleId) {
  return {
    schema_version: 'decision/1',
    cycle_id: cycleId,
    agent: 'decision',
    action: 'TRADE',
    side: 'LONG',
    selected_case: 'red',
    symbol: 'BTCUSDT',
    confidence: 0.66,
    thesis: '红队 R1/R2 保持回踩做多，蓝队 R2 未解决 R:R 1.02 的问题。',
    case_comparison: {
      red: { strength: 'strong', decisive_evidence: '15m 回踩 EMA20 未破 + 1h up', fatal_flaw: null },
      blue: { strength: 'moderate', decisive_evidence: '双阻力 + 资金费拥挤', fatal_flaw: 'R:R 1.02 < 1.2' },
      decisive_factor: '红队 gate PASS，蓝队 gate FAIL_RR',
      opposing_risk: '资金费拥挤，若放量突破 68820 可能逼空。',
    },
    score_override: false,
    override_rationale: null,
    plan: { ...RED_PLAN, risk_pct: 0.7 },
    no_trade_reason: null,
    invalidation: { price_event: '15m 收盘跌破 67410.2', time: '24h time stop', review_at: 'next cycle' },
    evidence_refs: ['symbols.BTCUSDT.price.last'],
    warnings: [],
  }
}

function riskOutput(cycleId) {
  return {
    schema_version: 'risk_advice/1',
    cycle_id: cycleId,
    agent: 'risk',
    recommendation: 'APPROVE',
    size_multiplier: 1,
    max_risk_pct: 1.0,
    max_leverage: 10,
    concerns: [],
    conditions: [],
    reasoning: '风险在预算内，保留保护单，按计划执行。',
    confidence: 0.8,
    warnings: [],
  }
}

function executionOutput(cycleId) {
  return {
    schema_version: 'execution_entry/1',
    cycle_id: cycleId,
    agent: 'execution',
    mode: 'ENTRY',
    executable: true,
    blocked_reason: null,
    symbol: 'BTCUSDT',
    side: 'LONG',
    order_plan: {
      symbol: 'BTCUSDT',
      side: 'LONG',
      action: 'open',
      entry_type: 'MARKET',
      leverage: 3,
      qty: '0.067',
      sl: '67410.2',
      tp: '69600.0',
      confidence: 0.66,
      reasoning_ref: 'decision:session-llm-fixture',
      execution_policy: { max_slippage_bps: 15, on_protection_failure: 'CLOSE_POSITION', protection_retry_max: 1 },
    },
    preflight: {
      reference_price: FIXED_PRICE,
      notional_usd: 4571.71,
      risk_usd: 55.23,
      risk_pct: 0.552,
      margin_required_usd: 1523.9,
      estimated_fee_usd: 1.83,
      checks: [
        { name: 'R01_SYMBOL', ok: true },
        { name: 'R02_POSITION', ok: true },
        { name: 'R04_LEVERAGE', ok: true },
        { name: 'R05_RISK', ok: true },
        { name: 'R08_SLTP', ok: true },
      ],
    },
    rationale: '按已批准计划市价开仓，同时挂 SL/TP；qty 已按 step 向下取整。',
    warnings: [],
  }
}

function roleKeyFrom(payload) {
  const role = payload.task?.role
  const round = payload.task?.round
  if (role === 'analysis') return 'analysis'
  if (role === 'red_team') return round === 2 ? 'red_response' : 'red_opening'
  if (role === 'blue_team') return round === 2 ? 'blue_response' : 'blue_opening'
  if (role === 'decision') return 'decision'
  if (role === 'risk') return 'risk'
  if (role === 'execution') {
    return payload.output_schema === 'execution_manage/1' ? 'execution_manage' : 'execution_entry'
  }
  if (role === 'review') return 'review'
  throw new Error(`session-llm: unknown role ${role}`)
}

function parsePayload(options) {
  const text = options?.messages?.[0]?.content?.[0]?.text ?? ''
  const base = text.split('\n\nYour previous output failed validation')[0]
  return JSON.parse(base)
}

function outputFor(roleKey, payload, cycleId, script) {
  if (roleKey === 'analysis') return analysisOutput(cycleId, { standAside: script === 'no_trade' })
  if (script === 'no_trade') throw new Error(`session-llm: no_trade script must stop at analysis, got ${roleKey}`)
  switch (roleKey) {
    case 'red_opening': return debateOutput(cycleId, payload, { side: 'LONG', round: 1, plan: RED_PLAN })
    case 'blue_opening': return debateOutput(cycleId, payload, { side: 'SHORT', round: 1, plan: BLUE_PLAN })
    case 'red_response': return debateOutput(cycleId, payload, { side: 'LONG', round: 2, plan: RED_PLAN })
    case 'blue_response': return debateOutput(cycleId, payload, { side: 'SHORT', round: 2, plan: BLUE_PLAN })
    case 'decision': return decisionOutput(cycleId)
    case 'risk': return riskOutput(cycleId)
    case 'execution_entry': return executionOutput(cycleId)
    default: throw new Error(`session-llm: unscripted role ${roleKey}`)
  }
}

async function* oneShotStream(text) {
  yield { type: 'text-delta', text }
  yield { type: 'usage', usage: { inputTokens: 1200, outputTokens: 240, totalTokens: 1440 } }
  yield { type: 'finish', reason: { kind: 'stop' } }
}

/**
 * Build a `llm.stream(options)`-shaped object. `optionsSeen` lets tests assert
 * that no tools / no external transport leaked into the request.
 */
export function createSessionLlm({ script = 'no_trade', cycleId = null } = {}) {
  if (!SESSION_LLM_SCRIPTS.includes(script)) {
    throw new Error(`session-llm: unknown script ${script}`)
  }
  const optionsSeen = []
  const llm = {
    stream(options) {
      optionsSeen.push(options)
      const payload = parsePayload(options)
      const resolvedCycleId = cycleId ?? payload.evidence?.cycle_id ?? payload.cycle_id ?? null
      if (!resolvedCycleId) throw new Error('session-llm: cannot resolve cycle_id from payload')
      const output = outputFor(roleKeyFrom(payload), payload, resolvedCycleId, script)
      return oneShotStream(JSON.stringify(output))
    },
  }
  return {
    llm,
    optionsSeen,
    script,
    transport: SESSION_LLM_TRANSPORT,
    isSessionFixture: true,
  }
}
