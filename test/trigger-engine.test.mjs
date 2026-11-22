import test from 'node:test'
import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import {
  TRIGGER_ENGINE_DEFAULTS,
  TriggerEngine,
  createTriggerEngine,
  nextKlineDelay,
  readTriggerEngineConfig,
} from '../host/trigger-engine.js'

const NOW = Date.parse('2026-11-23T00:00:00Z')
const K15 = 900_000

const RULES = {
  symbols: ['BTCUSDT'],
  timeframes: ['15m'],
  execution_mode: 'rule_direct',
  entry: { kind: 'candle_close', side: 'long', operator: 'gt', value: 67800 },
  exit: { stop_loss_pct: 0.02, take_profit_pct: 0.04 },
}

function triggeredItem(overrides = {}) {
  return {
    rule_key: 'strategy:strat-1:2',
    kind: 'strategy',
    strategy_id: 'strat-1',
    name: '突破直连',
    version: 2,
    generation: 3,
    mode_revision: 2,
    execution_mode: 'rule_direct',
    symbol: 'BTCUSDT',
    side: 'long',
    signal_key: '15m:1994071',
    mark: 68000,
    trigger_ref: 67800,
    distance_pct: 0.3,
    priority: 1,
    strategy_context: {
      strategy_id: 'strat-1',
      version: 2,
      generation: 3,
      mode_revision: 2,
      signal_key: '15m:1994071',
    },
    facts: { candle_close: 67900 },
    detail: '15m 收盘 67900 > 67800',
    ...overrides,
  }
}

function makeClock(start = NOW) {
  return {
    now: start,
    advance(ms) {
      this.now += ms
      return this.now
    },
  }
}

/** 可手动触发的假定时器：start()/stop() 有没有残留、tick 会不会重新武装都靠它断言。 */
function makeTimers() {
  let seq = 0
  const pending = new Map()
  return {
    timers: {
      setTimeout(fn, ms) {
        const id = ++seq
        pending.set(id, { fn, ms })
        return id
      },
      clearTimeout(id) {
        pending.delete(id)
      },
    },
    pending,
    size() {
      return pending.size
    },
    delays() {
      return [...pending.values()].map((item) => item.ms)
    },
    /** 按注册顺序取出一个回调执行（不自动重排，重排由引擎自己负责）。 */
    fireNext() {
      const entry = pending.entries().next()
      if (entry.done) return false
      const [id, item] = entry.value
      pending.delete(id)
      item.fn()
      return true
    },
    /** 按延迟精确取一个回调执行：mark(15s) 与 kline(3.1s) 的先后顺序由测试决定。 */
    fireDelay(ms) {
      for (const [id, item] of pending) {
        if (item.ms !== ms) continue
        pending.delete(id)
        item.fn()
        return true
      }
      return false
    },
  }
}

const flush = () => new Promise((resolve) => setImmediate(resolve))

function makeFakeClient(options = {}) {
  const state = {
    triggered: options.triggered ?? [],
    waiting: options.waiting ?? [],
    mode: options.mode ?? 'mounted',
    modeRevision: options.modeRevision ?? 2,
    rules: options.rules ?? RULES,
    leaseError: options.leaseError ?? null,
    cycleError: options.cycleError ?? null,
    scanError: options.scanError ?? null,
    strategyError: options.strategyError ?? null,
    releaseError: options.releaseError ?? null,
    dispatchedOk: options.dispatchedOk !== false,
    lease: options.lease ?? { lease_id: 'lease-1' },
    scans: 0,
  }
  const calls = []
  const client = {
    async scanRules(body) {
      calls.push({ path: 'scanRules', body })
      if (state.scanError) throw state.scanError
      state.scans += 1
      const list = typeof state.triggered === 'function' ? state.triggered() : state.triggered
      const waiting = typeof state.waiting === 'function' ? state.waiting() : state.waiting
      return {
        mode: state.mode,
        mode_revision: state.modeRevision,
        scope: body.scope,
        scan_interval_ms: body.scan_interval_ms,
        evaluated_at_ms: body.now_ms,
        triggered: list,
        waiting,
        transitioned: [],
        skipped: [],
        prices: {},
      }
    },
    async strategy(suffix) {
      calls.push({ path: 'strategy', suffix })
      if (state.strategyError) throw state.strategyError
      return { strategy: { confirmed_version: { rules: state.rules } } }
    },
    async acquireLease(body) {
      calls.push({ path: 'acquireLease', body })
      if (state.leaseError) throw state.leaseError
      return state.lease
    },
    async releaseLease(body) {
      calls.push({ path: 'releaseLease', body })
      if (state.releaseError) throw state.releaseError
      return { ok: true }
    },
    async createCycle(cycleId, symbol, slotId, opts) {
      calls.push({ path: 'createCycle', cycleId, symbol, slotId, opts })
      if (state.cycleError) throw state.cycleError
      return { cycle_id: cycleId, state: 'RUNNING' }
    },
    async markRulesDispatched(body) {
      calls.push({ path: 'markRulesDispatched', body })
      if (!state.dispatchedOk) {
        throw Object.assign(new Error('POST /api/rules/dispatched is not implemented'), {
          code: 'E_SERVICE_HTTP_404',
          status: 404,
        })
      }
      return { state: {}, idempotent: true, outcome: body.outcome }
    },
    async finalizeCycle(cycleId, payload) {
      calls.push({ path: 'finalizeCycle', cycleId, payload })
      return { ok: true }
    },
  }
  return { client, calls, state, paths: () => calls.map((call) => call.path) }
}

