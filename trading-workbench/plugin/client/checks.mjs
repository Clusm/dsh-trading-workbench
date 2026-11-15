/**
 * Static + registration checks for the M1.4 workbench client bundle.
 *
 * Usage: node trading-workbench/plugin/client/checks.mjs
 *
 * It loads client.js in a fake module-loader + Cordis context, then asserts the
 * contract that the UI must not break: slot registrations, zh/en parity, the
 * 14 live theme tokens, no literal colors, no external URLs/CDNs, no service
 * token access, no removed strategy/live surfaces.
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { toRoundView, toRoundSummary } from '../host/round-view.js'

const CLIENT_PATH = fileURLToPath(new URL('../client.js', import.meta.url))
const LOCALE_DIR = new URL('../locale/', import.meta.url)
const PKG_PATH = fileURLToPath(new URL('../package.json', import.meta.url))

const LIVE_TOKENS = new Set([
  '--dsw-alias-bg-base',
  '--dsw-alias-bg-layer-1',
  '--dsw-alias-bg-layer-2',
  '--dsw-alias-bg-overlay',
  '--dsw-alias-border-l1',
  '--dsw-alias-border-l2',
  '--dsw-alias-brand-primary',
  '--dsw-alias-label-primary',
  '--dsw-alias-label-secondary',
  '--dsw-alias-state-error-primary',
  '--dsw-alias-state-idle-primary',
  '--dsw-alias-state-success-primary',
  '--dsw-alias-state-warn-primary',
  '--dsw-specific-sidebar-fill',
])

// M2/M3（2026-11-14）刻意把「实盘」从禁用列表移除：Live 现在是真实能力，
// 界面必须能如实显示实盘环境（且未知不得冒充 demo）。其余词仍然禁用——
// 它们代表“用上了但没做到”的过度承诺，与 Live 支持无关。
const FORBIDDEN_TEXT = ['策略', '通道', '漏斗', '回测']
const FORBIDDEN_EN = [/\bstrategy\b/i, /\bbacktest\b/i, /\bfunnel\b/i, /\blive trading\b/i, /\breal[- ]money\b/i]

const SOURCE = readFileSync(CLIENT_PATH, 'utf8')
const failures = []
const notes = []

function check(name, ok, detail) {
  if (ok) {
    notes.push(`ok   ${name}`)
    return true
  }
  failures.push(`${name}${detail ? ` — ${detail}` : ''}`)
  return false
}

// ---- 1. module loader + fake Cordis context --------------------------------
let spec = null
const fakeWindow = {
  __ModuleLoader__: {
    load(value) { spec = value },
  },
}
// client.js is a classic script; evaluate it with only `window` in scope.
new Function('window', SOURCE)(fakeWindow)

check('module loader id', spec?.id === '@local/dsh-trading-workbench', String(spec?.id))
check('factory exported', typeof spec?.factory === 'function')

const reactStub = {
  createElement: () => null,
  Fragment: 'Fragment',
  useState: () => [null, () => {}],
  useEffect: () => {},
  useCallback: (fn) => fn,
  useMemo: (fn) => fn(),
  useRef: () => ({ current: null }),
}
const moduleExports = spec.factory((name) => {
  if (name !== 'react') throw new Error(`unexpected require(${name})`)
  return reactStub
})

check('module inject', Array.isArray(moduleExports.inject) && moduleExports.inject.includes('slots') && moduleExports.inject.includes('locale'), JSON.stringify(moduleExports.inject))

const registrations = []
let localeRegistration = null
let lookup = (key) => key
const ctx = {
  locale: {
    bind: () => lookup,
    register: (ns, dict) => {
      localeRegistration = { ns, dict }
      lookup = (key) => {
        const zh = dict?.zh?.[key]
        return typeof zh === 'string' ? zh : key
      }
      return () => {}
    },
  },
  slots: {
    inject: (_slot, callback) => {
      callback()
      return () => {}
    },
    register: (options, component) => {
      registrations.push({ options, component })
      return () => {}
    },
  },
  effect: (fn) => {
    const dispose = fn()
    return () => { if (typeof dispose === 'function') dispose() }
  },
}

moduleExports.apply(ctx, {})

const mainReg = registrations.find((entry) => entry.options?.name === 'main')
const sideReg = registrations.find((entry) => entry.options?.name === 'sidebar.panellist')
check('main slot registered', Boolean(mainReg))
check('main key', mainReg?.options?.key === 'trading-workbench', String(mainReg?.options?.key))
check('main locale namespace', mainReg?.options?.locale === 'tradingWorkbench', String(mainReg?.options?.locale))
check('main inject props', typeof mainReg?.options?.inject === 'function')
check('sidebar entry registered', Boolean(sideReg))
check('sidebar id matches main key', sideReg?.options?.id === mainReg?.options?.key, `${sideReg?.options?.id} vs ${mainReg?.options?.key}`)
check('sidebar order 40', sideReg?.options?.order === 40, String(sideReg?.options?.order))
check('sidebar label is function', typeof sideReg?.options?.label === 'function')
check('sidebar label resolves', (() => {
  try { return typeof sideReg.options.label() === 'string' && sideReg.options.label().length > 0 } catch { return false }
})())

// ---- 2. locale dictionary parity / coverage -------------------------------
check('locale registered', Boolean(localeRegistration) && localeRegistration.ns === 'tradingWorkbench')
const dict = localeRegistration?.dict ?? { zh: {}, en: {} }
const zhKeys = Object.keys(dict.zh ?? {})
const enKeys = Object.keys(dict.en ?? {})
check('zh/en key parity', zhKeys.length > 0 && zhKeys.length === enKeys.length && zhKeys.every((key) => key in (dict.en ?? {})), `${zhKeys.length} vs ${enKeys.length}`)
check('dictionary size', zhKeys.length >= 150, `${zhKeys.length} keys`)
check('no empty translations', zhKeys.every((key) => typeof dict.zh[key] === 'string' && dict.zh[key].length > 0 && typeof dict.en[key] === 'string' && dict.en[key].length > 0))

const staticKeys = new Set()
for (const match of SOURCE.matchAll(/\b(?:t|tr)\('([^']+)'/g)) staticKeys.add(match[1])
const dynamicKeys = [
  ...['cockpit', 'ai'].map((id) => `nav.${id}`),
  ...['cockpit', 'ai'].map((id) => `ws.${id}`),
  ...['current', 'history', 'review'].map((id) => `ai.${id}`),
  ...['evidence', 'prompt', 'json', 'rules', 'timing'].map((id) => `drawer.tab${id.charAt(0).toUpperCase()}${id.slice(1)}`),
  ...['analysis', 'red_opening', 'red_response', 'blue_opening', 'blue_response', 'decision', 'risk', 'execution_entry', 'review'].map((role) => `role.${role}`),
  ...['ok', 'error', 'invalid', 'pending', 'running', 'skipped', 'no_trade', 'failed', 'done', 'cancelled', 'trade'].map((status) => `status.${status}`),
  ...['long', 'short', 'buy', 'sell'].map((side) => `side.${side}`),
  ...['analysis', 'red', 'blue', 'decision', 'risk', 'execution'].map((stage) => `stage.${stage}`),
]
const missing = [...staticKeys].filter((key) => !(key in (dict.zh ?? {}))).concat(dynamicKeys.filter((key) => !(key in (dict.zh ?? {}))))
check('all used keys exist', missing.length === 0, missing.join(', '))

// ---- 3. theme tokens / colors / assets ------------------------------------
const usedTokens = new Set()
for (const match of SOURCE.matchAll(/--dsw-[a-z0-9-]+/g)) usedTokens.add(match[0])
const unknownTokens = [...usedTokens].filter((token) => !LIVE_TOKENS.has(token))
check('only live theme tokens', unknownTokens.length === 0, unknownTokens.join(', '))
check('used token count', usedTokens.size >= 10, `${usedTokens.size} tokens`)

const hexLiterals = [...SOURCE.matchAll(/#[0-9a-fA-F]{3,8}\b/g)].map((match) => match[0])
const rgbLiterals = [...SOURCE.matchAll(/\brgba?\(/g)].map((match) => match[0])
const hslLiterals = [...SOURCE.matchAll(/\bhsla?\(/g)].map((match) => match[0])
check('no literal hex colors', hexLiterals.length === 0, hexLiterals.join(', '))
check('no rgb()/hsl() colors', rgbLiterals.length + hslLiterals.length === 0, [...rgbLiterals, ...hslLiterals].join(', '))

check('no external http(s) URL', !/https?:\/\//.test(SOURCE))
check('no CDN/font reference', !/@import|fonts\.googleapis|cdn\./.test(SOURCE))
check('no chart library', !/d3|chart\.js|echarts|recharts/i.test(SOURCE))

// ---- 4. data boundaries ---------------------------------------------------
check('only relative API fetches', !/fetch\(\s*['"]http/.test(SOURCE) && SOURCE.includes('fetch(url, {'))
check('no service token access', !/WORKBENCH_API_TOKEN|Bearer|Authorization|\.api-token/.test(SOURCE))
// The appearance preference is the only thing allowed in local storage; the
// service token must never be read or persisted by the browser half.
const storageUse = SOURCE.match(/localStorage|sessionStorage|indexedDB/g) ?? []
const storageNearSecret = /(localStorage|sessionStorage|indexedDB)[^\n]{0,120}(token|secret|credential|Bearer|Authorization)/i.test(SOURCE)
check(
  'no credential storage',
  !storageNearSecret && storageUse.every(() => SOURCE.includes('twb.appearance.updown')),
  storageNearSecret ? 'storage near secret-looking field' : storageUse.join(','),
)
check('host routes only', SOURCE.includes('/trading-workbench/api/state') && SOURCE.includes('/trading-workbench/api/run') && !/api\/cycles|127\.0\.0\.1:8791/.test(SOURCE))
check('no mock round data', !/mockRound|fakeRound|sampleRound|placeholderRound/i.test(SOURCE))

// ---- 5. removed surfaces --------------------------------------------------
const forbiddenFound = FORBIDDEN_TEXT.filter((word) => SOURCE.includes(word))
check('no removed Chinese surfaces', forbiddenFound.length === 0, forbiddenFound.join(', '))
const forbiddenEnFound = FORBIDDEN_EN.filter((re) => re.test(SOURCE))
check('no removed English surfaces', forbiddenEnFound.length === 0, forbiddenEnFound.map(String).join(', '))
check('no model selector', !/<select[^>]*model|model-selector|selectModel/i.test(SOURCE))
// §4.2：环境徽标必须来自服务端事实，且未知不得渲染成 demo。
check('environment badge is service-driven', SOURCE.includes("data-env-live") && SOURCE.includes("env.unknown"))
check('environment badge has live label', SOURCE.includes("'env.live'") && SOURCE.includes('twb-env--live'))

// ---- 6. metadata + install manifest --------------------------------------
const zhLocale = JSON.parse(readFileSync(new URL('zh.json', LOCALE_DIR), 'utf8'))
const enLocale = JSON.parse(readFileSync(new URL('en.json', LOCALE_DIR), 'utf8'))
check('zh meta title/description', Boolean(zhLocale?.meta?.title) && Boolean(zhLocale?.meta?.description))
check('en meta title/description', Boolean(enLocale?.meta?.title) && Boolean(enLocale?.meta?.description))
check('locale meta mentions M1.4 UI', /M1\.4|工作台|workbench/i.test(zhLocale.meta.description) && /M1\.4|workbench/i.test(enLocale.meta.description))

const pkg = JSON.parse(readFileSync(PKG_PATH, 'utf8'))
check('package exports client', pkg?.exports?.['./client'] === './client.js')
check('package ships client files', Array.isArray(pkg?.files) && pkg.files.some((entry) => entry.startsWith('client/')))
check('package dsh.client web', pkg?.dsh?.client?.platform === 'web' && pkg?.dsh?.client?.immediately === true)

// ---- 7. safety rails ------------------------------------------------------
check('run guard exists', SOURCE.includes('runningRef') && /status === 409/.test(SOURCE))
check('drawer escape handling', SOURCE.includes("event.key === 'Escape'"))
check('aria live region', SOURCE.includes('aria-live'))
check('tabs use role=tab', SOURCE.includes("role: 'tab'") && SOURCE.includes("role: 'tablist'"))

// ---- 8. stale-copy scan (S1) ---------------------------------------------
// "未提供" is allowed: the P0 empty-state taxonomy needs it for fields the Host
// has not mapped yet (scorecard/violations). The banned phrases are the ones
// that told the user to wait for a Host extension.
const STALE_COPY = [
  '未暴露',
  '待 Host 暴露',
  '尚未接入',
  'does not expose',
  'await a Host extension',
  'dataGapList',
]
const staleFound = STALE_COPY.filter((phrase) => SOURCE.includes(phrase))
check('no stale Host-not-exposed copy', staleFound.length === 0, staleFound.join(', '))
check(
  'locale meta no skeleton wording',
  !/5\s*视图\s*骨架|skeleton views/i.test(zhLocale.meta.description) &&
    !/5\s*视图\s*骨架|skeleton views/i.test(enLocale.meta.description),
)

// ---- 9. RoundView reasonDetail (t83) -------------------------------------
function record(overrides = {}) {
  return {
    roundId: 'cyc-test',
    cycleId: 'cyc-test',
    status: 'no_trade',
    outcome: 'no_trade',
    reasonCode: 'EXECUTION_BLOCKED:OTHER',
    result: { detail: null, outputs: {}, calls: [] },
    ...overrides,
  }
}

const withRationale = toRoundView(record({
  result: {
    detail: null,
    outputs: {
      execution: {
        executable: false,
        blocked_reason: 'ACCOUNT_UNAVAILABLE',
        rationale: '缺少 account equity/balance，无法计算 qty，拒绝发 naked order',
      },
    },
    calls: [],
  },
}))
check('reasonDetail comes from execution.rationale',
  withRationale.reasonDetail === '缺少 account equity/balance，无法计算 qty，拒绝发 naked order',
  String(withRationale.reasonDetail))
check('reasonCode EXECUTION_BLOCKED:OTHER preserved', withRationale.reasonCode === 'EXECUTION_BLOCKED:OTHER')
check('execution.blocked_reason preserved for the UI', withRationale.execution?.blocked_reason === 'ACCOUNT_UNAVAILABLE')

const fromDetail = toRoundView(record({
  reasonCode: 'EXECUTION_BLOCKED:EXECUTION_PLAN_INVALID',
  result: { detail: 'order plan missing qty', outputs: {}, calls: [] },
}))
check('reasonDetail comes from result.detail string', fromDetail.reasonDetail === 'order plan missing qty', String(fromDetail.reasonDetail))

const fromObjectDetail = toRoundView(record({
  result: { detail: { message: 'no account balance', code: 'E_ACCOUNT_EMPTY' }, outputs: {}, calls: [] },
}))
check('reasonDetail flattens detail objects', fromObjectDetail.reasonDetail === 'no account balance · E_ACCOUNT_EMPTY', String(fromObjectDetail.reasonDetail))

const secretRecord = toRoundView(record({
  result: { detail: null, outputs: { execution: { rationale: 'failed with token=sk-abcdef123456 and Authorization: Bearer abc.def' } }, calls: [] },
}))
check('reasonDetail redacts secrets',
  !/sk-abcdef123456|Bearer abc\.def/.test(secretRecord.reasonDetail ?? '') && /redacted/.test(secretRecord.reasonDetail ?? ''),
  String(secretRecord.reasonDetail))
check('reasonDetail truncates long text', (toRoundView(record({
  result: { detail: null, outputs: { execution: { rationale: 'x'.repeat(1200) } }, calls: [] },
})).reasonDetail ?? '').length <= 401)

const withoutDetail = toRoundView(record({ result: { detail: null, outputs: {}, calls: [] } }))
check('reasonDetail is null when nothing displayable', withoutDetail.reasonDetail === null, String(withoutDetail.reasonDetail))

const promptRecord = toRoundView(record({
  result: {
    detail: null,
    outputs: { execution: { rationale: 'blocked because account equity is missing' } },
    calls: [],
    prompt: 'SYSTEM PROMPT MUST NOT LEAK',
    rawPrompt: 'SECRET PROMPT',
  },
}))
check('reasonDetail does not include prompt text', !String(promptRecord.reasonDetail).includes('PROMPT'))
check('RoundView has no prompt fields', !('prompt' in promptRecord) && !('rawPrompt' in promptRecord))

const summary = toRoundSummary(record({
  result: { detail: null, outputs: { execution: { rationale: 'blocked because account equity is missing' } }, calls: [] },
}))
check('RoundSummary carries reasonDetail', summary.reasonDetail === 'blocked because account equity is missing', String(summary.reasonDetail))
check('client maps EXECUTION_BLOCKED codes', SOURCE.includes("reason.executionBlock") && SOURCE.includes('EXECUTION_BLOCKED'))
check('client shows blocked_reason and detail', SOURCE.includes('concl.blockedReason') && SOURCE.includes('execution?.rationale'))
check('client has explicit no-detail copy', SOURCE.includes('concl.noDetail'))

// ---- 10. NO_CANDIDATE detail notes (t86 contract) ------------------------
const NO_CANDIDATE_DETAIL = {
  reason: '波动率过高，且方向不明确',
  confidence: 0.31,
  regime: 'CHOPPY',
  notes: ['BTC 1h ATR 扩至 2.1%', '资金费率转负', 'ETH 与 BTC 相关性下降'],
  warnings: ['流动性薄，滑点风险高'],
}
const noCandidate = toRoundView(record({
  reasonCode: 'NO_CANDIDATE',
  status: 'no_trade',
  outcome: 'no_trade',
  result: {
    detail: NO_CANDIDATE_DETAIL,
    outputs: {
      analysis: {
        stand_aside: true,
        stand_aside_reason: '波动率过高，且方向不明确',
        market_read: { regime: 'CHOPPY', notes: ['BTC 1h ATR 扩至 2.1%', '资金费率转负'] },
        warnings: ['流动性薄，滑点风险高'],
      },
    },
    calls: [],
  },
}))
check('NO_CANDIDATE reasonDetail carries the reason', String(noCandidate.reasonDetail).includes('波动率过高'), String(noCandidate.reasonDetail))
check('NO_CANDIDATE reasonNotes has 1-3 strings',
  Array.isArray(noCandidate.reasonNotes) && noCandidate.reasonNotes.length >= 1 && noCandidate.reasonNotes.length <= 3,
  JSON.stringify(noCandidate.reasonNotes))
check('NO_CANDIDATE notes include analysis notes',
  noCandidate.reasonNotes.some((note) => note.includes('ATR 扩至 2.1%')),
  JSON.stringify(noCandidate.reasonNotes))
check('NO_CANDIDATE notes include detail notes',
  noCandidate.reasonNotes.some((note) => note.includes('ETH 与 BTC 相关性下降')),
  JSON.stringify(noCandidate.reasonNotes))
check('NO_CANDIDATE falls back to warnings when notes are absent',
  toRoundView(record({
    reasonCode: 'NO_CANDIDATE',
    result: { detail: { reason: 'x', warnings: ['流动性薄'] }, outputs: {}, calls: [] },
  })).reasonNotes[0] === '流动性薄')
check('NO_CANDIDATE analysis summary exposes market read',
  Array.isArray(noCandidate.analysis?.market_read?.notes) && noCandidate.analysis.market_read.notes.length >= 1,
  JSON.stringify(noCandidate.analysis))
check('NO_CANDIDATE notes stay display-only',
  !noCandidate.reasonNotes.some((note) => note.includes('0.31') || note.includes('CHOPPY')),
  JSON.stringify(noCandidate.reasonNotes))

const legacyNoCandidate = toRoundView(record({
  reasonCode: 'NO_CANDIDATE',
  result: { detail: 'stand aside: no edge', outputs: {}, calls: [] },
}))
check('legacy string NO_CANDIDATE detail still works', legacyNoCandidate.reasonDetail === 'stand aside: no edge', String(legacyNoCandidate.reasonDetail))

const secretNotes = toRoundView(record({
  reasonCode: 'NO_CANDIDATE',
  result: {
    detail: { reason: 'x', notes: ['token=sk-abc123456789 leaked'], warnings: ['Authorization: Bearer zzz'] },
    outputs: {},
    calls: [],
  },
}))
check('NO_CANDIDATE notes redact secrets',
  !/sk-abc123456789|Bearer zzz/.test(JSON.stringify(secretNotes.reasonNotes)) &&
    /redacted/.test(JSON.stringify(secretNotes.reasonNotes)),
  JSON.stringify(secretNotes.reasonNotes))

const analysisOnly = toRoundView(record({
  reasonCode: 'NO_CANDIDATE',
  result: { detail: null, outputs: { analysis: { market_read: { regime: 'CALM', notes: ['价差窄'] } } }, calls: [] },
}))
check('analysis notes survive without result.detail', analysisOnly.reasonNotes[0] === '价差窄', JSON.stringify(analysisOnly.reasonNotes))

const noCandidatePrompt = toRoundView(record({
  reasonCode: 'NO_CANDIDATE',
  result: { detail: { reason: 'stand aside' }, outputs: { analysis: { market_read: { notes: ['note'] } } }, calls: [], prompt: 'PROMPT MUST NOT LEAK' },
}))
check('NO_CANDIDATE view excludes prompt text', !JSON.stringify(noCandidatePrompt).includes('PROMPT'))
check('client renders reason notes list', SOURCE.includes('concl-notes') && SOURCE.includes('reasonNotes'))

// M1.4 auto schedule: the footer must reflect /state.schedule instead of a
// hard-coded "not enabled (M2)", and must fall back for older Hosts.
check('schedule footer reads /state.schedule',
  SOURCE.includes('scheduleLabel(stateData, t)') &&
    SOURCE.includes("'s4.scheduleOn'") &&
    SOURCE.includes("'s4.schedulePaused'"))
check('schedule footer keeps the M2 fallback for old hosts',
  SOURCE.includes("if (!info || info.enabled === false) return t('s4.scheduleOff')"))

// ---- 11. v2 P0 batch (05 §3.1: N01/N02/N03) ------------------------------
// 03 §8 owns the vocabulary; the acceptance threshold is "rows >= 40" and the
// 20-code set (18 measured + 2 new route codes) must all resolve.
const reasonKeys = Object.keys(dict.zh ?? {}).filter((key) => key.startsWith('reason.'))
check('reason label table has at least 40 rows', reasonKeys.length >= 40, String(reasonKeys.length))
// Exactly the 20 unique codes of 01 §3.7 (18 measured + 2 new route codes).
// EXECUTION_BLOCKED:OTHER resolves through the documented wildcard, so the
// resolver accepts an exact key or a documented base-code key.
const ACCEPTANCE_CODES = [
  'RISK_UNAVAILABLE', 'DEBATE_INCOMPLETE', 'EXECUTION_BLOCKED:PRICE_MOVED', 'EXECUTION_PLAN_INVALID',
  'E_RISK_SYMBOL_POSITION', 'DECISION_INVALID', 'LLM_FINISH_ERROR', 'TRADE_PLAN_READY', 'LLM_TIMEOUT',
  'E_RISK_LIQ_DISTANCE', 'E_EVIDENCE_UNAVAILABLE', 'E_CYCLE_DUPLICATE_SLOT', 'HOST_STALE_RUN', 'E_RISK_RR',
  'AI_RISK_REJECT', 'NO_CANDIDATE', 'EXECUTION_BLOCKED:OTHER', 'EXECUTION_BLOCKED:SPREAD_TOO_WIDE',
  'ROUTE_UNAVAILABLE', 'ROUTE_FALLBACK_APPLIED',
]
// Documented wildcards live as templates, not as code keys: reasonLabel renders
// them via fmt() so the sub-code/status stays visible.
const DOCUMENTED_WILDCARDS = {
  EXECUTION_BLOCKED: 'reason.executionBlock',
  E_SERVICE_HTTP: 'reason.serviceHttp',
}
const resolves = (code) => {
  if (`reason.${code}` in (dict.zh ?? {})) return true
  const base = code.includes(':') ? code.slice(0, code.indexOf(':')) : code
  if (base in DOCUMENTED_WILDCARDS) return DOCUMENTED_WILDCARDS[base] in (dict.zh ?? {})
  return `reason.${base}` in (dict.zh ?? {})
}
const unresolvedCodes = ACCEPTANCE_CODES.filter((code) => !resolves(code))
check('all 20 acceptance codes resolve to a Chinese label (exact or documented wildcard)',
  ACCEPTANCE_CODES.length === 20 && new Set(ACCEPTANCE_CODES).size === 20 && unresolvedCodes.length === 0,
  `${ACCEPTANCE_CODES.length} listed / ${new Set(ACCEPTANCE_CODES).size} unique; unresolved: ${unresolvedCodes.join(', ')}`)
// The E_SERVICE_HTTP_{n} wildcard previously lost its backslash in the client
// bundle, so every real HTTP status fell through to "未收录原因码".
const BACKSLASH = String.fromCharCode(92)
check('E_SERVICE_HTTP wildcard keeps its regex escape',
  SOURCE.includes(`E_SERVICE_HTTP_?(${BACKSLASH}d{3})`),
  'expected the literal E_SERVICE_HTTP_?(\\d{3}) in client.js')
check('unknown reason copy is gone', !/'reason\.unknown'/.test(SOURCE) && SOURCE.includes('reason.unmapped') && !SOURCE.includes("unknown: ['"))
check('EXECUTION_BLOCKED wildcard is still rendered', SOURCE.includes("fmt(zh('reason.executionBlock')"))

function v2record(overrides = {}) {
  return {
    roundId: 'cyc-v2', cycleId: 'cyc-v2', status: 'failed', outcome: 'no_trade',
    reasonCode: 'RISK_UNAVAILABLE', durationMs: 60000, gaps: [],
    result: {
      debateMode: 'symmetric_two_round',
      calls: [
        { call_id: 'c1', role: 'analysis', role_key: 'analysis', attempt: 1, schema_ok: true, latency_ms: 9, started_at_ms: 1, ended_at_ms: 2 },
        {
          call_id: 'c2', role: 'risk', role_key: 'risk', attempt: 1, schema_ok: false, latency_ms: 45000,
          error_code: 'LLM_TIMEOUT', error_message: 'LLM call exceeded 45000 ms',
          validation_errors: [{ path: '$', message: 'LLM call exceeded 45000 ms', keyword: 'LLM_TIMEOUT' }],
          started_at_ms: 3, ended_at_ms: 4,
        },
      ],
      outputs: {
        redOpening: { symbol: 'BTCUSDT', tradeable: true, confidence: 0.62, thesis: '红队论点', evidence: [], rebuttal: [], opponent_steelman: '蓝队最强论点' },
        decision: { action: 'NO_TRADE', selected_case: 'red', case_comparison: { decisive_factor: '风控未产出' } },
      },
    },
    ...overrides,
  }
}

const v2view = toRoundView(v2record())
check('RoundView carries attribution', v2view.attribution?.one_liner_zh === '风控 AI 未产出，按保守策略不交易', String(v2view.attribution?.one_liner_zh))
check('RoundView carries per-call error codes', v2view.nodes[1]?.error_code === 'LLM_TIMEOUT', String(v2view.nodes[1]?.error_code))
check('RoundView carries debate sides', v2view.debate?.rounds?.r1?.red?.tradeable === true && v2view.debate?.adopted?.side === 'red')
check('RoundView carries the stage rollup', v2view.stages.find((stage) => stage.stage_key === 'risk')?.status === 'error')
check('RoundView carries chain stop identity', v2view.chain.stop_kind === 'failed' && v2view.chain.stop_error_code === 'LLM_TIMEOUT')
check('RoundView carries the build identity', v2view.build?.chain_version === 'ai-chain/1' && 'configRevision' in (v2view.build ?? {}))
const legal = toRoundView({ roundId: 'cyc-n', status: 'done', outcome: 'no_trade', reasonCode: 'NO_CANDIDATE', result: { calls: [], outputs: {} } })
check('a legal terminal has no stopped_at', legal.chain.stop_kind === 'decision_no_trade' && legal.chain.stopped_at === null)

// ---- 12. 05 P1-1: model / effort configuration (plugin-local) ------------
check('config drawer uses the plugin-local catalog/config routes',
  SOURCE.includes("const LLM_CATALOG_URL = `${API}/llm/catalog`") && SOURCE.includes("const LLM_CONFIG_URL = `${API}/llm/config`"))
check('config drawer never writes the Harness default (P-03=A)',
  !/saveSelection|agentDefaultModel|profiles[\\/]/.test(SOURCE))
check('config drawer exposes presets, per-role rows and the symmetric debate row',
  SOURCE.includes("'config.preset'") && SOURCE.includes("'config.debate'") && SOURCE.includes('config.asymmetric'))

// ---- 13. 05 P1-2: running progress from the in_flight object -------------
check('running stage comes from in_flight and falls back to the N04 wording',
  SOURCE.includes("'concl.runningStage'") && SOURCE.includes("'concl.submitted'") &&
    SOURCE.includes('typeof model.inFlight === ') && SOURCE.includes('stage_label'))

// ---- 14. 05 P1-3: interaction / infrastructure convergence ----------------
check('absolute times use Asia/Shanghai, no browser-local getters remain',
  SOURCE.includes("const DISPLAY_TZ = 'Asia/Shanghai'")
    && SOURCE.includes('function dayKeyInShanghai')
    && !/getHours\(|getDate\(|getMonth\(|getFullYear\(/.test(SOURCE))
check('failed-round sentences come from code+params; the raw string is folded',
  SOURCE.includes('function reasonSentence')
    && SOURCE.includes("'concl.rawDetail'")
    && SOURCE.includes("h('details'")
    && SOURCE.includes("round?.reasonParams"))
check('no dead P1/P2 "not wired" copy keys remain',
  !SOURCE.includes('未接入（P1）') && !SOURCE.includes('未接入（P2）') && !SOURCE.includes('未实现（P2）'))
check('the blocking banner is announced and the repair reason is visible',
  SOURCE.includes("role: 'alert'") && SOURCE.includes("'aria-describedby': 'twb-repair-hint'") && SOURCE.includes("'positions.repairHint'"))

// ---- 15. F1: stop semantics, automation status, release identity ----------
// §3.2: the top-bar stop is 关调度 + service pause. The Host implements it in
// stopAutomation(); that function must never reach the flattening emergency
// path, so the source is asserted directly (not just the rendered text).
const INTEGRATION_SOURCE = readFileSync(new URL('../host/integration.js', import.meta.url), 'utf8')
const ROUND_VIEW_SOURCE = readFileSync(new URL('../host/round-view.js', import.meta.url), 'utf8')
const SCHEDULER_SOURCE = readFileSync(new URL('../host/scheduler.js', import.meta.url), 'utf8')
const SERVICE_CLIENT_SOURCE = readFileSync(new URL('../host/service-client.js', import.meta.url), 'utf8')
const BUILD_SOURCE = readFileSync(new URL('../host/build.js', import.meta.url), 'utf8')
const stopStart = INTEGRATION_SOURCE.indexOf('async stopAutomation(')
const stopEnd = INTEGRATION_SOURCE.indexOf('async startAutomation(')
const stopBody = stopStart >= 0 && stopEnd > stopStart ? INTEGRATION_SOURCE.slice(stopStart, stopEnd) : ''
check('Host stop path exists', stopBody.length > 0, String(stopBody.length))
check('Host stop path uses the non-flattening pause control state',
  stopBody.includes('state: CONTROL_STOP_STATE') && INTEGRATION_SOURCE.includes("const CONTROL_STOP_STATE = 'pause'"))
check('Host stop path never mentions emergency', !/emergency/i.test(stopBody))
check('Host stop path never flattens or closes a position',
  !/flatten|positions\/close|client\.close\(/i.test(stopBody))
check('Host stop closes the local scheduler before calling the service',
  stopBody.includes('setEnabled(false)') && stopBody.indexOf('setEnabled(false)') < stopBody.indexOf('client.control('))
check('Host stop reports pending_confirmation when the service is unreachable',
  stopBody.includes("'pending_confirmation'") && stopBody.includes('local-only'))
check('scheduler publishes the control facade the stop route uses',
  SCHEDULER_SOURCE.includes('integration.schedulerControl = facade') && SCHEDULER_SOURCE.includes('setEnabled: (value) => this.setEnabled(value)'))
check('automation state is derived from the service control row, not from local memory',
  INTEGRATION_SOURCE.includes('controlState === CONTROL_STOP_STATE') && INTEGRATION_SOURCE.includes("reasonCode = 'SERVICE_UNREACHABLE'"))
check('client stops through the Host automation route',
  SOURCE.includes('automation/${action}') && SOURCE.includes("'data-auto-action'"))
check('client shows a progress state before the service confirms',
  SOURCE.includes("state: 'stopping'") && SOURCE.includes("t('auto.stopping')"))
check('client distinguishes 已停止 / 待确认 / 规则阻断',
  SOURCE.includes("'auto.stopped'") && SOURCE.includes("'auto.pendingService'") && SOURCE.includes("'auto.pendingControl'") && SOURCE.includes("'auto.blocked'"))
check('attention items carry 影响 + 原因 + 下一步 and a category',
  SOURCE.includes('impact:') && SOURCE.includes('next:') && SOURCE.includes('ATTENTION_CATEGORY') && SOURCE.includes('function dedupeAttention'))
check('a deliberate stop does not raise a second control banner',
  SOURCE.includes('const operatorStop = control === ') && SOURCE.includes('!operatorStop'))
check('1024 folds optional chrome but keeps status + stop action',
  SOURCE.includes('@container (max-width:1199px)') && SOURCE.includes('.twb-precheck-toggle{display:none}') && SOURCE.includes('.twb-auto-btn{max-width:150px}'))
// §5.1: the source release is packaged, captured at load, and never re-read.
check('build identity reports the packaged release separately from loaded_at',
  BUILD_SOURCE.includes('release_id: RELEASE.release_id') && BUILD_SOURCE.includes('loaded_at: loadedAt'))
check('release manifest is read once at module load, never per call',
  BUILD_SOURCE.includes('const RELEASE = readReleaseManifest()')
    && !/function currentBuild[\s\S]*readReleaseManifest\(\)/.test(BUILD_SOURCE))
// T01 G3: an automatic slot must be recorded as auto, not as a manual run.
check('cycle creation sends the trigger',
  SERVICE_CLIENT_SOURCE.includes("if (trigger === 'auto' || trigger === 'manual') body.trigger = trigger"))
check('host passes the round trigger to the service',
  (INTEGRATION_SOURCE.match(/createCycle\([^)]*\{ trigger/g) ?? []).length >= 2)
// A (§8·A, t2 收口): confidence is an analysis field. The legacy strong tier is
// still mapped for history but must not reach the AI chain as a usable budget.
check('legacy strong caps are not handed to the chain',
  INTEGRATION_SOURCE.includes('const STRONG_TIER_CAPS = ')
    && INTEGRATION_SOURCE.includes('if (STRONG_TIER_CAPS.has(key)) continue'))
check('the envelope marks strong/confidence as not used for authorization',
  INTEGRATION_SOURCE.includes('used_for_execution_authorization')
    && /confidence: false[\s\S]{0,40}strong_tier: false/.test(INTEGRATION_SOURCE))
check('confidence is labelled as an analysis field in the UI',
  (dict.zh['concl.confidence'] ?? '').includes('分析字段')
    && /analysis/i.test(dict.en['concl.confidence'] ?? '')
    && (dict.zh['concl.confidenceHint'] ?? '').includes('不参与授权'))
const F1_KEYS = [
  'auto.running', 'auto.stopped', 'auto.stopping', 'auto.pendingService', 'auto.pendingControl',
  'auto.blocked', 'auto.stop', 'auto.start', 'auto.lastSuccess', 'auto.nextRun', 'auto.stopDone',
  'auto.stopPending', 'auto.hint', 'auto.detail', 'env.demo', 's2.catNoTrade', 's2.catRule',
  's2.catExec', 's2.catPending', 's2.staleImpact', 's2.staleNext', 'system.releaseId',
  'system.releaseDigest', 'system.loadedAt',
]
const missingF1 = F1_KEYS.filter((key) => !(key in (dict.zh ?? {})) || !(key in (dict.en ?? {})))
check('all F1 copy keys exist in both languages', missingF1.length === 0, missingF1.join(', '))
check('the stop copy states that positions and protection orders are untouched',
  (dict.zh['auto.stopDone'] ?? '').includes('持仓与交易所保护单保持不变')
    && (dict.en['auto.stopDone'] ?? '').toLowerCase().includes('protection orders are unchanged'))
check('the stale copy never claims everything is fine',
  !/一切正常|all good/i.test(dict.zh['s2.staleImpact'] ?? '') + (dict.en['s2.staleImpact'] ?? ''))

// ---- 16. F2 (§3.1): one settings entry, five groups ------------------------
// The entry, the group list and the save/apply split are structural, so they are
// asserted on the source (the render suite covers the behaviour).
check('settings entry is a single drawer type with five groups',
  SOURCE.includes("const SETTINGS_GROUPS = ['connection', 'trading', 'automation', 'appearance', 'diagnostics']") &&
    SOURCE.includes("drawer.type === 'settings'") &&
    SOURCE.includes("data-settings-open") &&
    SOURCE.includes("'data-settings-open': 'automation'"))
check('the five groups have their own copy in both languages',
  ['connection', 'trading', 'automation', 'appearance', 'diagnostics'].every((group) => `set.group.${group}` in (dict.zh ?? {}) && `set.group.${group}` in (dict.en ?? {})))
check('service-owned settings are labelled read-only instead of faked editable',
  SOURCE.includes("t('set.readonly')") && SOURCE.includes("'data-readonly': 'true'") &&
    SOURCE.includes("t('set.envLocked')") && SOURCE.includes("t('set.limitsSub')"))
// §3.1: a stale form must not overwrite a newer value.
check('the Host rejects a stale config revision instead of overwriting',
  INTEGRATION_SOURCE.includes('E_CONFIG_REVISION_CONFLICT') && INTEGRATION_SOURCE.includes('sendJson(res, 409') &&
    INTEGRATION_SOURCE.includes('body?.baseRevision'))
check('the client sends the revision it rendered and keeps the draft on conflict',
  SOURCE.includes('JSON.stringify({ config: payload, baseRevision })') &&
    SOURCE.includes('set.conflict') && SOURCE.includes("t('set.reloadConfig')") &&
    SOURCE.includes("notice.conflict"))
check('the settings form shows before / after / effective time',
  SOURCE.includes("t('set.before')") && SOURCE.includes("t('set.after')") &&
    SOURCE.includes("t('set.effectiveNext')") && SOURCE.includes("t('set.lastChange')") &&
    SOURCE.includes("'data-config-diff'"))
// C (§8·C) is the only fund-caliber source; the client never derives its own PnL.
check('fund calibers come from /api/pnl through the Host',
  INTEGRATION_SOURCE.includes("export const PNL_PATH = ") && INTEGRATION_SOURCE.includes("client.request(`/api/pnl?range_s=") &&
    SOURCE.includes("const PNL_URL = `${API}/pnl`"))
check('unknown fund values are shown as unknown, never as zero',
  SOURCE.includes("t('set.fundingUnknown')") && SOURCE.includes("t('set.unknownNote')") &&
    SOURCE.includes("t('set.fundsUnavailable')") && SOURCE.includes("t('set.coveragePartial')"))
check('reconcile shows matched/partial/unknown with diff, reason and last success',
  SOURCE.includes("t('set.reconcileMatched')") && SOURCE.includes("t('set.reconcilePartial')") &&
    SOURCE.includes("t('set.reconcileUnknown')") && SOURCE.includes("t('set.reconcileLastSuccess')"))
// B (t3): pending is a state, not a failure, and it is visible in diagnostics.
check('pending orders are shown as 待确认 with an age, never as failed',
  SOURCE.includes("t('set.diagPending')") && SOURCE.includes("t('set.diagPendingNote')") &&
    INTEGRATION_SOURCE.includes('pendingConfirmation: order?.pending_confirmation === true') &&
    INTEGRATION_SOURCE.includes('function projectOutbox'))
// G2: one fact for the executed route.
check('the executed route has a single source (round snapshot) and never the live default',
  SOURCE.includes('model.round?.route') && SOURCE.includes("'system.routeSourceRound'") &&
    SOURCE.includes("t('system.routeDiffNote')") && !SOURCE.includes("t('system.routeDefault')") &&
    !SOURCE.includes("t('system.savedDefault')") &&
    typeof (dict.zh ?? {})['system.routeSourceRound'] === 'string' &&
    typeof (dict.en ?? {})['system.routeSourceRound'] === 'string')
// G3 is t5's write-side fix; t6 only displays the distinction.
check('automatic and manual rounds are distinguishable in settings (display only)',
  SOURCE.includes("t('set.triggerAuto')") && SOURCE.includes("t('set.triggerManual')") &&
    SOURCE.includes("'data-trigger'") && !SOURCE.includes("trigger: 'auto'"))
// F4: the allowed entry range reaches the UI from round-view (no new Service field).
check('round-view passes the plan entry range and expiry (F4)',
  readFileSync(new URL('../host/round-view.js', import.meta.url), 'utf8').includes('entry_zone_low') &&
    readFileSync(new URL('../host/round-view.js', import.meta.url), 'utf8').includes('valid_until:') &&
    SOURCE.includes("t('ai.entryZone')") && SOURCE.includes("t('ai.validUntil')") && SOURCE.includes("t('ai.zoneMissing')"))
check('a refresh reopens the same settings group (payload-less, like the system drawer)',
  SOURCE.includes("if (type === 'settings')") && SOURCE.includes('SETTINGS_GROUPS.includes(tab)'))
check('the settings drawer is a keyboard-reachable dialog with a real tablist',
  SOURCE.includes("className: 'twb-drawer-tabs twb-settings-tabs'") && SOURCE.includes("'aria-modal': 'true'") &&
    SOURCE.includes("'ArrowRight'") && SOURCE.includes("id: `twb-drawer-tab-${tab}`"))

// P-1/P-2/P-3 (t6 review): one source, one unit conversion, unknown stays unknown.
const tradingStart = SOURCE.indexOf('function TradingGroup({ model, t })')
const tradingEnd = SOURCE.indexOf('function TriggersBlock(')
const tradingBody = tradingStart >= 0 && tradingEnd > tradingStart ? SOURCE.slice(tradingStart, tradingEnd) : ''
check('trading limits read only authorization.effective_caps (no fallback chain)',
  tradingBody.length > 0 && tradingBody.includes('authorization?.effective_caps') &&
    tradingBody.includes('ratioText') && !/caps\.maxRiskPct|caps\.maxLeverage|limits\.per_trade|limits\.max_leverage/.test(tradingBody))
check('the risk limit group exposes no save control (the service has no risk-write route)',
  tradingBody.length > 0 && !/onSave|saveConfig|method: 'POST'|fetch\(/.test(tradingBody) &&
    SOURCE.includes("t('set.capsOnlySource')"))
check('an absent legacy tier reads as unknown, only an explicit false says 不参与授权',
  tradingBody.includes('used_for_execution_authorization === undefined') &&
    tradingBody.includes('set.legacyUnknown') && SOURCE.includes('set.legacyAuthorized'))
check('funding is never rendered as 0 when it is not backfilled',
  SOURCE.includes("fundingKnown === true ? signedAmountText(books?.funding_pnl)") &&
    SOURCE.includes("t('set.fundingUnknown')"))

// ---- 17. T15 (§8·D): gate wired, auditable, visible -----------------------
import { ATTRIBUTION_CODES } from '../host/attribution.js'

check('the gate audit is a fixed whitelist (not the raw gate object)',
  INTEGRATION_SOURCE.includes('export function gateAuditOf(') &&
    INTEGRATION_SOURCE.includes("'ok', 'code', 'sub_code', 'detail', 'checked_at', 'mode', 'enforced', 'quote'") &&
    INTEGRATION_SOURCE.includes('export const GATE_AUDIT_KEYS'))
check('the gate audit reaches both the ledger artifact and the round record',
  INTEGRATION_SOURCE.includes('gate: gateAudit.gate') &&
    INTEGRATION_SOURCE.includes('record.gateAudit = gateAuditOf(result)'))
check('plan gaps and failureCategory are forwarded (never re-derived in the Host)',
  INTEGRATION_SOURCE.includes('export function projectPlanGaps(') &&
    INTEGRATION_SOURCE.includes("failure_category: gateAudit.failure_category") &&
    INTEGRATION_SOURCE.includes('result?.failureCategory'))
check('round-view renders a gate-blocked execution stage (not pending, not ok)',
  ROUND_VIEW_SOURCE.includes("status: gateBlocked ? 'blocked'") &&
    ROUND_VIEW_SOURCE.includes('blocked_by_gate') &&
    ROUND_VIEW_SOURCE.includes('summarizeStages(result, gate)') &&
    SOURCE.includes("'data-gate-blocked'") && SOURCE.includes("t('ai.stageBlocked')"))
check('the blocked reason comes from the gate detail + plan gaps (one source)',
  SOURCE.includes("blocked.detail") && SOURCE.includes("t('ai.gatePlanGaps')") &&
    SOURCE.includes("t('ai.gateQuoteEvidence')") && SOURCE.includes("t('ai.gateObserveOnly')"))
check('attribution carries the gate reason codes with a one-line summary',
  ['EXECUTION_BLOCKED:PLAN_MISSING', 'EXECUTION_BLOCKED:PLAN_INVALID', 'EXECUTION_BLOCKED:VALID_UNTIL_MISSING',
    'EXECUTION_BLOCKED:VALID_UNTIL_EXPIRED', 'EXECUTION_BLOCKED:BACKGROUND_EVIDENCE_STALE', 'EXECUTION_BLOCKED:QUOTE_STALE',
    'EXECUTION_BLOCKED:PRICE_OUT_OF_ZONE', 'EXECUTION_BLOCKED:SL_TP_INVALID', 'EXECUTION_BLOCKED:E_RULES_CHANGED']
    .every((code) => ATTRIBUTION_CODES.includes(code)))
check('trace fields are forwarded: timeout_ms / started_at_ms / ended_at_ms (+ run start)',
  INTEGRATION_SOURCE.includes('timeout_ms: Math.round(call.timeout_ms)') &&
    INTEGRATION_SOURCE.includes('started_at_ms: Math.round(call.started_at_ms)') &&
    INTEGRATION_SOURCE.includes('ended_at_ms: Math.round(call.ended_at_ms)') &&
    INTEGRATION_SOURCE.includes('started_at_ms: runStartedMs'))
check('a zero or missing timeout is never sent as 0',
  INTEGRATION_SOURCE.includes('call.timeout_ms > 0') && INTEGRATION_SOURCE.includes('export function epochMsOf('))
check('the failed-call text is forwarded whole or not at all (no client truncation)',
  INTEGRATION_SOURCE.includes('export function failedCallText(') &&
    INTEGRATION_SOURCE.includes('{ response_text: failedCallText(call) }') &&
    !/response_text[^\n]{0,80}slice\(/.test(INTEGRATION_SOURCE))
check('the pre-submit gate reuses evaluateEntryPlan (no parallel judgement in the Host)',
  INTEGRATION_SOURCE.includes("import { evaluateEntryPlan } from './ai/semantics.js'") &&
    INTEGRATION_SOURCE.includes('export function preSubmitGateOf(') &&
    INTEGRATION_SOURCE.includes('const verdict = evaluateEntryPlan({') &&
    !/function evaluateEntryPlan\s*\(/.test(INTEGRATION_SOURCE))
check('the pre-submit check never pretends to have a submit-time price',
  INTEGRATION_SOURCE.includes("requote: 'unavailable'") &&
    INTEGRATION_SOURCE.includes("source: 'evidence_build'") &&
    INTEGRATION_SOURCE.includes("verdict.code === 'VALID_UNTIL_EXPIRED'"))
check('only an enforced time-based verdict blocks a submission',
  INTEGRATION_SOURCE.includes('if (preSubmitGate?.enforced && preSubmitGate.ok === false)') &&
    INTEGRATION_SOURCE.includes('EXECUTION_BLOCKED:${preSubmitGate.code}'))
check('exchange reachability is projected from the exchange sub-object only',
  INTEGRATION_SOURCE.includes('export function projectExchange(') &&
    INTEGRATION_SOURCE.includes('exchange: projectExchange(status?.exchange)') &&
    INTEGRATION_SOURCE.includes('exchange: portfolio.exchange ?? null') &&
    SOURCE.includes("model.state?.portfolio?.exchange") &&
    !SOURCE.includes("model.state?.stale") && !SOURCE.includes("stateData?.stale"))
check('a missing exchange block says unavailable instead of "all fine"',
  SOURCE.includes("t('set.exchangeMissing')") && SOURCE.includes("t('set.exchangeBlocked')") &&
    SOURCE.includes("t('set.exchangeRetryIn')") && SOURCE.includes("t('set.exchangeLastSuccess')"))

// F21-1 (t24): T21 experiment B wrapped caps.maxRiskPct in a helper and the
// old assertion (which only scanned TradingGroup's body) stayed green. The ban
// now covers the WHOLE settings subtree, and the render suite carries a decoy
// case that any non-envelope source would show up in.
// The settings surface starts at SettingsBlock (the first settings component) and
// ends where the command dialogs begin — Drawer sits *before* it in the file.
const settingsStart = SOURCE.indexOf('function SettingsBlock(')
const settingsEnd = SOURCE.indexOf('function ActionDialog(')
const settingsSubtree = settingsStart >= 0 && settingsEnd > settingsStart ? SOURCE.slice(settingsStart, settingsEnd) : ''
// Comments may *describe* the forbidden sources (that is how the P-1 rule is
// documented); the ban applies to executable code only.
const settingsCode = settingsSubtree
  .replace(/\/\*[\s\S]*?\*\//g, '')
  .replace(/\/\/[^\n]*/g, '')
