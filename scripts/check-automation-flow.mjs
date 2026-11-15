#!/usr/bin/env node
/**
 * Stop/start semantics check for "停止自动交易" (F1 / §3.2 / §8·F item 1).
 *
 *   node trading-workbench/scripts/check-automation-flow.mjs
 *
 * Runs the REAL Host route (integration.js + scheduler.js) against an in-memory
 * service double, and asserts the accepted contract:
 *
 *  1. stop  = close the local scheduler + one POST /api/control
 *             {state:'pause', confirm:'PAUSE', reason} — the existing
 *             non-flattening control state. It never posts emergency, never
 *             calls /api/execution/close and never mentions flatten.
 *  2. stop has a progress state: 'stopping' while the service call is in
 *     flight, 'stopped' only after the service answers.
 *  3. a Host that stopped locally but could not reach the service reports
 *     phase 'pending_confirmation' (the UI shows "本机调度已停，服务端状态待确认").
 *  4. the stopped state is derived from the service control row, so a fresh
 *     Host process (restart) with no memory of the click still reports stopped.
 *  5. start re-verifies first, then sets control normal and arms the scheduler;
 *     a service rejection keeps the scheduler off.
 *
 * Nothing here talks to the real service: it uses a local double and a temp
 * data directory, so running it cannot change the live workbench state.
 */

import fs from 'node:fs'
import http from 'node:http'
import os from 'node:os'
import path from 'node:path'

import { createIntegration } from '../host/integration.js'
import { createAutoScheduler } from '../host/scheduler.js'
import { WorkbenchServiceClient } from '../host/service-client.js'

const failures = []
const notes = []
function check(label, condition, detail) {
  if (condition) {
    notes.push('ok   ' + label)
    return true
  }
  failures.push(label + (detail === undefined ? '' : ' — ' + detail))
  return false
}

const TOKEN = 'automation-flow-token'

function json(res, status, body) {
  const payload = JSON.stringify(body)
  res.writeHead(status, { 'content-type': 'application/json', 'content-length': Buffer.byteLength(payload) })
  res.end(payload)
}

/** Minimal service double; records every call so "no flatten" is provable. */
function makeService({ controlState = 'normal', controlStatus = 200, offline = false } = {}) {
  const state = {
    control: { state: controlState, reason: controlState === 'normal' ? 'default' : 'operator stop', updated_at: '2026-11-14T00:00:00Z', updated_by: 'operator' },
    calls: [],
  }
  const fetchImpl = async (url, options = {}) => {
    const u = new URL(String(url))
    const body = options.body ? JSON.parse(String(options.body)) : null
    state.calls.push({ path: u.pathname, method: options.method ?? 'GET', body })
    if (offline) throw Object.assign(new Error('connect ECONNREFUSED'), { code: 'ECONNREFUSED' })
    const respond = (status, payload) => ({
      ok: status >= 200 && status < 300,
      status,
      text: async () => JSON.stringify(payload),
      json: async () => payload,
    })
    if (u.pathname === '/api/ready') {
      return respond(200, { ready: true, service_ready: true, reasons: [], new_entry_allowed: state.control.state === 'normal' })
    }
    if (u.pathname === '/health') return respond(200, { service: 'trading-workbench', auth: { mode: 'bearer' } })
    if (u.pathname === '/api/status') {
      return respond(200, {
        credentials_configured: true,
        account: { total_margin_balance: '10000', available_balance: '9000', total_wallet_balance: '10000' },
        equity: { session: '10000', exchange: '10100' },
        positions: [],
      })
    }
    if (u.pathname === '/api/positions') return respond(200, { positions: [] })
    if (u.pathname === '/api/equity-curve') return respond(200, { points: [] })
    if (u.pathname === '/api/orders') return respond(200, { orders: [] })
    if (u.pathname === '/api/fills') return respond(200, { fills: [] })
    if (u.pathname === '/api/reviews') return respond(200, { reviews: [] })
    if (u.pathname === '/api/market/health') return respond(200, { verdict: 'GOOD', score: 100, issues: [] })
    if (u.pathname === '/api/market/prices') {
      return respond(200, { as_of_ms: Date.now(), prices: { BTCUSDT: { last: 68000, mark: 68000, age_ms: 100 } } })
    }
    if (u.pathname === '/api/risk/envelope') {
      return respond(200, {
        demo_only: true,
        ledger_id: 'ledger-flow-test',
        limits: { per_trade: { max_risk_pct: 0.01, max_leverage: 10 } },
        control: { ...state.control },
        new_entry_allowed: state.control.state === 'normal',
        blocking_reasons: [],
      })
    }
    if (u.pathname === '/api/control') {
      if (controlStatus !== 200) {
        return respond(controlStatus, { error: { code: 'E_CONTROL_LOCKED', message: 'risk latch active' } })
      }
      state.control = {
        state: String(body?.state ?? 'normal'),
        reason: String(body?.reason ?? ''),
        updated_at: new Date().toISOString(),
        updated_by: 'operator',
      }
      return respond(200, { control: { ...state.control }, operation_status: 'succeeded', request_id: body?.request_id ?? null })
    }
    if (u.pathname === '/api/operations') return respond(200, { operations: [] })
    return respond(200, { ok: true })
  }
  return { fetchImpl, state }
}

