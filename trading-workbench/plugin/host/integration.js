/**
 * M1.3c Host integration.
 *
 * One manual round:
 *   service snapshot -> ai-chain (symmetric two rounds) -> P1 -> P2
 *   -> AI audit persistence -> cycle finalize.
 *
 * LLM transport is `ctx.llm.stream` by default. Setting
 * WORKBENCH_LLM_MODE=session_fixture swaps in a deterministic in-conversation
 * LLM (see ./session-llm.js) so the integration can be tested without an
 * external provider; that mode also forbids signed order submission.
 *
 * The only network peer is the loopback workbench-service; every `/api/*` call
 * carries `Authorization: Bearer <data/.api-token>`. `/api/cycles` provides
 * the server-side md-evidence/1 snapshot, and the ai-runs/calls/artifacts
 * endpoints persist the round's audit. The local RoundView JSON is retained
 * as a fallback when those endpoints are unavailable (404).
 * It never fabricates an approval, a service fact or a successful order.
 */

import { createHash, randomUUID } from 'node:crypto'
import { spawn } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { buildEvidenceView, evidenceSummary, runChain } from './ai/index.js'
import {
  CHAIN_DEADLINE_MS,
  DEFAULT_EXECUTION_GATE_MODE,
  EVIDENCE_MAX_AGE_MS,
  ENTRY_SNAPSHOT_MAX_AGE_MS,
  EXECUTION_GATE_MODES,
  MAX_ATTEMPTS_PER_CALL,
  MAX_CALLS,
  ROLE_EFFORT,
  ROLE_MAX_TOKENS,
} from './ai/constants.js'
import { evaluateEntryPlan } from './ai/semantics.js'
import { resolveHarnessRoute, streamPrompt } from './ai/llm.js'
import { createSessionLlm, SESSION_LLM_SCRIPTS, SESSION_LLM_TRANSPORT } from './session-llm.js'
import { ROLE_LABELS, toRoundSummary, toRoundView } from './round-view.js'
import { RoundArchive, archiveDay, parseRoundDate } from './round-archive.js'
import { createServiceSetup } from './service-setup.js'
import { currentBuild, setLoadedConfigRevision } from './build.js'
import {
  LLM_CONFIG_FILE,
  PRESETS,
  PRESET_NAMES,
  budgetPreview,
  createLlmCatalog,
  ensureFirstRunConfig,
  freezeRound,
  materializePreset,
  readConfig,
  reviewRouteFromConfig,
  saveConfig,
} from './llm-config.js'
import {
  buildReviewContext,
  dateKeyInShanghai,
  DEFAULT_REVIEW_TIMEOUT_MS,
  generateDailyReview,
  isValidReviewDate,
} from './review.js'
import {
  DEFAULT_DATA_DIR,
  DEFAULT_SERVICE_URL,
  ServiceClientError,
  WorkbenchServiceClient,
  loadApiToken,
} from './service-client.js'
export const API_PREFIX = '/trading-workbench/api'
export const RUN_PATH = `${API_PREFIX}/run`
export const STATE_PATH = `${API_PREFIX}/state`
export const ROUNDS_PATH = `${API_PREFIX}/rounds`
/** 15 §4: the archived days the date picker may offer (newest first). */
export const ROUNDS_DATES_PATH = `${ROUNDS_PATH}/dates`
export const MARKET_PRICES_PATH = `${API_PREFIX}/market/prices`
export const RISK_ENVELOPE_PATH = `${API_PREFIX}/risk/envelope`
export const CLOSE_PATH = `${API_PREFIX}/positions/close`
export const REPAIR_PATH = `${API_PREFIX}/protection/repair`
export const CYCLE_EVIDENCE_PATH = `${API_PREFIX}/cycles`
export const RECONCILE_PATH = `${API_PREFIX}/reconcile`
export const REVIEWS_DAILY_PATH = `${API_PREFIX}/reviews/daily`
export const LLM_PROBE_PATH = `${API_PREFIX}/llm/probe`
export const CONTROL_PATH = `${API_PREFIX}/control`
// F1 (§3.2/§8·F): the top-bar primary action. Two routes, one owner each:
//  - /automation/stop  = close the local scheduler + POST /api/control pause
//  - /automation/start = re-verify, then control normal + arm the scheduler
export const AUTOMATION_STOP_PATH = `${API_PREFIX}/automation/stop`
export const AUTOMATION_START_PATH = `${API_PREFIX}/automation/start`
// 15 §10: one-click start of the local workbench-service. The Host only does
// what the operator script does (spawn the venv python with the same arguments)
// and never claims success before /health answers. Same-origin + loopback only;
// WORKBENCH_ALLOW_SERVICE_START=off disables the capability entirely.
export const SERVICE_START_PATH = `${API_PREFIX}/service/start`
export const SERVICE_START_TIMEOUT_MS = 45_000
// 16 §1: first-run bootstrap routes (readiness + one idempotent setup job).
export const SETUP_READINESS_PATH = `${API_PREFIX}/setup/readiness`
export const SERVICE_SETUP_PATH = `${API_PREFIX}/service/setup`
export const SERVICE_START_POLL_MS = 700
// Equivalent of /health for the *Host* half: /health lives in index.js and only
// reports the three load fields, so the runtime-verification script needs its
// own read-only route for data dir / service identity / automation state.
export const HOST_INFO_PATH = `${API_PREFIX}/host`
export const AUTOMATION_CONTRACT = 'trading-workbench/automation/1'
export const OPERATIONS_PATH = `${API_PREFIX}/operations`
// F2 (§8·F / t6): C's fund-caliber DTO (four equity calibers + four strategy books
// + matched/partial/unknown reconcile) is read-only. The client must not compute
// its own PnL, and an unavailable DTO must not fall back to zeros.
export const PNL_PATH = `${API_PREFIX}/pnl`
/** 实盘接入：唯一的凭据写入入口（写本机文件，不经过浏览器存储，不回显明文）。 */
export const LIVE_SETUP_PATH = `${API_PREFIX}/live-setup`
/**
 * Demo 接入：与 live-setup 同一套语义。
 *
 * 为什么它不是"插件配置项"：基線 §5.3 要求 Demo key 只放本机凭据存储，
 * 不进入插件配置/日志/前端 bundle。cordis 的 plugin config 会随 profile 导出，
 * 所以 Demo key 只能走"写本机 0600 文件 + 只回指纹"这一条路。
 */
export const DEMO_SETUP_PATH = `${API_PREFIX}/demo-setup`

/** 只用于算凭据指纹；明文不参与任何返回值或日志。 */
function credentialFingerprint(value) {
  return createHash('sha256').update(String(value), 'utf8').digest('hex').slice(0, 8)
}

function fileExists(target) {
  if (typeof target !== 'string' || target.length === 0) return false
  try {
    return fs.statSync(target).isFile()
  } catch {
    return false
  }
}

/** PATH 里的第一个 python，返回绝对路径——能力判定要 stat 它，不能只认命令名。 */
function resolveSystemPython(env = process.env) {
  const win = process.platform === 'win32'
  const names = win ? ['python.exe', 'python3.exe'] : ['python3', 'python']
  const rawPath = String(env?.PATH ?? env?.Path ?? '')
  for (const entry of rawPath.split(path.delimiter)) {
    const dir = entry.trim().replace(/^"|"$/g, '')
    if (!dir) continue
    for (const name of names) {
      const candidate = path.join(dir, name)
      if (fileExists(candidate)) return candidate
    }
  }
  return null
}

/**
 * dataDir 的默认解析（分发前提，见 docs/bundle-DISTRIBUTION.md §4.2）。
 *
 * 顺序：
 *  1. 显式配置 / WORKBENCH_DATA_DIR 优先；
 *  2. 包内的 data/ 只要已经有文件就继续用它——否则升级会把现有轮次归档孤立掉；
 *  3. 都没有时落在用户目录下：包安装位置在 node_modules 里，升级即被替换；
 *     有 DSH_HOME 时用它（与宿主的其它数据放在一起），否则用 ~/.dsh。
 */
export function resolveDefaultDataDir(env = process.env, legacyDir = DEFAULT_DATA_DIR) {
  const explicit = String(env?.WORKBENCH_DATA_DIR ?? '').trim()
  if (explicit) return path.resolve(explicit)
  try {
    if (legacyDir && fs.existsSync(legacyDir) && fs.readdirSync(legacyDir).length > 0) {
      return legacyDir
    }
  } catch {
    // 包目录不可读时不要因噎废食：回落到用户目录即可。
  }
  const home = String(env?.DSH_HOME ?? '').trim() || path.join(os.homedir(), '.dsh')
  return path.join(home, 'data', 'trading-workbench')
}
export const DEFAULT_PNL_RANGE_S = 86_400
export const MAX_PNL_RANGE_S = 90 * 86_400
export const LLM_CATALOG_PATH = `${API_PREFIX}/llm/catalog`
export const LLM_CONFIG_PATH = `${API_PREFIX}/llm/config`
export const MAX_PRICE_SYMBOLS = 10
export const MAX_REPAIR_SYMBOLS = 10
export const MAX_COMMAND_BODY_BYTES = 8 * 1024
export const MAX_REVIEW_BODY_BYTES = 2 * 1024
// 15 §4: GET /rounds?date= answers one day at a time. The ceiling is far above
// the 96/day schedule limit and only exists so a corrupted partition cannot
// build a multi-megabyte response.
export const MAX_ROUNDS_PER_DAY = 2000
export const HOST_ROUNDS_CONTRACT = 'trading-workbench/host-rounds/1'
export const DEFAULT_SYMBOL = 'BTCUSDT'
export const MAX_BODY_BYTES = 16 * 1024
export const DEFAULT_RUN_TIMEOUT_MS = 400_000
// 15 §3: rounds are archived per day and never trimmed. The old 100-row cap
// (DEFAULT_STORE_MAX_ROUNDS) existed only because the single JSON cache was
// rewritten whole on every round; it is gone with the day-partitioned archive.
// A service cycle RUNNING longer than this is treated as an orphan from a dead
// Host run. Rounds are capped at DEFAULT_RUN_TIMEOUT_MS (400s), so 15 minutes is
// safely past any live round and still short enough to self-heal the scheduler.
export const CYCLE_STALE_MS = 15 * 60_000
export const PORTFOLIO_CACHE_MS = 8_000

export const SERVICE_GAP_DEMO_KEY = 'No Demo API key/secret: P2 never submits a signed order. Enter it in Settings -> Connection & account (demo setup), or set WORKBENCH_DEMO_API_KEY/SECRET for the service process.'
export const SERVICE_GAP_FIXTURE_MODE = 'WORKBENCH_LLM_MODE=session_fixture replaces ctx.llm with deterministic in-conversation outputs and forbids order submission.'

/**
 * 03 C6 + the 2026-11-12 measurement: these "static" gaps must describe THIS
 * profile, not a template. The Demo-key line is only true when the service
 * reports credentials_configured=false; the fixture line only in that mode.
 */
export function serviceGaps({ credentialsConfigured = null, llmMode = 'harness' } = {}) {
  const gaps = []
  if (credentialsConfigured === false) gaps.push(SERVICE_GAP_DEMO_KEY)
  if (llmMode === 'session_fixture') gaps.push(SERVICE_GAP_FIXTURE_MODE)
  return gaps
}

const SYMBOL_RE = /^[A-Z0-9]{2,20}USDT$/
const CYCLE_ID_RE = /^[A-Za-z0-9._:-]{1,64}$/
const CYCLE_EVIDENCE_SUFFIX = '/evidence'
const CONTROL_BLOCKING = new Set(['close_only', 'paused', 'pause', 'emergency', 'halted'])

export class HostIntegrationError extends Error {
  constructor(code, message, { status = 500, retryable = false, details = {}, round = null } = {}) {
    super(message)
    this.name = 'HostIntegrationError'
    this.code = code
    this.status = status
    this.retryable = retryable
    this.details = details
    this.round = round
  }
}

function nowIso(now) {
  return new Date(now).toISOString()
}

/**
 * Parse the read-only `?symbols=` list for /market/prices: comma separated,
 * trimmed/uppercased, de-duplicated, at most MAX_PRICE_SYMBOLS and BTCUSDT
 * when the parameter is absent or empty. No symbol is fabricated beyond the
 * documented default.
 */
export function parsePriceSymbols(raw, { max = MAX_PRICE_SYMBOLS, fallback = DEFAULT_SYMBOL } = {}) {
  const seen = new Set()
  const symbols = []
  for (const part of String(raw ?? '').split(',')) {
    const symbol = part.trim().toUpperCase()
    if (!symbol || seen.has(symbol)) continue
    seen.add(symbol)
    symbols.push(symbol)
    if (symbols.length >= max) break
  }
  return symbols.length > 0 ? symbols : [fallback]
}

/** Clamp ?range_s= to a sane window; the service already floors it at 300s. */
export function parsePnlRange(raw, { fallback = DEFAULT_PNL_RANGE_S, max = MAX_PNL_RANGE_S } = {}) {
  const value = Number(raw)
  if (!Number.isFinite(value) || value < 1) return fallback
  return Math.min(max, Math.max(300, Math.floor(value)))
}

/**
 * Command-route helpers for the two destructive Demo actions (close / repair).
 * They reject rather than coerce: a missing confirmation or reason must never
 * fall back to a permissive default.
 */
function requireJsonRequest(req) {
  const raw = req.headers['content-type']
  const mediaType = typeof raw === 'string' ? raw.split(';')[0].trim().toLowerCase() : ''
  if (mediaType !== 'application/json') {
    throw new HostIntegrationError('E_CONTENT_TYPE_JSON_REQUIRED', 'content-type must be application/json', { status: 415 })
  }
}

function commandSymbol(raw) {
  const symbol = typeof raw === 'string' ? raw.trim().toUpperCase() : ''
  if (!SYMBOL_RE.test(symbol)) {
    throw new HostIntegrationError('E_SYMBOL_INVALID', 'symbol must look like BTCUSDT', {
      status: 422,
      details: { symbol: raw ?? null },
    })
  }
  return symbol
}

function commandReason(raw) {
  const reason = typeof raw === 'string' ? raw.trim() : ''
  if (reason.length < 2) {
    throw new HostIntegrationError('E_REASON_REQUIRED', 'reason must be at least 2 characters', { status: 422 })
  }
  return reason
}

function commandQty(raw) {
  if (raw === undefined || raw === null || raw === '') return null
  const text = String(raw).trim()
  if (!/^[0-9]+(\.[0-9]+)?$/.test(text) || Number(text) <= 0) {
    throw new HostIntegrationError('E_QTY_INVALID', 'qty must be a positive number', {
      status: 422,
      details: { qty: raw },
    })
  }
  return text
}

function commandSymbols(raw) {
  if (raw === undefined || raw === null) return undefined
  const list = Array.isArray(raw) ? raw : String(raw).split(',')
  const seen = new Set()
  const symbols = []
  for (const item of list) {
    if (typeof item !== 'string' || item.trim() === '') continue
    const symbol = commandSymbol(item)
    if (seen.has(symbol)) continue
    seen.add(symbol)
    symbols.push(symbol)
  }
  if (symbols.length > MAX_REPAIR_SYMBOLS) {
    throw new HostIntegrationError('E_SYMBOLS_TOO_MANY', `at most ${MAX_REPAIR_SYMBOLS} symbols per request`, {
      status: 422,
      details: { count: symbols.length },
    })
  }
  return symbols.length > 0 ? symbols : undefined
}

/**
 * A 2xx response can still carry the service error envelope (e.g. HTTP 202
 * E_FILL_UNCONFIRMED). Never turn that into `ok: true` for a command route.
 */
function throwIfServiceErrorEnvelope(result) {
  const error = result?.error
  if (!error || typeof error !== 'object') return result
  throw new HostIntegrationError(error.code ?? 'E_SERVICE_REJECTED', error.message ?? 'service reported an error', {
    status: 502,
    retryable: Boolean(error.retryable),
    details: { ...(error.details ?? {}), trace_id: result?.trace_id ?? null },
  })
}

/** Map LLM/transport failures to explicit Host errors; never fake a report. */
function reviewHostError(error, date) {
  if (error instanceof HostIntegrationError) return error
  const code = error?.code ?? 'E_REVIEW_FAILED'
  if (code === 'LLM_TIMEOUT' || code === 'CHAIN_DEADLINE') {
    return new HostIntegrationError('E_REVIEW_TIMEOUT', 'daily review timed out; no cache was written', {
      status: 504,
      retryable: true,
      details: { date, reason: code },
    })
  }
  if (code === 'NO_LLM' || code === 'NO_ADAPTER') {
    return new HostIntegrationError('NO_ADAPTER', error?.message ?? 'No Harness LLM is available', {
      status: 503,
      details: { date, reason: code },
    })
  }
  return new HostIntegrationError('E_REVIEW_FAILED', error?.message ?? String(error), {
    status: 502,
    retryable: false,
    details: { date, reason: code },
  })
}

function cycleIdFor(now) {
  const stamp = new Date(now).toISOString().replace(/[-:]/g, '').replace(/\.\d{3}Z$/, 'Z')
  return `cyc-${stamp}-${randomUUID().slice(0, 6)}`
}

function finite(value) {
  const n = typeof value === 'number' ? value : Number(value)
  return Number.isFinite(n) ? n : null
}

/**
 * Read the service's envelope limits.
 *
 * A (§8·A, t2) changed the rules: confidence no longer selects a larger risk or
 * leverage budget. The `strong_*` values are still mapped here so historical
 * rounds and the legacy display keep their own snapshot, but they are marked
 * `used_for_execution_authorization: false` and are NOT handed to the AI chain
 * (see STRONG_TIER_CAPS in run()), so no role can believe a 2× budget exists.
 */
/**
 * T15 (§8·D) — the gate verdict must be *auditable*, not just enforced.
 *
 * The chain returns outputs.gate (evaluateEntryPlan + mode/enforced/quote) and
 * outputs.plan_gaps, plus a round-level failureCategory. Forwarding three raw
 * objects into the audit table would let any future field leak, so this is a
 * fixed whitelist: code / sub_code / detail / checked_at / mode / enforced /
 * quote (+ ok, without which "blocked" and "passed" look identical).
 *
 * Nothing here decides anything. The decision stays in ai/semantics.js.
 */
export const GATE_AUDIT_KEYS = Object.freeze([
  'ok', 'code', 'sub_code', 'detail', 'checked_at', 'mode', 'enforced', 'quote',
])
export const PLAN_GAP_KEYS = Object.freeze(['code', 'field', 'reason_zh'])
export const MAX_PLAN_GAP_ROLES = 3
export const MAX_PLAN_GAPS_PER_ROLE = 3

export function projectGateAudit(gate) {
  if (!gate || typeof gate !== 'object' || Array.isArray(gate)) return null
  const quote = gate.quote && typeof gate.quote === 'object' && !Array.isArray(gate.quote)
    ? {
      last: typeof gate.quote.last === 'number' && Number.isFinite(gate.quote.last) ? gate.quote.last : null,
      age_ms: typeof gate.quote.age_ms === 'number' && Number.isFinite(gate.quote.age_ms) ? gate.quote.age_ms : null,
      // The only two allowed provenance values: the frozen build-time price, or a
      // price fetched right before submitting. Never "some price".
      source: gate.quote.source === 'pre_submit' ? 'pre_submit'
        : (gate.quote.source === 'evidence_build' ? 'evidence_build' : null),
    }
    : null
  const projected = {
    ok: gate.ok === true,
    code: typeof gate.code === 'string' ? gate.code : null,
    sub_code: typeof gate.sub_code === 'string' ? gate.sub_code : null,
    detail: typeof gate.detail === 'string' ? gate.detail : null,
    checked_at: Number.isFinite(Number(gate.checked_at)) ? Number(gate.checked_at) : null,
    mode: typeof gate.mode === 'string' ? gate.mode : null,
    enforced: gate.enforced === true,
    quote,
  }
  return projected
}

export function projectPlanGaps(outputs) {
  const raw = outputs?.plan_gaps
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null
  const out = {}
  for (const [role, list] of Object.entries(raw).slice(0, MAX_PLAN_GAP_ROLES)) {
    if (!Array.isArray(list) || list.length === 0) continue
    out[role] = list
      .filter((item) => item && typeof item === 'object')
      .slice(0, MAX_PLAN_GAPS_PER_ROLE)
      .map((item) => ({
        code: typeof item.code === 'string' ? item.code : null,
        field: typeof item.field === 'string' ? item.field : null,
        reason_zh: typeof item.reason_zh === 'string' ? item.reason_zh : null,
      }))
  }
  return Object.keys(out).length > 0 ? out : null
}

/**
 * One projection used twice: (a) attached to the execution artifact so the
 * ledger can answer "why didn't this round execute?", (b) handed to
 * round-view.js so the UI renders the same facts.
 */
/**
 * T15 (§8·D) — the pre-submit gate.
 *
 * The chain's gate ran when the evidence was built; measured evidence→submit gaps
 * are 60–304s (median 177s), so a plan can expire while it waits. This re-runs the
 * SAME ai/semantics.js#evaluateEntryPlan right before submitting — never a second
 * implementation — and records the verdict next to the round.
 *
 * Two honest limits, both recorded rather than papered over:
 *  - no re-quote: the Host cannot obtain the submit-time price (Service refreshes
 *    it inside submit()), so the quote stays source=evidence_build and price-based
 *    codes must never block;
 *  - only VALID_UNTIL_EXPIRED is time-based, so only it is allowed to block, and
 *    only when the gate mode is enforce (which stays off until re-quote exists).
 */
export function preSubmitGateOf({ decision, now, mode = DEFAULT_EXECUTION_GATE_MODE } = {}) {
  if (mode === EXECUTION_GATE_MODES.OFF) return null
  const checkedAt = Number.isFinite(now) ? now : Date.now()
  const verdict = evaluateEntryPlan({
    plan: decision?.plan ?? null,
    quote: null,
    now: checkedAt,
    backgroundMaxAgeMs: EVIDENCE_MAX_AGE_MS,
    entryMaxAgeMs: ENTRY_SNAPSHOT_MAX_AGE_MS,
  })
  const timeBased = verdict.ok === false && verdict.code === 'VALID_UNTIL_EXPIRED'
  return {
    ok: verdict.ok !== false,
    code: verdict.code ?? null,
    sub_code: verdict.sub_code ?? null,
    detail: verdict.detail ?? null,
    checked_at: checkedAt,
    mode,
    enforced: mode === EXECUTION_GATE_MODES.ENFORCE && timeBased,
    // Everything price-dependent is advisory: we did not re-quote.
    advisory_only: !(mode === EXECUTION_GATE_MODES.ENFORCE && timeBased),
    quote: { last: null, age_ms: null, source: 'evidence_build' },
    requote: 'unavailable',
    note: 'pre_submit 未重新报价（Host 拿不到提交价；Service 会在 submit() 内自行强制刷新），因此只有时间类判定（VALID_UNTIL_EXPIRED）允许拦截。',
  }
}

/** ISO timestamp -> positive epoch-ms, or null (never 0 / NaN). */
export function epochMsOf(value) {
  if (typeof value === 'number' && Number.isFinite(value) && value > 0) return Math.round(value)
  if (typeof value !== 'string' || !value) return null
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null
}

/**
 * T15 (§8·D): only a failed / truncated / non-ok call is worth its raw text, and
 * it must be stored whole. The chain keeps response_sha256, not the text, so this
 * returns null unless the record really carries it — the Host must not
 * reconstruct, truncate or invent model output.
 */
export function failedCallText(call) {
  if (!call || typeof call !== 'object') return null
  const ok = call.schema_ok === true && (call.status === undefined || call.status === 'ok')
  const truncated = call.finish_kind === 'max_tokens' || call.finish_reason === 'max_tokens'
  if (ok && !truncated && !call.error_code) return null
  const text = call.response_text ?? call.raw_text ?? call.text ?? null
  return typeof text === 'string' && text.length > 0 ? text : null
}

export function gateAuditOf(result) {
  const gate = projectGateAudit(result?.outputs?.gate)
  const planGaps = projectPlanGaps(result?.outputs)
  const failureCategory = typeof result?.failureCategory === 'string' && result.failureCategory
    ? result.failureCategory
    : null
  if (!gate && !planGaps && !failureCategory) return null
  return { gate, plan_gaps: planGaps, failure_category: failureCategory }
}

