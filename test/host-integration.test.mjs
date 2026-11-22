/**
 * M1.3b Host integration regression suite.
 *
 * Run: node trading-workbench/plugin/test/host-integration.test.mjs
 *
 * Uses a fake workbench-service (fetch) and a fake Harness LLM stream. No
 * network, no service, no DB, no real model and no real order.
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import { createHash } from 'node:crypto'
import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { createIntegration, HostIntegrationError, capsFromEnvelope, portfolioGapText, resolveDefaultDataDir, DECLARED_QTY_UPPER_BOUND } from '../host/integration.js'
import { PLUGIN_ID, PLUGIN_VERSION } from '../index.js'
import { createAutoScheduler } from '../host/scheduler.js'
import { toRoundView } from '../host/round-view.js'
import { BUILD, currentBuild, hostLoadSeq, markHostLoad, setLoadedConfigRevision } from '../host/build.js'
import { CHAIN_VERSION, PROMPT_VERSIONS } from '../host/ai/constants.js'
import { ATTRIBUTION_CODES, attributeRound, stopKindOf } from '../host/attribution.js'
import { loadApiToken, READY_TIMEOUT_MS, WorkbenchServiceClient } from '../host/service-client.js'

const NOW = Date.parse('2026-11-09T01:47:00Z')
const SERVICE_TOKEN = 'test-service-token'
const ROUTE = { provider: 'fake', model: 'fake-1' }
const SERVICE_ACCOUNT = {
  equity: { session: '10000', exchange: '10100', available: '9000' },
  account: {
    total_margin_balance: '10000',
    total_wallet_balance: '9900',
    available_balance: '9000',
    total_unrealized_profit: '100',
  },
}

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

function analysisFixture(cycleId) {
  return {
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
    symbols_analyzed: ['BTCUSDT'],
    candidates: [{
      symbol: 'BTCUSDT',
      side_bias: 'BOTH',
      setup_type: 'retest',
      thesis: '15m 回踩 EMA20 未破，1h 结构仍为 HL。',
      trigger: { description: '15m 回踩 68100–68200 不破并重新站上 EMA20', price: 68100.0, valid_until: '2026-11-09T03:45:20Z' },
      invalidation: { price: 67410.2, condition: '15m 收盘跌破 EMA20 且 1h 转为 LH/LL' },
      rubric: { trend_alignment: 0.9, level_confluence: 0.8, momentum: 0.7, volatility_fit: 0.8, liquidity: 0.9 },
      key_evidence_refs: ['symbols.BTCUSDT.price.last'],
      flags: ['funding_extreme'],
    }],
    primary_symbol: 'BTCUSDT',
    stand_aside: false,
    stand_aside_reason: null,
    confidence: 0.7,
    evidence_refs: ['symbols.BTCUSDT.price.last'],
    warnings: [],
  }
}

function standAsideFixture(cycleId) {
  return {
    ...analysisFixture(cycleId),
    candidates: [],
    primary_symbol: null,
    stand_aside: true,
    stand_aside_reason: 'NO_CANDIDATE',
  }
}

function debateFixture(cycleId, { side, round, confidence, plan, respondsTo = null, rebuttal = [] }) {
  const isRed = side === 'LONG'
  return {
    schema_version: 'debate_case/1',
    cycle_id: cycleId,
    agent: isRed ? 'red_team' : 'blue_team',
    symbol: 'BTCUSDT',
    side,
    round,
    responds_to: respondsTo,
    rebuttal,
    tradeable: true,
    confidence,
    thesis: isRed ? '15m 回踩 EMA20 未破，1h 趋势向上，回踩做多。' : '双阻力 + 资金费拥挤，冲高失败做空。',
    evidence: [
      { ref: 'symbols.BTCUSDT.price.last', claim: '现价 68234.5，位于 EMA20 上方', weight: 'high' },
      { ref: 'symbols.BTCUSDT.levels.resistances', claim: '上方阻力 68820', weight: 'medium' },
      { ref: 'symbols.BTCUSDT.timeframes.15m.rsi14', claim: '15m RSI 未过热', weight: 'high' },
    ],
    trigger: { type: isRed ? 'retest' : 'rejection', description: '回踩/冲高失败后入场', price: isRed ? 68100 : 68820, valid_until: '2026-11-09T03:45:20Z' },
    plan_draft: plan,
    invalidation: { price: isRed ? 67410.2 : 69050.0, condition: '结构被破坏' },
    opponent_steelman: isRed ? '空头有资金费与双阻力。' : '多头有趋势与 EMA20。',
    no_case_reason: null,
    warnings: [],
  }
}

function decisionFixture(cycleId) {
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
      red: { strength: 'strong', decisive_evidence: '回踩 EMA20 未破', fatal_flaw: null },
      blue: { strength: 'moderate', decisive_evidence: '双阻力压制反弹', fatal_flaw: 'R:R 1.02 < 1.2' },
      decisive_factor: '红队 gate PASS，蓝队 gate FAIL_RR',
      opposing_risk: '资金费拥挤，放量突破 68820 可能逼空。',
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

function riskFixture(cycleId) {
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

function executionFixture(cycleId) {
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
      reasoning_ref: 'decision:fixture',
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
    rationale: '按已批准计划市价开仓，同时挂 SL/TP。',
    warnings: [],
  }
}

function oneShotStream(text) {
  return (async function* stream() {
    yield { type: 'text-delta', text }
    yield { type: 'finish', reason: { kind: 'stop' } }
  })()
}

function maxTokensStream(text) {
  return (async function* stream() {
    yield { type: 'text-delta', text }
    yield { type: 'usage', usage: { inputTokens: 1819, outputTokens: 4000, totalTokens: 5819 } }
    yield { type: 'finish', reason: { kind: 'max-tokens' } }
  })()
}

function parsePayload(options) {
  const text = options.messages[0].content[0].text
  return JSON.parse(text.split('\n\n')[0])
}

function roleKeyFrom(payload) {
  const role = payload.task?.role
  const round = payload.task?.round
  if (role === 'analysis') return 'analysis'
  if (role === 'red_team') return round === 2 ? 'red_response' : 'red_opening'
  if (role === 'blue_team') return round === 2 ? 'blue_response' : 'blue_opening'
  if (role === 'decision') return 'decision'
  if (role === 'risk') return 'risk'
  if (role === 'execution') return payload.output_schema === 'execution_manage/1' ? 'execution_manage' : 'execution_entry'
  throw new Error(`unknown role ${role}`)
}

function makeFakeLlm({ mode = 'trade', maxTokensRoles = [], executionBlocked = null } = {}) {
  const seen = []
  const maxTokensSet = new Set(maxTokensRoles)
  const attemptsByRole = {}
  const llm = {
    stream(options) {
      const payload = parsePayload(options)
      const roleKey = roleKeyFrom(payload)
      const cycleId = payload.evidence?.cycle_id ?? payload.analysis?.cycle_id ?? payload.cycle_id
      attemptsByRole[roleKey] = (attemptsByRole[roleKey] ?? 0) + 1
      seen.push({
        roleKey,
        payload,
        maxTokens: options.maxTokens,
        attempt: attemptsByRole[roleKey],
        provider: options.provider ?? null,
        model: options.model ?? null,
        effort: options.reasoningEffort ?? null,
      })
      assert.equal(Object.prototype.hasOwnProperty.call(options, 'tools'), false)
      if (maxTokensSet.has(roleKey) && attemptsByRole[roleKey] === 1) {
        return maxTokensStream('{"schema_version":"partial"')
      }
      if (roleKey === 'analysis') {
        return oneShotStream(JSON.stringify(mode === 'no_trade' ? standAsideFixture(cycleId) : analysisFixture(cycleId)))
      }
      if (roleKey === 'red_opening') return oneShotStream(JSON.stringify(debateFixture(cycleId, { side: 'LONG', round: 1, confidence: 0.68, plan: RED_PLAN })))
      if (roleKey === 'blue_opening') return oneShotStream(JSON.stringify(debateFixture(cycleId, { side: 'SHORT', round: 1, confidence: 0.55, plan: BLUE_PLAN })))
      if (roleKey === 'red_response') {
        return oneShotStream(JSON.stringify(debateFixture(cycleId, {
          side: 'LONG', round: 2, confidence: 0.70, plan: RED_PLAN, respondsTo: payload.task.responds_to,
          rebuttal: [
            { target_claim: '资金费拥挤，多头拥挤', counter: '拥挤不等于反转，价格未放量滞涨。' },
            { target_claim: '阻力 68820 冲高失败可短', counter: '阻力距离近，放量突破则空头失效。' },
          ],
        })))
      }
      if (roleKey === 'blue_response') {
        return oneShotStream(JSON.stringify(debateFixture(cycleId, {
          side: 'SHORT', round: 2, confidence: 0.58, plan: BLUE_PLAN, respondsTo: payload.task.responds_to,
          rebuttal: [
            { target_claim: '15m 回踩 EMA20 未破', counter: 'EMA20 距离近，跌破即失效。' },
            { target_claim: '1h 结构仍为 HL', counter: '资金费拥挤，结构可能快速破坏。' },
          ],
        })))
      }
      if (roleKey === 'decision') return oneShotStream(JSON.stringify(decisionFixture(cycleId)))
      if (roleKey === 'risk') return oneShotStream(JSON.stringify(riskFixture(cycleId)))
      if (roleKey === 'execution_entry') {
        const execution = executionFixture(cycleId)
        if (executionBlocked) {
          execution.executable = false
          execution.blocked_reason = executionBlocked
          execution.order_plan = null
          execution.preflight = null
          execution.rationale = 'No account equity or available balance is supplied; refusing to invent a size.'
        }
        return oneShotStream(JSON.stringify(execution))
      }
      throw new Error(`unexpected role ${roleKey}`)
    },
  }
  return { llm, seen }
}

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  }
}

function serviceEvidenceFixture(cycleId, account = SERVICE_ACCOUNT) {
  const snapshotId = `ev-${String(cycleId).slice(-6)}`
  const producedAt = new Date(NOW).toISOString()
  return {
    cycle_id: cycleId,
    account,
    evidence: {
      cycle_id: cycleId,
      snapshot_id: snapshotId,
      sha256: `sha256:service-${String(cycleId).slice(-6)}`,
      path: `evidence/2026-11-09/${cycleId}/evidence.json`,
      bytes: 4321,
      schema_version: 'md-evidence/1',
      verdict: 'GOOD',
      evidence: {
        schema_version: 'md-evidence/1',
        cycle_id: cycleId,
        snapshot_id: snapshotId,
        produced_at: producedAt,
        as_of: producedAt,
        data_health: { verdict: 'GOOD', score: 0.95, issues: [] },
        market: { regime: 'trend_up' },
        primary_symbol: 'BTCUSDT',
        universe: { deep: [{ symbol: 'BTCUSDT' }] },
        symbols: {
          BTCUSDT: {
            price: { last: 68234.5, mark: 68240, index: 68239, bid: 68234, ask: 68235, spread_bps: 1.5, age_ms: 0 },
            age_s: 0,
          },
        },
      },
    },
  }
}

function makeFakeService({
  p1Mode = 'approved',
  p1Status = null,
  p1Code = null,
  credentials = false,
  controlState = 'normal',
  readyPaused = false,
  p2Status = null,
  p2Code = null,
  cyclesStatus = 200,
  duplicateSlot = false,
  statusPositions = [],
  cycleIdOverride = null,
  positions = undefined,
  orders = undefined,
  account = SERVICE_ACCOUNT,
  closeStatus = null,
  closeCode = null,
  leaseReject = false,
} = {}) {
  const calls = []
  let activeLease = null
  let leaseSeq = 0
  const fetchImpl = async (url, options = {}) => {
    const u = new URL(url)
    const headers = options.headers ?? {}
    const body = options.body ? JSON.parse(options.body) : null
    calls.push({
      path: u.pathname,
      query: u.search,
      method: options.method ?? 'GET',
      headers,
      body,
    })
    if (u.pathname === '/api/ready') {
      // readyPaused：服务健康（service_ready=true）但不允许开新仓（ready=false）。
      // 这正是“用户暂停自动交易”的真实形状。
      if (readyPaused) {
        return jsonResponse(200, { ready: false, service_ready: true, reasons: ['CONTROL_NOT_NORMAL'], control_state: 'pause', auth: { mode: 'bearer' } })
      }
      return jsonResponse(200, { ready: true, service_ready: true, reasons: [], auth: { mode: 'bearer' } })
    }
    if (u.pathname === '/health') return jsonResponse(200, { auth: { mode: 'bearer' } })
    if (u.pathname === '/api/execution/lease/acquire') {
      if (leaseReject) {
        return jsonResponse(409, { error: { code: 'E_LEASE_HELD', message: 'another Host holds the lease' } })
      }
      if (activeLease && !activeLease.revoked) {
        return jsonResponse(409, { error: { code: 'E_LEASE_HELD', message: 'another execution lease is active' } })
      }
      leaseSeq += 1
      activeLease = {
        lease_id: `lease-${leaseSeq}`,
        run_id: body?.run_id ?? null,
        generation: leaseSeq,
        expires_at_ms: NOW + 900_000,
        revoked: false,
        ok: true,
      }
      return jsonResponse(200, activeLease)
    }
    if (u.pathname === '/api/execution/lease/renew') {
      if (!activeLease || activeLease.revoked || activeLease.lease_id !== body?.lease_id) {
        return jsonResponse(409, { error: { code: 'E_LEASE_STALE', message: 'lease stale' } })
      }
      return jsonResponse(200, activeLease)
    }
    if (u.pathname === '/api/execution/lease/release') {
      if (activeLease && activeLease.lease_id === body?.lease_id) activeLease.revoked = true
      return jsonResponse(200, { ok: true, released: true })
    }
    if (u.pathname === '/api/execution/lease/revoke_run') {
      if (activeLease && activeLease.run_id === body?.run_id) activeLease.revoked = true
      return jsonResponse(200, { ok: true, revoked: true })
    }
    if (u.pathname === '/api/cycles') {
      if (duplicateSlot) {
        return jsonResponse(409, { error: { code: 'E_CYCLE_DUPLICATE_SLOT', message: 'slot_id already used' } })
      }
      if (cyclesStatus !== 200) {
        return jsonResponse(cyclesStatus, { error: { code: 'E_NOT_FOUND', message: 'POST /api/cycles not implemented' } })
      }
      const cycleId = cycleIdOverride ?? body?.cycle_id ?? null
      return jsonResponse(200, serviceEvidenceFixture(cycleId, account))
    }
    if (/^\/api\/cycles\/[^/]+\/ai-runs$/.test(u.pathname) && (options.method ?? 'GET') === 'POST') {
      return jsonResponse(200, { ai_run: { ai_run_id: 'run-service-test', cycle_id: u.pathname.split('/')[3] } })
    }
    if (/^\/api\/ai-runs\/[^/]+\/calls\/[^/]+$/.test(u.pathname)) {
      return jsonResponse(200, { call: { call_id: u.pathname.split('/').pop() } })
    }
    if (/^\/api\/ai-runs\/[^/]+\/events$/.test(u.pathname) && options.method === 'POST') {
      return jsonResponse(200, { seq: calls.filter((call) => call.path.endsWith('/events')).length, ai_run_id: u.pathname.split('/')[3] })
    }
    if (/^\/api\/cycles\/[^/]+\/artifacts\/[^/]+$/.test(u.pathname)) {
      return jsonResponse(200, { output: { output_id: 'out-test' } })
    }
    if (u.pathname.startsWith('/api/cycles/') && u.pathname.endsWith('/finalize')) {
      return jsonResponse(200, { cycle: { cycle_id: u.pathname.split('/')[3], state: body?.state, outcome: body?.outcome }, idempotent: false })
    }
    if (u.pathname === '/api/risk/envelope') {
      return jsonResponse(200, {
        demo_only: true,
        ledger_id: 'ledger-test',
        limits: {
          per_trade: {
            max_risk_pct: 0.01,
            strong_max_risk_pct: 0.02,
            strong_min_confidence: 0.65,
            strong_min_rr: 2.0,
            max_leverage: 10,
            strong_max_leverage: 15,
            max_notional_multiple: 2,
            min_risk_reward: 1.2,
            sl_min_distance_pct: 0.002,
            sl_max_distance_pct: 0.05,
          },
        },
        control: { state: controlState },
        risk_state: {},
        account: { credentials_configured: credentials },
        new_entry_allowed: true,
        blocking_reasons: [],
      })
    }
    if (u.pathname === '/api/status') {
      return jsonResponse(200, {
        credentials_configured: credentials,
        account: account?.account ?? null,
        equity: account?.equity ?? null,
        positions: statusPositions,
      })
    }
    if (positions !== undefined && u.pathname === '/api/positions') {
      return jsonResponse(200, { positions })
    }
    if (orders !== undefined && u.pathname === '/api/orders') {
      return jsonResponse(200, { orders })
    }
    if (u.pathname === '/api/control') {
      if (body.state === 'normal') return jsonResponse(423, { error: { code: 'E_CONTROL_LOCKED', message: 'risk latch active' } })
      return jsonResponse(200, { control: { state: body.state }, operation_status: 'succeeded', request_id: body.request_id })
    }
    if (u.pathname === '/api/operations') return jsonResponse(200, { operations: [{ request_id: 'req-history-test', action: 'control', status: 'succeeded' }] })
    if (u.pathname === '/api/market/health') return jsonResponse(200, { verdict: 'GOOD', score: 100, issues: [] })
    if (u.pathname === '/api/market/prices') {
      return jsonResponse(200, {
        as_of_ms: NOW,
        prices: {
          BTCUSDT: { last: 68234.5, mark: 68240, index: 68239, bid: 68234, ask: 68235, spread_bps: 1.5, age_ms: 0, complete: true, errors: [] },
        },
        trace_id: 'trace-prices',
      })
    }
    if (u.pathname === '/api/risk/evaluate') {
      if (p1Status !== null) {
        return jsonResponse(p1Status, {
          error: { code: p1Code ?? `E_P1_${p1Status}`, message: `P1 HTTP ${p1Status}`, retryable: false, details: { stage: 'P1' } },
          trace_id: 'trace-p1',
        })
      }
      if (p1Mode === 'reject') {
        return jsonResponse(422, {
          error: {
            code: 'E_RISK_R05',
            message: 'risk too large',
            retryable: false,
            details: {
              rule: 'R05',
              violations: [{ rule: 'R05', action: 'reject', detail: 'risk too large' }],
              verdict: {
                verdict: 'rejected',
                approved_qty: '0',
                leverage: 3,
                violations: [{ rule: 'R05', action: 'reject', detail: 'risk too large' }],
                blocking_rule: 'R05',
                blocked_reason: 'risk too large',
              },
            },
          },
          trace_id: 'trace-p1',
        })
      }
      const plan = body?.plan ?? null
      return jsonResponse(200, {
        cycle_id: body?.cycle_id ?? null,
        plan,
        verdict: { verdict: 'approved', approved_qty: '0.067', leverage: 3, violations: [] },
        approval: {
          token: 'approval-token-secret',
          jti: 'jti-test',
          plan_hash: 'hash-test',
          approved_qty: '0.067',
          expires_at_ms: NOW + 120_000,
        },
      })
    }
    if (u.pathname === '/api/execution/order') {
      if (p2Status !== null) {
        return jsonResponse(p2Status, {
          error: { code: p2Code ?? `E_P2_${p2Status}`, message: `P2 HTTP ${p2Status}`, retryable: false, details: { stage: 'P2' } },
          trace_id: 'trace-p2',
        })
      }
      return jsonResponse(201, { order_id: 123, client_order_id: 'cid-test', cycle_id: body?.cycle_id ?? null })
    }
    if (u.pathname === '/api/execution/close') {
      if (closeStatus !== null) {
        return jsonResponse(closeStatus, {
          error: { code: closeCode ?? `E_CLOSE_${closeStatus}`, message: 'close failed', retryable: false, details: { stage: 'CLOSE' } },
          trace_id: 'trace-close',
        })
      }
      return jsonResponse(200, {
        symbol: body?.symbol ?? null,
        qty: body?.qty ?? null,
        reason: body?.reason ?? null,
        status: 'CLOSED',
        closed: true,
        venue: 'demo',
      })
    }
    if (u.pathname === '/api/protection/repair') {
      return jsonResponse(200, {
        ok: true,
        repaired: [{ symbol: body?.symbols?.[0] ?? 'ALL', status: 'OK' }],
        venue: 'demo',
      })
    }
    if (u.pathname === '/api/execution/reconcile') {
      return jsonResponse(200, {
        ok: true,
        adopted: [],
        closed: [],
        protection_missing: [],
        outbox: { pending: 0, uncertain: 0 },
        venue: 'demo',
        demo_only: true,
        ledger_id: 'ledger-test',
      })
    }
    if (/^\/api\/cycles\/[^/]+\/evidence$/.test(u.pathname)) {
      const cycleId = u.pathname.split('/')[3]
      return jsonResponse(200, {
        cycle_id: cycleId,
        snapshot_id: 'ev-test',
        agent: u.searchParams.get('agent'),
        path: `evidence/${cycleId}.json`,
        sha256: 'sha256:abc',
        bytes: 4321,
        schema_version: 'md-evidence/1',
        verdict: 'GOOD',
        evidence: { schema_version: 'md-evidence/1', cycle_id: cycleId, snapshot_id: 'ev-test' },
      })
    }
    return jsonResponse(404, { error: { code: 'E_NOT_FOUND', message: u.pathname } })
  }
  return { fetchImpl, calls }
}

function makeIntegration({ fakeLlm = makeFakeLlm(), service = makeFakeService(), token = SERVICE_TOKEN, config = {}, reviewTimeoutMs = undefined, runTimeoutMs = undefined, llmCatalog = undefined, serviceLauncher = undefined, spawnImpl = undefined, serviceStartTimeoutMs = undefined } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-host-'))
  const tokenFile = path.join(dir, '.api-token')
  if (token !== null) fs.writeFileSync(tokenFile, `${token}\n`, 'utf8')
  const integration = createIntegration({
    serviceBaseUrl: 'http://127.0.0.1:8791',
    dataDir: dir,
    tokenFile,
    storePath: path.join(dir, 'rounds.json'),
    fetchImpl: service.fetchImpl,
    llm: fakeLlm.llm,
    route: ROUTE,
    now: () => NOW,
    config: { debateMode: 'symmetric_two_round', ...config },
    ...(llmCatalog === undefined ? {} : { llmCatalog }),
    ...(serviceLauncher === undefined ? {} : { serviceLauncher }),
    ...(spawnImpl === undefined ? {} : { spawnImpl }),
    ...(serviceStartTimeoutMs === undefined ? {} : { serviceStartTimeoutMs }),
    ...(reviewTimeoutMs === undefined ? {} : { reviewTimeoutMs }),
    ...(runTimeoutMs === undefined ? {} : { runTimeoutMs }),
  })
  return { integration, service, fakeLlm, dir, tokenFile }
}


test(
  "live setup writes the credential to a 0600 file and never returns the secret",
  async () => {
    const env = makeIntegration()
    const calls = []
    const makeReq = (method, body) => ({
      method,
      // 不发送 Origin：同源校验依赖 ctx.webServer，这条用例测的是“写文件 + 不回显”，
      // 同源策略由别的用例覆盖（否则这里会退化成测 mock）。
      headers: { host: "127.0.0.1:54830", "content-type": "application/json" },
      on: (event, handler) => { if (event === "data" && body) handler(Buffer.from(JSON.stringify(body))); if (event === "end") handler() },
      [Symbol.asyncIterator]: async function* () { if (body) yield Buffer.from(JSON.stringify(body)) },
    })
    const makeRes = () => {
      const rec = { status: 0, body: null }
      return {
        statusCode: 0,
        setHeader: () => {},
        writeHead(code) { rec.status = code; this.statusCode = code },
        end(text) {
          rec.status = rec.status || this.statusCode || 200
          rec.body = text ? JSON.parse(text) : null
          calls.push(rec)
        },
      }
    }
    const body = { apiKey: "LIVEKEY123456", apiSecret: "LIVESECRET9876" }
    await env.integration.handleLiveSetup(makeReq("POST", body), makeRes())
    assert.equal(calls.length, 1)
    const payload = calls[0].body
    assert.equal(payload.ok, true, JSON.stringify(payload))
    assert.equal(payload.restartRequired, true)
    // 明文绝不回传
    assert.equal(JSON.stringify(payload).includes("LIVESECRET9876"), false)
    assert.equal(JSON.stringify(payload).includes("LIVEKEY123456"), false)
    const file = payload.file.path
    assert.equal(fs.existsSync(file), true)
    const text = fs.readFileSync(file, "utf8")
    assert.match(text, /BINANCE_LIVE_API_KEY=LIVEKEY123456/)
    if (process.platform !== "win32") {
      const mode = fs.statSync(file).mode & 0o777
      assert.equal(mode, 0o600)
    }
    // 空值必须被拒绝
    const rejected = []
    const res2 = {
      statusCode: 0,
      setHeader: () => {},
      writeHead(code) { this.statusCode = code },
      end(text) { rejected.push(text ? JSON.parse(text) : null) },
    }
    await env.integration.handleLiveSetup(makeReq("POST", { apiKey: "", apiSecret: "" }), res2)
    assert.equal(rejected[0].ok, false)
  },
)

function demoSetupReq(method, body) {
  return {
    method,
    headers: { host: "127.0.0.1:54830", "content-type": "application/json" },
    on: (event, handler) => {
      if (event === "data" && body) handler(Buffer.from(JSON.stringify(body)))
      if (event === "end") handler()
    },
    [Symbol.asyncIterator]: async function* () { if (body) yield Buffer.from(JSON.stringify(body)) },
  }
}

function demoSetupRes() {
  const rec = { status: 0, body: null }
  return {
    rec,
    statusCode: 0,
    setHeader: () => {},
    writeHead(code) { rec.status = code; this.statusCode = code },
    end(text) {
      rec.status = rec.status || this.statusCode || 200
      rec.body = text ? JSON.parse(text) : null
    },
  }
}

test(
  "demo setup writes the credential under dataDir/credentials and never returns the secret",
  async () => {
    const env = makeIntegration()
    const expected = path.join(env.dir, "credentials", "demo.env")

    // GET before any write: honest "absent", and it names the file the service will read.
    const before = demoSetupRes()
    await env.integration.handleDemoSetup(demoSetupReq("GET"), before)
    assert.equal(before.rec.body.ok, true)
    assert.equal(before.rec.body.credential.configured, false)
    assert.equal(before.rec.body.file.path, expected)
    // effective_dotenv is what the service will really read. On a dev checkout that
    // can still be the reference project .env (documented fallback); it is always
    // reported rather than guessed, and after our own write it must be demo.env.
    assert.ok(typeof before.rec.body.effective_dotenv === "string")

    const res = demoSetupRes()
    await env.integration.handleDemoSetup(
      demoSetupReq("POST", { apiKey: "DEMOKEY123456", apiSecret: "DEMOSECRET9876" }),
      res,
    )
    const payload = res.rec.body
    assert.equal(payload.ok, true, JSON.stringify(payload))
    assert.equal(payload.restartRequired, true)
    assert.equal(payload.file.path, expected)
    // Once demo.env exists it is the first candidate, so the service reads it.
    assert.equal(payload.effective_dotenv, expected)
    // 明文绝不回传
    assert.equal(JSON.stringify(payload).includes("DEMOSECRET9876"), false)
    assert.equal(JSON.stringify(payload).includes("DEMOKEY123456"), false)
    assert.equal(fs.existsSync(expected), true)
    const text = fs.readFileSync(expected, "utf8")
    assert.match(text, /WORKBENCH_DEMO_API_KEY=DEMOKEY123456/)
    assert.match(text, /WORKBENCH_DEMO_API_SECRET=DEMOSECRET9876/)
    if (process.platform !== "win32") {
      assert.equal(fs.statSync(expected).mode & 0o777, 0o600)
    }

    // GET after the write: the file is configured and the fingerprint matches the key.
    const after = demoSetupRes()
    await env.integration.handleDemoSetup(demoSetupReq("GET"), after)
    assert.equal(after.rec.body.credential.configured, true)
    assert.equal(typeof after.rec.body.credential.fingerprint, "string")
    assert.equal(after.rec.body.credential.fingerprint.length, 8)

    // Empty values are rejected, and a rejected write must not create a file.
    const rejected = demoSetupRes()
    await env.integration.handleDemoSetup(demoSetupReq("POST", { apiKey: "", apiSecret: "" }), rejected)
    assert.equal(rejected.rec.status, 422)
    assert.equal(rejected.rec.body.ok, false)
  },
)

test("dataDir default never lands in the package directory (upgrade would drop it)", async () => {
  const explicit = fs.mkdtempSync(path.join(os.tmpdir(), "twb-dd-"))
  assert.equal(resolveDefaultDataDir({ WORKBENCH_DATA_DIR: explicit }), path.resolve(explicit))

  const fallback = resolveDefaultDataDir({})
  assert.equal(path.isAbsolute(fallback), true)
  assert.equal(fallback.includes("node_modules"), false)
  // A packaged install lives in node_modules; the fallback must be under the user profile.
  assert.equal(fallback.startsWith(os.homedir()), true)
})

test("launcher paths prefer the dataDir credential file over the reference checkout", async () => {
  const env = makeIntegration()
  const demoEnv = path.join(env.dir, "credentials", "demo.env")
  fs.mkdirSync(path.dirname(demoEnv), { recursive: true })
  fs.writeFileSync(demoEnv, "WORKBENCH_DEMO_API_KEY=K\nWORKBENCH_DEMO_API_SECRET=S\n", "utf8")

  const paths = env.integration.serviceLauncherPaths()
  // A credential written by the settings entry must be what the service reads,
  // even on a machine that still has the reference project's .env around.
  assert.equal(paths.dotenv, demoEnv)
  // A python that was not found must stay null instead of throwing or faking a path.
  assert.ok(paths.python === null || typeof paths.python === "string")
  // Capability must be a decision, not an exception, on a machine without the reference venv.
  const capability = env.integration.serviceStartCapability()
  assert.equal(capability.command, "service/start")
  assert.equal(typeof capability.supported, "boolean")
  const details = env.integration.serviceStartDetails()
  assert.equal(details.dotenv_path, demoEnv)
})


test("pausing auto trading does not blank the portfolio (readiness != service health)", async () => {
  // 真实故障现场（2026-11-14）：用户按了停止自动交易，control=pause，
  // /api/ready 返回 ready=false 而 service_ready=true。旧实现把 ready 当成
  // “服务不可用”，于是持仓/订单/成交/权益全部清空，界面看起来像断线。
  const service = makeFakeService({ readyPaused: true, positions: [{ symbol: "BTCUSDT", position_side: "SHORT", qty: "0.028", entry_price: "67412.7", mark_price: "66701.0", upnl: "19.9", leverage: 3, margin: "648", margin_mode: "ISOLATED", liq_price: "89505" }] })
  const env = makeIntegration({ service })
  const portfolio = await env.integration.loadPortfolio({ force: true })
  assert.notEqual(portfolio.source, "none", "paused must not be reported as service-down")
  assert.equal((portfolio.positions ?? []).length, 1, JSON.stringify(portfolio.gaps))
  assert.equal(portfolio.gaps?.includes("E_SERVICE_NOT_READY: workbench-service readiness is false"), false)
})

test('execution payload carries service account and equity', async () => {
  const fake = makeFakeLlm()
  const env = makeIntegration({ fakeLlm: fake })
  const result = await env.integration.run()
  assert.equal(result.ok, true, JSON.stringify(result.round?.error))
  const execution = fake.seen.find((entry) => entry.roleKey === 'execution_entry')
  assert.ok(execution)
  assert.equal(execution.payload.account.total_margin_balance, '10000')
  assert.equal(execution.payload.account.available_balance, '9000')
  assert.equal(execution.payload.equity.session, '10000')
  assert.equal(execution.payload.equity.exchange, '10100')
  assert.equal(execution.payload.available_balance, '9000')
  assert.equal(execution.payload.equity_session, '10000')
  assert.equal(execution.payload.equity_exchange, '10100')
  assert.equal(JSON.stringify(execution.payload).includes('apiKey'), false)
})

test('missing service account blocks execution with ACCOUNT_UNAVAILABLE', async () => {
  const fake = makeFakeLlm({ executionBlocked: 'ACCOUNT_UNAVAILABLE' })
  const service = makeFakeService({ account: null })
  const env = makeIntegration({ fakeLlm: fake, service })
  const result = await env.integration.run()
  assert.equal(result.ok, true, JSON.stringify(result.round?.error))
  assert.equal(result.round.outcome, 'no_trade')
  assert.equal(result.round.reasonCode, 'EXECUTION_BLOCKED:ACCOUNT_UNAVAILABLE')
  assert.equal(service.calls.some((call) => call.path === '/api/execution/order'), false)
})

test('persistAiAudit sends max_tokens, status, finish_reason and usage', async () => {
  const env = makeIntegration()
  const captured = []
  const client = {
    createAiRun: async () => ({ ai_run: { ai_run_id: 'ai-run-1' } }),
    putAiCall: async (aiRunId, callId, payload) => { captured.push({ aiRunId, callId, payload }) },
    putArtifact: async () => ({}),
  }
  const result = {
    outcome: 'no_trade',
    mode: 'FULL',
    debateMode: 'symmetric_two_round',
    outputs: {},
    calls: [{
      call_id: 'c1', role: 'analysis', round: 1, attempt: 1,
      provider: 'fake', model: 'fake-1', effort: 'high',
      prompt_version: 'analysis.v1', output_schema_version: 'analysis/1',
      max_tokens: 4000, status: 'max_tokens', finish_kind: 'max_tokens', finish_reason: 'max_tokens',
      latency_ms: 1200, input_tokens: 1819, output_tokens: 4000, total_tokens: 5819,
      error_code: 'LLM_FINISH_ERROR', error_message: 'LLM stream finished with max-tokens',
      request_sha256: 'sha256:req', response_sha256: 'sha256:res', validation_errors: [],
    }],
  }
  await env.integration.persistAiAudit(client, 'cyc-20261109T010000Z-abcdef', result, { startedAt: '2026-11-09T01:00:00Z' })
  assert.equal(captured.length, 1)
  assert.equal(captured[0].payload.max_tokens, 4000)
  assert.equal(captured[0].payload.status, 'max_tokens')
  assert.equal(captured[0].payload.finish_reason, 'max_tokens')
  assert.equal(captured[0].payload.input_tokens, 1819)
  assert.equal(captured[0].payload.output_tokens, 4000)
  assert.equal(captured[0].payload.total_tokens, 5819)
  assert.equal(captured[0].payload.error_code, 'LLM_FINISH_ERROR')
})

test('host sends constants role caps and 1.5x max-tokens retry caps', async () => {
  const fake = makeFakeLlm({
    maxTokensRoles: ['analysis', 'red_opening', 'blue_opening', 'red_response', 'blue_response', 'decision'],
  })
  const env = makeIntegration({ fakeLlm: fake })
  const result = await env.integration.run()
  assert.equal(result.ok, true, JSON.stringify(result.round?.error))
  const byRole = new Map()
  for (const entry of fake.seen) {
    if (!byRole.has(entry.roleKey)) byRole.set(entry.roleKey, [])
    byRole.get(entry.roleKey).push(entry.maxTokens)
  }
  const base = {
    analysis: 8000,
    red_opening: 4000,
    blue_opening: 4000,
    red_response: 4600,
    blue_response: 4600,
    decision: 4000,
    risk: 3000,
    execution_entry: 3200,
  }
  for (const [role, expectedBase] of Object.entries(base)) {
    const caps = byRole.get(role)
    assert.ok(caps, `${role} should be called`)
    assert.equal(caps[0], expectedBase, `${role} base cap`)
    if (['analysis', 'red_opening', 'blue_opening', 'red_response', 'blue_response', 'decision'].includes(role)) {
      assert.equal(caps[1], Math.min(Math.ceil(expectedBase * 1.5), 10000), `${role} escalated cap`)
      assert.notEqual(caps[1], caps[0], `${role} must not retry with the same cap`)
    }
  }
  assert.equal(env.service.calls.some((call) => call.path === '/api/execution/order'), false)
})

test('explicit config.roleMaxTokens overrides constants caps', async () => {
  const fake = makeFakeLlm({ mode: 'no_trade', maxTokensRoles: ['analysis'] })
  const env = makeIntegration({ fakeLlm: fake, config: { roleMaxTokens: { analysis: 1234 } } })
  const result = await env.integration.run()
  assert.equal(result.ok, true, JSON.stringify(result.round?.error))
  const analysisCaps = fake.seen.filter((entry) => entry.roleKey === 'analysis').map((entry) => entry.maxTokens)
  assert.deepEqual(analysisCaps, [1234, 1851])
})

test('no_trade round reaches state and rounds with Bearer-only service calls', async () => {
  const env = makeIntegration({ fakeLlm: makeFakeLlm({ mode: 'no_trade' }) })
  const result = await env.integration.run()
  assert.equal(result.ok, true, JSON.stringify(result.round?.error))
  assert.equal(result.round.outcome, 'no_trade')
  assert.equal(result.round.reasonCode, 'NO_CANDIDATE')
  assert.equal(result.round.scorecard, null)
  assert.equal(result.round.risk, null)
  assert.equal(result.round.aiCalls.length, 1)
  assert.ok(env.service.calls.length > 0)
  assert.ok(env.service.calls.every((call) => call.headers.authorization === `Bearer ${SERVICE_TOKEN}`))
  assert.ok(env.service.calls.every((call) => !call.headers.authorization.includes('approval-token')))
  const state = await env.integration.state()
  assert.equal(state.service.token_present, true)
  assert.equal(state.rounds.length, 1)
  const list = env.integration.listRounds()
  assert.equal(list.rounds.length, 1)
  assert.equal(list.rounds[0].roundId, result.round.roundId)
  assert.equal(env.integration.getRound(result.round.roundId)?.round.roundId, result.round.roundId)
  assert.equal(env.integration.getRound('missing'), null)
})

test('P1 rejection preserves HTTP 422 and P1 cycle_id without P2', async () => {
  const env = makeIntegration({ service: makeFakeService({ p1Mode: 'reject' }) })
  const result = await env.integration.run()
  assert.equal(result.ok, false)
  assert.equal(result.round.status, 'failed')
  assert.equal(result.round.outcome, 'failed')
  assert.equal(result.round.reasonCode, 'E_RISK_R05', JSON.stringify(result.round.aiCalls))
  assert.equal(result.round.httpStatus, 422)
  assert.equal(result.round.serviceStatus, 'rejected')
  assert.equal(result.round.errorStatus.stage, 'P1')
  assert.equal(result.round.errorStatus.httpStatus, 422)
  assert.equal(result.round.execution.p1.verdict, 'rejected')
  assert.deepEqual(result.round.risk.violations, [{ rule: 'R05', action: 'reject', detail: 'risk too large' }])
  assert.equal(result.round.risk.blocking_rule, 'R05')
  assert.equal(env.service.calls.some((call) => call.path === '/api/execution/order'), false)
  assert.ok(env.service.calls.every((call) => call.headers.authorization === `Bearer ${SERVICE_TOKEN}`))
  const p1Call = env.service.calls.find((call) => call.path === '/api/risk/evaluate')
  assert.ok(p1Call)
  assert.equal(p1Call.body.cycle_id, result.round.cycleId)
  assert.equal(p1Call.body.plan.symbol, 'BTCUSDT')
  assert.equal(p1Call.body.plan.qty, '0.067')
  assert.equal(result.round.gaps.some((gap) => gap.includes('No server evidence snapshot')), false)
  assert.equal(result.round.freshness.source, 'service:/api/cycles')
  assert.match(result.round.freshness.snapshot_id, /^ev-/)
})

test('P2 credentials missing closes as no_trade without submitting', async () => {
  const env = makeIntegration()
  const result = await env.integration.run()
  assert.equal(result.ok, true, JSON.stringify(result.round?.error))
  assert.equal(result.round.outcome, 'no_trade')
  assert.equal(result.round.reasonCode, 'E_DEMO_CREDENTIALS_MISSING', JSON.stringify(result.round.aiCalls))
  assert.equal(result.round.serviceStatus, 'degraded')
  assert.equal(result.round.errorStatus, null)
  assert.equal(result.round.execution.p1.verdict, 'approved')
  assert.equal(result.round.execution.p2.ok, false)
  assert.equal(env.service.calls.some((call) => call.path === '/api/execution/order'), false)
  assert.ok(env.service.calls.some((call) => call.path === '/api/risk/evaluate'))
  assert.ok(env.service.calls.every((call) => call.headers.authorization === `Bearer ${SERVICE_TOKEN}`))
  assert.equal(JSON.stringify(result.round).includes('approval-token-secret'), false)
  assert.equal(JSON.stringify(await env.integration.state()).includes('approval-token-secret'), false)
})

test('P2 submits once with the P1 token when credentials are configured', async () => {
  const env = makeIntegration({ service: makeFakeService({ credentials: true }) })
  const result = await env.integration.run()
  assert.equal(result.ok, true, JSON.stringify(result.round?.error))
  assert.equal(result.round.outcome, 'trade')
  assert.equal(result.round.execution.p1.verdict, 'approved')
  assert.equal(result.round.execution.p2.ok, true)
  assert.equal(result.round.scorecard.grade, 'A')
  assert.equal(result.round.scorecard.gate, 'PASS')
  assert.equal(result.round.scorecard.debate_quality, 'thin')
  assert.deepEqual(result.round.risk.violations, [])
  assert.equal(result.round.risk.blocking_rule, null)
  const p2Calls = env.service.calls.filter((call) => call.path === '/api/execution/order')
  assert.equal(p2Calls.length, 1)
  assert.equal(p2Calls[0].body.approval_token, 'approval-token-secret')
  assert.equal(p2Calls[0].body.cycle_id, result.round.cycleId)
  assert.equal(env.service.calls.find((call) => call.path === '/api/risk/evaluate').body.cycle_id, result.round.cycleId)
  assert.equal(JSON.stringify(result.round).includes('approval-token-secret'), false)
})

test('positions keep protection_ok three-valued (UNKNOWN/null are not MISSING)', async () => {
  const positions = [
    { symbol: 'BTCUSDT', position_side: 'LONG', protection: { status: 'OK' } },
    { symbol: 'DOGEUSDT', position_side: 'LONG', protection: { status: 'PROTECTED' } },
    { symbol: 'ETHUSDT', position_side: 'LONG', protection: { status: 'MISSING' } },
    { symbol: 'SOLUSDT', position_side: 'SHORT', protection: { status: 'PARTIAL' } },
    { symbol: 'XRPUSDT', position_side: 'LONG', protection: { status: 'DUPLICATE' } },
    { symbol: 'BNBUSDT', position_side: 'LONG', protection: { status: 'UNKNOWN' } },
    { symbol: 'ADAUSDT', position_side: 'LONG' },
  ]
  const env = makeIntegration({ service: makeFakeService({ positions }) })
  const state = await env.integration.state({ force: true })
  const bySymbol = new Map(state.positions.map((position) => [position.symbol, position.protection_ok]))
  assert.equal(bySymbol.size, 7)
  assert.equal(bySymbol.get('BTCUSDT'), true)
  assert.equal(bySymbol.get('DOGEUSDT'), true)
  assert.equal(bySymbol.get('ETHUSDT'), false)
  assert.equal(bySymbol.get('SOLUSDT'), false)
  assert.equal(bySymbol.get('XRPUSDT'), false)
  assert.equal(bySymbol.get('BNBUSDT'), null)
  assert.equal(bySymbol.get('ADAUSDT'), null)
})

test('service cycle evidence and audit rows share one snapshot/call chain', async () => {
  const env = makeIntegration({ service: makeFakeService({ credentials: true }) })
  const result = await env.integration.run()
  assert.equal(result.ok, true, JSON.stringify(result.round?.error))
  const snapshotId = result.round.freshness.snapshot_id
  assert.match(snapshotId, /^ev-/)
  assert.equal(result.round.freshness.source, 'service:/api/cycles')
  assert.equal(String(snapshotId).startsWith('host-'), false)
  assert.equal(result.round.evidence.snapshot_id, snapshotId)
  assert.equal(result.round.evidence.evidence_id, snapshotId)
  assert.equal(result.round.evidence.evidence_hash, result.round.freshness.sha256)
  assert.equal(result.round.evidence.service_sha256, result.round.freshness.sha256)
  assert.equal(result.round.evidence.bytes, 4321)
  assert.equal(result.round.evidence.verdict, 'GOOD')
  assert.ok(result.round.evidence.view_evidence_hash)
  assert.notEqual(result.round.evidence.view_evidence_hash, result.round.evidence.evidence_hash)
  assert.ok(result.round.aiCalls.length > 0)
  for (const call of result.round.aiCalls) {
    assert.ok(call.call_id)
    assert.equal(call.evidence_snapshot_id, snapshotId)
  }
  const aiRunCall = env.service.calls.find((call) => call.path.endsWith('/ai-runs'))
  assert.ok(aiRunCall, 'Host must create a service AI run')
  const aiRunId = 'run-service-test'
  const callCalls = env.service.calls.filter((call) => call.path.includes(`/api/ai-runs/${aiRunId}/calls/`))
  assert.ok(callCalls.length > 0)
  for (const call of callCalls) {
    assert.equal(call.body.evidence_snapshot_id, snapshotId)
    assert.ok(call.body.started_at)
    assert.ok(call.body.ended_at)
  }
  const artifactCalls = env.service.calls.filter((call) => call.path.includes('/artifacts/'))
  assert.ok(artifactCalls.length > 0)
  for (const artifact of artifactCalls) {
    assert.ok(artifact.body.call_id, `artifact ${artifact.path} must carry call_id`)
    assert.ok(artifact.body.output_id.includes(aiRunId))
    assert.equal(artifact.body.ai_run_id, aiRunId)
  }
})

test('P1 evidence miss preserves 422, cycle_id and a specific gap', async () => {
  const env = makeIntegration({ service: makeFakeService({ p1Status: 422, p1Code: 'E_EVIDENCE_UNAVAILABLE' }) })
  const result = await env.integration.run()
  assert.equal(result.ok, false)
  assert.equal(result.round.status, 'failed')
  assert.equal(result.round.httpStatus, 422)
  assert.equal(result.round.errorStatus.stage, 'P1')
  assert.equal(result.round.errorStatus.code, 'E_EVIDENCE_UNAVAILABLE')
  assert.ok(result.round.gaps.some((gap) => gap.includes('P1 has no server md-evidence/1 snapshot')))
  assert.equal(result.round.execution.p1.code, 'E_EVIDENCE_UNAVAILABLE')
})

test('P1 401/409/422/423/503 keep displayable service status', async () => {
  const cases = [
    [401, 'unauthorized'],
    [409, 'conflict'],
    [422, 'rejected'],
    [423, 'locked'],
    [503, 'unavailable'],
  ]
  for (const [status, expected] of cases) {
    const env = makeIntegration({ service: makeFakeService({ p1Status: status }) })
    const result = await env.integration.run()
    assert.equal(result.ok, false)
    assert.equal(result.round.status, 'failed')
    assert.equal(result.round.httpStatus, status)
    assert.equal(result.round.serviceStatus, expected)
    assert.equal(result.round.errorStatus.stage, 'P1')
    assert.equal(result.round.errorStatus.httpStatus, status)
    assert.equal(env.service.calls.some((call) => call.path === '/api/execution/order'), false)
  }
})

test('P2 401/409/422/423/503 keep displayable service status', async () => {
  const cases = [
    [401, 'unauthorized'],
    [409, 'conflict'],
    [422, 'rejected'],
    [423, 'locked'],
    [503, 'unavailable'],
  ]
  for (const [status, expected] of cases) {
    const env = makeIntegration({ service: makeFakeService({ credentials: true, p2Status: status }) })
    const result = await env.integration.run()
    assert.equal(result.ok, false)
    assert.equal(result.round.status, 'failed')
    assert.equal(result.round.httpStatus, status)
    assert.equal(result.round.serviceStatus, expected)
    assert.equal(result.round.errorStatus.stage, 'P2')
    assert.equal(result.round.errorStatus.httpStatus, status)
  }
})

test('POST /api/cycles receives the Host cycle and P1/P2 share it', async () => {
  const env = makeIntegration({ service: makeFakeService({ cyclesStatus: 200, credentials: true }) })
  const result = await env.integration.run()
  assert.equal(result.ok, true, JSON.stringify(result.round?.error))
  assert.equal(result.round.outcome, 'trade')
  const cycleCall = env.service.calls.find((call) => call.path === '/api/cycles')
  const p1Call = env.service.calls.find((call) => call.path === '/api/risk/evaluate')
  const p2Call = env.service.calls.find((call) => call.path === '/api/execution/order')
  assert.equal(cycleCall.body.cycle_id, result.round.cycleId)
  assert.equal(p1Call.body.cycle_id, result.round.cycleId)
  assert.equal(p2Call.body.cycle_id, result.round.cycleId)
  assert.equal(result.round.gaps.some((gap) => gap.includes('POST /api/cycles is not implemented')), false)
})

test('state falls back to local daily-review files when the service reviews endpoint is missing', async () => {
  const env = makeIntegration()
  const dir = path.join(env.dir, 'reviews')
  fs.mkdirSync(dir, { recursive: true })
  fs.writeFileSync(path.join(dir, '2026-11-13.md'), '# local review', 'utf8')
  const state = await env.integration.state()
  assert.equal(state.reviews.length, 1)
  assert.equal(state.reviews[0].date, '2026-11-13')
  assert.equal(state.reviewsSource, 'local_files')
  assert.equal(state.reviewsGap, null)
  assert.ok(state.gaps.some((gap) => gap.includes('local files')))
})
test('role probe uses the configured role route and explicit effort (no trading)', async () => {
  const env = makeIntegration()
  const seen = []
  env.integration.llmConfig = {
    revision: 7,
    roleRoutes: { analysis: { provider: 'qoder', model: 'dfmodel', effort: 'high' } },
    defaultRoute: { provider: 'qoder', model: 'dfmodel', effort: 'high' },
  }
  env.integration.llm = {
    stream(options) {
      seen.push(options)
      return (async function* () {
        yield { type: 'text-delta', text: '{"ok":true}' }
        yield { type: 'finish', reason: { kind: 'stop' } }
      })()
    },
  }
  const result = await env.integration.probeRole('analysis')
  assert.equal(result.ok, true)
  assert.equal(result.provider, 'qoder')
  assert.equal(result.model, 'dfmodel')
  assert.equal(result.effort, 'high')
  assert.equal(seen[0].provider, 'qoder')
  assert.equal(seen[0].model, 'dfmodel')
  assert.equal(seen[0].reasoningEffort, 'high')
  assert.equal(Object.prototype.hasOwnProperty.call(seen[0], 'tools'), false)
})
test('a timed-out cycle create adopts the RUNNING cycle the service actually made', async () => {
  // 2026-11-13 regression: POST /api/cycles exceeds the data budget, the service
  // commits RUNNING, the Host fails the round and the orphan blocks later runs.
  const created = 'cyc-20261113T155214Z-b0ab49'
  let createCalls = 0
  const service = makeFakeService({ credentials: true })
  const baseFetch = service.fetchImpl
  const fetchImpl = async (url, options = {}) => {
    const u = new URL(url)
    if (u.pathname === '/api/cycles' && (options.method ?? 'GET') === 'POST') {
      createCalls += 1
      if (createCalls === 1) {
        const err = new Error('workbench-service /api/cycles timed out')
        err.name = 'ServiceClientError'
        throw Object.assign(err, {
          code: 'E_SERVICE_TIMEOUT',
          status: 503,
          retryable: true,
          details: {},
        })
      }
    }
    if (u.pathname === '/api/cycles' && (options.method ?? 'GET') === 'GET') {
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify({
          cycles: [{ cycle_id: created, slot_id: 'auto-slot-1', state: 'RUNNING', started_at: new Date(NOW).toISOString() }],
        }),
      }
    }
    if (u.pathname === `/api/cycles/${created}/evidence`) {
      // The real GET /api/cycles/:id/evidence returns the persisted record flat,
      // with the md-evidence/1 payload nested under `evidence`.
      const record = serviceEvidenceFixture(created).evidence
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify({
          cycle_id: created,
          snapshot_id: record.snapshot_id,
          sha256: record.sha256,
          path: record.path,
          bytes: record.bytes,
          schema_version: record.schema_version,
          verdict: record.verdict,
          evidence: record.evidence,
        }),
      }
    }
    return baseFetch(url, options)
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-cycle-'))
  const tokenFile = path.join(dir, '.api-token')
  fs.writeFileSync(tokenFile, `${SERVICE_TOKEN}\n`, 'utf8')
  const integration = createIntegration({
    serviceBaseUrl: 'http://127.0.0.1:8791',
    dataDir: dir,
    tokenFile,
    storePath: path.join(dir, 'rounds.json'),
    fetchImpl,
    llm: makeFakeLlm().llm,
    route: ROUTE,
    now: () => NOW,
    config: { debateMode: 'symmetric_two_round' },
  })
  const result = await integration.run({ trigger: 'auto', slotId: 'auto-slot-1' })
  assert.notEqual(result.round.reasonCode, 'E_EVIDENCE_UNAVAILABLE')
  assert.equal(result.round.cycleId, created)
  assert.equal(result.round.cycleSource, 'service')
  assert.ok(result.round.gaps.some((gap) => gap.includes('already created cycle')))
  assert.ok(createCalls >= 1)
})
test('an old orphan RUNNING cycle is finalized before the retry', async () => {
  const orphan = 'cyc-20261113T140000Z-orphan'
  const service = makeFakeService({ credentials: true })
  const baseFetch = service.fetchImpl
  const finalized = []
  let firstCreate = true
  const fetchImpl = async (url, options = {}) => {
    const u = new URL(url)
    if (u.pathname === '/api/cycles' && (options.method ?? 'GET') === 'POST' && firstCreate) {
      firstCreate = false
      const err = Object.assign(new Error('timeout'), {
        code: 'E_SERVICE_TIMEOUT', status: 503, retryable: true, details: {},
      })
      err.name = 'ServiceClientError'
      throw err
    }
    if (u.pathname === '/api/cycles' && (options.method ?? 'GET') === 'GET') {
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify({
          cycles: [{ cycle_id: orphan, slot_id: 'auto-old', state: 'RUNNING', started_at: new Date(NOW - 3 * 3600_000).toISOString() }],
        }),
      }
    }
    if (u.pathname === `/api/cycles/${orphan}/finalize`) {
      finalized.push(JSON.parse(options.body))
      return { ok: true, status: 200, text: async () => JSON.stringify({ cycle: { cycle_id: orphan, state: 'FAILED' } }) }
    }
    if (/^\/api\/cycles\/[^/]+\/evidence$/.test(u.pathname)) {
      const id = u.pathname.split('/')[3]
      const record = serviceEvidenceFixture(id).evidence
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify({
          cycle_id: id,
          snapshot_id: record.snapshot_id,
          sha256: record.sha256,
          path: record.path,
          bytes: record.bytes,
          schema_version: record.schema_version,
          verdict: record.verdict,
          evidence: record.evidence,
        }),
      }
    }
    return baseFetch(url, options)
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-orphan-'))
  const tokenFile = path.join(dir, '.api-token')
  fs.writeFileSync(tokenFile, `${SERVICE_TOKEN}\n`, 'utf8')
  const integration = createIntegration({
    serviceBaseUrl: 'http://127.0.0.1:8791',
    dataDir: dir,
    tokenFile,
    storePath: path.join(dir, 'rounds.json'),
    fetchImpl,
    llm: makeFakeLlm().llm,
    route: ROUTE,
    now: () => NOW,
    config: { debateMode: 'symmetric_two_round' },
  })
  const result = await integration.run({ trigger: 'auto', slotId: 'auto-new' })
  assert.equal(finalized.length, 1)
  assert.equal(finalized[0].reason, 'HOST_STALE_CYCLE')
  assert.notEqual(result.round?.reasonCode, 'E_CYCLE_RUNNING')
})
test('a second Host is blocked by the lease before P1/P2', async () => {
  const env = makeIntegration({ service: makeFakeService({ credentials: true, leaseReject: true }) })
  await assert.rejects(
    () => env.integration.run(),
    (error) => /LEASE/i.test(String(error.code)) || /lease/i.test(String(error.message)),
  )
  assert.equal(env.service.calls.some((call) => call.path === '/api/risk/evaluate'), false)
  assert.equal(env.service.calls.some((call) => call.path === '/api/execution/order'), false)
  const rounds = env.integration.store.list()
  assert.equal(rounds[0].status, 'failed')
  assert.equal(rounds[0].reasonCode, 'E_LEASE_HELD')
})
test('Host acquires one execution lease and passes it to P1/P2 before release', async () => {
  const env = makeIntegration({ service: makeFakeService({ credentials: true }) })
  const result = await env.integration.run()
  assert.equal(result.ok, true, JSON.stringify(result.round?.error))
  const leases = env.service.calls.filter((call) => call.path === '/api/execution/lease/acquire')
  const releases = env.service.calls.filter((call) => call.path === '/api/execution/lease/release')
  const p1 = env.service.calls.find((call) => call.path === '/api/risk/evaluate')
  const p2 = env.service.calls.find((call) => call.path === '/api/execution/order')
  assert.equal(leases.length, 1)
  assert.equal(releases.length, 1)
  assert.ok(leases[0].body.run_id)
  assert.equal(p1.body.lease_id, 'lease-1')
  assert.equal(p2.body.lease_id, 'lease-1')
  assert.equal(env.integration.runLeases.size, 0)
})
test('service-created cycle_id is adopted and shared by P1/P2', async () => {
  const serviceCycle = 'cyc-20261109T014700Z-abcdef'
  const env = makeIntegration({
    service: makeFakeService({ cyclesStatus: 200, cycleIdOverride: serviceCycle, credentials: true }),
  })
  const result = await env.integration.run()
  assert.equal(result.ok, true, JSON.stringify(result.round?.error))
  assert.equal(result.round.cycleId, serviceCycle)
  assert.equal(result.round.cycleSource, 'service')
  const p1Call = env.service.calls.find((call) => call.path === '/api/risk/evaluate')
  const p2Call = env.service.calls.find((call) => call.path === '/api/execution/order')
  assert.equal(p1Call.body.cycle_id, serviceCycle)
  assert.equal(p2Call.body.cycle_id, serviceCycle)
  assert.ok(result.round.gaps.some((gap) => gap.includes('service cycle')))
})

test('env token wins over file token, and file token is readable', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-token-'))
  const file = path.join(dir, '.api-token')
  fs.writeFileSync(file, 'file-token\n', 'utf8')
  const fromFile = loadApiToken({ dataDir: dir, env: {} })
  assert.equal(fromFile.source, 'file')
  assert.equal(fromFile.token, 'file-token')
  assert.equal(fromFile.present, true)
  const fromEnv = loadApiToken({ dataDir: dir, env: { WORKBENCH_API_TOKEN: 'env-token' } })
  assert.equal(fromEnv.source, 'env')
  assert.equal(fromEnv.token, 'env-token')
  const missing = loadApiToken({ dataDir: path.join(dir, 'missing'), env: {} })
  assert.equal(missing.present, false)
  assert.equal(missing.token, null)
})

test('envelope strong/SL/notional caps are mapped', () => {
  const caps = capsFromEnvelope({
    limits: {
      per_trade: {
        max_risk_pct: 0.01,
        strong_max_risk_pct: 0.02,
        max_leverage: 10,
        strong_max_leverage: 15,
        min_risk_reward: 1.2,
        sl_min_distance_pct: 0.002,
        sl_max_distance_pct: 0.05,
        max_notional_multiple: 2,
      },
    },
  })
  assert.equal(caps.maxRiskPct, 1)
  assert.equal(caps.strongMaxRiskPct, 2)
  assert.equal(caps.maxLeverage, 10)
  assert.equal(caps.strongMaxLeverage, 15)
  assert.equal(caps.minRr, 1.2)
  assert.equal(caps.slMinPct, 0.2)
  assert.equal(caps.slMaxPct, 5)
  assert.equal(caps.maxNotionalMultiple, 2)
})

test('service status mappings are displayable and fail closed', async () => {
  const cases = [
    { status: 401, code: 'E_SERVICE_UNAUTHORIZED', expectedStatus: 502 },
    { status: 409, code: 'E_SERVICE_CONFLICT', expectedStatus: 409 },
    { status: 422, code: 'E_SERVICE_REJECTED', expectedStatus: 422 },
    { status: 423, code: 'E_SERVICE_LOCKED', expectedStatus: 423 },
    { status: 503, code: 'E_SERVICE_UNAVAILABLE', expectedStatus: 503 },
  ]
  for (const row of cases) {
    const env = makeIntegration({
      service: {
        calls: [],
        fetchImpl: async () => jsonResponse(row.status, { error: { code: `E_HTTP_${row.status}`, message: `HTTP ${row.status}` } }),
      },
    })
    await assert.rejects(
      () => env.integration.run(),
      (error) => error instanceof HostIntegrationError && error.status === row.expectedStatus && error.code === row.code,
    )
  }
  const offline = makeIntegration({ service: { calls: [], fetchImpl: async () => { throw new Error('ECONNREFUSED') } } })
  await assert.rejects(
    () => offline.integration.run(),
    (error) => error instanceof HostIntegrationError && error.status === 503 && error.code === 'E_SERVICE_UNAVAILABLE',
  )
})

test('missing service token fails closed before any service call', async () => {
  const env = makeIntegration({ token: null })
  await assert.rejects(
    () => env.integration.run(),
    (error) => error instanceof HostIntegrationError && error.status === 503 && error.code === 'E_SERVICE_TOKEN_MISSING',
  )
  assert.equal(env.service.calls.length, 0)
})

test('a second manual run while one is in flight returns 409', async () => {
  const service = makeFakeService()
  const originalFetch = service.fetchImpl
  service.fetchImpl = async (url, options) => {
    if (new URL(url).pathname === '/api/ready') await new Promise((resolve) => setTimeout(resolve, 50))
    return originalFetch(url, options)
  }
  const env = makeIntegration({ service, fakeLlm: makeFakeLlm({ mode: 'no_trade' }) })
  const first = env.integration.run()
  await new Promise((resolve) => setTimeout(resolve, 10))
  await assert.rejects(
    () => env.integration.run(),
    (error) => error instanceof HostIntegrationError && error.status === 409 && error.code === 'RUN_IN_PROGRESS',
  )
  const result = await first
  assert.equal(result.round.outcome, 'no_trade')
})

test('run route preserves P1 service HTTP status', async () => {
  const env = makeIntegration({ service: makeFakeService({ p1Status: 423 }) })
  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address()
  try {
    const response = await fetch(`http://127.0.0.1:${port}/trading-workbench/api/run`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: '{}',
    })
    assert.equal(response.status, 423)
    const body = await response.json()
    assert.equal(body.round.status, 'failed')
    assert.equal(body.round.httpStatus, 423)
    assert.equal(body.round.service_status, 'locked')
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})

test('run/state/rounds routes work over loopback HTTP', async () => {
  const env = makeIntegration({ fakeLlm: makeFakeLlm({ mode: 'no_trade' }) })
  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address()
  const base = `http://127.0.0.1:${port}`
  try {
    const runResponse = await fetch(`${base}/trading-workbench/api/run`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ symbol: 'BTCUSDT' }),
    })
    assert.equal(runResponse.status, 200)
    const runBody = await runResponse.json()
    assert.equal(runBody.round.outcome, 'no_trade')
    const stateResponse = await fetch(`${base}/trading-workbench/api/state`)
    assert.equal(stateResponse.status, 200)
    const stateBody = await stateResponse.json()
    assert.equal(stateBody.rounds.length, 1)
    assert.equal(JSON.stringify(stateBody).includes('approval-token-secret'), false)
    const roundsResponse = await fetch(`${base}/trading-workbench/api/rounds`)
    assert.equal(roundsResponse.status, 200)
    const roundsBody = await roundsResponse.json()
    assert.equal(roundsBody.rounds.length, 1)
    const roundResponse = await fetch(`${base}/trading-workbench/api/rounds/${runBody.round.roundId}`)
    assert.equal(roundResponse.status, 200)
    const roundBody = await roundResponse.json()
    assert.equal(roundBody.round.roundId, runBody.round.roundId)
    const missing = await fetch(`${base}/trading-workbench/api/rounds/nope`)
    assert.equal(missing.status, 404)
    const pricesResponse = await fetch(`${base}/trading-workbench/api/market/prices?symbols=btcusdt,BTCUSDT,ETHUSDT`)
    const pricesBody = await pricesResponse.json()
    const pricesCall = env.service.calls.filter((call) => call.path === '/api/market/prices').at(-1)
    assert.deepEqual(
      {
        ok: pricesBody.ok,
        last: pricesBody.prices?.BTCUSDT?.last,
        asOf: pricesBody.as_of_ms,
        traceId: pricesBody.trace_id,
        forwarded: decodeURIComponent(pricesCall?.query ?? ''),
      },
      { ok: true, last: 68234.5, asOf: NOW, traceId: 'trace-prices', forwarded: '?symbols=BTCUSDT,ETHUSDT' },
    )
    const envelopeResponse = await fetch(`${base}/trading-workbench/api/risk/envelope`)
    const envelopeBody = await envelopeResponse.json()
    assert.deepEqual(
      {
        ok: envelopeBody.ok,
        newEntry: envelopeBody.new_entry_allowed,
        maxRisk: envelopeBody.limits?.per_trade?.max_risk_pct,
      },
      { ok: true, newEntry: true, maxRisk: 0.01 },
    )
    const evidenceResponse = await fetch(`${base}/trading-workbench/api/cycles/cyc-test/evidence?agent=analysis`)
    const evidenceBody = await evidenceResponse.json()
    const evidenceCall = env.service.calls.filter((call) => call.path === '/api/cycles/cyc-test/evidence').at(-1)
    assert.deepEqual(
      {
        ok: evidenceBody.ok,
        cycleId: evidenceBody.cycle_id,
        snapshotId: evidenceBody.snapshot_id,
        agent: evidenceBody.agent,
        forwarded: evidenceCall?.query,
        leaksToken: JSON.stringify(evidenceBody).includes(SERVICE_TOKEN),
      },
      { ok: true, cycleId: 'cyc-test', snapshotId: 'ev-test', agent: 'analysis', forwarded: '?agent=analysis', leaksToken: false },
    )
    const reconcileResponse = await fetch(`${base}/trading-workbench/api/reconcile`, { method: 'POST' })
    const reconcileBody = await reconcileResponse.json()
    const reconcileCall = env.service.calls.filter((call) => call.path === '/api/execution/reconcile').at(-1)
    assert.deepEqual(
      {
        ok: reconcileBody.ok,
        requestId: /^req-/.test(reconcileBody.requestId ?? ''),
        outbox: reconcileBody.result?.outbox,
        method: reconcileCall?.method,
        sentBody: reconcileCall?.body,
      },
        { ok: true, requestId: true, outbox: { pending: 0, uncertain: 0 }, method: 'POST', sentBody: { request_id: reconcileBody.requestId } },
    )
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})

test('close and repair commands proxy to the service after confirmation', async () => {
  const env = makeIntegration()
  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  try {
    const closeResponse = await fetch(`${base}/trading-workbench/api/positions/close`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ symbol: 'btcusdt', confirm: 'CLOSE', reason: 'manual risk exit', qty: '0.02' }),
    })
    assert.equal(closeResponse.status, 200)
    const closeBody = await closeResponse.json()
    assert.match(closeBody.requestId, /^req-/)
    assert.deepEqual(
      { ok: closeBody.ok, result: closeBody.result, sent: env.service.calls.find((call) => call.path === '/api/execution/close')?.body },
      {
        ok: true,
        result: { symbol: 'BTCUSDT', qty: '0.02', reason: 'manual risk exit', status: 'CLOSED', closed: true, venue: 'demo' },
        sent: { symbol: 'BTCUSDT', reason: 'manual risk exit', qty: '0.02', request_id: closeBody.requestId },
      },
    )
    const repairResponse = await fetch(`${base}/trading-workbench/api/protection/repair`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ symbols: ['ethusdt', 'ETHUSDT', 'btcusdt'], confirm: 'REPAIR' }),
    })
    assert.equal(repairResponse.status, 200)
    const repairBody = await repairResponse.json()
    assert.match(repairBody.requestId, /^req-/)
    assert.deepEqual(
      { ok: repairBody.ok, sent: env.service.calls.find((call) => call.path === '/api/protection/repair')?.body },
      { ok: true, sent: { symbols: ['ETHUSDT', 'BTCUSDT'], request_id: repairBody.requestId } },
    )
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})

test('close without confirm is rejected before calling the service', async () => {
  const env = makeIntegration()
  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  try {
    const response = await fetch(`${base}/trading-workbench/api/positions/close`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ symbol: 'BTCUSDT', reason: 'manual exit' }),
    })
    assert.equal(response.status, 422)
    const body = await response.json()
    assert.deepEqual(
      { code: body.error?.code, expected: body.error?.details?.expected, called: env.service.calls.some((call) => call.path === '/api/execution/close') },
      { code: 'E_CONFIRM_REQUIRED', expected: 'CLOSE', called: false },
    )
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})

test('close service failure is surfaced once without retry', async () => {
  const env = makeIntegration({ service: makeFakeService({ closeStatus: 503, closeCode: 'E_DEMO_CREDENTIALS_MISSING' }) })
  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  try {
    const response = await fetch(`${base}/trading-workbench/api/positions/close`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ symbol: 'BTCUSDT', confirm: 'CLOSE', reason: 'manual risk exit' }),
    })
    assert.equal(response.status, 503)
    const body = await response.json()
    assert.deepEqual(
      {
        ok: body.ok,
        code: body.error?.code,
        serviceStatus: body.error?.details?.service_status,
        closeCalls: env.service.calls.filter((call) => call.path === '/api/execution/close').length,
        leaksToken: JSON.stringify(body).includes(SERVICE_TOKEN),
      },
      { ok: false, code: 'E_SERVICE_UNAVAILABLE', serviceStatus: 503, closeCalls: 1, leaksToken: false },
    )
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})

test('daily review generates, caches and serves markdown once per date', async () => {
  const markdown = '# 2026-11-09 复盘\n\n- 本日无轮次记录，权益点未提供。'
  let release
  const gate = new Promise((resolve) => { release = resolve })
  let streamStarted
  const started = new Promise((resolve) => { streamStarted = resolve })
  const gatedLlm = {
    llm: {
      stream() {
        streamStarted()
        return (async function* stream() {
          await gate
          yield { type: 'text-delta', text: markdown }
          yield { type: 'finish', reason: { kind: 'stop' } }
        })()
      },
    },
  }
  const env = makeIntegration({ fakeLlm: gatedLlm })
  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  try {
    const pending = fetch(`${base}/trading-workbench/api/reviews/daily`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ date: '2026-11-09' }),
    })
    await started
    const conflict = await fetch(`${base}/trading-workbench/api/reviews/daily`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ date: '2026-11-09' }),
    })
    const conflictBody = await conflict.json()
    release()
    const generated = await pending
    const body = await generated.json()
    const cached = await fetch(`${base}/trading-workbench/api/reviews/daily?date=2026-11-09`)
    const cachedBody = await cached.json()
    const filePath = path.join(env.dir, 'reviews', '2026-11-09.md')
    assert.deepEqual(
      {
        status: generated.status,
        ok: body.ok,
        date: body.date,
        model: body.model,
        markdown: body.markdown,
        conflict: conflict.status,
        conflictCode: conflictBody.error?.code,
        cached: cachedBody.cached,
        cachedMarkdown: cachedBody.markdown,
        fileMatches: fs.existsSync(filePath) && fs.readFileSync(filePath, 'utf8') === body.markdown,
      },
      {
        status: 200,
        ok: true,
        date: '2026-11-09',
        model: 'fake-1',
        markdown,
        conflict: 409,
        conflictCode: 'E_REVIEW_IN_PROGRESS',
        cached: true,
        cachedMarkdown: markdown,
        fileMatches: true,
      },
    )
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})

test('daily review GET without cache returns 404 and never calls the LLM', async () => {
  let llmCalls = 0
  const env = makeIntegration({
    fakeLlm: {
      llm: {
        stream() {
          llmCalls += 1
          throw new Error('GET must not call the LLM')
        },
      },
    },
  })
  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  try {
    const response = await fetch(`${base}/trading-workbench/api/reviews/daily?date=2026-11-10`)
    const body = await response.json()
    assert.deepEqual(
      { status: response.status, code: body.error?.code, date: body.error?.details?.date, llmCalls },
      { status: 404, code: 'E_REVIEW_NOT_GENERATED', date: '2026-11-10', llmCalls: 0 },
    )
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})

test('daily review timeout fails without writing a cache file', async () => {
  const env = makeIntegration({
    reviewTimeoutMs: 30,
    fakeLlm: {
      llm: {
        stream() {
          return (async function* stream() {
            while (true) {
              await new Promise((resolve) => setTimeout(resolve, 5))
              yield { type: 'text-delta', text: 'x' }
            }
          })()
        },
      },
    },
  })
  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  try {
    const response = await fetch(`${base}/trading-workbench/api/reviews/daily`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ date: '2026-11-11' }),
    })
    const body = await response.json()
    assert.deepEqual(
      {
        status: response.status,
        ok: body.ok,
        code: body.error?.code,
        cacheWritten: fs.existsSync(path.join(env.dir, 'reviews', '2026-11-11.md')),
      },
      { status: 504, ok: false, code: 'E_REVIEW_TIMEOUT', cacheWritten: false },
    )
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})

// ---- M1.4 auto-scheduler preflight -----------------------------------------
// The scheduler must never spend an LLM call on a round that cannot trade.

test('preflight approves when service, credentials and account are ready', async () => {
  const env = makeIntegration({ service: makeFakeService({ credentials: true }) })
  const verdict = await env.integration.preflight()
  assert.equal(verdict.ok, true, JSON.stringify(verdict))
  assert.equal(verdict.control_state, 'normal')
  assert.equal(verdict.credentials_configured, true)
  assert.equal(verdict.account_ready, true)
})

test('preflight blocks on a non-normal control state without spending an LLM call', async () => {
  const fakeLlm = makeFakeLlm()
  const env = makeIntegration({
    fakeLlm,
    service: makeFakeService({ credentials: true, controlState: 'close_only' }),
  })
  const verdict = await env.integration.preflight()
  assert.equal(verdict.ok, false)
  assert.equal(verdict.code, 'CONTROL_LOCKED')
  assert.equal(fakeLlm.seen.length, 0, 'preflight must not call the model')
})

test('preflight blocks when demo credentials are missing', async () => {
  const env = makeIntegration({ service: makeFakeService({ credentials: false }) })
  const verdict = await env.integration.preflight()
  assert.equal(verdict.ok, false)
  assert.equal(verdict.code, 'E_DEMO_CREDENTIALS_MISSING')
})

test('preflight blocks when the account snapshot carries no numbers', async () => {
  const env = makeIntegration({
    service: makeFakeService({
      credentials: true,
      account: { account: null, equity: null },
    }),
  })
  const verdict = await env.integration.preflight()
  assert.equal(verdict.ok, false)
  assert.equal(verdict.code, 'ACCOUNT_UNAVAILABLE')
})

test('preflight blocks when the service is not ready', async () => {
  const service = makeFakeService({ credentials: true })
  const env = makeIntegration({ service })
  const original = env.service.fetchImpl
  // Simulate a not-ready service by forcing /api/ready to answer false.
  env.integration.fetchImpl = async (url, options) => {
    if (String(url).includes('/api/ready')) {
      return new Response(JSON.stringify({ ready: false, service_ready: false, reasons: ['E_BOOTING'] }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    }
    return original(url, options)
  }
  const verdict = await env.integration.preflight()
  assert.equal(verdict.ok, false)
  assert.equal(verdict.code, 'E_SERVICE_NOT_READY')
})
// ---- M1.4 AI decision-chain transparency -----------------------------------
// The UI can only explain what the DTO carries. These lock the fields that make
// "why did it trade / why did it stop" answerable.

test('RoundView exposes per-side gate numbers, decision rationale and chain rollup', () => {
  const view = toRoundView({
    roundId: 'cyc-r', cycleId: 'cyc-c', status: 'no_trade', outcome: 'no_trade',
    reasonCode: 'BOTH_SIDES_UNTRADEABLE', durationMs: 108000, gaps: [],
    result: {
      calls: [
        { call_id: '1', role: 'analysis', role_key: 'analysis', attempt: 1, schema_ok: true, latency_ms: 29666, input_tokens: 3204, output_tokens: 4081, finish_kind: 'stop' },
        { call_id: '2', role: 'red_team', role_key: 'red_opening', attempt: 1, schema_ok: true, latency_ms: 21000, input_tokens: 3448, output_tokens: 4235, finish_kind: 'max_tokens' },
        { call_id: '3', role: 'blue_team', role_key: 'blue_opening', attempt: 2, repaired: true, schema_ok: true, latency_ms: 15226, input_tokens: 177, output_tokens: 1940, finish_kind: 'stop' },
      ],
      scorecard: {
        debate_quality: 'both_weak', tie: false, degraded: false,
        red: { grade: 'D', gate: 'FAIL_CONFIDENCE', gate_reasons: ['LOW_CONFIDENCE'], rr: 1.23, stop_distance_pct: 0.3246, confidence: 0.36, rubric_score: 0.61, leverage: 2 },
        blue: { grade: 'C', gate: 'PASS', gate_reasons: [], rr: 3.02, stop_distance_pct: 0.4543, confidence: 0.45 },
      },
      outputs: {
        decision: {
          action: 'NO_TRADE', confidence: 0.45,
          thesis: '区间下沿反弹但 ADX 18 不支持追多',
          case_comparison: {
            red: { strength: 'weak', decisive_evidence: 'taker buy 0.89', fatal_flaw: '逆势' },
            blue: { strength: 'moderate', decisive_evidence: 'trend aligned', fatal_flaw: 'ADX 偏低' },
            decisive_factor: '没有一侧同时满足趋势与盈亏比', opposing_risk: '假突破',
          },
          plan: { entry: { style: 'MARKET', price: 74542 }, stop_loss: { price: 74300 }, take_profit: { price: 74840 }, risk_pct: 0.5, leverage: 2, rr_to_target: 1.23 },
          invalidation: { price_event: '收上 75180', time: '8h' },
          warnings: ['衍生品缺失'],
        },
        analysis: {
          stand_aside: false,
          candidates: [{ symbol: 'BTCUSDT', side_bias: 'SHORT', setup_type: 'trend_pullback', confidence: 0.43 }],
        },
      },
    },
  })

  assert.equal(view.chain.calls, 3)
  assert.equal(view.chain.retries, 1)
  assert.equal(view.chain.truncated, 1)
  assert.equal(view.chain.tokens_in, 6829)
  assert.equal(view.chain.deadline_ms, 340000)
  assert.equal(view.chain.slowest.role_key, 'analysis')

  assert.equal(view.scorecard.red.rr, 1.23)
  assert.equal(view.scorecard.red.stop_distance_pct, 0.3246)
  assert.equal(view.scorecard.blue.confidence, 0.45)
  assert.equal(view.scorecard.gate, null, 'no selected case -> top-level gate stays null')

  assert.equal(view.decision.thesis, '区间下沿反弹但 ADX 18 不支持追多')
  assert.equal(view.decision.case_comparison.blue.decisive_evidence, 'trend aligned')
  assert.equal(view.decision.plan.entry_price, 74542)
  assert.equal(view.decision.plan.rr, 1.23)
  assert.equal(view.decision.invalidation.time, '8h')
  assert.deepEqual(view.decision.warnings, ['衍生品缺失'])

  assert.equal(view.analysis.candidates[0].setup_type, 'trend_pullback')
})


// ---- 05 N01/N02: decision-chain projection + load identity ----------------
test('RoundView projects debate, per-call errors, stop kind, attribution and stages', () => {
  const view = toRoundView({
    roundId: 'cyc-v2', cycleId: 'cyc-v2', status: 'failed', outcome: 'no_trade',
    reasonCode: 'RISK_UNAVAILABLE', durationMs: 60000, gaps: [],
    result: {
      debateMode: 'symmetric_two_round',
      degraded: false,
      calls: [
        { call_id: 'c1', role: 'analysis', role_key: 'analysis', attempt: 1, schema_ok: true, latency_ms: 10, started_at_ms: 1000, ended_at_ms: 1010 },
        {
          call_id: 'c2', role: 'risk', role_key: 'risk', attempt: 1, schema_ok: false, latency_ms: 45000,
          error_code: 'LLM_TIMEOUT', error_message: 'LLM call exceeded 45000 ms',
          validation_errors: [{ path: '$', message: 'LLM call exceeded 45000 ms', keyword: 'LLM_TIMEOUT' }],
          started_at_ms: 2000, ended_at_ms: 47000,
        },
      ],
      outputs: {
        redOpening: {
          schema_version: 'debate_case/1', agent: 'red_team', symbol: 'BTCUSDT', side: 'LONG', round: 1,
          tradeable: true, confidence: 0.62, thesis: '15m 回踩 EMA20 未破，1h 结构仍为 HL。',
          evidence: [{ ref: 'symbols.BTCUSDT.price.last', claim: 'taker buy 0.83', weight: 'high' }],
          rebuttal: [{ target_claim: '资金费拥挤', counter: '拥挤不等于反转。' }],
          opponent_steelman: '蓝队最强论点：上方双阻力未被突破。',
          no_case_reason: null, plan_draft: null,
        },
        decision: {
          action: 'NO_TRADE', selected_case: 'red', confidence: 0.4,
          case_comparison: { decisive_factor: '风控未产出', opposing_risk: '未知风险' },
        },
      },
    },
  })

  assert.equal(view.attribution.category, '链路')
  assert.equal(view.attribution.stage, '风控 AI')
  assert.equal(view.attribution.one_liner_zh, '风控 AI 未产出，按保守策略不交易')
  assert.equal(view.attribution.one_liner_en, 'Risk AI produced no output; the round stays conservative and does not trade')
  assert.equal(view.attribution.next_action_en, 'Raise the risk output cap or drop a tier; wait for the next round')
  assert.equal(view.attribution.next_action_zh, '提高风控输出上限或降档；等待下一轮')
  assert.equal(view.attribution.mapped, true)

  assert.equal(view.nodes[1].error_code, 'LLM_TIMEOUT')
  assert.equal(view.nodes[1].error_message, 'LLM call exceeded 45000 ms')
  assert.equal(view.nodes[1].validation_errors[0].keyword, 'LLM_TIMEOUT')

  assert.equal(view.chain.stop_kind, 'failed')
  assert.equal(view.chain.stop_role_key, 'risk')
  assert.equal(view.chain.stop_call_id, 'c2')
  assert.equal(view.chain.stop_error_code, 'LLM_TIMEOUT')
  assert.ok(view.chain.stopped_at, 'a failed round names the first stage that never produced')

  assert.equal(view.debate.mode, 'symmetric_two_round')
  assert.equal(view.debate.rounds.r1.red.tradeable, true)
  assert.equal(view.debate.rounds.r1.red.thesis, '15m 回踩 EMA20 未破，1h 结构仍为 HL。')
  assert.equal(view.debate.rounds.r1.red.evidence[0].claim, 'taker buy 0.83')
  assert.equal(view.debate.rounds.r1.blue, null)
  assert.equal(view.debate.adopted.side, 'red')
  assert.equal(view.debate.adopted.decisive_factor, '风控未产出')

  const riskStage = view.stages.find((stage) => stage.stage_key === 'risk')
  assert.equal(riskStage.status, 'error')
  assert.equal(riskStage.error_code, 'LLM_TIMEOUT')
  assert.equal(riskStage.started_at_ms, 2000)
  assert.equal(riskStage.finished_at_ms, 47000)
  assert.equal(view.stages.find((stage) => stage.stage_key === 'analysis').status, 'ok')
  assert.equal(view.stages.find((stage) => stage.stage_key === 'decision').status, 'pending')

  assert.equal(typeof view.build.buildId, 'string')
  assert.equal(view.build.chain_version, 'ai-chain/1')
  assert.equal(view.build.configRevision, null, 'no saved config yet -> null, never 0')
})

test('a legal terminal never reads as an early stop', () => {
  const noTrade = toRoundView({
    roundId: 'cyc-skip', status: 'done', outcome: 'no_trade', reasonCode: 'NO_CANDIDATE',
    result: { calls: [{ call_id: 'c1', role: 'analysis', role_key: 'analysis', attempt: 1, schema_ok: true, latency_ms: 5 }], outputs: {} },
  })
  assert.equal(noTrade.chain.stop_kind, 'decision_no_trade')
  assert.equal(noTrade.chain.stopped_at, null)

  const skipped = toRoundView({
    roundId: 'cyc-slot', status: 'skipped', outcome: 'skipped', reasonCode: 'E_CYCLE_DUPLICATE_SLOT',
    result: { calls: [], outputs: {} },
  })
  assert.equal(skipped.chain.stop_kind, 'skipped')
  assert.equal(skipped.chain.stopped_at, null)

  // Unknown codes fail closed.
  assert.equal(stopKindOf({ reasonCode: 'E_SOMETHING_NEW', status: 'failed' }), 'failed')
  assert.equal(stopKindOf({ reasonCode: 'E_SOMETHING_NEW', status: 'done' }), 'failed')
})

test('attribution covers the 02 route codes and never invents a sentence', () => {
  const route = attributeRound({ reasonCode: 'ROUTE_UNAVAILABLE', calls: [] })
  assert.equal(route.category, '基础设施（路由层）')
  assert.equal(route.mapped, true)
  assert.match(route.one_liner_zh, /路由不可用/)

  const fallback = attributeRound({ reasonCode: 'ROUTE_FALLBACK_APPLIED', calls: [] })
  assert.equal(fallback.mapped, true)
  assert.match(fallback.one_liner_zh, /备用路由/)

  const unknown = attributeRound({ reasonCode: 'E_NEVER_SEEN', calls: [] })
  assert.equal(unknown.mapped, false)
  assert.equal(unknown.one_liner_zh, null)
  assert.equal(unknown.next_action_zh, null)
  // The English columns are carried by the same DTO for the panel language
  // switch; an unmapped code must leave both null (never translated by guess).
  assert.equal(unknown.one_liner_en, null)
  assert.equal(unknown.next_action_en, null)

  // Every mapped leaf carries both languages: the Client never re-derives 01
  // prose and must not fall back to Chinese when the panel is in English.
  for (const code of ATTRIBUTION_CODES) {
    const entry = attributeRound({ reasonCode: code, calls: [] })
    assert.equal(typeof entry.one_liner_en, 'string', `${code} one_liner_en`)
    assert.ok(entry.one_liner_en.trim().length > 0, `${code} one_liner_en empty`)
    assert.equal(typeof entry.next_action_en, 'string', `${code} next_action_en`)
    assert.ok(entry.next_action_en.trim().length > 0, `${code} next_action_en empty`)
  }

  // EXECUTION_BLOCKED:<sub> inherits the base sentence when the sub-code is new.
  const blocked = attributeRound({ reasonCode: 'EXECUTION_BLOCKED:NEW_SUB', calls: [] })
  assert.equal(blocked.category, '执行')
  assert.equal(blocked.mapped, true)
})

test('build identity is process-level and stable, and configRevision is settable by the Host', () => {
  const first = currentBuild()
  const second = currentBuild()
  assert.equal(first.buildId, second.buildId, 'same process -> same build id')
  assert.match(first.buildId, /^twb-/)
  assert.equal(first.chain_version, 'ai-chain/1')
  assert.equal(first.configRevision, null)
  assert.ok(first.loaded_at)
  assert.ok(first.prompt_set)
  assert.equal(setLoadedConfigRevision(3), 3)
  assert.equal(currentBuild().configRevision, 3)
  setLoadedConfigRevision(null)
  assert.equal(currentBuild().configRevision, null)
})
test('a Host reload advances buildId even when the module instance is cached', () => {
  // Regression: a cached ESM ./host/build.js used to freeze loaded_at, so a new
  // probe route served while /health still advertised the previous load.
  const before = currentBuild()
  const seqBefore = hostLoadSeq()
  const after = markHostLoad(Date.parse('2026-11-13T15:00:00.000Z'))
  assert.equal(after.loaded_at, '2026-11-13T15:00:00.000Z')
  // buildId = 载入时刻 + 当前提示集摘要（ENTRY_DIGEST = sha256(chain_version + PROMPT_VERSIONS)）。
  // 这里从 build() 自己算出的 prompt_set 派生，不写死 8 位串：提示词版本一旦（有意）bump，
  // 断言仍成立，不会制造假红；写死字面量只会把"提示词变了"伪装成"reload 回归"。
  const promptDigest = String(after.prompt_set ?? '').split('/')[1]
  assert.match(promptDigest, /^[0-9a-f]{8}$/)
  assert.equal(after.buildId, `twb-20261113T150000Z-${promptDigest}`)
  assert.notEqual(after.buildId, before.buildId)
  assert.equal(hostLoadSeq(), seqBefore + 1)
  assert.equal(currentBuild().buildId, after.buildId)
  assert.equal(after.chain_version, 'ai-chain/1')
})


/**
 * T23：prompt_set 必须由 chain_version + PROMPT_VERSIONS 派生，不能是写死的摘要。
 *
 * 背景：t17 里为了不再假红，把 buildId 断言改成"从 after.prompt_set 派生"。
 * 但 prompt_set 与 buildId 后缀是同源生成（build.js 的 ENTRY_DIGEST），
 * 只断言"8 位 hex"或"两者相等"抓不到"digest 被写死"这个缺口——写死后两条
 * 断言依然绿。这里用**换输入 → 输出必变**的方式钉住派生关系。
 */