function makeIntegration(options = {}) {
  const store = {
    records: [],
    upsert(record) {
      this.records.push(record)
      return record
    },
  }
  const integration = {
    env: {},
    dataDir: options.dataDir ?? null,
    logger: options.logger ?? null,
    llm: options.llm ?? null,
    route: options.route ?? null,
    llmConfig: options.llmConfig ?? null,
    store,
    dispatchCalls: [],
    async dispatchRuleTrigger(args) {
      this.dispatchCalls.push(args)
      if (options.dispatchError) throw options.dispatchError
      return options.dispatchResult ?? {
        ok: true,
        outcome: 'trade',
        reasonCode: 'TRADE_EXECUTED',
        round: { roundId: args.roundId, status: 'done' },
      }
    },
  }
  return integration
}

function makeEngine({
  mode = 'shadow',
  clock = makeClock(),
  timersInstance = null,
  engineConfig = {},
  clientOptions = {},
  integrationOptions = {},
  env = null,
} = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-trigger-'))
  const fake = makeFakeClient(clientOptions)
  const integration = makeIntegration({ dataDir: dir, ...integrationOptions })
  const engine = new TriggerEngine({
    integration,
    client: fake.client,
    config: { ...engineConfig },
    env: env ?? { WORKBENCH_TRIGGER_MODE: mode },
    logger: null,
    now: () => clock.now,
    timers: timersInstance?.timers ?? null,
    statePath: path.join(dir, 'trigger-engine-state.json'),
  })
  return { engine, integration, client: fake.client, calls: fake.calls, state: fake.state, store: integration.store, clock, dir, timers: timersInstance }
}

function makeLlm(value) {
  return {
    async *stream() {
      yield { type: 'text-delta', text: typeof value === 'string' ? value : JSON.stringify(value) }
      yield { type: 'finish', reason: { kind: 'stop' } }
    },
  }
}

// ---------------------------------------------------------------------------
// 三档 / 配置 / 时间网格
// ---------------------------------------------------------------------------

test('trigger engine defaults match the v3.1 contract and env config is clamped', () => {
  assert.equal(TRIGGER_ENGINE_DEFAULTS.enabled, false)
  assert.equal(TRIGGER_ENGINE_DEFAULTS.mode, 'off')
  assert.equal(TRIGGER_ENGINE_DEFAULTS.markIntervalMs, 15_000)
  assert.equal(TRIGGER_ENGINE_DEFAULTS.klineCloseDelayMs, 3_000)
  assert.equal(TRIGGER_ENGINE_DEFAULTS.confirmMode, 'off')
  assert.equal(TRIGGER_ENGINE_DEFAULTS.confirmTimeoutMs, 15_000)
  assert.equal(TRIGGER_ENGINE_DEFAULTS.maxDispatchPerTick, 1)

  const shadow = readTriggerEngineConfig({ WORKBENCH_TRIGGER_MODE: 'shadow', WORKBENCH_TRIGGER_MARK_MS: '20000' })
  assert.equal(shadow.mode, 'shadow')
  assert.equal(shadow.enabled, true)
  assert.equal(shadow.markIntervalMs, 20_000)
  // 每个字段都有硬边界：非法 mode 回落到 off，间隔被夹在 [5s, 5min]。
  assert.equal(readTriggerEngineConfig({ WORKBENCH_TRIGGER_MODE: 'nonsense' }).mode, 'off')
  assert.equal(readTriggerEngineConfig({ WORKBENCH_TRIGGER_MODE: 'live', WORKBENCH_TRIGGER_MARK_MS: '1' }).markIntervalMs, 5_000)
  const veto = readTriggerEngineConfig({ WORKBENCH_TRIGGER_CONFIRM: 'veto', WORKBENCH_TRIGGER_CONFIRM_TIMEOUT_MS: '9000' })
  assert.equal(veto.confirmMode, 'veto')
  assert.equal(veto.confirmTimeoutMs, 9_000)
  assert.equal(readTriggerEngineConfig({}, { maxDispatchPerTick: 9 }).maxDispatchPerTick, 3)
})