export function capsFromEnvelope(envelope) {
  const perTrade = envelope?.limits?.per_trade ?? {}
  return {
    maxRiskPct: finite(perTrade.max_risk_pct) === null ? undefined : Number(perTrade.max_risk_pct) * 100,
    strongMaxRiskPct: finite(perTrade.strong_max_risk_pct) === null ? undefined : Number(perTrade.strong_max_risk_pct) * 100,
    maxLeverage: Number.isInteger(perTrade.max_leverage) ? perTrade.max_leverage : undefined,
    strongMaxLeverage: Number.isInteger(perTrade.strong_max_leverage) ? perTrade.strong_max_leverage : undefined,
    minRr: finite(perTrade.min_risk_reward) ?? undefined,
    slMinPct: finite(perTrade.sl_min_distance_pct) === null ? undefined : Number(perTrade.sl_min_distance_pct) * 100,
    slMaxPct: finite(perTrade.sl_max_distance_pct) === null ? undefined : Number(perTrade.sl_max_distance_pct) * 100,
    maxNotionalMultiple: finite(perTrade.max_notional_multiple) ?? undefined,
    // A (§8·A): the strong tier is a read-only legacy field. Nothing downstream
    // may scale risk/leverage by confidence.
    used_for_execution_authorization: {
      confidence: false,
      strong_tier: false,
    },
  }
}

// Caps derived from the legacy strong tier. They are dropped before the AI chain
// sees its caps object: prompt constraints must describe ONE budget.
const STRONG_TIER_CAPS = new Set(['strongMaxRiskPct', 'strongMaxLeverage'])

function controlState(envelope) {
  return envelope?.control?.state ?? envelope?.control_state ?? 'normal'
}

/**
 * The service control row as the UI needs it: state plus why/when/by whom.
 * A control state without a reason cannot be explained, so it is kept as-is
 * and marked unknown rather than defaulted.
 */
function controlInfoOf(control) {
  if (!control || typeof control !== 'object') return null
  const state = typeof control.state === 'string' && control.state ? control.state : null
  if (!state) return null
  return {
    state,
    reason: typeof control.reason === 'string' && control.reason ? control.reason : null,
    updated_at: typeof control.updated_at === 'string' && control.updated_at ? control.updated_at : null,
    updated_by: typeof control.updated_by === 'string' && control.updated_by ? control.updated_by : null,
  }
}

// Control states that stop new entries. 'pause' is the documented non-flattening
// operator stop; close_only/emergency are risk-engine states and must read as
// "blocked by rules", not as "auto trading stopped by the user".
const CONTROL_STOP_STATE = 'pause'
const CONTROL_RULE_STATES = new Set(['close_only', 'emergency', 'halted'])

function leadingSkipCount(schedule) {
  const history = Array.isArray(schedule?.history) ? schedule.history : []
  let count = 0
  for (const entry of history) {
    if (!entry || entry.kind !== 'skip') break
    count += 1
  }
  return count
}

/** Public shape of the last stop attempt (no token, no internal promise). */
function publicStopRecord(attempt) {
  if (!attempt || typeof attempt !== 'object') return null
  return {
    request_id: attempt.request_id ?? null,
    reason: attempt.reason ?? null,
    requested_at: attempt.requested_at ?? null,
    phase: attempt.phase ?? null,
    scheduler_stopped_at: attempt.scheduler_stopped_at ?? null,
    confirmed_at: attempt.confirmed_at ?? null,
    error: attempt.error ?? null,
    service_control: attempt.service_control ?? null,
  }
}

function schedulerReachable(service) {
  if (!service || typeof service !== 'object') return null
  if (typeof service.state === 'string' && service.state) return service.state === 'online'
  if (typeof service.ready === 'boolean') return service.ready === true
  return null
}

function serviceStatusForError(status, code = null) {
  if (status === 401 || status === 403) return 'unauthorized'
  if (status === 409) return 'conflict'
  if (status === 422) return 'rejected'
  if (status === 423) return 'locked'
  if (status === 503 || status === null || status === undefined) return 'unavailable'
  if (typeof status === 'number' && status >= 500) return 'unavailable'
  return code ? 'error' : 'unknown'
}

function errorStatus(stage, status, code, message = null, retryable = false) {
  return {
    stage,
    httpStatus: Number.isInteger(status) ? status : null,
    code: code ?? null,
    message: message ?? null,
    retryable: Boolean(retryable),
  }
}

function buildServiceEvidence({ cycleResponse, symbol, now }) {
  const wrapper = cycleResponse?.evidence
  const md = wrapper?.evidence
  if (!md || typeof md !== 'object' || Array.isArray(md)) {
    throw new HostIntegrationError('E_EVIDENCE_UNAVAILABLE', 'POST /api/cycles returned no service md-evidence/1', {
      status: 503,
      retryable: true,
      details: { cycle_id: cycleResponse?.cycle_id ?? null },
    })
  }
  const snapshotId = wrapper.snapshot_id ?? md.snapshot_id
  const sha256 = wrapper.sha256
  const producedAt = md.produced_at
  if (!snapshotId || !sha256 || !producedAt) {
    throw new HostIntegrationError('E_EVIDENCE_UNAVAILABLE', 'service evidence identity is incomplete', {
      status: 503,
      details: {
        cycle_id: cycleResponse?.cycle_id ?? null,
        snapshot_id: snapshotId ?? null,
        sha256: sha256 ?? null,
      },
    })
  }
  const symbols = md.symbols
  if (!symbols || typeof symbols !== 'object' || Array.isArray(symbols) || Object.keys(symbols).length === 0) {
    throw new HostIntegrationError('E_EVIDENCE_UNAVAILABLE', 'service evidence has no symbols', {
      status: 503,
      details: { cycle_id: cycleResponse?.cycle_id ?? null },
    })
  }
  // md-evidence/1 stores candles as a columnar object; the AI projection only
  // accepts array candle lists. Keep the service identity/data and drop the
  // non-array candle containers before projection.
  const symbolsForView = {}
  for (const [key, value] of Object.entries(symbols)) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) continue
    const clone = structuredClone(value)
    if (clone.candles !== undefined && !Array.isArray(clone.candles)) delete clone.candles
    if (clone.bars !== undefined && !Array.isArray(clone.bars)) delete clone.bars
    if (clone.timeframes && typeof clone.timeframes === 'object') {
      for (const tf of Object.values(clone.timeframes)) {
        if (tf && typeof tf === 'object' && tf.candles !== undefined && !Array.isArray(tf.candles)) {
          delete tf.candles
        }
      }
    }
    symbolsForView[key] = clone
  }
  const primary = typeof symbol === 'string' && symbolsForView[symbol]
    ? symbol
    : (md.universe?.deep?.find((entry) => entry?.symbol && symbolsForView[entry.symbol])?.symbol
      ?? Object.keys(symbolsForView)[0])
  if (!primary) {
    throw new HostIntegrationError('E_EVIDENCE_UNAVAILABLE', 'service evidence has no primary symbol', {
      status: 503,
      details: { cycle_id: cycleResponse?.cycle_id ?? null },
    })
  }
  const view = buildEvidenceView({
    snapshot_id: String(snapshotId),
    cycle_id: String(md.cycle_id ?? cycleResponse.cycle_id),
    produced_at: producedAt,
    as_of: typeof md.as_of === 'string' ? md.as_of : producedAt,
    data_health: md.data_health,
    market: md.market,
    primary_symbol: primary,
    symbols: symbolsForView,
    delivery: md.delivery,
  }, { now })
  return {
    view,
    ref: {
      snapshot_id: String(snapshotId),
      sha256: String(sha256),
      path: String(wrapper.path ?? ''),
      bytes: Number.isFinite(wrapper.bytes) ? wrapper.bytes : null,
      produced_at: producedAt,
      verdict: md.data_health?.verdict ?? wrapper.verdict ?? null,
      schema_version: md.schema_version ?? wrapper.schema_version ?? 'md-evidence/1',
    },
  }
}

function artifactCallId(result, agent, attempt) {
  const role = agent === 'red' || agent === 'blue' ? `${agent}_team` : agent
  const round = agent === 'red' || agent === 'blue' ? attempt : 1
  const call = (result?.calls ?? []).find((entry) => (
    entry?.role === role && (entry?.round === round || entry?.phase === round)
  ))
  return call?.call_id ?? null
}

function planForService(execution, cycleId) {
  const order = execution?.order_plan
  if (!order) {
    throw new HostIntegrationError('E_PLAN_MISSING', 'execution did not produce an order plan', {
      status: 422,
      details: { stage: 'P1' },
    })
  }
  return {
    symbol: order.symbol,
    side: order.side,
    action: order.action ?? 'open',
    entry_type: order.entry_type ?? 'MARKET',
    leverage: order.leverage,
    qty: order.qty,
    sl: order.sl,
    tp: order.tp,
    confidence: typeof order.confidence === 'number' ? order.confidence : 0.6,
    reasoning_ref: `ai-chain:${cycleId}`,
  }
}

function p1ResultFromService(response) {
  const verdict = response?.verdict ?? {}
  const approval = response?.approval ?? {}
  const kind = verdict.verdict
  return {
    verdict: kind === 'approved' || kind === 'reduced' ? kind : 'rejected',
    code: kind === 'approved' || kind === 'reduced' ? null : (verdict.blocking_rule ? `E_RISK_${verdict.blocking_rule}` : 'P1_REJECTED'),
    detail: verdict.blocked_reason ?? null,
    approved_qty: approval.approved_qty ?? verdict.approved_qty ?? null,
    leverage: verdict.leverage ?? approval.leverage ?? null,
    approval: approval.token
      ? {
          jti: approval.jti ?? null,
          plan_hash: approval.plan_hash ?? null,
          approved_qty: approval.approved_qty ?? null,
          expires_at_ms: approval.expires_at_ms ?? null,
        }
      : null,
    plan: response?.plan ?? null,
    raw: response,
    token: approval.token ?? null,
  }
}

function p2ResultFromService(response) {
  return {
    ok: true,
    status: 200,
    order_id: response?.order_id ?? response?.orderId ?? response?.order?.orderId ?? null,
    client_order_id: response?.client_order_id ?? response?.clientOrderId ?? null,
  }
}

function summarizeService({ baseUrl, ready, envelope, status, tokenInfo, now, authMode = null }) {
  const serviceReady = ready?.service_ready === true || ready?.ready === true
  return {
    base_url: baseUrl,
    token_present: Boolean(tokenInfo?.present),
    token_source: tokenInfo?.source ?? 'none',
    auth_mode: authMode ?? ready?.auth?.mode ?? null,
    ready: serviceReady,
    service_ready: serviceReady,
    demo_only: envelope?.demo_only === true,
    ledger_id: envelope?.ledger_id ?? null,
    // §4.2：环境从服务端事实取（envelope 或 status 任一上报即可），不在这里猜。
    venue: (typeof envelope?.venue === 'string' && envelope.venue)
      || (typeof status?.venue === 'string' && status.venue)
      || null,
    control_state: controlState(envelope),
    credentials_configured: status?.credentials_configured === true,
    new_entry_allowed: envelope?.new_entry_allowed === true,
    checked_at: nowIso(now),
  }
}

function serviceStateLabel(service) {
  if (service?.ready === true) return 'online'
  if (service?.token_present === false || service?.token_configured === false) return 'no_token'
  if (service?.error_status === 401 || service?.error_status === 403
    || service?.error_code === 'E_AUTH_REQUIRED' || service?.error_code === 'E_SERVICE_UNAUTHORIZED') return 'auth_failed'
  if (service?.error_code === 'E_SERVICE_OFFLINE' || service?.error_code === 'E_SERVICE_TIMEOUT') return 'offline'
  if (service?.error_code === 'E_SERVICE_UNAVAILABLE' || service?.error_status === 503) return 'unavailable'
  if (service?.ready === false) return 'not_ready'
  return 'offline'
}

function stableServiceBlock(baseService, portfolioService, now) {
  const merged = { ...(baseService ?? {}), ...(portfolioService ?? {}) }
  const tokenConfigured = typeof merged.token_present === 'boolean'
    ? merged.token_present
    : Boolean(merged.token_configured)
  return {
    ...merged,
    state: serviceStateLabel({ ...merged, token_configured: tokenConfigured }),
    ready: merged.ready === true,
    demo_only: typeof merged.demo_only === 'boolean' ? merged.demo_only : null,
    auth_mode: merged.auth_mode ?? null,
    ledger_id: merged.ledger_id ?? null,
    control_state: merged.control_state ?? 'unknown',
    credentials_configured: merged.credentials_configured === true,
    new_entry_allowed: merged.new_entry_allowed === true,
    checked_at: merged.checked_at ?? nowIso(now),
    base_url: merged.base_url ?? null,
    token_configured: tokenConfigured,
    token_present: tokenConfigured,
    last_run: baseService?.last_run ?? null,
    // §4.2：环境由服务端自报，Host 只透传，不推断。
    venue: typeof merged.venue === 'string' && merged.venue ? merged.venue : null,
  }
}

/**
 * 环境身份：唯一事实来源是服务端的 `venue` 字段。
 * 服务端没报就说 unknown，**不回退成 demo**——把未知说成 demo 会让用户
 * 在实盘上看到“模拟盘”徽标，这是最危险的一类谎言。
 */
function environmentOf(service, sourceFallback = 'service-guard') {
  const venue = typeof service?.venue === 'string' && service.venue ? service.venue : null
  return {
    venue,
    demo_only: service?.demo_only === true ? true : (venue === 'live' ? false : null),
    source: venue ? sourceFallback : 'unknown',
    known: Boolean(venue),
  }
}

/**
 * 03 C6: one diagnostic line per gap. A detail that merely repeats the code
 * ("E_SERVICE_TIMEOUT: E_SERVICE_TIMEOUT") carries no information.
 */
export function portfolioGapText(gapCode, detail = null) {
  const text = typeof detail === 'string' ? detail.trim() : ''
  return text && text !== gapCode ? `${gapCode}: ${text}` : gapCode
}

function emptyPortfolio(gapCode, detail = null) {
  return {
    fetchedAt: nowIso(Date.now()),
    cached: false,
    cacheHit: false,
    source: 'none',
    service: null,
    equityCurve: {
      range: 'all',
      points: [],
      count: 0,
      session: null,
      exchange: null,
      latest: null,
      empty: true,
      empty_reason: gapCode,
      source: 'empty',
      gap: gapCode,
    },
    equitySession: null,
    equityExchange: null,
    positions: [],
    positionsEmpty: true,
    positionsEmptyReason: 'NO_OPEN_POSITIONS',
    orders: [],
    ordersEmpty: true,
    ordersEmptyReason: 'NO_ORDERS',
    fills: [],
    fillsEmpty: true,
    fillsEmptyReason: 'NO_FILLS',
    reviews: [],
    reviewsEmpty: true,
    reviewsEmptyReason: gapCode === 'E_REVIEWS_ENDPOINT_MISSING' ? 'E_REVIEWS_ENDPOINT_MISSING' : gapCode,
    reviewsGap: gapCode,
    account: { empty: true, gap: gapCode, empty_reason: 'NO_ACCOUNT_VALUES', source: null },
    outbox: null,
    exchange: null,
    recovery: null,
    startup: null,
    protection: null,
    control: null,
    accountEmpty: true,
    accountGap: gapCode,
    gaps: [portfolioGapText(gapCode, detail)],
  }
}

function mapEquityPoint(point) {
  return {
    ts: point?.ts ?? null,
    session: point?.equity_session ?? null,
    exchange: point?.equity_exchange ?? null,
    available: point?.available ?? null,
    upnl: point?.upnl ?? null,
    margin_used: point?.margin_used ?? null,
  }
}

/**
 * Keep `protection_ok` three-valued like broker.py `protection_ok()`:
 * OK/PROTECTED -> true, MISSING/PARTIAL/DUPLICATE -> false, UNKNOWN or no
 * status (never reconciled) -> null. Legacy consumers only; new UI reads
 * `protection.status` directly.
 */
function protectionOkFor(status) {
  const normalized = typeof status === 'string' ? status.trim().toUpperCase() : status
  if (normalized === 'OK' || normalized === 'PROTECTED') return true
  if (normalized === 'UNKNOWN') return null
  if (normalized === null || normalized === undefined || normalized === '') return null
  return false
}

function mapPosition(position, index = 0) {
  const protection = position?.protection && typeof position.protection === 'object' ? position.protection : {}
  const protectionStatus = protection.status ?? position?.protection_status ?? null
  return {
    key: `${position?.symbol ?? 'unknown'}:${position?.position_side ?? position?.side ?? '?'}:${index}`,
    symbol: position?.symbol ?? null,
    side: position?.position_side ?? position?.side ?? null,
    qty: position?.qty ?? null,
    entry: position?.entry_price ?? null,
    mark: position?.mark_price ?? null,
    upnl: position?.upnl ?? null,
    sl: protection.sl ?? null,
    tp: protection.tp ?? null,
    protection: protectionStatus,
    protection_ok: protectionOkFor(protectionStatus),
    origin: position?.origin ?? null,
    leverage: position?.leverage ?? null,
    margin: position?.margin ?? null,
    margin_mode: position?.margin_mode ?? null,
    liq_price: position?.liq_price ?? null,
    stale: position?.stale ?? null,
    raw: position ?? null,
  }
}

function commandRequestId(body) {
  const value = body.request_id ?? `req-${randomUUID()}`
  if (typeof value !== 'string' || !/^[A-Za-z0-9_-]{8,80}$/.test(value)) {
    throw new HostIntegrationError('E_REQUEST_ID_INVALID', 'invalid request_id', { status: 422 })
  }
  return value
}

/**
 * B (t3) marks SUBMITTING/UNKNOWN rows as "待确认" (display=PENDING_CONFIRMATION),
 * never as a definitive failure. The order list has to carry that verbatim: the
 * UI decides the wording, but it must not have to guess the state.
 */
function mapOrder(order) {
  const status = order?.status === undefined ? null : order.status
  return {
    orderId: order?.exchange_order_id ?? order?.order_id ?? order?.orderId ?? null,
    clientOrderId: order?.client_order_id ?? order?.clientOrderId ?? null,
    symbol: order?.symbol ?? null,
    side: order?.side ?? null,
    action: order?.purpose ?? order?.action ?? null,
    qty: order?.qty ?? order?.quantity ?? order?.qty_requested ?? order?.qty_filled ?? null,
    type: order?.order_type ?? order?.type ?? null,
    status,
    display: order?.display ?? null,
    pendingConfirmation: order?.pending_confirmation === true,
    ageSeconds: Number.isFinite(Number(order?.age_s)) ? Number(order.age_s) : null,
    convergenceReason: order?.convergence_reason ?? null,
    businessKey: order?.business_key ?? null,
    intentId: order?.intent_id ?? null,
    price: order?.price ?? order?.avg_price ?? null,
    createdAt: order?.created_at ?? order?.createdAt ?? order?.time ?? null,
    raw: order ?? null,
  }
}

/**
 * T20 shape of /api/status.exchange — the ONLY place the UI may read exchange
 * reachability from. The input already answers "can we read right now / when
 * did it last work / when will we retry"; the Host must not fold it into a
 * top-level "stale" flag (top level has no such key, so reading it would give
 * undefined and read as "normal").
 */
export function projectExchange(exchange) {
  if (!exchange || typeof exchange !== 'object' || Array.isArray(exchange)) return null
  const numOr = (value) => (Number.isFinite(Number(value)) ? Number(value) : null)
  return {
    source: typeof exchange.source === 'string' ? exchange.source : null,
    reachable: exchange.reachable === true,
    stale: typeof exchange.stale === 'boolean' ? exchange.stale : null,
    last_success_at: typeof exchange.last_success_at === 'string' ? exchange.last_success_at : null,
    cooldown_until: typeof exchange.cooldown_until === 'string' ? exchange.cooldown_until : null,
    cooldown_remaining_s: numOr(exchange.cooldown_remaining_s),
    read_timeout_s: numOr(exchange.read_timeout_s),
    read_budget_s: numOr(exchange.read_budget_s),
    fetched_at: typeof exchange.fetched_at === 'string' ? exchange.fetched_at : null,
    unknown: Array.isArray(exchange.unknown) ? exchange.unknown.slice(0, 5).map((item) => String(item)) : [],
    error: exchange.error === null || exchange.error === undefined ? null : String(exchange.error),
  }
}

/** Read-only projection of /api/status.outbox (B: pending is not a failure). */
function projectOutbox(outbox) {
  if (!outbox || typeof outbox !== 'object' || Array.isArray(outbox)) return null
  const count = (value) => (Number.isFinite(Number(value)) ? Number(value) : null)
  const rows = Array.isArray(outbox.pending) ? outbox.pending : []
  return {
    pending_count: count(outbox.pending_confirmation_count) ?? rows.length,
    pending: rows.slice(0, 10).map((row) => ({
      clientOrderId: row?.client_order_id ?? null,
      businessKey: row?.business_key ?? null,
      symbol: row?.symbol ?? null,
      side: row?.side ?? null,
      status: row?.status ?? null,
      display: row?.display ?? 'PENDING_CONFIRMATION',
      pendingConfirmation: true,
      ageSeconds: Number.isFinite(Number(row?.age_s)) ? Number(row.age_s) : null,
    })),
    unknown: count(outbox.unknown),
    submitting: count(outbox.submitting),
    failed: count(outbox.failed),
    resolved: count(outbox.resolved),
    checked: count(outbox.checked),
  }
}

function mapFill(fill) {
  return {
    fillId: fill?.fill_id ?? fill?.id ?? fill?.trade_id ?? null,
    orderId: fill?.exchange_order_id ?? fill?.order_id ?? null,
    clientOrderId: fill?.client_order_id ?? null,
    symbol: fill?.symbol ?? null,
    side: fill?.side ?? null,
    qty: fill?.qty ?? fill?.quantity ?? null,
    price: fill?.price ?? null,
    realizedPnl: fill?.realized_pnl ?? fill?.realizedPnl ?? null,
    commission: fill?.commission ?? null,
    tradeTime: fill?.trade_time ?? fill?.trade_time_ms ?? fill?.time ?? null,
    raw: fill ?? null,
  }
}

function meaningfulValue(value) {
  if (value === null || value === undefined) return false
  if (typeof value === 'boolean') return false
  if (typeof value === 'string' && (value.trim() === '' || value.trim().toLowerCase() === 'none')) return false
  return true
}

function mapAccount(status) {
  const account = status?.account && typeof status.account === 'object' ? status.account : null
  const equity = status?.equity && typeof status.equity === 'object' ? status.equity : null
  const mapped = {
    equity: account?.equity_session ?? equity?.session ?? null,
    total_equity: account?.total_margin_balance ?? equity?.exchange ?? null,
    wallet_balance: account?.total_wallet_balance ?? null,
    available: account?.available_balance ?? equity?.available ?? null,
    available_balance: account?.available_balance ?? equity?.available ?? null,
    upnl: account?.total_unrealized_profit ?? equity?.upnl ?? null,
    unrealized_pnl: account?.total_unrealized_profit ?? equity?.upnl ?? null,
    day_pnl: account?.day_pnl ?? null,
    today_pnl: account?.today_pnl ?? null,
    margin_used: account?.total_initial_margin ?? null,
    equity_start: account?.starting_equity ?? account?.equity_start ?? null,
    source: account ? 'account' : (equity ? 'equity' : null),
  }
  const hasValue = Object.entries(mapped).some(([key, value]) => key !== 'source' && meaningfulValue(value))
  if (!hasValue) {
    return {
      empty: true,
      gap: 'E_ACCOUNT_EMPTY',
      empty_reason: status?.credentials_configured === false ? 'DEMO_CREDENTIALS_MISSING' : 'NO_ACCOUNT_VALUES',
      source: mapped.source,
    }
  }
  return { ...mapped, empty: false, gap: null, empty_reason: null }
}

async function finalizeCycleSafely(client, cycleId, { outcome, reasonCode = null, errorCode = null }, record) {
  const state = outcome === 'trade' || outcome === 'no_trade' ? 'DONE' : 'FAILED'
  const terminalOutcome = outcome === 'trade' || outcome === 'no_trade' ? outcome : 'error'
  const payload = {
    state,
    outcome: terminalOutcome,
    reason: reasonCode ?? terminalOutcome,
  }
  if (errorCode) payload.error = { code: errorCode }
  try {
    await client.finalizeCycle(cycleId, payload)
    return true
  } catch (error) {
    record?.gaps?.push(`Could not finalize service cycle ${cycleId}: ${error?.code ?? error?.message ?? error}.`)
    return false
  }
}

/** 16 §1: the setup job as the UI reads it (no promises, bounded log tail). */
function serializeSetupJob(job) {
  if (!job) return null
  return {
    id: job.id,
    phase: job.phase,
    created_at: job.created_at,
    updated_at: job.updated_at,
    steps: (job.steps ?? []).map((step) => ({
      id: step.id,
      label: step.label,
      status: step.status,
      started_at: step.started_at,
      finished_at: step.finished_at,
      exit_code: step.exit_code,
      log_tail: (step.log_tail ?? []).slice(-12),
    })),
    error: job.error ?? null,
  }
}

