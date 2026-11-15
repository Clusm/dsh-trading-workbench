/**
 * Render regression for reason redaction (t88 repair round 2).
 *
 * Usage: node trading-workbench/plugin/test/render-smoke.mjs
 *
 * The canonical render harness lives in ../client/render-smoke.mjs and runs as a
 * script (it does not export its harness). This test therefore executes a
 * temporary copy of that file with one extra scenario inserted before its report
 * block, so the copy still resolves ../client.js and keeps the whole existing
 * suite as a regression gate. The extra scenario forces the client fallback path:
 * `reasonDetail` is empty while `execution.rationale` contains token/Bearer/
 * api_key/sk- strings, and it asserts the rendered UI only shows `[redacted]`.
 *
 * The Host DTO is asserted directly: toRoundView() must sanitize both
 * reasonDetail and execution.rationale.
 */

import { readFileSync, writeFileSync, unlinkSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { spawnSync } from 'node:child_process'
import { join } from 'node:path'
import { toRoundView } from '../host/round-view.js'

const failures = []
const notes = []
function expect(label, condition, detail) {
  if (condition) notes.push(`ok   ${label}`)
  else failures.push(`${label}${detail ? ` — ${detail}` : ''}`)
}

const SECRET_RATIONALE =
  'blocked: token=sk-abcdef123456 rejected; Authorization: Bearer abc.def.ghi; ' +
  'api_key=plain-secret-key; retry with Bearer qqq.www'
const RAW_SECRETS = /sk-abcdef123456|abc\.def\.ghi|plain-secret-key|qqq\.www/

// ---- Host DTO ------------------------------------------------------------
const view = toRoundView({
  roundId: 'cyc-secret',
  cycleId: 'cyc-secret',
  status: 'failed',
  outcome: 'no_trade',
  reasonCode: 'EXECUTION_BLOCKED:ACCOUNT_UNAVAILABLE',
  result: {
    detail: null,
    outputs: {
      execution: {
        executable: false,
        blocked_reason: 'ACCOUNT_UNAVAILABLE',
        rationale: SECRET_RATIONALE,
      },
    },
    calls: [],
  },
})
const dto = JSON.stringify({ reasonDetail: view.reasonDetail, rationale: view.execution?.rationale })
expect('T1 Host DTO redacts reasonDetail and execution.rationale',
  !RAW_SECRETS.test(dto) && /\[redacted\]/.test(dto), dto)
expect('T2 Host keeps the blocked code next to the sanitized rationale',
  view.execution?.blocked_reason === 'ACCOUNT_UNAVAILABLE' && String(view.reasonCode).startsWith('EXECUTION_BLOCKED:'))

const clientSource = readFileSync(fileURLToPath(new URL('../client.js', import.meta.url)), 'utf8')
expect('T3 client has the same redaction helper as the Host',
  clientSource.includes('SECRET_PATTERNS') && clientSource.includes('redactDetail') &&
    clientSource.includes('clipDetail') && /sk-\[A-Za-z0-9_-\]/.test(clientSource))

// ---- UI render (canonical harness + injected scenario) --------------------
const canonicalDir = fileURLToPath(new URL('../client/', import.meta.url))
const canonicalPath = join(canonicalDir, 'render-smoke.mjs')
const marker = '// ---- report --------------------------------------------------------------'
const source = readFileSync(canonicalPath, 'utf8')
if (!source.includes(marker)) {
  failures.push(`injection marker missing in ${canonicalPath}`)
} else {
  const scenario = `
// ---- injected t88 scenario: secret-bearing rationale is redacted ------------
const SECRET_RATIONALE = 'blocked: token=sk-abcdef123456 rejected; Authorization: Bearer abc.def.ghi; api_key=plain-secret-key; retry with Bearer qqq.www'
const SECRET_BLOCKED = { ...ROUND, roundId: 'cyc-secret-render', status: 'failed', outcome: 'no_trade', reasonCode: 'EXECUTION_BLOCKED:ACCOUNT_UNAVAILABLE', reasonDetail: null, reasonNotes: [], headline: '本轮失败：执行阻断（ACCOUNT_UNAVAILABLE）', execution: { executable: false, blocked_reason: 'ACCOUNT_UNAVAILABLE', symbol: 'BTCUSDT', side: 'long', order_plan: null, rationale: SECRET_RATIONALE, p1: null, p2: null }, nodes: ROUND.nodes.slice(0, 4) }
ROUNDS_BY_ID[SECRET_BLOCKED.roundId] = SECRET_BLOCKED
serverState = makeState({ lastRound: SECRET_BLOCKED, rounds: [{ ...ROUND_SUMMARY, roundId: SECRET_BLOCKED.roundId, status: 'failed', outcome: 'no_trade', reasonCode: SECRET_BLOCKED.reasonCode, headline: SECRET_BLOCKED.headline, startedAt: SECRET_BLOCKED.startedAt }, ROUND_SUMMARY, OLDER] })
await click('操作台')
await click('更多')
await click('刷新数据')
await click('关闭')
const secretText = textOf(await render(panel))
expect('T4 secret rationale is redacted in the rendered UI', secretText.includes('[redacted]') && !/sk-abcdef123456|abc\\.def\\.ghi|plain-secret-key|qqq\\.www/.test(secretText), secretText.slice(0, 700))
expect('T5 blocked code stays visible next to the redacted detail', secretText.includes('执行阻断（ACCOUNT_UNAVAILABLE）') && secretText.includes('ACCOUNT_UNAVAILABLE'))

`
  const tempPath = join(canonicalDir, '.render-smoke.t88.tmp.mjs')
  writeFileSync(tempPath, source.replace(marker, scenario + marker))
  let child = null
  try {
    child = spawnSync(process.execPath, [tempPath], { stdio: 'inherit' })
  } finally {
    try { unlinkSync(tempPath) } catch { /* best effort cleanup */ }
  }
  expect('T6 canonical render suite (with the injected scenario) passes', child?.status === 0, `exit ${child?.status}`)
}

for (const line of notes) console.log(line)
if (failures.length > 0) {
  console.error(`\nFAILED ${failures.length} render check(s):`)
  for (const failure of failures) console.error(` - ${failure}`)
  process.exit(1)
}
console.log(`\nAll ${notes.length} redaction render checks passed.`)
