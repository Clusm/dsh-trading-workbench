/**
 * M1.4b Host /state aggregation tests.
 *
 * Run: node trading-workbench/plugin/test/host-state.test.mjs
 * Fake service only: no network, no participant service, no real order.
 */

import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { createIntegration, PORTFOLIO_CACHE_MS } from '../host/integration.js'
import { httpFetch, startServer } from './http-fixture.mjs'

const TOKEN = 'state-test-token'
const BASE_NOW = Date.parse('2026-11-09T05:00:00Z')

function jsonResponse(status, body) {
  return { ok: status >= 200 && status < 300, status, text: async () => JSON.stringify(body) }
}

function makeService() {
  const calls = []
  const fetchImpl = async (url, options = {}) => {
    const u = new URL(url)
    calls.push({ path: u.pathname, query: u.search, headers: options.headers ?? {} })
    if (u.pathname === '/api/ready') return jsonResponse(200, { ready: true, reasons: [], auth: { mode: 'bearer' } })
    if (u.pathname === '/health') return jsonResponse(200, { auth: { mode: 'bearer' } })
    if (u.pathname === '/api/status') {
      return jsonResponse(200, {
        credentials_configured: false,
        account: { available_balance: '9000', total_margin_balance: '10000' },
        equity: { session: '10000', exchange: '10100', available: true },
        positions: [{
          symbol: 'BTCUSDT',
          position_side: 'LONG',
          qty: '0.5',
          entry_price: '68000',
          mark_price: '69000',
          upnl: '500',
          protection: { status: 'OK', sl: '67000', tp: '70000' },
          origin: 'ai',
        }],
      })
    }
    if (u.pathname === '/api/positions') {
      return jsonResponse(200, {
        source: 'exchange',
        stale: false,
        positions: [{
          symbol: 'BTCUSDT',
          position_side: 'LONG',
          qty: '0.5',
          entry_price: '68000',
          mark_price: '69000',
          upnl: '500',
          leverage: 3,
          margin: '1200',
          margin_mode: 'ISOLATED',
          protection: { status: 'OK', sl: '67000', tp: '70000' },
          origin: 'ai',
        }],
      })
    }
    if (u.pathname === '/api/market/prices') {
      return jsonResponse(200, { as_of_ms: BASE_NOW, prices: { BTCUSDT: { last: 69000, mark: 69000, complete: true } } })
    }
    if (u.pathname === '/api/equity-curve') {
      return jsonResponse(200, {
        range_s: 86400,
        bucket_s: 60,
        points: [
          { ts: '2026-11-09T04:59:00Z', equity_session: '9900', equity_exchange: '10000', available: '9000', upnl: '100', margin_used: '1000' },
          { ts: '2026-11-09T05:00:00Z', equity_session: '10000', equity_exchange: '10100', available: '9000', upnl: '500', margin_used: '1200' },
        ],
      })
    }
    if (u.pathname === '/api/orders') {
      return jsonResponse(200, {
        orders: [{
          client_order_id: 'cid-1',
          exchange_order_id: '42',
          symbol: 'BTCUSDT',
          side: 'BUY',
          purpose: 'ENTRY',
          qty: '0.5',
          order_type: 'MARKET',
          status: 'FILLED',
          price: '68000',
          created_at: '2026-11-09T04:59:00Z',
        }],
      })
    }
    if (u.pathname === '/api/fills') {
      return jsonResponse(200, {
        fills: [{
          fill_id: 'fill-1',
          client_order_id: 'cid-1',
          symbol: 'BTCUSDT',
          side: 'BUY',
          qty: '0.5',
          price: '68000',
          realized_pnl: '0',
          commission: '1.2',
          trade_time_ms: 1762664340000,
        }],
      })
    }
    if (u.pathname === '/api/reviews') {
      return jsonResponse(404, { error: { code: 'E_NOT_FOUND', message: 'reviews not implemented' } })
    }
    if (u.pathname === '/api/risk/envelope') {
      return jsonResponse(200, {
        demo_only: true,
        ledger_id: 'ledger-state',
        limits: { per_trade: {} },
        control: { state: 'normal' },
        account: { credentials_configured: false },
        new_entry_allowed: false,
      })
    }
    return jsonResponse(404, { error: { code: 'E_NOT_FOUND', message: u.pathname } })
  }
  return { fetchImpl, calls }
}