check('no settings surface may take limits from another snapshot (whole subtree)',
  settingsCode.length > 0 &&
    // `caps?.` also catches optional chaining (T21 experiment B used `model?.caps?.maxRiskPct`).
    !/caps\s*\??\.|limits\.per_trade|roundCaps|stateData\?\.budget/.test(settingsCode) &&
    settingsCode.includes('authorization?.effective_caps'))
check('the approval TTL/flags read only from envelope.approval (never 0, never guessed)',
  settingsSubtree.includes('const approval = env?.approval') &&
    settingsSubtree.includes('approval.single_use === true') &&
    settingsSubtree.includes('approval.plan_hash_bound === true') &&
    settingsSubtree.includes('approval.rules_version_bound === true') &&
    settingsSubtree.includes('approvalPolicy?.bound === true') &&
    settingsSubtree.includes("t('set.limitMissing')") &&
    settingsSubtree.includes("t('set.approvalNote')"))
check('the one-submission constraint is spelled out in both languages',
  (dict.zh['set.approvalNote'] ?? '').includes('每次批准只对一次提交有效') &&
    /one submission/i.test(dict.en['set.approvalNote'] ?? ''))
check('the TTL row shows the effective value and the ceiling',
  settingsSubtree.includes('ttl_max_s') && settingsSubtree.includes("t('set.approvalMaxLabel')"))