function roundViewOf(record) {
  const view = toRoundView(record)
  return {
    ...view,
    cycleSource: record?.cycleSource ?? null,
    cycle_source: record?.cycleSource ?? null,
    serviceStatus: record?.serviceStatus ?? null,
    service_status: record?.serviceStatus ?? null,
    errorStatus: record?.errorStatus ?? null,
    error_status: record?.errorStatus ?? null,
    httpStatus: record?.httpStatus ?? null,
  }
}

function roundSummaryOf(record) {
  const view = toRoundSummary(record)
  return {
    ...view,
    cycleSource: record?.cycleSource ?? null,
    cycle_source: record?.cycleSource ?? null,
    serviceStatus: record?.serviceStatus ?? null,
    service_status: record?.serviceStatus ?? null,
    errorStatus: record?.errorStatus ?? null,
    error_status: record?.errorStatus ?? null,
    httpStatus: record?.httpStatus ?? null,
  }
}

function hostErrorFromService(error) {
  if (!(error instanceof ServiceClientError)) return null
  if (error.code.startsWith('E_CONTROL_') || error.code === 'E_OPERATION_EXISTS') {
    return new HostIntegrationError(error.code, error.message, { status: error.status, retryable: false, details: error.details })
  }
  if (error.code === 'E_SERVICE_TOKEN_MISSING') {
    return new HostIntegrationError('E_SERVICE_TOKEN_MISSING', error.message, {
      status: 503,
      details: { ...error.details, guidance: 'Start the service so it creates data/.api-token, or set WORKBENCH_API_TOKEN.' },
    })
  }
  if (error.status === 401 || error.status === 403) {
    return new HostIntegrationError('E_SERVICE_UNAUTHORIZED', error.message, {
      status: 502,
      retryable: false,
      details: { ...error.details, service_status: error.status, guidance: 'Host token does not match the running service.' },
    })
  }
  if (error.code.startsWith('E_LEASE_')) {
    return new HostIntegrationError(error.code, error.message, {
      status: error.status,
      retryable: error.code !== 'E_LEASE_HELD',
      details: error.details,
    })
  }
  if (error.status === 409) {
    return new HostIntegrationError('E_SERVICE_CONFLICT', error.message, {
      status: 409,
      retryable: false,
      details: error.details,
    })
  }
  if (error.status === 422) {
    return new HostIntegrationError('E_SERVICE_REJECTED', error.message, {
      status: 422,
      retryable: false,
      details: error.details,
    })
  }
  if (error.status === 423) {
    return new HostIntegrationError('E_SERVICE_LOCKED', error.message, {
      status: 423,
      retryable: false,
      details: error.details,
    })
  }
  return new HostIntegrationError(
    error.code === 'E_SERVICE_TIMEOUT' ? 'E_SERVICE_TIMEOUT' : 'E_SERVICE_UNAVAILABLE',
    error.message,
    { status: 503, retryable: error.retryable, details: error.details },
  )
}

function executionAccountContext({ cycleResponse, snapshot }) {
  const wrapper = cycleResponse?.account && typeof cycleResponse.account === 'object'
    ? cycleResponse.account
    : null
  const wrapperHasParts = wrapper && (wrapper.account || wrapper.equity)
  const rawAccount = wrapperHasParts && wrapper.account && typeof wrapper.account === 'object'
    ? wrapper.account
    : wrapper && !wrapperHasParts && typeof wrapper === 'object' ? wrapper : snapshot?.status?.account ?? null
  const equity = wrapperHasParts && wrapper.equity && typeof wrapper.equity === 'object'
    ? wrapper.equity
    : snapshot?.status?.equity ?? null
  return {
    account: rawAccount && typeof rawAccount === 'object' ? rawAccount : null,
    equity: equity && typeof equity === 'object' ? equity : null,
  }
}

function hasNumericValue(value) {
  if (typeof value === 'number') return Number.isFinite(value)
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    return Number.isFinite(parsed)
  }
  return false
}

/** Count non-flat positions from a service snapshot (envelope + status). */
function countOpenPositions(snapshot) {
  const rows = snapshot?.status?.positions
  if (Array.isArray(rows)) {
    const live = rows.filter((row) => {
      if (!row || typeof row !== 'object') return false
      const qty = row.qty ?? row.positionAmt ?? row.quantity
      if (qty === undefined || qty === null) return true
      const parsed = Number(qty)
      if (!Number.isFinite(parsed)) return true
      return Math.abs(parsed) > 0
    })
    if (live.length > 0) return live.length
  }
  const count = snapshot?.envelope?.positions?.count
  if (Number.isFinite(count) && count > 0) return count
  return 0
}

export class WorkbenchHostIntegration {
  constructor({
    ctx = null,
    serviceBaseUrl = DEFAULT_SERVICE_URL,
    // null = resolve from WORKBENCH_DATA_DIR / existing package data / user profile.
    dataDir = null,
    tokenFile = null,
    storePath = null,
    env = process.env,
    fetchImpl = globalThis.fetch,
    llm = null,
    route = null,
    config = {},
    now = Date.now,
    logger = null,
    runTimeoutMs = DEFAULT_RUN_TIMEOUT_MS,
    reviewTimeoutMs = DEFAULT_REVIEW_TIMEOUT_MS,
    // 05 P1-1: the model catalog + config seam is injectable (tests pass a fake
    // catalog; production derives it from ctx.llm).
    llmCatalog = null,
    llmConfigFile = LLM_CONFIG_FILE,
    // 15 §10 test seams: where the launcher lives, how to spawn it, and how long
    // to wait for /health. Production passes nothing (derived from the plugin
    // location); tests point them at fixtures so no real process is ever started.
    serviceLauncher = null,
    spawnImpl = null,
    serviceStartTimeoutMs = SERVICE_START_TIMEOUT_MS,
  } = {}) {
    this.ctx = ctx
    this.serviceBaseUrl = String(serviceBaseUrl).replace(/\/+$/, '')
    const resolvedDataDir = dataDir === null || dataDir === undefined
      ? resolveDefaultDataDir(env)
      : dataDir
    this.dataDir = resolvedDataDir
    this.tokenFile = tokenFile
    this.env = env
    this.fetchImpl = fetchImpl
    this.llmMode = String(env?.WORKBENCH_LLM_MODE ?? '').trim().toLowerCase() === 'session_fixture'
      ? 'session_fixture'
      : 'harness'
    const requestedScript = String(env?.WORKBENCH_LLM_SCRIPT ?? 'no_trade').trim().toLowerCase()
    this.llmScript = SESSION_LLM_SCRIPTS.includes(requestedScript) ? requestedScript : 'no_trade'
    this.sessionLlm = this.llmMode === 'session_fixture'
      ? createSessionLlm({ script: this.llmScript })
      : null
    this.llm = llm ?? this.sessionLlm?.llm ?? ctx?.llm ?? null
    this.route = route ?? (this.llmMode === 'session_fixture'
      ? { provider: 'session', model: 'in-conversation', source: SESSION_LLM_TRANSPORT }
      : (ctx ? resolveHarnessRoute(ctx) : null))
    this.config = config
    this.now = typeof now === 'function' ? now : Date.now
    this.logger = logger ?? ctx?.logger ?? null
    this.runTimeoutMs = runTimeoutMs
    this.reviewTimeoutMs = reviewTimeoutMs
    this.reviewInFlight = new Set()
    // 15 §3: the archive is a directory of `YYYY-MM-DD.jsonl` partitions. A
    // caller that passes a `.json` path is pointing at the OLD single-file cache:
    // that file becomes the migration source and the partitions live next to it.
    const requestedStore = storePath === null ? path.join(resolvedDataDir, 'host-rounds.json') : String(storePath)
    this.storePath = requestedStore
    this.roundsDir = requestedStore.endsWith('.json')
      ? path.join(path.dirname(requestedStore), 'rounds')
      : requestedStore
    const retentionDays = Number(env?.WORKBENCH_ROUNDS_RETENTION_DAYS ?? 0)
    this.store = new RoundArchive({
      dir: this.roundsDir,
      legacyPath: requestedStore.endsWith('.json') ? requestedStore : null,
      retentionDays: Number.isFinite(retentionDays) ? retentionDays : 0,
      now: () => this.now(),
      logger: this.logger,
    })
    // A05: the 24h auto budget is a sidecar (auto-budget.json), never derived
    // from the UI round partition. It survives archive retention either way.
    this.autoBudgetPath = path.join(resolvedDataDir, 'auto-budget.json')
    this.autoBudget = this.loadAutoBudget()
    this.inFlight = null
    // 15 §10: one service start at a time; the last outcome is a read-only fact
    // for /health (never a claim that a spawn succeeded).
    this.serviceStartInFlight = null
    this.serviceStartLast = null
    this.serviceLauncherOverride = serviceLauncher && typeof serviceLauncher === 'object' ? serviceLauncher : null
    this.spawnImpl = typeof spawnImpl === 'function' ? spawnImpl : spawn
    this.serviceStartTimeoutMs = Number.isFinite(serviceStartTimeoutMs) && serviceStartTimeoutMs > 0
      ? Math.trunc(serviceStartTimeoutMs)
      : SERVICE_START_TIMEOUT_MS
    // 16 §1: first-run bootstrap. It reuses the SAME spawn seam and the SAME
    // /health probe as the button, so tests never launch a real process.
    this.serviceSetup = createServiceSetup({
      dataDir: resolvedDataDir,
      env: this.env,
      logger: this.logger,
      spawnImpl: this.spawnImpl,
      launcherPaths: this.serviceLauncherPaths(),
      probeServiceHealth: (options) => this.probeServiceHealth(options),
      startService: () => this.startService(),
      now: () => this.now(),
    })
    // Rounds whose ownership was revoked by the watchdog/stale repair. The
    // late promise may still resolve, but it must never reach P1/P2 or overwrite
    // the terminal failed record.
    this.abandonedRuns = new Set()
    // Cross-process execution leases held by this Host instance, keyed by round.
    this.runLeases = new Map()
    // 01 §5.3: structured running progress (in-memory only; /state projects it).
    this.inFlightStatus = null
    this.staleRepaired = false
    this.lastRunStartedAt = null
    this.scheduleStatus = null
    this.scheduleProvider = null
    this.lastRunAt = null
    this.lastService = null
    // F1: the last merged /state service block (what the UI is currently told)
    // and the control row the last successful portfolio fetch saw. Both are
    // read-only projections of service facts; nothing here is a decision.
    this.lastServiceBlock = null
    this.lastControl = null
    // The control facade published by scheduler.js (see its _attachControl).
    this.schedulerControl = null
    // Last explicit stop/start attempt (in-memory only). The *state* of
    // automation is always re-derived from the service control row, which is
    // persisted by the service — so a Host restart cannot invent a stop.
    this.lastStop = null
    this.lastStart = null
    this.stopInFlight = null
    this.lastCredentialsConfigured = null
    this.lastError = null
    this.portfolioCache = null
    this.portfolioInFlight = null
    this.portfolioCacheKey = null
    this.portfolioTtlMs = PORTFOLIO_CACHE_MS
    // 02 §3.1: plugin-local config only (P-03=A). Revision is loaded once and
    // exposed through /health.loadedConfigRevision (01's single field table).
    this.llmConfigFile = llmConfigFile
    this.catalog = llmCatalog ?? createLlmCatalog({ llm: this.llm, now: () => this.now() })
    const loaded = readConfig(this.dataDir)
    this.llmConfig = loaded.config
    this.configLoadError = loaded.error
    this.configBanner = loaded.error ? '配置不可读，未固化（可用配置抽屉重新保存）' : null
    this.configBannerCode = loaded.error?.code ?? null
    this.configFixationChecked = Boolean(loaded.config)
    if (loaded.config?.revision) setLoadedConfigRevision(loaded.config.revision)
  }

  /**
   * Persist one round's AI audit into the service ledger (M1.3c).
   * Best-effort: a service without the audit endpoints (404) is silently
   * ignored so older profiles keep working; other failures become a gap and
   * never change the round outcome.
   */
  async persistAiAudit(client, cycleId, result, record, events = []) {
    if (!cycleId || !result || !Array.isArray(result.calls) || result.calls.length === 0) return
    const evidenceSnapshotId = record?.freshness?.snapshot_id
      ?? result?.evidence?.snapshot_id
      ?? null
    try {
      const calls = result.calls
      const tokenInput = calls.reduce((sum, call) => sum + (Number(call.input_tokens) || 0), 0)
      const tokenOutput = calls.reduce((sum, call) => sum + (Number(call.output_tokens) || 0), 0)
      // 02 §3.3: the frozen route snapshot rides along in ai_runs.config_json;
      // ai_runs.provider/model stay the *default* route (per-call values live in
      // ai_calls, which is what heterogeneous debate needs).
      const routeSnapshot = record?.routeSnapshot ?? null
      // T15 (§8·D / C8): the four time points come from ai_calls.*_at_ms, but the
      // run row needs its own start to be able to say "how long did the chain
      // take". The service rejects a non-positive value, so only a real epoch-ms
      // is forwarded (never 0, never NaN).
      const runStartedMs = epochMsOf(record?.startedAt ?? result?.startedAt)
      const run = await client.createAiRun(cycleId, {
        mode: result.mode ?? 'FULL',
        trigger: record?.trigger ?? 'manual',
        ...(runStartedMs === null ? {} : { started_at_ms: runStartedMs }),
        provider: routeSnapshot?.default?.provider ?? this.route?.provider ?? null,
        model: routeSnapshot?.default?.model ?? this.route?.model ?? null,
        prompt_set: 'ai-chain/1',
        token_input: tokenInput,
        token_output: tokenOutput,
        cost_estimate: null,
        config: {
          debateMode: result.debateMode ?? null,
          llmMode: this.llmMode,
          routeSnapshot,
          fallbackApplied: null,
        },
      })
      const aiRunId = run?.ai_run?.ai_run_id ?? null
      if (!aiRunId) return
      for (const call of result.calls) {
        await client.putAiCall(aiRunId, call.call_id, {
          role: call.role,
          attempt: call.attempt,
          phase: call.round,
          provider: call.provider,
          model: call.model,
          // D3 (05 §4) + 02 §3.3: the effort that actually ran. With a frozen
          // config the chain records the freeze-time effectiveEffort (never a
          // re-derivation from the stream); without one it records ROLE_EFFORT.
          reasoning_effort: call.effort ?? null,
          temperature: typeof call.temperature === 'number' ? call.temperature : null,
          prompt_version: call.prompt_version,
          output_schema_version: call.output_schema_version,
          max_tokens: call.max_tokens ?? null,
          status: call.status ?? (call.schema_ok ? 'ok' : 'invalid'),
          finish_reason: call.finish_reason ?? call.finish_kind ?? null,
          latency_ms: call.latency_ms,
          // D3: per-attempt timestamps (not the round's). Old records keep the
          // round start so the column is never null for a real call.
          started_at: Number.isFinite(call.started_at_ms) ? nowIso(call.started_at_ms) : (record?.startedAt ?? null),
          ended_at: Number.isFinite(call.ended_at_ms) ? nowIso(call.ended_at_ms) : nowIso(this.now()),
          input_tokens: call.input_tokens,
          output_tokens: call.output_tokens,
          total_tokens: call.total_tokens,
          error_code: call.error_code ?? null,
          error_message: call.error_message ?? null,
          request_sha256: call.request_sha256,
          response_sha256: call.response_sha256,
          evidence_snapshot_id: evidenceSnapshotId,
          validation_errors: call.validation_errors ?? [],
          // D3: slice meta only (budget/estimates/snapshot), never slice text.
          input_slice: call.input_slice ?? null,
          // T15 (§8·D): the four time points and the effective timeout are what
          // "why was this round slow / why did it time out" is answered from.
          // timeout_ms must be a real positive budget: the service stores 0 as
          // NULL + reason 'reported_zero_is_not_a_timeout', so 0 is never sent.
          ...(Number.isFinite(call.timeout_ms) && call.timeout_ms > 0 ? { timeout_ms: Math.round(call.timeout_ms) } : {}),
          ...(Number.isFinite(call.started_at_ms) ? { started_at_ms: Math.round(call.started_at_ms) } : {}),
          ...(Number.isFinite(call.ended_at_ms) ? { ended_at_ms: Math.round(call.ended_at_ms) } : {}),
          // Only a *failed* call's text is worth storing, and it is stored whole
          // (no client-side truncation). The chain keeps only response_sha256
          // today, so this is forwarded only when the record actually carries the
          // text — the Host never reconstructs or truncates it.
          ...(failedCallText(call) === null ? {} : { response_text: failedCallText(call) }),
        })
      }
      // 01 §5.4 / RT-M6: post-hoc flush. Events exist for review only; they are
      // written *after* createAiRun (never before a run, never as realtime
      // progress), so the running UI never depends on this write path.
      if (Array.isArray(events) && events.length > 0) {
        let flushed = 0
        for (const event of events) {
          try {
            await client.postAiEvent(aiRunId, event)
            flushed += 1
          } catch (error) {
            record?.gaps?.push(
              `ai_run_events flush stopped at ${flushed + 1}/${events.length} (${error?.code ?? error?.message ?? error}); review memory stays local.`,
            )
            break
          }
        }
        if (record) record.aiEventsFlushed = flushed
      }
      const artifacts = [
        ['analysis', 'analysis', 1],
        ['redOpening', 'red', 1],
        ['redResponse', 'red', 2],
        ['blueOpening', 'blue', 1],
        ['blueResponse', 'blue', 2],
        ['decision', 'decision', 1],
        ['risk', 'risk', 1],
        ['execution', 'execution', 1],
      ]
      // T15: the gate verdict rides with the execution artifact, because that is
      // the artifact that has to answer "why was this plan not submitted". It is
      // a whitelisted projection (see gateAuditOf), not the raw gate object.
      const gateAudit = gateAuditOf(result)
      for (const [key, agent, attempt] of artifacts) {
        const output = result.outputs?.[key]
        if (!output || typeof output !== 'object' || Array.isArray(output)) continue
        const forwarded = key === 'execution' && gateAudit
          ? {
            ...output,
            gate: gateAudit.gate,
            plan_gaps: gateAudit.plan_gaps,
            failure_category: gateAudit.failure_category,
          }
          : output
        await client.putArtifact(cycleId, agent, {
          ai_run_id: aiRunId,
          call_id: artifactCallId(result, agent, attempt),
          attempt,
          output: forwarded,
          output_id: `out-${cycleId}-${aiRunId}-${agent}-${attempt}`,
          schema_version: forwarded.schema_version ?? null,
          evidence_snapshot_id: evidenceSnapshotId,
        })
      }
    } catch (error) {
      const status = error?.status ?? null
      const code = error?.code ?? error?.name ?? 'E_AUDIT_FAILED'
      if (status === 404 || code === 'E_NOT_FOUND' || code === 'E_SERVICE_HTTP_404') return
      record?.gaps?.push(`AI audit persistence failed (${code}); audit remains in local RoundView JSON.`)
    }
  }

  tokenInfo() {
    return loadApiToken({ dataDir: this.dataDir, tokenFile: this.tokenFile, env: this.env })
  }

  clientWithToken(token) {
    return new WorkbenchServiceClient({
      baseUrl: this.serviceBaseUrl,
      token,
      fetchImpl: this.fetchImpl,
    })
  }

  /**
   * D-6=B: fixate the "balanced" preset on first launch. Never writes when the
   * catalog is unavailable or a preset model is missing — the banner explains
   * why and the UI keeps the unfixed window (loadedConfigRevision=null).
   */
  async ensureConfigReady() {
    if (this.llmConfig || this.configLoadError || this.configFixationChecked) return this.llmConfig
    this.configFixationChecked = true
    const result = await ensureFirstRunConfig({ dataDir: this.dataDir, catalog: this.catalog, now: () => this.now() })
    if (result.ok) {
      this.llmConfig = result.config
      this.configBanner = null
      this.configBannerCode = null
      if (result.config?.revision) setLoadedConfigRevision(result.config.revision)
      return this.llmConfig
    }
    // A failed fixation is retried on the next call (the catalog cache bounds it).
    this.configFixationChecked = false
    this.configBanner = result.banner ?? '配置未固化'
    this.configBannerCode = result.code ?? null
    return null
  }

  /** Project the in-memory running state (01 §5.3) with computed elapsed times. */
  serializeInFlight() {
    const status = this.inFlightStatus
    if (!status) return null
    const now = this.now()
    return {
      roundId: status.roundId,
      round_id: status.roundId,
      cycleId: status.cycleId,
      trigger: status.trigger,
      symbol: status.symbol,
      started_at: status.started_at,
      started_at_ms: status.startedAtMs,
      elapsed_ms: Math.max(0, now - status.startedAtMs),
      deadline_ms: CHAIN_DEADLINE_MS,
      phase: status.phase ?? null,
      stage_key: status.stage_key ?? null,
      stage_label: status.stage_label ?? null,
      stage_started_at: status.stage_started_at ?? null,
      stage_started_at_ms: status.stage_started_at_ms ?? null,
      stage_elapsed_ms: Number.isFinite(status.stage_started_at_ms)
        ? Math.max(0, now - status.stage_started_at_ms)
        : null,
      attempt: status.attempt ?? null,
      max_attempts: MAX_ATTEMPTS_PER_CALL,
      calls_done: status.calls_done ?? 0,
      calls_budget: MAX_CALLS,
      degraded: status.degraded === true,
      roles_done: [...(status.roles_done ?? [])],
      last_event: status.last_event ?? null,
    }
  }

  /** One stage boundary outside the chain (P1 / P2 gates). */
  setInFlightStage(stageKey, label) {
    if (!this.inFlightStatus) return
    const now = this.now()
    this.inFlightStatus.stage_key = stageKey
    this.inFlightStatus.stage_label = label
    this.inFlightStatus.phase = stageKey === 'p1' ? 'P1' : stageKey === 'p2' ? 'P2' : null
    this.inFlightStatus.stage_started_at_ms = now
    this.inFlightStatus.stage_started_at = nowIso(now)
    this.inFlightStatus.attempt = 1
  }

  /** 02 §3.4: the route the *last round* actually ran (snapshot), not a guess. */
  lastRouteSnapshot() {
    // 15 §3.3: "last round" spans day partitions — after midnight the previous
    // round is yesterday's file, and the route snapshot must still be it.
    const rounds = this.store.listRecent(1)
    const snapshot = rounds[0]?.routeSnapshot
    if (!snapshot?.default) {
      return { configRevision: snapshot?.configRevision ?? null, default: null, resolvedAt: snapshot?.resolvedAt ?? null }
    }
    return {
      configRevision: snapshot.configRevision ?? null,
      resolvedAt: snapshot.resolvedAt ?? null,
      default: snapshot.default,
    }
  }

  configSummary() {
    return {
      revision: this.llmConfig?.revision ?? null,
      schema: this.llmConfig?.schema ?? null,
      preset: this.llmConfig?.preset ?? null,
      source: this.llmConfig?.defaultRoute?.source ?? null,
      updatedAt: this.llmConfig?.updatedAt ?? null,
      default: this.llmConfig?.defaultRoute ?? null,
      roleRoutes: this.llmConfig?.roleRoutes ?? null,
      debateAsymmetric: this.llmConfig?.debateAsymmetric === true,
      fallback: this.llmConfig?.fallback ?? { mode: 'fail' },
      banner: this.configBanner ?? null,
      bannerCode: this.configBannerCode ?? null,
      budget: budgetPreview(),
      catalog: { available: this.catalog.usable !== false },
    }
  }

  /** Presets materialised against the live catalog (single source of truth). */
  async presetSummaries() {
    const out = {}
    for (const name of PRESET_NAMES) {
      const materialized = await materializePreset(name, { catalog: this.catalog })
      out[name] = materialized.ok
        ? { label: PRESETS[name].label, config: materialized.config, missing: [] }
        : { label: PRESETS[name].label, config: null, missing: materialized.missing ?? [] }
    }
    return out
  }

  /** 02 §3.2: freeze the round against the live catalog before any side effect. */
  async freezeRoundRoutes() {
    const config = await this.ensureConfigReady()
    if (!config) return { ok: true, legacy: true, snapshot: null, roleRoutes: null, effectiveEfforts: null }
    return freezeRound({ config, catalog: this.catalog, now: () => this.now() })
  }