function promptDigestOf(versions, chainVersion = CHAIN_VERSION) {
  return createHash('sha256')
    .update(chainVersion)
    .update(JSON.stringify(versions))
    .digest('hex')
    .slice(0, 8)
}

test('prompt_set is derived from chain_version + PROMPT_VERSIONS, not a hardcoded digest', () => {
  const expected = promptDigestOf(PROMPT_VERSIONS)
  assert.match(expected, /^[0-9a-f]{8}$/)
  assert.equal(BUILD.chain_version, CHAIN_VERSION)
  // 产品值必须等于同一条派生式（写死 digest 的实现在这里必红）。
  assert.equal(BUILD.prompt_set, `prompts/${expected}`)

  const build = currentBuild()
  assert.equal(build.prompt_set, BUILD.prompt_set)
  assert.equal(build.buildId.endsWith(`-${expected}`), true, 'buildId suffix must be the same derived digest')

  // reload 只推进 loaded_at，不改变摘要（摘要只由输入决定，不由加载次数决定）。
  const after = markHostLoad(Date.parse('2026-11-13T16:30:00.000Z'))
  assert.equal(after.prompt_set, BUILD.prompt_set)
  assert.equal(after.buildId.endsWith(`-${expected}`), true)

  // 变异敏感 #1：任一角色版本变化，摘要必须随之变化（逐键覆盖，防止只把部分键算进去）。
  for (const role of Object.keys(PROMPT_VERSIONS)) {
    const mutated = { ...PROMPT_VERSIONS, [role]: `${PROMPT_VERSIONS[role]}-probe` }
    assert.notEqual(promptDigestOf(mutated), expected, `changing ${role} must change the digest`)
    assert.notEqual(`prompts/${promptDigestOf(mutated)}`, BUILD.prompt_set, `changing ${role} must change prompt_set`)
  }
  // 变异敏感 #2：chain_version 也参与派生（单独改它同样改变摘要）。
  assert.notEqual(promptDigestOf(PROMPT_VERSIONS, 'ai-chain/9-probe'), expected)
  // 变异敏感 #3：键集合也是输入——删掉一个键必须改变摘要。
  const withoutFirst = { ...PROMPT_VERSIONS }
  delete withoutFirst[Object.keys(PROMPT_VERSIONS)[0]]
  assert.notEqual(promptDigestOf(withoutFirst), expected)
})