function makeIntegration({ token = TOKEN, service = makeService(), now = () => BASE_NOW } = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-state-'))
  const tokenFile = path.join(dir, '.api-token')
  if (token !== null) fs.writeFileSync(tokenFile, `${token}\n`, 'utf8')
  return {
    dir,
    service,
    integration: createIntegration({
      serviceBaseUrl: 'http://127.0.0.1:8791',
      dataDir: dir,
      tokenFile,
      storePath: path.join(dir, 'rounds.json'),
      fetchImpl: service.fetchImpl,
      llm: { stream() { throw new Error('state test must not call the LLM') } },
      route: { provider: 'fake', model: 'fake-1' },
      now,
    }),
  }
}

test('state maps equity/positions/orders/fills and reports reviews gap', async () => {
  const env = makeIntegration()
  const state = await env.integration.state({ force: true })
  assert.equal(state.ok, true)
  assert.equal(state.service.state, 'online')
  assert.equal(state.service.ready, true)
  assert.equal(state.service.demo_only, true)
  assert.equal(state.service.auth_mode, 'bearer')
  assert.equal(state.service.ledger_id, 'ledger-state')
  assert.equal(state.service.control_state, 'normal')
  assert.equal(state.service.credentials_configured, false)
  assert.equal(state.service.new_entry_allowed, false)
  assert.equal(state.service.token_configured, true)
  assert.equal(state.service.base_url, 'http://127.0.0.1:8791')
  assert.ok(state.service.checked_at)
  assert.ok('last_run' in state.service)
  assert.equal(state.equityCurve.range, 'all')
  assert.equal(state.equityCurve.empty, false)
  assert.equal(state.equityCurve.session, '10000')
  assert.equal(state.equityCurve.exchange, '10100')
  assert.equal(state.equityCurve.latest.session, '10000')
  assert.equal(state.equitySession, '10000')
  assert.equal(state.equityExchange, '10100')
  assert.equal(state.positionsEmpty, false)
  assert.equal(state.positionsEmptyReason, null)
  assert.equal(state.ordersEmpty, false)
  assert.equal(state.fillsEmpty, false)
  assert.equal(state.reviewsEmpty, true)
  assert.equal(state.reviewsEmptyReason, 'E_REVIEWS_ENDPOINT_MISSING')
  assert.equal(state.reviewsGap, 'E_REVIEWS_ENDPOINT_MISSING')
  assert.equal(state.accountEmpty, false)
  assert.equal(state.account.gap, null)
  assert.equal(state.equityCurve.points.length, 2)
  assert.equal(state.equityCurve.points[1].session, '10000')
  assert.equal(state.equityCurve.points[1].exchange, '10100')
  assert.equal(state.positions.length, 1)
  assert.equal(state.positions[0].symbol, 'BTCUSDT')
  assert.equal(state.positions[0].side, 'LONG')
  assert.equal(state.positions[0].mark, '69000')
  assert.equal(state.positions[0].upnl, '500')
  assert.equal(state.positions[0].sl, '67000')
  assert.equal(state.positions[0].tp, '70000')
  assert.equal(state.positions[0].protection, 'OK')
  assert.equal(state.positions[0].origin, 'ai')
  assert.equal(state.orders.length, 1)
  assert.equal(state.orders[0].clientOrderId, 'cid-1')
  assert.equal(state.fills.length, 1)
  assert.equal(state.fills[0].realizedPnl, '0')
  assert.equal(state.account.equity, '10000')
  assert.equal(state.account.total_equity, '10000')
  assert.equal(state.account.available, '9000')
  assert.equal(state.reviews.length, 0)
  assert.ok(state.gaps.some((gap) => gap.includes('reviews')))
  assert.ok(env.service.calls.every((call) => call.headers.authorization === `Bearer ${TOKEN}`))
  assert.equal(JSON.stringify(state).includes(TOKEN), false)
  assert.equal(env.service.calls.some((call) => call.path === '/api/execution/order'), false)
})

test('state cache throttles repeated polls and manual refresh forces one fetch', async () => {
  let now = BASE_NOW
  const env = makeIntegration({ now: () => now })
  const first = await env.integration.state({ force: true })
  assert.equal(first.portfolio.cache_hit, false)
  const callsAfterFirst = env.service.calls.length
  now += 4_000
  const second = await env.integration.state()
  assert.equal(second.portfolio.cache_hit, true)
  assert.equal(second.portfolio.cached, true)
  assert.equal(env.service.calls.length, callsAfterFirst)
  now += Math.max(1, PORTFOLIO_CACHE_MS)
  const third = await env.integration.state()
  assert.equal(third.portfolio.cache_hit, false)
  assert.ok(env.service.calls.length > callsAfterFirst)
  const beforeForce = env.service.calls.length
  const forced = await env.integration.state({ force: true })
  assert.equal(forced.portfolio.cache_hit, false)
  assert.ok(env.service.calls.length > beforeForce)
})