  /** 02 §2.6: the daily review uses the config's review route, with an explicit effort. */
  async resolveReviewRoute() {
    const config = await this.ensureConfigReady()
    if (!config) {
      return {
        provider: this.route?.provider ?? null,
        model: this.route?.model ?? null,
        effort: ROLE_EFFORT.review,
        source: 'roleTable',
      }
    }
    const intended = reviewRouteFromConfig(config)
    const snapshot = await this.catalog.snapshot()
    if (!snapshot.available) {
      throw new HostIntegrationError('ROUTE_UNAVAILABLE', '模型目录不可用，无法校验日复盘路由/档位', { status: 422 })
    }
    const info = await this.catalog.resolve(intended.provider, intended.model)
    const efforts = info?.efforts ?? []
    if (!info) {
      throw new HostIntegrationError('ROUTE_UNAVAILABLE', `日复盘模型 ${intended.provider}/${intended.model} 不在目录内`, { status: 422 })
    }
    if (efforts.length === 0) {
      return { provider: intended.provider, model: intended.model, effort: null, source: 'unsupported' }
    }
    const effective = intended.effort ?? info.defaultEffort ?? null
    if (effective === null || !efforts.includes(effective)) {
      throw new HostIntegrationError(
        'ROUTE_UNAVAILABLE',
        `日复盘档位 ${intended.effort ?? 'null'} 不受 ${intended.provider}/${intended.model} 支持（可用 ${efforts.join('/')}）；按 P-05=A 不映射`,
        { status: 422, details: { requestedEffort: intended.effort ?? null, availableEfforts: efforts } },
      )
    }
    return { provider: intended.provider, model: intended.model, effort: effective, source: 'roleRoutes' }
  }

  stateSync() {
    const token = this.tokenInfo()
    this.store.ensure()
    this.repairStaleRunning()
    // 15 §4: /state carries TODAY's partition (the UI default) plus the dates
    // that exist. `last_round` still means "the newest round anywhere", so the
    // cockpit keeps showing the last decision even after midnight.
    const today = archiveDay(this.now())
    const rounds = this.store.listDate(today)
    const newest = this.store.listRecent(1)[0] ?? null
    const last = newest ? roundViewOf(newest) : null
    return {
      ok: true,
      contract: HOST_ROUNDS_CONTRACT,
      rounds_date: today,
      available_dates: this.store.listDateKeys(),
      // 15 §10: whether this profile can start the service from the UI, and why
      // not when it cannot. The UI only renders the button when supported.
      service_start: this.serviceStartCapability(),
      service: {
        base_url: this.serviceBaseUrl,
        token_present: token.present,
        token_source: token.source,
        token_path: token.source === 'file' ? token.path : null,
        token_error: token.error,
        last_run: this.lastService ?? last?.service ?? null,
      },
      llm: {
        transport: this.llmMode === 'session_fixture' ? SESSION_LLM_TRANSPORT : 'ctx.llm.stream',
        mode: this.llmMode,
        script: this.llmMode === 'session_fixture' ? this.llmScript : null,
        route_selected: Boolean(this.route),
        provider: this.route?.provider ?? null,
        model: this.route?.model ?? null,
      },
      // 05 P1-1 / 02 §3.4: the three route facts live side by side so the UI can
      // show "frozen for this round" vs "Harness default" vs "pending revision".
      llm_config: { ...this.configSummary(), last_route: this.lastRouteSnapshot() },
      last_config_change: this.lastConfigChange ?? null,
      // D1: process-level load identity, same three field names as /health.
      build: currentBuild(),
      // 01 §5.3 / 05 P1-N01: a structured object while a round runs, null when
      // idle. `round_id` is kept as the 01 field name; `roundId` is the client
      // contract name (both point at the same round).
      in_flight: this.inFlightStatus ? this.serializeInFlight() : null,
      schedule: typeof this.scheduleProvider === 'function'
        ? this.scheduleProvider()
        : this.scheduleStatus,
      last_run_at: this.lastRunAt ?? last?.finishedAt ?? last?.startedAt ?? null,
      last_error: this.lastError,
      last_round: last,
      rounds: rounds.map(roundSummaryOf),
      gaps: this.staticServiceGaps(),
    }
  }

  /** Static environment notes for THIS profile (03 C6: no template lies). */
  staticServiceGaps() {
    const credentials = this.lastCredentialsConfigured
    return serviceGaps({ credentialsConfigured: credentials, llmMode: this.llmMode })
  }

  async fetchPortfolio(tokenInfo) {
    const checkedAt = this.now()
    if (!tokenInfo.present) {
      return {
        ...emptyPortfolio(
          'E_SERVICE_TOKEN_MISSING',
          'start workbench-service so it creates data/.api-token, or set WORKBENCH_API_TOKEN',
        ),
        fetchedAt: nowIso(checkedAt),
        service: {
          base_url: this.serviceBaseUrl,
          token_present: false,
          token_source: tokenInfo.source,
          token_path: tokenInfo.source === 'file' ? tokenInfo.path : null,
          token_error: tokenInfo.error,
          ready: false,
        },
      }
    }
    const client = this.clientWithToken(tokenInfo.token)
    let ready
    try {
      ready = await client.ready()
    } catch (error) {
      const code = error instanceof ServiceClientError ? error.code : 'E_SERVICE_UNAVAILABLE'
      return {
        ...emptyPortfolio(code, error?.message ?? String(error)),
        fetchedAt: nowIso(checkedAt),
        service: {
          base_url: this.serviceBaseUrl,
          token_present: true,
          token_source: tokenInfo.source,
          token_path: tokenInfo.source === 'file' ? tokenInfo.path : null,
          ready: false,
          error_code: code,
          error_status: error?.status ?? null,
        },
      }
    }
    // 关键区分：service_ready = 服务本身健康；ready = 允许开新仓。
    // 用户暂停自动交易后 ready 变 false（CONTROL_NOT_NORMAL），这只代表不能开新仓，
    // 不代表账户事实不可读。旧实现把两者混为一谈：一暂停，持仓/订单/成交/权益全消失，
    // 看起来就像「连不上服务」。
    const serviceHealthy = ready?.service_ready === true || ready?.ready === true
    if (!serviceHealthy) {
      return {
        ...emptyPortfolio('E_SERVICE_NOT_READY', 'workbench-service is not reachable'),
        fetchedAt: nowIso(checkedAt),
        service: {
          base_url: this.serviceBaseUrl,
          token_present: true,
          token_source: tokenInfo.source,
          token_path: tokenInfo.source === 'file' ? tokenInfo.path : null,
          ready: false,
          reasons: ready?.reasons ?? [],
        },
      }
    }

    const [statusR, positionsR, equityR, ordersR, fillsR, reviewsR, envelopeR, healthR] = await Promise.allSettled([
      client.status(),
      client.positions(),
      client.equityCurve('all'),
      client.orders(50),
      client.fills(100),
      client.reviews(20),
      client.riskEnvelope(),
      client.health(),
    ])
    const gaps = []
    const value = (result, label) => {
      if (result.status === 'fulfilled') return result.value
      const error = result.reason
      const code = error instanceof ServiceClientError ? error.code : 'E_SERVICE_UNAVAILABLE'
      const status = error instanceof ServiceClientError ? error.status : null
      gaps.push(`${label} unavailable (${code}${status ? ` HTTP ${status}` : ''})`)
      return null
    }
    const status = value(statusR, 'account status')
    const positionsPayload = value(positionsR, 'positions')
    const equityPayload = value(equityR, 'equity curve')
    const ordersPayload = value(ordersR, 'orders')
    const fillsPayload = value(fillsR, 'fills')
    const reviewsPayload = value(reviewsR, 'reviews')
    const envelope = value(envelopeR, 'risk envelope')
    const health = value(healthR, 'service health')

    let positions = []
    if (Array.isArray(positionsPayload?.positions)) {
      positions = positionsPayload.positions.map(mapPosition)
    } else if (Array.isArray(status?.positions)) {
      positions = status.positions.map(mapPosition)
      gaps.push('positions came from /api/status because /api/positions was unavailable')
    } else if (positionsPayload) {
      gaps.push('positions payload was empty or malformed')
    }

    const equityPoints = Array.isArray(equityPayload?.points)
      ? equityPayload.points.map(mapEquityPoint)
      : []
    const orders = Array.isArray(ordersPayload?.orders) ? ordersPayload.orders.map(mapOrder) : []
    const fills = Array.isArray(fillsPayload?.fills) ? fillsPayload.fills.map(mapFill) : []
    let reviews = []
    let reviewsSource = reviewsPayload ? 'service' : null
    if (Array.isArray(reviewsPayload?.reviews)) reviews = reviewsPayload.reviews
    else if (Array.isArray(reviewsPayload?.items)) reviews = reviewsPayload.items
    else if (reviewsPayload && typeof reviewsPayload === 'object') {
      gaps.push('reviews endpoint returned no reviews array')
    }
    if (reviews.length === 0) {
      const local = this.localReviews(20)
      if (local.length > 0) {
        reviews = local
        reviewsSource = 'local_files'
        gaps.push('reviews list came from local files because the service reviews endpoint was unavailable')
      }
    }

    const service = summarizeService({
      baseUrl: this.serviceBaseUrl,
      ready,
      envelope,
      status,
      tokenInfo,
      now: checkedAt,
      authMode: health?.auth?.mode ?? null,
    })
    const lastEquityPoint = equityPoints.length > 0 ? equityPoints[equityPoints.length - 1] : null
    const statusEquity = status?.equity && typeof status.equity === 'object' ? status.equity : null
    const equitySession = lastEquityPoint?.session ?? statusEquity?.session ?? null
    const equityExchange = lastEquityPoint?.exchange ?? statusEquity?.exchange ?? null
    const equityEmpty = equityPoints.length === 0
    const account = mapAccount(status)
    return {
      fetchedAt: nowIso(checkedAt),
      cached: false,
      cacheHit: false,
      source: 'service',
      service,
      equityCurve: {
        range: 'all',
        points: equityPoints,
        count: equityPoints.length,
        session: equitySession,
        exchange: equityExchange,
        latest: lastEquityPoint
          ? { ts: lastEquityPoint.ts, session: lastEquityPoint.session, exchange: lastEquityPoint.exchange }
          : null,
        empty: equityEmpty,
        empty_reason: equityEmpty
          ? (equityPayload ? 'NO_EQUITY_POINTS' : 'E_EQUITY_CURVE_UNAVAILABLE')
          : null,
        source: 'service',
        gap: equityPayload
          ? (equityEmpty ? 'E_EQUITY_CURVE_EMPTY' : null)
          : 'E_EQUITY_CURVE_UNAVAILABLE',
      },
      equitySession,
      equityExchange,
      positions,
      positionsEmpty: positions.length === 0,
      positionsEmptyReason: positions.length === 0 ? 'NO_OPEN_POSITIONS' : null,
      orders,
      ordersEmpty: orders.length === 0,
      ordersEmptyReason: orders.length === 0 ? 'NO_ORDERS' : null,
      fills,
      fillsEmpty: fills.length === 0,
      fillsEmptyReason: fills.length === 0 ? 'NO_FILLS' : null,
      reviews,
      reviewsSource,
      reviewsEmpty: reviews.length === 0,
      reviewsEmptyReason: reviews.length === 0
        ? (reviewsPayload ? 'NO_REVIEWS' : 'E_REVIEWS_ENDPOINT_MISSING')
        : null,
      reviewsGap: reviews.length > 0 ? null : (reviewsPayload ? null : 'E_REVIEWS_ENDPOINT_MISSING'),
      account,
      performance: status?.performance ?? null,
      // F2 diagnostics: pending/unknown order rows, startup recovery and the
      // protection verdict are Service facts the settings page shows as-is.
      outbox: projectOutbox(status?.outbox),
      exchange: projectExchange(status?.exchange),
      recovery: status?.recovery ?? null,
      startup: status?.startup ?? null,
      protection: status?.protection ?? null,
      // F1: the control row (state/reason/updated_at) is a service fact the
      // top bar must not have to invent. It rides with the portfolio because
      // that is where /api/risk/envelope is already fetched.
      control: controlInfoOf(envelope?.control),
      accountEmpty: account.empty === true,
      accountGap: account.gap ?? null,
      gaps: [...new Set(gaps)],
    }
  }

  async loadPortfolio({ force = false } = {}) {
    const tokenInfo = this.tokenInfo()
    const cacheKey = tokenInfo.token ?? 'none'
    const now = this.now()
    if (!force && this.portfolioCache
      && this.portfolioCache.key === cacheKey
      && now - this.portfolioCache.at < this.portfolioTtlMs) {
      return {
        ...this.portfolioCache.value,
        cached: true,
        cacheHit: true,
        cacheAgeMs: now - this.portfolioCache.at,
        refreshMode: 'cache',
      }
    }
    const hadInFlight = Boolean(this.portfolioInFlight)
    if (this.portfolioInFlight) {
      if (!force) return this.portfolioInFlight
      // A forced refresh must not return the in-flight value. Wait for it to
      // settle, then issue a new fetch so refresh=1 always bypasses the TTL.
      try {
        await this.portfolioInFlight
      } catch { /* the new fetch below owns the error surface */ }
    }
    const refreshMode = force ? (hadInFlight ? 'forced_after_inflight' : 'forced') : 'ttl'
    const promise = this.fetchPortfolio(tokenInfo)
      .then((value) => {
        this.portfolioCache = { at: this.now(), key: cacheKey, value }
        return { ...value, cached: false, cacheHit: false, cacheAgeMs: 0, refreshMode }
      })
      .finally(() => {
        this.portfolioInFlight = null
      })
    this.portfolioInFlight = promise
    return promise
  }

  // --------------------------------------------------------------- automation
  // F1 (§3.2/§8·F). One rule: the *state* of automation is derived from facts
  // the service owns (control row + schedule), never from "the user clicked
  // stop in this tab". A page refresh or a Host restart therefore cannot make a
  // stopped workbench look like a running one, and it cannot make a running one
  // look stopped either.

  /** The control facade published by scheduler.js, or null when there is none. */
  schedulerControlHandle() {
    const handle = this.schedulerControl
    return handle && typeof handle === 'object' ? handle : null
  }

  schedulerStatus() {
    const handle = this.schedulerControlHandle()
    if (handle && typeof handle.status === 'function') {
      try { return handle.status() } catch (error) {
        this.logger?.warn?.('[twb] scheduler status failed: ' + String(error?.message ?? error))
        return null
      }
    }
    return this.scheduleStatus && typeof this.scheduleStatus === 'object' ? this.scheduleStatus : null
  }

  /**
   * Newest finished round that was not a failure. Read from the persisted round
   * store so the "最近成功" fact survives a Host restart (the scheduler history
   * is memory only).
   */
  lastSuccessfulRoundAt() {
    try {
      // 15 §3.3: newest-first across partitions; a few hundred is far more than
      // the UI/automation needs and keeps the read bounded.
      for (const row of this.store.listRecent(500, { maxDays: 30 })) {
        const status = String(row?.status ?? '')
        if (status === 'failed' || status === 'running') continue
        const at = row?.finishedAt ?? row?.startedAt ?? null
        if (at) return at
      }
    } catch (error) {
      this.logger?.warn?.('[twb] cannot read rounds for last success: ' + String(error?.message ?? error))
    }
    return this.lastRunAt ?? null
  }

  /**
   * One projection of "is automatic trading on, and if not why".
   * states: running | stopped | stopping | pending | blocked | unknown
   *  - stopped  : the service control row says pause (operator stop, persisted)
   *  - pending  : the local scheduler is off but the service has not confirmed
   *               (reason SERVICE_UNREACHABLE or CONTROL_NORMAL)
   *  - blocked  : a concrete rule/execution reason stops new risk
   *  - unknown  : not enough facts yet — never reported as running
   */
  automationSnapshot(service = null, { now = this.now(), control = undefined, reachable = undefined } = {}) {
    const schedule = this.schedulerStatus()
    const svc = service && typeof service === 'object' ? service : null
    const serviceReachable = reachable === undefined ? schedulerReachable(svc) : reachable
    const controlRow = control !== undefined
      ? controlInfoOf(control)
      : (serviceReachable ? controlInfoOf(this.lastControl) : null)
    const controlState = controlRow?.state
      ?? (serviceReachable && typeof svc?.control_state === 'string' && svc.control_state !== 'unknown' ? svc.control_state : 'unknown')
    const schedulerEnabled = schedule ? schedule.enabled === true : null
    const stopRecord = this.lastStop ? publicStopRecord(this.lastStop) : null
    let state = 'unknown'
    let reasonCode = null
    if (this.stopInFlight) {
      state = 'stopping'
      reasonCode = 'STOP_REQUESTED'
    } else if (controlState === CONTROL_STOP_STATE) {
      state = 'stopped'
      reasonCode = 'SERVICE_CONTROL_PAUSE'
    } else if (CONTROL_RULE_STATES.has(controlState)) {
      state = 'blocked'
      reasonCode = 'SERVICE_CONTROL_' + controlState.toUpperCase()
    } else if (serviceReachable === false) {
      // The service is the only authority for "stopped". Locally off + service
      // unknown is "待确认", never "已停止".
      if (schedulerEnabled === false) {
        state = 'pending'
        reasonCode = 'SERVICE_UNREACHABLE'
      } else {
        state = 'blocked'
        reasonCode = String(svc?.error_code ?? 'SERVICE_UNREACHABLE')
      }
    } else if (serviceReachable === null) {
      state = 'unknown'
      reasonCode = 'SERVICE_NOT_CHECKED'
    } else if (schedulerEnabled === false) {
      state = 'pending'
      reasonCode = 'CONTROL_NORMAL'
    } else if (schedulerEnabled === null) {
      state = 'unknown'
      reasonCode = 'SCHEDULER_UNAVAILABLE'
    } else if (schedule?.paused_until) {
      state = 'blocked'
      reasonCode = 'SCHEDULE_PAUSED'
    } else if (schedule?.last_result?.kind === 'failed') {
      state = 'blocked'
      reasonCode = String(schedule.last_result.code ?? 'SCHEDULE_FAILED')
    } else if (leadingSkipCount(schedule) >= 2) {
      state = 'blocked'
      reasonCode = String(schedule?.last_result?.code ?? 'SCHEDULE_STALLED')
    } else {
      state = 'running'
    }
    return {
      contract: AUTOMATION_CONTRACT,
      state,
      reason_code: reasonCode,
      scheduler_enabled: schedulerEnabled,
      scheduler_supported: Boolean(this.schedulerControlHandle()),
      last_success_at: this.lastSuccessfulRoundAt(),
      next_at: schedule?.next_at ?? null,
      interval_ms: schedule?.interval_ms ?? null,
      last_result: schedule?.last_result ?? null,
      paused_until: schedule?.paused_until ?? null,
      pause_reason: schedule?.pause_reason ?? null,
      service_control: {
        state: controlState,
        reason: controlRow?.reason ?? null,
        updated_at: controlRow?.updated_at ?? null,
        updated_by: controlRow?.updated_by ?? null,
        confirmed: serviceReachable === true && controlState !== 'unknown',
      },
      stop: stopRecord,
      checked_at: nowIso(now),
    }
  }

  /**
   * "停止自动交易": close the local scheduler, then ask the service for the
   * existing non-flattening control state 'pause'.
   *
   * Deliberately NOT 'emergency': ExecutionService.control() calls
   * _flatten_all() for emergency, and a stop must never close positions or
   * cancel exchange-side protection orders (§3.2).
   */
  async stopAutomation({ reason = null, requestId = null, now = this.now() } = {}) {
    const resolvedRequestId = typeof requestId === 'string' && requestId
      ? requestId
      : 'req-stop-' + randomUUID().slice(0, 8)
    const text = String(reason ?? '').trim() || 'operator stopped automation from the workbench'
    const attempt = {
      request_id: resolvedRequestId,
      reason: text,
      requested_at: nowIso(now),
      phase: 'stopping',
      scheduler_stopped_at: null,
      confirmed_at: null,
      error: null,
      service_control: null,
    }
    this.lastStop = attempt
    this.logCommand('automation/stop', { requestId: resolvedRequestId, reason: text })

    // 1) Local first: a stop must not depend on the network.
    const scheduler = { supported: false, stopped: false, status: null, error: null }
    const handle = this.schedulerControlHandle()
    if (handle && typeof handle.setEnabled === 'function') {
      scheduler.supported = true
      try {
        handle.setEnabled(false)
        scheduler.stopped = true
        attempt.scheduler_stopped_at = nowIso(this.now())
      } catch (error) {
        scheduler.error = String(error?.code ?? error?.message ?? 'SCHEDULER_STOP_FAILED')
      }
      scheduler.status = this.schedulerStatus()
    }

    // 2) Then the service, which owns the persisted state and revokes leases.
    const flight = (async () => {
      try {
        const result = throwIfServiceErrorEnvelope(await this.callService((client) => client.control({
          state: CONTROL_STOP_STATE,
          confirm: CONTROL_STOP_STATE.toUpperCase(),
          reason: text,
          request_id: resolvedRequestId,
        })))
        attempt.service_control = controlInfoOf(result?.control)
        attempt.phase = 'stopped'
        attempt.confirmed_at = nowIso(this.now())
        if (attempt.service_control) this.lastControl = attempt.service_control
        this.logger?.info?.(
          '[twb] automation stopped request_id=' + resolvedRequestId
          + ' scheduler=' + (scheduler.stopped ? 'off' : 'unknown') + ' service_control=' + CONTROL_STOP_STATE,
        )
      } catch (error) {
        const code = String(error?.code ?? error?.name ?? 'E_SERVICE_UNAVAILABLE')
        attempt.phase = 'pending_confirmation'
        attempt.error = {
          code,
          message: String(error?.message ?? error),
          status: Number.isInteger(error?.status) ? error.status : null,
        }
        this.logger?.warn?.(
          '[twb] automation stop is local-only request_id=' + resolvedRequestId + ': ' + code,
        )
      }
      return attempt
    })()
    this.stopInFlight = flight
    try {
      await flight
    } finally {
      if (this.stopInFlight === flight) this.stopInFlight = null
    }

    const confirmed = attempt.phase === 'stopped'
    return {
      ok: true,
      request_id: resolvedRequestId,
      phase: attempt.phase,
      scheduler,
      service_control: attempt.service_control,
      error: attempt.error,
      // The immediate answer must not depend on a follow-up /state: it is built
      // from what just happened plus the last service block.
      automation: this.automationSnapshot(this.lastServiceBlock, confirmed
        ? { control: attempt.service_control, reachable: true }
        : { control: null, reachable: false }),
    }
  }

  /**
   * "启动自动交易" (§3.2). Verification first: identity/data/unresolved state,
   * then the control row, then the scheduler. A failure at any step leaves the
   * scheduler off — resuming is never a side effect of a page load.
   */
  async startAutomation({ reason = null, requestId = null, now = this.now() } = {}) {
    const resolvedRequestId = typeof requestId === 'string' && requestId
      ? requestId
      : 'req-start-' + randomUUID().slice(0, 8)
    const text = String(reason ?? '').trim() || 'operator started automation from the workbench'
    const attempt = {
      request_id: resolvedRequestId,
      reason: text,
      requested_at: nowIso(now),
      phase: 'starting',
      scheduler_started_at: null,
      error: null,
      service_control: null,
    }
    this.lastStart = attempt
    this.logCommand('automation/start', { requestId: resolvedRequestId, reason: text })

    const verdict = await this.preflight({ ignoreControl: true })
    if (verdict?.ok !== true) {
      attempt.phase = 'blocked'
      attempt.error = { code: String(verdict?.code ?? 'PREFLIGHT_REJECTED'), message: String(verdict?.detail ?? 'preflight rejected') }
      attempt.automation = this.automationSnapshot(this.lastServiceBlock)
      return { ok: false, request_id: resolvedRequestId, phase: attempt.phase, error: attempt.error, automation: attempt.automation }
    }

    try {
      const result = throwIfServiceErrorEnvelope(await this.callService((client) => client.control({
        state: 'normal',
        confirm: 'NORMAL',
        reason: text,
        request_id: resolvedRequestId,
      })))
      attempt.service_control = controlInfoOf(result?.control)
      if (attempt.service_control) this.lastControl = attempt.service_control
    } catch (error) {
      attempt.phase = 'rejected'
      attempt.error = {
        code: String(error?.code ?? 'E_CONTROL_REJECTED'),
        message: String(error?.message ?? error),
        status: Number.isInteger(error?.status) ? error.status : null,
      }
      this.logger?.warn?.('[twb] automation start rejected request_id=' + resolvedRequestId + ': ' + attempt.error.code)
      return {
        ok: false,
        request_id: resolvedRequestId,
        phase: attempt.phase,
        error: attempt.error,
        automation: this.automationSnapshot(this.lastServiceBlock, attempt.service_control
          ? { control: attempt.service_control, reachable: true }
          : undefined),
      }
    }

    const scheduler = { supported: false, started: false, status: null, error: null }
    const handle = this.schedulerControlHandle()
    if (handle && typeof handle.setEnabled === 'function') {
      scheduler.supported = true
      try {
        handle.setEnabled(true)
        scheduler.started = true
        attempt.scheduler_started_at = nowIso(this.now())
      } catch (error) {
        scheduler.error = String(error?.code ?? error?.message ?? 'SCHEDULER_START_FAILED')
      }
      scheduler.status = this.schedulerStatus()
    }
    attempt.phase = 'running'
    this.logger?.info?.('[twb] automation started request_id=' + resolvedRequestId)
    return {
      ok: true,
      request_id: resolvedRequestId,
      phase: attempt.phase,
      scheduler,
      service_control: attempt.service_control,
      error: null,
      automation: this.automationSnapshot(this.lastServiceBlock, {
        control: attempt.service_control ?? { state: 'normal' },
        reachable: true,
      }),
    }
  }