test('nextKlineDelay aligns to the K-line close plus delay', () => {
  assert.equal(nextKlineDelay(NOW, K15, 3_000), 3_000)
  assert.equal(nextKlineDelay(NOW + 1_000, K15, 3_000), 2_000)
  assert.equal(nextKlineDelay(NOW + 5_000, K15, 3_000), K15 - 2_000)
  assert.equal(nextKlineDelay(NOW + K15 - 100, K15, 3_000), 3_100)
})

test('off mode never scans, never archives and reports mode=off', async () => {
  const { engine, calls, store } = makeEngine({ mode: 'off' })
  const mark = await engine.tick('mark')
  assert.equal(mark.skipped, true)
  assert.equal(mark.reason, 'MODE_OFF')
  const kline = await engine.tick('kline')
  assert.equal(kline.skipped, true)
  assert.equal(calls.length, 0)
  assert.equal(store.records.length, 0)
  assert.equal(engine.status().activeMode, 'off')
  assert.equal(engine.status().enabled, false)
})

// ---------------------------------------------------------------------------
// shadow
// ---------------------------------------------------------------------------

test('shadow mode archives the trigger and never takes a lease, cycle or P1/P2', async () => {
  const second = triggeredItem({
    rule_key: 'strategy:strat-2:1',
    strategy_id: 'strat-2',
    strategy_context: { strategy_id: 'strat-2', version: 1, generation: 1, mode_revision: 2, signal_key: '15m:1994071' },
  })
  const { engine, calls, store, integration } = makeEngine({
    mode: 'shadow',
    clientOptions: { triggered: [triggeredItem(), second] },
  })
  const result = await engine.tick('mark')
  assert.equal(result.ok, true)
  assert.equal(result.mode, 'shadow')
  // 只调扫描：零租约、零 cycle、零 P1/P2、零 /rules/dispatched。
  assert.deepEqual(calls.map((call) => call.path), ['scanRules'])
  assert.equal(integration.dispatchCalls.length, 0)
  // 每次 tick 有状态与归档；maxDispatchPerTick=1，另一条记为 suppressed。
  assert.equal(store.records.length, 1)
  const record = store.records[0]
  assert.equal(record.outcome, 'shadow_triggered')
  assert.equal(record.status, 'no_trade')
  assert.equal(record.trigger, 'rule')
  assert.equal(record.cycleId, null)
  assert.equal(record.cycleSource, 'trigger')
  assert.equal(record.strategy.strategy_id, 'strat-1')
  assert.equal(record.strategy.signal_key, '15m:1994071')
  assert.equal(record.shadow.mark, 68000)
  assert.equal(record.shadow.trigger_ref, 67800)
  assert.equal(result.shadow.suppressed, 1)
  const status = engine.status()
  assert.equal(status.shadowTriggerCount, 1)
  assert.equal(status.shadowSuppressedCount, 1)
  assert.equal(status.dispatchCount, 0)
  assert.equal(status.lastTrigger.outcome, 'shadow_triggered')
  // 心跳文件（界面告警用）
  const state = JSON.parse(fs.readFileSync(engine.statePath, 'utf8'))
  assert.equal(state.contract, 'trading-workbench/trigger-engine/1')
  assert.equal(state.mode, 'shadow')
  assert.equal(state.last_scan_at_ms, NOW)
  assert.equal(state.last_scan_at, new Date(NOW).toISOString())
  assert.equal(state.lease_busy_count, 0)
  assert.equal(status.lastScanAt, new Date(NOW).toISOString())
  assert.equal(status.armedCount, 2)
  assert.equal(status.waitingCount, 0)
})