check('the TTL row shows the effective value and the ceiling',
  settingsSubtree.includes('ttl_max_s') && settingsSubtree.includes("t('set.approvalMaxLabel')"))

// ---- 17. 15: the round archive is day-partitioned, not a 100-row cache -----
const ARCHIVE_SOURCE = readFileSync(new URL('../host/round-archive.js', import.meta.url), 'utf8')
check('the 100-row round cap is gone from the Host',
  !/DEFAULT_STORE_MAX_ROUNDSs*=|maxRoundss*=/.test(INTEGRATION_SOURCE) &&
    INTEGRATION_SOURCE.includes("from './round-archive.js'"))
check('rounds are archived per Asia/Shanghai day and never trimmed by count',
  ARCHIVE_SOURCE.includes('export function archiveDay') &&
    ARCHIVE_SOURCE.includes("ARCHIVE_FILE_SUFFIX = '.jsonl'") &&
    !/slice\(0,\s*this\.maxRounds\)/.test(ARCHIVE_SOURCE) &&
    INTEGRATION_SOURCE.includes('listDate(') && INTEGRATION_SOURCE.includes('listRecent('))
check('the archive query is one day at a time and rejects a future day',
  INTEGRATION_SOURCE.includes('parseRoundDate') && INTEGRATION_SOURCE.includes('ROUNDS_DATES_PATH') &&
    ARCHIVE_SOURCE.includes('E_DATE_FUTURE') && ARCHIVE_SOURCE.includes('E_DATE_INVALID'))