  // ---- 15 §10: one-click start of the local trading service pose -----------
  /**
   * Where the service launcher lives. Derived from the plugin's own location so
   * no absolute workspace path is baked into the code:
   *   <workspace>/multi-agent-trader/.venv/{Scripts|bin}/python
   *   <root>/service/run_service.py
   *   <workspace>/multi-agent-trader/.env   (demo key/secret source)
   * Every path is overridable through the documented env names.
   */
  serviceLauncherPaths() {
    const hostDir = path.dirname(fileURLToPath(import.meta.url))
    const root = path.dirname(path.dirname(hostDir))
    const workspace = path.dirname(root)
    const win = process.platform === 'win32'
    const env = this.env ?? {}
    const dataDir = path.resolve(String(this.dataDir))
    const venvPython = (venvDir) => path.join(venvDir, win ? 'Scripts' : 'bin', win ? 'python.exe' : 'python')
    // 解释器发现顺序：显式 → dataDir 下的 service-venv（升级不丢）→ 包内 .venv
    // → 作者机上的参考项目 venv（只作兜底，别人机器上不存在）→ PATH 里的 python。
    const python = env.WORKBENCH_SERVICE_PYTHON
      || [
        path.join(dataDir, 'service-venv'),
        path.join(root, '.venv'),
        path.join(workspace, 'multi-agent-trader', '.venv'),
      ].map(venvPython).find((candidate) => fileExists(candidate))
      || resolveSystemPython(env)
      || null
    // Demo 凭据文件同理：先看 settings 写下的 dataDir 凭据，再看参考项目的 .env。
    const demoEnv = this.demoEnvFilePath()
    const dotenv = env.WORKBENCH_DOTENV_FILE
      || [demoEnv, path.join(workspace, 'multi-agent-trader', '.env')].find((candidate) => fileExists(candidate))
      || demoEnv
    const derived = {
      root,
      workspace,
      python,
      entry: env.WORKBENCH_SERVICE_ENTRY || path.join(root, 'service', 'run_service.py'),
      dotenv,
    }
    const override = this.serviceLauncherOverride
    return override ? { ...derived, ...override } : derived
  }

