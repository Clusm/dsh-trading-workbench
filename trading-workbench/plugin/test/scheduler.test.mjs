/**
 * M1.4 auto-schedule regression suite.
 *
 * Run: node trading-workbench/plugin/test/scheduler.test.mjs
 *
 * Pure unit tests: a fake integration stands in for the Host round runner, so
 * there is no network, no service, no LLM and no order.
 */

import test from 'node:test'
import assert from 'node:assert/strict'

import {
  AUTO_SCHEDULE_DEFAULTS,
  AutoScheduler,
  createAutoScheduler,
  readAutoScheduleConfig,
} from '../host/scheduler.js'

const T0 = Date.parse('2026-11-10T04:00:00Z')
const TEN_MIN = 10 * 60_000

function clock(start = T0) {
  let current = start
  return {
    now: () => current,
    advance: (ms) => { current += ms },
    set: (ms) => { current = ms },
  }
}

function fakeIntegration({
  preflight = { ok: true },
  runResult = { ok: true, round: { outcome: 'no_trade', status: 'no_trade', reasonCode: 'NO_CANDIDATE' } },
  runError = null,
  rounds = [],
  inFlight = null,
  autoRounds24h = null,
} = {}) {
  const integration = {
    inFlight,
    autoRoundsLast24h: autoRounds24h === null ? undefined : () => autoRounds24h,
    store: {
      load() {},
      list() { return rounds },
    },
    calls: [],
    preflightCalls: 0,
    async preflight() {
      this.preflightCalls += 1
      return preflight
    },
    async run(options) {
      this.calls.push(options)
      if (runError) throw runError
      return runResult
    },
  }
  return integration
}

function makeScheduler({ env = {}, config = {}, integration = fakeIntegration(), time = clock() } = {}) {
  const scheduler = createAutoScheduler({ integration, env, now: time.now, config })
  return { scheduler, integration, time }
}

test('A05: the 24h budget uses the durable sidecar, not the 100-row UI cache', () => {
  const integration = fakeIntegration({ autoRounds24h: 96 })
  const { scheduler } = makeScheduler({ integration, config: { maxRoundsPerDay: 96 } })
  assert.equal(scheduler.status().rounds_last_24h, 96)
})
test('defaults are on and follow the 10 minute contract', () => {
  const config = readAutoScheduleConfig({})
  assert.equal(config.enabled, true)
  assert.equal(config.intervalMs, TEN_MIN)
  assert.equal(config.symbol, 'BTCUSDT')
  assert.equal(config.maxRoundsPerDay, AUTO_SCHEDULE_DEFAULTS.maxRoundsPerDay)
})

test('WORKBENCH_AUTO_SCHEDULE=off disables the scheduler', () => {
  assert.equal(readAutoScheduleConfig({ WORKBENCH_AUTO_SCHEDULE: 'off' }).enabled, false)
  assert.equal(readAutoScheduleConfig({ WORKBENCH_AUTO_SCHEDULE: '0' }).enabled, false)
  assert.equal(readAutoScheduleConfig({ WORKBENCH_AUTO_SCHEDULE: 'on' }).enabled, true)
})

test('interval and symbol are clamped and sanitised', () => {
  const config = readAutoScheduleConfig({
    WORKBENCH_AUTO_INTERVAL_MS: '1000',
    WORKBENCH_AUTO_SYMBOL: 'btcusdt',
    WORKBENCH_AUTO_MAX_ROUNDS_PER_DAY: '9999',
  })
  assert.equal(config.intervalMs, 60_000)
  assert.equal(config.symbol, 'BTCUSDT')
  assert.equal(config.maxRoundsPerDay, 288)

  const fallback = readAutoScheduleConfig({ WORKBENCH_AUTO_SYMBOL: 'not a symbol!' })
  assert.equal(fallback.symbol, 'BTCUSDT')
})