check('/state carries today\'s partition plus the day keys',
  INTEGRATION_SOURCE.includes('rounds_date: today') && INTEGRATION_SOURCE.includes('available_dates: this.store.listDateKeys()'))
check('the client asks for an archived day instead of re-slicing a cache',
  SOURCE.includes('`${ROUNDS_URL}?date=') && SOURCE.includes("t('ai.dateToday')") &&
    !SOURCE.includes('最近 100 轮') && !SOURCE.includes('last 100 local round'))
check('the archive copy exists in both languages',
  ['ai.historyDate', 'ai.dateToday', 'ai.datePick', 'ai.dayEmpty', 'ai.historyReadonly', 'ai.listScope']
    .every((key) => key in (dict.zh ?? {}) && key in (dict.en ?? {})) &&
    (dict.zh['ai.listScope'] ?? '').includes('按日归档') &&
    /day-partitioned archive/i.test(dict.en['ai.listScope'] ?? ''))

// ---- 18. 15 §10: one-click start of the local service ----------------------
check('the Host exposes a local-only service start command',
  INTEGRATION_SOURCE.includes('SERVICE_START_PATH') &&
    INTEGRATION_SOURCE.includes('isSameOriginRequest(req, this.ctx)') &&
    INTEGRATION_SOURCE.includes('E_SERVICE_LAUNCHER_MISSING') &&
    INTEGRATION_SOURCE.includes('E_SERVICE_START_TIMEOUT') &&
    INTEGRATION_SOURCE.includes('probeServiceHealth'))