// ---------------------------------------------------------------------------
// live
// ---------------------------------------------------------------------------

test('live dispatch runs lease -> cycle(purpose=trigger) -> P1/P2 -> reported -> release in order', async () => {
  const { engine, calls, integration } = makeEngine({ mode: 'live', clientOptions: { triggered: [triggeredItem()] } })
  const result = await engine.tick('mark')
  assert.equal(result.ok, true)
  assert.equal(result.mode, 'live')
  assert.deepEqual(calls.map((call) => call.path), [
    'scanRules', 'strategy', 'acquireLease', 'createCycle', 'markRulesDispatched', 'releaseLease',
  ])

  const lease = calls.find((call) => call.path === 'acquireLease')
  assert.equal(lease.body.owner, 'host')
  assert.match(lease.body.run_id, /^cyc-/)
  assert.ok(lease.body.ttl_s >= 120)

  const cycle = calls.find((call) => call.path === 'createCycle')
  assert.equal(cycle.symbol, 'BTCUSDT')
  assert.equal(cycle.opts.purpose, 'trigger')
  assert.deepEqual(cycle.opts.symbols, ['BTCUSDT'])
  assert.equal(cycle.opts.trigger, 'auto')

  const reported = calls.find((call) => call.path === 'markRulesDispatched')
  assert.equal(reported.body.rule_key, 'strategy:strat-1:2')
  assert.equal(reported.body.symbol, 'BTCUSDT')
  assert.equal(reported.body.outcome, 'dispatched')
  assert.equal(reported.body.at_ms, NOW)

  // P1/P2 只由 integration.dispatchRuleTrigger 走一遍（触发引擎不自己调 P1/P2）。
  assert.equal(integration.dispatchCalls.length, 1)
  const dispatch = integration.dispatchCalls[0]
  assert.equal(dispatch.leaseId, 'lease-1')
  assert.equal(dispatch.cycleCreated, true)
  assert.equal(dispatch.strategyRules, RULES)
  assert.equal(dispatch.trigger, 'rule')
  assert.deepEqual(dispatch.strategyContext, triggeredItem().strategy_context)
  assert.equal(dispatch.confirm, null)

  assert.equal(engine.status().lastTrigger.outcome, 'dispatched')
  assert.equal(engine.status().lastTrigger.reason_code, 'TRADE_EXECUTED')
  assert.equal(engine.status().dispatchCount, 1)
  assert.equal(engine.status().lastError, null)
  // 释放的一定是本次拿到的租约
  assert.deepEqual(calls.at(-1).body, { lease_id: 'lease-1' })
})

test('an exception inside the dispatch still releases the lease and reports rejected', async () => {
  const boom = Object.assign(new Error('round ownership was revoked'), { code: 'HOST_RUN_ABANDONED' })
  const { engine, calls, store } = makeEngine({
    mode: 'live',
    clientOptions: { triggered: [triggeredItem({ rules: RULES })] },
    integrationOptions: { dispatchError: boom },
  })
  const result = await engine.tick('mark')
  assert.equal(result.ok, true)
  assert.deepEqual(calls.map((call) => call.path), [
    'scanRules', 'acquireLease', 'createCycle', 'markRulesDispatched', 'releaseLease',
  ])
  assert.deepEqual(calls.at(-1).body, { lease_id: 'lease-1' })
  const reported = calls.find((call) => call.path === 'markRulesDispatched')
  assert.equal(reported.body.outcome, 'rejected')
  assert.equal(reported.body.reason_code, 'HOST_RUN_ABANDONED')
  assert.equal(result.dispatched[0].dispatched, false)
  assert.equal(engine.status().lastError.code, 'HOST_RUN_ABANDONED')
  assert.equal(store.records.length, 0, '规则轮次的归档由 dispatchRuleTrigger 负责，引擎不重复写')
})