test('changing one prompt version in a temp copy actually moves the built prompt_set', async () => {
  // 上面那条用的是"在测试内构造输入"；这里再进一步：把 build.js 与 constants.js
  // 复制到临时目录，改掉 decision 的版本号，**真的重新加载产品模块**，断言
  // 它算出的 prompt_set 变了。写死 digest 的实现无法通过这一条。
  const pluginDir = fileURLToPath(new URL('..', import.meta.url))
  const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-t23-prompts-'))
  try {
    fs.mkdirSync(path.join(tmpDir, 'host', 'ai'), { recursive: true })
    fs.copyFileSync(path.join(pluginDir, 'host', 'build.js'), path.join(tmpDir, 'host', 'build.js'))
    const constantsPath = path.join(pluginDir, 'host', 'ai', 'constants.js')
    const source = fs.readFileSync(constantsPath, 'utf8')
    // 只在 PROMPT_VERSIONS 块内改 decision，避免误伤 OUTPUT_SCHEMAS 里同名的
    // 'decision/1'（第一次写这条用例时就踩了这个坑：改错了键，摘要当然不变）。
    const marker = 'export const PROMPT_VERSIONS = Object.freeze({'
    const markerAt = source.indexOf(marker)
    assert.ok(markerAt >= 0, 'constants.js must still export PROMPT_VERSIONS')
    const head = source.slice(0, markerAt)
    const body = source.slice(markerAt)
    const patchedBody = body.replace(/(\bdecision:\s*')([^']+)(')/, (whole, lead, version, tail) => `${lead}${version}-probe${tail}`)
    assert.notEqual(patchedBody, body, 'patch must hit the decision entry inside PROMPT_VERSIONS')
    const patched = head + patchedBody
    fs.writeFileSync(path.join(tmpDir, 'host', 'ai', 'constants.js'), patched, 'utf8')
    const patchedConstants = await import(pathToFileURL(path.join(tmpDir, 'host', 'ai', 'constants.js')).href)
    assert.equal(
      patchedConstants.PROMPT_VERSIONS.decision,
      `${PROMPT_VERSIONS.decision}-probe`,
      'the temp copy must really carry the mutated version',
    )

    const mutated = await import(pathToFileURL(path.join(tmpDir, 'host', 'build.js')).href)
    const mutatedVersions = { ...PROMPT_VERSIONS, decision: `${PROMPT_VERSIONS.decision}-probe` }
    const mutatedDigest = promptDigestOf(mutatedVersions)
    assert.equal(mutated.BUILD.prompt_set, `prompts/${mutatedDigest}`)
    assert.notEqual(mutated.BUILD.prompt_set, BUILD.prompt_set, 'a changed prompt version must change prompt_set')
    assert.notEqual(mutatedDigest, promptDigestOf(PROMPT_VERSIONS))
    assert.equal(mutated.currentBuild().buildId.endsWith(`-${mutatedDigest}`), true)
  } finally {
    fs.rmSync(tmpDir, { recursive: true, force: true })
  }
})

test('RoundView never turns missing values into the strings null/undefined', () => {
  const view = toRoundView({
    roundId: 'cyc-n', cycleId: 'cyc-n', status: 'done', outcome: 'no_trade', gaps: [],
    result: { calls: [], outputs: { analysis: { stand_aside: false, warnings: [] } } },
  })
  assert.equal(view.analysis.stand_aside_reason, null)
  assert.equal(view.reasonDetail, null)
})

test('RoundView stops_at ignores R2 stages when the debate degraded', () => {
  const view = toRoundView({
    roundId: 'cyc-d', cycleId: 'cyc-d', status: 'done', outcome: 'trade', durationMs: 40000, gaps: [],
    result: {
      degraded: true,
      calls: ['analysis', 'red_opening', 'blue_opening', 'decision', 'risk', 'execution_entry'].map((role, index) => ({
        call_id: String(index + 1), role, role_key: role, attempt: 1, schema_ok: true, latency_ms: 1000,
      })),
      outputs: {},
    },
  })
  // A degraded round never asks for R2, so it must not look "stopped at red_response".
  assert.equal(view.chain.stopped_at, null)
})

// ---- 05 P1-1: per-role model / effort configuration -----------------------
const CONFIG_MODELS = [
  { provider: 'qoder', id: 'dfmodel', name: 'DeepSeek-Flash', efforts: ['off', 'low', 'high', 'max'], defaultEffort: 'high', contextWindow: 200000 },
  { provider: 'qoder', id: 'qmodel_latest', name: 'Qwen4-Max-Preview', efforts: ['off', 'low', 'high', 'xhigh'], defaultEffort: 'xhigh', contextWindow: 200000 },
]

function makeFakeCatalog({ models = CONFIG_MODELS, unavailable = false } = {}) {
  const state = { models: models.map((entry) => ({ ...entry })), unavailable, resolveCalls: [] }
  const byProvider = () => {
    const map = new Map()
    for (const entry of state.models) {
      if (!map.has(entry.provider)) map.set(entry.provider, [])
      map.get(entry.provider).push(entry)
    }
    return map
  }
  return {
    usable: true,
    state,
    get resolveCalls() { return state.resolveCalls },
    async snapshot() {
      if (state.unavailable) return { available: false, fetchedAt: null, groups: [], failures: [], reason: 'CATALOG_UNAVAILABLE' }
      const map = byProvider()
      return {
        available: map.size > 0,
        fetchedAt: new Date(NOW).toISOString(),
        groups: [...map.entries()].map(([id, list]) => ({
          id, name: id,
          models: list.map((entry) => ({ id: entry.id, name: entry.name, efforts: entry.efforts, defaultEffort: entry.defaultEffort, contextWindow: entry.contextWindow })),
        })),
        failures: [],
        reason: map.size > 0 ? null : 'NO_MODELS',
      }
    },
    async resolve(provider, model) {
      state.resolveCalls.push(`${provider}/${model}`)
      if (state.unavailable) return null
      return state.models.find((entry) => entry.provider === provider && entry.id === model) ?? null
    },
    async providerForModel(model) {
      if (state.unavailable) return null
      return state.models.find((entry) => entry.id === model)?.provider ?? null
    },
  }
}

function readSavedConfig(dir) {
  const file = path.join(dir, 'llm-config.json')
  return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : null
}

test('P1-1 D-6=B: first launch fixates the balanced preset as revision 1', async () => {
  const env = makeIntegration({ fakeLlm: makeFakeLlm({ mode: 'no_trade' }), llmCatalog: makeFakeCatalog() })
  const state = await env.integration.state()
  assert.equal(state.llm_config.revision, 1)
  assert.equal(state.llm_config.preset, 'balanced')
  assert.equal(state.llm_config.default.provider, 'qoder')
  assert.equal(state.llm_config.default.model, 'dfmodel')
  assert.equal(state.llm_config.banner, null)
  const saved = readSavedConfig(env.dir)
  assert.equal(saved.schema, 'twb-llm-config/1')
  assert.equal(saved.revision, 1)
  assert.equal(saved.defaultRoute.source, 'explicit')
  for (const role of ['analysis', 'red_opening', 'blue_opening', 'decision', 'risk', 'execution_entry', 'review']) {
    assert.equal(saved.roleRoutes[role].model, 'dfmodel', `${role} must fixate dfmodel, never the global default`)
  }
  assert.equal(currentBuild().configRevision, 1, '/health.loadedConfigRevision comes from the file')
  setLoadedConfigRevision(null)
})

test('P1-1 D-6=B: an unavailable catalog writes nothing and shows a banner', async () => {
  const env = makeIntegration({ fakeLlm: makeFakeLlm({ mode: 'no_trade' }), llmCatalog: makeFakeCatalog({ unavailable: true }) })
  const state = await env.integration.state()
  assert.equal(state.llm_config.revision, null, 'the first-launch window keeps revision=null')
  assert.match(state.llm_config.banner, /目录暂不可用/)
  assert.equal(readSavedConfig(env.dir), null, 'no config file may be written')
})

test('P1-1 D-6=B: a missing preset model writes nothing (no silent replacement)', async () => {
  const env = makeIntegration({
    fakeLlm: makeFakeLlm({ mode: 'no_trade' }),
    llmCatalog: makeFakeCatalog({ models: [CONFIG_MODELS[1]] }), // dfmodel missing
  })
  const state = await env.integration.state()
  assert.equal(state.llm_config.revision, null)
  assert.match(state.llm_config.banner, /预设模型不可用（缺 dfmodel）/)
  assert.equal(readSavedConfig(env.dir), null)
})

test('P1-1 P-05=A: an unsupported effort is rejected at save time (no silent mapping)', async () => {
  const env = makeIntegration({ fakeLlm: makeFakeLlm({ mode: 'no_trade' }), llmCatalog: makeFakeCatalog() })
  await env.integration.state() // fixates revision 1
  const before = readSavedConfig(env.dir)
  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  try {
    const candidate = JSON.parse(JSON.stringify(before))
    delete candidate.revision
    delete candidate.updatedAt
    candidate.roleRoutes.decision = { provider: 'qoder', model: 'qmodel_latest', effort: 'max' }
    const response = await fetch(`${base}/trading-workbench/api/llm/config`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ config: candidate }),
    })
    const body = await response.json()
    assert.equal(response.status, 422)
    assert.equal(body.ok, false)
    assert.equal(body.error.code, 'ROUTE_UNAVAILABLE')
    const errors = body.error.details.errors
    assert.ok(errors.some((item) => item.code === 'UNSUPPORTED_REASONING_EFFORT' && item.role === 'decision'), JSON.stringify(errors))
    assert.deepEqual(readSavedConfig(env.dir), before, 'a rejected save must not touch the file')
  } finally {
    await new Promise((resolve) => server.close(resolve))
    setLoadedConfigRevision(null)
  }
})