test('no token yields explicit empty state without service calls', async () => {
  const env = makeIntegration({ token: null })
  const state = await env.integration.state({ force: true })
  assert.equal(state.ok, true)
  assert.equal(state.positions.length, 0)
  assert.equal(state.positionsEmpty, true)
  assert.equal(state.fills.length, 0)
  assert.equal(state.fillsEmpty, true)
  assert.equal(state.equityCurve.points.length, 0)
  assert.equal(state.equityCurve.empty, true)
  assert.equal(state.equityCurve.session, null)
  assert.equal(state.equityCurve.exchange, null)
  assert.equal(state.equityCurve.gap, 'E_SERVICE_TOKEN_MISSING')
  assert.equal(state.accountEmpty, true)
  assert.equal(state.account.gap, 'E_SERVICE_TOKEN_MISSING')
  assert.equal(state.reviewsGap, 'E_SERVICE_TOKEN_MISSING')
  assert.equal(state.service.token_present, false)
  assert.equal(state.service.token_configured, false)
  assert.equal(state.service.state, 'no_token')
  assert.equal(state.service.ready, false)
  assert.ok(state.gaps.some((gap) => gap.includes('E_SERVICE_TOKEN_MISSING')))
  assert.equal(env.service.calls.length, 0)
})

test('401 and service failures degrade to empty state plus gaps', async () => {
  const unauthorized = makeIntegration({
    service: { calls: [], fetchImpl: async () => jsonResponse(401, { error: { code: 'E_AUTH_REQUIRED', message: 'bad token' } }) },
  })
  const state401 = await unauthorized.integration.state({ force: true })
  assert.equal(state401.ok, true)
  assert.equal(state401.service.state, 'auth_failed')
  assert.equal(state401.service.token_configured, true)
  assert.equal(state401.positions.length, 0)
  assert.equal(state401.equityCurve.points.length, 0)
  assert.ok(state401.gaps.some((gap) => gap.includes('E_AUTH_REQUIRED')))
  assert.equal(JSON.stringify(state401).includes(TOKEN), false)

  const partial = makeIntegration({
    service: {
      calls: [],
      fetchImpl: async (url) => {
        const pathname = new URL(url).pathname
        if (pathname === '/api/ready') return jsonResponse(200, { ready: true })
        if (pathname === '/api/status') return jsonResponse(200, { positions: [] })
        if (pathname === '/api/positions') return jsonResponse(503, { error: { code: 'E_SERVICE_UNAVAILABLE', message: 'down' } })
        if (pathname === '/api/equity-curve') return jsonResponse(503, { error: { code: 'E_SERVICE_UNAVAILABLE', message: 'down' } })
        if (pathname === '/api/orders') return jsonResponse(503, { error: { code: 'E_SERVICE_UNAVAILABLE', message: 'down' } })
        if (pathname === '/api/fills') return jsonResponse(503, { error: { code: 'E_SERVICE_UNAVAILABLE', message: 'down' } })
        if (pathname === '/api/reviews') return jsonResponse(404, { error: { code: 'E_NOT_FOUND', message: 'reviews' } })
        return jsonResponse(200, {})
      },
    },
  })
  const state = await partial.integration.state({ force: true })
  assert.equal(state.ok, true)
  assert.equal(state.positions.length, 0)
  assert.equal(state.orders.length, 0)
  assert.equal(state.fills.length, 0)
  assert.ok(state.gaps.filter((gap) => gap.includes('unavailable')).length >= 3)

  const offline = makeIntegration({
    service: { calls: [], fetchImpl: async () => { throw new Error('ECONNREFUSED') } },
  })
  const offlineState = await offline.integration.state({ force: true })
  assert.equal(offlineState.ok, true)
  assert.equal(offlineState.service.state, 'offline')
  assert.equal(offlineState.positions.length, 0)
  assert.equal(offlineState.equityCurve.empty, true)
  assert.ok(offlineState.gaps.some((gap) => gap.includes('E_SERVICE_OFFLINE')))
})