test('a throwing releaseLease does not lose the dispatch result', async () => {
  const { engine, calls } = makeEngine({
    mode: 'live',
    clientOptions: {
      triggered: [triggeredItem({ rules: RULES })],
      releaseError: Object.assign(new Error('lease already gone'), { code: 'E_LEASE_STALE', status: 409 }),
    },
  })
  const result = await engine.tick('mark')
  assert.equal(result.ok, true)
  assert.equal(result.dispatched[0].dispatched, true)
  assert.equal(calls.at(-1).path, 'releaseLease')
})

// ---------------------------------------------------------------------------
// 租约 / cycle 忙
// ---------------------------------------------------------------------------

test('E_LEASE_HELD skips the tick, counts it, and retries on the next tick without killing anyone', async () => {
  const clock = makeClock()
  const { engine, calls, state, integration } = makeEngine({
    mode: 'live',
    clock,
    clientOptions: { triggered: [triggeredItem({ rules: RULES })] },
  })
  state.leaseError = Object.assign(new Error('another Host holds the lease'), { code: 'E_LEASE_HELD', status: 409 })
  const first = await engine.tick('mark')
  assert.equal(first.dispatched[0].dispatched, false)
  assert.equal(first.dispatched[0].reason, 'E_LEASE_HELD')
  assert.equal(engine.status().leaseBusyCount, 1)
  assert.equal(engine.status().cycleBusyCount, 0)
  assert.equal(calls.filter((call) => call.path === 'createCycle').length, 0)
  assert.equal(calls.filter((call) => call.path === 'finalizeCycle').length, 0)
  assert.equal(calls.filter((call) => call.path === 'releaseLease').length, 0, '没拿到的租约不能释放')
  assert.equal(integration.dispatchCalls.length, 0)
  assert.equal(calls.find((call) => call.path === 'markRulesDispatched').body.outcome, 'rejected')
  assert.equal(engine.status().pendingRetry.ruleKey, 'strategy:strat-1:2')

  // 下个 tick：服务端不会重复给同一条边沿，Host 用 pendingRetry 重试。
  state.leaseError = null
  state.triggered = []
  clock.advance(15_000)
  const second = await engine.tick('mark')
  assert.equal(second.dispatched.length, 1)
  assert.equal(second.dispatched[0].dispatched, true)
  assert.equal(second.dispatched[0].retried, true)
  assert.equal(integration.dispatchCalls.length, 1)
  assert.equal(engine.status().leaseBusyCount, 1)
  assert.equal(engine.status().pendingRetry, null)
})

test('E_CYCLE_RUNNING skips the tick, releases our lease and never finalizes the other cycle', async () => {
  const { engine, calls, state, integration } = makeEngine({
    mode: 'live',
    clientOptions: { triggered: [triggeredItem({ rules: RULES })] },
  })
  state.cycleError = Object.assign(new Error('a research cycle is RUNNING'), {
    code: 'E_CYCLE_RUNNING',
    status: 409,
  })
  const result = await engine.tick('mark')
  assert.equal(result.dispatched[0].dispatched, false)
  assert.equal(result.dispatched[0].reason, 'E_CYCLE_RUNNING')
  assert.equal(engine.status().cycleBusyCount, 1)
  assert.equal(engine.status().leaseBusyCount, 0)
  // 自己的租约必须释放；别人的 cycle 一个都不能动。
  assert.deepEqual(calls.filter((call) => call.path === 'releaseLease').length, 1)
  assert.deepEqual(calls.filter((call) => call.path === 'finalizeCycle'), [])
  assert.equal(integration.dispatchCalls.length, 0)
  assert.equal(calls.find((call) => call.path === 'markRulesDispatched').body.outcome, 'rejected')
  assert.equal(engine.status().pendingRetry.attempts, 1)
})

test('maxDispatchPerTick keeps a single dispatch per tick even with several triggered rules', async () => {
  const second = triggeredItem({
    rule_key: 'strategy:strat-2:1',
    strategy_id: 'strat-2',
    strategy_context: { strategy_id: 'strat-2', version: 1, generation: 1, mode_revision: 2, signal_key: '15m:1994071' },
    rules: RULES,
  })
  const single = makeEngine({
    mode: 'live',
    clientOptions: { triggered: [triggeredItem({ rules: RULES }), second] },
  })
  const res = await single.engine.tick('mark')
  assert.equal(res.scan.triggered, 2)
  assert.equal(single.integration.dispatchCalls.length, 1)
  assert.equal(single.engine.status().lastTrigger.rule_key, 'strategy:strat-1:2')

  const both = makeEngine({
    mode: 'live',
    engineConfig: { maxDispatchPerTick: 2 },
    clientOptions: { triggered: [triggeredItem({ rules: RULES }), second] },
  })
  const res2 = await both.engine.tick('mark')
  assert.equal(res2.dispatched.filter((entry) => entry.dispatched).length, 2)
  assert.equal(both.integration.dispatchCalls.length, 2)
})