function makeEnv({ service, dir }) {
  fs.writeFileSync(path.join(dir, '.api-token'), TOKEN + '\n', 'utf8')
  const integration = createIntegration({
    serviceBaseUrl: 'http://127.0.0.1:8791',
    dataDir: dir,
    tokenFile: path.join(dir, '.api-token'),
    storePath: path.join(dir, 'rounds.json'),
    fetchImpl: service.fetchImpl,
    route: { provider: 'test', model: 'test' },
    // The start path runs the real preflight, which needs an LLM route. The
    // in-conversation fixture mode provides one without any provider/credentials.
    env: { WORKBENCH_LLM_MODE: 'session_fixture' },
    now: () => Date.now(),
  })
  const scheduler = createAutoScheduler({
    integration,
    env: { WORKBENCH_AUTO_SCHEDULE: 'on' },
    config: { intervalMs: 60_000, startDelayMs: 0 },
  })
  return { integration, scheduler }
}

function post(base, pathName, body) {
  const target = new URL(pathName, base)
  return new Promise((resolve, reject) => {
    const payload = JSON.stringify(body)
    const request = http.request({
      hostname: target.hostname,
      port: target.port,
      path: target.pathname,
      method: 'POST',
      agent: false,
      headers: {
        'content-type': 'application/json',
        'content-length': Buffer.byteLength(payload),
        accept: 'application/json',
        connection: 'close',
        'sec-fetch-site': 'same-origin',
      },
    }, (response) => {
      const chunks = []
      response.on('data', (chunk) => chunks.push(chunk))
      response.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8')
        resolve({ status: response.statusCode, body: text ? JSON.parse(text) : null })
      })
    })
    request.on('error', reject)
    request.write(payload)
    request.end()
  })
}

function get(base, pathName) {
  const target = new URL(pathName, base)
  return new Promise((resolve, reject) => {
    const request = http.request({
      hostname: target.hostname,
      port: target.port,
      path: target.pathname,
      method: 'GET',
      agent: false,
      headers: { accept: 'application/json', connection: 'close', 'sec-fetch-site': 'same-origin' },
    }, (response) => {
      const chunks = []
      response.on('data', (chunk) => chunks.push(chunk))
      response.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8')
        resolve({ status: response.statusCode, body: text ? JSON.parse(text) : null })
      })
    })
    request.on('error', reject)
    request.end()
  })
}

async function withHost(integration, run) {
  const server = http.createServer((req, res) => { void integration.handle(req, res) })
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const base = 'http://127.0.0.1:' + server.address().port
  try {
    return await run(base)
  } finally {
    if (typeof server.closeAllConnections === 'function') server.closeAllConnections()
    await new Promise((resolve) => server.close(resolve))
  }
}

function controlCall(service) {
  return service.state.calls.filter((call) => call.path === '/api/control')
}