  /**
   * Environment for the spawned service. The Host must not launder its own
   * Node/Electron env quirks into the Python service: Node's NO_PROXY may list
   * IPv6 hosts as "[::1]", which httpx parses as "host:port" and rejects with
   * InvalidURL (observed 2026-11-15: the service booted read-only because its
   * execution client could not be built). Keep the user's proxy, fix the syntax.
   */
  serviceSpawnEnv(dotenvPath) {
    const env = { ...(this.env ?? process.env), WORKBENCH_DOTENV_FILE: dotenvPath }
    for (const key of ['NO_PROXY', 'no_proxy']) {
      const value = env[key]
      if (typeof value === 'string' && value.includes('[')) {
        env[key] = value.replace(/\[([^\]]+)\]/g, '$1')
      }
    }
    return env
  }

  serviceLogPaths() {
    const dir = path.join(this.dataDir, 'logs')
    return { dir, stdout: path.join(dir, 'service.out.log'), stderr: path.join(dir, 'service.err.log') }
  }

  /**
   * Read-only capability fact for /state + /health. Recomputed (two stats) so a
   * venv that appears later does not need a plugin reload to light the button up.
   */
  serviceStartCapability() {
    const env = this.env ?? {}
    const switchText = String(env.WORKBENCH_ALLOW_SERVICE_START ?? '').trim().toLowerCase()
    const disabled = ['0', 'off', 'false', 'no', 'disabled'].includes(switchText)
    const paths = this.serviceLauncherPaths()
    // fileExists tolerates a null python (nothing found on this machine) instead
    // of letting a stat throw and turning "cannot start" into a 500.
    const pythonOk = fileExists(paths.python)
    const entryOk = fs.existsSync(paths.entry)
    const capability = {
      supported: !disabled && pythonOk && entryOk,
      reason: disabled ? 'DISABLED_BY_ENV' : !pythonOk ? 'PYTHON_MISSING' : !entryOk ? 'SERVICE_ENTRY_MISSING' : null,
      command: 'service/start',
    }
    return capability
  }

  /** The launcher detail belongs in /health diagnostics, not the 8s /state poll. */
  serviceStartDetails() {
    const paths = this.serviceLauncherPaths()
    const logs = this.serviceLogPaths()
    const url = new URL(this.serviceBaseUrl)
    return {
      ...this.serviceStartCapability(),
      python_path: paths.python,
      entry_path: paths.entry,
      dotenv_path: paths.dotenv,
      data_dir: path.resolve(String(this.dataDir)),
      host: url.hostname,
      port: url.port || (url.protocol === 'https:' ? '443' : '80'),
      log_path: logs.stdout,
      timeout_ms: this.serviceStartTimeoutMs,
      last: this.serviceStartLast,
    }
  }

  /** Cheap readiness probe against the service's own /health (no token needed). */
  async probeServiceHealth({ timeoutMs = 1500 } = {}) {
    const controller = typeof AbortController === 'function' ? new AbortController() : null
    const timer = controller ? setTimeout(() => controller.abort(), timeoutMs) : null
    try {
      const response = await this.fetchImpl(`${this.serviceBaseUrl}/health`, {
        method: 'GET',
        headers: { accept: 'application/json' },
        ...(controller ? { signal: controller.signal } : {}),
      })
      if (!response?.ok) return { ok: false, status: response?.status ?? null, error: 'HTTP_NOT_OK' }
      const body = await response.json().catch(() => null)
      return { ok: body?.ok === true, status: response.status, service_ready: body?.ready ?? null }
    } catch (error) {
      return { ok: false, error: String(error?.code ?? error?.name ?? error?.message ?? 'UNREACHABLE') }
    } finally {
      if (timer) clearTimeout(timer)
    }
  }

  /**
   * Start the local service. Rules:
   *  - already answering /health  -> ok, state 'running', nothing spawned;
   *  - launcher missing / disabled -> 503 with the exact paths it looked for;
   *  - second click while one start is in flight -> 409;
   *  - success is only reported after /health says ok; a timeout is a failure
   *    with the pid, the wait and the log path (never a probable success).
   */
  async startService() {
    const capability = this.serviceStartCapability()
    if (!capability.supported) {
      throw new HostIntegrationError('E_SERVICE_LAUNCHER_MISSING', 'the Host cannot start the trading service in this profile', {
        status: 503,
        retryable: false,
        details: this.serviceStartDetails(),
      })
    }
    if (this.serviceStartInFlight) {
      throw new HostIntegrationError('E_SERVICE_START_IN_FLIGHT', 'a service start is already in progress', { status: 409, retryable: true })
    }
    const already = await this.probeServiceHealth()
    if (already.ok) {
      this.serviceStartLast = { at: nowIso(this.now()), ok: true, already: true, waited_ms: 0, pid: null }
      return { ok: true, state: 'running', already: true, ready: true, waited_ms: 0, pid: null }
    }
    this.serviceStartInFlight = this.launchService()
    try {
      const result = await this.serviceStartInFlight
      return result
    } finally {
      this.serviceStartInFlight = null
    }
  }

  async launchService() {
    const startedAt = this.now()
    const paths = this.serviceLauncherPaths()
    const logs = this.serviceLogPaths()
    const url = new URL(this.serviceBaseUrl)
    const port = url.port || (url.protocol === 'https:' ? '443' : '80')
    fs.mkdirSync(logs.dir, { recursive: true })
    const outFd = fs.openSync(logs.stdout, 'a')
    const errFd = fs.openSync(logs.stderr, 'a')
    let child = null
    try {
      child = this.spawnImpl(paths.python, [
        paths.entry,
        '--host', url.hostname || '127.0.0.1',
        '--port', String(port),
        '--data-dir', path.resolve(String(this.dataDir)),
      ], {
        cwd: path.dirname(paths.entry),
        env: this.serviceSpawnEnv(paths.dotenv),
        detached: true,
        windowsHide: true,
        stdio: ['ignore', outFd, errFd],
      })
      child.unref()
    } catch (error) {
      this.serviceStartLast = { at: nowIso(this.now()), ok: false, code: 'E_SERVICE_SPAWN_FAILED', message: String(error?.message ?? error) }
      throw new HostIntegrationError('E_SERVICE_SPAWN_FAILED', `cannot launch the service: ${error?.message ?? error}`, {
        status: 500,
        details: { python_path: paths.python, entry_path: paths.entry, log_path: logs.stdout },
      })
    } finally {
      // The child owns the descriptors now; the Host must not keep them open.
      try { fs.closeSync(outFd) } catch { /* already closed */ }
      try { fs.closeSync(errFd) } catch { /* already closed */ }
    }
    const pid = child?.pid ?? null
    this.logger?.info?.(`[twb] service start requested pid=${pid} port=${port}`)
    const deadline = Date.now() + this.serviceStartTimeoutMs
    let lastProbe = null
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, SERVICE_START_POLL_MS))
      lastProbe = await this.probeServiceHealth({ timeoutMs: 1200 })
      if (lastProbe.ok) {
        const waited = Date.now() - startedAt
        this.serviceStartLast = { at: nowIso(this.now()), ok: true, pid, waited_ms: waited }
        this.logCommand('service/start', { pid, waited_ms: waited, ok: true })
        this.logger?.info?.(`[twb] service ready pid=${pid} waited=${waited}ms`)
        return { ok: true, state: 'started', pid, ready: true, waited_ms: waited }
      }
    }
    const waited = Date.now() - startedAt
    this.serviceStartLast = { at: nowIso(this.now()), ok: false, code: 'E_SERVICE_START_TIMEOUT', pid, waited_ms: waited }
    this.logger?.warn?.(`[twb] service start timed out pid=${pid} waited=${waited}ms`)
    throw new HostIntegrationError('E_SERVICE_START_TIMEOUT', `service did not answer /health within ${Math.round(waited / 1000)}s`, {
      status: 504,
      retryable: true,
      details: { pid, waited_ms: waited, last_probe: lastProbe, log_path: logs.stdout, stderr_path: logs.stderr },
    })
  }

  /** Read-only host identity for scripts/verify-runtime.mjs (and the UI). */
  hostInfo() {
    const build = currentBuild()
    const token = this.tokenInfo()
    return {
      ok: true,
      contract: HOST_ROUNDS_CONTRACT,
      plugin: '@local/dsh-trading-workbench',
      data_dir: path.resolve(String(this.dataDir)),
      // 15 §3: `rounds_path` is the day-partition directory; the legacy
      // single-file cache (if it was migrated) stays visible for audit.
      rounds_path: path.resolve(String(this.roundsDir)),
      legacy_rounds_path: this.storePath.endsWith('.json') ? path.resolve(String(this.storePath)) : null,
      archive: this.store.stats(),
      // 15 §10: the launcher detail (absolute paths, timeout, last attempt) lives
      // here so the 8s /state poll stays small.
      service_start: this.serviceStartDetails(),
      service_base_url: this.serviceBaseUrl,
      token_present: token.present,
      token_source: token.source,
      llm_mode: this.llmMode,
      environment: environmentOf(this.lastServiceBlock),
      build,
      release: {
        release_id: build.release_id,
        source: build.release_source,
        source_digest: build.release_source_digest,
        generated_at: build.release_generated_at,
        plugin_version: build.plugin_version,
      },
      loaded_at: build.loaded_at,
      automation: this.automationSnapshot(this.lastServiceBlock),
      schedule: this.schedulerStatus(),
      checked_at: nowIso(this.now()),
    }
  }

  async handleAutomationStop(req, res) {
    if (req.method !== 'POST') return methodNotAllowed(res, ['POST'])
    if (!isSameOriginRequest(req, this.ctx)) {
      return sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin POST only.' } })
    }
    requireJsonRequest(req)
    const body = await readJsonBody(req, MAX_COMMAND_BODY_BYTES)
    // A stop is a safety action: it must not be blocked on typing a reason. An
    // explicit reason is recorded when supplied; otherwise a fixed one is.
    const reason = typeof body?.reason === 'string' && body.reason.trim().length >= 2 ? body.reason.trim() : null
    const requestId = commandRequestId(body)
    return sendJson(res, 200, await this.stopAutomation({ reason, requestId }))
  }

  /**
   * 实盘接入入口（§4.5 的落地形态）。
   *
   * 刻意这样设计：
   *  - 只写本机文件 service/config/live.env（0600），密钥**不回传、不进日志、不落前端存储**；
   *  - 响应里只有指纹与路径，前端拿不到明文；
   *  - 不在这里重启服务：切环境是运维动作（见 scripts/switch-venue.mjs 与预检）。
   */
  /**
   * Demo 凭据文件（settings → 连接与账户 → Demo 接入 的唯一写目标）。
   *
   * 放在 dataDir 下而不是包目录：包安装后位于 node_modules，升级会替换整个目录；
   * 凭据和轮次归档一样必须活在升级之外。服务进程通过 WORKBENCH_DOTENV_FILE
   * 读它（见 serviceLauncherPaths / serviceSpawnEnv）。
   */
  demoEnvFilePath() {
    return path.resolve(String(this.dataDir), 'credentials', 'demo.env')
  }

  readDemoSetup() {
    const file = this.demoEnvFilePath()
    let exists = false
    let fingerprint = null
    let keys = []
    try {
      if (fs.existsSync(file)) {
        exists = true
        const text = fs.readFileSync(file, 'utf8')
        keys = text
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter((line) => line && !line.startsWith('#'))
          .map((line) => line.split('=')[0].trim())
        const keyLine = text.split(/\r?\n/).find((line) => /^(WORKBENCH_DEMO_API_KEY|BINANCE_DEMO_API_KEY)\s*=/.test(line))
        if (keyLine) {
          const value = keyLine.split('=').slice(1).join('=').trim()
          if (value) fingerprint = credentialFingerprint(value)
        }
      }
    } catch (error) {
      return { path: file, exists, keys, fingerprint, error: error.message }
    }
    return { path: file, exists, keys, fingerprint, error: null }
  }

  liveEnvFilePath() {
    // 与 demo.env 同目录：凭据跟着 dataDir 走，包目录升级不丢。
    // 服务侧优先读这里，包内 config/live.env 只作旧安装兜底（credentials.py）。
    return path.resolve(String(this.dataDir), 'credentials', 'live.env')
  }

  readLiveSetup() {
    const file = this.liveEnvFilePath()
    let exists = false
    let fingerprint = null
    let keys = []
    try {
      if (fs.existsSync(file)) {
        exists = true
        const text = fs.readFileSync(file, 'utf8')
        keys = text
          .split(/\r?\n/)
          .map((line) => line.trim())
          .filter((line) => line && !line.startsWith('#'))
          .map((line) => line.split('=')[0].trim())
        const keyLine = text.split(/\r?\n/).find((line) => /^BINANCE_LIVE_API_KEY\s*=/.test(line))
        if (keyLine) {
          const value = keyLine.split('=').slice(1).join('=').trim()
          if (value) fingerprint = credentialFingerprint(value)
        }
      }
    } catch (error) {
      return { path: file, exists, keys, fingerprint, error: error.message, service: null }
    }
    return { path: file, exists, keys, fingerprint, error: null }
  }

  async handleLiveSetup(req, res) {
    if (!isSameOriginRequest(req, this.ctx)) {
      return sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin only.' } })
    }
    const current = this.readLiveSetup()
    const identity = this.lastServiceBlock ?? null
    if (req.method === 'GET') {
      return sendJson(res, 200, {
        ok: true,
        contract: 'trading-workbench/live-setup/1',
        venue: identity?.venue ?? null,
        credential: { source: current.exists ? 'live.env' : 'absent', fingerprint: current.fingerprint },
        file: { path: current.path, exists: current.exists, keys: current.keys },
        // 步骤由 Host 给，避免前端各处拼文案造成第二套真相。
        steps: [
          'live.env 已写入后，用 WORKBENCH_VENUE=live 启动服务（或跑 scripts/switch-venue.mjs）',
          '启动前先跑 scripts/venue-preflight.mjs：有持仓/挂单/缺凭据时它会拒绝',
          '切到 live 后默认不自动交易，需要显式启动',
        ],
      })
    }
    if (req.method !== 'POST') return methodNotAllowed(res, ['GET', 'POST'])
    requireJsonRequest(req)
    const body = await readJsonBody(req, MAX_COMMAND_BODY_BYTES)
    const apiKey = typeof body?.apiKey === 'string' ? body.apiKey.trim() : ''
    const apiSecret = typeof body?.apiSecret === 'string' ? body.apiSecret.trim() : ''
    if (!apiKey || !apiSecret) {
      return sendJson(res, 422, { ok: false, error: { code: 'E_VALIDATION', message: 'apiKey and apiSecret are required' } })
    }
    if (apiKey.length > 256 || apiSecret.length > 256) {
      return sendJson(res, 422, { ok: false, error: { code: 'E_VALIDATION', message: 'credential too long' } })
    }
    const file = this.liveEnvFilePath()
    const content = [
      '# Written by the workbench live-setup entry (permissions 0600).',
      '# Never commit this file; rotate the key if it leaks.',
      'BINANCE_LIVE_API_KEY=' + apiKey,
      'BINANCE_LIVE_API_SECRET=' + apiSecret,
      '',
    ].join('\n')
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true })
      fs.writeFileSync(file, content, { encoding: 'utf8', mode: 0o600 })
      try { fs.chmodSync(file, 0o600) } catch { /* Windows best effort */ }
    } catch (error) {
      return sendJson(res, 500, { ok: false, error: { code: 'E_WRITE_FAILED', message: error.message } })
    }
    // 只回指纹与路径；明文不返回、不记录。
    return sendJson(res, 200, {
      ok: true,
      file: { path: file, exists: true },
      fingerprint: credentialFingerprint(apiKey),
      restartRequired: true,
      note: 'credential stored on disk only; the browser never receives it back',
    })
  }

  /**
   * Demo 接入（settings → 连接与账户）。
   *
   * 与 handleLiveSetup 同一套安全语义：写本机 0600 文件、只回指纹与路径、
   * 明文不回传也不落日志。区别只在目标文件与变量名——Demo 是 v1 的主环境，
   * 却一直没有写入通道，分发给别人时只能手改 .env。
   */
  async handleDemoSetup(req, res) {
    if (!isSameOriginRequest(req, this.ctx)) {
      return sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin only.' } })
    }
    const current = this.readDemoSetup()
    const block = this.lastServiceBlock ?? null
    const paths = this.serviceLauncherPaths()
    if (req.method === 'GET') {
      return sendJson(res, 200, {
        ok: true,
        contract: 'trading-workbench/demo-setup/1',
        credential: {
          configured: current.exists && Boolean(current.fingerprint),
          source: current.exists ? 'demo.env' : 'absent',
          fingerprint: current.fingerprint,
          // 服务侧自己看到的结论：文件写了但服务没重启时会不一致，这个字段就是为了不撒谎。
          service_configured: typeof block?.credentials_configured === 'boolean' ? block.credentials_configured : null,
          service_fingerprint: block?.key_fingerprint ?? block?.credential_fingerprint ?? null,
        },
        file: { path: current.path, exists: current.exists, keys: current.keys, error: current.error ?? null },
        // 服务启动时实际读取的 dotenv；可能被 WORKBENCH_DOTENV_FILE 改写。
        effective_dotenv: paths.dotenv,
        steps: [
          '保存后用「启动服务」重启 workbench-service，凭据才会被进程读到',
          '服务只读不写交易所：没有凭据时全链路只读，不会签名下单',
          'Demo 账号在币安合约测试网申请；本工具只接受 demo-fapi.binance.com',
        ],
      })
    }
    if (req.method !== 'POST') return methodNotAllowed(res, ['GET', 'POST'])
    requireJsonRequest(req)
    const body = await readJsonBody(req, MAX_COMMAND_BODY_BYTES)
    const apiKey = typeof body?.apiKey === 'string' ? body.apiKey.trim() : ''
    const apiSecret = typeof body?.apiSecret === 'string' ? body.apiSecret.trim() : ''
    if (!apiKey || !apiSecret) {
      return sendJson(res, 422, { ok: false, error: { code: 'E_VALIDATION', message: 'apiKey and apiSecret are required' } })
    }
    if (apiKey.length > 256 || apiSecret.length > 256) {
      return sendJson(res, 422, { ok: false, error: { code: 'E_VALIDATION', message: 'credential too long' } })
    }
    const file = this.demoEnvFilePath()
    const content = [
      '# Written by the workbench demo-setup entry (permissions 0600).',
      '# Never commit this file; rotate the key if it leaks.',
      'WORKBENCH_DEMO_API_KEY=' + apiKey,
      'WORKBENCH_DEMO_API_SECRET=' + apiSecret,
      '',
    ].join('\n')
    try {
      fs.mkdirSync(path.dirname(file), { recursive: true })
      fs.writeFileSync(file, content, { encoding: 'utf8', mode: 0o600 })
      try { fs.chmodSync(file, 0o600) } catch { /* Windows best effort */ }
    } catch (error) {
      return sendJson(res, 500, { ok: false, error: { code: 'E_WRITE_FAILED', message: error.message } })
    }
    this.logger?.info?.('[twb] demo credentials written to the local credential file')
    // 只回指纹与路径；明文不返回、不记录。
    return sendJson(res, 200, {
      ok: true,
      contract: 'trading-workbench/demo-setup/1',
      file: { path: file, exists: true },
      fingerprint: credentialFingerprint(apiKey),
      restartRequired: true,
      effective_dotenv: this.serviceLauncherPaths().dotenv,
      note: 'credential stored on disk only; the browser never receives it back',
    })
  }

  async handleAutomationStart(req, res) {
    if (req.method !== 'POST') return methodNotAllowed(res, ['POST'])
    if (!isSameOriginRequest(req, this.ctx)) {
      return sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin POST only.' } })
    }
    requireJsonRequest(req)
    const body = await readJsonBody(req, MAX_COMMAND_BODY_BYTES)
    const reason = typeof body?.reason === 'string' && body.reason.trim().length >= 2 ? body.reason.trim() : null
    const requestId = commandRequestId(body)
    const result = await this.startAutomation({ reason, requestId })
    if (result.ok === true) return sendJson(res, 200, result)
    const error = result.error ?? { code: 'E_AUTOMATION_START_FAILED', message: 'automation start was rejected' }
    const status = Number.isInteger(error.status) && error.status >= 400 ? error.status : 423
    return sendJson(res, status, { ...result, ok: false, error })
  }

  async state({ force = false } = {}) {
    await this.ensureConfigReady()
    const base = this.stateSync()
    const portfolio = await this.loadPortfolio({ force })
    const service = stableServiceBlock(base.service, portfolio.service, this.now())
    // The merged block is the only place that always carries the flag (the
    // portfolio block omits it on success).
    if (typeof service?.credentials_configured === 'boolean') {
      this.lastCredentialsConfigured = service.credentials_configured
    }
    // 03 C6: the environment notes are recomputed here because `base` was built
    // before the credential flag was known. Detail text never repeats the code.
    const topLevelGaps = [
      ...serviceGaps({ credentialsConfigured: this.lastCredentialsConfigured, llmMode: this.llmMode }),
      ...(base.gaps ?? []),
      ...(portfolio.gaps ?? []),
    ]
    if (portfolio.equityCurve?.gap) {
      topLevelGaps.push(portfolioGapText(portfolio.equityCurve.gap, portfolio.equityCurve.empty_reason))
    }
    if (portfolio.account?.gap) {
      topLevelGaps.push(portfolioGapText(portfolio.account.gap, portfolio.account.empty_reason))
    }
    // F1: remember what the UI was just told, and the control row that came
    // with this portfolio. Both are facts for the automation projection; the
    // service remains the only writer of the control state itself.
    this.lastServiceBlock = service
    this.lastControl = portfolio.control ?? null
    return {
      ...base,
      service,
      // §2.2 / §3.2: one projection the top bar can trust, computed on the Host
      // from service facts (control row + schedule), not inferred in the client.
      automation: this.automationSnapshot(service, { now: this.now() }),
      host: {
        data_dir: path.resolve(String(this.dataDir)),
        service_base_url: this.serviceBaseUrl,
        environment: environmentOf(service),
        release: {
          release_id: base?.build?.release_id ?? null,
          source: base?.build?.release_source ?? 'absent',
          source_digest: base?.build?.release_source_digest ?? null,
          generated_at: base?.build?.release_generated_at ?? null,
          plugin_version: base?.build?.plugin_version ?? null,
        },
        loaded_at: base?.build?.loaded_at ?? null,
      },
      equityCurve: portfolio.equityCurve,
      equity_curve: portfolio.equityCurve,
      equitySession: portfolio.equitySession ?? portfolio.equityCurve?.session ?? null,
      equityExchange: portfolio.equityExchange ?? portfolio.equityCurve?.exchange ?? null,
      positions: portfolio.positions,
      positionsEmpty: portfolio.positionsEmpty,
      positionsEmptyReason: portfolio.positionsEmptyReason,
      orders: portfolio.orders,
      ordersEmpty: portfolio.ordersEmpty,
      ordersEmptyReason: portfolio.ordersEmptyReason,
      fills: portfolio.fills,
      fillsEmpty: portfolio.fillsEmpty,
      fillsEmptyReason: portfolio.fillsEmptyReason,
      reviews: portfolio.reviews,
      reviewsSource: portfolio.reviewsSource ?? null,
      reviewsEmpty: portfolio.reviewsEmpty,
      reviewsEmptyReason: portfolio.reviewsEmptyReason,
      reviewsGap: portfolio.reviewsGap,
      account: portfolio.account,
      performance: portfolio.performance ?? null,
      accountEmpty: portfolio.accountEmpty,
      accountGap: portfolio.accountGap,
      portfolio: {
        fetched_at: portfolio.fetchedAt,
        source: portfolio.source,
        cached: portfolio.cached,
        cache_hit: portfolio.cacheHit,
        cache_age_ms: portfolio.cacheAgeMs ?? 0,
        refresh_mode: portfolio.refreshMode ?? null,
        gaps: portfolio.gaps,
        // F2 diagnostics (B facts): pending/unknown outbox rows, startup recovery
        // and the protection verdict. Kept inside the portfolio block so the
        // "same fetch" provenance stays visible.
        outbox: portfolio.outbox ?? null,
        exchange: portfolio.exchange ?? null,
        recovery: portfolio.recovery ?? null,
        startup: portfolio.startup ?? null,
        protection: portfolio.protection ?? null,
        control: portfolio.control ?? null,
      },
      gaps: [...new Set(topLevelGaps)],
    }
  }
  listRounds(date = null) {
    this.store.ensure()
    this.repairStaleRunning()
    const today = archiveDay(this.now())
    const day = date ?? today
    const records = this.store.listDate(day)
    // 15 §4: a single day is the unit of query. The cap only guards against a
    // pathological partition; it is far above the 96/day schedule ceiling.
    const truncated = records.length > MAX_ROUNDS_PER_DAY
    return {
      ok: true,
      date: day,
      today,
      rounds: records.slice(0, MAX_ROUNDS_PER_DAY).map(roundSummaryOf),
      total: records.length,
      truncated,
    }
  }

  getRound(roundId) {
    // 15 §4: detail lookup spans the whole archive, so an old round opens from
    // the date list exactly like today's does.
    const record = this.store.find(roundId)
    return record ? { ok: true, round: roundViewOf(record) } : null
  }

  async serviceSnapshot(client, tokenInfo) {
    const checkedAt = this.now()
    const ready = await client.ready()
    const serviceReady = ready?.service_ready === true
      || (ready?.service_ready === undefined && ready?.ready === true)
    if (!serviceReady) {
      throw new HostIntegrationError('E_SERVICE_NOT_READY', 'workbench-service is not ready for a new entry', {
        status: 503,
        retryable: true,
        details: { reasons: ready?.reasons ?? [], ready },
      })
    }
    const envelope = await client.riskEnvelope()
    let status = null
    let statusError = null
    try {
      status = await client.status()
    } catch (error) {
      statusError = hostErrorFromService(error)?.code ?? 'E_SERVICE_STATUS_UNAVAILABLE'
    }
    let marketHealth = null
    let healthError = null
    try {
      marketHealth = await client.marketHealth()
    } catch (error) {
      healthError = hostErrorFromService(error)?.code ?? 'E_MARKET_HEALTH_UNAVAILABLE'
    }
    const prices = await client.prices([DEFAULT_SYMBOL])
    return {
      checkedAt,
      ready,
      envelope,
      status,
      statusError,
      marketHealth,
      healthError,
      prices,
      service: summarizeService({
        baseUrl: this.serviceBaseUrl,
        ready,
        envelope,
        status,
        tokenInfo,
        now: checkedAt,
      }),
    }
  }

  /**
   * Cheap, LLM-free gate the auto scheduler calls before it spends a round.
   * Mirrors the blockers that would otherwise surface only after 6–8 LLM calls
   * (control_state, Demo credentials, account equity) so an auto round that
   * cannot possibly trade costs nothing.
   */
  async preflight({ ignoreControl = false } = {}) {
    const tokenInfo = this.tokenInfo()
    if (!tokenInfo.present) {
      return { ok: false, code: 'E_SERVICE_TOKEN_MISSING', detail: 'workbench-service API token is not configured' }
    }
    if (this.llmMode !== 'session_fixture') {
      if (!this.llm || typeof this.llm.stream !== 'function') {
        return { ok: false, code: 'NO_ADAPTER', detail: 'no Harness LLM is available to the Host plugin' }
      }
      if (!this.route?.provider || !this.route?.model) {
        return { ok: false, code: 'NO_ADAPTER', detail: 'no default Harness model route is selected' }
      }
    }
    const client = this.clientWithToken(tokenInfo.token)
    let snapshot
    try {
      snapshot = await this.serviceSnapshot(client, tokenInfo)
    } catch (error) {
      return {
        ok: false,
        code: error?.code ?? 'E_SERVICE_UNAVAILABLE',
        detail: error?.message ?? String(error),
      }
    }
    const state = controlState(snapshot.envelope)
    const credentials = snapshot.status?.credentials_configured === true
    const account = executionAccountContext({ cycleResponse: null, snapshot })
    const accountReady = [
      account.account?.available_balance,
      account.account?.total_margin_balance,
      account.account?.total_wallet_balance,
      account.equity?.exchange,
      account.equity?.session,
    ].some(hasNumericValue)
    const verdict = {
      ok: true,
      code: null,
      detail: null,
      control_state: state,
      credentials_configured: credentials,
      account_ready: accountReady,
      service_ready: true,
    }
    // F1 §3.2: "启动自动交易" must verify identity/data/unresolved state BEFORE it
    // touches the control row, and the control row is exactly what it is about
    // to change. `ignoreControl` is used only by that explicit start path.
    if (!ignoreControl && CONTROL_BLOCKING.has(state)) {
      return { ...verdict, ok: false, code: 'CONTROL_LOCKED', detail: `control_state=${state} blocks new entries` }
    }
    if (!credentials) {
      return {
        ...verdict,
        ok: false,
        code: 'E_DEMO_CREDENTIALS_MISSING',
        detail: 'Demo credentials are not configured; P2 cannot sign an order',
      }
    }
    if (!accountReady) {
      return { ...verdict, ok: false, code: 'ACCOUNT_UNAVAILABLE', detail: 'account equity / available balance is unavailable' }
    }
    // M1.4e: an open position makes R02 reject every new entry. Catching that
    // here costs one status call instead of a full ~3 minute, ~50k-token chain
    // that can only end in E_RISK_SYMBOL_POSITION.
    const openPositions = countOpenPositions(snapshot)
    if (openPositions > 0) {
      return {
        ...verdict,
        ok: false,
        code: 'POSITION_OPEN',
        detail: `${openPositions} open position(s); a new entry would be rejected by R02`,
        open_positions: openPositions,
      }
    }
    return verdict
  }

  setScheduleStatus(status) {
    this.scheduleStatus = status ?? null
  }

  /**
   * A previous Host process can leave a record stuck at status="running"
   * (killed mid-round, provider hang, OS restart). After this process starts,
   * inFlight is null, so every "running" record is stale by definition.
   * Repairing them keeps the UI honest and the round list countable.
   */
  repairStaleRunning() {
    if (this.staleRepaired) return false
    this.staleRepaired = true
    let changed = false
    try {
      // 15 §3.3: only recent partitions can hold a stale "running" row; the
      // scan stays bounded instead of walking years of history.
      for (const record of this.store.listRecent(200, { maxDays: 7 })) {
        if (record?.status !== 'running' || record.roundId === this.inFlight) continue
        const finished = this.now()
        const startedMs = Date.parse(record.startedAt ?? '')
        record.status = 'failed'
        record.outcome = 'failed'
        record.reasonCode = 'HOST_STALE_RUN'
        record.headline = '失败：HOST_STALE_RUN'
        record.finishedAt = record.finishedAt ?? nowIso(finished)
        record.durationMs = Number.isFinite(record.durationMs)
          ? record.durationMs
          : Math.max(0, finished - (Number.isFinite(startedMs) ? startedMs : finished))
        record.error = record.error ?? {
          code: 'HOST_STALE_RUN',
          message: 'round was left running by a previous Host process',
          status: 504,
          retryable: true,
          details: { round_id: record.roundId },
        }
        record.gaps = [...(record.gaps ?? []), 'recovered on startup: previous Host left this round running']
        this.store.upsert(record)
        this.revokeLeaseForRun(record.roundId)
        changed = true
      }
    } catch (error) {
      this.logger?.warn?.(`[twb] repairStaleRunning failed: ${error?.message ?? error}`)
    }
    if (changed) this.logger?.warn?.('[twb] repaired stale running round(s) from a previous Host process')
    return changed
  }

  /**
   * Force a hung round to a terminal state so the scheduler can keep running.
   * Safe to call twice: a round that is no longer `running` is left untouched.
   */
  revokeRunLease(roundId) {
    const entry = this.runLeases.get(roundId)
    if (!entry) return
    this.runLeases.delete(roundId)
    try {
      const pending = entry.client.releaseLease({ lease_id: entry.leaseId })
      if (pending && typeof pending.catch === 'function') {
        pending.catch((error) => this.logger?.warn?.(`[twb] lease release failed: ${error?.code ?? error}`))
      }
    } catch (error) {
      this.logger?.warn?.(`[twb] lease release failed: ${error?.code ?? error}`)
    }
  }

  /**
   * Recover a cycle whose POST answer was lost (timeout/offline).
   *
   * `POST /api/cycles` builds and persists md-evidence/1 before answering, so a
   * slow write can commit the RUNNING cycle and still blow the client budget.
   * Failing the round there is wrong twice over: the LLM chain never runs, and
   * the orphan RUNNING cycle then rejects every later create with
   * E_CYCLE_RUNNING (observed 2026-11-13: one 12s timeout wedged the scheduler).
   *
   * Rules:
   *  - a RUNNING cycle carrying our slot_id is this attempt -> adopt it;
   *  - an old RUNNING cycle without our slot_id is an orphan -> finalize it and
   *    create once more;
   *  - a young RUNNING cycle without our slot_id may be another Host -> leave it.
   */
  async recoverCycleAfterAmbigousCreate(client, { cycleId, slotId, trigger = null }) {
    const listed = await client.listCycles(20)
    const cycles = Array.isArray(listed?.cycles) ? listed.cycles : []
    const running = cycles.filter((item) => String(item?.state ?? '').toUpperCase() === 'RUNNING')
    if (running.length === 0) return null
    const mine = slotId ? running.find((item) => String(item?.slot_id ?? '') === String(slotId)) : null
    const target = mine ?? running[0]
    if (!mine) {
      const startedAt = Date.parse(target?.started_at ?? '')
      const ageMs = Number.isFinite(startedAt) ? this.now() - startedAt : Number.POSITIVE_INFINITY
      if (ageMs < CYCLE_STALE_MS) return null
      try {
        await client.finalizeCycle(target.cycle_id, {
          state: 'FAILED',
          outcome: 'error',
          reason: 'HOST_STALE_CYCLE',
        })
      } catch (error) {
        this.logger?.warn?.(`[twb] stale cycle finalize failed: ${error?.code ?? error}`)
        return null
      }
      const created = await client.createCycle(cycleId, undefined, slotId, { trigger })
      if (!created?.cycle_id) return null
      const evidence = await client.cycleEvidence(created.cycle_id)
      return { ...created, evidence }
    }
    const evidence = await client.cycleEvidence(target.cycle_id)
    return { ...target, evidence }
  }

  revokeLeaseForRun(runId) {
    try {
      const tokenInfo = this.tokenInfo()
      if (!tokenInfo.present) return
      const client = this.clientWithToken(tokenInfo.token)
      const pending = client.revokeLeaseRun({ run_id: runId })
      if (pending && typeof pending.catch === 'function') {
        pending.catch((error) => this.logger?.warn?.(`[twb] stale lease revoke failed: ${error?.code ?? error}`))
      }
    } catch (error) {
      this.logger?.warn?.(`[twb] stale lease revoke failed: ${error?.code ?? error}`)
    }
  }

  async releaseRunLease(roundId) {
    const entry = this.runLeases.get(roundId)
    if (!entry) return
    this.runLeases.delete(roundId)
    try {
      await entry.client.releaseLease({ lease_id: entry.leaseId })
    } catch (error) {
      this.logger?.warn?.(`[twb] lease release failed: ${error?.code ?? error}`)
    }
  }

  abandonRun(roundId, code = 'HOST_RUN_TIMEOUT', detail = null) {
    this.abandonedRuns.add(roundId)
    // The Service-side lease must be revoked even if this process later dies.
    if (this.runLeases.has(roundId)) this.revokeRunLease(roundId)
    else this.revokeLeaseForRun(roundId)
    let changed = false
    try {
      const record = this.store.find(roundId)
      if (record && record.status === 'running') {
        const finished = this.now()
        record.status = 'failed'
        record.outcome = 'failed'
        record.reasonCode = code
        record.headline = `失败：${code}`
        record.finishedAt = nowIso(finished)
        record.durationMs = Number.isFinite(record.durationMs) ? record.durationMs : finished - (Date.parse(record.startedAt ?? '') || finished)
        record.error = {
          code,
          message: detail ?? `round ${roundId} did not finish in time`,
          status: 504,
          retryable: true,
          details: { round_id: roundId, timeout: true },
        }
        record.gaps = [...(record.gaps ?? []), `watchdog: ${code} at ${record.finishedAt}`]
        this.store.upsert(record)
        this.lastError = record.error
        changed = true
      }
    } catch (error) {
      this.logger?.warn?.(`[twb] abandonRun failed for ${roundId}: ${error?.message ?? error}`)
    }
    if (this.inFlight === roundId) {
      this.inFlight = null
      // 01 §5.3: an abandoned round must not leave a stale running stage.
      this.inFlightStatus = null
    }
    if (changed) {
      this.logger?.warn?.(`[twb] abandoned hung round ${roundId} (${code})`)
    }
    return changed
  }

  /**
   * Load the durable auto-run budget sidecar. On first use (or after a Host
   * reload) seed it from the retained round cache so an upgrade does not reset
   * the 24h budget. It never contains prompts, tokens or orders.
   */
  loadAutoBudget() {
    const cutoff = this.now() - 7 * 24 * 3_600_000
    const valid = (row) => row && typeof row.at === 'string' && Number.isFinite(Date.parse(row.at))
      && Date.parse(row.at) >= cutoff
    let rows = []
    try {
      const parsed = JSON.parse(fs.readFileSync(this.autoBudgetPath, 'utf8'))
      rows = Array.isArray(parsed?.rounds) ? parsed.rounds.filter(valid) : []
    } catch (error) {
      if (error?.code !== 'ENOENT') this.logger?.warn?.(`[twb] cannot read auto budget: ${error?.message ?? error}`)
    }
    if (rows.length === 0) {
      // 15 §3.3: the budget window is 7 days, so only the last few partitions
      // are needed to seed it (never the whole archive).
      rows = this.store.listRecent(2000, { maxDays: 8 })
        .filter((row) => row?.trigger === 'auto'
          && Number.isFinite(Date.parse(row?.startedAt ?? ''))
          && Date.parse(row.startedAt) >= cutoff)
        .map((row) => ({ roundId: row.roundId ?? null, at: row.startedAt }))
    }
    return rows.slice(-2000)
  }

  persistAutoBudget() {
    try {
      fs.mkdirSync(path.dirname(this.autoBudgetPath), { recursive: true })
      const tmp = `${this.autoBudgetPath}.tmp`
      fs.writeFileSync(tmp, JSON.stringify({ contract: 'trading-workbench/auto-budget/1', rounds: this.autoBudget }, null, 2), 'utf8')
      fs.renameSync(tmp, this.autoBudgetPath)
    } catch (error) {
      this.logger?.warn?.(`[twb] cannot persist auto budget: ${error?.message ?? error}`)
    }
  }

  recordAutoRunStart(atMs, roundId = null) {
    if (this.autoBudget.some((row) => roundId && row.roundId === roundId)) return
    const at = nowIso(atMs)
    this.autoBudget.push({ roundId, at })
    const cutoff = this.now() - 7 * 24 * 3_600_000
    this.autoBudget = this.autoBudget.filter((row) => Date.parse(row.at) >= cutoff).slice(-2000)
    this.persistAutoBudget()
  }

  autoRoundsLast24h(at = this.now()) {
    const since = at - 24 * 3_600_000
    const seen = new Set()
    let count = 0
    for (const row of this.autoBudget) {
      const started = Date.parse(row?.at ?? '')
      if (!Number.isFinite(started) || started < since) continue
      const key = row.roundId ?? `${started}:${count}`
      if (seen.has(key)) continue
      seen.add(key)
      count += 1
    }
    return count
  }

  /**
   * Recovery used by the scheduler before it skips an "in flight" round: a
   * round whose start is older than the run timeout is hung, not busy.
   */
  recoverStaleInFlight() {
    if (!this.inFlight) return false
    const started = Number.isFinite(this.lastRunStartedAt) ? this.lastRunStartedAt : null
    if (started === null) return false
    const age = this.now() - started
    if (age < this.runTimeoutMs + 30_000) return false
    return this.abandonRun(this.inFlight, 'HOST_STALE_RUN', `in-flight round older than ${Math.round(age / 1000)}s`)
  }

  async run({ symbol = DEFAULT_SYMBOL, trigger = 'manual', slotId = null } = {}) {
    if (this.inFlight) {
      throw new HostIntegrationError('RUN_IN_PROGRESS', 'a manual trading round is already running', {
        status: 409,
        retryable: true,
        details: { round_id: this.inFlight },
      })
    }
    if (typeof symbol !== 'string' || !SYMBOL_RE.test(symbol)) {
      throw new HostIntegrationError('E_INVALID_SYMBOL', 'symbol must look like BTCUSDT', {
        status: 400,
        details: { symbol },
      })
    }
    const tokenInfo = this.tokenInfo()
    if (!tokenInfo.present) {
      throw new HostIntegrationError('E_SERVICE_TOKEN_MISSING', 'workbench-service API token is not configured', {
        status: 503,
        details: {
          token_source: tokenInfo.source,
          token_path: tokenInfo.source === 'file' ? tokenInfo.path : null,
          guidance: 'Start workbench-service (it creates data/.api-token) or set WORKBENCH_API_TOKEN, then retry.',
        },
      })
    }
    if (!this.llm || typeof this.llm.stream !== 'function') {
      throw new HostIntegrationError('NO_ADAPTER', 'No Harness LLM is available to the Host plugin', {
        status: 503,
        details: { guidance: 'Select a default model in Harness.' },
      })
    }
    if (!this.route?.provider || !this.route?.model) {
      throw new HostIntegrationError('NO_ADAPTER', 'No default Harness model route is selected', {
        status: 503,
        details: { guidance: 'Select a default model in Harness.' },
      })
    }

    const started = this.now()
    const cycleId = cycleIdFor(started)
    const roundId = cycleId
    let activeCycleId = cycleId
    this.inFlight = roundId
    const record = {
      roundId,
      cycleId,
      cycleSource: 'host',
      trigger,
      slotId: slotId ?? null,
      symbol,
      status: 'running',
      outcome: null,
      reasonCode: null,
      headline: '运行中',
      startedAt: nowIso(started),
      finishedAt: null,
      durationMs: null,
      result: null,
      service: null,
      freshness: null,
      gaps: [],
      serviceStatus: 'pending',
      errorStatus: null,
      httpStatus: null,
      error: null,
    }
    this.store.upsert(record)
    if (trigger === 'auto') this.recordAutoRunStart(started, roundId)

    // 02 §3.2 / P-05=A: freeze this round's per-role routes against the live
    // catalog BEFORE any service or LLM side effect. A freeze failure is a legal
    // no-trade termination (no call, no service cycle), not a crash.
    const frozen = await this.freezeRoundRoutes()
    if (!frozen.ok) {
      const finished = this.now()
      record.status = 'no_trade'
      record.outcome = 'no_trade'
      record.reasonCode = frozen.code ?? 'ROUTE_UNAVAILABLE'
      record.headline = `无交易：${record.reasonCode}`
      record.result = {
        outcome: 'no_trade',
        reasonCode: record.reasonCode,
        detail: frozen.detail ?? null,
        calls: [],
        outputs: {},
      }
      record.routeSnapshot = frozen.routeSnapshot ?? null
      record.gaps.push(`route freeze blocked this round: ${frozen.detail ?? frozen.code}`)
      record.gaps.push('P-05=A: no AI call was started and no service cycle was created.')
      record.finishedAt = nowIso(finished)
      record.durationMs = finished - started
      record.serviceStatus = 'ok'
      this.lastRunAt = nowIso(finished)
      this.store.upsert(record)
      if (this.inFlight === roundId) this.inFlight = null
      return { ok: true, round: roundViewOf(record) }
    }
    record.routeSnapshot = frozen.snapshot ?? null

    // 01 §5.3 / 05 P1-N01: structured running progress (in-memory only). The
    // events collected here are flushed post-hoc in persistAiAudit — they are
    // review material, never realtime telemetry (RT-M6).
    const inFlightState = {
      roundId,
      cycleId: activeCycleId,
      trigger,
      symbol,
      startedAtMs: started,
      started_at: nowIso(started),
      stage_key: null,
      stage_label: null,
      phase: null,
      stage_started_at_ms: null,
      stage_started_at: null,
      attempt: null,
      calls_done: 0,
      degraded: false,
      roles_done: [],
      last_event: null,
    }
    this.inFlightStatus = inFlightState
    const runEvents = []
    const onProgress = (event) => {
      runEvents.push(event)
      inFlightState.last_event = event.event_type ?? null
      if (event.event_type === 'role_start') {
        inFlightState.stage_key = event.role_key
        inFlightState.stage_label = ROLE_LABELS[event.role_key] ?? event.role_key
        inFlightState.phase = event.phase ?? null
        inFlightState.stage_started_at_ms = event.started_at_ms ?? this.now()
        inFlightState.stage_started_at = nowIso(inFlightState.stage_started_at_ms)
        inFlightState.attempt = event.attempt ?? 1
      } else if (event.event_type === 'role_end') {
        inFlightState.calls_done += 1
        if (event.status === 'ok' && event.role_key && !inFlightState.roles_done.includes(event.role_key)) {
          inFlightState.roles_done.push(event.role_key)
        }
      } else if (event.event_type === 'debate_degraded') {
        inFlightState.degraded = true
      }
    }

    // M1.4c: hard watchdog. A provider that never closes its stream used to
    // leave `inFlight` set forever: the round stayed "running", the scheduler
    // stayed busy and every later tick was skipped — automation died silently.
    // The timer only unblocks the system (clear inFlight + persist a failed
    // record); if the hung call ever resolves, its normal path simply finalizes
    // the same round with the real outcome.
    this.lastRunStartedAt = started
    const watchdog = setTimeout(() => {
      this.abandonRun(roundId, 'HOST_RUN_TIMEOUT', `round exceeded ${this.runTimeoutMs} ms`)
    }, Math.max(1_000, this.runTimeoutMs))
    if (typeof watchdog?.unref === 'function') watchdog.unref()

    const client = this.clientWithToken(tokenInfo.token)
    // T15: the pre-submit verdict is decided inside the p2 gateway and recorded
    // after the chain returns. It never decides anything by itself unless the gate
    // mode is enforce (which stays off while there is no re-quote).
    let preSubmitGate = null
    let p1Token = null
    let p1Plan = null
    let p1Raw = null
    let p1Error = null
    let p2Error = null
    let cycleCreated = false
    let cycleResponse = null
    let leaseId = null
    try {
      const leaseTtlS = Math.max(120, Math.ceil((Number(this.runTimeoutMs) + 60_000) / 1000))
      const lease = await client.acquireLease({ run_id: roundId, owner: 'host', ttl_s: leaseTtlS })
      leaseId = lease?.lease_id ?? null
      if (!leaseId) throw new HostIntegrationError('E_LEASE_REQUIRED', 'service did not issue an execution lease', { status: 409 })
      this.runLeases.set(roundId, { client, leaseId })
      const snapshot = await this.serviceSnapshot(client, tokenInfo)
      this.lastService = snapshot.service
      this.lastCredentialsConfigured = snapshot.status?.credentials_configured === true
        ? true
        : (snapshot.status?.credentials_configured === false ? false : null)
      record.service = snapshot.service
      record.serviceStatus = snapshot.healthError || snapshot.statusError ? 'degraded' : 'ok'
      // 03 C6: the environment notes are appended only when they are true for
      // this profile (measured 2026-11-12: credentials_configured=true while the
      // static "No Demo API key" gap still showed).
      record.gaps.push(...serviceGaps({
        credentialsConfigured: this.lastCredentialsConfigured,
        llmMode: this.llmMode,
      }))
      const primarySymbol = symbol || DEFAULT_SYMBOL
      try {
        let cycle
        try {
          cycle = await client.createCycle(cycleId, primarySymbol, slotId, { trigger: record.trigger })
        } catch (error) {
          if (error instanceof ServiceClientError && error.status === 409 && error.code === 'E_CYCLE_RUNNING') {
            const staleCycleId = error.details?.cycle_id
            if (staleCycleId) {
              try {
                await client.finalizeCycle(staleCycleId, {
                  state: 'FAILED',
                  outcome: 'error',
                  reason: 'HOST_STALE_CYCLE',
                })
                record.gaps.push(`Finalized stale RUNNING service cycle ${staleCycleId} before starting a new manual round.`)
              } catch (finalizeError) {
                record.gaps.push(
                  `Stale service cycle ${staleCycleId} could not be finalized: ${finalizeError?.code ?? finalizeError?.message ?? finalizeError}.`,
                )
              }
            }
            cycle = await client.createCycle(cycleId, primarySymbol, slotId, { trigger: record.trigger })
          } else {
            throw error
          }
        }
        cycleResponse = cycle
        cycleCreated = true
        if (cycle?.cycle_id && cycle.cycle_id !== cycleId) {
          activeCycleId = String(cycle.cycle_id)
          record.cycleId = activeCycleId
          record.cycleSource = 'service'
          record.gaps.push(
            `POST /api/cycles generated service cycle ${activeCycleId}; Host adopted it for RoundView, P1 and P2 (requested Host id ${cycleId}).`,
          )
        } else if (cycle?.cycle_id) {
          activeCycleId = String(cycle.cycle_id)
          record.cycleSource = 'host'
        }
      } catch (error) {
        cycleResponse = null
        if (error instanceof ServiceClientError && error.status === 404) {
          record.cycleSource = 'host'
          record.gaps.push(
            'POST /api/cycles is not implemented (404); Host forwarded its cycle_id to P1, but no server evidence snapshot can be created for this cycle.',
          )
        } else if (error instanceof ServiceClientError && error.code === 'E_CYCLE_DUPLICATE_SLOT') {
          // M1.4c: the service already ran this scheduler slot (a second timer,
          // a reload, or a retry after a lost response). That is the slot's job
          // working as designed — not a trading failure. Close the record as a
          // skip and return without spending a single LLM call.
          const finished = this.now()
          record.status = 'skipped'
          record.outcome = 'no_trade'
          record.reasonCode = 'OVERLAP_SKIPPED'
          record.headline = '跳过：该调度槽位已完成'
          record.finishedAt = nowIso(finished)
          record.durationMs = finished - started
          record.serviceStatus = 'ok'
          record.gaps.push(`slot ${slotId ?? '-'} was already consumed on the service side; no round was run.`)
          this.lastRunAt = nowIso(finished)
          this.store.upsert(record)
          return { ok: true, skipped: true, round: roundViewOf(record) }
        } else if (error instanceof ServiceClientError && [401, 403, 409, 423].includes(error.status)) {
          record.serviceStatus = serviceStatusForError(error.status, error.code)
          record.errorStatus = errorStatus('CYCLE', error.status, error.code, error.message, error.retryable)
          record.httpStatus = error.status
          record.status = 'failed'
          record.outcome = 'failed'
          record.reasonCode = error.code
          record.headline = `失败：${error.code}`
          record.error = { code: error.code, message: error.message, status: error.status, retryable: error.retryable, details: error.details ?? {} }
          throw new HostIntegrationError(error.code, error.message, {
            status: error.status,
            retryable: error.retryable,
            details: { ...(error.details ?? {}), stage: 'CYCLE', cycle_id: activeCycleId },
          })
        } else {
          // A timeout/offline POST /api/cycles is ambiguous: the service may
          // have committed the RUNNING cycle before the answer was lost. Adopt
          // it (and its evidence) instead of failing the round and leaving an
          // orphan cycle that blocks every later round with E_CYCLE_RUNNING.
          const recovered = await this.recoverCycleAfterAmbigousCreate(client, {
            cycleId,
            slotId,
            trigger: record.trigger,
          }).catch(() => null)
          if (recovered) {
            cycleResponse = recovered
            cycleCreated = true
            if (recovered.cycle_id) {
              activeCycleId = String(recovered.cycle_id)
              record.cycleId = activeCycleId
              record.cycleSource = 'service'
            }
            record.gaps.push(
              `POST /api/cycles did not answer (${error?.code ?? 'timeout'}) but the service had already created cycle ${activeCycleId}; the Host adopted it and its persisted evidence instead of leaving a RUNNING orphan.`,
            )
          } else {
            record.gaps.push(`POST /api/cycles failed: ${error?.code ?? error?.message ?? error}; P1 may reject missing server evidence.`)
          }
        }
      }
      let evidence
      let evidenceRef
      try {
        const built = buildServiceEvidence({
          cycleResponse,
          symbol: primarySymbol,
          now: this.now(),
        })
        evidence = built.view
        evidenceRef = built.ref
      } catch (error) {
        if (error instanceof HostIntegrationError) throw error
        throw new HostIntegrationError('E_EVIDENCE_INVALID', `cannot project service evidence: ${error?.message ?? error}`, {
          status: 502,
          details: { cycle_id: activeCycleId },
        })
      }
      record.freshness = {
        snapshot_id: evidenceRef.snapshot_id,
        produced_at: evidenceRef.produced_at,
        age_ms: evidence.age_ms,
        data_verdict: evidenceRef.verdict,
        source: 'service:/api/cycles',
        sha256: evidenceRef.sha256,
        path: evidenceRef.path,
        bytes: evidenceRef.bytes,
      }
      record.evidence = evidenceRef
      if (snapshot.healthError) {
        record.gaps.push(`market health unavailable: ${snapshot.healthError}; evidence marked DEGRADED`)
      }
      if (snapshot.statusError) {
        record.gaps.push(`account status unavailable: ${snapshot.statusError}; P2 is disabled for this round`)
      }
      if (!snapshot.status?.credentials_configured) {
        record.gaps.push('Demo credentials are not configured; P1/P2 cannot be attempted as a real order.')
      }

      const caps = capsFromEnvelope(snapshot.envelope)
      const capOverride = {}
      for (const [key, value] of Object.entries(caps)) {
        // A (§8·A): the legacy strong tier is display/history only. Handing it
        // to the chain would let a role plan for a budget that no longer exists.
        if (STRONG_TIER_CAPS.has(key)) continue
        if (value !== undefined) capOverride[key] = value
      }
      const state = controlState(snapshot.envelope)
      const blockedByRiskState = Boolean(snapshot.envelope?.risk_state?.daily_loss_latched || snapshot.envelope?.risk_state?.drawdown_latched)
      const localVerdict = CONTROL_BLOCKING.has(state) || blockedByRiskState
        ? { verdict: 'close_only', detail: state, blockers: snapshot.envelope?.blocking_reasons ?? [] }
        : { verdict: 'approved', blockers: snapshot.envelope?.blocking_reasons ?? [] }

      const gateways = {
        p1: async ({ execution }) => {
          if (this.abandonedRuns.has(roundId)) {
            return {
              verdict: 'rejected',
              code: 'HOST_RUN_ABANDONED',
              detail: 'round ownership was revoked; P1 was not called',
              service_status: 409,
            }
          }
          this.setInFlightStage('p1', 'P1 风控闸门')
          try {
            const plan = planForService(execution, activeCycleId)
            const response = await client.evaluatePlan(plan, activeCycleId, leaseId)
            p1Raw = response
            const mapped = p1ResultFromService(response)
            p1Token = mapped.token
            p1Plan = mapped.plan
            const { token: _token, raw: _raw, ...safe } = mapped
            return safe
          } catch (error) {
            if (error instanceof ServiceClientError) {
              p1Error = {
                status: error.status,
                code: error.code,
                message: error.message,
                retryable: error.retryable,
                details: error.details ?? {},
              }
              return {
                verdict: 'rejected',
                code: error.code,
                detail: error.message,
                service_status: error.status,
              }
            }
            const mapped = hostErrorFromService(error)
            if (mapped) {
              p1Error = {
                status: error.status ?? mapped.status,
                code: mapped.code,
                message: mapped.message,
                retryable: mapped.retryable,
                details: mapped.details ?? {},
              }
              return {
                verdict: 'rejected',
                code: mapped.code,
                detail: mapped.message,
                service_status: error.status ?? null,
              }
            }
            throw error
          }
        },
        p2: async ({ decision } = {}) => {
          if (this.abandonedRuns.has(roundId)) {
            return {
              ok: false,
              code: 'HOST_RUN_ABANDONED',
              detail: 'round ownership was revoked; P2 was not called',
              status: 409,
            }
          }
          this.setInFlightStage('p2', 'P2 执行')
          // §8·D / T15：真正提交前的最后一道确定性检查（同一个 evaluateEntryPlan）。
          // 拿不到提交价，所以只有时间类判定可以拦（见 preSubmitGateOf）。
          preSubmitGate = preSubmitGateOf({
            decision,
            now: this.now(),
            mode: this.config?.executionGate ?? DEFAULT_EXECUTION_GATE_MODE,
          })
          if (preSubmitGate?.enforced && preSubmitGate.ok === false) {
            return {
              ok: false,
              code: `EXECUTION_BLOCKED:${preSubmitGate.code}`,
              detail: preSubmitGate.detail,
              status: 422,
              pre_submit: true,
            }
          }
          if (this.llmMode === 'session_fixture') {
            return {
              ok: false,
              code: 'E_TEST_MODE_NO_ORDER',
              detail: 'session-llm fixture mode: signed order submission is disabled.',
              status: 503,
            }
          }
          if (snapshot.status?.credentials_configured !== true) {
            return {
              ok: false,
              code: 'E_DEMO_CREDENTIALS_MISSING',
              detail: 'Demo API key/secret not configured; signed execution was not attempted.',
              status: 503,
            }
          }
          if (!p1Token) {
            p2Error = { status: 401, code: 'E_APPROVAL_REQUIRED', message: 'P1 did not issue an approval token.', retryable: false, details: {} }
            return { ok: false, code: 'E_APPROVAL_REQUIRED', detail: 'P1 did not issue an approval token.', status: 401 }
          }
          try {
            const response = await client.submitOrder({
              approval_token: p1Token,
              cycle_id: activeCycleId,
              plan: p1Plan,
              lease_id: leaseId,
            })
            return p2ResultFromService(response)
          } catch (error) {
            if (error instanceof ServiceClientError) {
              p2Error = {
                status: error.status,
                code: error.code,
                message: error.message,
                retryable: error.retryable,
                details: error.details ?? {},
              }
              return { ok: false, code: error.code, detail: error.message, status: error.status }
            }
            const mapped = hostErrorFromService(error)
            if (mapped) {
              p2Error = {
                status: error.status ?? mapped.status,
                code: mapped.code,
                message: mapped.message,
                retryable: mapped.retryable,
                details: mapped.details ?? {},
              }
              return { ok: false, code: mapped.code, detail: mapped.message, status: error.status ?? null }
            }
            throw error
          }
        },
      }

      const result = await runChain({
        llm: this.llm,
        route: this.route,
        roleRoutes: frozen.roleRoutes ?? null,
        effectiveEfforts: frozen.effectiveEfforts ?? null,
        onProgress,
        evidence,
        sessionId: `twb-run-${randomUUID()}`,
        hardRisk: localVerdict,
        account: executionAccountContext({ cycleResponse, snapshot }).account,
        equity: executionAccountContext({ cycleResponse, snapshot }).equity,
        config: {
          ...(this.config ?? {}),
          now: this.now,
          roleMaxTokens: {
            ...ROLE_MAX_TOKENS,
            ...(this.config?.roleMaxTokens ?? {}),
          },
          roleTimeoutsMs: {
            // M1.4f: analysis timeout now lives in constants.js (70s). A Host
            // override here would silently advance the documented budget.
            ...(this.config?.roleTimeoutsMs ?? {}),
          },
          caps: { ...(this.config?.caps ?? {}), ...capOverride },
        },
        gateways,
      })

      if (this.abandonedRuns.has(roundId)) {
        // A watchdog/stale repair already wrote the terminal failed record.
        // Keep it; this late result is audit material only.
        throw new HostIntegrationError(
          'HOST_RUN_ABANDONED',
          'round ownership was revoked before the late result could be finalized',
          { status: 409, retryable: false, details: { round_id: roundId } },
        )
      }

      await this.persistAiAudit(client, activeCycleId, result, record, runEvents)

      // P1 returns RiskVerdict.as_dict() on approval, or inside the 422 error
      // details on rejection. Persist only the verdict so RoundView can surface
      // violations/blocking_rule; the approval token never leaves this scope.
      const p1Verdict = p1Raw?.verdict ?? p1Error?.details?.verdict ?? null
      record.riskVerdict = p1Verdict && typeof p1Verdict === 'object' ? p1Verdict : null

      const finished = this.now()
      record.finishedAt = nowIso(finished)
      record.durationMs = finished - started
      record.result = result
      record.outcome = result.outcome
      record.reasonCode = result.reasonCode
      // T15: one whitelisted projection for the ledger, the UI and the repair hints.
      // Kept next to reasonCode so "why didn't it execute" travels with the round.
      record.gateAudit = gateAuditOf(result)
      // T15: the pre-submit check is a second, separately-labelled verdict. It is
      // never merged into the chain gate — one ran on the build-time evidence, the
      // other ran right before submitting without a re-quote.
      if (preSubmitGate) {
        record.preSubmitGate = preSubmitGate
        if (preSubmitGate.requote === 'unavailable' && preSubmitGate.advisory_only) {
          record.gaps.push(
            'pre_submit 未重新报价：闸门报价来源仍是 evidence_build；只有时间类判定（VALID_UNTIL_EXPIRED）可拦。需 Service 回包带提交价才能做真正的 pre_submit 报价。',
          )
        }
      }
      const gatewayFailure = p1Error ?? p2Error
      if (gatewayFailure) {
        const stage = p1Error ? 'P1' : 'P2'
        record.status = 'failed'
        record.outcome = 'failed'
        record.reasonCode = gatewayFailure.code
        record.serviceStatus = serviceStatusForError(gatewayFailure.status, gatewayFailure.code)
        record.errorStatus = errorStatus(
          stage,
          gatewayFailure.status,
          gatewayFailure.code,
          gatewayFailure.message,
          gatewayFailure.retryable,
        )
        record.httpStatus = Number.isInteger(gatewayFailure.status) ? gatewayFailure.status : 502
        record.headline = `失败：${gatewayFailure.code}`
        record.error = {
          code: gatewayFailure.code,
          message: gatewayFailure.message,
          status: record.httpStatus,
          retryable: Boolean(gatewayFailure.retryable),
          details: { ...(gatewayFailure.details ?? {}), stage, cycle_id: activeCycleId },
        }
      } else {
        record.headline = result.outcome === 'trade'
          ? '交易计划已通过 P1/P2'
          : result.outcome === 'no_trade'
            ? `无交易：${result.reasonCode}`
            : `失败：${result.reasonCode}`
        record.status = result.outcome === 'trade' ? 'done' : result.outcome === 'no_trade' ? 'no_trade' : 'failed'
        record.error = result.error ?? null
        if (result.outcome === 'failed') {
          record.serviceStatus = 'error'
          record.httpStatus = 502
        } else if (result.reasonCode === 'E_DEMO_CREDENTIALS_MISSING') {
          record.serviceStatus = 'degraded'
        }
      }
      if (p1Error) {
        record.gaps.push(`P1 failed (${p1Error.status ?? 'n/a'} ${p1Error.code}): ${p1Error.message}`)
        if (!cycleCreated) {
          record.gaps.push(
            `No server evidence snapshot for Host cycle ${activeCycleId} (POST /api/cycles unavailable); P1 cannot find md-evidence/1.`,
          )
        } else if (p1Error.code === 'E_EVIDENCE_UNAVAILABLE' || /evidence/i.test(p1Error.message)) {
          record.gaps.push(`P1 has no server md-evidence/1 snapshot for Host cycle ${activeCycleId}.`)
        }
      }
      if (p2Error) {
        record.gaps.push(`P2 failed (${p2Error.status ?? 'n/a'} ${p2Error.code}): ${p2Error.message}`)
      }
      if (!p2Error && result.reasonCode === 'E_DEMO_CREDENTIALS_MISSING') {
        record.gaps.push('P2 was skipped because Demo credentials are not configured; no real order was attempted.')
      }
      if (cycleCreated) {
        await finalizeCycleSafely(client, activeCycleId, {
          outcome: record.outcome,
          reasonCode: record.reasonCode,
          errorCode: record.error?.code ?? null,
        }, record)
      }
      this.lastRunAt = nowIso(finished)
      this.lastError = record.error
      this.portfolioCache = null
      this.store.upsert(record)
      return { ok: record.outcome !== 'failed', round: roundViewOf(record) }
    } catch (error) {
      if (this.abandonedRuns.has(roundId)) {
        // Do not overwrite HOST_RUN_TIMEOUT/HOST_STALE_RUN with a late
        // HOST_RUN_ABANDONED or a provider error.
        this.abandonedRuns.delete(roundId)
        throw error
      }
      const finished = this.now()
      const mapped = error instanceof HostIntegrationError
        ? error
        : hostErrorFromService(error) ?? new HostIntegrationError('HOST_RUN_FAILED', error?.message ?? String(error), { status: 500 })
      record.finishedAt = nowIso(finished)
      record.durationMs = finished - started
      record.status = 'failed'
      record.outcome = 'failed'
      record.reasonCode = mapped.code
      record.headline = `失败：${mapped.code}`
      record.serviceStatus = serviceStatusForError(mapped.status, mapped.code)
      record.errorStatus = errorStatus(mapped.code === 'NO_ADAPTER' ? 'LLM' : 'SERVICE', mapped.status, mapped.code, mapped.message, mapped.retryable)
      record.httpStatus = mapped.status
      record.error = {
        code: mapped.code,
        message: mapped.message,
        status: mapped.status,
        retryable: Boolean(mapped.retryable),
        details: mapped.details ?? {},
      }
      if (cycleCreated) {
        await finalizeCycleSafely(client, activeCycleId, {
          outcome: 'failed',
          reasonCode: mapped.code,
          errorCode: mapped.code,
        }, record)
      }
      this.lastRunAt = nowIso(finished)
      this.lastError = record.error
      this.portfolioCache = null
      this.store.upsert(record)
      const wrapped = mapped instanceof HostIntegrationError
        ? mapped
        : new HostIntegrationError(mapped.code, mapped.message, { status: mapped.status, details: mapped.details })
      wrapped.round = roundViewOf(record)
      throw wrapped
    } finally {
      // Only clear our own slot: a watchdog-freed round may already have been
      // replaced by a newer one, and the old finally must not clobber it.
      try { if (watchdog) clearTimeout(watchdog) } catch { /* best effort */ }
      if (this.inFlight === roundId) this.inFlight = null
      // 01 §5.3: idle means null, never a stale stage. Only clear the status
      // that belongs to this run; an abandoned run may finish after a new run
      // has already populated inFlightStatus.
      if (this.inFlightStatus?.roundId === roundId) this.inFlightStatus = null
      this.abandonedRuns.delete(roundId)
      await this.releaseRunLease(roundId)
    }
  }

  /**
   * Service call with the Host token and the existing hostErrorFromService
   * mapping; never retries and never exposes the token.
   */
  async callService(request) {
    const tokenInfo = this.tokenInfo()
    const client = this.clientWithToken(tokenInfo.token)
    try {
      return await request(client)
    } catch (error) {
      const mapped = hostErrorFromService(error)
      if (mapped) throw mapped
      throw error
    }
  }

  logCommand(action, { requestId, symbol = null, symbols = null, qty = null, reason = null }) {
    const target = symbols && symbols.length > 0 ? symbols.join(',') : (symbol ?? 'all')
    const qtyText = qty ?? 'position'
    const reasonText = reason === null ? 'n/a' : JSON.stringify(reason)
    this.logger?.info?.(
      `[twb] command=${action} request_id=${requestId} actor=local-user target=${target} qty=${qtyText} reason=${reasonText}`,
    )
  }

  async handleCloseCommand(req, res) {
    requireJsonRequest(req)
    const body = await readJsonBody(req, MAX_COMMAND_BODY_BYTES)
    if (body.confirm !== 'CLOSE') {
      throw new HostIntegrationError('E_CONFIRM_REQUIRED', 'confirm must equal "CLOSE"', {
        status: 422,
        details: { expected: 'CLOSE' },
      })
    }
    const symbol = commandSymbol(body.symbol)
    const reason = commandReason(body.reason)
    const qty = commandQty(body.qty)
    const requestId = commandRequestId(body)
    this.logCommand('positions/close', { requestId, symbol, qty, reason })
    const payload = { symbol, reason, request_id: requestId }
    if (qty !== null) payload.qty = qty
    const result = throwIfServiceErrorEnvelope(await this.callService((client) => client.close(payload)))
    sendJson(res, 200, { ok: true, requestId, result })
  }

  async handleRepairCommand(req, res) {
    requireJsonRequest(req)
    const body = await readJsonBody(req, MAX_COMMAND_BODY_BYTES)
    if (body.confirm !== 'REPAIR') {
      throw new HostIntegrationError('E_CONFIRM_REQUIRED', 'confirm must equal "REPAIR"', {
        status: 422,
        details: { expected: 'REPAIR' },
      })
    }
    const symbols = commandSymbols(body.symbols)
    const requestId = commandRequestId(body)
    this.logCommand('protection/repair', { requestId, symbols })
    const result = throwIfServiceErrorEnvelope(
      await this.callService((client) => client.repairProtection({ ...(symbols ? { symbols } : {}), request_id: requestId })),
    )
    sendJson(res, 200, { ok: true, requestId, result })
  }

  reviewDate(raw) {
    const date = raw === undefined || raw === null || raw === ''
      ? dateKeyInShanghai(this.now())
      : String(raw)
    if (!isValidReviewDate(date)) {
      throw new HostIntegrationError('E_REVIEW_DATE_INVALID', 'date must be YYYY-MM-DD', {
        status: 422,
        details: { date: raw ?? null },
      })
    }
    return date
  }

  reviewFilePath(date) {
    return path.join(this.dataDir, 'reviews', `${date}.md`)
  }

  localReviews(limit = 20) {
    const dir = path.join(this.dataDir, 'reviews')
    try {
      return fs.readdirSync(dir)
        .filter((name) => /^\d{4}-\d{2}-\d{2}\.md$/.test(name))
        .map((name) => {
          const file = path.join(dir, name)
          const stat = fs.statSync(file)
          return {
            date: name.slice(0, 10),
            generatedAt: new Date(stat.mtimeMs).toISOString(),
            bytes: stat.size,
            cached: true,
            source: 'local_file',
          }
        })
        .sort((a, b) => String(b.date).localeCompare(String(a.date)))
        .slice(0, Math.max(1, limit))
    } catch (error) {
      if (error?.code !== 'ENOENT') this.logger?.warn?.(`[twb] cannot read local reviews: ${error?.message ?? error}`)
      return []
    }
  }

  async probeRole(role) {
    const key = String(role || '').trim()
    const probeRoles = new Set([...Object.keys(ROLE_LABELS), 'execution_manage'])
    if (!probeRoles.has(key)) {
      throw new HostIntegrationError('E_PROBE_ROLE', `unknown role: ${key}`, { status: 422 })
    }
    if (!this.llm || typeof this.llm.stream !== 'function') {
      throw new HostIntegrationError('NO_ADAPTER', 'No Harness LLM is available to the Host plugin', { status: 503 })
    }
    const route = this.llmConfig?.roleRoutes?.[key] ?? this.llmConfig?.defaultRoute ?? this.route
    if (!route?.provider || !route?.model) {
      throw new HostIntegrationError('NO_ADAPTER', `no model route for role ${key}`, { status: 503 })
    }
    const effort = typeof route.effort === 'string' && route.effort ? route.effort : null
    const prompt = {
      system: 'You are a model-route health probe. Reply with exactly {"ok":true}. Do not call tools.',
      user: `role=${key}`,
      maxTokens: 32,
      temperature: 0,
      effort,
    }
    const started = this.now()
    const result = await streamPrompt({
      llm: this.llm,
      route: { provider: route.provider, model: route.model },
      prompt,
      timeoutMs: 30_000,
      role: key,
      sessionId: `twb-probe-${key}`,
    })
    return {
      ok: true,
      role: key,
      provider: route.provider,
      model: route.model,
      effort,
      configRevision: this.llmConfig?.revision ?? null,
      latencyMs: this.now() - started,
      text: String(result.text || '').trim().slice(0, 200),
      usage: result.usage ?? null,
    }
  }

  async handleLlmProbe(req, res) {
    if (req.method !== 'POST') return methodNotAllowed(res, ['POST'])
    if (!isSameOriginRequest(req, this.ctx)) {
      sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin POST only.' } })
      return
    }
    const body = await readJsonBody(req, MAX_REVIEW_BODY_BYTES)
    try {
      sendJson(res, 200, await this.probeRole(body.role))
    } catch (error) {
      const mapped = error instanceof HostIntegrationError
        ? error
        : new HostIntegrationError('LLM_PROBE_FAILED', error?.message ?? String(error), { status: 502 })
      sendJson(res, mapped.status, {
        ok: false,
        error: { code: mapped.code, message: mapped.message, details: mapped.details ?? {} },
      })
    }
  }

  async handleDailyReview(req, res, requestUrl) {
    if (!isSameOriginRequest(req, this.ctx)) {
      sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin only.' } })
      return
    }
    if (req.method === 'GET') {
      const date = this.reviewDate(requestUrl.searchParams.get('date'))
      const filePath = this.reviewFilePath(date)
      let markdown
      let stat
      try {
        stat = fs.statSync(filePath)
        markdown = fs.readFileSync(filePath, 'utf8')
      } catch (error) {
        if (error?.code === 'ENOENT') {
          throw new HostIntegrationError('E_REVIEW_NOT_GENERATED', `no cached daily review for ${date}`, {
            status: 404,
            details: { date },
          })
        }
        throw error
      }
      sendJson(res, 200, { ok: true, date, markdown, generatedAt: nowIso(stat.mtimeMs), cached: true })
      return
    }
    if (req.method !== 'POST') return methodNotAllowed(res, ['GET', 'POST'])

    const body = await readJsonBody(req, MAX_REVIEW_BODY_BYTES)
    const date = this.reviewDate(body.date)
    if (this.reviewInFlight.has(date)) {
      throw new HostIntegrationError('E_REVIEW_IN_PROGRESS', `a daily review for ${date} is already being generated`, {
        status: 409,
        retryable: true,
        details: { date },
      })
    }
    this.reviewInFlight.add(date)
    const requestId = `rev-${randomUUID().slice(0, 8)}`
    try {
      const state = await this.state()
      const data = buildReviewContext({ state, date })
      this.logger?.info?.(`[twb] daily review request_id=${requestId} date=${date} actor=local-user`)
      // 02 §2.6 / F2: the daily review used effort:null and silently inherited
      // the model default. It now passes the configured (or role-table) effort.
      const reviewRoute = await this.resolveReviewRoute()
      const review = await generateDailyReview({
        llm: this.llm,
        route: { provider: reviewRoute.provider, model: reviewRoute.model },
        effort: reviewRoute.effort,
        date,
        data,
        timeoutMs: this.reviewTimeoutMs,
        sessionId: `twb-review-${date}`,
      })
      const filePath = this.reviewFilePath(date)
      fs.mkdirSync(path.dirname(filePath), { recursive: true })
      const tmp = `${filePath}.${requestId}.tmp`
      try {
        fs.writeFileSync(tmp, review.markdown, 'utf8')
        fs.renameSync(tmp, filePath)
      } catch (error) {
        try { fs.rmSync(tmp, { force: true }) } catch { /* best effort */ }
        throw error
      }
      sendJson(res, 200, {
        ok: true,
        date,
        markdown: review.markdown,
        generatedAt: review.generatedAt,
        model: review.model,
        requestId,
      })
    } catch (error) {
      throw reviewHostError(error, date)
    } finally {
      this.reviewInFlight.delete(date)
    }
  }

  async handle(req, res) {
    if (!isLoopback(req.socket?.remoteAddress)) {
      sendJson(res, 403, { ok: false, error: { code: 'LOOPBACK_ONLY', message: 'Loopback requests only.' } })
      return
    }
    let pathname
    let requestUrl
    try {
      requestUrl = new URL(req.url ?? '/', 'http://localhost')
      pathname = requestUrl.pathname
    } catch {
      sendJson(res, 400, { ok: false, error: { code: 'BAD_URL', message: 'Invalid request URL.' } })
      return
    }

    try {
      if (pathname === RUN_PATH) {
        if (req.method !== 'POST') return methodNotAllowed(res, ['POST'])
        if (!isSameOriginRequest(req, this.ctx)) {
          sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin POST only.' } })
          return
        }
        const body = await readJsonBody(req)
        const result = await this.run({ symbol: body.symbol ?? DEFAULT_SYMBOL, trigger: 'manual' })
        const httpStatus = result.round?.status === 'failed'
          ? (Number.isInteger(result.round?.httpStatus) ? result.round.httpStatus : 502)
          : 200
        sendJson(res, httpStatus, result)
        return
      }
      if (pathname === STATE_PATH) {
        if (req.method !== 'GET') return methodNotAllowed(res, ['GET'])
        const refresh = requestUrl.searchParams.get('refresh')
        const force = refresh === '1' || refresh === 'true'
        sendJson(res, 200, await this.state({ force }))
        return
      }
      if (pathname === ROUNDS_PATH) {
        if (req.method !== 'GET') return methodNotAllowed(res, ['GET'])
        // 15 §4: ?date=YYYY-MM-DD selects one archived day; absent means today.
        let date
        try {
          date = parseRoundDate(requestUrl.searchParams.get('date'), { now: this.now() })
        } catch (error) {
          sendJson(res, error.status ?? 400, {
            ok: false,
            error: { code: error.code ?? 'E_DATE_INVALID', message: error.message },
          })
          return
        }
        sendJson(res, 200, this.listRounds(date))
        return
      }
      if (pathname === ROUNDS_DATES_PATH) {
        if (req.method !== 'GET') return methodNotAllowed(res, ['GET'])
        sendJson(res, 200, {
          ok: true,
          today: archiveDay(this.now()),
          dates: this.store.listDateKeys(),
        })
        return
      }
      if (pathname.startsWith(`${ROUNDS_PATH}/`)) {
        if (req.method !== 'GET') return methodNotAllowed(res, ['GET'])
        let roundId
        try {
          roundId = decodeURIComponent(pathname.slice(ROUNDS_PATH.length + 1))
        } catch {
          sendJson(res, 400, { ok: false, error: { code: 'BAD_ROUND_ID', message: 'Invalid round id encoding.' } })
          return
        }
        if (!roundId || roundId.includes('/')) {
          sendJson(res, 404, { ok: false, error: { code: 'ROUND_NOT_FOUND', message: 'Round not found.' } })
          return
        }
        const hit = this.getRound(roundId)
        if (!hit) {
          sendJson(res, 404, { ok: false, error: { code: 'ROUND_NOT_FOUND', message: 'Round not found.' } })
          return
        }
        sendJson(res, 200, hit)
        return
      }
      if (pathname === MARKET_PRICES_PATH) {
        if (req.method !== 'GET') return methodNotAllowed(res, ['GET'])
        if (!isSameOriginRequest(req, this.ctx)) {
          sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin GET only.' } })
          return
        }
        const symbols = parsePriceSymbols(requestUrl.searchParams.get('symbols'))
        const payload = await this.callService((client) => client.prices(symbols))
        sendJson(res, 200, { ...(payload && typeof payload === 'object' ? payload : {}), ok: true })
        return
      }
      if (pathname === RISK_ENVELOPE_PATH) {
        if (req.method !== 'GET') return methodNotAllowed(res, ['GET'])
        if (!isSameOriginRequest(req, this.ctx)) {
          sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin GET only.' } })
          return
        }
        const payload = await this.callService((client) => client.riskEnvelope())
        sendJson(res, 200, { ...(payload && typeof payload === 'object' ? payload : {}), ok: true })
        return
      }
      if (pathname === OPERATIONS_PATH) {
        if (req.method !== 'GET') return methodNotAllowed(res, ['GET'])
        if (!isSameOriginRequest(req, this.ctx)) return sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED' } })
        const payload = await this.callService((client) => client.operations(100))
        return sendJson(res, 200, { ...payload, ok: true })
      }
      if (pathname === LLM_CATALOG_PATH) {
        if (req.method !== 'GET') return methodNotAllowed(res, ['GET'])
        if (!isSameOriginRequest(req, this.ctx)) {
          return sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin GET only.' } })
        }
        await this.ensureConfigReady()
        const snapshot = await this.catalog.snapshot(requestUrl.searchParams.get('refresh') === '1')
        return sendJson(res, 200, {
          ok: true,
          available: snapshot.available,
          fetchedAt: snapshot.fetchedAt,
          reason: snapshot.reason ?? null,
          groups: snapshot.groups,
          failures: snapshot.failures,
          presets: await this.presetSummaries(),
          config: this.configSummary(),
        })
      }
      if (pathname === LLM_CONFIG_PATH) {
        if (!['GET', 'POST'].includes(req.method)) return methodNotAllowed(res, ['GET', 'POST'])
        if (!isSameOriginRequest(req, this.ctx)) {
          return sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin only.' } })
        }
        if (req.method === 'GET') {
          await this.ensureConfigReady()
          return sendJson(res, 200, { ok: true, config: this.llmConfig, summary: this.configSummary() })
        }
        requireJsonRequest(req)
        const body = await readJsonBody(req, MAX_BODY_BYTES)
        const candidate = body?.config && typeof body.config === 'object' ? body.config : body
        // §3.1 / F2: a stale form must not overwrite a newer value. The client
        // sends the revision it rendered; anything else is a conflict (409), not
        // a silent last-writer-wins save.
        const providedRevision = body?.baseRevision ?? body?.revision ?? null
        if (providedRevision !== null && providedRevision !== undefined) {
          const currentRevision = this.llmConfig?.revision ?? null
          const provided = Number(providedRevision)
          const current = currentRevision === null ? null : Number(currentRevision)
          if (!Number.isFinite(provided) || provided !== current) {
            return sendJson(res, 409, {
              ok: false,
              error: {
                code: 'E_CONFIG_REVISION_CONFLICT',
                message: '配置已被更新：请刷新后重新保存，避免覆盖新值',
                details: { provided: providedRevision, current: currentRevision },
              },
            })
          }
        }
        const saved = await saveConfig({ dataDir: this.dataDir, candidate, catalog: this.catalog, now: () => this.now() })
        if (!saved.ok) {
          return sendJson(res, 422, {
            ok: false,
            error: { code: saved.code ?? 'E_CONFIG_INVALID', message: '配置未保存', details: { errors: saved.errors ?? [] } },
          })
        }
        this.llmConfig = saved.config
        this.configBanner = null
        this.configBannerCode = null
        this.configFixationChecked = true
        setLoadedConfigRevision(saved.config.revision)
        this.lastConfigChange = { revision: saved.config.revision, at: saved.config.updatedAt, actor: 'local-user' }
        this.logger?.info?.(`[twb] llm-config saved revision=${saved.config.revision} preset=${saved.config.preset} actor=local-user`)
        return sendJson(res, 200, { ok: true, config: saved.config, summary: this.configSummary(), effectiveFrom: 'nextRound' })
      }
      if (pathname === LIVE_SETUP_PATH) {
        return this.handleLiveSetup(req, res)
      }
      if (pathname === DEMO_SETUP_PATH) {
        return this.handleDemoSetup(req, res)
      }
      if (pathname === PNL_PATH) {
        if (req.method !== 'GET') return methodNotAllowed(res, ['GET'])
        if (!isSameOriginRequest(req, this.ctx)) {
          return sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin GET only.' } })
        }
        const rangeS = parsePnlRange(requestUrl.searchParams.get('range_s'))
        let payload
        try {
          payload = await this.callService((client) => client.request(`/api/pnl?range_s=${rangeS}`))
        } catch (error) {
          // A missing/old service route is a *known unknown*, not a zero balance.
          const code = error instanceof ServiceClientError ? error.code : (error?.code ?? 'E_SERVICE_UNAVAILABLE')
          const status = error instanceof ServiceClientError ? error.status : (error?.status ?? 502)
          return sendJson(res, 200, {
            ok: false,
            source: 'service:/api/pnl',
            fetched_at: nowIso(this.now()),
            range_s: rangeS,
            error: {
              code,
              message: error?.message ?? String(error),
              status: Number.isInteger(status) ? status : null,
            },
          })
        }
        const body = payload && typeof payload === 'object' ? payload : {}
        return sendJson(res, 200, {
          ...body,
          ok: true,
          source: 'service:/api/pnl',
          fetched_at: nowIso(this.now()),
          range_s: Number.isFinite(Number(body.range_s)) ? Number(body.range_s) : rangeS,
        })
      }
      if (pathname === HOST_INFO_PATH) {
        if (req.method !== 'GET') return methodNotAllowed(res, ['GET'])
        if (!isSameOriginRequest(req, this.ctx)) {
          return sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin GET only.' } })
        }
        return sendJson(res, 200, this.hostInfo())
      }
      if (pathname === AUTOMATION_STOP_PATH) {
        await this.handleAutomationStop(req, res)
        return
      }
      if (pathname === AUTOMATION_START_PATH) {
        await this.handleAutomationStart(req, res)
        return
      }
      if (pathname === SETUP_READINESS_PATH) {
        // 16 §1: what this machine actually has. Read-only, no side effects —
        // the wizard asks before it offers to install anything.
        if (req.method !== 'GET') return methodNotAllowed(res, ['GET'])
        const force = requestUrl.searchParams.get('force') === '1'
        sendJson(res, 200, { ok: true, setup: await this.serviceSetup.readiness({ force }) })
        return
      }
      if (pathname === SERVICE_SETUP_PATH) {
        if (req.method !== 'POST') return methodNotAllowed(res, ['POST'])
        if (!isSameOriginRequest(req, this.ctx)) return sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED' } })
        requireJsonRequest(req)
        await readJsonBody(req, MAX_COMMAND_BODY_BYTES)
        const { job, reused } = this.serviceSetup.start()
        this.logCommand('service/setup', { job_id: job.id, reused })
        sendJson(res, 200, { ok: true, job_id: job.id, reused, job: serializeSetupJob(job) })
        return
      }
      if (pathname.startsWith(`${SERVICE_SETUP_PATH}/`)) {
        if (req.method !== 'GET') return methodNotAllowed(res, ['GET'])
        let jobId = ''
        try {
          jobId = decodeURIComponent(pathname.slice(SERVICE_SETUP_PATH.length + 1))
        } catch {
          jobId = ''
        }
        const job = this.serviceSetup.status(jobId)
        if (!job) {
          sendJson(res, 404, { ok: false, error: { code: 'E_SETUP_JOB_UNKNOWN', message: 'no setup job with that id' } })
          return
        }
        sendJson(res, 200, { ok: true, job: serializeSetupJob(job) })
        return
      }
      if (pathname === SERVICE_START_PATH) {
        // 15 §10: local-only, same-origin-aware, JSON body required. Like the
        // automation routes it is an operator action, so it is audited.
        if (req.method !== 'POST') return methodNotAllowed(res, ['POST'])
        if (!isSameOriginRequest(req, this.ctx)) return sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED' } })
        requireJsonRequest(req)
        await readJsonBody(req, MAX_COMMAND_BODY_BYTES)
        const result = await this.startService()
        this.logCommand('service/start', { pid: result?.pid ?? null, ok: result?.ok === true })
        sendJson(res, 200, result)
        return
      }
      if (pathname === CONTROL_PATH) {
        if (req.method !== 'POST') return methodNotAllowed(res, ['POST'])
        if (!isSameOriginRequest(req, this.ctx)) return sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED' } })
        requireJsonRequest(req)
        const body = await readJsonBody(req, MAX_COMMAND_BODY_BYTES)
        if (!['normal', 'pause', 'close_only', 'emergency'].includes(body.state) || body.confirm !== body.state.toUpperCase()) {
          throw new HostIntegrationError('E_CONTROL_CONFIRM_REQUIRED', 'state and uppercase confirmation must match', { status: 422 })
        }
        const reason = commandReason(body.reason)
        const requestId = commandRequestId(body)
        const result = throwIfServiceErrorEnvelope(await this.callService((client) => client.control({ state: body.state, confirm: body.confirm, reason, request_id: requestId })))
        return sendJson(res, 200, { ok: true, requestId, result })
      }
      if (pathname.startsWith(`${CYCLE_EVIDENCE_PATH}/`) && pathname.endsWith(CYCLE_EVIDENCE_SUFFIX)) {
        if (req.method !== 'GET') return methodNotAllowed(res, ['GET'])
        if (!isSameOriginRequest(req, this.ctx)) {
          sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin GET only.' } })
          return
        }
        let cycleId
        try {
          cycleId = decodeURIComponent(
            pathname.slice(CYCLE_EVIDENCE_PATH.length + 1, -CYCLE_EVIDENCE_SUFFIX.length),
          )
        } catch {
          sendJson(res, 400, { ok: false, error: { code: 'BAD_CYCLE_ID', message: 'Invalid cycle id encoding.' } })
          return
        }
        if (!CYCLE_ID_RE.test(cycleId)) {
          sendJson(res, 400, { ok: false, error: { code: 'E_CYCLE_ID_INVALID', message: 'Invalid cycle id.' } })
          return
        }
        const agent = requestUrl.searchParams.get('agent')
        const payload = await this.callService((client) => client.cycleEvidence(cycleId, agent || null))
        sendJson(res, 200, { ...(payload && typeof payload === 'object' ? payload : {}), ok: true })
        return
      }
      if (pathname === RECONCILE_PATH) {        if (req.method !== 'POST') return methodNotAllowed(res, ['POST'])
        if (!isSameOriginRequest(req, this.ctx)) {
          sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin POST only.' } })
          return
        }
        const contentType = req.headers['content-type']
        if (typeof contentType === 'string' && contentType.length > 0 && !/^application\/json\b/i.test(contentType)) {
          sendJson(res, 415, { ok: false, error: { code: 'E_CONTENT_TYPE_JSON_REQUIRED', message: 'content-type must be application/json' } })
          return
        }
        const body = await readJsonBody(req, MAX_COMMAND_BODY_BYTES)
        const requestId = commandRequestId(body)
        this.logCommand('execution/reconcile', { requestId })
        const result = await this.callService((client) => client.reconcile({ request_id: requestId }))
        sendJson(res, 200, { ok: true, requestId, result })
        return
      }
      if (pathname === LLM_PROBE_PATH) {
        await this.handleLlmProbe(req, res)
        return
      }
      if (pathname === REVIEWS_DAILY_PATH) {
        await this.handleDailyReview(req, res, requestUrl)
        return
      }
      if (pathname === CLOSE_PATH) {
        if (req.method !== 'POST') return methodNotAllowed(res, ['POST'])
        if (!isSameOriginRequest(req, this.ctx)) {
          sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin POST only.' } })
          return
        }
        await this.handleCloseCommand(req, res)
        return
      }
      if (pathname === REPAIR_PATH) {
        if (req.method !== 'POST') return methodNotAllowed(res, ['POST'])
        if (!isSameOriginRequest(req, this.ctx)) {
          sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin POST only.' } })
          return
        }
        await this.handleRepairCommand(req, res)
        return
      }
      sendJson(res, 404, { ok: false, error: { code: 'NOT_FOUND', message: 'Not found.' } })
    } catch (error) {
      const mapped = error instanceof HostIntegrationError
        ? error
        : new HostIntegrationError('HOST_ERROR', error?.message ?? String(error), { status: 500 })
      sendJson(res, mapped.status, {
        ok: false,
        error: {
          code: mapped.code,
          message: mapped.message,
          retryable: mapped.retryable,
          details: mapped.details,
        },
        round: mapped.round ?? undefined,
      })
    }
  }

  routes() {
    return [
      { kind: 'exact', path: RUN_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: STATE_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: ROUNDS_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'prefix', path: ROUNDS_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: MARKET_PRICES_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: RISK_ENVELOPE_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: CONTROL_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: AUTOMATION_STOP_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: AUTOMATION_START_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: SERVICE_START_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: SETUP_READINESS_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: SERVICE_SETUP_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'prefix', path: SERVICE_SETUP_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: HOST_INFO_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: OPERATIONS_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: PNL_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: LLM_CATALOG_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: LLM_CONFIG_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: CLOSE_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: REPAIR_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: RECONCILE_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: LLM_PROBE_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'exact', path: REVIEWS_DAILY_PATH, handler: (req, res) => this.handle(req, res) },
      { kind: 'prefix', path: CYCLE_EVIDENCE_PATH, handler: (req, res) => this.handle(req, res) },
    ]
  }
}