test('tick runs one auto round and passes an epoch-aligned slot id', async () => {
  const { scheduler, integration, time } = makeScheduler()
  await scheduler.tick()

  assert.equal(integration.calls.length, 1)
  assert.deepEqual(integration.calls[0], {
    symbol: 'BTCUSDT',
    trigger: 'auto',
    slotId: `auto-${TEN_MIN}-${Math.floor(T0 / TEN_MIN)}`,
  })

  // A second tick inside the same slot must not run again (double timers, reload,
  // clock jump): the service would reject the duplicate slot anyway.
  time.advance(60_000)
  await scheduler.tick()
  assert.equal(integration.calls.length, 1)
  assert.equal(scheduler.status().last_result.code, 'OVERLAP_SKIPPED')

  time.advance(TEN_MIN)
  await scheduler.tick()
  assert.equal(integration.calls.length, 2)
  assert.notEqual(integration.calls[1].slotId, integration.calls[0].slotId)
})

test('tick does not run when disabled', async () => {
  const { scheduler, integration } = makeScheduler({ env: { WORKBENCH_AUTO_SCHEDULE: 'off' } })
  await scheduler.tick()
  assert.equal(integration.calls.length, 0)
  assert.equal(scheduler.status().last_result.code, 'AUTO_DISABLED')
})

test('a failed preflight skips without spending a round or tripping the breaker', async () => {
  const integration = fakeIntegration({ preflight: { ok: false, code: 'CONTROL_LOCKED', detail: 'close_only' } })
  const { scheduler } = makeScheduler({ integration })

  for (let i = 0; i < 5; i += 1) {
    await scheduler.tick()
  }
  assert.equal(integration.calls.length, 0)
  assert.equal(integration.preflightCalls, 5)
  assert.equal(scheduler.consecutiveFailures, 0)
  assert.equal(scheduler.status().last_result.code, 'CONTROL_LOCKED')
})

test('a round already in flight is skipped as overlap', async () => {
  const integration = fakeIntegration({ inFlight: 'manual-round' })
  const { scheduler } = makeScheduler({ integration })
  await scheduler.tick()
  assert.equal(integration.calls.length, 0)
  assert.equal(scheduler.status().last_result.code, 'ROUND_IN_FLIGHT')
})

test('three consecutive failures open the circuit and later ticks are skipped', async () => {
  const integration = fakeIntegration({
    runResult: { ok: false, round: { outcome: 'failed', status: 'failed', reasonCode: 'LLM_TIMEOUT' } },
  })
  const { scheduler, time } = makeScheduler({ integration })

  for (let index = 0; index < 3; index += 1) {
    await scheduler.tick()
    time.advance(TEN_MIN)
  }
  assert.equal(integration.calls.length, 3)
  assert.ok(scheduler.pausedUntil !== null, 'circuit should be open')
  assert.equal(scheduler.consecutiveFailures, 0)

  await scheduler.tick()
  assert.equal(integration.calls.length, 3, 'paused scheduler must not run')
  assert.equal(scheduler.status().last_result.code, 'PAUSED_AFTER_FAILURES')

  time.advance(scheduler.config.pauseAfterFailuresMs + 1)
  await scheduler.tick()
  assert.equal(integration.calls.length, 4, 'scheduler resumes after the pause elapses')
})

test('a successful round resets the failure counter', async () => {
  const integration = fakeIntegration()
  const { scheduler } = makeScheduler({ integration })
  scheduler.consecutiveFailures = 2
  await scheduler.tick()
  assert.equal(scheduler.consecutiveFailures, 0)
  assert.equal(scheduler.pausedUntil, null)
})

test('duplicate slot / running cycle are treated as overlap, not failure', async () => {
  const integration = fakeIntegration({
    runError: Object.assign(new Error('slot already used'), { code: 'E_CYCLE_DUPLICATE_SLOT' }),
  })
  const { scheduler } = makeScheduler({ integration })
  await scheduler.tick()
  assert.equal(scheduler.consecutiveFailures, 0)
  assert.equal(scheduler.status().last_result.code, 'OVERLAP_SKIPPED')
})