test('P1-1: a saved config bumps the revision and applies from the next round', async () => {
  const env = makeIntegration({ fakeLlm: makeFakeLlm({ mode: 'no_trade' }), llmCatalog: makeFakeCatalog() })
  await env.integration.state()
  const before = readSavedConfig(env.dir)
  const candidate = JSON.parse(JSON.stringify(before))
  delete candidate.revision
  delete candidate.updatedAt
  candidate.preset = 'saver'
  candidate.roleRoutes.decision = { provider: 'qoder', model: 'dfmodel', effort: 'low' }
  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  try {
    const response = await fetch(`${base}/trading-workbench/api/llm/config`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ config: candidate }),
    })
    const body = await response.json()
    assert.equal(response.status, 200)
    assert.equal(body.config.revision, 2)
    assert.equal(body.effectiveFrom, 'nextRound')
    assert.equal(readSavedConfig(env.dir).roleRoutes.decision.effort, 'low')
  } finally {
    await new Promise((resolve) => server.close(resolve))
    setLoadedConfigRevision(null)
  }
})

test('P1-1: the round freezes per-role routes and lands routeSnapshot + effectiveEffort', async () => {
  const fake = makeFakeLlm()
  const env = makeIntegration({ fakeLlm: fake, service: makeFakeService({ credentials: true }), llmCatalog: makeFakeCatalog() })
  const result = await env.integration.run()
  assert.ok(result.round.roundId)
  const seen = Object.fromEntries(fake.seen.map((entry) => [entry.roleKey, entry]))
  for (const role of ['analysis', 'red_opening', 'blue_opening', 'decision', 'risk', 'execution_entry']) {
    assert.equal(seen[role].provider, 'qoder', `${role} provider`)
    assert.equal(seen[role].model, 'dfmodel', `${role} model`)
  }
  assert.equal(seen.analysis.effort, 'high')
  assert.equal(seen.decision.effort, 'max')
  assert.equal(seen.risk.effort, 'max')
  assert.equal(seen.execution_entry.effort, 'low')
  // R1/R2 symmetry (P-01=A).
  assert.equal(seen.red_opening.effort, seen.blue_opening.effort)

  const runCall = env.service.calls.find((call) => call.path.endsWith('/ai-runs'))
  const snapshot = runCall.body.config.routeSnapshot
  assert.equal(snapshot.configRevision, 1)
  assert.equal(snapshot.default.provider, 'qoder')
  assert.match(snapshot.catalogFingerprint, /^sha256:[0-9a-f]{64}$/)
  assert.equal(Object.keys(snapshot.roles).length, 10)
  assert.equal(snapshot.roles.decision.effectiveEffort, 'max')
  assert.equal(snapshot.roles.analysis.effortSource, 'roleRoutes')
  assert.equal(snapshot.debateAsymmetric, false)

  const callRows = env.service.calls.filter((call) => call.path.includes('/calls/'))
  const byRole = Object.fromEntries(callRows.map((call) => [call.body.role, call.body]))
  assert.equal(byRole.analysis.reasoning_effort, 'high')
  assert.equal(byRole.decision.reasoning_effort, 'max')
  assert.equal(byRole.execution.reasoning_effort, 'low')
  assert.equal(byRole.analysis.provider, 'qoder')
  assert.equal(byRole.analysis.model, 'dfmodel')
  setLoadedConfigRevision(null)
})

