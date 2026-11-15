/** Offline architecture reproductions; reuses existing fake seams, never real HTTP/LLM. */
import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'
import { randomUUID } from 'node:crypto'
import { fileURLToPath, pathToFileURL } from 'node:url'
import { createIntegration } from '../plugin/host/integration.js'
import { createAutoScheduler } from '../plugin/host/scheduler.js'
import { resolveHarnessRoute } from '../plugin/host/ai/llm.js'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const source = fs.readFileSync(path.join(root, 'plugin/test/host-integration.test.mjs'), 'utf8')
const prefix = source.slice(0, source.indexOf('\ntest('))
const fixturePath = path.join(root, 'plugin/test', `.architecture-fixtures-${randomUUID()}.mjs`)
fs.writeFileSync(fixturePath, `${prefix}\nexport { makeIntegration, makeFakeLlm, makeFakeService, NOW };\n`, { flag: 'wx' })
const emit = (finding, facts) => console.log(JSON.stringify({ finding, ...facts }))

try {
  const { makeIntegration, makeFakeLlm, makeFakeService, NOW } = await import(pathToFileURL(fixturePath).href)
  let release
  const gate = new Promise((resolve) => { release = resolve })
  const fake = makeFakeLlm()
  const original = fake.llm.stream.bind(fake.llm)
  let first = true
  fake.llm.stream = (options) => {
    const stream = original(options)
    return (async function* () {
      if (first) { first = false; await gate }
      yield* stream
    })()
  }
  const env = makeIntegration({ fakeLlm: fake, service: makeFakeService({ credentials: true }), runTimeoutMs: 50 })
  const pending = env.integration.run()
  await new Promise((resolve) => setTimeout(resolve, 1200))
  const abandoned = env.integration.store.list()[0]
  assert.equal(abandoned.status, 'failed')
  assert.equal(env.integration.inFlight, null)
  const timedOutCode = abandoned.reasonCode
  release()
  const late = await pending
  const submits = env.service.calls.filter((call) => call.path === '/api/execution/order').length
  assert.equal(submits, 1)
  emit('watchdog_zombie_execution', { timeout_status: 'failed', timeout_code: timedOutCode, lock_released: true, later_p2_submits: submits, final_status: late.round.status })

  let selected = { provider: 'internal-provider', model: 'model-a' }
  const ctx = { llm: {}, agentDefaultModel: { currentSelection: () => selected } }
  const routing = createIntegration({ ctx, dataDir: env.dir, tokenFile: env.tokenFile })
  selected = { provider: 'internal-provider', model: 'model-b' }
  assert.equal(resolveHarnessRoute(ctx).model, 'model-b')
  assert.equal(routing.route.model, 'model-a')
  emit('llm_route_snapshot', { current_harness_model: 'model-b', workbench_route: routing.route.model })

  const budget = makeIntegration()
  for (let i = 0; i < 96; i++) budget.integration.store.upsert({ roundId: `auto-audit-${i}`, trigger: 'auto', startedAt: new Date(NOW - i * 1000).toISOString(), status: 'done' })
  const scheduler = createAutoScheduler({ integration: budget.integration, now: () => NOW })
  assert.equal(scheduler.status().rounds_last_24h, 96)
  for (let i = 0; i < 5; i++) budget.integration.store.upsert({ roundId: `manual-audit-${i}`, trigger: 'manual', startedAt: new Date(NOW).toISOString(), status: 'done' })
  const afterFive = scheduler.status().rounds_last_24h
  assert.equal(afterFive, 95)
  for (let i = 5; i < 100; i++) budget.integration.store.upsert({ roundId: `manual-audit-${i}`, trigger: 'manual', startedAt: new Date(NOW).toISOString(), status: 'done' })
  assert.equal(scheduler.status().rounds_last_24h, 0)
  emit('budget_uses_ui_cache', { actual_auto_rounds_in_window: 96, counted_after_five_manual_rounds: afterFive, displayed_after_manual_eviction: 0, retained_rows: budget.integration.store.list().length })

  // Feed the real ReconcilePanel a Service-shaped stale response, using the
  // existing offline renderer only up to its first reconciliation result.
  const rendererPath = path.join(root, 'plugin/client/render-smoke.mjs')
  const renderer = fs.readFileSync(rendererPath, 'utf8').replace(/\r\n/g, '\n')
  const cutoff = renderer.indexOf("expect('H7b reconcile result")
  assert.ok(cutoff > 0)
  let reproduction = renderer.slice(0, cutoff)
  const oldShape = "ok: true,\n        adopted: [],\n        closed: [],\n        protection_missing: ['ETHUSDT'],\n        outbox: { pending: 1, uncertain: 0 },"
  assert.ok(reproduction.includes(oldShape))
  reproduction = reproduction.replace(oldShape, "exchange_reachable: false, stale: true, adopted: [], closed_external: ['BTCUSDT'], protection_missing: [], outbox: { checked: 1, resolved: 0, errors: 1 },")
  reproduction += `\nif (!reconDone.includes('本地账本与交易所一致')) throw new Error('stale UI counterexample no longer reproduces');\nconsole.log(JSON.stringify({finding:'reconcile_contract_false_success', exchange_reachable:false, stale:true, unresolved_queries:1, external_closes:1, rendered_verdict:'本地账本与交易所一致'}));\n`
  const renderAuditPath = path.join(root, 'plugin/client', `.architecture-render-${randomUUID()}.mjs`)
  try {
    fs.writeFileSync(renderAuditPath, reproduction, { flag: 'wx' })
    await import(pathToFileURL(renderAuditPath).href)
  } finally { fs.unlinkSync(renderAuditPath) }
} finally {
  fs.unlinkSync(fixturePath)
}