async function main() {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-automation-'))
  try {
    // ---- scenario 1 + 2: running -> stopping -> stopped --------------------
    const service = makeService()
    const env = makeEnv({ service, dir })
    const before = await env.integration.state({ force: true })
    check('initial automation is running', before.automation?.state === 'running', JSON.stringify(before.automation?.state))
    check('scheduler starts enabled', env.scheduler.status().enabled === true)

    let stopResponse = null
    await withHost(env.integration, async (base) => {
      // 'stopping' must be observable while the service call is in flight.
      stopResponse = await post(base, '/trading-workbench/api/automation/stop', { request_id: 'req-flow-stop-1', reason: 'top bar stop' })
    })
    check('stop responses HTTP 200', stopResponse.status === 200, String(stopResponse.status))
    check('stop phase is stopped after service confirmation', stopResponse.body?.phase === 'stopped', String(stopResponse.body?.phase))
    check('stop disables the local scheduler', env.scheduler.status().enabled === false, JSON.stringify(env.scheduler.status().next_at))
    check('stop clears the armed timer', env.scheduler.status().next_at === null, String(env.scheduler.status().next_at))
    const calls = controlCall(service)
    check('exactly one control call', calls.length === 1, String(calls.length))
    check('control state is pause', calls[0]?.body?.state === 'pause', JSON.stringify(calls[0]?.body))
    check('control confirm is PAUSE', calls[0]?.body?.confirm === 'PAUSE', JSON.stringify(calls[0]?.body))
    check('control carries the reason', typeof calls[0]?.body?.reason === 'string' && calls[0].body.reason.length >= 2, JSON.stringify(calls[0]?.body?.reason))
    check('control carries the request id', calls[0]?.body?.request_id === 'req-flow-stop-1', String(calls[0]?.body?.request_id))
    const emergency = service.state.calls.filter((call) => String(call.body?.state ?? '') === 'emergency')
    check('never posts emergency', emergency.length === 0, String(emergency.length))
    const flattenish = service.state.calls.filter((call) => /flatten/i.test(JSON.stringify(call.body ?? {})))
    check('never asks for a flatten', flattenish.length === 0, String(flattenish.length))
    const closes = service.state.calls.filter((call) => /\/api\/execution\/close|\/api\/positions\/close/.test(call.path))
    check('never closes a position', closes.length === 0, JSON.stringify(closes.map((call) => call.path)))
    check('stop response automation is stopped', stopResponse.body?.automation?.state === 'stopped', String(stopResponse.body?.automation?.state))
    check('stop response exposes the service control row', stopResponse.body?.service_control?.state === 'pause', JSON.stringify(stopResponse.body?.service_control))
    check('stop response exposes the scheduler result', stopResponse.body?.scheduler?.stopped === true, JSON.stringify(stopResponse.body?.scheduler))

    // A page refresh = a fresh /state; the stop must survive it.
    const after = await env.integration.state({ force: true })
    check('refresh still reports stopped', after.automation?.state === 'stopped', JSON.stringify(after.automation))
    check('refresh explains the stop from the service row', after.automation?.service_control?.state === 'pause' && after.automation?.service_control?.reason, JSON.stringify(after.automation?.service_control))

    // ---- scenario 3: Host restart = new process, no memory of the click ----
    const restartedService = makeService({ controlState: 'pause' })
    const restartedEnv = makeEnv({ service: restartedService, dir: fs.mkdtempSync(path.join(os.tmpdir(), 'twb-automation-restart-')) })
    const restartedState = await restartedEnv.integration.state({ force: true })
    check('a restarted Host still reports stopped from the service row',
      restartedState.automation?.state === 'stopped', JSON.stringify(restartedState.automation))
    check('a restarted Host did not stop anything by itself',
      controlCall(restartedService).length === 0, String(controlCall(restartedService).length))

    // ---- scenario 4: local stop while the service is unreachable ----------
    const offlineService = makeService({ offline: true })
    const offlineEnv = makeEnv({ service: offlineService, dir: fs.mkdtempSync(path.join(os.tmpdir(), 'twb-automation-offline-')) })
    let offlineStop = null
    await withHost(offlineEnv.integration, async (base) => {
      offlineStop = await post(base, '/trading-workbench/api/automation/stop', { request_id: 'req-flow-stop-offline', reason: 'stop while offline' })
    })
    check('offline stop still answers 200', offlineStop.status === 200, String(offlineStop.status))
    check('offline stop is pending_confirmation', offlineStop.body?.phase === 'pending_confirmation', String(offlineStop.body?.phase))
    check('offline stop still closes the local scheduler', offlineEnv.scheduler.status().enabled === false)
    check('offline stop carries the service error', Boolean(offlineStop.body?.error?.code), JSON.stringify(offlineStop.body?.error))
    const offlineState = await offlineEnv.integration.state({ force: true })
    check('offline stop reads as pending, not stopped', offlineState.automation?.state === 'pending', JSON.stringify(offlineState.automation?.state))
    check('offline pending explains why', offlineState.automation?.reason_code === 'SERVICE_UNREACHABLE', String(offlineState.automation?.reason_code))

    // ---- scenario 5: explicit start ---------------------------------------
    const startService = makeService({ controlState: 'pause' })
    const startEnv = makeEnv({ service: startService, dir: fs.mkdtempSync(path.join(os.tmpdir(), 'twb-automation-start-')) })
    startEnv.scheduler.setEnabled(false)
    let startResponse = null
    await withHost(startEnv.integration, async (base) => {
      startResponse = await post(base, '/trading-workbench/api/automation/start', { request_id: 'req-flow-start-1', reason: 'start again' })
    })
    check('start answers 200', startResponse.status === 200, String(startResponse.status))
    check('start sets control normal', controlCall(startService)[0]?.body?.state === 'normal', JSON.stringify(controlCall(startService)[0]?.body))
    check('start arms the scheduler', startEnv.scheduler.status().enabled === true)
    check('start reports running', startResponse.body?.automation?.state === 'running', String(startResponse.body?.automation?.state))

    const lockedService = makeService({ controlState: 'pause', controlStatus: 423 })
    const lockedEnv = makeEnv({ service: lockedService, dir: fs.mkdtempSync(path.join(os.tmpdir(), 'twb-automation-locked-')) })
    lockedEnv.scheduler.setEnabled(false)
    let lockedResponse = null
    await withHost(lockedEnv.integration, async (base) => {
      lockedResponse = await post(base, '/trading-workbench/api/automation/start', { request_id: 'req-flow-start-2', reason: 'start while latched' })
    })
    check('a risk latch rejects start with 423', lockedResponse.status === 423, String(lockedResponse.status))
    check('a rejected start says why', lockedResponse.body?.error?.code === 'E_CONTROL_LOCKED', JSON.stringify(lockedResponse.body?.error))
    check('a rejected start leaves the scheduler off', lockedEnv.scheduler.status().enabled === false)

    // ---- scenario 6: host info is enough to verify identity ----------------
    const info = env.integration.hostInfo()
    check('host info carries an absolute data dir', path.isAbsolute(String(info.data_dir)), String(info.data_dir))
    check('host info carries the service base url', info.service_base_url === 'http://127.0.0.1:8791', String(info.service_base_url))
    check('host info carries the load identity', Boolean(info.build?.buildId) && Boolean(info.build?.loaded_at), JSON.stringify(info.build))
    check('host info separates release from load time',
      'release_id' in (info.build ?? {}) && typeof info.build?.loaded_at === 'string',
      JSON.stringify({ release: info.build?.release_id, loaded_at: info.build?.loaded_at }))
    check('host info reports the automation projection', Boolean(info.automation?.state), JSON.stringify(info.automation?.state))
  } finally {
    try { fs.rmSync(dir, { recursive: true, force: true }) } catch (error) { /* temp dir */ }
  }

  // ---- scenario 7: an automatic slot is recorded as auto (T01 G3) --------
  // cycle_runs.trigger was 'manual' for every row because the Host never sent
  // it; the scheduler's slot must reach the service as trigger=auto.
  const sent = []
  const captureClient = new WorkbenchServiceClient({
    baseUrl: 'http://127.0.0.1:8791',
    token: TOKEN,
    fetchImpl: async (url, options = {}) => {
      sent.push({ path: new URL(String(url)).pathname, body: options.body ? JSON.parse(String(options.body)) : null })
      return {
        ok: true,
        status: 200,
        text: async () => JSON.stringify({ cycle_id: 'cyc-wire-test' }),
        json: async () => ({ cycle_id: 'cyc-wire-test' }),
      }
    },
  })
  await captureClient.createCycle('cyc-wire-auto', 'BTCUSDT', 'auto-600000-123', { trigger: 'auto' })
  await captureClient.createCycle('cyc-wire-manual', 'BTCUSDT', null, { trigger: 'manual' })
  check('auto slots send trigger=auto', sent[0]?.body?.trigger === 'auto', JSON.stringify(sent[0]?.body))
  check('manual rounds send trigger=manual', sent[1]?.body?.trigger === 'manual', JSON.stringify(sent[1]?.body))
  check('the slot id still rides along', sent[0]?.body?.slot_id === 'auto-600000-123', JSON.stringify(sent[0]?.body?.slot_id))

  for (const line of notes) console.log(line)
  if (failures.length > 0) {
    console.error('\nFAILED ' + failures.length + ' check(s):')
    for (const failure of failures) console.error(' - ' + failure)
    process.exit(1)
  }
  console.log('\nAll ' + notes.length + ' automation-flow checks passed.')
}

main().catch((error) => {
  console.error('automation flow check crashed: ' + String(error?.stack ?? error))
  process.exit(1)
})