test('P1-1 P-05=A: a freeze-time unsupported effort is ROUTE_UNAVAILABLE with zero calls', async () => {
  const catalog = makeFakeCatalog()
  const fake = makeFakeLlm()
  const env = makeIntegration({ fakeLlm: fake, service: makeFakeService({ credentials: true }), llmCatalog: catalog })
  await env.integration.state() // fixate revision 1 (balanced: decision=max on dfmodel)
  catalog.state.models = catalog.state.models.map((entry) => (
    entry.id === 'dfmodel' ? { ...entry, efforts: ['off', 'low', 'high'] } : entry
  ))
  const result = await env.integration.run()
  assert.equal(result.round.status, 'no_trade')
  assert.equal(result.round.reasonCode, 'ROUTE_UNAVAILABLE')
  assert.match(result.round.reasonDetail, /UNSUPPORTED|不支持|校验失败/)
  assert.equal(fake.seen.length, 0, 'no LLM call may start')
  assert.equal(env.service.calls.filter((call) => call.path === '/api/cycles').length, 0, 'no service cycle may be created')
  const view = toRoundView(env.integration.store.list()[0])
  assert.equal(view.chain.stop_kind, 'skipped', 'a legal termination is not a failed round')
  assert.equal(view.attribution.category, '基础设施（路由层）')
  setLoadedConfigRevision(null)
})