// ---------------------------------------------------------------------------
// 定时器
// ---------------------------------------------------------------------------

test('start() arms the mark and kline timers, ticks re-arm themselves, and stop() leaves none behind', async () => {
  const clock = makeClock()
  const timers = makeTimers()
  const { engine, calls } = makeEngine({ mode: 'shadow', clock, timersInstance: timers })
  engine.start()
  assert.equal(engine.status().started, true)
  assert.equal(timers.size(), 2)
  assert.deepEqual(timers.delays().sort((a, b) => a - b), [3_000, 15_000])

  clock.advance(15_000)
  assert.equal(timers.fireDelay(15_000), true)
  await flush()
  await flush()
  assert.equal(calls.filter((call) => call.path === 'scanRules').length, 1)
  assert.equal(calls[0].body.scope, 'mark')
  assert.equal(timers.size(), 2, 'mark tick 之后必须重新武装')

  engine.stop()
  assert.equal(timers.size(), 0)
  assert.equal(engine.status().started, false)
  assert.equal(engine.status().next.mark_at, null)
  assert.equal(engine.status().next.kline_at, null)
  // stop 之后再怎么推进时钟都不会有定时器触发扫描（tick() 本身是手动入口，不在此列）。
  clock.advance(10 * 60_000)
  assert.equal(timers.size(), 0)
  await flush()
  assert.equal(calls.filter((call) => call.path === 'scanRules').length, 1)
})

test('a kline tick asks the service for scope=all right after the close', async () => {
  const clock = makeClock(NOW + K15 - 100)
  const timers = makeTimers()
  const { engine, calls } = makeEngine({ mode: 'shadow', clock, timersInstance: timers })
  engine.start()
  assert.deepEqual(timers.delays().sort((a, b) => a - b), [100 + 3_000, 15_000])
  clock.advance(3_100)
  timers.fireDelay(3_100)
  await flush()
  await flush()
  const scan = calls.find((call) => call.path === 'scanRules')
  assert.ok(scan)
  assert.equal(scan.body.scope, 'all')
  assert.equal(scan.body.scan_interval_ms, 15_000)
  engine.stop()
})

test('overlapping ticks are skipped instead of stacking', async () => {
  let release = null
  const gate = new Promise((resolve) => { release = resolve })
  const fake = makeEngine({ mode: 'shadow', clientOptions: { triggered: [] } })
  const original = fake.client.scanRules
  fake.client.scanRules = async (body) => {
    await gate
    return original(body)
  }
  const first = fake.engine.tick('mark')
  const second = await fake.engine.tick('mark')
  assert.equal(second.skipped, true)
  assert.equal(second.reason, 'TICK_IN_PROGRESS')
  release()
  const done = await first
  assert.equal(done.ok, true)
  assert.equal(fake.engine.status().skippedTicks, 1)
})

// ---------------------------------------------------------------------------
// 心跳 / 状态 / 失败
// ---------------------------------------------------------------------------

test('status() exposes the heartbeat, armed counts and busy counters the UI needs', async () => {
  const clock = makeClock()
  const fake = makeEngine({ mode: 'shadow', clock })
  fake.state.waiting = [
    { rule_key: 'strategy:strat-9:1', symbol: 'ETHUSDT', distance_pct: -0.42, blocked_by: 'level_hold' },
  ]
  await fake.engine.tick('mark')
  const status = fake.engine.status()
  for (const key of [
    'mode', 'activeMode', 'lastScanAt', 'armedCount', 'waitingCount',
    'lastTrigger', 'lastError', 'leaseBusyCount', 'ping',
  ]) {
    if (key === 'ping') continue
    assert.ok(key in status, 'status() missing ' + key)
  }
  assert.equal(status.lastScanAtMs, NOW)
  assert.equal(status.waitingCount, 1)
  assert.equal(status.waitingMinDistancePct, 0.42)
  assert.equal(status.armedCount, 1)
  assert.equal(status.heartbeat.stale, false)
  assert.equal(status.heartbeat.max_age_ms, 60_000)

  clock.advance(61_000)
  assert.equal(fake.engine.status().heartbeat.stale, true, '超过 60s 没有扫描必须告警')
  fake.engine.setMode('off')
  const offStatus = fake.engine.status()
  assert.equal(offStatus.activeMode, 'off')
  assert.equal(offStatus.heartbeat.stale, false, 'off 模式不该报心跳告警')
})