check('the launcher derives its paths from the plugin, never a hard-coded workspace',
  INTEGRATION_SOURCE.includes('fileURLToPath(import.meta.url)') &&
    INTEGRATION_SOURCE.includes('WORKBENCH_SERVICE_PYTHON') &&
    INTEGRATION_SOURCE.includes('run_service.py') &&
    !INTEGRATION_SOURCE.includes('C:\\Users') && !INTEGRATION_SOURCE.includes('/Users/'))
check('demo credentials have a local write entry that never echoes the secret',
  INTEGRATION_SOURCE.includes('DEMO_SETUP_PATH') &&
    INTEGRATION_SOURCE.includes('async handleDemoSetup(') &&
    INTEGRATION_SOURCE.includes('demoEnvFilePath()') &&
    INTEGRATION_SOURCE.includes('mode: 0o600') &&
    INTEGRATION_SOURCE.includes('WORKBENCH_DEMO_API_KEY=') &&
    INTEGRATION_SOURCE.includes("'credentials', 'demo.env'") &&
    INTEGRATION_SOURCE.includes('fingerprint: credentialFingerprint(apiKey)'))
check('nothing guesses a python: falling back to a system interpreter is explicit',
  INTEGRATION_SOURCE.includes('function resolveSystemPython(') &&
    INTEGRATION_SOURCE.includes("path.join(dataDir, 'service-venv')") &&
    INTEGRATION_SOURCE.includes('function fileExists('))