test('P1-1 F2: the daily review passes an explicit effort (never null)', async () => {
  const env = makeIntegration()
  const route = await env.integration.resolveReviewRoute()
  assert.deepEqual(route, { provider: 'fake', model: 'fake-1', effort: 'high', source: 'roleTable' })

  const configured = makeIntegration({ llmCatalog: makeFakeCatalog() })
  await configured.integration.state()
  const saved = await configured.integration.resolveReviewRoute()
  assert.equal(saved.provider, 'qoder')
  assert.equal(saved.model, 'dfmodel')
  assert.equal(saved.effort, 'high', 'review effort comes from the frozen role route, not the model default')
  setLoadedConfigRevision(null)
})

test('P1-1: a config is plugin-local only (no Harness default write, no service endpoint)', async () => {
  const env = makeIntegration({ fakeLlm: makeFakeLlm({ mode: 'no_trade' }), llmCatalog: makeFakeCatalog() })
  await env.integration.state()
  const saved = readSavedConfig(env.dir)
  assert.equal(saved.defaultRoute.source, 'explicit')
  assert.equal(JSON.stringify(saved).includes('agentDefaultModel'), false, 'never persists a followed global default')
  const paths = env.integration.routes().map((route) => route.path)
  assert.ok(paths.includes('/trading-workbench/api/llm/catalog'))
  assert.ok(paths.includes('/trading-workbench/api/llm/config'))
  assert.equal(env.service.calls.filter((call) => call.path.startsWith('/api/llm')).length, 0, 'config never talks to the service')
  setLoadedConfigRevision(null)
})


test('P1-1 P-01=A: asymmetric debate is rejected unless explicitly allowed', async () => {
  const env = makeIntegration({ fakeLlm: makeFakeLlm({ mode: 'no_trade' }), llmCatalog: makeFakeCatalog() })
  await env.integration.state()
  const before = readSavedConfig(env.dir)
  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}`
  try {
    const candidate = JSON.parse(JSON.stringify(before))
    delete candidate.revision
    delete candidate.updatedAt
    candidate.roleRoutes.blue_opening = { provider: 'qoder', model: 'qmodel_latest', effort: 'high' }
    const rejected = await fetch(`${base}/trading-workbench/api/llm/config`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ config: candidate }),
    })
    const rejectedBody = await rejected.json()
    assert.equal(rejected.status, 422)
    assert.ok(rejectedBody.error.details.errors.some((item) => item.code === 'E_DEBATE_ASYMMETRIC'), JSON.stringify(rejectedBody.error.details.errors))

    candidate.debateAsymmetric = true
    const accepted = await fetch(`${base}/trading-workbench/api/llm/config`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ config: candidate }),
    })
    const acceptedBody = await accepted.json()
    assert.equal(accepted.status, 200)
    assert.equal(acceptedBody.config.debateAsymmetric, true)
    assert.equal(acceptedBody.config.roleRoutes.blue_opening.model, 'qmodel_latest')
  } finally {
    await new Promise((resolve) => server.close(resolve))
    setLoadedConfigRevision(null)
  }
})


// ---- 05 P1-2: running progress (in_flight object) + post-hoc events ------
test('P1-2: /state.in_flight is a structured progress object while a round runs', async () => {
  // t30: the gate is releasable so the deliberately hung run settles at the end
  // of the case (a permanently pending run kept the whole file from exiting).
  let releaseGate = null
  const gate = new Promise((resolve) => { releaseGate = resolve })
  const hangingLlm = { stream: () => (async function* () { await gate; throw new Error('released by test') })() }
  const env = makeIntegration()
  env.integration.llm = hangingLlm
  const pending = env.integration.run()
  await new Promise((resolve) => setTimeout(resolve, 40))
  const live = env.integration.stateSync().in_flight
  assert.ok(live && typeof live === 'object', 'idle-or-object, never a bare string')
  assert.equal(live.roundId, live.round_id, 'both field names point at the same round')
  assert.equal(live.stage_key, 'analysis')
  assert.equal(live.stage_label, '分析')
  assert.equal(live.phase, 'ANALYZING')
  assert.equal(live.attempt, 1)
  assert.equal(live.max_attempts, 2)
  assert.equal(live.calls_done, 0)
  assert.equal(live.calls_budget, 14)
  assert.equal(live.deadline_ms, 340000)
  assert.deepEqual(live.roles_done, [])
  // The clock is Host-computed from the round start, so a later poll grows it.
  env.integration.now = () => NOW + 3000
  const later = env.integration.stateSync().in_flight
  assert.ok(later.elapsed_ms >= 3000, String(later.elapsed_ms))
  env.integration.abandonRun(env.integration.inFlight, 'HOST_RUN_TIMEOUT', 'test cleanup')
  assert.equal(env.integration.stateSync().in_flight, null, 'abandoned round leaves no stale stage')
  // Release the hung stream and let the run settle: no pending handle survives
  // the case, so the file exits naturally (with or without --test-force-exit).
  releaseGate()
  await pending.catch(() => {})
})

test('P1-2: ai_run_events are flushed after createAiRun, never used as progress', async () => {
  const fake = makeFakeLlm()
  const env = makeIntegration({ fakeLlm: fake, service: makeFakeService({ credentials: true }) })
  const result = await env.integration.run()
  assert.ok(result.round.roundId)
  const indexOfRun = env.service.calls.findIndex((call) => call.path.endsWith('/ai-runs') && call.method === 'POST')
  const eventCalls = env.service.calls.filter((call) => call.path.endsWith('/events'))
  assert.ok(indexOfRun >= 0, 'ai run created')
  assert.ok(eventCalls.length >= fake.seen.length, `${eventCalls.length} >= ${fake.seen.length}`)
  const firstEventIndex = env.service.calls.findIndex((call) => call.path.endsWith('/events'))
  assert.ok(firstEventIndex > indexOfRun, 'events are flushed only after createAiRun')
  assert.equal(eventCalls[0].body.event_type, 'role_start')
  assert.equal(eventCalls[0].body.role_key, 'analysis')
  assert.equal(eventCalls[0].body.phase, 'ANALYZING')
  assert.ok(eventCalls.some((call) => call.body.event_type === 'role_end' && call.body.status === 'ok'), 'role_end recorded')
  const round = env.integration.store.list()[0]
  assert.equal(round.aiEventsFlushed, eventCalls.length, 'flush count is recorded on the round')
  assert.equal(env.integration.stateSync().in_flight, null, 'idle after the round')
})

// ---- 05 P1-3: infrastructure honesty (gaps, ready budget, A6 detail) ------
test('P1-3/03 C6: the Demo-key gap is reported only when credentials are missing', async () => {
  const ready = makeIntegration({ service: makeFakeService({ credentials: true }) })
  const readyState = await ready.integration.state()
  assert.equal(readyState.gaps.some((gap) => gap.includes('No Demo API key')), false, JSON.stringify(readyState.gaps))
  const missing = makeIntegration({ service: makeFakeService({ credentials: false }) })
  const missingState = await missing.integration.state()
  assert.ok(missingState.gaps.some((gap) => gap.includes('No Demo API key')), JSON.stringify(missingState.gaps))
})

test('P1-3/03 C6: a gap detail never repeats its own code', async () => {
  assert.equal(portfolioGapText('E_SERVICE_TIMEOUT', 'E_SERVICE_TIMEOUT'), 'E_SERVICE_TIMEOUT')
  assert.equal(portfolioGapText('E_SERVICE_TIMEOUT', '  '), 'E_SERVICE_TIMEOUT')
  assert.equal(portfolioGapText('E_SERVICE_TIMEOUT', 'ready probe aborted'), 'E_SERVICE_TIMEOUT: ready probe aborted')
})

test('P1-3: the readiness probe has its own 20s budget (12s stays on data calls)', async () => {
  assert.ok(READY_TIMEOUT_MS >= 20_000, String(READY_TIMEOUT_MS))
  const client = new WorkbenchServiceClient({ baseUrl: 'http://127.0.0.1:1', token: 'token' })
  let captured = null
  client.request = (pathname, options) => {
    captured = { pathname, options }
    return Promise.resolve({ ready: true })
  }
  await client.ready()
  assert.equal(captured.pathname, '/api/ready')
  assert.equal(captured.options.timeoutMs, READY_TIMEOUT_MS)
  // Data routes keep the original 12s budget (no global slowdown).
  client.request = (pathname, options) => { captured = { pathname, options }; return Promise.resolve({}) }
  await client.status()
  assert.equal(captured.options?.timeoutMs, undefined)
})

test('P1-3/03 A6: a Host failure carries code+params, not an English headline', async () => {
  const failingLlm = {
    stream() {
      const error = new Error('socket hang up while streaming the analysis call')
      error.code = 'LLM_TRANSPORT'
      throw error
    },
  }
  const env = makeIntegration({ fakeLlm: { llm: failingLlm, seen: [] }, service: makeFakeService({ credentials: true }) })
  const result = await env.integration.run()
  assert.equal(result.round.reasonCode, 'LLM_TRANSPORT')
  assert.equal(result.round.reasonDetail, null, JSON.stringify(result.round.reasonDetail))
  assert.ok(result.round.reasonParams, JSON.stringify(result.round.reasonParams))
  assert.equal(result.round.reasonParams.role, 'analysis')
  assert.equal(result.round.reasonParams.error_code, 'LLM_TRANSPORT')
  assert.match(String(result.round.reasonRaw), /socket hang up/)
})
test('a duplicate scheduler slot is a clean skip, not a failed round', async () => {
  const fakeLlm = makeFakeLlm()
  const service = makeFakeService({ credentials: true, duplicateSlot: true })
  const env = makeIntegration({ fakeLlm, service })
  const result = await env.integration.run({ trigger: 'auto', slotId: 'auto-600000-test' })
  assert.equal(result.skipped, true)
  assert.equal(result.ok, true)
  assert.equal(result.round.status, 'skipped')
  assert.equal(result.round.reasonCode, 'OVERLAP_SKIPPED')
  assert.equal(fakeLlm.seen.length, 0, 'no LLM call may be spent on a duplicate slot')
})
// ---- M1.4c: hung provider must not wedge the workbench ---------------------

test('the run watchdog frees inFlight and persists a failed round', async () => {
  // A provider that yields nothing until the case releases it (t30: the release
  // keeps the file exitable; the watchdog is still what frees inFlight).
  let releaseHung = null
  const hungLlm = {
    stream: () => ({
      [Symbol.asyncIterator]: () => ({
        next: () => new Promise((resolve, reject) => { releaseHung = () => reject(new Error('released by test')) }),
      }),
    }),
  }
  const env = makeIntegration({ runTimeoutMs: 60 })
  env.integration.llm = hungLlm
  const pending = env.integration.run({ trigger: 'auto', slotId: 'auto-600000-hang' })
  // run() clamps the watchdog to >= 1s (a 0ms timeout would be pathological).
  await new Promise((resolve) => setTimeout(resolve, 1_700))
  assert.equal(env.integration.inFlight, null, 'watchdog must clear inFlight')
  const rounds = env.integration.store.list()
  assert.equal(rounds.length, 1)
  assert.equal(rounds[0].status, 'failed')
  assert.equal(rounds[0].reasonCode, 'HOST_RUN_TIMEOUT')
  // Release the stream so the run settles instead of lingering as a handle.
  releaseHung?.()
  await pending.catch(() => {})
})
test('a late result after the watchdog cannot reach P1/P2 or overwrite the failed round', async () => {
  let releaseHung = null
  const fakeLlm = makeFakeLlm()
  const originalStream = fakeLlm.llm.stream.bind(fakeLlm.llm)
  let first = true
  fakeLlm.llm.stream = (options) => {
    const stream = originalStream(options)
    return (async function* () {
      if (first) {
        first = false
        await new Promise((resolve) => { releaseHung = resolve })
      }
      yield* stream
    })()
  }
  const service = makeFakeService({ credentials: true })
  const env = makeIntegration({ fakeLlm, service, runTimeoutMs: 60 })
  const pending = env.integration.run({ trigger: 'auto', slotId: 'auto-600000-late-result' })
  await new Promise((resolve) => setTimeout(resolve, 1_700))

  assert.equal(env.integration.inFlight, null, 'watchdog must release the slot')
  const terminal = env.integration.store.list()[0]
  assert.equal(terminal.status, 'failed')
  assert.equal(terminal.reasonCode, 'HOST_RUN_TIMEOUT')

  releaseHung?.()
  await pending.catch(() => {})
  assert.equal(
    service.calls.filter((call) => call.path === '/api/execution/order').length,
    0,
    'late output must not reach P2',
  )
  assert.equal(
    service.calls.filter((call) => call.path === '/api/risk/evaluate').length,
    0,
    'late output must not reach P1',
  )
  const after = env.integration.store.list()[0]
  assert.equal(after.status, 'failed')
  assert.equal(after.reasonCode, 'HOST_RUN_TIMEOUT', 'late result must not overwrite the timeout record')
  assert.equal(env.integration.inFlightStatus, null)
})
test('A05: the durable auto budget survives the round archive keeping every round', () => {
  const env = makeIntegration()
  for (let i = 0; i < 96; i += 1) {
    env.integration.recordAutoRunStart(NOW - i * 1_000, `auto-budget-${i}`)
    env.integration.store.upsert({
      roundId: `auto-store-${i}`,
      trigger: 'auto',
      status: 'done',
      startedAt: new Date(NOW - i * 1_000).toISOString(),
    })
  }
  for (let i = 0; i < 5; i += 1) {
    env.integration.store.upsert({
      roundId: `manual-budget-${i}`,
      trigger: 'manual',
      status: 'done',
      startedAt: new Date(NOW).toISOString(),
    })
  }
  const scheduler = createAutoScheduler({ integration: env.integration, now: () => NOW })
  assert.equal(env.integration.autoRoundsLast24h(NOW), 96)
  assert.equal(scheduler.status().rounds_last_24h, 96)
  // 15 §3: the UI list is no longer capped — every round is archived — while the
  // 24h budget still comes from its own sidecar, never from the round list.
  assert.equal(env.integration.store.list().length, 101, 'every round is archived')
})
test('GET /rounds answers one archived day at a time', async () => {
  const env = makeIntegration()
  const today = '2026-11-09'
  const yesterday = '2026-11-08'
  env.integration.store.upsert({
    roundId: 'cyc-day-today', trigger: 'manual', status: 'done', outcome: 'no_trade',
    startedAt: new Date(NOW).toISOString(), reasonCode: 'NO_CANDIDATE', gaps: [],
  })
  env.integration.store.upsert({
    roundId: 'cyc-day-old', trigger: 'manual', status: 'done', outcome: 'no_trade',
    startedAt: new Date(NOW - 26 * 3_600_000).toISOString(), reasonCode: 'NO_CANDIDATE', gaps: [],
  })

  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}/trading-workbench/api`
  try {
    const defaultDay = await (await fetch(`${base}/rounds`)).json()
    assert.equal(defaultDay.date, today, 'the day defaults to today')
    assert.deepEqual(defaultDay.rounds.map((row) => row.roundId), ['cyc-day-today'])

    const oldDay = await (await fetch(`${base}/rounds?date=${yesterday}`)).json()
    assert.equal(oldDay.date, yesterday)
    assert.deepEqual(oldDay.rounds.map((row) => row.roundId), ['cyc-day-old'])

    const dates = await (await fetch(`${base}/rounds/dates`)).json()
    assert.deepEqual(dates.dates, [today, yesterday])
    assert.equal(dates.today, today)

    const future = await fetch(`${base}/rounds?date=2999-01-01`)
    assert.equal(future.status, 400)
    assert.equal((await future.json()).error.code, 'E_DATE_FUTURE')

    const invalid = await fetch(`${base}/rounds?date=yesterday`)
    assert.equal(invalid.status, 400)
    assert.equal((await invalid.json()).error.code, 'E_DATE_INVALID')

    // 15 §4: /state carries today's partition plus the dates that exist.
    const state = await (await fetch(`${base}/state`)).json()
    assert.equal(state.rounds_date, today)
    assert.deepEqual(state.available_dates, [today, yesterday])
    assert.deepEqual(state.rounds.map((row) => row.roundId).filter((id) => id.startsWith('cyc-day')), ['cyc-day-today'])
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})
test('POST /rounds-like commands: /service/start only reports success after /health', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-start-'))
  const python = path.join(dir, 'venv', 'python.exe')
  const entry = path.join(dir, 'run_service.py')
  fs.mkdirSync(path.dirname(python), { recursive: true })
  fs.writeFileSync(python, '', 'utf8')
  fs.writeFileSync(entry, '', 'utf8')
  const spawned = []
  const env = makeIntegration({
    serviceLauncher: { python, entry, dotenv: path.join(dir, '.env') },
    serviceStartTimeoutMs: 4_000,
    spawnImpl: (command, args, options) => {
      spawned.push({ command, args, options })
      return { pid: 4242, unref() {} }
    },
  })
  // The service fake answers /health with 200 but WITHOUT ok:true — a real
  // "not ready yet" shape. Only the injected probe below turns it into ready.
  let healthReady = false
  const baseFetch = env.integration.fetchImpl
  env.integration.fetchImpl = async (url, options) => {
    if (String(url).endsWith('/health')) {
      return healthReady
        ? { ok: true, status: 200, json: async () => ({ ok: true, ready: true }) }
        : { ok: true, status: 200, json: async () => ({ ok: false }) }
    }
    return baseFetch(url, options)
  }

  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}/trading-workbench/api`
  try {
    const post = () => fetch(`${base}/service/start`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ reason: 'test' }),
    })

    // 1) service never becomes ready -> honest timeout, never a "probable" ok.
    const timeoutResponse = await post()
    assert.equal(timeoutResponse.status, 504)
    const timeoutBody = await timeoutResponse.json()
    assert.equal(timeoutBody.error.code, 'E_SERVICE_START_TIMEOUT')
    assert.equal(timeoutBody.error.details.pid, 4242)
    assert.equal(spawned.length, 1, 'the launcher ran exactly once')
    assert.deepEqual(spawned[0].args.slice(-2), ['--data-dir', path.resolve(env.dir)])
    assert.equal(spawned[0].options.detached, true)

    // 2) the second attempt finds a ready service and spawns nothing.
    healthReady = true
    const alreadyResponse = await post()
    assert.equal(alreadyResponse.status, 200)
    const alreadyBody = await alreadyResponse.json()
    assert.equal(alreadyBody.ok, true)
    assert.equal(alreadyBody.already, true)
    assert.equal(spawned.length, 1, 'an already-running service is not spawned twice')

    // 3) /state and /host publish the capability facts the UI renders.
    const state = await (await fetch(`${base}/state`)).json()
    assert.deepEqual(state.service_start, { supported: true, reason: null, command: 'service/start' })
    const hostInfo = await (await fetch(`${base}/host`)).json()
    assert.equal(hostInfo.service_start.supported, true)
    assert.equal(hostInfo.service_start.python_path, python)
    assert.equal(hostInfo.service_start.entry_path, entry)
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})
function fakeSpawn({ code = 1, lines = [] } = {}) {
  return () => {
    const handlers = {}
    const child = {
      stdout: { on() {} },
      stderr: { on() {} },
      on(event, fn) { handlers[event] = fn; return child },
      kill() {},
    }
    setTimeout(() => {
      for (const line of lines) { /* captured via onLine only when real streams exist */ }
      handlers.close?.(code, null)
    }, 0)
    return child
  }
}

test('17 §1: dataDir never defaults into the package (node_modules) on a fresh install', () => {
  const empty = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-emptydata-'))
  const explicit = path.join(os.tmpdir(), 'twb-explicit-data')
  const home = path.join(os.tmpdir(), 'twb-dshhome')
  // 1) explicit config / env wins
  assert.equal(resolveDefaultDataDir({ WORKBENCH_DATA_DIR: explicit }, empty), path.resolve(explicit))
  // 2) an existing install keeps its data where it already is (no migration on upgrade)
  const seeded = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-seeded-'))
  fs.writeFileSync(path.join(seeded, '.api-token'), 'x', 'utf8')
  assert.equal(resolveDefaultDataDir({}, seeded), seeded)
  // 3) fresh install falls back to the user dir, never the package directory
  assert.equal(resolveDefaultDataDir({ DSH_HOME: home }, empty), path.join(home, 'data', 'trading-workbench'))
})

test('16 §1: readiness is honest on a machine without Python', async () => {
  const env = makeIntegration({
    serviceLauncher: { python: path.join(os.tmpdir(), 'no-such-python.exe'), entry: path.join(os.tmpdir(), 'no-such-entry.py') },
    spawnImpl: fakeSpawn({ code: 127 }),
  })
  const setup = await env.integration.serviceSetup.readiness({ force: true })
  assert.equal(setup.ready, false)
  assert.equal(setup.python.found, false)
  assert.equal(setup.venv.exists, false)
  const codes = setup.blockers.map((item) => item.code)
  assert.ok(codes.includes('PYTHON_MISSING'), codes.join(','))
  assert.ok(codes.includes('SERVICE_DOWN'), codes.join(','))
})

test('16 §1: setup stops at the first failing step instead of pretending', async () => {
  const env = makeIntegration({
    serviceLauncher: { python: path.join(os.tmpdir(), 'no-such-python.exe'), entry: path.join(os.tmpdir(), 'no-such-entry.py') },
    spawnImpl: fakeSpawn({ code: 127 }),
  })
  const { job } = env.integration.serviceSetup.start()
  await job.promise
  assert.equal(job.phase, 'failed')
  assert.equal(job.steps.find((step) => step.id === 'create-venv').status, 'failed')
  assert.equal(job.error.code, 'PYTHON_MISSING')
  assert.equal(job.steps.find((step) => step.id === 'install-deps').status, 'pending')
})

test('16 §1: the setup routes expose readiness and the job, and 404 an unknown job', async () => {
  const env = makeIntegration({
    serviceLauncher: { python: path.join(os.tmpdir(), 'no-such-python.exe'), entry: path.join(os.tmpdir(), 'no-such-entry.py') },
    spawnImpl: fakeSpawn({ code: 127 }),
  })
  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}/trading-workbench/api`
  try {
    const readiness = await (await fetch(`${base}/setup/readiness?force=1`)).json()
    assert.equal(readiness.ok, true)
    assert.equal(readiness.setup.python.found, false)
    assert.ok(Array.isArray(readiness.setup.blockers))

    const started = await fetch(`${base}/service/setup`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({}),
    })
    assert.equal(started.status, 200)
    const body = await started.json()
    assert.equal(body.ok, true)
    assert.match(body.job_id, /^setup-/)
    assert.ok(Array.isArray(body.job.steps))

    const status = await (await fetch(`${base}/service/setup/${body.job_id}`)).json()
    assert.equal(status.ok, true)
    assert.equal(status.job.id, body.job_id)

    const missing = await fetch(`${base}/service/setup/setup-nope`)
    assert.equal(missing.status, 404)
    assert.equal((await missing.json()).error.code, 'E_SETUP_JOB_UNKNOWN')
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})
test('POST /service/start refuses when the launcher cannot be found', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-nostart-'))
  const env = makeIntegration({
    serviceLauncher: { python: path.join(dir, 'nope', 'python.exe'), entry: path.join(dir, 'nope', 'run_service.py') },
    spawnImpl: () => { throw new Error('must not spawn') },
  })
  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const base = `http://127.0.0.1:${server.address().port}/trading-workbench/api`
  try {
    const response = await fetch(`${base}/service/start`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({}),
    })
    assert.equal(response.status, 503)
    const body = await response.json()
    assert.equal(body.error.code, 'E_SERVICE_LAUNCHER_MISSING')
    assert.match(body.error.details.python_path, /nope/)
    assert.equal(body.error.details.supported, false)
  } finally {
    await new Promise((resolve) => server.close(resolve))
  }
})
test('a round left "running" by a previous Host process is repaired on load', async () => {
  const env = makeIntegration()
  // Simulate a Host that died mid-round: a persisted record still says running.
  env.integration.store.upsert({
    roundId: 'cyc-orphan', cycleId: 'cyc-orphan', trigger: 'auto', symbol: 'BTCUSDT',
    status: 'running', outcome: null, reasonCode: null, headline: '运行中',
    startedAt: new Date(NOW - 600_000).toISOString(), finishedAt: null, durationMs: null,
    gaps: [],
  })
  const state = env.integration.stateSync()
  const round = state.rounds.find((item) => item.roundId === 'cyc-orphan')
  assert.equal(round.status, 'failed')
  assert.equal(round.reasonCode, 'HOST_STALE_RUN')
  assert.equal(state.in_flight, null)
})
test('preflight skips while a position is open instead of burning a chain on R02', async () => {
  const service = makeFakeService({
    credentials: true,
    statusPositions: [{ symbol: 'BTCUSDT', position_side: 'SHORT', qty: '0.075', entry_price: '73907.3' }],
  })
  const env = makeIntegration({ service })
  const verdict = await env.integration.preflight()
  assert.equal(verdict.ok, false)
  assert.equal(verdict.code, 'POSITION_OPEN')
  assert.equal(verdict.open_positions, 1)
})