export function createIntegration(options = {}) {
  return new WorkbenchHostIntegration(options)
}

export function createIntegrationRoutes(integration) {
  return integration.routes()
}

export function isLoopback(address) {
  if (typeof address !== 'string') return false
  return address === '::1'
    || address === '::ffff:127.0.0.1'
    || address.startsWith('127.')
    || address.startsWith('::ffff:127.')
}

function isSameOriginRequest(req, ctx) {
  const site = req.headers['sec-fetch-site']
  if (typeof site === 'string' && site !== 'same-origin' && site !== 'none') return false
  const origin = req.headers.origin
  if (typeof origin === 'string' && origin.length > 0) {
    try {
      if (!ctx?.webServer || origin !== ctx.webServer.getOrigin()) return false
    } catch {
      return false
    }
  }
  return true
}

function sendJson(res, status, body) {
  const payload = JSON.stringify(body)
  res.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'cache-control': 'no-store',
    'content-length': Buffer.byteLength(payload),
  })
  res.end(payload)
}

function methodNotAllowed(res, allowed) {
  const payload = JSON.stringify({ ok: false, error: { code: 'METHOD_NOT_ALLOWED', message: `Allowed: ${allowed.join(', ')}` } })
  res.writeHead(405, {
    allow: allowed.join(', '),
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(payload),
  })
  res.end(payload)
}

function readJsonBody(req, limit = MAX_BODY_BYTES) {
  return new Promise((resolve, reject) => {
    let size = 0
    let settled = false
    const chunks = []
    req.on('data', (chunk) => {
      if (settled) return
      size += chunk.length
      if (size > limit) {
        settled = true
        reject(new HostIntegrationError('BODY_TOO_LARGE', 'request body is too large', { status: 413 }))
        // Drain instead of destroying the socket so the 413 response can reach
        // the caller; the body is ignored once the limit is exceeded.
        req.resume()
        return
      }
      chunks.push(chunk)
    })
    req.on('end', () => {
      if (settled) return
      settled = true
      const text = Buffer.concat(chunks).toString('utf8').trim()
      if (!text) return resolve({})
      try {
        const parsed = JSON.parse(text)
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          return reject(new HostIntegrationError('BAD_BODY', 'request body must be a JSON object', { status: 400 }))
        }
        resolve(parsed)
      } catch {
        reject(new HostIntegrationError('BAD_BODY', 'request body is not valid JSON', { status: 400 }))
      }
    })
    req.on('error', (error) => {
      if (settled) return
      settled = true
      reject(error)
    })
  })
}