check('the settings page can enter a demo key (zh/en copy present)',
  SOURCE.includes('DEMO_SETUP_URL') &&
    SOURCE.includes('function DemoOnboarding(') &&
    SOURCE.includes("'demo.save'") &&
    SOURCE.includes('demoSetup'))
check('the Host never reports success before /health answers',
  INTEGRATION_SOURCE.includes('if (lastProbe.ok)') &&
    INTEGRATION_SOURCE.includes("state: 'started'") &&
    INTEGRATION_SOURCE.includes('E_SERVICE_START_TIMEOUT'))
check('the spawned service env is normalised (Node NO_PROXY brackets break httpx)',
  INTEGRATION_SOURCE.includes('serviceSpawnEnv') && INTEGRATION_SOURCE.includes('NO_PROXY'))
check('/state carries the capability so the UI can hide a dead button',
  INTEGRATION_SOURCE.includes('service_start: this.serviceStartCapability()') &&
    SOURCE.includes('serviceStart: stateData?.service_start') &&
    SOURCE.includes("startServiceSupported(model)"))
check('the start copy exists in both languages',
  ['s2.startService', 'svc.starting', 'svc.started', 'svc.startFailed', 'svc.missing']
    .every((key) => key in (dict.zh ?? {}) && key in (dict.en ?? {})) &&
    (dict.zh['s2.startService'] ?? '') === '启动服务')