test('portfolio orders expose service ledger quantities without inventing close-position size', async () => {
  const orders = [
    { exchange_order_id: 'entry', symbol: 'BTCUSDT', qty_requested: '0.075', qty_filled: '0.050' },
    { exchange_order_id: 'protect', symbol: 'BTCUSDT', qty_requested: null, qty_filled: null, close_position: 1 },
  ]
  const env = makeIntegration({ service: makeFakeService({ orders }) })
  const state = await env.integration.state({ force: true })
  assert.equal(state.orders[0].qty, '0.075')
  assert.equal(state.orders[1].qty, null)
})

test('operator control routes enforce origin/confirmation, preserve request IDs and hard-risk rejection', async () => {
  const env = makeIntegration()
  const server = http.createServer((req, res) => env.integration.handle(req, res))
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const url = `http://127.0.0.1:${server.address().port}/trading-workbench/api`
  const post = (body, headers = {}) => fetch(`${url}/control`, { method: 'POST', headers: { 'content-type': 'application/json', ...headers }, body: JSON.stringify(body) })
  try {
    assert.equal((await fetch(`${url}/control`)).status, 405)
    assert.equal((await post({ state: 'emergency', confirm: 'yes', reason: 'test' })).status, 422)
    assert.equal((await post({ state: 'pause', confirm: 'PAUSE', reason: 'test' }, { origin: 'https://evil.example' })).status, 403)
    assert.equal(env.service.calls.filter((call) => call.path === '/api/control').length, 0)
    const result = await post({ state: 'pause', confirm: 'PAUSE', reason: 'manual pause', request_id: 'req-confirmed-control' })
    assert.equal(result.status, 200)
    assert.equal((await result.json()).requestId, 'req-confirmed-control')
    assert.equal(env.service.calls.find((call) => call.path === '/api/control').body.request_id, 'req-confirmed-control')
    const locked = await post({ state: 'normal', confirm: 'NORMAL', reason: 'manual resume' })
    assert.equal(locked.status, 423)
    assert.equal((await locked.json()).error.code, 'E_CONTROL_LOCKED')
    const history = await (await fetch(`${url}/operations`)).json()
    assert.equal(history.operations[0].request_id, 'req-history-test')
    assert.equal(JSON.stringify(history).includes(SERVICE_TOKEN), false)
  } finally { await new Promise((resolve) => server.close(resolve)) }
})

test('preflight ignores a flat position row', async () => {
  const service = makeFakeService({
    credentials: true,
    statusPositions: [{ symbol: 'BTCUSDT', position_side: 'SHORT', qty: '0' }],
  })
  const env = makeIntegration({ service })
  const verdict = await env.integration.preflight()
  assert.equal(verdict.code === 'POSITION_OPEN', false)
})

function mountedStrategyService({ eligible = true, authorized = true, configFail = false } = {}) {
  const base = makeFakeService({ credentials: true })
  const oldFetch = base.fetchImpl
  const strategy = { strategy_id: 'test-mounted', name: '阈值测试', current_version: 1, generation: 1, enabled: true,
    confirmed_version: { validation: { execution_ready: true }, rules: { symbols: ['BTCUSDT'], timeframes: ['15m'], entry: { kind: 'mark_threshold', side: 'long', operator: 'gt', value: 68000 }, exit: { stop_loss_pct: 0.02, take_profit_pct: 0.04 } } } }
  base.fetchImpl = async (url, options = {}) => {
    const u = new URL(url)
    if (u.pathname === '/api/ready') return jsonResponse(200, { ready: true, service_ready: true, capabilities: { strategies: true } })
    if (u.pathname === '/api/strategies/config') return configFail ? jsonResponse(503, { error: { code: 'STRATEGY_CONFIG_DOWN' } }) : jsonResponse(200, { config: { mode: 'mounted', revision: 2 } })
    // v3.1（docs/18 §2 S9）：Host 只在触发引擎关闭时保留 10 分钟槽旧行为，候选一律由
    // 服务端扫描给出（/evaluate-candidates 是 /api/rules/scan 的兼容别名）；404 回退已删除。
    if (u.pathname === '/api/strategies/evaluate-candidates') {
      const ready = authorized && eligible
      return jsonResponse(200, {
        mode: 'mounted',
        revision: 2,
        candidates: ready ? [strategy] : [],
        skipped: ready ? [] : [{ strategy_id: strategy.strategy_id, name: strategy.name, reason_code: 'SIGNAL_FALSE' }],
        triggered: ready ? [strategy] : [],
        waiting: [],
        transitioned: [],
      })
    }
    if (u.pathname === '/api/market/marks') return jsonResponse(200, { prices: { BTCUSDT: { mark: eligible ? 68240 : 67000, as_of_ms: NOW, stale: false } } })
    return oldFetch(url, options)
  }
  return base
}

test('mounted run freezes strategy identity into both P1/P2 and archived view', async () => {
  const env = makeIntegration({ service: mountedStrategyService() })
  const result = await env.integration.run()
  const evaluate = env.service.calls.find(c => c.path === '/api/risk/evaluate')
  const submit = env.service.calls.find(c => c.path === '/api/execution/order')
  assert.ok(evaluate, JSON.stringify(result.round))
  assert.ok(submit, JSON.stringify(result.round))
  assert.equal(evaluate.body.strategy_context.strategy_id, 'test-mounted')
  assert.equal(evaluate.body.strategy_context.mode_revision, 2)
  assert.match(evaluate.body.strategy_context.signal_key, /^15m:\d+$/)
  assert.deepEqual(submit.body.strategy_context, evaluate.body.strategy_context)
  assert.equal(result.round.strategy.strategy_id, 'test-mounted')
  assert.ok(result.round.strategy_selection_reason)
})

test('mounted mode with no enabled or eligible strategy never falls back to native AI', async () => {
  for (const options of [{ authorized: false }, { eligible: false }]) {
    const env = makeIntegration({ service: mountedStrategyService(options) })
    const result = await env.integration.run()
    assert.equal(result.round.reasonCode, 'NO_ELIGIBLE_STRATEGY')
    assert.equal(env.fakeLlm.seen.length, 0)
    assert.equal(env.service.calls.filter(c => ['/api/risk/evaluate','/api/execution/order'].includes(c.path)).length, 0)
  }
})

test('strategy-capable service config failure blocks instead of native fallback', async () => {
  const env = makeIntegration({ service: mountedStrategyService({ configFail: true }) })
  await assert.rejects(env.integration.run())
  assert.equal(env.fakeLlm.seen.length, 0)
  assert.equal(env.service.calls.filter(c => c.path === '/api/execution/order').length, 0)
})

// ---- Phase-2 & P0/P1 Dual-Engine Strategy Suite ---------------------------

test('P0-1: plugin identity is dsh-trading-workbench 0.2.1 and launcher root points to package root', () => {
  const pkgPath = fileURLToPath(new URL('../package.json', import.meta.url))
  const expectedRoot = path.dirname(pkgPath)
  const pkg = JSON.parse(fs.readFileSync(pkgPath, 'utf8'))
  assert.equal(pkg.version, '0.2.1')
  assert.equal(PLUGIN_ID, 'dsh-trading-workbench')
  assert.equal(PLUGIN_VERSION, '0.2.1')

  const env = makeIntegration()
  const paths = env.integration.serviceLauncherPaths()
  assert.equal(paths.root, expectedRoot)
  assert.equal(paths.entry, path.join(expectedRoot, 'service', 'run_service.py'))
  assert.equal(env.integration.hostInfo().plugin, 'dsh-trading-workbench')
  assert.equal(env.integration.listRounds().plugin, 'dsh-trading-workbench')
})

test('D-1: a manual run with an open position stops before any LLM call and records POSITION_OPEN', async () => {
  // docs/16 §5.1 / docs/19 §4 P1：自动调度早就在 scheduler.preflight 里做过持仓预检，
  // 手动「运行一轮」以前会跑完整条 6–8 次 LLM 的开仓链，最后在 P1 以
  // E_RISK_SYMBOL_POSITION 失败（约 4–5 万 token、2 分钟）。现在 0 次 LLM 调用。
  const base = makeFakeService({
    credentials: true,
    statusPositions: [{ symbol: 'BTCUSDT', position_side: 'LONG', qty: '0.05', entry_price: '67500' }],
  })
  const oldFetch = base.fetchImpl
  base.fetchImpl = async (url, options = {}) => {
    const u = new URL(url)
    if (u.pathname === '/api/ready') return jsonResponse(200, { ready: true, service_ready: true, capabilities: { strategies: true } })
    if (u.pathname === '/api/strategies/config') return jsonResponse(200, { config: { mode: 'mounted', revision: 5 } })
    if (u.pathname === '/api/strategies/evaluate-candidates') {
      throw new Error('must not evaluate new-entry candidates when an open position exists')
    }
    if (u.pathname === '/api/strategies/authorized') {
      throw new Error('must not select new-entry strategies when an open position exists')
    }
    return oldFetch(url, options)
  }
  const fakeLlm = makeFakeLlm({ mode: 'no_trade' })
  const env = makeIntegration({ service: base, fakeLlm })
  const result = await env.integration.run()

  assert.equal(result.ok, true)
  assert.equal(result.round.outcome, 'no_trade')
  assert.equal(result.round.reasonCode, 'POSITION_OPEN')
  assert.equal(result.round.status, 'no_trade')
  assert.equal(result.round.aiCalls.length, 0)
  assert.equal(fakeLlm.seen.length, 0, 'D-1: 有持仓时必须 0 次 LLM 调用')
  assert.equal(result.round.symbol, 'BTCUSDT')
  assert.equal(result.round.strategy, null)
  assert.equal(env.service.calls.some((c) => c.path === '/api/cycles'), false, 'D-1 不创建 cycle')
  assert.equal(env.service.calls.some((c) => c.path === '/api/risk/evaluate'), false)
  assert.ok(result.round.gaps.some((gap) => gap.includes('D-1 预检')))
})

