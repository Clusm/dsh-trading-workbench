/**
 * M1.3c 回归：会话内 LLM + AI 审计落库 + 测试模式禁止下单。
 *
 * Run: node trading-workbench/plugin/test/audit-persistence.test.mjs
 * 纯离线 fake service，不碰真实服务、不碰真实账本、不发订单。
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { createIntegration } from '../host/integration.js'
import { SESSION_LLM_TRANSPORT } from '../host/session-llm.js'

const NOW = Date.parse('2026-11-09T05:30:00Z')
const TOKEN = 'test-token'
const AI_RUN_ID = 'run-20261109T053000Z-abc123-r1'

function jsonResponse(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  }
}

function serviceEvidenceFixture(cycleId) {
  const snapshotId = `ev-${String(cycleId).slice(-6)}`
  const producedAt = new Date(NOW).toISOString()
  return {
    cycle_id: cycleId,
    evidence: {
      cycle_id: cycleId,
      snapshot_id: snapshotId,
      sha256: `sha256:audit-${String(cycleId).slice(-6)}`,
      path: `evidence/2026-11-09/${cycleId}/evidence.json`,
      bytes: 1000,
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

function makeService({ audit = true } = {}) {
  const calls = []
  const fetchImpl = async (url, options = {}) => {
    const u = new URL(url)
    const body = options.body ? JSON.parse(options.body) : null
    const method = options.method ?? 'GET'
    calls.push({ path: u.pathname, method, body })

    if (u.pathname === '/api/ready') return jsonResponse(200, { ready: true, service_ready: true, reasons: [], auth: { mode: 'bearer' } })
    if (u.pathname === '/api/cycles' && method === 'POST') {
      return jsonResponse(200, serviceEvidenceFixture(body?.cycle_id ?? 'cyc-test'))
    }
    if (u.pathname.startsWith('/api/cycles/') && u.pathname.endsWith('/finalize')) {
      return jsonResponse(200, { cycle: { cycle_id: 'cyc-test', state: body?.state }, idempotent: false })
    }
    if (u.pathname === '/api/risk/envelope') {
      return jsonResponse(200, {
        demo_only: true,
        ledger_id: 'ledger-test',
        limits: {
          per_trade: {
            max_risk_pct: 0.01, strong_max_risk_pct: 0.02, strong_min_confidence: 0.65,
            strong_min_rr: 2.0, max_leverage: 10, strong_max_leverage: 15,
            max_notional_multiple: 2, min_risk_reward: 1.2,
            sl_min_distance_pct: 0.002, sl_max_distance_pct: 0.05,
          },
        },
        control: { state: 'normal' },
        risk_state: {},
        account: { credentials_configured: true },
        new_entry_allowed: true,
        blocking_reasons: [],
      })
    }
    if (u.pathname === '/api/status') {
      return jsonResponse(200, { credentials_configured: true, equity: { session: '10000', available: true }, positions: [] })
    }
    if (u.pathname === '/api/market/health') return jsonResponse(200, { verdict: 'GOOD', score: 100, issues: [] })
    if (u.pathname === '/api/market/prices') {
      return jsonResponse(200, {
        as_of_ms: NOW,
        prices: {
          BTCUSDT: { last: 68234.5, mark: 68240, index: 68239, bid: 68234, ask: 68235, spread_bps: 1.5, age_ms: 0, complete: true, errors: [] },
        },
      })
    }
    if (audit && /^\/api\/cycles\/[^/]+\/ai-runs$/.test(u.pathname) && method === 'POST') {
      return jsonResponse(200, { ai_run: { ai_run_id: AI_RUN_ID, cycle_id: 'cyc-test', state: 'RUNNING' }, idempotent: false })
    }
    if (audit && /^\/api\/ai-runs\/[^/]+\/calls\//.test(u.pathname) && method === 'PUT') {
      return jsonResponse(200, { call: { call_id: 'c1' }, idempotent: false })
    }
    if (audit && /^\/api\/ai-runs\/[^/]+\/events$/.test(u.pathname) && method === 'POST') {
      return jsonResponse(200, { seq: calls.filter((call) => call.path.endsWith('/events')).length, ai_run_id: AI_RUN_ID })
    }
    if (audit && /^\/api\/cycles\/[^/]+\/artifacts\//.test(u.pathname) && method === 'PUT') {
      return jsonResponse(200, { output: { output_id: 'o1' }, idempotent: false })
    }
    if (u.pathname === '/api/execution/lease/acquire') {
      return jsonResponse(200, { lease_id: 'audit-lease-1', run_id: body?.run_id, expires_at_ms: NOW + 900_000, ok: true })
    }
    if (u.pathname === '/api/execution/lease/release') {
      return jsonResponse(200, { ok: true, released: true })
    }
    if (u.pathname === '/api/execution/lease/revoke_run') {
      return jsonResponse(200, { ok: true, revoked: true })
    }
    if (u.pathname === '/api/risk/evaluate') {
      return jsonResponse(200, {
        plan: body?.plan ?? null,
        verdict: { verdict: 'approved', approved_qty: '0.067', leverage: 3, violations: [] },
        approval: { token: 'approval-token-secret', approved_qty: '0.067', expires_at_ms: NOW + 120_000 },
      })
    }
    if (u.pathname === '/api/execution/order') return jsonResponse(201, { order_id: 1 })
    return jsonResponse(404, { error: { code: 'E_NOT_FOUND', message: u.pathname } })
  }
  return { fetchImpl, calls }
}

function makeIntegration({ service = makeService(), env = {} } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-audit-'))
  const tokenFile = path.join(dir, '.api-token')
  fs.writeFileSync(tokenFile, `${TOKEN}\n`, 'utf8')
  const integration = createIntegration({
    serviceBaseUrl: 'http://127.0.0.1:8791',
    dataDir: dir,
    tokenFile,
    storePath: path.join(dir, 'rounds.json'),
    fetchImpl: service.fetchImpl,
    env: { WORKBENCH_LLM_MODE: 'session_fixture', ...env },
    now: () => NOW,
    config: { debateMode: 'symmetric_two_round' },
  })
  return { integration, service, dir }
}

test('session_fixture is visible in /state and never submits an order', async () => {
  const { integration, service } = makeIntegration()
  const state = integration.stateSync()
  assert.equal(state.llm.transport, SESSION_LLM_TRANSPORT)
  assert.equal(state.llm.mode, 'session_fixture')
  assert.equal(state.llm.script, 'no_trade')
  assert.equal(state.llm.provider, 'session')
  // 05 N02 / D1: /state carries the same three identity names as /health.
  assert.equal(typeof state.build.buildId, 'string')
  assert.equal(state.build.chain_version, 'ai-chain/1')
  assert.equal(state.build.configRevision, null)

  const result = await integration.run({ symbol: 'BTCUSDT', trigger: 'manual' })
  assert.equal(result.round.status, 'no_trade')
  assert.equal(result.round.reasonCode, 'NO_CANDIDATE')
  assert.equal(service.calls.some((c) => c.path === '/api/execution/order'), false)
})

test('AI calls, artifacts and ai-run are persisted through the service endpoints', async () => {
  const { integration, service } = makeIntegration()
  const result = await integration.run({ symbol: 'BTCUSDT', trigger: 'manual' })
  const paths = service.calls.map((c) => `${c.method} ${c.path}`)
  assert.ok(paths.some((p) => p.startsWith('POST /api/cycles/') && p.endsWith('/ai-runs')), 'ai-run created')
  assert.ok(paths.some((p) => p.startsWith('PUT /api/ai-runs/') && p.includes('/calls/')), 'call audit written')
  assert.ok(paths.some((p) => p.startsWith('PUT /api/cycles/') && p.includes('/artifacts/analysis')), 'analysis artifact written')
  const callRow = service.calls.find((c) => c.path.includes('/calls/'))
  assert.equal(callRow.body.role, 'analysis')
  assert.equal(callRow.body.status, 'ok')
  assert.equal(typeof callRow.body.request_sha256, 'string')
  assert.equal(callRow.body.evidence_snapshot_id, result.round.freshness.snapshot_id)
  assert.ok(callRow.body.started_at)
  assert.ok(callRow.body.ended_at)
  // 05 N02 / D3: one payload change adds effort/temperature/slice/timestamps.
  assert.equal(callRow.body.reasoning_effort, 'high')
  assert.equal(typeof callRow.body.temperature, 'number')
  assert.equal(callRow.body.input_slice.role, 'analysis')
  assert.equal(typeof callRow.body.input_slice.budget_tokens, 'number')
  assert.match(callRow.body.started_at, /^\d{4}-\d{2}-\d{2}T/)
  assert.match(callRow.body.ended_at, /^\d{4}-\d{2}-\d{2}T/)
  const artifactRow = service.calls.find((c) => c.path.includes('/artifacts/analysis'))
  assert.ok(artifactRow.body.call_id)
  assert.equal(artifactRow.body.ai_run_id, AI_RUN_ID)
  // 05 P1-2: events are flushed in a batch after createAiRun (post-hoc), not
  // used as realtime progress.
  const eventCalls = service.calls.filter((c) => c.path.endsWith('/events'))
  assert.ok(eventCalls.length > 0, 'ai_run_events flushed')
  assert.ok(eventCalls.every((c) => c.method === 'POST' && c.path.startsWith('/api/ai-runs/')))
  assert.equal(eventCalls[0].body.event_type, 'role_start')
  assert.ok(eventCalls.some((c) => c.body.event_type === 'role_end'))
  assert.equal(result.round.status, 'no_trade')
})

test('services without audit endpoints degrade silently (404)', async () => {
  const service = makeService({ audit: false })
  const { integration } = makeIntegration({ service })
  const result = await integration.run({ symbol: 'BTCUSDT', trigger: 'manual' })
  assert.equal(result.round.status, 'no_trade')
  assert.equal(result.round.gaps.some((g) => g.includes('AI audit persistence failed')), false)
})

test('full_chain fixture still forbids signed order submission', async () => {
  const { integration, service } = makeIntegration({
    env: { WORKBENCH_LLM_SCRIPT: 'full_chain' },
  })
  const result = await integration.run({ symbol: 'BTCUSDT', trigger: 'manual' })
  assert.equal(service.calls.some((c) => c.path === '/api/execution/order'), false)
  assert.equal(typeof result.round.status, 'string')
})
