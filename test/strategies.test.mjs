import test from 'node:test'
import assert from 'node:assert/strict'
import { StrategyAssistant, STRATEGY_WHITELIST_SYMBOLS, pickOrderedCandidate } from '../host/strategies.js'
import * as strategiesModule from '../host/strategies.js'

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

// v3.1（docs/18 §2 S4/S9，docs/19 §3.1）：多候选排序由服务端确定性给出，Host 只取第 1 条。
// 旧实现里的 StrategyAssistant.choose（模型选策略）与 eligibleStrategies（404 回退）已删除，
// 这里断言的是新的契约，而不是旧行为。

test('strategy module surface: no model-based selection, no legacy eligible fallback', () => {
  assert.equal(typeof StrategyAssistant.prototype.choose, 'undefined')
  assert.equal('eligibleStrategies' in strategiesModule, false)
  assert.equal(typeof pickOrderedCandidate, 'function')
  assert.equal(STRATEGY_WHITELIST_SYMBOLS.length, 20)
})

test('pickOrderedCandidate takes the first server-ordered candidate and never re-sorts', () => {
  const first = candidate('server-first', { priority: 2 })
  const second = candidate('server-second', { priority: 9 })
  const picked = pickOrderedCandidate([first, second])
  assert.equal(picked.strategy.strategy_id, 'server-first')
  assert.equal(picked.candidates, 2)
  assert.match(picked.reason, /服务端确定性排序/)

  const single = pickOrderedCandidate([candidate('only')])
  assert.equal(single.strategy.strategy_id, 'only')
  assert.equal(single.reason, '唯一满足条件的已授权策略')

  // 空候选、非法条目都不会让 Host 去调模型：只有 null 结果和一句原因。
  assert.equal(pickOrderedCandidate([]).strategy, null)
  assert.equal(pickOrderedCandidate(null).strategy, null)
  assert.equal(pickOrderedCandidate([null, 'x', 0, candidate('kept')]).strategy.strategy_id, 'kept')
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
    assert.ok(capturedPrompt.system.includes(token), 'clarification prompt missing ' + token)
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