function makePhase2StrategyService({
  candidates = [],
  skipped = [],
  p1Code = null,
  p1Status = null,
  strategiesList = [],
  mode = 'mounted',
} = {}) {
  const base = makeFakeService({ credentials: true, p1Code, p1Status })
  const oldFetch = base.fetchImpl
  const decisionsRecorded = []
  base.decisionsRecorded = decisionsRecorded
  base.fetchImpl = async (url, options = {}) => {
    const u = new URL(url)
    const method = options.method ?? 'GET'
    const body = options.body ? JSON.parse(options.body) : null
    if (u.pathname === '/api/ready') {
      return jsonResponse(200, { ready: true, service_ready: true, capabilities: { strategies: true } })
    }
    if (u.pathname === '/api/strategies/config' && method === 'GET') {
      return jsonResponse(200, { config: { mode, revision: 4 } })
    }
    if (u.pathname === '/api/strategies' && method === 'GET') {
      return jsonResponse(200, { strategies: strategiesList })
    }
    if (u.pathname === '/api/strategies/evaluate-candidates' && method === 'POST') {
      return jsonResponse(200, { mode, revision: 4, candidates, skipped })
    }
    if (/^\/api\/strategies\/[^/]+\/decisions$/.test(u.pathname) && method === 'POST') {
      const strategyId = decodeURIComponent(u.pathname.split('/')[3])
      decisionsRecorded.push({ strategyId, body })
      return jsonResponse(200, { ok: true, decision: body })
    }
    if (u.pathname === '/api/strategies/attribution' && method === 'GET') {
      return jsonResponse(200, { items: [{ strategy_id: 's1', win_rate: 0.6 }] })
    }
    if (/^\/api\/strategies\/[^/]+\/preview$/.test(u.pathname) && method === 'POST') {
      return jsonResponse(200, { triggered: true, indicator_facts: { rsi14: 28.5 } })
    }
    return oldFetch(url, options)
  }
  return base
}

test('Phase-2 rule_direct executes P1 -> P2 with zero LLM calls and records strategy decision', async () => {
  const directCandidate = {
    strategy_id: 'strat-direct-1',
    name: '纯规则突破直连',
    version: 2,
    current_version: 2,
    generation: 3,
    execution_mode: 'rule_direct',
    symbol: 'BTCUSDT',
    side: 'LONG',
    signal_key: '15m:1994071',
    mark: 68000,
    rules: {
      symbols: ['BTCUSDT'],
      timeframes: ['15m'],
      execution_mode: 'rule_direct',
      entry: { kind: 'candle_close', side: 'long', operator: 'gt', value: 67800 },
      exit: { stop_loss_pct: 0.02, take_profit_pct: 0.04 },
    },
  }
  const service = makePhase2StrategyService({ candidates: [directCandidate] })
  const llm = {
    stream() {
      throw new Error('rule_direct must make 0 LLM calls')
    },
  }
  const env = makeIntegration({ service, fakeLlm: { llm, seen: [] } })
  const result = await env.integration.run()

  assert.equal(result.ok, true)
  assert.equal(result.round.outcome, 'trade')
  assert.equal(result.round.status, 'done')
  assert.equal(result.round.mode, 'rule_direct')
  assert.equal(result.round.reasonCode, 'TRADE_EXECUTED')
  assert.equal(result.round.aiCalls.length, 0)
  assert.equal(result.round.chain.stop_kind, 'completed')
  assert.equal(result.round.chain.stopped_at, null)
  assert.equal(result.round.strategy.strategy_id, 'strat-direct-1')
  assert.equal(result.round.strategy.execution_mode, 'rule_direct')
  assert.equal(result.round.attribution.mapped, true)

  const p1Call = service.calls.find((c) => c.path === '/api/risk/evaluate')
  const p2Call = service.calls.find((c) => c.path === '/api/execution/order')
  assert.ok(p1Call)
  assert.ok(p2Call)
  assert.equal(p1Call.body.plan.side, 'LONG')
  assert.equal(Number(p1Call.body.plan.sl), 66640)
  assert.equal(Number(p1Call.body.plan.tp), 70720)
  assert.equal(p1Call.body.strategy_context.signal_key, '15m:1994071')
  assert.deepEqual(p2Call.body.strategy_context, p1Call.body.strategy_context)

  // v3.1（docs/18 §2 S5）：Host 不再估算仓位。plan.qty 只是「申报上限」，
  // 真实数量只来自服务端 approved_qty（risk/rules.py 的 R05/R06/R07/R12）。
  assert.equal(p1Call.body.plan.qty, DECLARED_QTY_UPPER_BOUND)
  const rawRecord = env.integration.store.find(result.round.roundId)
  assert.ok(rawRecord, 'round must be archived')
  assert.equal(rawRecord.sizing.source, 'service')
  assert.equal(rawRecord.sizing.declared_qty_upper_bound, DECLARED_QTY_UPPER_BOUND)
  assert.equal(rawRecord.sizing.approved_qty, '0.067')
  assert.equal(JSON.stringify(rawRecord.sizing).includes('5000'), false, 'no local equity fallback in the archive')
  assert.ok(rawRecord.gaps.some((gap) => gap.includes('仓位只由服务端计算')))

  assert.equal(service.decisionsRecorded.length, 1)
  assert.equal(service.decisionsRecorded[0].strategyId, 'strat-direct-1')
  assert.equal(service.decisionsRecorded[0].body.execution_mode, 'rule_direct')
  assert.equal(service.decisionsRecorded[0].body.outcome, 'executed')
})

test('Phase-2 ai_assisted records ai_vetoed decision on NO_CANDIDATE and STRATEGY_AI_CONFLICT', async () => {
  const shortCandidate = {
    strategy_id: 'strat-ai-short',
    name: 'AI辅助空头策略',
    version: 1,
    current_version: 1,
    generation: 1,
    execution_mode: 'ai_assisted',
    symbol: 'BTCUSDT',
    side: 'SHORT',
    signal_key: '1h:498517',
    mark: 68240,
    rules: {
      symbols: ['BTCUSDT'],
      timeframes: ['1h'],
      execution_mode: 'ai_assisted',
      entry: { kind: 'rsi_threshold', side: 'short', period: 14, operator: 'gt', value: 70 },
      exit: { stop_loss_pct: 0.02, take_profit_pct: 0.03 },
    },
  }

  // 1) AI conflict (decisionFixture picks LONG while strategy is SHORT)
  const conflictSvc = makePhase2StrategyService({ candidates: [shortCandidate] })
  const conflictEnv = makeIntegration({ service: conflictSvc, fakeLlm: makeFakeLlm({ mode: 'trade' }) })
  const conflictRes = await conflictEnv.integration.run()
  assert.equal(conflictRes.ok, true)
  assert.equal(conflictRes.round.outcome, 'no_trade')
  assert.equal(conflictRes.round.reasonCode, 'STRATEGY_AI_CONFLICT')
  assert.equal(conflictRes.round.strategy.execution_mode, 'ai_assisted')
  assert.equal(conflictSvc.calls.some((c) => c.path === '/api/risk/evaluate'), false)
  assert.equal(conflictSvc.decisionsRecorded.length, 1)
  assert.equal(conflictSvc.decisionsRecorded[0].body.outcome, 'ai_vetoed')
  assert.equal(conflictSvc.decisionsRecorded[0].body.reason_code, 'STRATEGY_AI_CONFLICT')

  // 2) Analysis stand_aside NO_CANDIDATE
  const standAsideSvc = makePhase2StrategyService({ candidates: [shortCandidate] })
  const standAsideEnv = makeIntegration({ service: standAsideSvc, fakeLlm: makeFakeLlm({ mode: 'no_trade' }) })
  const standAsideRes = await standAsideEnv.integration.run()
  assert.equal(standAsideRes.ok, true)
  assert.equal(standAsideRes.round.outcome, 'no_trade')
  assert.equal(standAsideRes.round.reasonCode, 'NO_CANDIDATE')
  assert.equal(standAsideSvc.decisionsRecorded.length, 1)
  assert.equal(standAsideSvc.decisionsRecorded[0].body.outcome, 'ai_vetoed')
  assert.equal(standAsideSvc.decisionsRecorded[0].body.reason_code, 'NO_CANDIDATE')
})

test('P1-6: strategy business rejections at P1 close as no_trade instead of failed, and skipped reasons are summarized', async () => {
  const longCandidate = {
    strategy_id: 'strat-ai-long',
    name: '多头策略',
    version: 1,
    current_version: 1,
    generation: 1,
    execution_mode: 'ai_assisted',
    symbol: 'BTCUSDT',
    side: 'LONG',
    signal_key: '15m:1994071',
    mark: 68240,
    rules: {
      symbols: ['BTCUSDT'],
      timeframes: ['15m'],
      entry: { kind: 'mark_threshold', side: 'long', operator: 'gt', value: 68000 },
      exit: { stop_loss_pct: 0.02, take_profit_pct: 0.04 },
    },
  }
  for (const rejectCode of ['E_STRATEGY_SIGNAL_FALSE', 'E_STRATEGY_PLAN_MISMATCH', 'E_STRATEGY_SIGNAL_DUPLICATE']) {
    const svc = makePhase2StrategyService({ candidates: [longCandidate], p1Status: 422, p1Code: rejectCode })
    const env = makeIntegration({ service: svc })
    const res = await env.integration.run()
    assert.equal(res.ok, true, `${rejectCode} must not crash round`)
    assert.equal(res.round.status, 'no_trade')
    assert.equal(res.round.outcome, 'no_trade')
    assert.equal(res.round.reasonCode, rejectCode)
    assert.notEqual(res.round.chain.stop_kind, 'failed')
  }

  // Informative skip summary when candidates=[] and skipped carries reasons
  const skippedSvc = makePhase2StrategyService({
    candidates: [],
    skipped: [
      { strategy_id: 's1', name: 'EMA金叉', reason_code: 'SIGNAL_ALREADY_USED' },
      { strategy_id: 's2', name: '资金费反转', reason_code: 'CIRCUIT_BREAKER_TRIPPED' },
    ],
  })
  const skippedEnv = makeIntegration({ service: skippedSvc })
  const skippedRes = await skippedEnv.integration.run()
  assert.equal(skippedRes.ok, true)
  assert.equal(skippedRes.round.reasonCode, 'NO_ELIGIBLE_STRATEGY')
  assert.match(skippedRes.round.strategy_selection_reason, /EMA金叉（本周期信号已消费）/)
  assert.match(skippedRes.round.strategy_selection_reason, /资金费反转（策略熔断已触发）/)
})

test('Phase-2 /state exposes strategy_summary and /strategies routes allow attribution, preview, decisions and dynamic analyze route', async () => {
  const strategiesList = [
    { strategy_id: 's1', name: '趋势一号', enabled: true, circuit_state: 'ok' },
    { strategy_id: 's2', name: '均值二号', enabled: false, circuit: { state: 'tripped' } },
  ]
  const service = makePhase2StrategyService({ strategiesList, mode: 'mounted' })
  const oldFetch = service.fetchImpl
  service.fetchImpl = async (url, options = {}) => {
    const u = new URL(url)
    const method = options.method ?? 'GET'
    if (u.pathname === '/api/strategies/s1' && method === 'GET') {
      return jsonResponse(200, { strategy: { draft: { revision: 1, source_text: 'BTC突破做多', answers: {}, questions: [] } } })
    }
    if (u.pathname === '/api/strategies/s1/draft' && method === 'PUT') {
      return jsonResponse(200, { strategy: { draft: JSON.parse(options.body) } })
    }
    return oldFetch(url, options)
  }

  const seenAnalyzeRoutes = []
  let activeModel = 'dynamic-model-v2'
  const ctx = {
    agentDefaultModel: { currentSelection: () => ({ provider: 'dsh-live', model: activeModel }) },
    llm: {
      async *stream(opts) {
        seenAnalyzeRoutes.push({ provider: opts.provider, model: opts.model })
        yield {
          type: 'text-delta',
          text: JSON.stringify({ summary: '已解析', rules: { symbols: ['BTCUSDT'] }, questions: [], suggestions: [], sources: {} }),
        }
        yield { type: 'finish', reason: { kind: 'stop' } }
      },
    },
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-p2-http-'))
  const tokenFile = path.join(dir, '.api-token')
  fs.writeFileSync(tokenFile, `${SERVICE_TOKEN}\n`, 'utf8')
  const integration = createIntegration({
    ctx,
    serviceBaseUrl: 'http://127.0.0.1:8791',
    dataDir: dir,
    tokenFile,
    fetchImpl: service.fetchImpl,
    now: () => NOW,
  })

  const state = await integration.state({ force: true })
  assert.deepEqual(state.strategy_summary, {
    supported: true,
    mode: 'mounted',
    revision: 4,
    total_count: 2,
    enabled_count: 1,
    tripped_count: 1,
    tripped_names: ['均值二号'],
  })

  const callRoute = async (method, urlPath, body = null) => {
    const raw = body !== null ? Buffer.from(JSON.stringify(body), 'utf8') : null
    const req = {
      method,
      url: urlPath,
      socket: { remoteAddress: '127.0.0.1' },
      headers: { host: '127.0.0.1:8791', ...(raw ? { 'content-type': 'application/json' } : {}) },
      on(event, handler) {
        if (event === 'data' && raw) handler(raw)
        if (event === 'end') handler()
      },
      resume() {},
    }
    const res = demoSetupRes()
    await integration.handle(req, res)
    return res.rec
  }

  // GET /strategies/attribution
  const attrRes = await callRoute('GET', '/trading-workbench/api/strategies/attribution?range_s=86400')
  assert.equal(attrRes.status, 200)
  assert.equal(attrRes.body.items[0].strategy_id, 's1')

  // POST /strategies/:id/preview with >100KB payload (120KB <= 150KB limit)
  const largePad = 'x'.repeat(115_000)
  const previewRes = await callRoute('POST', '/trading-workbench/api/strategies/s1/preview', { note: largePad })
  assert.equal(previewRes.status, 200)
  assert.equal(previewRes.body.triggered, true)

  // POST /strategies/:id/analyze missing revision -> 422
  const missingRev = await callRoute('POST', '/trading-workbench/api/strategies/s1/analyze', {})
  assert.equal(missingRev.status, 422)

  // POST /strategies/:id/analyze uses live DSH selection dynamically
  activeModel = 'dynamic-model-v3'
  const analyzeRes = await callRoute('POST', '/trading-workbench/api/strategies/s1/analyze', { revision: 1 })
  assert.equal(analyzeRes.status, 200)
  assert.deepEqual(seenAnalyzeRoutes[0], { provider: 'dsh-live', model: 'dynamic-model-v3' })
})

test('Venue switch API runs preflight, blocks on open positions or missing Live credentials, and switches Demo <-> Live cleanly', async () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-venue-switch-'))
  const tokenFile = path.join(dir, '.api-token')
  fs.writeFileSync(tokenFile, `${SERVICE_TOKEN}\n`, 'utf8')
  const fakePython = path.join(dir, 'python-fake')
  const fakeEntry = path.join(dir, 'run_service.py')
  fs.writeFileSync(fakePython, 'echo ok\n', 'utf8')
  fs.writeFileSync(fakeEntry, 'print("ok")\n', 'utf8')

  let activeVenue = 'demo'
  let serviceOnline = true
  let openPositions = [{ symbol: 'BTCUSDT', qty: '0.02' }]
  const spawnedCalls = []

  const fetchImpl = async (url, options = {}) => {
    const u = new URL(url)
    if (u.pathname === '/health') {
      if (!serviceOnline) throw new Error('ECONNREFUSED')
      return jsonResponse(200, { ok: true, ready: true, venue: activeVenue, pid: 99999 })
    }
    if (u.pathname === '/api/positions') {
      return jsonResponse(200, { positions: openPositions })
    }
    if (u.pathname === '/api/orders') {
      return jsonResponse(200, { orders: [] })
    }
    if (u.pathname === '/api/status') {
      return jsonResponse(200, { positions: openPositions, outbox: { pending_count: 0 } })
    }
    if (u.pathname === '/api/control') {
      return jsonResponse(200, { control: { state: 'pause', reason: 'test' } })
    }
    return jsonResponse(200, { ok: true })
  }

  const integration = createIntegration({
    serviceBaseUrl: 'http://127.0.0.1:8791',
    dataDir: dir,
    tokenFile,
    fetchImpl,
    serviceLauncher: { python: fakePython, entry: fakeEntry, dotenv: path.join(dir, 'demo.env') },
    spawnImpl: (cmd, args, opts) => {
      spawnedCalls.push({ cmd, args, env: opts.env })
      activeVenue = opts.env.WORKBENCH_VENUE
      serviceOnline = true
      return { pid: 42424, unref() {} }
    },
    serviceStartTimeoutMs: 5000,
    now: () => NOW,
  })
  integration.stopRunningServiceProcess = async () => {
    serviceOnline = false
    return true
  }

  const callRoute = async (method, urlPath, body = null) => {
    const raw = body !== null ? Buffer.from(JSON.stringify(body), 'utf8') : null
    const req = {
      method,
      url: urlPath,
      socket: { remoteAddress: '127.0.0.1' },
      headers: { host: '127.0.0.1:8791', ...(raw ? { 'content-type': 'application/json' } : {}) },
      on(event, handler) {
        if (event === 'data' && raw) handler(raw)
        if (event === 'end') handler()
      },
      resume() {},
    }
    const res = demoSetupRes()
    await integration.handle(req, res)
    return res.rec
  }

  // 1) Preflight with open position + missing Live credentials reports both blockers
  const pfBlocked = await callRoute('GET', '/trading-workbench/api/venue/switch?target=live')
  assert.equal(pfBlocked.status, 200)
  assert.equal(pfBlocked.body.can_switch, false)
  assert.ok(pfBlocked.body.blockers.includes('OPEN_POSITIONS'))
  assert.ok(pfBlocked.body.blockers.includes('LIVE_CREDENTIALS_MISSING'))

  // 2) Attempting POST while blocked returns 409 E_VENUE_SWITCH_BLOCKED
  const blockedPost = await callRoute('POST', '/trading-workbench/api/venue/switch', {
    target: 'live',
    confirm: 'SWITCH_TO_LIVE',
  })
  assert.equal(blockedPost.status, 409)
  assert.equal(blockedPost.body.error.code, 'E_VENUE_SWITCH_BLOCKED')

  // 3) Save Live credentials and clear open positions -> switch to Live succeeds
  openPositions = []
  const saveLive = await callRoute('POST', '/trading-workbench/api/live-setup', {
    apiKey: 'live-api-key-123456',
    apiSecret: 'live-api-secret-654321',
  })
  assert.equal(saveLive.status, 200)

  const switchLive = await callRoute('POST', '/trading-workbench/api/venue/switch', {
    target: 'live',
    confirm: 'SWITCH_TO_LIVE',
  })
  assert.equal(switchLive.status, 200)
  assert.equal(switchLive.body.ok, true)
  assert.equal(switchLive.body.switched, true)
  assert.equal(switchLive.body.venue, 'live')
  assert.equal(spawnedCalls.length, 1)
  assert.equal(spawnedCalls[0].env.WORKBENCH_VENUE, 'live')
  assert.ok(spawnedCalls[0].args.includes(path.join(dir, 'live')))
  assert.equal(fs.existsSync(path.join(dir, 'live', '.api-token')), true)
  assert.equal(integration.readPreferredVenue(), 'live')

  // 4) Switch back to Demo succeeds with isolated root dataDir
  const switchDemo = await callRoute('POST', '/trading-workbench/api/venue/switch', {
    target: 'demo',
    confirm: 'SWITCH_TO_DEMO',
  })
  assert.equal(switchDemo.status, 200)
  assert.equal(switchDemo.body.venue, 'demo')
  assert.equal(spawnedCalls.length, 2)
  assert.equal(spawnedCalls[1].env.WORKBENCH_VENUE, 'demo')
  assert.equal(integration.readPreferredVenue(), 'demo')
})

