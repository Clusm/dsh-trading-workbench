import test from 'node:test'
import assert from 'node:assert/strict'
import { StrategyAssistant, STRATEGY_WHITELIST_SYMBOLS, eligibleStrategies } from '../host/strategies.js'

const rules = { symbols: ['BTCUSDT'], timeframes: ['15m'], entry: { kind: 'mark_threshold', side: 'long', operator: 'gt', value: 100 } }
const candidate = (id, overrides = {}) => ({
  strategy_id: id,
  name: id,
  current_version: 1,
  generation: 2,
  enabled: true,
  confirmed_version: { rules, validation: { execution_ready: true } },
  ...overrides,
})
const llm = (value, onCall = null) => ({
  async *stream(opts) {
    onCall?.(opts)
    yield { type: 'text-delta', text: typeof value === 'string' ? value : JSON.stringify(value) }
    yield { type: 'finish', reason: { kind: 'stop' } }
  },
})
const route = { provider: 'fake', model: 'test' }

test('candidate prefilter rejects stale/unknown prices and inactive/unsupported strategies', () => {
  const now = Date.now()
  const q = { BTCUSDT: { mark: 101, as_of_ms: now, stale: false } }
  assert.equal(eligibleStrategies([candidate('a')], q, now).length, 1)
  assert.equal(eligibleStrategies([{ ...candidate('a'), enabled: false }], q, now).length, 0)
  assert.equal(eligibleStrategies([candidate('a')], { BTCUSDT: { ...q.BTCUSDT, stale: true } }, now).length, 0)
  assert.equal(eligibleStrategies([candidate('a')], q, now + 6000).length, 0)
  assert.equal(eligibleStrategies([candidate('a')], { BTCUSDT: { mark: 99, as_of_ms: now, stale: false } }, now).length, 0)
})

test('eligibleStrategies supports quote.age_ms, gt/lt/gte/lte operators, and consumed signal keys', () => {
  const now = 1_700_000_000_000
  const signalKey = `15m:${Math.floor(now / 900_000)}`
  // age_ms provided by service without as_of_ms
  assert.equal(eligibleStrategies([candidate('a')], { BTCUSDT: { mark: 101, age_ms: 1200, stale: false } }, now).length, 1)
  assert.equal(eligibleStrategies([candidate('a')], { BTCUSDT: { mark: 101, age_ms: 5500, stale: false } }, now).length, 0)
  // null age_ms must NOT coerce to 0ms
  assert.equal(eligibleStrategies([candidate('a')], { BTCUSDT: { mark: 101, age_ms: null, as_of_ms: now - 7000, stale: false } }, now).length, 0)

  // gte / lte / lt operators
  const makeOp = (operator, value) => candidate(`op-${operator}`, {
    confirmed_version: {
      validation: { execution_ready: true },
      rules: { symbols: ['BTCUSDT'], timeframes: ['15m'], entry: { kind: 'mark_threshold', side: 'short', operator, value } },
    },
  })
  const exactQuote = { BTCUSDT: { mark: 100, age_ms: 100, stale: false } }
  assert.equal(eligibleStrategies([makeOp('gte', 100)], exactQuote, now).length, 1)
  assert.equal(eligibleStrategies([makeOp('lte', 100)], exactQuote, now).length, 1)
  assert.equal(eligibleStrategies([makeOp('lt', 101)], exactQuote, now).length, 1)
  assert.equal(eligibleStrategies([makeOp('lt', 100)], exactQuote, now).length, 0)

  // Consumed signal key filtering via parameter or strategy.consumed_signal_keys
  const fullKey = `a:1:BTCUSDT:LONG:${signalKey}`
  assert.equal(eligibleStrategies([candidate('a')], { BTCUSDT: { mark: 105, age_ms: 50, stale: false } }, now, new Set([fullKey])).length, 0)
  assert.equal(
    eligibleStrategies([candidate('a', { consumed_signal_keys: [fullKey] })], { BTCUSDT: { mark: 105, age_ms: 50, stale: false } }, now).length,
    0,
  )
})

test('no candidate means no model call; selection cannot escape authorized set', async () => {
  const assistant = new StrategyAssistant({ llm: llm({ strategy_id: 'evil', reason: 'x' }), route })
  assert.equal((await assistant.choose([], {})).strategy, null)
  assert.equal((await assistant.choose([candidate('a')], {})).strategy.strategy_id, 'a')
  await assert.rejects(assistant.choose([candidate('a'), candidate('b')], {}), (e) => e.code === 'E_STRATEGY_UNAUTHORIZED_SELECTION')
})