test('a failing scan records lastError and never throws out of tick()', async () => {
  const fake = makeEngine({
    mode: 'live',
    clientOptions: { scanError: Object.assign(new Error('service offline'), { code: 'E_SERVICE_OFFLINE' }) },
  })
  const result = await fake.engine.tick('mark')
  assert.equal(result.ok, false)
  assert.equal(result.error.code, 'E_SERVICE_OFFLINE')
  assert.equal(fake.engine.status().lastError.code, 'E_SERVICE_OFFLINE')
  assert.equal(fake.calls.filter((call) => call.path === 'acquireLease').length, 0)
  const state = JSON.parse(fs.readFileSync(fake.engine.statePath, 'utf8'))
  assert.equal(state.last_error.code, 'E_SERVICE_OFFLINE')
})

test('a service without /api/rules/dispatched still dispatches and records the report failure', async () => {
  const fake = makeEngine({
    mode: 'live',
    clientOptions: { triggered: [triggeredItem({ rules: RULES })], dispatchedOk: false },
  })
  const result = await fake.engine.tick('mark')
  assert.equal(result.dispatched[0].dispatched, true)
  assert.equal(fake.engine.status().lastDispatchReportError.code, 'E_SERVICE_HTTP_404')
  assert.equal(fake.calls.at(-1).path, 'releaseLease', '回写失败也必须释放租约')
})

test('rules missing from the scan entry are fetched from the strategy detail before dispatching', async () => {
  const fake = makeEngine({ mode: 'live', clientOptions: { triggered: [triggeredItem()] } })
  await fake.engine.tick('mark')
  const detail = fake.calls.find((call) => call.path === 'strategy')
  assert.equal(detail.suffix, '/strat-1')
  assert.equal(fake.integration.dispatchCalls[0].strategyRules, RULES)

  const broken = makeEngine({
    mode: 'live',
    clientOptions: {
      triggered: [triggeredItem()],
      strategyError: Object.assign(new Error('strategy gone'), { code: 'E_STRATEGY_NOT_FOUND', status: 404 }),
    },
  })
  const res = await broken.engine.tick('mark')
  assert.equal(res.dispatched[0].dispatched, false)
  assert.equal(res.dispatched[0].reason, 'E_STRATEGY_NOT_FOUND')
  assert.equal(broken.calls.filter((call) => call.path === 'acquireLease').length, 0)
})

// ---------------------------------------------------------------------------
// trigger_confirm（fail-closed）
// ---------------------------------------------------------------------------

test('the veto hook is attached only for ai_assisted rules when confirmMode=veto', async () => {
  const ruleDirect = makeEngine({
    mode: 'live',
    engineConfig: { confirmMode: 'veto' },
    clientOptions: { triggered: [triggeredItem({ rules: RULES })] },
  })
  await ruleDirect.engine.tick('mark')
  assert.equal(ruleDirect.integration.dispatchCalls[0].confirm, null, 'rule_direct 必须 0 LLM')

  const aiRules = { ...RULES, execution_mode: 'ai_assisted' }
  const aiAssisted = makeEngine({
    mode: 'live',
    engineConfig: { confirmMode: 'veto' },
    clientOptions: { triggered: [triggeredItem({ execution_mode: 'ai_assisted', rules: aiRules })] },
  })
  await aiAssisted.engine.tick('mark')
  assert.equal(typeof aiAssisted.integration.dispatchCalls[0].confirm, 'function')

  const confirmOff = makeEngine({
    mode: 'live',
    clientOptions: { triggered: [triggeredItem({ execution_mode: 'ai_assisted', rules: aiRules })] },
  })
  await confirmOff.engine.tick('mark')
  assert.equal(confirmOff.integration.dispatchCalls[0].confirm, null, 'confirmMode=off 时不调用模型')
})