// ---- 19. 17 §1/§4.2/§4.3: distribution preconditions ------------------------
const PLUGIN_PKG = JSON.parse(readFileSync(new URL('../package.json', import.meta.url), 'utf8'))
const SETUP_SOURCE = readFileSync(new URL('../host/service-setup.js', import.meta.url), 'utf8')
check('dataDir never defaults into the package directory',
  INTEGRATION_SOURCE.includes('resolveDefaultDataDir') &&
    INTEGRATION_SOURCE.includes("path.join(home, 'data', 'trading-workbench')") &&
    !INTEGRATION_SOURCE.includes('dataDir = DEFAULT_DATA_DIR,'))
check('an existing install keeps its data (no silent migration on upgrade)',
  INTEGRATION_SOURCE.includes('legacyDir') && INTEGRATION_SOURCE.includes('return legacyDir'))
check('the bundle declares the DSH version it was verified on',
  typeof PLUGIN_PKG.engines?.dsh === 'string' && PLUGIN_PKG.engines.dsh.includes('0.2.2-rc.1') &&
    typeof PLUGIN_PKG.engines?.node === 'string')
check('the launcher and the setup both branch per platform (Scripts vs bin)',
  INTEGRATION_SOURCE.includes("'Scripts' : 'bin'") && SETUP_SOURCE.includes("'Scripts' : 'bin'") &&
    SETUP_SOURCE.includes("'python.exe' : 'python'"))
check('the first-run wizard refuses to install without a Python',
  SETUP_SOURCE.includes("code: 'PYTHON_MISSING'") && SETUP_SOURCE.includes('needsPython'))
check('the setup copy exists in both languages',
  ['s2.setupService', 'setup.title', 'setup.start', 'setup.running', 'setup.pythonMissing', 'setup.failed']
    .every((key) => key in (dict.zh ?? {}) && key in (dict.en ?? {})) &&
    (dict.zh['s2.setupService'] ?? '') === '初始化服务')

// ---- report --------------------------------------------------------------
for (const line of notes) console.log(line)
if (failures.length > 0) {
  console.error(`\nFAILED ${failures.length} check(s):`)
  for (const failure of failures) console.error(` - ${failure}`)
  process.exit(1)
}
console.log(`\nAll ${notes.length} checks passed.`)
