/**
 * 会话内 LLM 回归：不依赖外部 provider、不联网、不下单。
 *
 * Run: node trading-workbench/plugin/test/session-llm.test.mjs
 *
 * 目的：把 2026-11-09 端到端轮次里「模型输出不稳」这个变量隔离掉，
 * 只验证 Host AI 链 + schema/semantic + P1/P2 网关接线的正确性。
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { runChain } from '../host/ai/index.js'
import { createSessionLlm, SESSION_LLM_TRANSPORT } from '../host/session-llm.js'

const CYCLE_ID = 'cyc-20261109T014520Z-7f3a1c'
const PRODUCED_AT = '2026-11-09T01:45:20Z'
const NOW = Date.parse('2026-11-09T01:47:00Z')
const ROUTE = { provider: 'session', model: 'in-conversation' }
const HARD_RISK = { verdict: 'approved', approved_qty: '0.067', blockers: [] }

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

function evidenceFixture(cycleId = CYCLE_ID) {
  return {
    snapshot_id: `snap-${cycleId}`,
    cycle_id: cycleId,
    produced_at: PRODUCED_AT,
    as_of: PRODUCED_AT,
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

function config(overrides = {}) {
  return {
    now: () => NOW,
    chainDeadlineMs: 20_000,
    roleTimeoutsMs: {
      analysis: 2_000, red_opening: 2_000, blue_opening: 2_000,
      red_response: 2_000, blue_response: 2_000,
      decision: 2_000, risk: 2_000, execution_entry: 2_000,
    },
    ...overrides,
  }
}

test('no_trade script closes cleanly after one analysis call', async () => {
  const session = createSessionLlm({ script: 'no_trade' })
  const result = await runChain({
    llm: session.llm,
    route: ROUTE,
    evidence: evidenceFixture(),
    config: config(),
  })
  assert.equal(result.ok, true)
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.reasonCode, 'NO_CANDIDATE')
  assert.equal(result.calls.length, 1)
  assert.equal(result.calls[0].schema_ok, true)
  assert.equal(result.calls[0].role, 'analysis')
  assert.equal(session.optionsSeen.length, 1)
  assert.equal(session.transport, SESSION_LLM_TRANSPORT)
  assert.equal(Object.hasOwn(session.optionsSeen[0], 'tools'), false)
})

test('session LLM resolves cycle_id from the payload (no hard-coded drift)', async () => {
  const otherCycle = 'cyc-20261109T050000Z-abc123'
  const session = createSessionLlm({ script: 'no_trade' })
  const result = await runChain({
    llm: session.llm,
    route: ROUTE,
    evidence: evidenceFixture(otherCycle),
    config: config(),
  })
  assert.equal(result.cycleId, otherCycle)
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.calls.every((c) => c.schema_ok), true)
})

test('full_chain script runs all 8 roles and reaches P1/P2', async () => {
  const session = createSessionLlm({ script: 'full_chain' })
  const gatewayCalls = []
  const gateways = {
    p1: async (args) => {
      gatewayCalls.push('p1')
      assert.equal(args.cycleId, CYCLE_ID)
      return { verdict: 'approved', approved_qty: '0.067', token: 'test-token' }
    },
    p2: async () => {
      gatewayCalls.push('p2')
      return { ok: true, order_id: 'test-order' }
    },
  }
  const result = await runChain({
    llm: session.llm,
    route: ROUTE,
    evidence: evidenceFixture(),
    config: config(),
    hardRisk: HARD_RISK,
    gateways,
  })
  assert.equal(result.outcome, 'trade', JSON.stringify(result.error ?? result.detail))
  assert.equal(result.reasonCode, 'TRADE_PLAN_READY')
  assert.equal(result.calls.length, 8)
  assert.deepEqual(
    result.calls.map((c) => c.role),
    ['analysis', 'red_team', 'blue_team', 'red_team', 'blue_team', 'decision', 'risk', 'execution'],
  )
  assert.equal(result.calls.every((c) => c.schema_ok), true)
  assert.deepEqual(gatewayCalls, ['p1', 'p2'])
  assert.equal(session.optionsSeen.length, 8)
  assert.equal(session.optionsSeen.every((o) => !Object.hasOwn(o, 'tools')), true)
  assert.equal(session.optionsSeen.every((o) => !Object.hasOwn(o, 'fetch')), true)
})

test('unknown script is rejected explicitly', () => {
  assert.throws(() => createSessionLlm({ script: 'live' }), /unknown script/)
})

test('full_chain does not fabricate a trade when P2 rejects', async () => {
  const session = createSessionLlm({ script: 'full_chain' })
  const result = await runChain({
    llm: session.llm,
    route: ROUTE,
    evidence: evidenceFixture(),
    config: config(),
    hardRisk: HARD_RISK,
    gateways: {
      p1: async () => ({ verdict: 'approved', approved_qty: '0.067' }),
      p2: async () => ({ ok: false, code: 'E_TEST_MODE_NO_ORDER', detail: 'fixture mode' }),
    },
  })
  assert.equal(result.outcome, 'no_trade')
  assert.equal(result.calls.length, 8)
})