test('empty equity points use status fallback and explicit empty gap', async () => {
  const base = makeService()
  const fetchImpl = async (url, options) => {
    const pathname = new URL(url).pathname
    if (pathname === '/api/equity-curve') return jsonResponse(200, { points: [] })
    if (pathname === '/api/status') {
      return jsonResponse(200, {
        credentials_configured: false,
        account: null,
        equity: { session: '9900', exchange: '10050', available: false },
      })
    }
    return base.fetchImpl(url, options)
  }
  const env = makeIntegration({ service: { fetchImpl, calls: base.calls } })
  const state = await env.integration.state({ force: true })
  assert.equal(state.equityCurve.empty, true)
  assert.equal(state.equityCurve.gap, 'E_EQUITY_CURVE_EMPTY')
  assert.equal(state.equityCurve.empty_reason, 'NO_EQUITY_POINTS')
  assert.equal(state.equityCurve.session, '9900')
  assert.equal(state.equityCurve.exchange, '10050')
  assert.equal(state.equitySession, '9900')
  assert.equal(state.equityExchange, '10050')
  assert.ok(state.gaps.some((gap) => gap.includes('E_EQUITY_CURVE_EMPTY')))
  assert.equal(state.accountEmpty, false)
  assert.equal(state.account.equity, '9900')
})

test('placeholder account values are empty, not numeric zero', async () => {
  const base = makeService()
  const fetchImpl = async (url, options) => {
    const pathname = new URL(url).pathname
    if (pathname === '/api/status') {
      return jsonResponse(200, {
        credentials_configured: true,
        account: null,
        equity: { session: null, exchange: null, available: false },
      })
    }
    if (pathname === '/api/equity-curve') return jsonResponse(200, { points: [] })
    return base.fetchImpl(url, options)
  }
  const env = makeIntegration({ service: { fetchImpl, calls: base.calls } })
  const state = await env.integration.state({ force: true })
  assert.equal(state.accountEmpty, true)
  assert.equal(state.account.empty, true)
  assert.equal(state.account.gap, 'E_ACCOUNT_EMPTY')
  assert.equal(state.account.available, undefined)
  assert.equal(state.account.available_balance, undefined)
  assert.ok(state.gaps.some((gap) => gap.includes('E_ACCOUNT_EMPTY')))
})

test('ready 503 keeps unavailable and is distinguishable from offline', async () => {
  const env = makeIntegration({
    service: {
      calls: [],
      fetchImpl: async () => jsonResponse(503, { error: { code: 'E_SERVICE_UNAVAILABLE', message: 'down' } }),
    },
  })
  const state = await env.integration.state({ force: true })
  assert.equal(state.service.state, 'unavailable')
  assert.equal(state.service.error_status, 503)
  assert.equal(state.equityCurve.empty, true)
  assert.equal(state.accountEmpty, true)
})

test('forced refresh waits for in-flight work and fetches again', async () => {
  const base = makeService()
  let readyCalls = 0
  const fetchImpl = async (url, options) => {
    if (new URL(url).pathname === '/api/ready') {
      readyCalls += 1
      await new Promise((resolve) => setTimeout(resolve, 30))
    }
    return base.fetchImpl(url, options)
  }
  const env = makeIntegration({ service: { fetchImpl, calls: base.calls } })
  const first = env.integration.state({ force: true })
  await new Promise((resolve) => setTimeout(resolve, 5))
  const second = env.integration.state({ force: true })
  const [firstState, secondState] = await Promise.all([first, second])
  assert.equal(firstState.equityCurve.count, 2)
  assert.equal(secondState.equityCurve.count, 2)
  assert.equal(secondState.portfolio.refresh_mode, 'forced_after_inflight')
  assert.equal(readyCalls, 2)
})

test('GET /state?refresh=1 forces refresh over the Host route', async () => {
  const env = makeIntegration()
  // t30: node:http (one connection per request) instead of global fetch — see
  // test/http-fixture.mjs for the Windows/undici/force-exit assertion.
  const server = await startServer((req, res) => env.integration.handle(req, res))
  try {
    const first = await httpFetch(`${server.url}/trading-workbench/api/state?refresh=1`)
    assert.equal(first.status, 200)
    const firstBody = await first.json()
    assert.equal(firstBody.portfolio.cache_hit, false)
    const before = env.service.calls.length
    const second = await httpFetch(`${server.url}/trading-workbench/api/state`)
    assert.equal(second.status, 200)
    const secondBody = await second.json()
    assert.equal(secondBody.portfolio.cache_hit, true)
    assert.equal(env.service.calls.length, before)
    const third = await httpFetch(`${server.url}/trading-workbench/api/state?refresh=1`)
    assert.equal(third.status, 200)
    assert.ok(env.service.calls.length > before)
  } finally {
    await server.close()
  }
})