test('trigger_confirm fails closed: no route, malformed verdict, exception and timeout all veto', async () => {
  const clock = makeClock()
  const context = {
    strategy: { rules: RULES, name: '复核' },
    strategyContext: triggeredItem().strategy_context,
    plan: { symbol: 'BTCUSDT', side: 'LONG', leverage: 5, sl: 66640, tp: 70720 },
    cycleId: 'cyc-test',
    item: triggeredItem(),
  }

  // 1) 没有可用模型路由 → veto
  const noRoute = makeEngine({ mode: 'live', clock })
  const noRouteVerdict = await noRoute.engine.confirmTrigger(context)
  assert.equal(noRouteVerdict.verdict, 'veto')
  assert.equal(noRouteVerdict.reason_code, 'NO_ADAPTER')

  // 2) 模型说 proceed → proceed
  const ok = makeEngine({
    mode: 'live',
    clock,
    integrationOptions: { llm: makeLlm({ verdict: 'proceed' }), route: { provider: 'fake', model: 'fake-1' } },
  })
  const proceed = await ok.engine.confirmTrigger(context)
  assert.equal(proceed.verdict, 'proceed')
  assert.equal(proceed.reason_code, null)

  // 3) 模型说 veto → veto（理由透传）
  const vetoEngine = makeEngine({
    mode: 'live',
    clock,
    integrationOptions: {
      llm: makeLlm({ verdict: 'veto', reason_code: 'DATA_CONFLICT', note: '收盘价与标记价矛盾' }),
      route: { provider: 'fake', model: 'fake-1' },
    },
  })
  const veto = await vetoEngine.engine.confirmTrigger(context)
  assert.equal(veto.verdict, 'veto')
  assert.equal(veto.reason_code, 'DATA_CONFLICT')

  // 4) 输出不合 schema → veto
  const weird = makeEngine({
    mode: 'live',
    clock,
    integrationOptions: { llm: makeLlm({ verdict: 'maybe' }), route: { provider: 'fake', model: 'fake-1' } },
  })
  const weirdVerdict = await weird.engine.confirmTrigger(context)
  assert.equal(weirdVerdict.verdict, 'veto')
  assert.equal(weirdVerdict.reason_code, 'E_TRIGGER_CONFIRM_INVALID')

  // 5) 模型异常 → veto
  const broken = makeEngine({
    mode: 'live',
    clock,
    integrationOptions: {
      llm: {
        stream() {
          throw Object.assign(new Error('provider exploded'), { code: 'LLM_TRANSPORT' })
        },
      },
      route: { provider: 'fake', model: 'fake-1' },
    },
  })
  const brokenVerdict = await broken.engine.confirmTrigger(context)
  assert.equal(brokenVerdict.verdict, 'veto')
  assert.equal(brokenVerdict.reason_code, 'LLM_TRANSPORT')

  // 6) 超时 → veto（fail-closed）。用立即触发的假定时器，不必等 15s。
  const immediateTimers = {
    timers: {
      setTimeout(fn) {
        Promise.resolve().then(fn)
        return 1
      },
      clearTimeout() {},
    },
  }
  const hangingLlm = {
    async *stream() {
      await new Promise(() => {})
    },
  }
  const slow = makeEngine({
    mode: 'live',
    clock,
    timersInstance: immediateTimers,
    engineConfig: { confirmTimeoutMs: 1_000 },
    integrationOptions: { llm: hangingLlm, route: { provider: 'fake', model: 'fake-1' } },
  })
  const timedOut = await slow.engine.confirmTrigger(context)
  assert.equal(timedOut.verdict, 'veto')
  assert.equal(timedOut.reason_code, 'E_TRIGGER_CONFIRM_TIMEOUT')
})

test('createTriggerEngine exposes the same class and refuses an integration without dispatchRuleTrigger', () => {
  const integration = makeIntegration()
  const engine = createTriggerEngine({ integration, client: makeFakeClient().client, env: { WORKBENCH_TRIGGER_MODE: 'shadow' } })
  assert.ok(engine instanceof TriggerEngine)
  assert.equal(engine.status().activeMode, 'shadow')
  assert.throws(() => new TriggerEngine({ integration: {} }), /dispatchRuleTrigger/)
})