test('the daily budget caps auto rounds', async () => {
  const rounds = Array.from({ length: 3 }, (_, index) => ({
    trigger: 'auto',
    startedAt: new Date(T0 - index * 60_000).toISOString(),
  }))
  const integration = fakeIntegration({ rounds })
  const { scheduler } = makeScheduler({ integration, config: { maxRoundsPerDay: 3 } })

  await scheduler.tick()
  assert.equal(integration.calls.length, 0)
  assert.equal(scheduler.status().last_result.code, 'DAILY_BUDGET_EXHAUSTED')
  assert.equal(scheduler.status().rounds_last_24h, 3)
})

test('manual rounds do not count against the auto budget', async () => {
  const rounds = [
    { trigger: 'manual', startedAt: new Date(T0 - 60_000).toISOString() },
    { trigger: 'auto', startedAt: new Date(T0 - 120_000).toISOString() },
  ]
  const integration = fakeIntegration({ rounds })
  const { scheduler } = makeScheduler({ integration, config: { maxRoundsPerDay: 2 } })
  await scheduler.tick()
  assert.equal(integration.calls.length, 1)
})

test('setEnabled toggles at runtime and start()/stop() manage the timer', () => {
  const { scheduler, time } = makeScheduler({ config: { startDelayMs: 5_000 } })
  const started = scheduler.start()
  assert.equal(started.started, true)
  assert.equal(started.next_at, new Date(T0 + 5_000).toISOString())

  scheduler.setEnabled(false)
  assert.equal(scheduler.status().enabled, false)
  assert.equal(scheduler.status().next_at, null)

  scheduler.setEnabled(true)
  assert.equal(scheduler.status().enabled, true)
  assert.ok(scheduler.status().next_at !== null)

  scheduler.stop()
  assert.equal(scheduler.status().started, false)
  assert.equal(scheduler.status().next_at, null)

  time.advance(1_000)
  scheduler.dispose()
})

test('status is JSON-serialisable and carries the contract id', () => {
  const { scheduler } = makeScheduler()
  const status = scheduler.status()
  assert.equal(status.contract, 'trading-workbench/auto-schedule/1')
  assert.doesNotThrow(() => JSON.stringify(status))
  assert.ok(Array.isArray(status.history))
})

test('constructor rejects an integration without run()', () => {
  assert.throws(() => new AutoScheduler({ integration: {} }), /requires an integration with run/i)
})

test('a hung round cannot keep the scheduler busy forever', async () => {
  let release
  const hung = new Promise((resolve) => { release = resolve })
  const integration = {
    inFlight: null,
    runTimeoutMs: 30,
    store: { load() {}, list() { return [] } },
    async preflight() { return { ok: true } },
    async run() {
      this.inFlight = 'hung-round'
      return hung
    },
    abandonRun() { this.inFlight = null; return true },
  }
  const { scheduler } = makeScheduler({ integration, config: { runGuardMs: 60 } })
  const started = Date.now()
  const result = await scheduler.tick()
  assert.equal(result, null)
  assert.ok(Date.now() - started < 2_000, 'the guard must fire quickly in this test')
  assert.equal(scheduler.busy, false, 'scheduler must not stay busy')
  assert.equal(scheduler.status().last_result.code, 'HOST_RUN_TIMEOUT')
  release(null)
})

test('a stale in-flight round is reclaimed before the in-flight skip', async () => {
  let recovered = false
  const integration = {
    inFlight: 'stale-round',
    runTimeoutMs: 30,
    store: { load() {}, list() { return [] } },
    recoverStaleInFlight() { recovered = true; this.inFlight = null; return true },
    async preflight() { return { ok: true } },
    async run() { return { ok: true, round: { outcome: 'no_trade', status: 'no_trade', reasonCode: 'NO_CANDIDATE' } } },
  }
  const { scheduler } = makeScheduler({ integration })
  await scheduler.tick()
  assert.equal(recovered, true)
  assert.equal(scheduler.status().last_result.kind, 'round')
})