test('choose handles null skip, malformed JSON/arrays, and per-call dynamic route override', async () => {
  const seen = []
  const assistant = new StrategyAssistant({ llm: null, route: null })
  await assert.rejects(
    assistant.choose([candidate('a'), candidate('b')], {}),
    (e) => e.code === 'ROUTE_UNAVAILABLE' && e.status === 503,
  )

  const skipChoice = await assistant.choose(
    [candidate('a'), candidate('b')],
    {},
    {
      llm: llm({ strategy_id: null, reason: '波动过大，本轮跳过' }, (opts) => seen.push(opts)),
      route: { provider: 'dyn-p', model: 'dyn-m' },
    },
  )
  assert.equal(skipChoice.strategy, null)
  assert.equal(skipChoice.reason, '波动过大，本轮跳过')
  assert.equal(seen[0].provider, 'dyn-p')
  assert.equal(seen[0].model, 'dyn-m')

  await assert.rejects(
    assistant.choose([candidate('a'), candidate('b')], {}, { llm: llm('not-json{{{'), route }),
    (e) => e.code === 'E_STRATEGY_SELECTION_INVALID' && e.status === 422,
  )
  await assert.rejects(
    assistant.choose([candidate('a'), candidate('b')], {}, { llm: llm(['array']), route }),
    (e) => e.code === 'E_STRATEGY_SELECTION_INVALID' && e.status === 422,
  )
  await assert.rejects(
    assistant.choose([candidate('a'), candidate('b')], {}, { llm: llm({ strategy_id: 123 }), route }),
    (e) => e.code === 'E_STRATEGY_UNAUTHORIZED_SELECTION' && e.status === 409,
  )
})

test('clarification preserves original/answers, enforces Phase-2 prompt contract, and supports dynamic route', async () => {
  assert.equal(STRATEGY_WHITELIST_SYMBOLS.length, 20)
  const draft = { revision: 3, source_text: '用户原文', answers: { q: '回答' }, questions: [] }
  const calls = []
  let capturedPrompt = null
  const client = {
    async strategy(path, opts) {
      calls.push({ path, opts })
      if (!opts) return { strategy: { draft } }
      return { strategy: { draft: opts.body } }
    },
  }
  const assistant = new StrategyAssistant({ llm: null, route: null })
  // Missing route rejected cleanly with 503 ROUTE_UNAVAILABLE
  await assert.rejects(assistant.analyze(client, 'a', 3), (e) => e.code === 'ROUTE_UNAVAILABLE' && e.status === 503)

  const dynamicLlm = llm(
    { summary: '理解', rules, questions: [], suggestions: [], sources: { entry: '原文' } },
    (opts) => { capturedPrompt = opts },
  )
  const result = await assistant.analyze(client, 'a', 3, {
    llm: dynamicLlm,
    route: { provider: 'live-provider', model: 'live-model' },
  })
  assert.equal(capturedPrompt.provider, 'live-provider')
  assert.equal(capturedPrompt.model, 'live-model')
  // Verify Phase-2 capabilities in clarification system prompt
  for (const token of [
    'candle_close', 'ema_cross', 'ema_compare', 'rsi_threshold', 'funding_threshold', '"all"',
    'rule_direct', 'ai_assisted', 'circuit_breaker', '0.002', '0.05', '1.2', 'klines', 'funding_rate', 'ZECUSDT', 'DOTUSDT',
  ]) {
    assert.ok(capturedPrompt.system.includes(token), `clarification prompt missing ${token}`)
  }
  assert.equal(result.strategy.draft.source_text, '用户原文')
  assert.equal(result.strategy.draft.answers.q, '回答')
  assert.equal(calls.at(-1).opts.body.revision, 3)
  await assert.rejects(assistant.analyze(client, 'a', 2, { llm: dynamicLlm, route }), (e) => e.status === 409 && e.code === 'E_STRATEGY_REVISION')
})

test('missing draft returns 409 E_STRATEGY_NO_DRAFT and malformed JSON does not overwrite draft', async () => {
  const assistant = new StrategyAssistant({ llm: llm('not valid json'), route })
  const emptyDraftClient = { async strategy() { return { strategy: { draft: null } } } }
  await assert.rejects(
    assistant.analyze(emptyDraftClient, 'a', 1),
    (e) => e.status === 409 && e.code === 'E_STRATEGY_NO_DRAFT',
  )

  let writes = 0
  const client = {
    async strategy(path, opts) {
      if (opts) { writes++; return {} }
      return { strategy: { draft: { revision: 1, source_text: 'x', answers: {} } } }
    },
  }
  await assert.rejects(
    assistant.analyze(client, 'a', 1),
    (e) => e.status === 422 && e.code === 'E_STRATEGY_ANALYSIS_INVALID',
  )
  const badShapeAssistant = new StrategyAssistant({ llm: llm({ summary: 'x', questions: [] }), route })
  await assert.rejects(
    badShapeAssistant.analyze(client, 'a', 1),
    (e) => e.status === 422 && e.code === 'E_STRATEGY_ANALYSIS_INVALID',
  )
  assert.equal(writes, 0)
  assert.equal(assistant.busy.size, 0)
})
