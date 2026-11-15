/**
 * M1.3a fake-LLM regression suite.
 *
 * Run: node trading-workbench/plugin/test/ai-chain.test.mjs
 * No network, no service, no DB, no real model.
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import {
  runChain,
  runChainFromContext,
  buildEvidenceView,
  sliceEvidence,
  buildPrompt,
  COMMON_SYSTEM,
  SCHEMAS,
  validateSchema,
  validateSemantics,
} from '../host/ai/index.js'
import { standAsideDetail } from '../host/ai/chain.js'
import {
  CHAIN_DEADLINE_MS,
  CHAIN_VARIANTS,
  DEFAULT_CHAIN_VARIANT,
  EVIDENCE_SLICE_TOKENS,
  MAX_OUTPUT_TOKENS,
  PROMPT_VERSIONS,
  ROLE_MAX_TOKENS,
  ROLE_TIMEOUT_MS,
  VARIANT_ROLES,
} from '../host/ai/constants.js'
import { describePlanGaps, evaluateEntryPlan } from '../host/ai/semantics.js'
import { entryPlanBasis } from '../host/ai/prompts.js'
import {
  FAILURE_CATEGORIES,
  categorizeFailure,
  deterministicDecision,
  deterministicNoTrade,
  resolveChainVariant,
  selectDeterministicCase,
  summarizeFailures,
} from '../host/ai/chain.js'
import { isRetryableLlmError } from '../host/ai/llm.js'
import { ChainError } from '../host/ai/errors.js'

const CYCLE_ID = 'cyc-20261109T014520Z-7f3a1c'
test('all roles request Chinese prose while preserving machine protocol values', () => {
  // T17：提示文本在 T16 被硬化（zone/valid_until 硬要求），版本号必须分开记。
  const expectedVersions = {
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
  }
  for (const [role, version] of Object.entries(expectedVersions)) {
    const prompt = buildPrompt(role)
    assert.match(prompt.system, /自然语言内容必须使用简体中文/)
    assert.match(prompt.system, /不得翻译或改写机器协议值/)
    assert.equal(prompt.promptVersion, version)
    // 版本号只存在于审计字段，不嵌进提示文本：bump 只改标识，不改模型输入。
    assert.equal(prompt.system.includes(prompt.promptVersion), false)
    assert.equal(prompt.user.includes(prompt.promptVersion), false)
  }
})
const PRODUCED_AT = '2026-11-09T01:45:20Z'
const NOW = Date.parse('2026-11-09T01:47:00Z')

const RED_PLAN = {
  entry: { style: 'MARKET', price: 68234.5, zone_low: 68100.0, zone_high: 68250.0 },
  stop_loss: { price: 67410.2, distance_pct: 1.21, basis: 'structure' },
  take_profit: { price: 69600.0, rr: 1.66 },
  risk_pct: 0.8,
  leverage: 3,
  time_stop_hours: 24,
  rr_to_target: 1.66,
}

const BLUE_PLAN = {
  entry: { style: 'MARKET', price: 68234.5 },
  stop_loss: { price: 69050.0, distance_pct: 1.20, basis: 'structure' },
  take_profit: { price: 67400.0, rr: 1.02 },
  risk_pct: 0.8,
  leverage: 3,
  time_stop_hours: 24,
  rr_to_target: 1.02,
}

function symbolView(overrides = {}) {
  return {
    price: { last: 68234.5, mark: 68240.0, bid: 68234.0, ask: 68235.0, spread_bps: 1.5, age_s: 1 },
    trend: { '15m': 'up', '1h': 'up', '4h': 'up', '1d': 'up', aligned: true },
    timeframes: {
      '15m': { trend: 'up', rsi14: 61.2, atr_pct: 0.61, ema20: 67410.2, ema50: 66980.1, candles: Array.from({ length: 20 }, (_, i) => ({ c: 68000 + i })) },
      '1h': { trend: 'up', rsi14: 58.1, atr_pct: 0.42, ema20: 67100.0, ema50: 66500.0, candles: Array.from({ length: 20 }, (_, i) => ({ c: 67500 + i })) },
      '4h': { trend: 'up', rsi14: 55.2, atr_pct: 0.9, candles: Array.from({ length: 20 }, () => ({ c: 65000 })) },
      '1d': { trend: 'up', rsi14: 54.1, atr_pct: 1.4, candles: Array.from({ length: 20 }, () => ({ c: 60000 })) },
    },
    levels: { supports: [68100, 67410.2], resistances: [68820, 69100] },
    derivatives: { funding_bp: 6.2, oi_change_1h_pct: 2.8 },
    flags: ['funding_extreme'],
    ...overrides,
  }
}

function evidenceFixture() {
  return {
    snapshot_id: 'snap-20261109T014520Z-7f3a1c',
    cycle_id: CYCLE_ID,
    produced_at: PRODUCED_AT,
    as_of: '2026-11-09T01:45:20Z',
    data_health: { verdict: 'GOOD', score: 100, issues: [] },
    market: { regime: 'trend_up', btc_dominance: 54.2 },
    primary_symbol: 'BTCUSDT',
    symbols: {
      BTCUSDT: symbolView(),
      ETHUSDT: symbolView({ price: { last: 3120.5, mark: 3121.0, bid: 3120.4, ask: 3120.6, spread_bps: 1.2, age_s: 1 } }),
    },
    delivery: { order_gate_ttl_s: 180, price_ttl_s: 15, audience: 'operator', render: 'md-evidence-prompt/1' },
    age_ms: 100_000,
  }
}

function analysisFixture() {
  return {
    schema_version: 'analysis/1',
    cycle_id: CYCLE_ID,
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
    confidence: 0.7,
    evidence_refs: ['symbols.BTCUSDT.price.last', 'symbols.BTCUSDT.timeframes.15m.rsi14'],
    warnings: [],
  }
}

function debateFixture({ side, round, confidence, plan, respondsTo = null, rebuttal = [], symbol = 'BTCUSDT' }) {
  const isRed = side === 'LONG'
  const agent = isRed ? 'red_team' : 'blue_team'
  const tradeable = Boolean(plan)
  return {
    schema_version: 'debate_case/1',
    cycle_id: CYCLE_ID,
    agent,
    symbol,
    side,
    round,
    responds_to: respondsTo,
    rebuttal,
    tradeable,
    confidence,
    thesis: isRed ? '15m 回踩 EMA20 未破，1h 趋势向上，回踩做多。' : '双阻力 + 资金费拥挤，冲高失败做空；但逆 1h 趋势。',
    evidence: [
      { ref: 'symbols.BTCUSDT.timeframes.15m.rsi14', claim: '15m RSI 61，未过热', weight: 'high' },
      { ref: 'symbols.BTCUSDT.price.last', claim: '现价 68234.5，位于 EMA20 上方', weight: 'high' },
      { ref: 'symbols.BTCUSDT.levels.resistances', claim: '上方 68820/69100 双阻力', weight: 'medium' },
    ],
    trigger: tradeable ? { type: isRed ? 'retest' : 'rejection', description: isRed ? '回踩 68100–68200 不破' : '冲高 68820 失败并跌回 68600', price: isRed ? 68100.0 : 68820.0, valid_until: '2026-11-09T03:45:20Z' } : null,
    plan_draft: plan,
    invalidation: tradeable ? { price: isRed ? 67410.2 : 69050.0, condition: isRed ? '15m 收盘跌破 EMA20' : '15m 收盘站上 69050' } : null,
    opponent_steelman: isRed ? '空头有资金费与双阻力，但缺少结构破坏。' : '多头有趋势与 EMA20，但双阻力与资金费仍在。',
    no_case_reason: tradeable ? null : 'NO_SETUP',
    warnings: [],
  }
}

function standAsideFixture(primary = null, overrides = {}) {
  return {
    ...analysisFixture(),
    candidates: [],
    primary_symbol: primary,
    stand_aside: true,
    stand_aside_reason: 'NO_CANDIDATE',
    ...overrides,
  }
}

function decisionFixture({ action = 'TRADE' } = {}) {  if (action === 'NO_TRADE') {
    return {
      schema_version: 'decision/1',
      cycle_id: CYCLE_ID,
      agent: 'decision',
      action: 'NO_TRADE',
      side: null,
      selected_case: null,
      symbol: null,
      confidence: 0.5,
      thesis: '两边都有风险，当前不交易。',
      case_comparison: {
        red: { strength: 'moderate', decisive_evidence: '趋势向上但资金费偏高', fatal_flaw: null },
        blue: { strength: 'weak', decisive_evidence: '双阻力但逆势', fatal_flaw: '缺少结构破坏' },
        decisive_factor: '证据冲突，不满足可执行条件',
        opposing_risk: '若强行交易，容易被双向波动止损',
      },
      score_override: false,
      override_rationale: null,
      plan: null,
      no_trade_reason: { code: 'NO_EDGE', detail: '两边证据接近，等待下一轮。' },
      invalidation: null,
      evidence_refs: ['symbols.BTCUSDT.price.last'],
      warnings: [],
    }
  }
  return {
    schema_version: 'decision/1',
    cycle_id: CYCLE_ID,
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

function riskFixture(recommendation = 'APPROVE') {
  return {
    schema_version: 'risk_advice/1',
    cycle_id: CYCLE_ID,
    agent: 'risk',
    recommendation,
    size_multiplier: recommendation === 'DOWNSIZE' ? 0.8 : 1,
    max_risk_pct: 1.0,
    max_leverage: 10,
    concerns: recommendation === 'REJECT' ? ['资金费拥挤'] : [],
    conditions: [],
    reasoning: '风险在预算内，保留保护单，按计划执行。',
    confidence: 0.8,
    warnings: [],
  }
}

function executionFixture() {
  return {
    schema_version: 'execution_entry/1',
    cycle_id: CYCLE_ID,
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
      reasoning_ref: 'decision:run-fixture',
      execution_policy: { max_slippage_bps: 15, on_protection_failure: 'CLOSE_POSITION', protection_retry_max: 1 },
    },
    preflight: {
      reference_price: 68234.5,
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

function roleKeyFrom(options) {
  const text = options.messages?.[0]?.content?.[0]?.text ?? ''
  const base = text.split('\n\n')[0]
  const payload = JSON.parse(base)
  const role = payload.task.role
  const round = payload.task.round
  if (role === 'analysis') return 'analysis'
  if (role === 'red_team') return round === 2 ? 'red_response' : 'red_opening'
  if (role === 'blue_team') return round === 2 ? 'blue_response' : 'blue_opening'
  if (role === 'decision') return 'decision'
  if (role === 'risk') return 'risk'
  if (role === 'execution') {
    return payload.output_schema === 'execution_manage/1' ? 'execution_manage' : 'execution_entry'
  }
  if (role === 'review') return 'review'
  throw new Error(`unknown fake role ${role}`)
}

function parsePayload(options) {
  const text = options.messages[0].content[0].text
  const base = text.split('\n\n')[0]
  return JSON.parse(base)
}

function makeFake({
  analysisMode = 'ok',
  decisionAction = 'TRADE',
  blueResponseHang = false,
  analysisHang = false,
  debateSymbolDrift = false,
  decisionSymbolDrift = false,
  decisionPlanDrift = false,
  r2PlanDrift = false,
  executionSymbolDrift = false,
  executionSideDrift = false,
  executionBlocked = null,
  // T16：允许替换红队计划，用来验证"缺 zone / 缺 valid_until"在链上的表现。
  redPlan = RED_PLAN,
  decisionPlan = null,
} = {}) {
  const optionsSeen = []
  let analysisAttempts = 0
  let redResponseCount = 0
  let blueResponseCount = 0
  const llm = {
    stream(options) {
      optionsSeen.push(options)
      const roleKey = roleKeyFrom(options)
      const payload = parsePayload(options)
      assert.equal(Object.prototype.hasOwnProperty.call(options, 'tools'), false, 'tools must not be sent')
      if (roleKey === 'analysis') {
        if (analysisHang) return hangStream(options.signal)
        analysisAttempts += 1
        if (analysisMode === 'repair' && analysisAttempts === 1) return oneShotStream('not json', options)
        if (analysisMode === 'always_bad') return oneShotStream('not json', options)
        if (analysisMode === 'max_tokens_once' && analysisAttempts === 1) {
          return maxTokensStream('{"schema_version":"analysis/1"', {
            inputTokens: 1819,
            outputTokens: 8000,
            totalTokens: 9819,
          }, options)
        }
        if (analysisMode === 'stand_aside_ok' || analysisMode === 'max_tokens_once') {
          return oneShotStream(JSON.stringify(standAsideFixture(null)), options)
        }
        if (analysisMode === 'stand_aside_no_notes') {
          return oneShotStream(JSON.stringify(standAsideFixture(null, {
            market_read: { ...analysisFixture().market_read, notes: [] },
            warnings: ['DERIVATIVES_PARTIAL'],
          })), options)
        }
        if (analysisMode === 'stand_aside_bad') return oneShotStream(JSON.stringify(standAsideFixture('BTCUSDT')), options)
        return oneShotStream(JSON.stringify(analysisFixture()), options)
      }
      if (roleKey === 'red_opening') {
        return oneShotStream(JSON.stringify(debateFixture({ side: 'LONG', round: 1, confidence: 0.68, plan: redPlan, symbol: debateSymbolDrift ? 'ETHUSDT' : 'BTCUSDT' })), options)
      }
      if (roleKey === 'blue_opening') {
        return oneShotStream(JSON.stringify(debateFixture({ side: 'SHORT', round: 1, confidence: 0.55, plan: BLUE_PLAN })), options)
      }
      if (roleKey === 'red_response') {
        redResponseCount += 1
        const respondsTo = payload.task.responds_to
        const response = debateFixture({
          side: 'LONG', round: 2, confidence: 0.70, plan: redPlan, respondsTo,
          rebuttal: [
            { target_claim: '资金费拥挤，多头拥挤', counter: '拥挤不等于反转，价格未放量滞涨。' },
            { target_claim: '68820/69100 双阻力，冲高失败可短', counter: '阻力距离近，放量突破则空头失效。' },
          ],
        })
        if (r2PlanDrift) response.plan_draft = { ...response.plan_draft, take_profit: { ...response.plan_draft.take_profit, price: response.plan_draft.take_profit.price + 1 } }
        return oneShotStream(JSON.stringify(response), options)
      }
      if (roleKey === 'blue_response') {
        blueResponseCount += 1
        if (blueResponseHang) return hangStream(options.signal)
        const respondsTo = payload.task.responds_to
        return oneShotStream(JSON.stringify(debateFixture({
          side: 'SHORT', round: 2, confidence: 0.52, plan: BLUE_PLAN, respondsTo,
          rebuttal: [
            { target_claim: '15m/1h 趋势向上，回踩可延续', counter: '趋势仍在，但双阻力未被突破。' },
            { target_claim: 'R:R 1.66', counter: '上方空间被压缩，追多性价比下降。' },
          ],
        })), options)
      }
      if (roleKey === 'decision') {
        const decision = decisionFixture({ action: decisionAction })
        if (decisionPlan && decision.action === 'TRADE') decision.plan = decisionPlan
        if (decisionSymbolDrift) decision.symbol = 'ETHUSDT'
        if (decisionPlanDrift) decision.plan = { ...decision.plan, stop_loss: { ...decision.plan.stop_loss, price: decision.plan.stop_loss.price + 1 } }
        return oneShotStream(JSON.stringify(decision), options)
      }
      if (roleKey === 'risk') {
        return oneShotStream(JSON.stringify(riskFixture()), options)
      }
      if (roleKey === 'execution_entry') {
        const execution = executionFixture()
        if (executionBlocked) {
          execution.executable = false
          execution.blocked_reason = executionBlocked
          execution.order_plan = null
          execution.preflight = null
          execution.rationale = 'No account equity or available balance is supplied; refusing to invent a size.'
        }
        if (executionSymbolDrift) {
          execution.symbol = 'ETHUSDT'
          execution.order_plan.symbol = 'ETHUSDT'
        }
        if (executionSideDrift) {
          execution.side = 'SHORT'
          execution.order_plan.side = 'SHORT'
        }
        return oneShotStream(JSON.stringify(execution), options)
      }
      throw new Error(`unexpected role ${roleKey}`)
    },
  }
  return { llm, optionsSeen, counters: { get analysisAttempts() { return analysisAttempts }, get redResponseCount() { return redResponseCount }, get blueResponseCount() { return blueResponseCount } } }
}

async function* oneShotStream(text, options) {
  yield { type: 'text-delta', text }
  yield { type: 'usage', usage: { inputTokens: 1000, outputTokens: 200, totalTokens: 1200 } }
  yield { type: 'finish', reason: { kind: 'stop' } }
}

async function* maxTokensStream(text, usage, options) {
  yield { type: 'text-delta', text }
  yield { type: 'usage', usage }
  yield { type: 'finish', reason: { kind: 'max-tokens' } }
}

async function* hangStream(signal) {
  if (signal?.aborted) throw new Error('aborted')
  await new Promise((resolve, reject) => {
    const onAbort = () => {
      clearTimeout(timer)
      signal?.removeEventListener('abort', onAbort)
      reject(new Error('aborted'))
    }
    const timer = setTimeout(() => {
      signal?.removeEventListener('abort', onAbort)
      reject(new Error('hang-test-timeout'))
    }, 10_000)
    signal?.addEventListener('abort', onAbort, { once: true })
  })
}

function baseConfig(overrides = {}) {
  return {
    now: () => NOW,
    roleTimeoutsMs: {
      analysis: 500,
      red_opening: 500,
      blue_opening: 500,
      red_response: 500,
      blue_response: 500,
      decision: 500,
      risk: 500,
      execution_entry: 500,
    },
    chainDeadlineMs: 5_000,
    ...overrides,
  }
}

test('evidence projection preserves hash/age and slices by role', () => {
  const view = buildEvidenceView(evidenceFixture(), { now: NOW })
  assert.match(view.evidence_hash, /^sha256:[0-9a-f]{64}$/)
  assert.equal(view.age_ms, 100_000)
  const analysis = sliceEvidence(view, 'analysis')
  const debate = sliceEvidence(view, 'debate')
  const risk = sliceEvidence(view, 'risk')
  assert.equal(Object.keys(analysis.data.symbols).length, 2)
  assert.equal(debate.meta.symbol_count, 1)
  assert.equal(Object.prototype.hasOwnProperty.call(debate.data, 'symbols'), false)
  assert.equal(debate.data.symbol.timeframes['4h'], undefined)
  assert.equal(risk.data.symbol.timeframes, undefined)
  assert.equal(analysis.data.evidence_hash, view.evidence_hash)
  assert.equal(analysis.data.age_ms, view.age_ms)
  assert.equal(debate.data.evidence_hash, view.evidence_hash)
})

test('prompts are complete and R2/decision inputs are symmetric', () => {
  const analysis = buildPrompt('analysis', { symbol: 'BTCUSDT', evidence: {}, constraints: {} })
  assert.ok(analysis.system.startsWith(COMMON_SYSTEM))
  assert.match(COMMON_SYSTEM, /omit optional prose/)
  assert.match(analysis.system, /Rank at most 2 candidates/)
  assert.match(analysis.system, /thesis ≤300 characters/)
  assert.match(analysis.system, /key_evidence_refs ≤4/)
  assert.match(analysis.system, /warnings ≤3/)
  const red = buildPrompt('red_opening', { symbol: 'BTCUSDT', evidence: {}, analysis: {}, constraints: {} })
  const blue = buildPrompt('blue_opening', { symbol: 'BTCUSDT', evidence: {}, analysis: {}, constraints: {} })
  assert.ok(red.system.startsWith(COMMON_SYSTEM))
  assert.ok(blue.system.startsWith(COMMON_SYSTEM))
  const redOpeningPayload = JSON.parse(red.user)
  assert.equal(Object.prototype.hasOwnProperty.call(redOpeningPayload, 'opponent_opening'), false)
  assert.equal(Object.prototype.hasOwnProperty.call(redOpeningPayload, 'red_response'), false)
  const r2 = buildPrompt('red_response', {
    symbol: 'BTCUSDT', evidence: {}, analysis: {},
    ownOpening: { round: 1, agent: 'red_team' },
    opponentOpening: { round: 1, agent: 'blue_team' },
    opponentCallId: 'run-x:blue_team:r1:1',
    constraints: {},
  })
  const r2Payload = JSON.parse(r2.user)
  assert.equal(r2Payload.opponent_opening.agent, 'blue_team')
  assert.equal(r2Payload.own_opening.round, 1)
  assert.equal(Object.prototype.hasOwnProperty.call(r2Payload, 'opponent_response'), false)
  const blueResponse = buildPrompt('blue_response', {
    symbol: 'BTCUSDT', evidence: {}, analysis: {},
    ownOpening: { round: 1, agent: 'blue_team' },
    opponentOpening: { round: 1, agent: 'red_team' },
    opponentCallId: 'run-x:red_team:r1:1',
    constraints: {},
  })
  for (const prompt of [red, blue, r2, blueResponse]) {
    assert.match(prompt.system, /thesis ≤300 characters/)
    assert.match(prompt.system, /evidence ≤3/)
    assert.match(prompt.system, /opponent_steelman ≤250 characters/)
  }
  for (const prompt of [red, blue]) {
    assert.match(prompt.system, /responds_to=null and rebuttal=\[\]/)
    assert.doesNotMatch(prompt.system, /rebuttal ≤2/)
  }
  for (const prompt of [r2, blueResponse]) {
    assert.match(prompt.system, /rebuttal ≤2/)
    assert.match(prompt.system, /counter ≤150 characters/)
  }
  const decision = buildPrompt('decision', {
    symbol: 'BTCUSDT', evidence: {}, analysis: {},
    redOpening: {}, blueOpening: {}, redResponse: {}, blueResponse: {}, scorecard: {},
    constraints: {},
  })
  const decisionPayload = JSON.parse(decision.user)
  assert.ok(decisionPayload.red_opening && decisionPayload.blue_opening)
  assert.ok(decisionPayload.red_response && decisionPayload.blue_response)
  assert.equal(decisionPayload.debate_mode, 'symmetric_two_round')
  assert.equal(decisionPayload.no_responses, false)
  const independentDecision = buildPrompt('decision', {
    symbol: 'BTCUSDT', evidence: {}, analysis: {}, redOpening: {}, blueOpening: {},
    redResponse: null, blueResponse: null, scorecard: {}, debateMode: 'independent', degraded: true, constraints: {},
  })
  const independentPayload = JSON.parse(independentDecision.user)
  assert.equal(independentPayload.debate_mode, 'independent')
  assert.equal(independentPayload.no_responses, true)
  assert.ok(independentPayload.note)
  const execution = buildPrompt('execution_entry', {
    symbol: 'BTCUSDT', evidence: {}, decision: {}, finalRisk: {}, constraints: {},
    account: {
      total_margin_balance: '10000', available_balance: '9000', total_unrealized_profit: '100',
      apiKey: 'must-not-leak', secret: 'must-not-leak',
    },
    equity: { session: '10000', exchange: '10100', available: '9000', token: 'must-not-leak' },
  })
  const executionPayload = JSON.parse(execution.user)
  assert.equal(executionPayload.task.mode, 'ENTRY')
  assert.equal(executionPayload.account.total_margin_balance, '10000')
  assert.equal(executionPayload.equity.session, '10000')
  assert.equal(executionPayload.available_balance, '9000')
  assert.equal(executionPayload.equity_session, '10000')
  assert.equal(executionPayload.equity_exchange, '10100')
  assert.equal(JSON.stringify(executionPayload).includes('must-not-leak'), false)
  const manage = buildPrompt('execution_manage', { symbol: 'BTCUSDT', evidence: {}, analysis: {}, position: {}, constraints: {} })
  assert.equal(JSON.parse(manage.user).task.mode, 'MANAGE')
})

test('future timestamp/bar evidence fails closed', () => {
  const futureProduced = evidenceFixture()
  futureProduced.produced_at = '2026-11-09T01:48:00Z'
  assert.throws(() => buildEvidenceView(futureProduced, { now: NOW }), /future|EVIDENCE_INVALID/)

  const futureAsOf = evidenceFixture()
  futureAsOf.as_of = '2026-11-09T01:48:00Z'
  assert.throws(() => buildEvidenceView(futureAsOf, { now: NOW }), /future|EVIDENCE_INVALID/)

  const futureCandle = evidenceFixture()
  futureCandle.symbols.BTCUSDT.timeframes['15m'].candles[0].t = NOW + 60_000
  assert.throws(() => buildEvidenceView(futureCandle, { now: NOW }), /future|EVIDENCE_INVALID/)

  const missingHealth = evidenceFixture()
  delete missingHealth.data_health
  assert.throws(() => buildEvidenceView(missingHealth, { now: NOW }), /data_health|EVIDENCE_INVALID/)

  const missingSnapshot = evidenceFixture()
  delete missingSnapshot.snapshot_id
  assert.throws(() => buildEvidenceView(missingSnapshot, { now: NOW }), /snapshot|EVIDENCE_INVALID/)

  const missingPrimary = evidenceFixture()
  delete missingPrimary.primary_symbol
  assert.throws(() => buildEvidenceView(missingPrimary, { now: NOW }), /primary|EVIDENCE_INVALID/)
})

test('evidence age/hash are server-derived and tampering is rejected', () => {
  const view = buildEvidenceView(evidenceFixture(), { now: NOW })
  assert.equal(view.age_ms, 100_000)
  const withHash = evidenceFixture()
  withHash.evidence_hash = view.evidence_hash
  assert.equal(buildEvidenceView(withHash, { now: NOW }).evidence_hash, view.evidence_hash)
  const tamperedHash = evidenceFixture()
  tamperedHash.evidence_hash = 'sha256:deadbeef'
  assert.throws(() => buildEvidenceView(tamperedHash, { now: NOW }), /evidence_hash|EVIDENCE_INVALID/)
  const badAge = evidenceFixture()
  badAge.age_ms = 1
  assert.throws(() => buildEvidenceView(badAge, { now: NOW }), /age_ms|EVIDENCE_INVALID/)
})

test('all prompt and schema contracts are present', () => {
  const roleKeys = ['analysis', 'red_opening', 'blue_opening', 'red_response', 'blue_response', 'decision', 'risk', 'execution_entry', 'execution_manage', 'review']
  for (const roleKey of roleKeys) {
    const prompt = buildPrompt(roleKey, { symbol: 'BTCUSDT', evidence: {}, analysis: {}, constraints: {} })
    assert.ok(prompt.system.startsWith(COMMON_SYSTEM), `${roleKey} common prefix`)
    assert.ok(prompt.schemaId)
    assert.ok(JSON.parse(prompt.user).output_schema)
  }
  assert.deepEqual(Object.keys(SCHEMAS).sort(), ['analysis', 'debate_case', 'decision', 'execution_entry', 'execution_manage', 'review_report', 'risk_advice'])
})

test('execution_manage and review schemas accept valid placeholders', () => {
  const manage = {
    schema_version: 'execution_manage/1', cycle_id: CYCLE_ID, agent: 'execution', mode: 'MANAGE',
    action: 'CLOSE', position_id: 'BTCUSDT:LONG', symbol: 'BTCUSDT',
    orders: [{ purpose: 'CLOSE', type: 'MARKET', qty: null, reduce_only: true }],
    reason: 'Protection missing and position unsafe; close at market.', urgency: 'high', warnings: [],
  }
  assert.equal(validateSchema(SCHEMAS.execution_manage, manage).length, 0)
  const badManage = structuredClone(manage)
  badManage.action = 'OPEN'
  assert.ok(validateSchema(SCHEMAS.execution_manage, badManage).length > 0)
  const review = {
    schema_version: 'review_report/1', review_id: 'rev-20261109', review_date: '2026-11-09', agent: 'review',
    period: { start: '2026-11-09T00:00:00Z', end: '2026-11-10T00:00:00Z' },
    summary: 'Manual M1.3a placeholder review; no live trading metrics are claimed.',
    process_assessment: 'Schema and orchestration fixtures are deterministic and offline.',
    what_worked: [], what_failed: [], risk_events: [], execution_issues: [], lessons: [], proposed_adjustments: [],
    evidence_refs: [], warnings: [],
  }
  assert.equal(validateSchema(SCHEMAS.review_report, review).length, 0)
})

test('output caps are raised and hard-capped at 10000', () => {
  assert.equal(MAX_OUTPUT_TOKENS, 10000)
  // M1.4c: openings 4800->4000, responses 5850->4600 (fit 40s at ~130 tok/s),
  // decision 4500->4000 and risk 1600->2400: both run at effort=max, and the
  // M1.4b pair timed out on 43/49 real risk calls.
  assert.deepEqual(ROLE_MAX_TOKENS, {
    analysis: 8000,
    red_opening: 4000,
    blue_opening: 4000,
    red_response: 4600,
    blue_response: 4600,
    decision: 4000,
    risk: 3000,
    execution_entry: 3200,
    execution_manage: 2400,
    review: 3000,
  })
  assert.ok(Object.values(ROLE_MAX_TOKENS).every((value) => value <= MAX_OUTPUT_TOKENS))
  assert.equal(Math.min(Math.ceil(ROLE_MAX_TOKENS.analysis * 1.5), MAX_OUTPUT_TOKENS), 10000)
  assert.equal(Math.min(Math.ceil(ROLE_MAX_TOKENS.red_opening * 1.5), MAX_OUTPUT_TOKENS), 6000)
  assert.equal(Math.min(Math.ceil(ROLE_MAX_TOKENS.red_response * 1.5), MAX_OUTPUT_TOKENS), 6900)
  assert.equal(Math.min(Math.ceil(ROLE_MAX_TOKENS.decision * 1.5), MAX_OUTPUT_TOKENS), 6000)
  assert.equal(Math.min(Math.ceil(ROLE_MAX_TOKENS.risk * 1.5), MAX_OUTPUT_TOKENS), 4500)
  assert.notEqual(Math.min(Math.ceil(ROLE_MAX_TOKENS.analysis * 1.5), MAX_OUTPUT_TOKENS), ROLE_MAX_TOKENS.analysis)
  assert.notEqual(Math.min(Math.ceil(ROLE_MAX_TOKENS.red_opening * 1.5), MAX_OUTPUT_TOKENS), ROLE_MAX_TOKENS.red_opening)
  assert.notEqual(Math.min(Math.ceil(ROLE_MAX_TOKENS.red_response * 1.5), MAX_OUTPUT_TOKENS), ROLE_MAX_TOKENS.red_response)
  assert.notEqual(Math.min(Math.ceil(ROLE_MAX_TOKENS.decision * 1.5), MAX_OUTPUT_TOKENS), ROLE_MAX_TOKENS.decision)
})

test('M1.4f time budget: analysis 70s, R1 60s, R2 55s, decision/risk 45s, chain 340s', () => {
  assert.equal(CHAIN_DEADLINE_MS, 340_000)
  assert.equal(ROLE_TIMEOUT_MS.red_opening, 60_000)
  assert.equal(ROLE_TIMEOUT_MS.blue_opening, 60_000)
  assert.equal(ROLE_TIMEOUT_MS.red_response, 55_000)
  assert.equal(ROLE_TIMEOUT_MS.blue_response, 55_000)
  assert.equal(ROLE_TIMEOUT_MS.decision, 45_000)
  assert.equal(ROLE_TIMEOUT_MS.risk, 45_000)
  assert.equal(ROLE_TIMEOUT_MS.execution_entry, 35_000)
  assert.equal(ROLE_TIMEOUT_MS.analysis, 70_000)
  // Serial stage wall-clock: 70 + 60 + 55 + 45 + 45 + 35 = 310s < 340s deadline.
  // A full-length retry no longer fits, so chain.js compresses retries to the
  // remaining budget (only short roles such as execution still get a replay).
  const wallClock = 70_000 + 60_000 + 55_000 + 45_000 + 45_000 + 35_000
  assert.equal(wallClock, 310_000)
  assert.ok(wallClock < CHAIN_DEADLINE_MS)
  // effort=max roles pay reasoning latency on top of ~130 tok/s decode:
  // decision 4000/130 = 30.8s < 45s*0.9; risk 2400/130 = 18.5s < 40s*0.9.
  assert.ok(ROLE_MAX_TOKENS.decision / 130 <= ROLE_TIMEOUT_MS.decision * 0.9 / 1000)
  assert.ok(ROLE_MAX_TOKENS.risk / 130 <= ROLE_TIMEOUT_MS.risk * 0.9 / 1000)
})

test('P1.9 analysis slice is 3500 and other slices are unchanged', () => {
  assert.equal(EVIDENCE_SLICE_TOKENS.analysis, 3500)
  assert.equal(EVIDENCE_SLICE_TOKENS.debate, 1800)
  assert.equal(EVIDENCE_SLICE_TOKENS.decision, 1200)
  assert.equal(EVIDENCE_SLICE_TOKENS.risk, 600)
  assert.equal(EVIDENCE_SLICE_TOKENS.execution, 400)
  assert.equal(EVIDENCE_SLICE_TOKENS.review, 0)
})

test('analysis timeout lives in constants (70s) and docs budget agrees', () => {
  const doc02 = fs.readFileSync(new URL('../../docs/02-AI决策链.md', import.meta.url), 'utf8')
  assert.match(doc02, /310s/)
  assert.match(doc02, /70s/)
  assert.match(doc02, /R1 60s；R2 55s/)
  assert.match(doc02, /3,500/)
  assert.match(doc02, /8,000/)
  assert.match(doc02, /4,000 \/ 4,600/)
  assert.match(doc02, /"maxLength": 600/)
  assert.match(doc02, /"maxLength": 400/)
  assert.match(doc02, /counter ≤150/)
  const docV1 = fs.readFileSync(new URL('../../docs/v1-设计方案.md', import.meta.url), 'utf8')
  assert.match(docV1, /50s（Host override；chain 常量 28s）/)
  assert.match(docV1, /178s/)
  assert.match(docV1, /195s/)
  assert.match(docV1, /8000/)
  assert.match(docV1, /4800 \/ 5850/)
  assert.match(docV1, /opponent_steelman\.maxLength=600/)
  assert.match(docV1, /rebuttal\.counter\.maxLength=400/)
  assert.equal(50 + 32 + 32 + 30 + 14 + 20, 178)
  assert.ok(50 + 32 + 32 + 30 + 14 + 20 <= 180)
})

test('schema positive/negative cases', () => {
  const analysis = analysisFixture()
  assert.equal(validateSchema(SCHEMAS.analysis, analysis).length, 0)
  const negative = structuredClone(analysis)
  negative.primary_symbol = 'bad'
  negative.stand_aside = false
  assert.ok(validateSchema(SCHEMAS.analysis, negative).length > 0)
  const missing = structuredClone(analysis)
  delete missing.market_read
  assert.ok(validateSchema(SCHEMAS.analysis, missing).length > 0)
  const debate = debateFixture({ side: 'LONG', round: 1, confidence: 0.68, plan: RED_PLAN })
  assert.equal(validateSchema(SCHEMAS.debate_case, debate).length, 0)
  const badDebate = structuredClone(debate)
  badDebate.side = 'SHORT'
  // Schema allows SHORT; semantic direction is tested by the chain. This
  // negative case exercises a schema-level missing field instead.
  delete badDebate.thesis
  assert.ok(validateSchema(SCHEMAS.debate_case, badDebate).length > 0)
  const longSteelman = structuredClone(debate)
  longSteelman.opponent_steelman = '多'.repeat(600)
  assert.equal(validateSchema(SCHEMAS.debate_case, longSteelman).length, 0)
  longSteelman.opponent_steelman = '多'.repeat(601)
  assert.ok(validateSchema(SCHEMAS.debate_case, longSteelman).length > 0)
  const r2Debate = debateFixture({ side: 'LONG', round: 2, confidence: 0.68, plan: RED_PLAN, respondsTo: 'run-x:blue_team:r1:1', rebuttal: [{ target_claim: '资'.repeat(180), counter: '证'.repeat(400) }] })
  assert.equal(validateSchema(SCHEMAS.debate_case, r2Debate).length, 0)
  const longCounter = structuredClone(r2Debate)
  longCounter.rebuttal[0].counter = '证'.repeat(401)
  assert.ok(validateSchema(SCHEMAS.debate_case, longCounter).length > 0)
  const longTarget = structuredClone(r2Debate)
  longTarget.rebuttal[0].target_claim = '资'.repeat(181)
  assert.ok(validateSchema(SCHEMAS.debate_case, longTarget).length > 0)
  const blocked = executionFixture(CYCLE_ID)
  blocked.executable = false
  blocked.blocked_reason = 'ACCOUNT_UNAVAILABLE'
  blocked.order_plan = null
  blocked.preflight = null
  assert.equal(validateSchema(SCHEMAS.execution_entry, blocked).length, 0)
  const badBlocked = structuredClone(blocked)
  badBlocked.blocked_reason = 'NO_SUCH_BLOCK'
  assert.ok(validateSchema(SCHEMAS.execution_entry, badBlocked).length > 0)
})

test('opening requires null responds_to and empty rebuttal', () => {
  const opening = debateFixture({ side: 'LONG', round: 1, confidence: 0.68, plan: RED_PLAN })
  const context = { cycleId: CYCLE_ID, primarySymbol: 'BTCUSDT' }
  assert.equal(validateSemantics('red_opening', opening, context).length, 0)
  const badResponds = structuredClone(opening)
  badResponds.responds_to = 'run-x:blue_team:r1:1'
  assert.ok(validateSemantics('red_opening', badResponds, context).length > 0)
  const badRebuttal = structuredClone(opening)
  badRebuttal.rebuttal = [{ target_claim: '资金费拥挤', counter: '拥挤不等于反转' }]
  assert.ok(validateSemantics('red_opening', badRebuttal, context).length > 0)
})

test('full symmetric chain makes 8 audit calls and trades', async () => {
  const fake = makeFake()
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(result.ok, true, JSON.stringify(result.error))
  assert.equal(result.outcome, 'trade', JSON.stringify(result.error))
  assert.equal(result.debateMode, 'symmetric_two_round')
  assert.equal(result.mode, 'full_chain')
  assert.equal(result.degraded, false, JSON.stringify(result.calls.filter((c) => c.schema_ok !== true)))
  assert.equal(result.calls.length, 8)
  assert.deepEqual(result.calls.map((c) => c.role_key), [
    'analysis', 'red_opening', 'blue_opening', 'red_response', 'blue_response', 'decision', 'risk', 'execution_entry',
  ])
  assert.equal(result.outputs.redResponse.responds_to, 'run-cyc-20261109T014520Z-7f3a1c:blue_team:r1:1')
  assert.equal(result.outputs.blueResponse.responds_to, 'run-cyc-20261109T014520Z-7f3a1c:red_team:r1:1')
  assert.equal(result.outputs.redResponse.plan_draft.entry.price, RED_PLAN.entry.price)
  assert.equal(result.outputs.decision.plan.entry.price, RED_PLAN.entry.price)
  assert.equal(result.outputs.execution.order_plan.qty, '0.067')
  assert.equal(result.scorecard.red.gate, 'PASS')
  assert.equal(result.scorecard.blue.gate, 'FAIL_RR')
  for (const call of result.calls) {
    assert.ok(call.call_id)
    assert.ok(call.role)
    assert.ok(call.attempt >= 1)
    assert.ok(call.latency_ms >= 0)
    assert.equal(call.schema_ok, true)
    assert.ok(call.input_tokens >= 0)
    assert.match(call.response_sha256, /^sha256:[0-9a-f]{64}$/)
  }
  assert.equal(result.calls.filter((c) => c.round === 2).length, 2)
  assert.equal(fake.optionsSeen.length, 8)
  const efforts = new Set(fake.optionsSeen.map((options) => options.reasoningEffort))
  assert.deepEqual([...efforts].sort(), ['high', 'low', 'max'])
  const expectedEffort = {
    analysis: 'high', red_opening: 'high', blue_opening: 'high', red_response: 'high', blue_response: 'high',
    decision: 'max', risk: 'max', execution_entry: 'low',
  }
  for (const call of result.calls) assert.equal(call.effort, expectedEffort[call.role_key], `${call.role_key} effort`)
  for (const options of fake.optionsSeen) {
    assert.equal(Object.prototype.hasOwnProperty.call(options, 'tools'), false)
    assert.equal(options.provider, 'fake')
    assert.equal(options.model, 'fake-1')
    assert.ok(options.system.startsWith(COMMON_SYSTEM))
  }
})

test('production seam resolves agentDefaultModel and calls ctx.llm only', async () => {
  const fake = makeFake()
  const ctx = {
    llm: fake.llm,
    agentDefaultModel: { currentSelection: () => ({ provider: 'fake', model: 'fake-1' }) },
  }
  const result = await runChainFromContext(ctx, evidenceFixture(), { config: baseConfig(), hardRisk: { verdict: 'approved' } })
  assert.equal(result.outcome, 'trade')
  assert.equal(result.calls.length, 8)
  assert.equal(fake.optionsSeen.length, 8)
  assert.equal(fake.optionsSeen[0].provider, 'fake')
  assert.equal(fake.optionsSeen[0].model, 'fake-1')
})

test('independent fallback makes 6 calls and skips R2', async () => {
  const fake = makeFake()
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig({ debateMode: 'independent' }),
  })
  assert.equal(result.outcome, 'trade')
  assert.equal(result.debateMode, 'independent')
  assert.equal(result.mode, 'opening_only')
  assert.equal(result.calls.length, 6)
  assert.equal(result.outputs.redResponse, null)
  assert.equal(result.outputs.blueResponse, null)
  assert.deepEqual(result.calls.map((c) => c.role_key), [
    'analysis', 'red_opening', 'blue_opening', 'decision', 'risk', 'execution_entry',
  ])
})

test('R2 timeout drops both responses and degrades to independent', async () => {
  const fake = makeFake({ blueResponseHang: true, decisionAction: 'NO_TRADE' })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    config: baseConfig({ roleTimeoutsMs: { ...baseConfig().roleTimeoutsMs, blue_response: 25 } }),
  })
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.debateMode, 'independent')
  assert.equal(result.mode, 'opening_only')
  assert.equal(result.degraded, true)
  assert.equal(result.outputs.redResponse, null)
  assert.equal(result.outputs.blueResponse, null)
  assert.equal(result.calls.length, 8)
  assert.equal(result.calls.filter((c) => c.role_key === 'blue_response').length, 2)
  assert.equal(result.calls.filter((c) => c.role_key === 'decision').length, 1)
  const fallbackEvent = result.calls.find((c) => c.validation_errors[0]?.keyword === 'DEBATE_ROUND2_DEGRADED')
  assert.ok(fallbackEvent)
  assert.equal(fallbackEvent.mode, 'opening_only')
  assert.equal(fallbackEvent.requested_debate_mode, 'symmetric_two_round')
  assert.ok(fake.counters.blueResponseCount >= 2)
})

test('max-tokens preserves usage and retries with escalated cap + compact suffix', async () => {
  const fake = makeFake({ analysisMode: 'max_tokens_once' })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    config: baseConfig(),
  })
  assert.equal(result.ok, true, result.error?.details?.cause?.stack ?? JSON.stringify(result.error))
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.reasonCode, 'NO_CANDIDATE')
  assert.equal(result.calls.length, 2)
  const [first, second] = result.calls
  assert.equal(first.status, 'max_tokens')
  assert.equal(first.finish_kind, 'max_tokens')
  assert.equal(first.max_tokens, 8000)
  assert.equal(first.input_tokens, 1819)
  assert.equal(first.output_tokens, 8000)
  assert.equal(first.total_tokens, 9819)
  assert.equal(first.error_code, 'LLM_FINISH_ERROR')
  assert.equal(second.status, 'ok')
  assert.equal(second.max_tokens, 10000)
  assert.equal(second.schema_ok, true)
  assert.equal(fake.optionsSeen[0].maxTokens, 8000)
  assert.equal(fake.optionsSeen[1].maxTokens, 10000)
  assert.ok(fake.optionsSeen[1].messages[0].content[0].text.includes('truncated by the output token limit'))
})

test('missing execution account blocks with ACCOUNT_UNAVAILABLE', async () => {
  const fake = makeFake({ executionBlocked: 'ACCOUNT_UNAVAILABLE' })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(result.ok, true)
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.reasonCode, 'EXECUTION_BLOCKED:ACCOUNT_UNAVAILABLE', JSON.stringify(result.calls))
  assert.equal(result.outputs.execution.executable, false)
  assert.equal(result.outputs.execution.blocked_reason, 'ACCOUNT_UNAVAILABLE')
})

test('one schema repair is allowed and recorded', async () => {
  const fake = makeFake({ analysisMode: 'repair' })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(result.outcome, 'trade')
  assert.equal(result.calls[0].schema_ok, false)
  assert.equal(result.calls[0].attempt, 1)
  assert.equal(result.calls[1].schema_ok, true)
  assert.equal(result.calls[1].repaired, true)
  assert.ok(result.calls[0].validation_errors.length > 0)
})

test('stand_aside=true with null primary reaches NO_TRADE(NO_CANDIDATE)', async () => {
  const fake = makeFake({ analysisMode: 'stand_aside_ok' })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.reasonCode, 'NO_CANDIDATE')
  assert.equal(result.calls.length, 1)
  assert.equal(result.calls[0].schema_ok, true)
  assert.deepEqual(result.detail, {
    reason: 'NO_CANDIDATE',
    confidence: 0.7,
    regime: 'trend_up',
    notes: ['资金费偏高，但价格未出现放量滞涨。'],
    warnings: [],
  })
})

test('stand_aside detail falls back to reason when notes are empty', async () => {
  const fake = makeFake({ analysisMode: 'stand_aside_no_notes' })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.reasonCode, 'NO_CANDIDATE')
  assert.equal(result.detail.reason, 'NO_CANDIDATE')
  assert.deepEqual(result.detail.notes, ['NO_CANDIDATE'])
  assert.deepEqual(result.detail.warnings, ['DERIVATIVES_PARTIAL'])
  assert.equal(result.detail.confidence, 0.7)
  assert.equal(result.detail.regime, 'trend_up')
})

test('stand_aside detail truncates and whitelists unbounded text', () => {
  const detail = standAsideDetail({
    stand_aside_reason: 'NO_CANDIDATE',
    confidence: Number.NaN,
    market_read: {
      regime: 'not_a_regime',
      notes: ['x'.repeat(300), '   ', 42, 'ok'],
    },
    warnings: Array.from({ length: 7 }, (_, index) => `w${index}-` + 'y'.repeat(300)),
  })
  assert.equal(detail.reason, 'NO_CANDIDATE')
  assert.equal(detail.confidence, null)
  assert.equal(detail.regime, null)
  assert.equal(detail.notes.length, 2)
  assert.equal(detail.notes[0].length, 160)
  assert.equal(detail.warnings.length, 5)
  assert.equal(detail.warnings[0].length, 160)
})

test('stand_aside=true with non-null primary fails semantic validation', async () => {
  const fake = makeFake({ analysisMode: 'stand_aside_bad' })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(result.outcome, 'failed')
  assert.equal(result.reasonCode, 'SCHEMA_INVALID')
  assert.equal(result.calls.length, 2)
  assert.ok(result.calls.every((call) => call.schema_ok === false))
})

test('NO_TRADE short-circuits before risk/execution', async () => {
  const fake = makeFake({ decisionAction: 'NO_TRADE' })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    config: baseConfig(),
  })
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.reasonCode, 'NO_EDGE')
  assert.equal(result.calls.length, 6)
  assert.equal(result.outputs.risk, undefined)
  assert.equal(result.outputs.execution, undefined)
})

test('hard risk rejection short-circuits before risk AI', async () => {
  const fake = makeFake()
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'rejected', violations: [{ rule: 'R15', action: 'reject' }] },
    config: baseConfig(),
  })
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.reasonCode, 'HARD_RISK_REJECT')
  assert.equal(result.calls.length, 6)
  assert.equal(result.outputs.risk, undefined)
})

test('unknown hardRisk verdict fails closed before risk AI', async () => {
  const fake = makeFake()
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'probably_fine' },
    config: baseConfig(),
  })
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.reasonCode, 'HARD_RISK_UNAVAILABLE')
  assert.equal(result.outputs.risk, undefined)
})

test('R1 debate symbol drift is rejected', async () => {
  const fake = makeFake({ debateSymbolDrift: true })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(result.outcome, 'failed')
  assert.equal(result.reasonCode, 'DEBATE_INCOMPLETE')
  assert.ok(result.calls.some((c) => c.role === 'red_team' && c.schema_ok === false))
})

test('decision symbol drift is rejected', async () => {
  const fake = makeFake({ decisionSymbolDrift: true })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.reasonCode, 'DECISION_INVALID')
  assert.ok(result.calls.filter((c) => c.role === 'decision').every((c) => c.schema_ok === false))
})

test('decision plan drift is rejected', async () => {
  const fake = makeFake({ decisionPlanDrift: true })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.reasonCode, 'DECISION_INVALID')
})

test('execution top-level/plan symbol drift is rejected', async () => {
  const fake = makeFake({ executionSymbolDrift: true })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.reasonCode, 'EXECUTION_PLAN_INVALID')
  assert.ok(result.calls.filter((c) => c.role === 'execution').every((c) => c.schema_ok === false))
})

test('execution top-level side drift is rejected', async () => {
  const fake = makeFake({ executionSideDrift: true })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.reasonCode, 'EXECUTION_PLAN_INVALID')
})

test('R2 plan drift degrades instead of using a drifted response', async () => {
  const fake = makeFake({ r2PlanDrift: true })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(result.outcome, 'trade')
  assert.equal(result.degraded, true)
  assert.equal(result.debateMode, 'independent')
  assert.equal(result.mode, 'opening_only')
  assert.equal(result.outputs.redResponse, null)
  assert.equal(result.outputs.blueResponse, null)
})

test('REDUCE requires a positive decimal-string qty; CLOSE allows null', () => {
  const baseManage = {
    schema_version: 'execution_manage/1', cycle_id: CYCLE_ID, agent: 'execution', mode: 'MANAGE',
    position_id: 'BTCUSDT:LONG', symbol: 'BTCUSDT', reason: 'Risk reduction per protection review.', urgency: 'normal', warnings: [],
  }
  const reduceNull = {
    ...baseManage, action: 'REDUCE',
    orders: [{ purpose: 'REDUCE', type: 'MARKET', qty: null, reduce_only: true }],
  }
  assert.ok(validateSchema(SCHEMAS.execution_manage, reduceNull).length === 0)
  assert.ok(validateSemantics('execution_manage', reduceNull, {}).length > 0)
  const close = {
    ...baseManage, action: 'CLOSE',
    orders: [{ purpose: 'CLOSE', type: 'MARKET', qty: null, reduce_only: true }],
  }
  assert.equal(validateSemantics('execution_manage', close, {}).length, 0)
})

test('plan copy compares full plan including entry zones', () => {
  const flipped = structuredClone(RED_PLAN)
  flipped.entry.zone_low = flipped.entry.zone_low - 100
  assert.equal(flipped.entry.zone_low === RED_PLAN.entry.zone_low, false)
  const errors = validateSemantics('decision', decisionFixture(), {
    cycleId: CYCLE_ID,
    caps: { maxRiskPct: 1, maxLeverage: 10, minRr: 1.2, slMinPct: 0.2, slMaxPct: 5 },
    primarySymbol: 'BTCUSDT',
    openings: { red: debateFixture({ side: 'LONG', round: 1, confidence: 0.68, plan: RED_PLAN }), blue: debateFixture({ side: 'SHORT', round: 1, confidence: 0.55, plan: BLUE_PLAN }) },
    scorecard: { red: { gate: 'PASS', rubric_score: 0.82 }, blue: { gate: 'FAIL_RR', rubric_score: 0.61 }, tie: false },
  })
  assert.equal(errors.length, 0)
  const drifted = decisionFixture()
  drifted.plan = { ...drifted.plan, entry: { ...drifted.plan.entry, zone_low: 1 } }
  const driftErrors = validateSemantics('decision', drifted, {
    cycleId: CYCLE_ID,
    caps: { maxRiskPct: 1, maxLeverage: 10, minRr: 1.2, slMinPct: 0.2, slMaxPct: 5 },
    primarySymbol: 'BTCUSDT',
    openings: { red: debateFixture({ side: 'LONG', round: 1, confidence: 0.68, plan: RED_PLAN }), blue: debateFixture({ side: 'SHORT', round: 1, confidence: 0.55, plan: BLUE_PLAN }) },
    scorecard: { red: { gate: 'PASS', rubric_score: 0.82 }, blue: { gate: 'FAIL_RR', rubric_score: 0.61 }, tie: false },
  })
  assert.ok(driftErrors.length > 0, 'a changed entry zone must break the plan copy')
})

// NOTE: the six tests below were re-authored after the file was accidentally
// truncated during an append (read/write round-trip lost the tail). They assert
// the same guarantees as the originals; behaviour, not wording, is the contract.
test('permanent schema failure is fail-closed after one repair', async () => {
  const fake = makeFake({ analysisMode: 'always_bad' })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(result.ok, false)
  assert.notEqual(result.outcome, 'trade')
  // 一次调用 + 一次修复尝试，随后失败关闭；不无限重试。
  assert.equal(result.calls.length, 2)
  for (const call of result.calls) assert.notEqual(call.schema_ok, true)
})

test('analysis timeout fails closed without throwing', async () => {
  const fake = makeFake({ analysisHang: true })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig({ roleTimeoutsMs: { analysis: 120 } }),
  })
  assert.equal(result.ok, false)
  assert.notEqual(result.outcome, 'trade')
  assert.ok(result.error?.code, JSON.stringify(result.error))
})

test('an unsupported reasoning effort fails the call once (no retry amplification)', async () => {
  let streams = 0
  const llm = {
    stream() {
      streams += 1
      throw new ChainError('UNSUPPORTED_REASONING_EFFORT', 'adapter rejected effort=max')
    },
  }
  const result = await runChain({
    llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(result.ok, false)
  assert.equal(streams, 1, 'non-retryable error must not be retried')
})

test('a transport-shaped message stays retryable (G3 list must stay narrow)', () => {
  const retryable = ['LLM_TIMEOUT', 'LLM_TRANSPORT', 'LLM_INCOMPLETE', 'LLM_FINISH_ERROR', 'JSON_PARSE', 'JSON_EMPTY']
  for (const code of retryable) {
    assert.equal(isRetryableLlmError(new ChainError(code, 'x')), true, code)
  }
  for (const code of ['UNSUPPORTED_REASONING_EFFORT', 'NO_ADAPTER', 'NO_LLM', 'CHAIN_DEADLINE', 'BUDGET_EXHAUSTED']) {
    assert.equal(isRetryableLlmError(new ChainError(code, 'x')), false, code)
  }
})

test('chain results carry attribution and per-attempt timestamps', async () => {
  const fake = makeFake()
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(result.outcome, 'trade', JSON.stringify(result.error))
  for (const call of result.calls) {
    assert.ok(Number.isFinite(call.started_at_ms), call.role_key)
    assert.ok(Number.isFinite(call.ended_at_ms), call.role_key)
    assert.ok(call.ended_at_ms >= call.started_at_ms, call.role_key)
    assert.ok(call.call_id)
  }
  assert.ok(result.attribution, 'round must carry attribution')
})

test('stale or non-GOOD evidence makes no LLM call', async () => {
  const fake = makeFake()
  const stale = evidenceFixture()
  stale.data_health = { verdict: 'DEGRADED', score: 40, issues: ['stale'] }
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: stale,
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.notEqual(result.outcome, 'trade')
  assert.equal(result.calls.length, 0, 'degraded evidence must not spend a model call')
  assert.equal(fake.optionsSeen.length, 0)
})

// ---------------------------------------------------------------- §8·D 执行闸门
const GATE_PLAN = {
  side: 'LONG',
  entry: { style: 'MARKET', price: 68234.5, zone_low: 68100.0, zone_high: 68250.0 },
  stop_loss: { price: 67410.2, distance_pct: 1.21, basis: 'structure' },
  take_profit: { price: 69600.0, rr: 1.66 },
  risk_pct: 0.8,
  leverage: 3,
  time_stop_hours: 24,
  rr_to_target: 1.66,
  valid_until: '2026-11-09T01:50:00Z',
}
const GATE_NOW = Date.parse('2026-11-09T01:47:00Z')

function gateContext(overrides = {}) {
  return {
    plan: structuredClone(GATE_PLAN),
    quote: { last: 68200.0, age_ms: 1_200, background_age_ms: 12_000 },
    now: GATE_NOW,
    ...overrides,
  }
}

test('gate passes only when zone, expiry, freshness and price all verify', () => {
  const verdict = evaluateEntryPlan(gateContext())
  assert.equal(verdict.ok, true, JSON.stringify(verdict))
  assert.equal(verdict.code, null)
  assert.equal(verdict.checked_at, GATE_NOW)
  assert.ok(verdict.checks.some((check) => check.code === 'PRICE_IN_ZONE' && check.ok))
})

test('a plan without an entry zone is PLAN_INVALID, not PRICE_MOVED', () => {
  const plan = structuredClone(GATE_PLAN)
  delete plan.entry.zone_low
  delete plan.entry.zone_high
  const verdict = evaluateEntryPlan(gateContext({ plan }))
  assert.equal(verdict.ok, false)
  assert.equal(verdict.code, 'PLAN_INVALID')
  assert.equal(verdict.sub_code, 'ENTRY_ZONE_MISSING')
  assert.match(verdict.detail, /入场区间/)
})

test('a plan without valid_until never inherits a default expiry', () => {
  const plan = structuredClone(GATE_PLAN)
  delete plan.valid_until
  const verdict = evaluateEntryPlan(gateContext({ plan }))
  assert.equal(verdict.code, 'VALID_UNTIL_MISSING')
})

test('an expired plan is refused even when everything else is fresh', () => {
  const verdict = evaluateEntryPlan(gateContext({ now: Date.parse('2026-11-09T01:51:00Z') }))
  assert.equal(verdict.code, 'VALID_UNTIL_EXPIRED')
})

test('background evidence and execution quote have separate TTLs', () => {
  // 背景证据 180s 仍是新鲜，只有执行盘口过期：必须报 QUOTE_STALE。
  const quote = { last: 68200.0, age_ms: 16_000, background_age_ms: 170_000 }
  const verdict = evaluateEntryPlan(gateContext({ quote }))
  assert.equal(verdict.code, 'QUOTE_STALE')
  // 反过来：盘口新鲜但背景证据过期 → BACKGROUND_EVIDENCE_STALE。
  const staleBackground = { last: 68200.0, age_ms: 1_000, background_age_ms: 181_000 }
  assert.equal(
    evaluateEntryPlan(gateContext({ quote: staleBackground })).code,
    'BACKGROUND_EVIDENCE_STALE',
  )

    // 若把两者压成同一个 15s，170s 的背景证据会被判过期——这正是要避免的。
  const bothFresh = evaluateEntryPlan(gateContext())
  assert.equal(bothFresh.ok, true)
})

test('price outside the approved zone is refused instead of chased', () => {
  const quote = { last: 68400.0, age_ms: 1_000, background_age_ms: 10_000 }
  const verdict = evaluateEntryPlan(gateContext({ quote }))
  assert.equal(verdict.code, 'PRICE_OUT_OF_ZONE')
  assert.match(verdict.detail, /不在计划允许的入场区间/)
})

test('SL/TP on the wrong side is refused', () => {
  const plan = structuredClone(GATE_PLAN)
  plan.stop_loss.price = 69_000.0
  assert.equal(evaluateEntryPlan(gateContext({ plan })).code, 'SL_TP_INVALID')
})

test('gate never throws on malformed input', () => {
  assert.equal(evaluateEntryPlan({}).code, 'PLAN_MISSING')
  assert.equal(evaluateEntryPlan({ plan: null }).code, 'PLAN_MISSING')
  assert.equal(evaluateEntryPlan({ plan: {} }).code, 'PLAN_INVALID')
})

test('observe mode records the gate verdict instead of stopping the round', async () => {
  const fake = makeFake()
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: { ...baseConfig(), executionGate: 'observe' },
  })
  assert.equal(result.ok, true, JSON.stringify(result.error))
  // 夹具的证据是历史时点，背景证据必然过期；observe 只记录不拦截。
  assert.equal(result.outputs.gate.ok, false)
  assert.equal(result.outputs.gate.enforced, false)
  assert.equal(result.outputs.gate.mode, 'observe')
  assert.ok(result.outputs.gate.code)
})

test('enforce mode blocks a plan the deterministic gate cannot verify', async () => {
  const fake = makeFake()
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: { ...baseConfig(), executionGate: 'enforce' },
  })
  assert.equal(result.ok, true)
  assert.equal(result.outcome, 'no_trade')
  assert.match(result.reasonCode, /^EXECUTION_BLOCKED:/)
  assert.equal(result.outputs.gate.enforced, true)
  // 关键：模型自报 executable=true 不足以放行。
  assert.equal(result.outputs.execution.executable, true)
})

test('gate off mode keeps the legacy behaviour and still reports mode', async () => {
  const fake = makeFake()
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: { ...baseConfig(), executionGate: 'off' },
  })
  assert.equal(result.outcome, 'trade')
  assert.equal(result.outputs.gate.mode, 'off')
  assert.equal(result.outputs.gate.ok, true)
})
// ------------------------------------------------------------ T16 §8·D 收尾
// 提示层硬化（zone/valid_until）、中文缺口原因、五类失败计数与 enforce 数据支撑。

const HARDENED_PLAN = {
  ...RED_PLAN,
  // 与 RED_PLAN 同一 shape，只补上 T16 要求的两个可验证字段。
  valid_until: '2026-11-09T01:55:00Z',
}

const DEBATE_SLICE = () => sliceEvidence(buildEvidenceView(evidenceFixture(), { now: NOW }), 'debate').data
const DECISION_SLICE = () => sliceEvidence(buildEvidenceView(evidenceFixture(), { now: NOW }), 'decision').data

test('prompt layer makes entry.zone_low/zone_high and valid_until hard requirements with a real basis', () => {
  for (const role of ['red_opening', 'blue_opening', 'red_response', 'blue_response']) {
    const prompt = buildPrompt(role, {
      symbol: 'BTCUSDT',
      evidence: role.endsWith('_response') ? { ...DEBATE_SLICE(), age_ms: 100_000 } : DEBATE_SLICE(),
      analysis: analysisFixture(),
      ownOpening: debateFixture({ side: role.startsWith('red') ? 'LONG' : 'SHORT', round: 1, confidence: 0.6, plan: role.startsWith('red') ? RED_PLAN : BLUE_PLAN }),
      opponentOpening: debateFixture({ side: role.startsWith('red') ? 'SHORT' : 'LONG', round: 1, confidence: 0.5, plan: role.startsWith('red') ? BLUE_PLAN : RED_PLAN }),
      opponentCallId: 'run-x:blue_team:r1:1',
      constraints: {},
    })
    // 硬要求写在系统提示里，不是"建议"。
    if (role.endsWith('_opening')) {
      assert.match(prompt.system, /HARD REQUIREMENT/)
      assert.match(prompt.system, /entry\.zone_low and entry\.zone_high are both mandatory/)
      assert.match(prompt.system, /valid_until is mandatory/)
      assert.match(prompt.system, /plan_basis\.zone_basis/)
      assert.match(prompt.system, /plan_basis\.valid_until_sources/)
    } else {
      // R2 不允许重新推导或丢掉 R1 的区间/失效时间（plan-copy 是整份计划比对）。
      assert.match(prompt.system, /including entry\.zone_low, entry\.zone_high and valid_until/)
      assert.match(prompt.system, /Never re-derive, round or drop the R1 zone or valid_until/)
    }
    const payload = JSON.parse(prompt.user)
    assert.deepEqual(payload.plan_basis.required_fields, ['entry.zone_low', 'entry.zone_high', 'valid_until'])
    // 取值依据是真实数字（levels/ATR），不是让模型瞎填。
    assert.equal(payload.plan_basis.zone_basis.anchor_price, 68234.5)
    assert.equal(payload.plan_basis.zone_basis.atr_pct_15m, 0.61)
    assert.ok(Math.abs(payload.plan_basis.zone_basis.atr_price_15m - 416.23045) < 1e-6)
    assert.ok(Math.abs(payload.plan_basis.zone_basis.min_width - 208.115225) < 1e-6)
    assert.equal(payload.plan_basis.zone_basis.supports[0].price, 68100)
    assert.equal(payload.plan_basis.zone_basis.resistances[0].price, 68820)
    // valid_until 依据：来自计划失效条件（analysis candidate 的 trigger.valid_until）。
    assert.equal(payload.plan_basis.valid_until_basis.analysis_trigger_valid_until, '2026-11-09T03:45:20Z')
    assert.equal(payload.plan_basis.valid_until_basis.as_of, '2026-11-09T01:45:20Z')
  }

  const decision = buildPrompt('decision', {
    symbol: 'BTCUSDT', evidence: DECISION_SLICE(), analysis: analysisFixture(),
    redOpening: debateFixture({ side: 'LONG', round: 1, confidence: 0.68, plan: RED_PLAN }),
    blueOpening: debateFixture({ side: 'SHORT', round: 1, confidence: 0.55, plan: BLUE_PLAN }),
    redResponse: null, blueResponse: null, scorecard: {}, constraints: {},
  })
  assert.match(decision.system, /plan\.entry\.zone_low, plan\.entry\.zone_high and plan\.valid_until are mandatory/)
  const payload = JSON.parse(decision.user)
  assert.equal(payload.plan_basis.valid_until_basis.red_trigger_valid_until, '2026-11-09T03:45:20Z')
  assert.equal(payload.plan_basis.valid_until_basis.blue_trigger_valid_until, '2026-11-09T03:45:20Z')
})

test('plan basis never invents values when the evidence lacks levels or ATR', () => {
  const basis = entryPlanBasis({ evidence: { symbol: { price: { last: 100 } } }, symbol: 'BTCUSDT' })
  assert.equal(basis.zone_basis.anchor_price, 100)
  assert.equal(basis.zone_basis.atr_pct_15m, null)
  assert.equal(basis.zone_basis.atr_price_15m, null)
  assert.equal(basis.zone_basis.min_width, null)
  assert.deepEqual(basis.zone_basis.supports, [])
  assert.deepEqual(basis.zone_basis.resistances, [])
  assert.equal(basis.valid_until_basis.analysis_trigger_valid_until, null)
  // 完全没数据时也不能抛，只能给 null。
  const empty = entryPlanBasis({})
  assert.equal(empty.zone_basis.anchor_price, null)
  assert.deepEqual(empty.required_fields, ['entry.zone_low', 'entry.zone_high', 'valid_until'])
})

test('describePlanGaps explains a missing zone / expiry in Chinese without judging', () => {
  assert.equal(describePlanGaps(null)[0].code, 'PLAN_MISSING')
  assert.match(describePlanGaps(null)[0].reason_zh, /计划整体缺失/)
  const noZone = structuredClone(HARDENED_PLAN)
  delete noZone.entry.zone_low
  delete noZone.entry.zone_high
  const zoneGaps = describePlanGaps(noZone)
  assert.equal(zoneGaps.length, 1)
  assert.equal(zoneGaps[0].code, 'ENTRY_ZONE_MISSING')
  assert.equal(zoneGaps[0].field, 'entry.zone_low/entry.zone_high')
  assert.match(zoneGaps[0].reason_zh, /入场区间/)
  assert.match(zoneGaps[0].reason_zh, /levels/)
  const noExpiry = structuredClone(HARDENED_PLAN)
  delete noExpiry.valid_until
  assert.equal(describePlanGaps(noExpiry)[0].code, 'VALID_UNTIL_MISSING')
  assert.match(describePlanGaps(noExpiry)[0].reason_zh, /失效时间/)
  assert.deepEqual(describePlanGaps(HARDENED_PLAN), [])
})

test('hardened prompt contract does not relax any existing schema or semantic check', () => {
  // 1) schema 仍然拒绝多余字段（没有任何"为了让 zone 通过"的放宽）。
  const extraField = structuredClone(HARDENED_PLAN)
  extraField.entry.zone_mid = 68_175
  assert.ok(validateSchema(SCHEMAS.decision, { ...decisionFixture(), plan: extraField }).length > 0)
  const missingSl = structuredClone(HARDENED_PLAN)
  delete missingSl.stop_loss
  assert.ok(validateSchema(SCHEMAS.decision, { ...decisionFixture(), plan: missingSl }).length > 0)
  // 2) plan-copy 语义仍是强校验：改 zone 或改 valid_until 都必须被拒。
  const openings = {
    red: debateFixture({ side: 'LONG', round: 1, confidence: 0.68, plan: HARDENED_PLAN }),
    blue: debateFixture({ side: 'SHORT', round: 1, confidence: 0.55, plan: BLUE_PLAN }),
  }
  const context = {
    cycleId: CYCLE_ID,
    caps: { maxRiskPct: 1, maxLeverage: 10, minRr: 1.2, slMinPct: 0.2, slMaxPct: 5 },
    primarySymbol: 'BTCUSDT',
    openings,
    scorecard: { red: { gate: 'PASS', rubric_score: 0.82 }, blue: { gate: 'FAIL_RR', rubric_score: 0.61 }, tie: false },
  }
  const copied = { ...decisionFixture(), plan: { ...HARDENED_PLAN, risk_pct: 0.7 } }
  assert.equal(validateSemantics('decision', copied, context).length, 0)
  const zoneDrift = { ...decisionFixture(), plan: { ...HARDENED_PLAN, entry: { ...HARDENED_PLAN.entry, zone_low: 67_000 }, risk_pct: 0.7 } }
  assert.ok(validateSemantics('decision', zoneDrift, context).length > 0)
  const expiryDrift = { ...decisionFixture(), plan: { ...HARDENED_PLAN, valid_until: '2026-11-09T09:00:00Z', risk_pct: 0.7 } }
  assert.ok(validateSemantics('decision', expiryDrift, context).length > 0)
  // 3) 提示硬化没有把 zone/valid_until 变成 schema 必填项：
  //    通过率不会被"换一个地方收紧"悄悄改掉，拦截点只在确定性闸门。
  const noZonePlan = { ...RED_PLAN, entry: { style: 'MARKET', price: 68234.5 } }
  assert.equal(validateSchema(SCHEMAS.debate_case, debateFixture({ side: 'LONG', round: 1, confidence: 0.68, plan: noZonePlan })).length, 0)
  assert.equal(validateSemantics('red_opening', debateFixture({ side: 'LONG', round: 1, confidence: 0.68, plan: noZonePlan }), { cycleId: CYCLE_ID, primarySymbol: 'BTCUSDT' }).length, 0)
})

test('a decision plan without a zone is blocked with a readable Chinese reason', async () => {
  const noZonePlan = { ...RED_PLAN, entry: { style: 'MARKET', price: 68234.5 } }
  const fake = makeFake({ redPlan: noZonePlan, decisionPlan: { ...noZonePlan, risk_pct: 0.7 } })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: { ...baseConfig(), executionGate: 'enforce' },
  })
  assert.equal(result.ok, true)
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.reasonCode, 'EXECUTION_BLOCKED:PLAN_INVALID')
  assert.equal(result.failureCategory, FAILURE_CATEGORIES.MODEL_ERROR)
  assert.match(result.outputs.gate.detail, /入场区间/)
  const gap = result.outputs.gate.gaps[0]
  assert.equal(gap.code, 'ENTRY_ZONE_MISSING')
  assert.match(gap.reason_zh, /入场区间/)
  // 缺口在 R1 就已产生，链上要能指出来源，而不是只怪 decision。
  assert.equal(result.outputs.plan_gaps.red_opening[0].code, 'ENTRY_ZONE_MISSING')
  assert.equal(result.outputs.gate.upstream_gaps.red_opening[0].code, 'ENTRY_ZONE_MISSING')
  // 模型自报 executable=true 仍不足以放行。
  assert.equal(result.outputs.execution.executable, true)
  // schema/语义校验没有被收紧或放宽：8 次调用全部 schema_ok。
  assert.ok(result.calls.every((call) => call.schema_ok === true))
})

test('a decision plan without valid_until is blocked with a readable Chinese reason', async () => {
  const fake = makeFake() // 默认 RED_PLAN 有 zone、没有 valid_until
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: { ...baseConfig(), executionGate: 'enforce' },
  })
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.reasonCode, 'EXECUTION_BLOCKED:VALID_UNTIL_MISSING')
  assert.match(result.outputs.gate.detail, /失效时间/)
  assert.equal(result.outputs.gate.gaps[0].code, 'VALID_UNTIL_MISSING')
  assert.match(result.outputs.gate.gaps[0].reason_zh, /trigger\.valid_until/)
  // observe 模式同样要能看到中文缺口（不能"评估了但没人看见"）。
  const observed = await runChain({
    llm: makeFake().llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: { ...baseConfig(), executionGate: 'observe' },
  })
  assert.equal(observed.outcome, 'trade')
  assert.equal(observed.outputs.gate.code, 'VALID_UNTIL_MISSING')
  assert.equal(observed.outputs.plan_gaps.decision[0].code, 'VALID_UNTIL_MISSING')
})

test('enforce lets a plan through once the model supplies zone and valid_until', async () => {
  // 证据在 5s 前构建：盘口真实年龄 5s + 构建时 1s = 6s < 15s，闸门可验证。
  const freshEvidence = evidenceFixture()
  freshEvidence.produced_at = '2026-11-09T01:46:55Z'
  freshEvidence.as_of = '2026-11-09T01:46:55Z'
  delete freshEvidence.age_ms
  const fake = makeFake({ redPlan: HARDENED_PLAN, decisionPlan: { ...HARDENED_PLAN, risk_pct: 0.7 } })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: freshEvidence,
    hardRisk: { verdict: 'approved' },
    config: { ...baseConfig(), executionGate: 'enforce' },
  })
  assert.equal(result.ok, true, JSON.stringify(result.error))
  assert.equal(result.outcome, 'trade', JSON.stringify(result.outputs.gate))
  assert.equal(result.outputs.gate.ok, true)
  assert.equal(result.outputs.gate.enforced, true)
  // 红队与 decision 的计划完整；蓝队仍缺 zone/valid_until，所以缺口只应指向蓝队。
  assert.deepEqual(Object.keys(result.outputs.plan_gaps), ['blue_opening'])
  assert.deepEqual(Object.keys(result.outputs.gate.upstream_gaps), ['blue_opening'])
  // 闸门用注入时钟，回放可复现。
  assert.equal(result.outputs.gate.checked_at, NOW)
  assert.ok(result.outputs.gate.checks.some((check) => check.code === 'PRICE_IN_ZONE' && check.ok))
  // 闸门记录的是真实盘口年龄（证据构建→校验 5s + 构建时 1s），不是构建时的 age_s。
  assert.equal(result.outputs.gate.quote.age_ms, 6_000)
  assert.equal(result.outputs.gate.quote.source, 'evidence_build')
})

test('a complete plan on a frozen 100s-old quote is QUOTE_STALE, not falsely fresh', async () => {
  // v1 用证据冻结价执行；这份证据构建于 100s 前。若闸门只报 price.age_s(1s)，
  // 就会在一份 100s 前的价格上宣称新鲜——这里锁死"必须报陈旧"。
  const fake = makeFake({ redPlan: HARDENED_PLAN, decisionPlan: { ...HARDENED_PLAN, risk_pct: 0.7 } })
  const result = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: { ...baseConfig(), executionGate: 'enforce' },
  })
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.reasonCode, 'EXECUTION_BLOCKED:QUOTE_STALE')
  assert.match(result.outputs.gate.detail, /执行盘口报价超过有效期/)
  assert.equal(result.outputs.gate.quote.age_ms, 101_000)
})

// T12 §4.4 的 S1 六轮：4 轮 PRICE_MOVED、1 轮 DECISION_INVALID（2 次 invalid 调用）、
// 1 轮 BOTH_SIDES_UNTRADEABLE；02fcd8 另有 1 次 invalid decision 调用后修复成功。
const T12_S1_ROUNDS = [
  { reason_code: 'EXECUTION_BLOCKED:PRICE_MOVED', outcome: 'no_trade', calls: [] },
  { reason_code: 'BOTH_SIDES_UNTRADEABLE', outcome: 'no_trade', calls: [] },
  {
    reason_code: 'DECISION_INVALID',
    outcome: 'no_trade',
    calls: [
      { status: 'invalid', error_code: 'SCHEMA_INVALID' },
      { status: 'invalid', error_code: 'SCHEMA_INVALID' },
    ],
  },
  {
    reason_code: 'EXECUTION_BLOCKED:PRICE_MOVED',
    outcome: 'no_trade',
    calls: [
      { status: 'invalid', error_code: 'SCHEMA_INVALID' },
      { status: 'ok', error_code: null },
    ],
  },
  { reason_code: 'EXECUTION_BLOCKED:PRICE_MOVED', outcome: 'no_trade', calls: [] },
  { reason_code: 'EXECUTION_BLOCKED:PRICE_MOVED', outcome: 'no_trade', calls: [] },
]

test('five-category counts reproduce the T12 baseline on the same six rounds', () => {
  const summary = summarizeFailures(T12_S1_ROUNDS)
  assert.equal(summary.rounds, 6)
  assert.equal(summary.rounds_by_category[FAILURE_CATEGORIES.ENTRY_CONDITION_LOST], 4)
  assert.equal(summary.rounds_by_category[FAILURE_CATEGORIES.MODEL_ERROR], 1)
  assert.equal(summary.rounds_by_category[FAILURE_CATEGORIES.DETERMINISTIC_GATE], 1)
  assert.equal(summary.rounds_by_category[FAILURE_CATEGORIES.DATA_STALE], 0)
  assert.equal(summary.rounds_by_category[FAILURE_CATEGORIES.RISK_BLOCKED], 0)
  assert.equal(summary.rounds_by_category[FAILURE_CATEGORIES.EXCHANGE_FAILURE], 0)
  // T12 的"模型错误 3"是调用级口径，必须能对上。
  assert.equal(summary.call_failures, 3)
  assert.equal(summary.calls_by_category[FAILURE_CATEGORIES.MODEL_ERROR], 3)
  assert.deepEqual(summary.unclassified_codes, [])
  assert.equal(summary.reason_codes['EXECUTION_BLOCKED:PRICE_MOVED'], 4)
})

test('reason codes map to the five categories without guessing unknown ones', () => {
  const cases = [
    ['EXECUTION_BLOCKED:PRICE_MOVED', 'no_trade', FAILURE_CATEGORIES.ENTRY_CONDITION_LOST],
    ['EXECUTION_BLOCKED:PRICE_OUT_OF_ZONE', 'no_trade', FAILURE_CATEGORIES.ENTRY_CONDITION_LOST],
    ['EXECUTION_BLOCKED:VALID_UNTIL_EXPIRED', 'no_trade', FAILURE_CATEGORIES.ENTRY_CONDITION_LOST],
    ['EXECUTION_BLOCKED:PLAN_INVALID', 'no_trade', FAILURE_CATEGORIES.MODEL_ERROR],
    ['EXECUTION_BLOCKED:SL_TP_INVALID', 'no_trade', FAILURE_CATEGORIES.MODEL_ERROR],
    ['EXECUTION_BLOCKED:SNAPSHOT_STALE', 'no_trade', FAILURE_CATEGORIES.DATA_STALE],
    ['EXECUTION_BLOCKED:PROTECTION_UNSUPPORTED', 'no_trade', FAILURE_CATEGORIES.EXCHANGE_FAILURE],
    ['DECISION_INVALID', 'no_trade', FAILURE_CATEGORIES.MODEL_ERROR],
    ['DEBATE_INCOMPLETE', 'failed', FAILURE_CATEGORIES.MODEL_ERROR],
    ['LLM_TIMEOUT', 'failed', FAILURE_CATEGORIES.MODEL_ERROR],
    ['SNAPSHOT_EXPIRED', 'no_trade', FAILURE_CATEGORIES.DATA_STALE],
    ['HOST_STALE_CYCLE', 'error', FAILURE_CATEGORIES.DATA_STALE],
    ['RISK_UNAVAILABLE', 'no_trade', FAILURE_CATEGORIES.RISK_BLOCKED],
    ['CONTROL_LOCKED', 'no_trade', FAILURE_CATEGORIES.RISK_BLOCKED],
    ['E_RISK_SYMBOL_POSITION', 'no_trade', FAILURE_CATEGORIES.RISK_BLOCKED],
    ['P2_REJECTED', 'no_trade', FAILURE_CATEGORIES.EXCHANGE_FAILURE],
    ['BOTH_SIDES_UNTRADEABLE', 'no_trade', FAILURE_CATEGORIES.DETERMINISTIC_GATE],
    ['NO_EDGE', 'no_trade', FAILURE_CATEGORIES.NO_TRADE],
    ['SOMETHING_UNKNOWN', 'failed', FAILURE_CATEGORIES.UNCLASSIFIED],
  ]
  for (const [code, outcome, expected] of cases) {
    assert.equal(categorizeFailure(code, { outcome }).category, expected, code)
  }
  assert.equal(categorizeFailure('TRADE_PLAN_READY', { outcome: 'trade' }).category, null)
  assert.equal(categorizeFailure(null, { outcome: 'no_trade' }).category, FAILURE_CATEGORIES.NO_TRADE)
  assert.equal(categorizeFailure(null, { outcome: 'failed' }).category, FAILURE_CATEGORIES.UNCLASSIFIED)
  // 未归类必须原样保留 code，方便下一步补映射，而不是悄悄并进五类。
  const summary = summarizeFailures([{ reason_code: 'SOMETHING_UNKNOWN', outcome: 'failed', calls: [] }])
  assert.deepEqual(summary.unclassified_codes, ['SOMETHING_UNKNOWN'])
})


// ---------------------------------------------- T17 §8·E 链路变体（baseline/lean）
test('chain variant is a fixed enum resolved at config parse time, not a flow editor', async () => {
  assert.deepEqual(Object.values(CHAIN_VARIANTS), ['baseline', 'lean'])
  assert.equal(DEFAULT_CHAIN_VARIANT, 'baseline')
  assert.deepEqual([...VARIANT_ROLES.lean], [
    'red_opening', 'blue_opening', 'red_response', 'blue_response', 'risk', 'execution_entry',
  ])
  // lean 只比 baseline 少 analysis 与 decision 两个 LLM 角色。
  assert.deepEqual(
    [...VARIANT_ROLES.baseline].filter((role) => !VARIANT_ROLES.lean.includes(role)),
    ['analysis', 'decision'],
  )
  assert.deepEqual(
    [...VARIANT_ROLES.lean].filter((role) => !VARIANT_ROLES.baseline.includes(role)),
    [],
  )
  assert.deepEqual(resolveChainVariant({}), { ok: true, variant: 'baseline', roles: VARIANT_ROLES.baseline })
  assert.equal(resolveChainVariant({ chainVariant: 'lean' }).variant, 'lean')
  for (const bad of ['fast', 'lean2', '', 'BASELINE', ' Lean', ['lean'], { variant: 'lean' }, 1, true]) {
    const resolved = resolveChainVariant({ chainVariant: bad })
    assert.equal(resolved.ok, false, JSON.stringify(bad))
    assert.deepEqual(resolved.allowed, ['baseline', 'lean'])
  }

  const fake = makeFake()
  const rejected = await runChain({
    llm: fake.llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: { ...baseConfig(), chainVariant: 'lean2' },
  })
  assert.equal(rejected.ok, false)
  assert.equal(rejected.reasonCode, 'CHAIN_VARIANT_UNKNOWN')
  assert.equal(rejected.calls.length, 0, 'unknown variant must not spend a model call')
  assert.equal(fake.optionsSeen.length, 0)
  assert.equal(rejected.snapshot.variant, null)
  assert.equal(rejected.snapshot.requested_variant, 'lean2')
  assert.equal(rejected.snapshot.execution_gate, 'observe')
})

test('lean drops exactly analysis + decision and records variant/prompt versions in the round snapshot', async () => {
  const baseline = await runChain({
    llm: makeFake().llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  const lean = await runChain({
    llm: makeFake().llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: { ...baseConfig(), chainVariant: 'lean' },
  })
  assert.equal(baseline.outcome, 'trade', JSON.stringify(baseline.error))
  assert.equal(lean.outcome, 'trade', JSON.stringify(lean.error))
  assert.equal(baseline.variant, 'baseline')
  assert.equal(lean.variant, 'lean')
  assert.deepEqual(baseline.calls.map((call) => call.role_key), [...VARIANT_ROLES.baseline])
  assert.deepEqual(lean.calls.map((call) => call.role_key), [...VARIANT_ROLES.lean])
  assert.equal(baseline.outputs.analysis.mode, 'FULL')
  assert.equal(lean.outputs.analysis, null, 'lean must not call analysis')
  assert.equal(lean.calls.some((call) => call.role_key === 'analysis'), false)
  assert.equal(lean.calls.some((call) => call.role_key === 'decision'), false)

  // 计划质量：lean 原样复制 R1，连 risk_pct 都不改（不重新定价、不降风险换通过率）。
  assert.equal(lean.outputs.decision.action, 'TRADE')
  assert.equal(lean.outputs.decision.selected_case, 'red')
  assert.deepEqual(lean.outputs.decision.plan, baseline.outputs.redOpening.plan_draft)
  assert.equal(lean.outputs.decision.plan.risk_pct, baseline.outputs.redOpening.plan_draft.risk_pct)

  // 同一个 decision 语义校验也接受确定性决策（精简不是少校验）。
  const semanticErrors = validateSemantics('decision', lean.outputs.decision, {
    cycleId: CYCLE_ID,
    caps: { maxRiskPct: 1, maxLeverage: 10, minRr: 1.2, slMinPct: 0.2, slMaxPct: 5 },
    primarySymbol: 'BTCUSDT',
    openings: { red: baseline.outputs.redOpening, blue: baseline.outputs.blueOpening },
    scorecard: baseline.scorecard,
  })
  assert.equal(semanticErrors.length, 0, JSON.stringify(semanticErrors))
  assert.equal(
    validateSchema(SCHEMAS.decision, lean.outputs.decision).length,
    0,
    'deterministic decision must satisfy the same decision/1 schema',
  )

  // 每轮快照：变体 + 角色 + 提示词版本 + 链版本 + 证据指纹 + 闸门模式。
  assert.equal(baseline.snapshot.variant, 'baseline')
  assert.deepEqual(lean.snapshot.variant, 'lean')
  assert.deepEqual(lean.snapshot.roles, [...VARIANT_ROLES.lean])
  assert.deepEqual(lean.snapshot.prompt_versions, {
    red_opening: PROMPT_VERSIONS.red_opening,
    blue_opening: PROMPT_VERSIONS.blue_opening,
    red_response: PROMPT_VERSIONS.red_response,
    blue_response: PROMPT_VERSIONS.blue_response,
    risk: PROMPT_VERSIONS.risk,
    execution_entry: PROMPT_VERSIONS.execution_entry,
  })
  assert.equal(lean.snapshot.prompt_versions.analysis, undefined)
  assert.equal(baseline.snapshot.prompt_versions.analysis, 'analysis.v2.zh2')
  assert.equal(baseline.snapshot.prompt_versions.decision, 'decision.v3.zh2')
  assert.equal(lean.snapshot.chain_version, 'ai-chain/1')
  assert.equal(lean.snapshot.execution_gate, 'observe')
  assert.equal(lean.snapshot.evidence_hash, baseline.snapshot.evidence_hash)
  // 每轮快照与审计里的 prompt_version 必须一致（否则账本无法区分硬化前后）。
  for (const call of lean.calls) assert.equal(call.prompt_version, lean.snapshot.prompt_versions[call.role_key])
})

test('lean keeps the risk, execution and gateway stages, so it cannot win by dropping checks', async () => {
  const seen = { p1: 0, p2: 0 }
  const gateways = {
    p1: async () => { seen.p1 += 1; return { verdict: 'approved', code: null } },
    p2: async () => { seen.p2 += 1; return { ok: true, client_order_id: 'wb-test-1' } },
  }
  for (const chainVariant of ['baseline', 'lean']) {
    seen.p1 = 0
    seen.p2 = 0
    const result = await runChain({
      llm: makeFake().llm,
      route: { provider: 'fake', model: 'fake-1' },
      evidence: evidenceFixture(),
      hardRisk: { verdict: 'approved' },
      gateways,
      config: { ...baseConfig(), chainVariant },
    })
    assert.equal(result.outcome, 'trade', chainVariant + ' ' + JSON.stringify(result.error))
    assert.equal(seen.p1, 1, chainVariant + ' must still call P1')
    assert.equal(seen.p2, 1, chainVariant + ' must still call P2')
    assert.ok(result.calls.some((call) => call.role_key === 'risk'), chainVariant + ' keeps risk AI')
    assert.ok(result.calls.some((call) => call.role_key === 'execution_entry'), chainVariant + ' keeps executor')
    assert.equal(result.outputs.risk.recommendation, 'APPROVE')
    assert.ok(result.outputs.execution.order_plan)
  }
  // 硬风控/控制态在 lean 上同样先于交易：rejected 一律阻断。
  const blocked = await runChain({
    llm: makeFake().llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'rejected', violations: [] },
    config: { ...baseConfig(), chainVariant: 'lean' },
  })
  assert.equal(blocked.reasonCode, 'HARD_RISK_REJECT')
  assert.equal(blocked.failureCategory, FAILURE_CATEGORIES.RISK_BLOCKED)
  assert.equal(blocked.outputs.risk, undefined, 'hard risk must still short-circuit before risk AI')
})

test('the same execution gate runs on both variants and cannot be bypassed by the lean path', async () => {
  const noZonePlan = { ...RED_PLAN, entry: { style: 'MARKET', price: 68234.5 } }
  const verdicts = {}
  for (const chainVariant of ['baseline', 'lean']) {
    const fake = makeFake({
      redPlan: noZonePlan,
      decisionPlan: chainVariant === 'baseline' ? { ...noZonePlan, risk_pct: 0.7 } : null,
    })
    const result = await runChain({
      llm: fake.llm,
      route: { provider: 'fake', model: 'fake-1' },
      evidence: evidenceFixture(),
      hardRisk: { verdict: 'approved' },
      config: { ...baseConfig(), chainVariant, executionGate: 'enforce' },
    })
    assert.equal(result.outcome, 'no_trade', chainVariant)
    assert.equal(result.reasonCode, 'EXECUTION_BLOCKED:PLAN_INVALID', chainVariant)
    assert.equal(result.outputs.gate.enforced, true)
    assert.equal(result.outputs.gate.gaps[0].code, 'ENTRY_ZONE_MISSING')
    assert.equal(result.outputs.gate.quote.source, 'evidence_build')
    assert.equal(result.failureCategory, FAILURE_CATEGORIES.MODEL_ERROR)
    verdicts[chainVariant] = {
      code: result.outputs.gate.code,
      sub_code: result.outputs.gate.sub_code,
      checks: result.outputs.gate.checks.map((check) => check.code).join(','),
    }
  }
  // 同一份 evaluateEntryPlan、同一份计划语义 → 两个变体的判定逐字段相同。
  assert.deepEqual(verdicts.lean, verdicts.baseline)

  // 反过来：计划完整 + 证据新鲜时，两个变体都被放行（闸门不会因变体而变严/变松）。
  const freshEvidence = evidenceFixture()
  freshEvidence.produced_at = '2026-11-09T01:46:55Z'
  freshEvidence.as_of = '2026-11-09T01:46:55Z'
  delete freshEvidence.age_ms
  for (const chainVariant of ['baseline', 'lean']) {
    const fake = makeFake({
      redPlan: HARDENED_PLAN,
      decisionPlan: chainVariant === 'baseline' ? { ...HARDENED_PLAN, risk_pct: 0.7 } : null,
    })
    const result = await runChain({
      llm: fake.llm,
      route: { provider: 'fake', model: 'fake-1' },
      evidence: freshEvidence,
      hardRisk: { verdict: 'approved' },
      config: { ...baseConfig(), chainVariant, executionGate: 'enforce' },
    })
    assert.equal(result.outcome, 'trade', chainVariant + ' ' + JSON.stringify(result.outputs.gate))
    assert.equal(result.outputs.gate.ok, true)
    assert.equal(result.outputs.gate.quote.source, 'evidence_build')
  }
})

test('deterministic selection follows the scorecard and never breaks a tie', () => {
  const openings = {
    red: debateFixture({ side: 'LONG', round: 1, confidence: 0.68, plan: HARDENED_PLAN }),
    blue: debateFixture({ side: 'SHORT', round: 1, confidence: 0.55, plan: BLUE_PLAN }),
  }
  const scorecard = (redGate, blueGate, redScore, blueScore, tie = false) => ({
    red: { gate: redGate, rubric_score: redScore },
    blue: { gate: blueGate, rubric_score: blueScore },
    tie,
  })
  assert.deepEqual(selectDeterministicCase({ openings, scorecard: scorecard('PASS', 'PASS', 0.8, 0.6) }), {
    action: 'TRADE', selectedKey: 'red', needsOverride: false,
  })
  assert.deepEqual(selectDeterministicCase({ openings, scorecard: scorecard('PASS', 'PASS', 0.6, 0.8) }), {
    action: 'TRADE', selectedKey: 'blue', needsOverride: false,
  })
  // 平局必须 NO_TRADE：这是 decision prompt 的硬规则，代码不得替模型打破它。
  const tie = selectDeterministicCase({ openings, scorecard: scorecard('PASS', 'PASS', 0.7, 0.7, true) })
  assert.equal(tie.action, 'NO_TRADE')
  assert.equal(tie.code, 'SCORECARD_TIE')
  // 只有 gate=PASS 的一侧可选。
  assert.equal(selectDeterministicCase({ openings, scorecard: scorecard('FAIL_RR', 'PASS', 0.9, 0.5) }).selectedKey, 'blue')
  const none = selectDeterministicCase({ openings, scorecard: scorecard('FAIL_RR', 'FAIL_RR', 0.9, 0.5) })
  assert.equal(none.action, 'NO_TRADE')
  assert.equal(none.code, 'NO_SIDE_PASSES_GATE')
})

test('deterministic decision object passes the same decision semantics, including override rules', () => {
  const openings = {
    red: debateFixture({ side: 'LONG', round: 1, confidence: 0.68, plan: HARDENED_PLAN }),
    blue: debateFixture({ side: 'SHORT', round: 1, confidence: 0.55, plan: BLUE_PLAN }),
  }
  const context = (scorecard) => ({
    cycleId: CYCLE_ID,
    caps: { maxRiskPct: 1, maxLeverage: 10, minRr: 1.2, slMinPct: 0.2, slMaxPct: 5 },
    primarySymbol: 'BTCUSDT',
    openings,
    scorecard,
  })
  const redWins = { red: { gate: 'PASS', rubric_score: 0.82 }, blue: { gate: 'FAIL_RR', rubric_score: 0.61 }, tie: false }
  const picked = deterministicDecision({ cycleId: CYCLE_ID, selectedKey: 'red', openings, scorecard: redWins })
  assert.equal(picked.score_override, false)
  assert.equal(picked.override_rationale, null)
  assert.deepEqual(picked.plan, HARDENED_PLAN)
  assert.equal(validateSemantics('decision', picked, context(redWins)).length, 0)
  assert.equal(validateSchema(SCHEMAS.decision, picked).length, 0)

  // 选分数更低但唯一可执行的一侧：必须带 override 理由，且理由不是"免检通行证"。
  const blueOnlyPass = { red: { gate: 'FAIL_RR', rubric_score: 0.9 }, blue: { gate: 'PASS', rubric_score: 0.5 }, tie: false }
  const overridden = deterministicDecision({ cycleId: CYCLE_ID, selectedKey: 'blue', openings, scorecard: blueOnlyPass })
  assert.equal(overridden.score_override, true)
  assert.ok(overridden.override_rationale)
  assert.equal(validateSemantics('decision', overridden, context(blueOnlyPass)).length, 0)
  const withoutOverride = { ...overridden, score_override: false, override_rationale: null }
  assert.ok(
    validateSemantics('decision', withoutOverride, context(blueOnlyPass)).length > 0,
    'the override flag is required by the same semantics, not granted by the lean path',
  )

  const noTrade = deterministicNoTrade({ cycleId: CYCLE_ID, code: 'SCORECARD_TIE', detail: '两侧分数相同。' })
  assert.equal(validateSemantics('decision', noTrade, context({ red: { gate: 'PASS', rubric_score: 0.7 }, blue: { gate: 'PASS', rubric_score: 0.7 }, tie: true })).length, 0)
  assert.equal(validateSchema(SCHEMAS.decision, noTrade).length, 0)
  assert.equal(noTrade.plan, null)
  assert.equal(noTrade.no_trade_reason.code, 'SCORECARD_TIE')
})

test('prompt version bump is label-only: the version string never enters the prompt text', () => {
  // 只校验"格式含版本 + 语言标记"，不锁死具体轮次；具体期望值只有一处
  // （文件顶部那张显式表，逐个 strictEqual），这样以后 bump 只改一处、不会假红。
  for (const role of Object.keys(PROMPT_VERSIONS)) {
    assert.match(PROMPT_VERSIONS[role], /\.v[0-9]+\.zh[0-9]+$/, role)
    const prompt = buildPrompt(role, { symbol: 'BTCUSDT', evidence: {}, analysis: {}, constraints: {} })
    assert.equal(prompt.system.includes(prompt.promptVersion), false, role)
    assert.equal(prompt.user.includes(prompt.promptVersion), false, role)
    assert.equal(/\.v[0-9]+\.zh[0-9]+/.test(prompt.system + prompt.user), false, role)
  }
})


// ------------------------------------------- T17 §8·E 离线评估脚本（replay）
const EVAL_SCRIPT_URL = new URL('../../scripts/evaluate-agent-chain.mjs', import.meta.url)

function mdEvidenceFixture() {
  const view = evidenceFixture()
  return {
    schema_version: 'md-evidence/1',
    snapshot_id: view.snapshot_id,
    cycle_id: view.cycle_id,
    produced_at: view.produced_at,
    // 服务端真实形态：as_of 是对象（bar_age/clock），生产代码回落到 produced_at。
    as_of: { server_time_ms: Date.parse(view.produced_at), clock_trusted: true },
    data_health: view.data_health,
    market: view.market,
    universe: { deep: [{ symbol: 'BTCUSDT' }, { symbol: 'ETHUSDT' }] },
    symbols: view.symbols,
    delivery: view.delivery,
  }
}

function replayRole(text, latency_ms, input_tokens, output_tokens) {
  return { text, status: 'ok', latency_ms, input_tokens, output_tokens, latency_source: 'ai_calls.latency_ms', ledger_valid: true }
}

function replaySample() {
  const roles = {
    analysis: replayRole(JSON.stringify(analysisFixture()), 26_000, 3_300, 1_200),
    red_opening: replayRole(JSON.stringify(debateFixture({ side: 'LONG', round: 1, confidence: 0.68, plan: RED_PLAN })), 12_000, 2_000, 900),
    blue_opening: replayRole(JSON.stringify(debateFixture({ side: 'SHORT', round: 1, confidence: 0.55, plan: BLUE_PLAN })), 11_000, 2_000, 850),
    red_response: replayRole(JSON.stringify(debateFixture({
      side: 'LONG', round: 2, confidence: 0.7, plan: RED_PLAN,
      respondsTo: 'run-' + CYCLE_ID + ':blue_team:r1:1',
      rebuttal: [{ target_claim: '资金费拥挤，多头拥挤', counter: '拥挤不等于反转，价格未放量滞涨。' }],
    })), 9_000, 1_900, 700),
    blue_response: replayRole(JSON.stringify(debateFixture({
      side: 'SHORT', round: 2, confidence: 0.52, plan: BLUE_PLAN,
      respondsTo: 'run-' + CYCLE_ID + ':red_team:r1:1',
      rebuttal: [{ target_claim: '15m/1h 趋势向上', counter: '趋势仍在，但双阻力未被突破。' }],
    })), 8_500, 1_900, 680),
    decision: replayRole(JSON.stringify(decisionFixture()), 18_340, 6_900, 2_400),
    risk: replayRole(JSON.stringify(riskFixture()), 13_900, 2_300, 1_200),
    execution_entry: replayRole(JSON.stringify(executionFixture()), 16_400, 3_200, 1_300),
  }
  return {
    contract: 't17/chain-replay/1',
    source: { kind: 'fixture' },
    rounds: [{
      cycle_id: CYCLE_ID,
      started_at: PRODUCED_AT,
      primary_symbol: 'BTCUSDT',
      produced_at_ms: Date.parse(PRODUCED_AT),
      evidence_sha256: 'sha256:fixture',
      evidence: mdEvidenceFixture(),
      roles,
    }],
    missing: [],
  }
}

test('offline evaluation compares baseline/lean on the same recorded evidence and is reproducible', async () => {
  const mod = await import(EVAL_SCRIPT_URL.href)
  const sample = replaySample()
  const first = await mod.buildComparison(sample)
  const second = await mod.buildComparison(sample)
  // 同输入同输出：两次运行逐字节一致（含报告哈希）。
  assert.equal(JSON.stringify(first), JSON.stringify(second))
  assert.equal(first.report_sha256, second.report_sha256)
  assert.match(first.report_sha256, /^sha256:[0-9a-f]{64}$/)
  assert.equal(first.deterministic, true)
  assert.equal(first.sample.rounds, 1)
  assert.equal(first.contract, 't17/chain-replay/1')

  // 同一历史时点证据、同一批模型输出：两个变体只差角色数。
  const baseRow = first.rows.baseline[0]
  const leanRow = first.rows.lean[0]
  assert.deepEqual(baseRow.role_sequence, [...VARIANT_ROLES.baseline])
  assert.deepEqual(leanRow.role_sequence, [...VARIANT_ROLES.lean])
  assert.equal(baseRow.calls, 8)
  assert.equal(leanRow.calls, 6)
  // 闸门在两变体上同码（同一份 evaluateEntryPlan）。
  assert.equal(baseRow.gate_code, leanRow.gate_code)
  assert.equal(baseRow.gate_quote_source, 'evidence_build')
  // 成本/耗时按账本记录回放，来自同一批调用记录。
  assert.equal(baseRow.critical_path_ms, 26_000 + 12_000 + 9_000 + 18_340 + 13_900 + 16_400)
  assert.equal(leanRow.critical_path_ms, 12_000 + 9_000 + 13_900 + 16_400)
  assert.equal(baseRow.input_tokens, 3_300 + 2_000 + 2_000 + 1_900 + 1_900 + 6_900 + 2_300 + 3_200)
  assert.equal(leanRow.input_tokens, 2_000 + 2_000 + 1_900 + 1_900 + 2_300 + 3_200)
  // 选边一致时计划逐字段一致（lean 只是原样复制 R1）。
  assert.equal(first.deltas.plan_identical_rate_same_selection, 1)
  assert.equal(first.deltas.gate_identical_rate_same_selection, 1)
  assert.equal(first.deltas.selection_identical_rate, 1)
  // 样本远小于门槛：结论必须是"保留 baseline"，不得自动切换。
  assert.equal(first.conclusion.recommendation, 'keep_baseline')
  assert.equal(first.conclusion.default_variant, 'baseline')
  assert.equal(first.conclusion.automatic_switch, false)
  assert.ok(first.conclusion.blocks_replacement.some((item) => item.includes('样本轮次')))

  // CSV 与 JSON 同源：每条记录一行，表头稳定。
  const csv = mod.renderCsv(first)
  const lines = csv.trim().split('\n')
  assert.equal(lines.length, 3)
  assert.match(lines[0], /^cycle_id,variant,outcome,reason_code/)
  assert.ok(lines[1].startsWith(CYCLE_ID + ',baseline,'))
  assert.ok(lines[2].startsWith(CYCLE_ID + ',lean,'))
})

test('offline evaluation script never touches trading APIs and claims no backtest returns', async () => {
  const src = fs.readFileSync(EVAL_SCRIPT_URL, 'utf8')
  for (const banned of ['fetch(', "from 'node:http'", "from 'node:https'", 'undici', 'axios', 'submitOrder(', 'placeOrder(', '/api/execution', '/api/control', '/api/orders']) {
    assert.equal(src.includes(banned), false, 'script must not reference ' + banned)
  }
  const mod = await import(EVAL_SCRIPT_URL.href)
  assert.equal(mod.SAFETY.network.startsWith('none'), true)
  assert.equal(mod.SAFETY.trading_writes.startsWith('none'), true)
  assert.match(mod.SAFETY.backtest_returns, /not produced/)
  const report = await mod.buildComparison(replaySample())
  // 只按"字段名"判定，不做文本匹配（说明文字里出现 pnl 不算收益字段）。
  const keys = new Set()
  const walk = (value, skip = false) => {
    if (Array.isArray(value)) { for (const item of value) walk(item, skip); return }
    if (!value || typeof value !== 'object') return
    for (const [key, item] of Object.entries(value)) {
      if (skip) continue
      keys.add(key)
      walk(item, key === 'safety')
    }
  }
  walk(report)
  for (const banned of ['pnl', 'profit', 'sharpe', 'equity_curve', 'backtest_return', 'return_pct']) {
    assert.equal([...keys].some((key) => key.toLowerCase().includes(banned)), false, 'report must not carry a ' + banned + ' field')
  }
  assert.match(report.safety.backtest_returns, /not produced/)
})

test('chain results carry the five-category label', async () => {
  const traded = await runChain({
    llm: makeFake().llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(traded.failureCategory, null)
  const blocked = await runChain({
    llm: makeFake({ executionBlocked: 'PRICE_MOVED' }).llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(blocked.reasonCode, 'EXECUTION_BLOCKED:PRICE_MOVED')
  assert.equal(blocked.failureCategory, FAILURE_CATEGORIES.ENTRY_CONDITION_LOST)
  const invalid = await runChain({
    llm: makeFake({ decisionSymbolDrift: true }).llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'approved' },
    config: baseConfig(),
  })
  assert.equal(invalid.reasonCode, 'DECISION_INVALID')
  assert.equal(invalid.failureCategory, FAILURE_CATEGORIES.MODEL_ERROR)
  const risk = await runChain({
    llm: makeFake().llm,
    route: { provider: 'fake', model: 'fake-1' },
    evidence: evidenceFixture(),
    hardRisk: { verdict: 'rejected', violations: [] },
    config: baseConfig(),
  })
  assert.equal(risk.reasonCode, 'HARD_RISK_REJECT')
  assert.equal(risk.failureCategory, FAILURE_CATEGORIES.RISK_BLOCKED)
})

