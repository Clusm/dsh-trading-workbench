/**
 * Render smoke test for the M1.4 workbench panel (t47 repair round).
 *
 * Usage: node trading-workbench/plugin/client/render-smoke.mjs
 *
 * No browser is available here, so this harness supplies a tiny hooks runtime
 * and walks the element tree the panel returns. It loads the real client.js and
 * drives the real components, view tabs, evidence drawer, run button and refresh
 * button through their own handlers against Host-shaped payloads.
 *
 * The fixtures follow the Host exactly:
 *  - `state.service` keeps only base_url/token_* plus the older summary in
 *    `last_run` (the shape t45 reviewed), and a second scenario uses the t46
 *    stable block with every field at the top level;
 *  - HEALTH repeats the same nesting;
 *  - equityCurve/positions/orders/fills/reviews/account are the t46 portfolio
 *    fields.
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { toRoundView } from '../host/round-view.js'

const CLIENT_PATH = fileURLToPath(new URL('../client.js', import.meta.url))
const SOURCE = readFileSync(CLIENT_PATH, 'utf8')

// ---- Host service summaries ------------------------------------------------
const SERVICE_SUMMARY = {
  base_url: 'http://127.0.0.1:8791',
  token_present: true,
  token_source: 'file',
  auth_mode: 'bearer',
  ready: true,
  demo_only: true,
  ledger_id: '6512827bcdfe4250a1bb5710217e044d',
  control_state: 'normal',
  credentials_configured: false,
  new_entry_allowed: false,
  checked_at: '2026-11-09T04:49:42.514Z',
}
// Pre-t46 shape: only transport fields at the top, summary nested in last_run.
const SERVICE_NESTED = {
  base_url: SERVICE_SUMMARY.base_url,
  token_present: true,
  token_source: 'file',
  token_error: null,
  last_run: SERVICE_SUMMARY,
}
// t46 stable block: every field at the top level, last_run kept for history.
const SERVICE_STABLE = {
  ...SERVICE_SUMMARY,
  token_configured: true,
  token_present: true,
  state: 'online',
  last_run: SERVICE_SUMMARY,
}

// ---- rounds ----------------------------------------------------------------
function node(roleKey, round, attempt, latency, inputTokens, outputTokens) {
  return {
    id: `run-x:${roleKey}:r${round}:${attempt}`,
    role: roleKey,
    role_key: roleKey,
    label: roleKey,
    stage: roleKey,
    round,
    attempt,
    status: 'ok',
    schema_ok: true,
    repaired: attempt > 1,
    latency_ms: latency,
    input_tokens: inputTokens,
    output_tokens: outputTokens,
    finish_kind: 'stop',
    error: null,
  }
}

const ROUND = {
  roundId: 'cyc-20261109T044942Z-06c625',
  cycleId: 'cyc-20261109T044943Z-cb5a4d',
  trigger: 'manual',
  status: 'done',
  outcome: 'trade',
  reasonCode: null,
  headline: '交易：做多 BTCUSDT',
  startedAt: '2026-11-09T04:49:42.508Z',
  finishedAt: '2026-11-09T04:51:02.508Z',
  durationMs: 80000,
  symbol: 'BTCUSDT',
  mode: 'full_chain',
  debateMode: 'symmetric_two_round',
  degraded: false,
  freshness: {
    snapshot_id: 'host-cyc-20261109T044943Z-cb5a4d',
    produced_at: new Date(Date.now() - 9000).toISOString(),
    age_ms: 9000,
    data_verdict: 'GOOD',
    source: 'host-composed:/api/market/prices',
  },
  decision: {
    action: 'trade', selected_case: 'red', symbol: 'BTCUSDT', side: 'long',
    confidence: 0.62, score_override: null, no_trade_reason: null,
    thesis: '15m/1h 上行突破回踩，量能配合，4h 仍处区间上沿之下。',
    case_comparison: {
      red: { strength: 'moderate', decisive_evidence: 'taker buy 0.83 且站上 EMA20', fatal_flaw: '4h 下行未破' },
      blue: { strength: 'weak', decisive_evidence: '区间上沿压力', fatal_flaw: 'RR 1.02 不足' },
      decisive_factor: '红队 RR 1.66 且触发器已确认',
      opposing_risk: '假突破回落',
    },
    plan: {
      entry_style: 'MARKET', entry_price: 68234.5, stop_loss: 67650, take_profit: 69400,
      risk_pct: 0.8, leverage: 3, rr: 1.66, time_stop_hours: 24,
    },
    invalidation: { price_event: '15m 收于 67650 下方', time: '24h 时间止损' },
    warnings: ['衍生品数据缺失'],
  },
  scorecard: {
    grade: 'A', gate: 'PASS', gate_reasons: [], debate_quality: 'thin', tie: false, degraded: false,
    red: {
      grade: 'A', gate: 'PASS', gate_reasons: [],
      rr: 1.66, stop_distance_pct: 0.86, confidence: 0.62,
      opening_confidence: 0.62, response_confidence: 0.64,
      rubric_score: 0.84, evidence_strength: 0.9, risk_pct: 0.8, leverage: 3, rebuttal_count: 2,
    },
    blue: {
      grade: 'C', gate: 'FAIL_RR', gate_reasons: ['RR_TOO_LOW'],
      rr: 1.02, stop_distance_pct: 1.2, confidence: 0.48, rubric_score: 0.6, evidence_strength: 0.7,
    },
  },
  chain: {
    calls: 8, retries: 3, repaired: 3, truncated: 1, timeouts: 0, invalid: 1,
    tokens_in: 51700, tokens_out: 9800, elapsed_ms: 80000, deadline_ms: 250000,
    slowest: { role_key: 'analysis', latency_ms: 29666 }, stopped_at: null,
  },
  risk: {
    recommendation: 'APPROVE', size_multiplier: 1, max_risk_pct: 1, max_leverage: 10, concerns: [],
    violations: [], blocking_rule: null,
  },
  execution: {
    executable: true, blocked_reason: null, symbol: 'BTCUSDT', side: 'long',
    order_plan: {
      symbol: 'BTCUSDT', side: 'long', action: 'open', entry_type: 'MARKET',
      leverage: 3, qty: '0.012', sl: '67650.0', tp: '69400.0', confidence: 0.62,
      reasoning_ref: 'ai-chain:cyc-20261109T044943Z-cb5a4d',
    },
    rationale: 'breakout continuation',
    p1: {
      verdict: 'reduced', code: null, detail: null, approved_qty: '0.010', leverage: 3,
      approval: { jti: 'jti-1', plan_hash: 'sha256:abc', approved_qty: '0.010', expires_at_ms: Date.now() + 120000 },
    },
    p2: { ok: true, code: null, detail: null, status: 200, order_id: '3910022', client_order_id: 'tw-cc0ffee1' },
  },
  nodes: [
    node('analysis', 1, 1, 9412, 1824, 1717),
    node('red_opening', 2, 1, 8300, 2100, 900),
    node('blue_opening', 2, 1, 7900, 2050, 880),
    node('red_response', 2, 2, 6100, 2600, 800),
    node('blue_response', 2, 2, 6000, 2580, 790),
    node('decision', 3, 1, 4300, 3200, 500),
    node('risk', 4, 1, 3900, 2900, 420),
    node('execution_entry', 5, 1, 2200, 1800, 380),
  ],
  aiCalls: [],
  evidence: {
    snapshot_id: 'host-cyc-20261109T044943Z-cb5a4d',
    cycle_id: 'cyc-20261109T044943Z-cb5a4d',
    evidence_hash: 'sha256:c44cf53565ad992318c3c67d8d788d40a7f3ac83673b5378cc13b21be44efcdd',
    age_ms: 9000,
    produced_at: new Date(Date.now() - 9000).toISOString(),
    primary_symbol: 'BTCUSDT',
    verdict: 'GOOD',
  },
  gaps: ['No Demo API key means P1/P2 cannot sign an order in this session.'],
  service: SERVICE_SUMMARY,
  error: null,
  cycleSource: 'service',
  serviceStatus: 'ok',
  errorStatus: null,
  httpStatus: null,
}
ROUND.aiCalls = ROUND.nodes.map((item, index) => ({
  call_id: item.id,
  role: item.role,
  role_key: item.role_key,
  round: item.round,
  attempt: item.attempt,
  provider: 'opencode-go',
  model: 'deepseek-v4.1-flash',
  effort: 'high',
  prompt_version: `${item.role_key}.v1`,
  output_schema_version: 'analysis/1',
  latency_ms: item.latency_ms,
  input_tokens: item.input_tokens,
  output_tokens: item.output_tokens,
  total_tokens: item.input_tokens + item.output_tokens,
  finish_kind: 'stop',
  schema_ok: true,
  validation_errors: [],
  request_sha256: `sha256:req${index}`,
  response_sha256: `sha256:res${index}`,
}))

const NO_TRADE = {
  ...ROUND,
  roundId: 'cyc-20261109T054411Z-a9e545',
  cycleId: 'cyc-20261109T054415Z-d4980c',
  status: 'no_trade',
  outcome: 'no_trade',
  reasonCode: 'NO_CANDIDATE',
  headline: '无交易：NO_CANDIDATE',
  durationMs: 30015,
  decision: null,
  risk: null,
  execution: null,
  nodes: [node('analysis', 1, 1, 20348, 1819, 2524)],
  gaps: ['Demo credentials are not configured; P1/P2 cannot be attempted as a real order.'],
}
NO_TRADE.aiCalls = [{ ...ROUND.aiCalls[0], call_id: NO_TRADE.nodes[0].id, role: 'analysis', role_key: 'analysis', round: 1, attempt: 1 }]

const ROUND_SUMMARY = {
  roundId: ROUND.roundId, cycleId: ROUND.cycleId, trigger: ROUND.trigger, status: ROUND.status,
  outcome: ROUND.outcome, reasonCode: ROUND.reasonCode, headline: ROUND.headline,
  startedAt: ROUND.startedAt, finishedAt: ROUND.finishedAt, durationMs: ROUND.durationMs,
  symbol: ROUND.symbol, mode: ROUND.mode, debateMode: ROUND.debateMode, degraded: ROUND.degraded,
  serviceStatus: 'ok',
}
const NO_TRADE_SUMMARY = { ...ROUND_SUMMARY, roundId: NO_TRADE.roundId, status: 'no_trade', outcome: 'no_trade', reasonCode: 'NO_CANDIDATE', headline: NO_TRADE.headline, durationMs: NO_TRADE.durationMs }
const OLDER = { ...ROUND_SUMMARY, roundId: 'cyc-20261109T044518Z-f5d1dc', status: 'no_trade', outcome: 'no_trade', reasonCode: 'NO_CANDIDATE', headline: '无交易：NO_CANDIDATE', startedAt: '2026-11-09T04:45:18.000Z' }
const ROUNDS_BY_ID = { [ROUND.roundId]: ROUND, [NO_TRADE.roundId]: NO_TRADE }

// ---- t46 portfolio fields --------------------------------------------------
const EQUITY_CURVE = {
  range: 'all',
  points: [
    { ts: '2026-11-09T03:00:00.000Z', session: 10000, exchange: 10000, available: 10000, upnl: 0, margin_used: 0 },
    { ts: '2026-11-09T04:00:00.000Z', session: 10420.4, exchange: 10418.1, available: 9800.2, upnl: 12.1, margin_used: 620 },
    { ts: '2026-11-09T05:00:00.000Z', session: 10842.31, exchange: 10844.02, available: 9412.6, upnl: 41.2, margin_used: 720 },
  ],
  count: 3,
  source: 'service',
  gap: null,
}
const POSITIONS = [
  {
    key: 'BTCUSDT:long:0', symbol: 'BTCUSDT', side: 'long', qty: '0.012',
    entry: 68120.5, mark: 68402.0, upnl: 3.38, sl: 67650.0, tp: 69400.0,
    protection: 'OK', protection_ok: true, origin: 'ai', leverage: 3, margin: 273.6,
    margin_mode: 'cross', liq_price: 61200.0, stale: false,
  },
  {
    key: 'ETHUSDT:short:1', symbol: 'ETHUSDT', side: 'short', qty: '0.4',
    entry: 3288.1, mark: 3291.4, upnl: -1.32, sl: 3330.0, tp: 3180.0,
    protection: 'MISSING', protection_ok: false, origin: 'external', leverage: 2, margin: 0,
    margin_mode: 'cross', liq_price: 0, stale: true,
  },
]
const ORDERS = [{
  orderId: '3910022', clientOrderId: 'tw-cc0ffee1', symbol: 'BTCUSDT', side: 'long',
  action: 'open', qty: '0.012', type: 'MARKET', status: 'FILLED', price: 68121.0,
  createdAt: '2026-11-09T04:50:03.000Z',
}]
const FILLS = [{
  fillId: 'fill-1', orderId: '3910022', clientOrderId: 'tw-cc0ffee1', symbol: 'BTCUSDT',
  side: 'long', qty: '0.012', price: 68121.0, realizedPnl: 12.4, commission: 0.41,
  tradeTime: '2026-11-09T04:50:04.000Z',
}]
const REVIEWS = [{
  review_id: 'rv-1', date: '2026-11-09', summary: 'BTC breakout long, protection held',
  markdown: '## review', generated_at: '2026-11-09T15:55:00.000Z',
}]
const ACCOUNT = {
  equity: 10844.02, available: 9412.6, upnl: 41.2, day_pnl: 184.32,
  equity_session: 10842.31, equity_exchange: 10844.02, empty: false,
}
const ACCOUNT_EMPTY = {
  empty: true, gap: 'E_ACCOUNT_EMPTY', empty_reason: 'DEMO_CREDENTIALS_MISSING', source: 'equity',
}
const PORTFOLIO = {
  fetched_at: '2026-11-09T05:00:00.000Z', source: 'service', cached: false,
  cache_hit: false, cache_age_ms: 0, gaps: [],
}
const PORTFOLIO_EMPTY_GAPS = {
  ...PORTFOLIO,
  gaps: [
    'positions empty (E_POSITION_EMPTY)',
    'orders empty (E_ORDER_EMPTY)',
    'fills empty (E_FILL_EMPTY)',
    'reviews unavailable (E_NOT_FOUND HTTP 404)',
  ],
}
const BUDGET_EMPTY = { gap: 'E_BUDGET_EMPTY', empty_reason: 'BUDGET_NOT_CONFIGURED' }
const EQUITY_EMPTY = {
  range: 'all', points: [], count: 0, session: null, exchange: null, latest: null,
  empty: true, empty_reason: 'NO_EQUITY_POINTS', source: 'service', gap: 'E_EQUITY_CURVE_EMPTY',
}

const HEALTH = {
  ok: true,
  plugin: '@local/dsh-trading-workbench',
  version: '0.1.0',
  contract: 'trading-workbench/m1.0',
  llm: { route_selected: true, routeSelected: true, provider: 'opencode-go', model: 'deepseek-v4.1-flash', lastProbe: { status: 'success', at: '2026-11-09T04:00:00.000Z', latencyMs: 1200 } },
  service: { ...SERVICE_NESTED, state: 'connected', detail: 'M1.3b service snapshot is available; see /state for the last round.' },
}

// ---- 05 P1-1: model catalog + plugin-local config fixtures -----------------
const CONFIG_GROUPS = [{
  id: 'qoder', name: 'qoder',
  models: [
    { id: 'dfmodel', name: 'DeepSeek-Flash', efforts: ['off', 'low', 'high', 'max'], defaultEffort: 'high', contextWindow: 200000 },
    { id: 'qmodel_latest', name: 'Qwen4-Max-Preview', efforts: ['off', 'low', 'high', 'xhigh'], defaultEffort: 'xhigh', contextWindow: 200000 },
  ],
}]
const CONFIG_ROLE_ROUTES = {
  analysis: { provider: 'qoder', model: 'dfmodel', effort: 'high' },
  red_opening: { provider: 'qoder', model: 'dfmodel', effort: 'high' },
  blue_opening: { provider: 'qoder', model: 'dfmodel', effort: 'high' },
  decision: { provider: 'qoder', model: 'dfmodel', effort: 'max' },
  risk: { provider: 'qoder', model: 'dfmodel', effort: 'max' },
  execution_entry: { provider: 'qoder', model: 'dfmodel', effort: 'low' },
  execution_manage: { provider: 'qoder', model: 'dfmodel', effort: 'low' },
  review: { provider: 'qoder', model: 'dfmodel', effort: 'high' },
}
const CONFIG_FILE_FIXTURE = {
  schema: 'twb-llm-config/1',
  revision: 1,
  updatedAt: '2026-11-12T05:00:00.000Z',
  defaultRoute: { provider: 'qoder', model: 'dfmodel', effort: 'high', source: 'explicit' },
  roleRoutes: CONFIG_ROLE_ROUTES,
  debateAsymmetric: false,
  fallback: { mode: 'fail' },
  preset: 'balanced',
}
const CONFIG_BUDGET = { serial_ms: 310000, deadline_ms: 340000, min_ms: 310000, max_ms: 340000, retry_margin_ms: 30000 }
const CONFIG_SUMMARY_FIXTURE = {
  revision: 1,
  schema: 'twb-llm-config/1',
  preset: 'balanced',
  source: 'explicit',
  updatedAt: CONFIG_FILE_FIXTURE.updatedAt,
  default: CONFIG_FILE_FIXTURE.defaultRoute,
  roleRoutes: CONFIG_ROLE_ROUTES,
  debateAsymmetric: false,
  fallback: { mode: 'fail' },
  banner: null,
  budget: CONFIG_BUDGET,
  catalog: { available: true },
}
const CONFIG_PRESETS_FIXTURE = {
  balanced: { label: '平衡', missing: [], config: CONFIG_FILE_FIXTURE },
  saver: {
    label: '省钱', missing: [],
    config: { ...CONFIG_FILE_FIXTURE, preset: 'saver', roleRoutes: Object.fromEntries(Object.entries(CONFIG_ROLE_ROUTES).map(([role, route]) => [role, { ...route, effort: role === 'decision' || role === 'risk' ? 'high' : 'low' }])) },
  },
  strongest: {
    label: '最强', missing: [],
    config: {
      ...CONFIG_FILE_FIXTURE,
      preset: 'strongest',
      defaultRoute: { provider: 'qoder', model: 'qmodel_latest', effort: 'high', source: 'explicit' },
      roleRoutes: { ...CONFIG_ROLE_ROUTES, analysis: { provider: 'qoder', model: 'qmodel_latest', effort: 'high' }, decision: { provider: 'qoder', model: 'qmodel_latest', effort: 'xhigh' }, risk: { provider: 'qoder', model: 'qmodel_latest', effort: 'xhigh' } },
    },
  },
}

// t18 read-only routes (prices / risk envelope)
const PRICES = {
  ok: true,
  as_of_ms: Date.now() - 1500,
  prices: {
    BTCUSDT: { last: 68402.5, mark: 68401, funding_rate: 0.0001, spread_bps: 0.2, age_ms: 1500 },
  },
}
// Real /api/risk/envelope shape (risk/service.py as_dict): the authorization
// block carries the authoritative effective caps. Ratios are ratios (0.01 = 1%).
const ENVELOPE = {
  ok: true, demo_only: true, ledger_id: '6512827bcdfe4250a1bb5710217e044d',
  limits: {
    per_trade: {
      max_risk_pct: 0.01, max_leverage: 10, max_notional_multiple: 2.0,
      strong_max_risk_pct: 0.02, strong_max_leverage: 15,
    },
    portfolio: { max_margin_usage: 0.6, max_open_positions: 1 },
    loss: { daily_loss_stop: 0.03, max_drawdown_from_peak: 0.08, consecutive_losses: 2 },
  },
  authorization: {
    execution_authorization_fields: ['max_risk_pct', 'max_leverage', 'max_notional_multiple', 'max_margin_usage', 'max_open_positions'],
    effective_caps: { max_risk_pct: 0.01, max_leverage: 10, max_notional_multiple: 2.0, max_margin_usage: 0.6, max_open_positions: 1 },
    authorization_model: 'recomputed_at_p1_and_p2',
    approval_token_is_authorization_source: false,
  },
  sizing_policy: { policy: 'deterministic', model_qty_role: 'upper_bound_only' },
  legacy_strong_tier: {
    fields: ['strong_max_risk_pct', 'strong_max_leverage', 'strong_min_confidence', 'strong_min_rr'],
    used_for_execution_authorization: false,
    confidence_used_for_sizing: false,
  },
  control: { state: 'normal' },
  new_entry_allowed: true,
  blocking_reasons: [],
}

// F2 / C DTO: /api/pnl answer shape (trading-workbench/api/account.py + store.py).
const PNL = {
  ok: true,
  source: 'service:/api/pnl',
  venue: 'demo',
  demo_only: true,
  ledger_id: '6512827bcdfe4250a1bb5710217e044d',
  range_s: 86400,
  calibers: {
    wallet_balance: '10412.44',
    unrealized_pnl: '41.20',
    margin_equity: '10453.64',
    available_balance: '9412.60',
    source: 'exchange',
    age_ms: 1200,
  },
  caliber_note: 'wallet_balance=交易所钱包余额；margin_equity=保证金权益（含未实现盈亏）',
  strategy: {
    window: { start_ms: 1762000000000, end_ms: 1762086400000, start: '2026-11-02T00:00:00Z', end: '2026-11-03T00:00:00Z' },
    fill_count: 3,
    flow_count: 2,
    realized_pnl: '18.40000000',
    commissions: { USDT: '0.41000000' },
    commission_quote: '0.41000000',
    funding_pnl: '-0.12000000',
    funding_by_asset: { USDT: '-0.12000000' },
    transfers: { in: { USDT: '500.00000000' }, out: {}, net_quote: '500.00000000', count: 1 },
    funding_known: true,
    net_strategy_pnl: '17.87000000',
    net_strategy_pnl_includes: ['realized_pnl', '-commission', '+funding'],
    excludes: ['transfers'],
    unknown: [],
    coverage: {
      fills: { complete: true, covered_from_ms: 1762000000000 },
      income: { complete: true, covered_from_ms: 1762000000000 },
      covered_from_ms: 1762000000000,
      complete: true,
      note: null,
    },
  },
  reconcile: {
    status: 'partial',
    checked_at: '2026-11-14T02:00:00.000Z',
    last_success_at: '2026-11-13T22:00:00.000Z',
    tolerance: '0.01',
    exchange_delta: '53.64000000',
    expected_delta: '517.87000000',
    diff: '-464.23000000',
    reasons: ['差额 -464.23000000 超出容差 0.01'],
    window: { start: '2026-11-02T00:00:00Z', end: '2026-11-03T00:00:00Z' },
    coverage: { complete: true, note: null, covered_from_ms: 1762000000000 },
  },
  schema: {
    declared_schema_version: '1',
    recorded_ledger_schema_version: '3',
    expected_ledger_schema_version: 3,
    missing_structures: [],
    consistent: true,
    reason: null,
  },
}

// The same DTO with funding not backfilled: net PnL is null, never 0.
const PNL_UNKNOWN = {
  ...PNL,
  calibers: { wallet_balance: null, unrealized_pnl: null, margin_equity: null, available_balance: null, source: 'none', age_ms: null },
  strategy: {
    ...PNL.strategy,
    funding_known: false,
    funding_pnl: '0',
    net_strategy_pnl: null,
    unknown: ['资金费未回填（income 流未标记完成），不能按 0 计入'],
    coverage: {
      fills: { complete: true, covered_from_ms: 1762000000000 },
      income: { complete: false, covered_from_ms: null },
      covered_from_ms: 1762000000000,
      complete: false,
      note: '统计覆盖不完整：资金费/划转可能未回填，历史不代表完整账户历史',
    },
  },
  reconcile: {
    ...PNL.reconcile,
    status: 'unknown',
    exchange_delta: null,
    expected_delta: null,
    diff: null,
    reasons: ['账本净盈亏不完整：资金费未回填（income 流未标记完成），不能按 0 计入'],
  },
}

function shanghaiToday() {
  try {
    return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date())
  } catch {
    return new Date().toISOString().slice(0, 10)
  }
}

function makeState({
  service = SERVICE_NESTED,
  lastRound = ROUND,
  rounds = [ROUND_SUMMARY, NO_TRADE_SUMMARY, OLDER],
  equityCurve = EQUITY_CURVE,
  positions = POSITIONS,
  orders = ORDERS,
  fills = FILLS,
  reviews = REVIEWS,
  account = ACCOUNT,
  portfolio = PORTFOLIO,
  budget = null,
  automation = null,
  host = null,
  // 15 §4: /state carries TODAY's partition plus the day keys that exist.
  roundsDate = shanghaiToday(),
  availableDates = null,
  // 15 §10: the Host decision on whether the UI may offer 启动服务.
  serviceStart = null,
} = {}) {
  return {
    ok: true,
    contract: 'trading-workbench/host-rounds/1',
    rounds_date: roundsDate,
    available_dates: Array.isArray(availableDates) ? availableDates : [roundsDate],
    service_start: serviceStart,
    service,
    automation,
    host,
    llm: { transport: 'ctx.llm.stream', mode: 'harness', route_selected: true, provider: 'opencode-go', model: 'deepseek-v4.1-flash' },
    in_flight: null,
    last_run_at: ROUND.finishedAt,
    last_error: null,
    last_round: lastRound,
    rounds,
    equityCurve,
    equity_curve: equityCurve,
    positions,
    orders,
    fills,
    reviews,
    account,
    budget,
    portfolio,
    gaps: ['AI-call audit is kept in the local RoundView JSON until the service AI-audit endpoint is wired end-to-end.'],
  }
}

// ---- mutable server side ---------------------------------------------------
const flags = { hangRun: false, run403: false, hangState: false, runOk: false, marketFail: false, envelopeFail: false, envelopeDeny: false, closeFail: false, evidenceFail: false, reconFail: false, roundDetailFail: false, hangRoundDetail: false, roundDetailNoId: false, catalogUnavailable: false, configSaveReject: false, configSaveConflict: false, pnlFail: false, pnlUnknown: false, reviewState: 'missing', hangAutomation: false, automationPhase: 'stopped', roundsFail: false, roundsOldHost: false, stateFailOnce: 0, serviceStartFail: false, setupReady: false, setupPolls: 0 }
// 15 §4: archived days other than today, keyed by day.
let roundsByDate = {}

// 16 §1: first-run bootstrap fixtures.
const SETUP_READINESS = {
  ready: false, needsPython: true,
  python: { found: true, path: "C:\\Python313\\python.exe", version: "3.13.12", source: "python" },
  venv: { dir: "data/service-venv", python: "data/service-venv/Scripts/python.exe", exists: false },
  deps: { installed: false, checked: false, detail: "venv missing" },
  service: { ok: false, error: "UNREACHABLE" },
  dotenv: { path: "multi-agent-trader/.env", exists: true },
  requirements: { path: "service/requirements.txt", exists: true },
  blockers: [{ code: "VENV_MISSING", message: "virtualenv missing" }, { code: "SERVICE_DOWN", message: "not answering" }],
}
function setupJobFixture(phase) {
  const step = (id, zh, en, status) => ({ id, label: { zh, en }, status, started_at: null, finished_at: null, exit_code: status === "failed" ? 1 : null, log_tail: status === "ok" ? [id + ": ok"] : [] })
  const done = phase !== "running"
  return {
    id: "setup-test", phase, created_at: new Date().toISOString(), updated_at: new Date().toISOString(), error: null,
    steps: [
      step("create-venv", "创建虚拟环境", "Create venv", done ? "ok" : "running"),
      step("install-deps", "安装依赖", "Install dependencies", done ? "ok" : "pending"),
      step("start-service", "启动服务", "Start service", done ? "ok" : "pending"),
      step("verify", "校验就绪", "Verify ready", done ? "ok" : "pending"),
    ],
  }
}

let pendingAutomationResolvers = []

/** Host answer for POST /automation/{stop,start} (same shape integration.js returns). */
function automationReply(pathname, body) {
  const stop = pathname.endsWith('/automation/stop')
  const phase = stop ? flags.automationPhase : 'running'
  const confirmed = !stop || phase === 'stopped'
  const automation = stop
    ? {
        contract: 'trading-workbench/automation/1',
        state: phase === 'stopped' ? 'stopped' : 'pending',
        reason_code: phase === 'stopped' ? 'SERVICE_CONTROL_PAUSE' : 'SERVICE_UNREACHABLE',
        scheduler_enabled: false,
        scheduler_supported: true,
        last_success_at: new Date(Date.now() - 7 * 60 * 1000).toISOString(),
        next_at: null,
        service_control: {
          state: phase === 'stopped' ? 'pause' : 'unknown',
          reason: body?.reason ?? null,
          updated_at: new Date().toISOString(),
          updated_by: 'operator',
          confirmed: phase === 'stopped',
        },
      }
    : {
        contract: 'trading-workbench/automation/1',
        state: 'running',
        reason_code: null,
        scheduler_enabled: true,
        scheduler_supported: true,
        last_success_at: new Date().toISOString(),
        next_at: new Date(Date.now() + 10 * 60 * 1000).toISOString(),
        service_control: { state: 'normal', confirmed: true },
      }
  return {
    ok: true,
    request_id: body?.request_id ?? null,
    phase,
    scheduler: { supported: true, stopped: stop, started: !stop, status: null, error: null },
    service_control: automation.service_control,
    error: stop && !confirmed ? { code: 'E_SERVICE_OFFLINE', message: 'service unreachable', status: 503 } : null,
    automation,
  }
}

function releaseAutomation() {
  const resolvers = pendingAutomationResolvers
  pendingAutomationResolvers = []
  for (const resolve of resolvers) resolve()
}
let savedConfig = null
let serverState = makeState()
let serverHealth = HEALTH
const strategyRecords = []
let marksOverride = null
const requestLog = []
const timers = []
let pendingStateResolvers = []

function reply(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, json: async () => body }
}

async function fetchStub(url, options) {
  const method = options?.method ?? 'GET'
  const target = String(url)
  const path = target.split('?')[0]
  requestLog.push({ url: target, method, body: options?.body ?? null })
  if (path.includes('/market/marks')) return reply({ ok: true, prices: marksOverride ?? {} })
  if (path.includes('/strategies')) {
    const sent = options?.body ? JSON.parse(options.body) : {}
    const suffix = path.split('/strategies')[1]
    const wrap = body => reply(JSON.parse(JSON.stringify(body)))
    if (suffix === '/config') return wrap({ config: { mode: 'native', revision: 0 } })
    if (!suffix && method === 'GET') return wrap({ strategies: strategyRecords })
    if (!suffix && method === 'POST') {
      const item = { strategy_id: 'strategy-ui', name: sent.name, current_version: null, enabled: false, generation: 0,
        draft: { revision: 1, source_text: sent.source_text, rules: {}, questions: [], answers: {}, suggestions: [], sources: {}, summary: '', content_hash: 'a'.repeat(64), validation: { valid: false, issues: ['缺少规则'], execution_ready: false } } }
      strategyRecords.push(item); return wrap({ strategy: item })
    }
    const item = strategyRecords.find(s => s.strategy_id === suffix.split('/')[1])
    if (!item) return wrap({ strategies: [] })
    if (suffix.endsWith('/draft')) {
      item.draft = { ...item.draft, ...sent, revision: sent.revision + 1 }
      return wrap({ strategy: item })
    }
    if (suffix.endsWith('/analyze')) {
      item.draft = { ...item.draft, revision: item.draft.revision + 1, summary: '价格阈值示例，来自用户原文',
        rules: { symbols: ['BTCUSDT'], timeframes: ['15m'], entry: { kind: 'mark_threshold', side: 'long', operator: 'gt', value: 100 }, exit: { stop_loss_pct: 0.02, take_profit_pct: 0.04 }, invalidation: { kind: 'stop_loss' }, position_constraints: { max_positions: 1 }, required_data: ['mark_price'], requires_ai: false },
        validation: { valid: true, execution_ready: true, issues: [], unsupported: [] } }
      return wrap({ strategy: item })
    }
    if (suffix.endsWith('/mount')) {
      item.current_version = 1; item.generation++
      item.confirmed_version = { rules: item.draft.rules, validation: item.draft.validation, content_hash: item.draft.content_hash }
      return wrap({ strategy: item })
    }
    if (suffix.endsWith('/authorization')) { item.enabled = sent.enabled; item.generation++; return wrap({ strategy: item }) }
    if (method === 'PATCH') { item.name = sent.name; return wrap({ strategy: item }) }
    return wrap({ strategy: item })
  }

  if (method === 'POST' && target.endsWith('/run')) {
    if (flags.hangRun) {
      return new Promise((resolve, reject) => {
        const signal = options?.signal
        if (signal && typeof signal.addEventListener === 'function') {
          signal.addEventListener('abort', () => {
            const error = new Error('aborted')
            error.name = 'AbortError'
            reject(error)
          })
        }
      })
    }
    if (flags.run403) {
      return reply({ ok: false, error: { code: 'E_AUTH_REQUIRED', message: 'auth required' } }, 403)
    }
    if (flags.runOk) {
      return reply({ ok: true, round_id: ROUND.roundId })
    }
    return reply({ ok: false, error: { code: 'RUN_IN_PROGRESS', message: 'a manual round is already running', details: { round_id: ROUND.roundId } } }, 409)
  }
  if (method === 'POST' && target.endsWith('/service/start')) {
    if (flags.serviceStartFail) return reply({ ok: false, error: { code: 'E_SERVICE_LAUNCHER_MISSING', message: 'the Host cannot start the service' } }, 503)
    return reply({ ok: true, state: 'started', pid: 4242, ready: true, waited_ms: 12000 })
  }
  if (path.endsWith('/setup/readiness')) {
    const base = flags.setupReady
      ? { ...SETUP_READINESS, ready: true, service: { ok: true }, blockers: [], deps: { installed: true, checked: true, detail: null }, venv: { ...SETUP_READINESS.venv, exists: true } }
      : SETUP_READINESS
    return reply({ ok: true, setup: base })
  }
  if (method === 'POST' && target.endsWith('/service/setup')) {
    flags.setupPolls = 0
    return reply({ ok: true, job_id: 'setup-test', reused: false, job: setupJobFixture('running') })
  }
  if (path.includes('/service/setup/')) {
    flags.setupPolls += 1
    if (flags.setupPolls > 1) flags.setupReady = true
    return reply({ ok: true, job: setupJobFixture(flags.setupReady ? 'ready' : 'running') })
  }
  if (path.endsWith('/state')) {
    // 15 §5: a plugin reload can 404/503 the first /state while routes re-register.
    if (flags.stateFailOnce > 0) {
      flags.stateFailOnce -= 1
      return reply({ ok: false, error: { code: 'E_HOST_STARTING', message: 'host routes are not registered yet' } }, 503)
    }
    if (flags.hangState) {
      return new Promise((resolve) => {
        pendingStateResolvers.push(() => resolve(reply({ ...serverState })))
      })
    }
    return reply(serverState)
  }
  if (path.endsWith('/operations')) return reply({ ok: true, operations: [
    { request_id: 'req-audit-1234', action: 'control', target: 'pause', reason: '测试暂停', status: 'succeeded', started_at: new Date().toISOString() },
    { request_id: 'req-audit-5678', action: 'close', target: 'BTCUSDT', reason: '测试未知', status: 'unknown', started_at: new Date().toISOString() },
  ] })
  if (method === 'POST' && (path.endsWith('/automation/stop') || path.endsWith('/automation/start'))) {
    const body = JSON.parse(options?.body || '{}')
    const stop = path.endsWith('/automation/stop')
    const answer = automationReply(path, body)
    if (flags.hangAutomation) {
      return new Promise((resolve) => {
        pendingAutomationResolvers.push(() => {
          if (stop && answer.phase === 'stopped') {
            serverState = { ...serverState, automation: answer.automation, service: { ...(serverState.service ?? {}), control_state: 'pause' } }
          }
          resolve(reply(answer))
        })
      })
    }
    if (stop && answer.phase === 'stopped') {
      serverState = { ...serverState, automation: answer.automation, service: { ...(serverState.service ?? {}), control_state: 'pause' } }
    }
    if (!stop) serverState = { ...serverState, automation: answer.automation, service: { ...(serverState.service ?? {}), control_state: 'normal' } }
    return reply(answer)
  }
  if (method === 'POST' && path.endsWith('/control')) {
    const body = JSON.parse(options.body)
    if (flags.controlHang) return new Promise((resolve, reject) => {
      options.signal?.addEventListener('abort', () => { const err = new Error('timeout'); err.name = 'AbortError'; reject(err) })
    })
    if (flags.controlFail) return reply({ ok: false, error: { code: 'E_CONTROL_LOCKED' } }, 423)
    return reply({ ok: true, requestId: body.request_id, result: { control: { state: body.state }, operation_status: body.state === 'emergency' ? 'partial' : 'succeeded', flattened: body.state === 'emergency' ? { ok: false } : null } })
  }
  if (method === 'POST' && path.endsWith('/positions/close')) {
    const body = JSON.parse(options?.body || '{}')
    if (flags.closeFail) return reply({ ok: false, error: { code: 'E_REASON_REQUIRED', message: 'reason required' } }, 422)
    return reply({ ok: true, requestId: 'req-test-close', result: { symbol: body.symbol, closed: true, status: 'CLOSED', venue: 'demo' } })
  }
  if (method === 'POST' && path.endsWith('/protection/repair')) {
    const body = JSON.parse(options?.body || '{}')
    const symbol = (body.symbols && body.symbols[0]) || 'ALL'
    if (flags.repairPartial) return reply({ ok: true, requestId: 'req-test-partial', result: { ok: false, operation_status: 'partial', repaired: [{ symbol, status: 'UNKNOWN' }] } })
    return reply({ ok: true, requestId: 'req-test-repair', result: { ok: true, repaired: [{ symbol, status: 'OK' }], venue: 'demo' } })
  }
  if (method === 'POST' && path.endsWith('/reconcile')) {
    if (flags.reconFail) return reply({ ok: false, error: { code: 'E_SERVICE_UNAVAILABLE', message: 'reconcile down' } }, 503)
    if (flags.reconStale) {
      return reply({
        ok: true,
        requestId: 'req-test-recon-stale',
        result: {
          ok: true,
          exchange_reachable: false,
          stale: true,
          adopted: [],
          closed_external: ['BTCUSDT'],
          protection_missing: [],
          outbox: { checked: 2, resolved: 0 },
          venue: 'demo',
          demo_only: true,
          ledger_id: 'ledger-test',
        },
      })
    }
    return reply({
      ok: true,
      requestId: 'req-test-recon',
      result: {
        ok: true,
        exchange_reachable: true,
        stale: false,
        adopted: [],
        closed_external: [],
        protection_missing: ['ETHUSDT'],
        outbox: { checked: 1, resolved: 0 },
        venue: 'demo',
        demo_only: true,
        ledger_id: 'ledger-test',
      },
    })
  }
  if (path.endsWith('/reviews/daily')) {
    const url = new URL(target, 'http://fixture.local')
    const date = url.searchParams.get('date') || '2026-11-10'
    if (method === 'POST') {
      if (flags.reviewState === 'conflict') return reply({ ok: false, error: { code: 'E_REVIEW_IN_PROGRESS', message: 'already generating' } }, 409)
      if (flags.reviewState === 'fail') return reply({ ok: false, error: { code: 'E_REVIEW_FAILED', message: 'model down' } }, 502)
      const sent = JSON.parse(options?.body || '{}')
      return reply({
        ok: true,
        date: sent.date || date,
        markdown: '# 2026-11-10 复盘\n\n## 概览\n\n- 轮次 3，成交 1\n- 无 P1 裁决\n\n本日无异常。',
        generatedAt: '2026-11-10T01:55:00.000Z',
        model: 'fake-1',
        cached: false,
      })
    }
    if (flags.reviewState === 'ok') {
      return reply({
        ok: true,
        date,
        markdown: '# 2026-11-10 复盘\n\n- 缓存报告\n\n本日无异常。',
        generatedAt: '2026-11-10T01:40:00.000Z',
        model: 'fake-1',
        cached: true,
      })
    }
    if (flags.reviewState === 'fail') return reply({ ok: false, error: { code: 'E_REVIEW_FAILED', message: 'model down' } }, 502)
    return reply({ ok: false, error: { code: 'E_REVIEW_NOT_GENERATED', details: { date } } }, 404)
  }
  if (path.includes('/cycles/') && path.endsWith('/evidence')) {
    if (flags.evidenceFail) return reply({ ok: false, error: { code: 'E_CYCLE_NOT_FOUND', message: 'not found' } }, 404)
    const url = new URL(target, 'http://fixture.local')
    const agent = url.searchParams.get('agent')
    return reply({
      ok: true,
      cycle_id: 'cyc-20261109T044943Z-cb5a4d',
      snapshot_id: 'ev-test',
      schema_version: 'md-evidence/1',
      verdict: 'GOOD',
      sha256: 'sha256:abc123def4567890',
      bytes: 4321,
      agent,
      path: 'evidence/cyc.json',
      evidence: { cycle_id: 'cyc-20261109T044943Z-cb5a4d', thesis: { side: 'long' }, scores: { red: 0.62 } },
    })
  }
  if (path.includes('/market/prices')) {
    if (flags.marketFail) return reply({ ok: false, error: { code: 'E_SERVICE_UNAVAILABLE' } }, 503)
    return reply(PRICES)
  }
  if (path.endsWith('/pnl')) {
    if (flags.pnlFail) {
      return reply({
        ok: false,
        source: 'service:/api/pnl',
        fetched_at: new Date().toISOString(),
        range_s: 86400,
        error: { code: 'E_SERVICE_TIMEOUT', message: 'workbench-service /api/pnl timed out', status: 503 },
      })
    }
    return reply({ ...(flags.pnlUnknown ? PNL_UNKNOWN : PNL), fetched_at: new Date().toISOString() })
  }
  if (path.endsWith('/risk/envelope')) {
    if (flags.envelopeFail) return reply({ ok: false, error: { code: 'E_SERVICE_UNAVAILABLE' } }, 503)
    if (flags.envelopeDeny) return reply({ ...ENVELOPE, new_entry_allowed: false, blocking_reasons: ['CONTROL_CLOSE_ONLY'] })
    return reply(ENVELOPE)
  }
  if (path.endsWith('/health')) return reply(serverHealth)
  if (path.endsWith('/llm/catalog')) {
    if (flags.catalogUnavailable) {
      return reply({
        ok: true, available: false, fetchedAt: null, groups: [], failures: [], reason: 'CATALOG_UNAVAILABLE',
        presets: {},
        config: { ...CONFIG_SUMMARY_FIXTURE, revision: null, banner: '目录暂不可用，配置未固化', catalog: { available: false } },
      })
    }
    return reply({
      ok: true, available: true, fetchedAt: new Date().toISOString(), groups: CONFIG_GROUPS, failures: [],
      presets: CONFIG_PRESETS_FIXTURE,
      config: CONFIG_SUMMARY_FIXTURE,
    })
  }
  if (path.endsWith('/llm/config')) {
    if (method === 'POST') {
      const sent = JSON.parse(options?.body || '{}')
      if (flags.configSaveConflict) {
        return reply({
          ok: false,
          error: {
            code: 'E_CONFIG_REVISION_CONFLICT',
            message: '配置已被更新：请刷新后重新保存，避免覆盖新值',
            details: { provided: sent.baseRevision ?? null, current: 3 },
          },
        }, 409)
      }
      if (flags.configSaveReject) {
        return reply({
          ok: false,
          error: {
            code: 'ROUTE_UNAVAILABLE',
            message: '配置未保存',
            details: { errors: [{ role: 'decision', code: 'UNSUPPORTED_REASONING_EFFORT', message: 'qmodel_latest does not support effort max' }] },
          },
        }, 422)
      }
      savedConfig = sent.config
      return reply({
        ok: true,
        config: { ...sent.config, revision: 2, updatedAt: '2026-11-12T06:00:00.000Z' },
        summary: { ...CONFIG_SUMMARY_FIXTURE, revision: 2 },
        effectiveFrom: 'nextRound',
      })
    }
    return reply({ ok: true, config: CONFIG_FILE_FIXTURE, summary: CONFIG_SUMMARY_FIXTURE })
  }
  if (path.endsWith('/rounds')) {
    // 15 §4: GET /rounds?date=YYYY-MM-DD answers one archived day.
    if (flags.roundsFail) return reply({ ok: false, error: { code: 'E_DATE_INVALID', message: 'date must be YYYY-MM-DD' } }, 400)
    // A Host that predates the archive ignores ?date= and answers without it.
    if (flags.roundsOldHost) return reply({ ok: true, rounds: serverState.rounds ?? [] })
    const requested = new URL(target, 'http://stub').searchParams.get('date') ?? shanghaiToday()
    const dayRounds = requested === shanghaiToday() ? (serverState.rounds ?? []) : (roundsByDate[requested] ?? [])
    return reply({ ok: true, date: requested, today: shanghaiToday(), rounds: dayRounds, total: dayRounds.length, truncated: false })
  }
  if (path.includes('/rounds/')) {
    // 05 N09: a failed detail fetch must be surfaced, never papered over.
    if (flags.roundDetailFail) return reply({ ok: false, error: { code: 'ROUND_NOT_FOUND', message: 'Round not found.' } }, 404)
    // ...and an in-flight one must not render the newest round either.
    if (flags.hangRoundDetail) return new Promise(() => {})
    const id = decodeURIComponent(path.split('/rounds/')[1] ?? '')
    const round = ROUNDS_BY_ID[id] ?? NO_TRADE
    // 03/A6: a payload that cannot be matched to the selection (no roundId).
    if (flags.roundDetailNoId) return reply({ ok: true, round: { ...round, roundId: undefined } })
    return reply({ ok: true, round })
  }
  return reply({ ok: true })
}

function releaseState() {
  const resolvers = pendingStateResolvers
  pendingStateResolvers = []
  for (const resolve of resolvers) resolve()
}

// ---- tiny hooks runtime ----------------------------------------------------
const Fragment = Symbol('fragment')
const runtime = { instances: new Map(), current: null, effects: [], dirty: false }

function instance(path) {
  let rec = runtime.instances.get(path)
  if (!rec) {
    rec = { path, cursor: 0, states: [], refs: {}, effectDeps: [] }
    runtime.instances.set(path, rec)
  }
  return rec
}
function schedule() { runtime.dirty = true }

const React = {
  Fragment,
  createElement(type, props, ...children) {
    const merged = { ...(props ?? {}) }
    if (children.length === 1) merged.children = children[0]
    else if (children.length > 1) merged.children = children
    return { $$el: true, type, props: merged }
  },
  useState(initial) {
    const rec = runtime.current
    const index = rec.cursor++
    if (index >= rec.states.length) rec.states[index] = typeof initial === 'function' ? initial() : initial
    const set = (next) => {
      rec.states[index] = typeof next === 'function' ? next(rec.states[index]) : next
      schedule()
    }
    return [rec.states[index], set]
  },
  useEffect(effect, deps) {
    const rec = runtime.current
    const index = rec.cursor++
    runtime.effects.push({ rec, index, effect, deps })
  },
  useCallback(fn) { runtime.current.cursor++; return fn },
  useMemo(fn) { runtime.current.cursor++; return fn() },
  useRef(initial) {
    const rec = runtime.current
    const index = rec.cursor++
    if (!(index in rec.refs)) rec.refs[index] = { current: initial }
    return rec.refs[index]
  },
}

// ---- element walker -------------------------------------------------------
function walk(element, parentPath, index) {
  if (element === null || element === undefined || element === false || element === true) return null
  if (typeof element === 'string' || typeof element === 'number') return { text: String(element) }
  if (Array.isArray(element)) return element.map((child, childIndex) => walk(child, parentPath, childIndex)).filter(Boolean)
  if (!element || element.$$el !== true) return null
  const key = element.props && element.props.key !== undefined ? element.props.key : index
  const name = typeof element.type === 'function' ? (element.type.name || 'component') : String(element.type)
  const path = `${parentPath}/${name}[${key}]`
  if (element.type === Fragment || (typeof element.type !== 'string' && typeof element.type !== 'function')) {
    return walk(element.props.children, path, 0)
  }
  if (typeof element.type === 'function') {
    const rec = instance(path)
    const previous = runtime.current
    runtime.current = rec
    rec.cursor = 0
    let output
    try {
      output = element.type(element.props)
    } catch (error) {
      throw new Error(`component ${element.type.name} threw: ${error && error.message ? error.message : error}`)
    } finally {
      runtime.current = previous
    }
    return walk(output, path, 0)
  }
  return { tag: element.type, props: element.props, children: walk(element.props.children, path, 0) }
}

function textOf(tree) {
  if (!tree) return ''
  if (Array.isArray(tree)) return tree.map(textOf).join(' ')
  if (tree.text !== undefined) return tree.text
  return textOf(tree.children)
}
function findAll(tree, predicate, output = []) {
  if (!tree) return output
  if (Array.isArray(tree)) { for (const node of tree) findAll(node, predicate, output); return output }
  if (predicate(tree)) output.push(tree)
  findAll(tree.children, predicate, output)
  return output
}
function findClickable(label) {
  const matches = findAll(currentTree, (node) => {
    if (node.tag !== 'button' || typeof node.props?.onClick !== 'function') return false
    const text = textOf(node)
    const aria = String(node.props?.['aria-label'] ?? '')
    const title = String(node.props?.title ?? '')
    return text.includes(label) || aria.includes(label) || title.includes(label)
  })
  if (matches.length === 0) return null
  // Prefer an exact aria-label/text match over a button that merely mentions the
  // word in its tooltip: the top-bar stop button explains "不平仓" in its title,
  // and that must not shadow the position 平仓 action.
  const rank = (node) => {
    const text = textOf(node).trim()
    const aria = String(node.props?.['aria-label'] ?? '').trim()
    if (aria === label || text === label) return 0
    if (aria.includes(label)) return 1
    if (text.includes(label)) return 2
    return 3
  }
  return matches.slice().sort((a, b) => rank(a) - rank(b))[0]
}
function findButtonByAria(label) {
  return findAll(currentTree, (node) => node.tag === 'button' && String(node.props?.['aria-label'] ?? '').includes(label))[0] ?? null
}
async function click(label) {
  const target = findClickable(label)
  if (!target) {
    const buttons = findAll(currentTree, (node) => node.tag === 'button')
      .map((node) => `${textOf(node)}|${node.props?.['aria-label'] ?? ''}`)
    console.error('VISIBLE BUTTONS:', buttons.join(' ;; '))
    throw new Error(`no clickable control found for "${label}"`)
  }
  runtime.current = null
  await Promise.resolve(target.props.onClick({ preventDefault() {}, stopPropagation() {} }))
  await flush()
  return render(panel)
}
function clickNoWait(label) {
  const target = findClickable(label)
  if (!target) throw new Error(`no clickable control found for "${label}"`)
  runtime.current = null
  const result = target.props.onClick({ preventDefault() {}, stopPropagation() {} })
  if (result && typeof result.catch === 'function') result.catch(() => {})
  return result
}
function changeSelect(label, value) {
  const target = findAll(currentTree, (node) => node.tag === 'select' && String(node.props?.['aria-label'] ?? '').includes(label))[0]
  if (!target) throw new Error(`no select found for "${label}"`)
  target.props.onChange({ target: { value } })
}
function changeInput(label, value) {
  const target = findAll(currentTree, (node) => node.tag === 'input' && String(node.props?.['aria-label'] ?? '').includes(label))[0]
  if (!target) throw new Error(`no input found for "${label}"`)
  target.props.onChange({ target: { value } })
}
function fireTimers(delay) {
  const due = timers.filter((timer) => timer.delay === delay)
  for (const timer of due) {
    const index = timers.indexOf(timer)
    if (index >= 0) timers.splice(index, 1)
    timer.fn()
  }
}

let currentTree = null
async function render(component) {
  for (let pass = 0; pass < 40; pass += 1) {
    runtime.dirty = false
    runtime.effects = []
    currentTree = walk(React.createElement(component, { t: lookup }), '', 0)
    for (const item of runtime.effects) {
      const previous = item.rec.effectDeps[item.index]
      const same = previous !== undefined && sameDeps(previous, item.deps)
      if (same) continue
      item.rec.effectDeps[item.index] = item.deps
      item.effect()
    }
    await flush()
    if (!runtime.dirty) return currentTree
  }
  throw new Error('render did not settle after 40 passes')
}
function sameDeps(a, b) {
  if (a === undefined || b === undefined) return a === b
  if (a.length !== b.length) return false
  return a.every((value, index) => Object.is(value, b[index]))
}
async function flush() {
  for (let step = 0; step < 16; step += 1) await Promise.resolve()
  await new Promise((resolve) => setImmediate(resolve))
}

// ---- load the bundle -------------------------------------------------------
let lookup = (key) => key
let spec = null
new Function('window', 'document', 'fetch', 'setTimeout', 'setInterval', 'clearTimeout', 'clearInterval', SOURCE)({
  __ModuleLoader__: { load(value) { spec = value } },
}, {
  visibilityState: 'visible',
  activeElement: null,
  documentElement: { lang: 'zh' },
  addEventListener() {},
  removeEventListener() {},
}, fetchStub, (fn, delay) => { timers.push({ fn, delay }); return timers.length }, () => 0, () => {}, () => {})

const moduleExports = spec.factory((name) => {
  if (name !== 'react') throw new Error(`unexpected require(${name})`)
  return React
})
const registrations = []
let LOCALE_DICT = { zh: {}, en: {} }
const ctx = {
  locale: {
    bind: () => (key) => lookup(key),
    register: (ns, dict) => {
      LOCALE_DICT = dict && typeof dict === 'object' ? dict : LOCALE_DICT
      lookup = (key) => (dict?.zh && typeof dict.zh[key] === 'string' ? dict.zh[key] : key)
      return () => {}
    },
  },
  slots: {
    inject: (_slot, callback) => { callback(); return () => {} },
    register: (options, component) => { registrations.push({ options, component }); return () => {} },
  },
  effect: (fn) => { const dispose = fn(); return () => { if (typeof dispose === 'function') dispose() } },
}
moduleExports.apply(ctx, {})
const panel = registrations.find((entry) => entry.options?.name === 'main')?.component

const failures = []
const notes = []
function expect(label, condition, detail) {
  if (condition) notes.push(`ok   ${label}`)
  else failures.push(`${label}${detail ? ` — ${detail}` : ''}`)
}

// The banned copy is the "wait for a Host extension" phrasing. "未提供" is a
// legitimate P0 empty-state value for fields the Host has not mapped yet.
const STALE_COPY = ['未暴露', '待 Host 暴露', '尚未接入', 'does not expose', 'await a Host extension', 'dataGapList']
const staleCopyFound = STALE_COPY.filter((needle) => SOURCE.includes(needle))
expect('S1 bundle contains no "Host does not expose" copy', staleCopyFound.length === 0, staleCopyFound.join(','))
function tableHeadersFor(headerText) {
  const table = findAll(currentTree, (node) => node.tag === 'table' && textOf(node).includes(headerText))[0]
  if (!table) return null
  return findAll(table, (node) => node.tag === 'th')
}

// ---- scenario L: optional live Host shape --------------------------------
// Run with TWB_LIVE_STATE_URL=http://127.0.0.1:59201/trading-workbench/api/state
// to assert the panel against the running Host instead of the fixtures.
const LIVE_URL = process.env.TWB_LIVE_STATE_URL || ''
if (LIVE_URL) {
  const response = await globalThis.fetch(LIVE_URL)
  const liveState = await response.json()
  serverState = liveState
  serverHealth = { ...HEALTH, service: liveState?.service ?? null }
  const liveText = textOf(await render(panel))
  expect('L1 live state responds ok', liveState?.ok === true)
  expect('L2 live service connected in S4', liveText.includes('已连接'), liveText.slice(0, 160))
  const livePositions = Array.isArray(liveState?.positions) ? liveState.positions : []
  if (livePositions.length > 0) {
    expect('L3 live positions use nine columns', (tableHeadersFor('保护')?.length ?? 0) === 9)
  } else {
    expect('L4 live positions empty state', liveText.includes('当前无持仓'), liveText.slice(0, 200))
  }
  const liveStale = ['未暴露', '待 Host'].filter((needle) => liveText.includes(needle))
  expect('L5 live has no stale "not exposed" copy', liveStale.length === 0, liveStale.join(','))
  const liveRound = liveState?.last_round ?? null
  const liveReason = String(liveRound?.reasonCode ?? '')
  if (liveReason.startsWith('EXECUTION_BLOCKED')) {
    // The label table renders EXECUTION_BLOCKED:<sub> as 执行阻断：<中文>; the
    // older 执行阻断（CODE） form is also acceptable when the sub-code is unmapped.
    expect('L6 live blocked round shows the blocking code',
      liveText.includes('执行阻断：') || liveText.includes('执行阻断（'),
      liveText.slice(0, 300))
    expect('L6 live blocked round never says unknown reason', !liveText.includes('未知原因'))
    const liveDetail = liveRound?.reasonDetail ?? liveRound?.execution?.rationale ?? null
    if (liveDetail) {
      expect('L6 live blocked round shows the real detail', liveText.includes(String(liveDetail).slice(0, 40)), String(liveDetail).slice(0, 80))
    }
    const liveBlocked = liveRound?.execution?.blocked_reason ?? null
    if (liveBlocked) {
      expect('L6 live blocked reason code is visible', liveText.includes(String(liveBlocked)), String(liveBlocked))
    }
  }
  serverState = makeState()
  serverHealth = HEALTH
  runtime.instances.clear()
}

// ---- scenario A: cockpit with the real nested service shape --------------
const overviewText = textOf(await render(panel))
expect('A1 cockpit renders title + DEMO', overviewText.includes('交易工作台') && overviewText.includes('DEMO'))
expect('A2 run control visible', overviewText.includes('运行一轮'))
expect('A3 service connected in S4', overviewText.includes('已连接'), overviewText.slice(-300))
expect('A4 equity hero from the Host account', overviewText.includes('10,844.02'), overviewText.slice(0, 200))
expect('A5 S1 market segment shows prices', overviewText.includes('68,402.5') && overviewText.includes('资金费率 0.0100%') && overviewText.includes('价差 0.2bp') && !overviewText.includes('未接入（P1）'), overviewText.slice(0, 260))
expect('A6 seven position columns (B+ blotter)', (tableHeadersFor('保护')?.length ?? 0) === 7, String(tableHeadersFor('保护')?.length))
expect('A7 six-code legend', overviewText.includes('缺失') && overviewText.includes('完好') && overviewText.includes('未对账'))
expect('A8 MISSING row shown as blocked', overviewText.includes('已阻断新开仓') || overviewText.includes('缺失'), overviewText.slice(0, 400))
expect('A9 distance cell has stop + liq', overviewText.includes('止损 1.1%') && overviewText.includes('强平 10.5%'), overviewText.match(/止损[^ ]*/)?.[0])
expect('A10 cockpit does not fetch a round detail', !requestLog.some((entry) => entry.url.includes('/rounds/')), JSON.stringify(requestLog.slice(-3)))
expect('A11 main is the cockpit tabpanel', findAll(currentTree, (node) => node.tag === 'main' && node.props?.role === 'tabpanel' && node.props?.id === 'twb-ws-panel-cockpit' && node.props?.['aria-labelledby'] === 'twb-ws-tab-cockpit').length === 1)
expect('A12 tape renders the Host order', overviewText.includes('68,121.0') && overviewText.includes('成交'))
expect('A13 today summary counts', overviewText.includes('今日 AI 摘要') && overviewText.includes('成交'), overviewText.slice(-400))
expect('A14 schedule is honestly off and Demo action boundary is visible', overviewText.includes('未启用（M2）') && overviewText.includes('Demo 处置需确认'))
expect('A15 engineering fields (ledger) are not on the first screen', !overviewText.includes('6512827bcdfe4250a1bb5710217e044d'))
expect('A16 no English portfolio gap on the trading page', !overviewText.includes('AI-call audit is kept'), overviewText.slice(0, 300))
expect('A17 risk budget blocks the new entry', overviewText.includes('新开仓') && overviewText.includes('已阻断') && overviewText.includes('保护缺失'), overviewText.slice(0, 600))
expect('A19 prices route called with the current symbol', requestLog.some((entry) => entry.url.includes('/market/prices?symbols=BTCUSDT')), JSON.stringify(requestLog.slice(0, 6)))
expect('A20 current envelope caps replace 上轮', !overviewText.includes('上轮') && overviewText.includes('1.0%') && overviewText.includes('10×'), overviewText.slice(0, 400))
expect('A21 no fabricated 24h change from prices', !/24h\s*[▲▼]/.test(overviewText))

// ---- scenario P: portfolio actions (close / repair, mock fetch) ----------
const actionButtons = findAll(currentTree, (node) => node.tag === 'button' && ['平仓', '补挂', '证据'].includes(String(node.props?.['aria-label'] ?? '')))
expect('P1 three actions per position row', actionButtons.length === 6, String(actionButtons.length))
const repairButtons = actionButtons.filter((node) => node.props['aria-label'] === '补挂')
expect('P2 repair disabled when protection OK', repairButtons[0]?.props?.disabled === true && repairButtons[1]?.props?.disabled === false, `${repairButtons[0]?.props?.disabled}/${repairButtons[1]?.props?.disabled}`)
// 03 B3: the reason is announced via aria-describedby and printed on screen.
const p2Text = textOf(await render(panel))
expect('P2b the disabled repair reason is not hover-only',
  repairButtons[0]?.props?.['aria-describedby'] === 'twb-repair-hint'
    && p2Text.includes('保护完好时“补挂”不可用')
    && Boolean(findAll(currentTree, (node) => node.props?.id === 'twb-repair-hint').length),
  JSON.stringify({ describedBy: repairButtons[0]?.props?.['aria-describedby'], hasHint: p2Text.includes('保护完好时“补挂”不可用') }))
await click('平仓')
const closeDialogText = textOf(await render(panel))
// 05 N08: the number is unrealized PnL; the old copy claimed it was realized.
expect('P3 close dialog fields', closeDialogText.includes('确认平仓（Demo）') && closeDialogText.includes('参考标记价') && closeDialogText.includes('当前未实现盈亏（参考，未扣手续费）') && closeDialogText.includes('不可撤销'))
expect('P4 close submit disabled without reason', findClickable('确认平仓')?.props?.disabled === true)
changeInput('平仓原因', 'risk exit')
await render(panel)
expect('P5 close submit enabled after reason', findClickable('确认平仓')?.props?.disabled === false)
await click('确认平仓')
const closePost = requestLog.filter((entry) => entry.method === 'POST' && entry.url.includes('/positions/close')).pop()
const closeBody = JSON.parse(closePost?.body || '{}')
expect('P6 close body carries confirm CLOSE', closeBody.confirm === 'CLOSE' && closeBody.symbol === 'BTCUSDT' && closeBody.reason === 'risk exit', JSON.stringify(closeBody))
const closeReceipt = textOf(await render(panel))
expect('P7 close receipt shows requestId + status', closeReceipt.includes('req-test-close') && closeReceipt.includes('CLOSED'), closeReceipt.slice(-500))
await click('关闭对话框')
const repairTargets = findAll(currentTree, (node) => node.tag === 'button' && node.props?.['aria-label'] === '补挂')
runtime.current = null
await Promise.resolve(repairTargets[1].props.onClick({ preventDefault() {}, stopPropagation() {} }))
await flush()
await render(panel)
const repairDialogText = textOf(await render(panel))
expect('P8 repair dialog explains semantics', repairDialogText.includes('已有保护单不改价') && repairDialogText.includes('确认补挂'))
await click('确认补挂')
const repairPost = requestLog.filter((entry) => entry.method === 'POST' && entry.url.includes('/protection/repair')).pop()
const repairBody = JSON.parse(repairPost?.body || '{}')
expect('P9 repair body carries confirm REPAIR', repairBody.confirm === 'REPAIR' && Array.isArray(repairBody.symbols) && repairBody.symbols[0] === 'ETHUSDT', JSON.stringify(repairBody))
const repairReceipt = textOf(await render(panel))
expect('P10 repair receipt', repairReceipt.includes('req-test-repair') && repairReceipt.includes('ETHUSDT'), repairReceipt.slice(-400))
await click('关闭对话框')
flags.repairPartial = true
const partialRepair = findAll(currentTree, (node) => node.tag === 'button' && node.props?.['aria-label'] === '补挂' && !node.props.disabled)[0]
await partialRepair.props.onClick()
await render(panel)
await click('确认补挂')
expect('P10b partial repair receipt is never called fully successful', textOf(currentTree).includes('部分完成，需核对') && textOf(currentTree).includes('UNKNOWN'))
await click('关闭对话框')
flags.repairPartial = false
flags.closeFail = true
await click('平仓')
changeInput('平仓原因', 'risk exit')
await render(panel)
await click('确认平仓')
const failCloseText = textOf(await render(panel))
expect('P11 close failure receipt', failCloseText.includes('E_REASON_REQUIRED') && failCloseText.includes('不会自动重试'), failCloseText.slice(-500))
expect('P12 no automatic retry', requestLog.filter((entry) => entry.url.includes('/positions/close')).length === 2, String(requestLog.filter((entry) => entry.url.includes('/positions/close')).length))
await click('关闭对话框')
flags.closeFail = false

// ---- scenario Q: inline precheck (green / yellow / red) -------------------
const runPosts = () => requestLog.filter((entry) => entry.method === 'POST' && entry.url.endsWith('/run')).length
serverState = makeState()
await click('更多')
await click('刷新数据')
await click('关闭')
const greenBefore = runPosts()
await click('运行一轮')
expect('Q1 green submits in one click', runPosts() === greenBefore + 1, `${greenBefore} -> ${runPosts()}`)
expect('Q2 green does not open the panel', !textOf(await render(panel)).includes('运行前预检'))
await click('关闭')
await click('预检')
const precheckText = textOf(await render(panel))
expect('Q3 precheck rows are inline', precheckText.includes('运行前预检') && precheckText.includes('服务在线') && precheckText.includes('无在跑轮次') && precheckText.includes('证据新鲜') && precheckText.includes('今日额度'), precheckText.slice(-700))
expect('Q4 entry blocked is explained separately', precheckText.includes('可运行，但本轮不会开仓'), precheckText.slice(-500))
await click('关闭预检')
flags.envelopeFail = true
await click('更多')
await click('刷新数据')
await click('关闭')
const yellowBefore = runPosts()
await click('运行一轮')
const yellowText = textOf(await render(panel))
// 05 N04: the precheck must quote the 340s chain deadline (5:40), not 180s.
expect('Q5 yellow opens confirm strip without submitting', yellowText.includes('需确认') && yellowText.includes('本轮最长 5:40（340s）') && yellowText.includes('继续提交') && runPosts() === yellowBefore, `${runPosts()}/${yellowBefore}`)
await click('继续提交')
await render(panel)
expect('Q6 confirm submits once', runPosts() === yellowBefore + 1, `${runPosts()}/${yellowBefore}`)
flags.envelopeFail = false
serverState = { ...makeState(), in_flight: { round_id: 'run-test', started_at: new Date().toISOString() } }
await click('更多')
await click('刷新数据')
await click('关闭')
const redRun = findClickable('运行一轮')
expect('Q7 red disables the run button', Boolean(redRun?.props?.disabled), String(redRun?.props?.disabled))
const redBefore = runPosts()
await click('预检')
const redText = textOf(await render(panel))
expect('Q8 red explains and offers repair', redText.includes('不可运行') && redText.includes('已有在跑轮次') && redText.includes('去修复'), redText.slice(-600))
expect('Q9 red never submits', runPosts() === redBefore)
await click('关闭预检')
serverState = makeState()
await click('更多')
await click('刷新数据')
await click('关闭')
await render(panel)
expect('A18 S2 is a single attention bar', findAll(currentTree, (node) => String(node.props?.className ?? '').split(' ').includes('twb-s2')).length === 1)

// ---- scenario B: AI workspace, round detail, drawer -----------------------
await click('AI 决策')
const aiText = textOf(await render(panel))
expect('B1 AI panel id follows the workspace', findAll(currentTree, (node) => node.tag === 'main' && node.props?.id === 'twb-ws-panel-ai').length === 1)
expect('B2 AI secondary tabs', aiText.includes('本轮') && aiText.includes('历史') && aiText.includes('分析'))
expect('B3 AI fetches the round detail', requestLog.some((entry) => entry.url.includes(`/rounds/${ROUND.roundId}`)), JSON.stringify(requestLog.slice(-4)))
for (const label of ['分析', '红队', '蓝队', '决策', '风控', '操盘手']) {
  expect(`B4 stage ${label}`, aiText.includes(label))
}
expect('B5 attempts are visible', aiText.includes('×2') || aiText.includes('尝试'))
expect('B6 verdict card fields', aiText.includes('AI 建议') && aiText.includes('风控裁决') && aiText.includes('控制态'))
expect('B7 N2 scorecard fields', aiText.includes('评级') && aiText.includes('辩论质量') && (aiText.includes('单薄') || aiText.includes('thin')) && aiText.includes('评级不是收益预测'), aiText.slice(-700))
expect('B7b violations empty is honest', aiText.includes('本轮规则无命中'))
expect('B7c red/blue comparison shows the gate numbers',
  aiText.includes('红蓝对比') && aiText.includes('1.66') && aiText.includes('0.86%') && aiText.includes('FAIL_RR'),
  aiText.slice(-800))
expect('B7d decision rationale is visible',
  aiText.includes('决策依据') && aiText.includes('上行突破回踩') &&
    aiText.includes('红队 RR 1.66 且触发器已确认') && aiText.includes('15m 收于 67650 下方'),
  aiText.slice(-800))
expect('B7e chain budget strip',
  aiText.includes('调用') && aiText.includes('重试') && aiText.includes('截断') && aiText.includes('用时'),
  aiText.slice(0, 600))
await click('证据')
const drawerText = textOf(await render(panel))
expect('B8 evidence drawer opens', drawerText.includes('证据抽屉'))
for (const tab of ['Prompt', '输出 JSON', '硬规则', '耗时']) expect(`B9 drawer tab ${tab}`, drawerText.includes(tab))
expect('B10 drawer body is a tabpanel', findAll(currentTree, (node) => node.props?.role === 'tabpanel' && String(node.props?.id ?? '').startsWith('twb-drawer-panel-')).length === 1)
await click('硬规则')
expect('B11 rules tab renders P1 rows', textOf(await render(panel)).includes('P1 结论'))
await click('关闭证据抽屉')
expect('B12 drawer closes', !textOf(await render(panel)).includes('证据抽屉'))

// ---- evidence full snapshot (lazy + cached per cycle/agent) ---------------
const evidenceRequests = () => requestLog.filter((entry) => entry.url.includes('/evidence'))
await click('证据')
const evidenceText = textOf(await render(panel))
expect('B12b evidence summary from the proxy', evidenceText.includes('ev-test') && evidenceText.includes('md-evidence/1') && evidenceText.includes('4321'), evidenceText.slice(-700))
const snapshotDetails = findAll(currentTree, (node) => node.tag === 'details')
const snapshotPre = findAll(currentTree, (node) => node.tag === 'pre' && node.props?.style?.maxHeight === 320)
expect('B12c full snapshot is collapsed by default', snapshotDetails.length === 1 && snapshotDetails[0].props?.open !== true && snapshotPre.length === 1, `${snapshotDetails.length}/${snapshotPre.length}`)
expect('B12d evidence loaded once per cycle', evidenceRequests().length === 1, String(evidenceRequests().length))
await click('关闭证据抽屉')
const stageEvidenceButtons = findAll(currentTree, (node) => node.tag === 'button' && textOf(node) === '证据' && !node.props?.disabled)
runtime.current = null
await Promise.resolve(stageEvidenceButtons[1].props.onClick({ preventDefault() {}, stopPropagation() {} }))
await flush()
await render(panel)
const agentEvidence = evidenceRequests().pop()
expect('B12e call switch requests that agent only', String(agentEvidence?.url ?? '').includes('?agent=analysis') && evidenceRequests().length === 2, `${agentEvidence?.url} / ${evidenceRequests().length}`)
await click('关闭证据抽屉')
flags.evidenceFail = true
runtime.current = null
await Promise.resolve(stageEvidenceButtons[2].props.onClick({ preventDefault() {}, stopPropagation() {} }))
await flush()
await render(panel)
const failedEvidence = textOf(await render(panel))
expect('B12f failure keeps host summary and names the code', failedEvidence.includes('全文未提供（E_CYCLE_NOT_FOUND）') && failedEvidence.includes('host-cyc'), failedEvidence.slice(-700))
flags.evidenceFail = false
await click('关闭证据抽屉')

await click('历史')
const historyText = textOf(await render(panel))
expect('B13 history lists reason codes', historyText.includes('NO_CANDIDATE') && historyText.includes('全部'))
expect('B13b history uses seven columns', (tableHeadersFor('原因码')?.length ?? 0) === 7, String(tableHeadersFor('原因码')?.length))
expect('B13c history has a duration filter', historyText.includes('全部时长') && historyText.includes('≤30s'))
await click('>90s')
const longText = textOf(await render(panel))
expect('B13d duration filter empties the table', longText.includes('该筛选下没有轮次'), longText.slice(-600))
await click('全部时长')
await click('失败 0')
const filteredText = textOf(await render(panel))
expect('B14 history filter empty state', filteredText.includes('该筛选下没有轮次'))
await click('分析')
const reviewText = textOf(await render(panel))
expect('B15a analysis sub-tabs exist', reviewText.includes('概览') && reviewText.includes('资金') && reviewText.includes('归因') && reviewText.includes('质量'), reviewText.slice(-400))
await click('资金')
const fundsText = textOf(await render(panel))
expect('B15b funds tab shows books and coverage', fundsText.includes('本地四本账') && fundsText.includes('覆盖范围'), fundsText.slice(-400))
expect('B15b2 funds tab renders an equity curve block', fundsText.includes('权益曲线'), fundsText.slice(-400))
expect('B15b3 funds tab offers time windows', fundsText.includes('统计窗口') && /1h/.test(fundsText) && /7d/.test(fundsText), fundsText.slice(-400))
await click('归因')
const attrText = textOf(await render(panel))
expect('B15c attribution tab never claims zero fills when fills exist', attrText.includes('决策到成交的归因') && !attrText.includes('无法归因'), attrText.slice(-400))
await click('质量')
const qualityText = textOf(await render(panel))
expect('B15d quality tab states the small-sample caveat', qualityText.includes('评分卡与实际结果') && qualityText.includes('样本量小'), qualityText.slice(-400))
await click('概览')
const reviewText2 = textOf(await render(panel))
expect('B15 review distribution + daily block', reviewText.includes('原因码构成') && reviewText.includes('AI 日报') && reviewText.includes('今日复盘未生成'), reviewText.slice(-600))
const reviewGets = () => requestLog.filter((entry) => entry.method !== 'POST' && entry.url.includes('/reviews/daily'))
const reviewPosts = () => requestLog.filter((entry) => entry.method === 'POST' && entry.url.includes('/reviews/daily'))
expect('R1 daily report GETs once on open, with the Shanghai date', reviewGets().length === 1 && /[?&]date=\d{4}-\d{2}-\d{2}/.test(reviewGets()[0].url) && reviewPosts().length === 0, JSON.stringify(reviewGets().map((entry) => entry.url)))
await click('生成复盘')
const generated = textOf(await render(panel))
expect('R2 generation posts the date', reviewPosts().length === 1 && JSON.parse(reviewPosts()[0].body).date.match(/^\d{4}-\d{2}-\d{2}$/) != null, String(reviewPosts()[0]?.body))
expect('R3 markdown renders headings, lists and text', generated.includes('2026-11-10 复盘') && generated.includes('轮次 3，成交 1') && generated.includes('本日无异常。') && !generated.includes('# 2026-11-10'), generated.slice(-600))
expect('R4 meta shows model and does not fake cache', generated.includes('fake-1') && !generated.includes('缓存'), generated.slice(-600))
flags.reviewState = 'conflict'
await click('重新生成')
const conflictText = textOf(await render(panel))
expect('R5 409 reads as already generating', conflictText.includes('正在生成，请稍候'), conflictText.slice(-400))
flags.reviewState = 'fail'
await click('重试')
const failedReview = textOf(await render(panel))
expect('R6 failure shows the code and a retry', failedReview.includes('生成失败（E_REVIEW_FAILED）') && failedReview.includes('重试'), failedReview.slice(-500))
flags.reviewState = 'missing'
await click('重试')
const missingReview = textOf(await render(panel))
expect('R7 retry after failure can fall back to not-generated', missingReview.includes('今日复盘未生成'), missingReview.slice(-400))
expect('R8 no automatic POSTs beyond the clicks', reviewPosts().length === 2, String(reviewPosts().length))
expect('B15b review has p50/p95 and timeline', reviewText.includes('耗时 p50') && reviewText.includes('时间线'), reviewText.slice(-600))
// 05 N06: attribution is data-driven — this fixture has exactly one trade round,
// so "0 成交" would be a lie.
expect('B16 review attribution follows the trade count', reviewText.includes('归因：成交 1 笔'), reviewText.slice(-400))

// ---- scenario C: system drawer -------------------------------------------
await click('更多')
await click('系统状态')
const systemText = textOf(await render(panel))
expect('C1 system drawer shows the ledger', systemText.includes('6512827bcdfe4250a1bb5710217e044d'))
expect('C2 system drawer shows auth + token source', systemText.includes('bearer') && systemText.includes('file'))
await click('关闭证据抽屉')
expect('C3 system drawer closes', findAll(currentTree, (node) => node.props?.role === 'dialog').length === 0)

// ---- scenario D: appearance preference -----------------------------------
await click('更多')
await click('红涨绿跌')
let rootNode = findAll(currentTree, (node) => node.tag === 'div' && String(node.props?.className ?? '') === 'twb-root')[0]
expect('D1 red-up preference switches the root', rootNode?.props?.['data-updown'] === 'red-up', String(rootNode?.props?.['data-updown']))
await click('只符号无色')
rootNode = findAll(currentTree, (node) => node.tag === 'div' && String(node.props?.className ?? '') === 'twb-root')[0]
expect('D2 mono preference switches the root', rootNode?.props?.['data-updown'] === 'mono', String(rootNode?.props?.['data-updown']))
await click('绿涨红跌（默认）')
rootNode = findAll(currentTree, (node) => node.tag === 'div' && String(node.props?.className ?? '') === 'twb-root')[0]
expect('D3 default preference restores green-up', rootNode?.props?.['data-updown'] === 'green-up', String(rootNode?.props?.['data-updown']))
await click('关闭')

// ---- scenario E: service shape matrix ------------------------------------
async function applyService(statePatch, healthPatch) {
  serverState = makeState({ service: statePatch })
  serverHealth = { ...HEALTH, ...(healthPatch ?? { service: { ...statePatch, state: 'connected' } }) }
  await click('更多')
  await click('刷新数据')
  await render(panel)
  const text = textOf(await render(panel))
  await click('关闭')
  return text
}
serverState = makeState({ service: SERVICE_STABLE })
serverHealth = { ...HEALTH, service: { ...SERVICE_STABLE, state: 'connected' } }
await click('更多')
await click('刷新数据')
let shapeText = textOf(await render(panel))
await click('关闭')
expect('E1 stable block: connected', shapeText.includes('已连接'))
const noTokenText = await applyService({ ...SERVICE_STABLE, token_present: false, token_configured: false })
expect('E2 missing token copy', noTokenText.includes('缺少服务 token'), noTokenText.slice(-200))
const tokenErrText = await applyService({ ...SERVICE_STABLE, token_error: 'unreadable: EACCES' })
expect('E3 unreadable token copy', tokenErrText.includes('token 缺失或不可读'), tokenErrText.slice(-200))
const authText = await applyService({ ...SERVICE_STABLE, error_status: 401, error_code: 'E_AUTH_REQUIRED' })
expect('E4 auth failure copy', authText.includes('鉴权失败'), authText.slice(-200))
const offlineText = await applyService({ ...SERVICE_STABLE, error_code: 'E_SERVICE_OFFLINE', ready: false })
expect('E5 offline copy', offlineText.includes('Host 未就绪') || offlineText.includes('服务不可达'), offlineText.slice(-200))
serverState = makeState()
serverHealth = HEALTH
await click('更多')
await click('刷新数据')
await click('关闭')
await render(panel)

// ---- scenario F: run guard + notices -------------------------------------
flags.runOk = false
await click('运行一轮')
let runText = textOf(await render(panel))
expect('F1 409 conflict notice', runText.includes('已有一轮在运行（409）'), runText.slice(-300))
flags.run403 = true
await click('运行一轮')
runText = textOf(await render(panel))
expect('F2 403 auth notice', runText.includes('鉴权失败'), runText.slice(-300))
flags.run403 = false
flags.runOk = true
await click('运行一轮')
runText = textOf(await render(panel))
expect('F3 success notice', runText.includes('本轮已结束') || runText.includes('已提交'), runText.slice(-300))
flags.runOk = false
flags.hangRun = true
const pendingRun = clickNoWait('运行一轮')
await render(panel)
let pendingText = textOf(await render(panel))
expect('F4 pending run disables the button', pendingText.includes('运行中'), pendingText.slice(0, 300))
fireTimers(400000)
await pendingRun
await render(panel)
runText = textOf(await render(panel))
expect('F5 timeout notice mentions RUN_TIMEOUT', runText.includes('RUN_TIMEOUT'), runText.slice(-300))
flags.hangRun = false

// ---- scenario G: refresh busy --------------------------------------------
flags.hangState = true
await click('更多')
clickNoWait('刷新数据')
await render(panel)
const refreshBtn = findButtonByAria('刷新数据')
expect('G1 refresh disabled while busy', Boolean(refreshBtn?.props?.disabled), String(refreshBtn?.props?.disabled))
expect('G2 refresh exposes aria-busy', String(refreshBtn?.props?.['aria-busy']) === 'true', String(refreshBtn?.props?.['aria-busy']))
releaseState()
await flush()
await render(panel)
flags.hangState = false
await click('关闭')
await render(panel)

// ---- scenario H: empty states --------------------------------------------
serverState = makeState({
  positions: [],
  orders: [],
  fills: [],
  reviews: [],
  account: ACCOUNT_EMPTY,
  equityCurve: EQUITY_EMPTY,
  budget: BUDGET_EMPTY,
  portfolio: PORTFOLIO_EMPTY_GAPS,
  lastRound: null,
  rounds: [],
})
serverState.positionsEmptyReason = 'E_POSITION_EMPTY'
serverState.ordersEmptyReason = 'E_ORDER_EMPTY'
serverState.fillsEmptyReason = 'E_FILL_EMPTY'
serverState.reviewsEmpty = { gap: 'E_NOT_FOUND', empty_reason: 'reviews unavailable (E_NOT_FOUND HTTP 404)' }
await click('更多')
await click('刷新数据')
await click('关闭')
await click('操作台')
const emptyText = textOf(await render(panel))
expect('H1 positions empty uses the sibling reason', emptyText.includes('当前无持仓') && emptyText.includes('E_POSITION_EMPTY'), emptyText.slice(0, 400))
expect('H2 orders/fills collapse to one row', emptyText.includes('订单 0 · 成交 0 · 展开查看'), emptyText.slice(0, 500))
expect('H3 no English portfolio gap leaks', !emptyText.includes('positions empty'), emptyText.slice(0, 400))
expect('H4 account empty shows the reason', emptyText.includes('无账户快照') && emptyText.includes('E_ACCOUNT_EMPTY'), emptyText.slice(0, 300))
expect('H5 no attention bar when nothing is pending', findAll(currentTree, (node) => String(node.props?.className ?? '').split(' ').includes('twb-s2')).length === 0, findAll(currentTree, (node) => String(node.props?.className ?? '').split(' ').includes('twb-s2')).map((n) => textOf(n).slice(0, 80)).join(' || '))
await click('展开查看')
const expandedText = textOf(await render(panel))
expect('H6 expanding shows order/fill empty reasons', expandedText.includes('当前无订单') && expandedText.includes('E_ORDER_EMPTY'), expandedText.slice(0, 500))
await click('对账')
const reconRequests = () => requestLog.filter((entry) => entry.method === 'POST' && entry.url.endsWith('/reconcile'))
const reconText = textOf(await render(panel))
expect('H7 reconciliation starts unrequested', reconText.includes('本地账本与交易所尚未核对') && reconText.includes('立即对账') && reconRequests().length === 0, reconText.slice(-400))
await click('立即对账')
const reconDone = textOf(await render(panel))
expect('H7b reconcile result is human, not JSON', reconDone.includes('保护缺失 1 个 · ETHUSDT') && reconDone.includes('1 / 0') && reconDone.includes('ledger-test') && !reconDone.includes('"outbox"'), reconDone.slice(-700))
expect('H7c reconcile receipt shows requestId and demo', reconDone.includes('req-test-recon') && reconDone.includes('Demo · 只读'), reconDone.slice(-500))
expect('H7d one click means one request', reconRequests().length === 1, String(reconRequests().length))
expect('H7d2 reconcile sends an empty body', reconRequests()[0].body === null, String(reconRequests()[0].body))
await click('重新对账')
await render(panel)
expect('H7e rerun is manual only', reconRequests().length === 2, String(reconRequests().length))
flags.reconFail = true
await click('重新对账')
const reconFailed = textOf(await render(panel))
expect('H7f failure shows code and retry', reconFailed.includes('对账失败（E_SERVICE_UNAVAILABLE）') && reconFailed.includes('重试'), reconFailed.slice(-500))
await click('重试')
await render(panel)
expect('H7g retry requests exactly once more', reconRequests().length === 4, String(reconRequests().length))
flags.reconFail = false
await click('重试')
await render(panel)
flags.reconStale = true
await click('重新对账')
const reconStale = textOf(await render(panel))
expect('H7h stale/unreachable reconcile never says consistent', !reconStale.includes('本地账本与交易所一致') && reconStale.includes('不可达') && reconStale.includes('已过期') && reconStale.includes('未解决查询'), reconStale.slice(-800))
flags.reconStale = false
await click('更多')
await click('系统状态')
const emptySystemText = textOf(await render(panel))
expect('H8 system drawer keeps the raw gaps', emptySystemText.includes('positions empty (E_POSITION_EMPTY)'), emptySystemText.slice(0, 500))
expect('H9 detailed risk panel replaces the old placeholder', emptySystemText.includes('资金与风险明细') && emptySystemText.includes('风控日亏损'))
await click('关闭证据抽屉')

// ---- scenario I: no-trade round + S2 aggregation -------------------------
serverState = makeState({
  lastRound: { ...NO_TRADE, freshness: { ...NO_TRADE.freshness, produced_at: new Date(Date.now() - 6 * 3600 * 1000).toISOString() } },
  rounds: [ROUND_SUMMARY, NO_TRADE_SUMMARY, OLDER],
})
await click('更多')
await click('刷新数据')
await click('关闭')
await click('AI 决策')
await click('本轮')
const noTradeText = textOf(await render(panel))
expect('I1 no-trade headline', noTradeText.includes('不交易'), noTradeText.slice(0, 400))
expect('I2 early-stop row collapses repeated stages', noTradeText.includes('未到达') && noTradeText.includes('NO_CANDIDATE'), noTradeText.slice(-500))
expect('I3 decision/risk empty copy', noTradeText.includes('未到达') || noTradeText.includes('未提供'))
const staleBanner = textOf(await render(panel))
expect('I4 stale evidence surfaces in S2', staleBanner.includes('还有 1 项') || staleBanner.includes('还有'), staleBanner.slice(0, 400))

// ---- scenario J: rejected round, no selected case, P1 rule hits -----------
const REJECTED = {
  ...ROUND,
  roundId: 'cyc-20261110T062000Z-b7c1d2',
  status: 'failed', outcome: 'no_trade', reasonCode: 'CONTROL_LOCKED',
  decision: { action: 'no_trade', selected_case: null, symbol: 'BTCUSDT', side: null, confidence: 0.55, score_override: null, no_trade_reason: 'risk too large' },
  scorecard: {
    grade: null, gate: null, gate_reasons: null, debate_quality: 'thin', tie: true, degraded: false,
    red: { grade: 'B', gate: 'FAIL_RR', gate_reasons: ['RR_TOO_LOW'] },
    blue: { grade: 'C', gate: 'PASS', gate_reasons: [] },
  },
  risk: {
    recommendation: 'REJECT', size_multiplier: null, max_risk_pct: 1, max_leverage: 10, concerns: ['risk too large'],
    violations: [{ rule: 'R05', action: 'reject', detail: 'risk too large' }], blocking_rule: 'R05',
  },
  nodes: ROUND.nodes.slice(0, 4),
}
ROUNDS_BY_ID[REJECTED.roundId] = REJECTED
serverState = makeState({
  lastRound: REJECTED,
  rounds: [{ ...ROUND_SUMMARY, roundId: REJECTED.roundId, status: 'failed', outcome: 'no_trade', reasonCode: 'CONTROL_LOCKED', startedAt: REJECTED.startedAt }, ROUND_SUMMARY, OLDER],
})
serverHealth = HEALTH
await click('更多')
await click('刷新数据')
await click('关闭')
await click('AI 决策')
await click('本轮')
const scorecardText = textOf(await render(panel))
expect('J1 rejected round reads red/blue when no case is selected', scorecardText.includes('本轮未选中案例') && scorecardText.includes('红队') && scorecardText.includes('蓝队'), scorecardText.slice(-900))
expect('J2 P1 violations render rule/action/detail', scorecardText.includes('R05') && scorecardText.includes('reject') && scorecardText.includes('risk too large'), scorecardText.slice(-600))
expect('J3 blocking rule is shown', scorecardText.includes('阻断规则') && scorecardText.includes('R05'))
expect('J4 no duplicated "未到达" blocks', (scorecardText.match(/未到达/g) ?? []).length === 1, String((scorecardText.match(/未到达/g) ?? []).length))

// ---- scenario O: market/envelope failure + envelope denial ---------------
flags.marketFail = true
flags.envelopeFail = true
await click('操作台')
await click('更多')
await click('刷新数据')
await click('关闭')
const failMarketText = textOf(await render(panel))
expect('O1 market failure is honest', failMarketText.includes('行情未提供') && !failMarketText.includes('未接入（P1）'), failMarketText.slice(0, 300))
expect('O2 envelope failure falls back to 上轮 caps', failMarketText.includes('上轮'), failMarketText.slice(0, 500))
flags.marketFail = false
flags.envelopeFail = false
flags.envelopeDeny = true
serverState = makeState({
  positions: [POSITIONS[0]],
  service: { ...SERVICE_STABLE, new_entry_allowed: true, control_state: 'normal' },
})
await click('更多')
await click('刷新数据')
await click('关闭')
const denyText = textOf(await render(panel))
expect('O3 envelope blockers appear as entry reasons', denyText.includes('CONTROL_CLOSE_ONLY') && denyText.includes('已阻断'), denyText.slice(0, 700))
flags.envelopeDeny = false

// ---- scenario K: EXECUTION_BLOCKED reason detail (t83) -------------------
const BLOCKED_DETAIL = '缺少 account equity/balance，无法计算 qty，拒绝发 naked order'
const BLOCKED = {
  ...ROUND,
  roundId: 'cyc-20261110T080000Z-b10c4e',
  status: 'failed',
  outcome: 'no_trade',
  reasonCode: 'EXECUTION_BLOCKED:OTHER',
  reasonDetail: BLOCKED_DETAIL,
  headline: '本轮失败：执行阻断（OTHER）',
  decision: { action: 'trade', selected_case: 'red', symbol: 'BTCUSDT', side: 'long', confidence: 0.61, score_override: null, no_trade_reason: null },
  execution: {
    executable: false, blocked_reason: 'ACCOUNT_UNAVAILABLE', symbol: 'BTCUSDT', side: 'long',
    order_plan: null, rationale: BLOCKED_DETAIL, p1: null, p2: null,
  },
  nodes: ROUND.nodes.slice(0, 4),
}
ROUNDS_BY_ID[BLOCKED.roundId] = BLOCKED
serverState = makeState({
  lastRound: BLOCKED,
  rounds: [
    { ...ROUND_SUMMARY, roundId: BLOCKED.roundId, status: 'failed', outcome: 'no_trade', reasonCode: BLOCKED.reasonCode, headline: BLOCKED.headline, startedAt: BLOCKED.startedAt },
    ROUND_SUMMARY,
    OLDER,
  ],
})
serverHealth = HEALTH
await click('操作台')
await click('更多')
await click('刷新数据')
await click('关闭')
const blockedText = textOf(await render(panel))
expect('K1 execution-blocked label carries the code', blockedText.includes('执行阻断（OTHER）'), blockedText.slice(0, 600))
expect('K2 execution-blocked never says unknown reason', !blockedText.includes('未知原因'))
expect('K3 blocked_reason is visible', blockedText.includes('执行阻断原因') && blockedText.includes('ACCOUNT_UNAVAILABLE'))
expect('K4 rationale is visible', blockedText.includes(BLOCKED_DETAIL))
await click('AI 决策')
await click('本轮')
const aiBlockedText = textOf(await render(panel))
expect('K5 AI report shows the same label and detail', aiBlockedText.includes('执行阻断（OTHER）') && aiBlockedText.includes(BLOCKED_DETAIL), aiBlockedText.slice(-600))

const BLOCKED_PLAN = {
  ...BLOCKED,
  roundId: 'cyc-20261110T080100Z-b10c4f',
  reasonCode: 'EXECUTION_BLOCKED:EXECUTION_PLAN_INVALID',
  reasonDetail: null,
  headline: '本轮失败：执行阻断（EXECUTION_PLAN_INVALID）',
  execution: { executable: false, blocked_reason: null, symbol: 'BTCUSDT', side: 'long', order_plan: null, rationale: null, p1: null, p2: null },
}
ROUNDS_BY_ID[BLOCKED_PLAN.roundId] = BLOCKED_PLAN
serverState = makeState({
  lastRound: BLOCKED_PLAN,
  rounds: [
    { ...ROUND_SUMMARY, roundId: BLOCKED_PLAN.roundId, status: 'failed', outcome: 'no_trade', reasonCode: BLOCKED_PLAN.reasonCode, headline: BLOCKED_PLAN.headline, startedAt: BLOCKED_PLAN.startedAt },
    ROUND_SUMMARY,
    OLDER,
  ],
})
await click('操作台')
await click('更多')
await click('刷新数据')
await click('关闭')
const noDetailText = textOf(await render(panel))
expect('K6 a blocked code without evidence shows no-detail copy', noDetailText.includes('执行阻断（EXECUTION_PLAN_INVALID）') && noDetailText.includes('暂无详细原因'), noDetailText.slice(0, 600))

// Existing mappings must not regress.
serverState = makeState({ lastRound: NO_TRADE, rounds: [NO_TRADE_SUMMARY, ROUND_SUMMARY, OLDER] })
await click('更多')
await click('刷新数据')
await click('关闭')
const noCandidateText = textOf(await render(panel))
expect('K7 existing NO_CANDIDATE mapping unchanged', noCandidateText.includes('没有合格标的') && !noCandidateText.includes('未知原因'))

// t86: NO_CANDIDATE detail carries reason + notes/warnings.
const NO_CANDIDATE_DETAILED = {
  ...NO_TRADE,
  roundId: 'cyc-20261110T080200Z-c4nd1d',
  reasonDetail: '波动率过高，且方向不明确',
  reasonNotes: ['BTC 1h ATR 扩至 2.1%', '资金费率转负', '流动性薄，滑点风险高'],
  analysis: {
    stand_aside: true,
    stand_aside_reason: '波动率过高，且方向不明确',
    market_read: { regime: 'CHOPPY', notes: ['BTC 1h ATR 扩至 2.1%', '资金费率转负'] },
    warnings: ['流动性薄，滑点风险高'],
  },
}
ROUNDS_BY_ID[NO_CANDIDATE_DETAILED.roundId] = NO_CANDIDATE_DETAILED
serverState = makeState({
  lastRound: NO_CANDIDATE_DETAILED,
  rounds: [
    { ...NO_TRADE_SUMMARY, roundId: NO_CANDIDATE_DETAILED.roundId, headline: NO_CANDIDATE_DETAILED.headline, startedAt: NO_CANDIDATE_DETAILED.startedAt },
    ROUND_SUMMARY,
    OLDER,
  ],
})
await click('更多')
await click('刷新数据')
await click('关闭')
const detailedNoCandidateText = textOf(await render(panel))
expect('K8 NO_CANDIDATE keeps its label', detailedNoCandidateText.includes('没有合格标的'))
expect('K8 NO_CANDIDATE shows the real detail', detailedNoCandidateText.includes('波动率过高，且方向不明确'), detailedNoCandidateText.slice(0, 700))
expect('K8 NO_CANDIDATE shows notes and warnings',
  ['BTC 1h ATR 扩至 2.1%', '资金费率转负', '流动性薄，滑点风险高'].every((note) => detailedNoCandidateText.includes(note)),
  detailedNoCandidateText.slice(0, 900))
const noteLists = findAll(currentTree, (node) => node.tag === 'ul' && String(node.props?.className ?? '').includes('twb-concl-notes'))
expect('K8 notes are capped at three items', noteLists.length === 1 && (noteLists[0].children ?? []).length === 3,
  `${noteLists.length}/${(noteLists[0]?.children ?? []).length}`)
expect('K8 NO_CANDIDATE never says unknown reason', !detailedNoCandidateText.includes('未知原因'))
await click('AI 决策')
await click('本轮')
const aiNoCandidateText = textOf(await render(panel))
expect('K9 AI report also shows NO_CANDIDATE notes',
  aiNoCandidateText.includes('波动率过高，且方向不明确') && aiNoCandidateText.includes('BTC 1h ATR 扩至 2.1%'),
  aiNoCandidateText.slice(-700))

// ---- live audit regressions: budget state and legacy Host order fields ----
ENVELOPE.limits.per_trade.max_leverage = 7
ENVELOPE.limits.portfolio = { max_open_positions: 1 }
serverState = makeState({
  orders: [{ orderId: 'audit-order', symbol: 'BTCUSDT', side: 'SELL', type: 'MARKET', status: 'FILLED', qty: null, raw: { qty_requested: '0.075' } }],
})
serverState.schedule = { enabled: true, rounds_last_24h: 96, max_rounds_per_day: 96, paused_until: null }
await click('更多')
await click('刷新数据')
await click('关闭')
await click('操作台')
await click('订单 1')
const budgetAuditText = textOf(await render(panel))
expect('audit daily budget exhaustion is visible', budgetAuditText.includes('已达 24 小时轮次上限'))
expect('audit budget exhaustion never claims scheduler is running normally', !budgetAuditText.includes('开（每 10 分钟）'))
expect('audit legacy Host orders recover requested quantity', budgetAuditText.includes('0.075'))
expect('audit removed old phase copy', !budgetAuditText.includes('Service 补字段，P1') && !budgetAuditText.includes('本阶段不提供下单 / 平仓 / 补挂'))
expect('audit nested service risk envelope supplies leverage cap', budgetAuditText.includes('7×'))
expect('audit nested service portfolio supplies max positions', budgetAuditText.includes('最大持仓') && budgetAuditText.includes('/ 1'))
expect('audit full position capacity blocks new entry in the UI', budgetAuditText.includes('已达持仓上限') && budgetAuditText.includes('已阻断'))

// ---- operator controls, history and explicit accounting scope ------------
serverState.performance = { equity_change: '12.34', realized_pnl: '10', commissions: { USDT: '1.25' }, unknown_commission_count: 0, net_realized_pnl: '8.75', baseline_at: '2026-11-10T16:03:00Z' }
ENVELOPE.limits.loss = { daily_loss_stop: 0.03, max_drawdown_from_peak: 0.08, consecutive_losses: 2 }
ENVELOPE.limits.portfolio.max_margin_usage = 0.6
ENVELOPE.risk_state = { daily_loss_ratio: 0.01, drawdown_ratio: 0.02, consecutive_losses: 1, day_start_equity: '5000', peak_equity: '5100' }
await click('更多')
await click('刷新数据')
await click('系统状态')
const detailAuditText = textOf(await render(panel))
expect('details display actual margin and risk thresholds', detailAuditText.includes('60.00%') && detailAuditText.includes('3.00%') && detailAuditText.includes('8.00%'))
expect('details display after-fee realized PnL with funding limitation', detailAuditText.includes('8.75') && detailAuditText.includes('不含资金费') && detailAuditText.includes('首个可用快照'))
expect('history shows durable request IDs and unknown outcome', detailAuditText.includes('req-audit-1234') && detailAuditText.includes('结果未确认，勿重复提交'))
const controlPosts = () => requestLog.filter((row) => row.method === 'POST' && row.url.endsWith('/control')).length
const beforeControl = controlPosts()
await click('暂停新开仓')
expect('control selection never submits', controlPosts() === beforeControl)
expect('control confirmation is disabled without reason and exact word', Boolean(findClickable('确认执行').props.disabled))
changeInput('原因', '测试暂停')
changeInput('输入大写状态词确认', 'PAUSE')
await render(panel)
const firstControl = clickNoWait('确认执行')
const duplicateControl = clickNoWait('确认执行')
await Promise.all([firstControl, duplicateControl])
await render(panel)
expect('control double click submits only once', controlPosts() === beforeControl + 1)
expect('control receipt is retained', textOf(currentTree).includes('控制态操作已完成') && textOf(currentTree).includes('req-'))
await click('关闭证据抽屉')
await click('更多')
await click('系统状态')
await click('紧急全平')
expect('emergency explains partial failure and market close', textOf(currentTree).includes('市价平掉所有 Demo 持仓') && textOf(currentTree).includes('可能部分失败'))
// 05 N08: emergency needs >= 8 characters and a deliberate second click.
changeInput('原因', '测试紧急处置演练')
changeInput('输入大写状态词确认', 'EMERGENCY')
await render(panel)
await click('确认执行')
expect('emergency first click only arms the confirmation', controlPosts() === beforeControl + 1, String(controlPosts()))
expect('emergency shows the second-click hint', textOf(currentTree).includes('再次点击确认（5 秒内有效）'), textOf(currentTree).slice(-400))
await click('确认执行')
expect('partial emergency never claims complete success', textOf(currentTree).includes('部分结果需人工核对') && !textOf(currentTree).includes('控制态操作已完成'))
await click('关闭证据抽屉')
await click('更多')
await click('系统状态')
await click('暂停新开仓')
changeInput('原因', '测试超时不重复')
changeInput('输入大写状态词确认', 'PAUSE')
await render(panel)
flags.controlHang = true
const beforeHang = controlPosts()
const hangingControl = clickNoWait('确认执行')
await render(panel)
expect('pending control disables drawer dismissal', Boolean(findButtonByAria('关闭证据抽屉').props.disabled))
await click('关闭证据抽屉')
expect('pending control keeps its drawer and request context', textOf(currentTree).includes('输入大写状态词确认'))
fireTimers(120000)
await hangingControl
await render(panel)
expect('control timeout keeps an unknown receipt without retry', controlPosts() === beforeHang + 1 && textOf(currentTree).includes('结果未确认；请查操作历史'))
flags.controlHang = false


// ---- scenario V: v2 P0 batch (N01/N02/N03/N04/N05/N06/N07/N09) -----------
// The Host DTO now carries debate / attribution / per-call errors / stages and
// the build identity; these fixtures are the shapes 01 §5.1 defines.
const FAILED_NODE = {
  ...node('risk', 1, 1, 45000, 2900, 0),
  id: 'run-v2:risk:r1:1', status: 'error', schema_ok: false, finish_kind: 'stop',
  error: 'LLM call exceeded 45000 ms',
  error_code: 'LLM_TIMEOUT',
  error_message: 'LLM call exceeded 45000 ms',
  validation_errors: [{ path: '$', message: 'LLM call exceeded 45000 ms', keyword: 'LLM_TIMEOUT' }],
}
const V2_ATTRIBUTION = {
  category: '链路', category_key: 'chain', stage: '风控 AI', stage_key: 'risk',
  role: '风控', role_key: 'risk',
  one_liner_zh: '风控 AI 未产出，按保守策略不交易',
  next_action_zh: '提高风控输出上限或降档；等待下一轮',
  reason_code: 'RISK_UNAVAILABLE', mapped: true,
}
const V2_BUILD = {
  buildId: 'twb-20261112T010000Z-deadbeef',
  configRevision: null,
  chain_version: 'ai-chain/1',
  loaded_at: '2026-11-12T01:00:00.000Z',
  prompt_set: 'prompts/deadbeef',
}
const V2_DEBATE = {
  mode: 'symmetric_two_round',
  requested_mode: 'symmetric_two_round',
  degraded: true,
  degrade_reason: 'DEBATE_ROUND2_DEGRADED',
  rounds: {
    r1: {
      red: {
        side: 'red', call_id: 'run-v2:red_team:r1:1', symbol: 'BTCUSDT', tradeable: true,
        confidence: 0.62, thesis: '红队论点：突破回踩且量能配合',
        evidence: [{ ref: 'ev-1', claim: 'taker buy 0.83', weight: 'high' }],
        rebuttal: [], opponent_steelman: '蓝队最强论点：区间上沿压力仍在',
        no_case_reason: null, plan_draft: null,
      },
      blue: null,
    },
    r2: { red: null, blue: null },
  },
  adopted: null,
}
const V2_ROUND = {
  ...ROUND,
  roundId: 'cyc-20261112T010000Z-v2v2v2',
  cycleId: 'cyc-20261112T010000Z-v2v2v2',
  status: 'failed',
  outcome: 'no_trade',
  reasonCode: 'RISK_UNAVAILABLE',
  reasonDetail: 'LLM call exceeded 45000 ms',
  headline: '本轮失败：风控 AI 未产出',
  decision: null,
  risk: null,
  execution: null,
  attribution: V2_ATTRIBUTION,
  debate: V2_DEBATE,
  build: V2_BUILD,
  stages: [
    { stage_key: 'analysis', label: '分析', status: 'ok', attempts: 1, started_at_ms: 1, finished_at_ms: 2, latency_ms: 9412, tokens: { in: 1824, out: 1717 }, error_code: null, calls: ['run-x:analysis:r1:1'] },
    { stage_key: 'risk', label: '风控', status: 'error', attempts: 1, started_at_ms: 3, finished_at_ms: 4, latency_ms: 45000, tokens: { in: 2900, out: 0 }, error_code: 'LLM_TIMEOUT', calls: ['run-v2:risk:r1:1'] },
  ],
  nodes: [...ROUND.nodes.slice(0, 5), FAILED_NODE],
}
V2_ROUND.aiCalls = V2_ROUND.nodes.map((item, index) => ({
  ...ROUND.aiCalls[index % ROUND.aiCalls.length],
  call_id: item.id, role: item.role, role_key: item.role_key, round: item.round, attempt: item.attempt,
  error_code: item.error_code ?? null, error_message: item.error_message ?? null,
  validation_errors: item.validation_errors ?? [],
}))
ROUNDS_BY_ID[V2_ROUND.roundId] = V2_ROUND
const V2_SUMMARY = { ...ROUND_SUMMARY, roundId: V2_ROUND.roundId, status: 'failed', outcome: 'no_trade', reasonCode: 'RISK_UNAVAILABLE', headline: V2_ROUND.headline, durationMs: 60000 }
serverState = makeState({ lastRound: V2_ROUND, rounds: [V2_SUMMARY, ROUND_SUMMARY, OLDER] })
serverState.llm = { provider: 'qmodel', model: 'qmodel-latest', transport: 'ctx.llm.stream' }
serverState.build = V2_BUILD
// 05 N05: the live Host keeps skipping on E_SERVICE_TIMEOUT; the footer must say so.
serverState.schedule = {
  enabled: true, interval_ms: 600000, rounds_last_24h: 12, max_rounds_per_day: 96, paused_until: null,
  last_result: { at: '2026-11-12T01:50:00.000Z', kind: 'skip', code: 'E_SERVICE_TIMEOUT', detail: 'preflight rejected', slot_id: 'auto-1', reason: null },
  history: [
    { at: '2026-11-12T01:50:00.000Z', kind: 'skip', code: 'E_SERVICE_TIMEOUT', detail: 'preflight rejected', slot_id: 'auto-1', reason: null },
    { at: '2026-11-12T01:40:00.000Z', kind: 'skip', code: 'E_SERVICE_TIMEOUT', detail: 'preflight rejected', slot_id: 'auto-0', reason: null },
    { at: '2026-11-12T01:30:00.000Z', kind: 'round', code: 'NO_CANDIDATE', detail: 'outcome=no_trade', slot_id: 'auto--1', reason: null },
  ],
}
serverState.service = { ...SERVICE_NESTED, ready: false, error_code: 'E_SERVICE_OFFLINE', last_run: { ...SERVICE_SUMMARY, ready: false, error_code: 'E_SERVICE_OFFLINE' } }
serverHealth = { ...HEALTH, build: V2_BUILD, service: { ...serverState.service, state: 'offline' } }
await click('操作台')
await click('更多')
await click('刷新数据')
await click('关闭')
const v2Cockpit = textOf(await render(panel))
expect('V1 service-down is an attention item', v2Cockpit.includes('服务不可达'), v2Cockpit.slice(0, 500))
expect('V2 "还有 N 项" is a real control', Boolean(findClickable('还有')), v2Cockpit.slice(0, 400))
await click('还有')
const v2PendingText = textOf(await render(panel))
expect('V3 pending list opens with every item', v2PendingText.includes('待处理清单') && v2PendingText.includes('自动调度连续跳过'), v2PendingText.slice(-800))
expect('V4 schedule skip reason is readable, not "开"', v2PendingText.includes('跳过：交易服务响应超时') && !v2PendingText.includes('开（每 10 分钟）'), v2PendingText.slice(-600))
await click('关闭')
await click('AI 决策')
await click('本轮')
const v2AiText = textOf(await render(panel))
expect('V5 failure attribution card is readable without expanding',
  v2AiText.includes('失败归因') && v2AiText.includes('链路') && v2AiText.includes('风控 AI 未产出，按保守策略不交易') && v2AiText.includes('提高风控输出上限或降档'),
  v2AiText.slice(-900))
expect('V6 per-call error names the code and the message',
  v2AiText.includes('失败调用') && v2AiText.includes('模型调用超时（LLM_TIMEOUT）') && v2AiText.includes('LLM call exceeded 45000 ms'),
  v2AiText.slice(-900))
expect('V7 debate shows both sides and the degradation banner',
  v2AiText.includes('辩论双侧') && v2AiText.includes('红队论点：突破回踩且量能配合') && v2AiText.includes('本轮辩论已降级'),
  v2AiText.slice(-900))

// 05 N02 / N09: drawer shows the three identity fields and two route rows.
await click('更多')
await click('系统状态')
const v2System = textOf(await render(panel))
expect('V8 build identity shows buildId + unsaved configRevision + chain version',
  v2System.includes('加载身份') && v2System.includes(V2_BUILD.buildId) && v2System.includes('未保存') && v2System.includes('ai-chain/1'),
  v2System.slice(-900))
// G2 (t6): the executed route has ONE source. The live/process default is never
// rendered as if it ran, and it is not shown as a second "caliber" either.
expect('V9 route rows have one source and never present the live default as executed',
  v2System.includes('执行路由（本轮冻结）') && v2System.includes('尚未执行') &&
    v2System.includes('路由来源（唯一事实）') && !v2System.includes('Harness 当前默认') &&
    !v2System.includes('已保存角色默认'),
  v2System.slice(-900))
expect('V10 schedule detail lists the last skip reason', v2System.includes('自动调度明细') && v2System.includes('交易服务响应超时'), v2System.slice(-900))
await click('关闭证据抽屉')

// 05 N07: an unrecorded protection state blocks and keeps its raw code.
serverState = makeState({ lastRound: V2_ROUND, rounds: [V2_SUMMARY, ROUND_SUMMARY, OLDER] })
serverState.service = SERVICE_NESTED
serverState.schedule = null
serverState.positions = [
  { ...POSITIONS[0], protection: 'MISMATCH', protection_ok: false },
]
await click('操作台')
await click('更多')
await click('刷新数据')
await click('关闭')
const unknownProt = textOf(await render(panel))
expect('V11 an unrecorded protection state is unknown (blocking) with its raw code',
  unknownProt.includes('未知（阻断）：MISMATCH') && /新开仓\s*已阻断/.test(unknownProt),
  JSON.stringify(unknownProt.match(/.{0,40}阻断.{0,40}/g)))

// 05 N09: detail failure keeps the selected round and offers both exits.
serverState = makeState({ lastRound: ROUND, rounds: [ROUND_SUMMARY, NO_TRADE_SUMMARY, OLDER] })
await click('更多')
await click('刷新数据')
await click('关闭')
await click('AI 决策')
await click('本轮')
flags.roundDetailFail = true
const roundOptions = findAll(currentTree, (node) => node.tag === 'button' && node.props?.role === 'option')
runtime.current = null
await Promise.resolve(roundOptions[1].props.onClick())
await flush()
await render(panel)
const detailFailText = textOf(await render(panel))
expect('V12 failed detail never swaps to the newest round',
  detailFailText.includes('该轮详情读取失败') && detailFailText.includes('回最新轮'),
  detailFailText.slice(-700))
// F5: the old assertion only looked at the suffix, which is also present when
// the header wrongly shows the newest round. Assert the header content itself.
const aiHeaderNode = findAll(currentTree, (node) => String(node.props?.className ?? '') === 'twb-ai-meta')[0]
const aiHeaderText = textOf(aiHeaderNode)
expect('V13 header is anchored to the selected round, not the newest',
  aiHeaderText.includes('#a9e545') && !aiHeaderText.includes('#06c625') && aiHeaderText.includes('该轮详情读取失败'),
  `${aiHeaderText} || ${detailFailText.slice(0, 300)}`)
await click('回最新轮')
flags.roundDetailFail = false
expect('V14 back-to-latest clears the error block', !textOf(await render(panel)).includes('该轮详情读取失败'))

// F1 follow-up: while the selected detail is still in flight, neither the header
// nor the body may show the newest round's facts.
flags.hangRoundDetail = true
runtime.current = null
await Promise.resolve(roundOptions[1].props.onClick())
await flush()
await render(panel)
const pendingHeaderNode = findAll(currentTree, (node) => String(node.props?.className ?? '') === 'twb-ai-meta')[0]
const pendingHeaderText = textOf(pendingHeaderNode)
const pendingBodyText = textOf(await render(panel))
expect('V19 in-flight detail keeps the selected id in the header',
  pendingHeaderText.includes('#a9e545') && pendingHeaderText.includes('正在连接…') && !pendingHeaderText.includes('#06c625'),
  pendingHeaderText)
expect('V19b in-flight detail never renders the newest round in the body',
  pendingBodyText.includes('正在连接…') && !pendingBodyText.includes('交易：做多 BTCUSDT'),
  pendingBodyText.slice(0, 400))
flags.hangRoundDetail = false
// The error block's "回最新轮" is not on screen in the loading state; use the
// round list, which selects the newest round explicitly.
const freshOptions = findAll(currentTree, (node) => node.tag === 'button' && node.props?.role === 'option')
runtime.current = null
await Promise.resolve(freshOptions[0].props.onClick())
await flush()
await render(panel)
const backHeaderNode = findAll(currentTree, (node) => String(node.props?.className ?? '') === 'twb-ai-meta')[0]
expect('V20 selecting the newest round restores its header', textOf(backHeaderNode).includes('#06c625'), textOf(backHeaderNode))


// ---- R1 (08 §6): switching between already-loaded rounds -----------------
// A = NO_TRADE (a9e545) is loaded first; B = OLDER (f5d1dc) and C = a new round
// (cccccc) never return. useJson keeps the previous payload while the new key is
// in flight, so the stale A payload must never be presented as B or C.
const R1_C_SUMMARY = {
  ...ROUND_SUMMARY,
  roundId: 'cyc-20261109T043000Z-cccccc',
  startedAt: '2026-11-09T04:30:00.000Z',
  status: 'no_trade', outcome: 'no_trade', reasonCode: 'NO_CANDIDATE',
  headline: '无交易：NO_CANDIDATE',
}
serverState = makeState({ lastRound: ROUND, rounds: [ROUND_SUMMARY, NO_TRADE_SUMMARY, OLDER, R1_C_SUMMARY] })
serverState.service = SERVICE_NESTED
serverState.schedule = null
serverHealth = HEALTH
await click('更多')
await click('刷新数据')
await click('关闭')
const aiHeaderTextOf = () => textOf(findAll(currentTree, (node) => String(node.props?.className ?? '') === 'twb-ai-meta')[0])
async function pickRound(index) {
  const options = findAll(currentTree, (node) => node.tag === 'button' && node.props?.role === 'option')
  runtime.current = null
  await Promise.resolve(options[index].props.onClick())
  await flush()
  await render(panel)
}
await pickRound(1)
const r1HeaderA = aiHeaderTextOf()
const r1BodyA = textOf(await render(panel))
expect('R1a a fully loaded round shows its own id in header and body',
  r1HeaderA.includes('#a9e545') && r1BodyA.includes('#a9e545'),
  `${r1HeaderA} || ${r1BodyA.slice(0, 240)}`)

flags.hangRoundDetail = true
await pickRound(2)
const r1HeaderB = aiHeaderTextOf()
const r1BodyB = textOf(await render(panel))
expect('R1b header follows the newly selected round while its detail is in flight',
  r1HeaderB.includes('#f5d1dc') && !r1HeaderB.includes('#a9e545'),
  r1HeaderB)
expect('R1c body shows the loading state of the selection, never the previously loaded round',
  r1BodyB.includes('正在连接…') && r1BodyB.includes('#f5d1dc') && !r1BodyB.includes('#a9e545'),
  r1BodyB.slice(0, 400))

await pickRound(3)
const r1HeaderC = aiHeaderTextOf()
const r1BodyC = textOf(await render(panel))
expect('R1d a second switch still follows the selection (C, not A/B)',
  r1HeaderC.includes('#cccccc') && !r1HeaderC.includes('#a9e545') && !r1HeaderC.includes('#f5d1dc'),
  r1HeaderC)
expect('R1e body for C is C\'s loading state, not an older round',
  r1BodyC.includes('正在连接…') && r1BodyC.includes('#cccccc') && !r1BodyC.includes('#a9e545') && !r1BodyC.includes('#f5d1dc'),
  r1BodyC.slice(0, 400))
flags.hangRoundDetail = false

// 05 N04: a running round never borrows the previous stage name.
serverState = makeState({
  lastRound: { ...ROUND, status: 'running', outcome: null, reasonCode: null, headline: '运行中' },
  rounds: [ROUND_SUMMARY, NO_TRADE_SUMMARY, OLDER],
})
await click('操作台')
await click('更多')
await click('刷新数据')
await click('关闭')
const runningText = textOf(await render(panel))
expect('V15 running state says submitted + waited, never the previous stage',
  runningText.includes('已提交 · 已等待') && !runningText.includes('正在执行'), runningText.slice(0, 700))

// 05 P1-2: a structured in_flight object names the live stage + elapsed time.
serverState = makeState({
  lastRound: { ...ROUND, status: 'running', outcome: null, reasonCode: null, headline: '运行中' },
  rounds: [ROUND_SUMMARY, NO_TRADE_SUMMARY, OLDER],
})
serverState.in_flight = {
  roundId: ROUND.roundId,
  round_id: ROUND.roundId,
  stage_key: 'decision',
  stage_label: '决策',
  elapsed_ms: 133000,
  stage_elapsed_ms: 12000,
  attempt: 1,
  max_attempts: 2,
  calls_done: 6,
  calls_budget: 14,
  roles_done: ['analysis', 'red_opening', 'blue_opening', 'red_response', 'blue_response'],
}
await click('更多')
await click('刷新数据')
await click('关闭')
const stageText = textOf(await render(panel))
const runningStageNode = findAll(currentTree, (node) => node.props?.['data-stage'] !== undefined)[0]
expect('V21 in_flight object names the live stage and elapsed clock',
  stageText.includes('决策 · 已耗时 2:13 / 上限 5:40') && stageText.includes('调用 6/14 · 尝试 1/2') &&
    runningStageNode?.props?.['data-stage'] === 'decision' && !stageText.includes('正在执行'),
  `${stageText.slice(0, 400)} || data-stage=${runningStageNode?.props?.['data-stage']}`)
serverState.in_flight.elapsed_ms = 138000
serverState.in_flight.stage_key = 'risk'
serverState.in_flight.stage_label = '风控'
await click('更多')
await click('刷新数据')
await click('关闭')
const stageText2 = textOf(await render(panel))
expect('V22 the stage and clock follow the next poll',
  stageText2.includes('风控 · 已耗时 2:18 / 上限 5:40'),
  stageText2.slice(0, 500))

// Compatibility: a legacy Host sends a string; an unknown stage falls back.
serverState.in_flight = ROUND.roundId
await click('更多')
await click('刷新数据')
await click('关闭')
const legacyText = textOf(await render(panel))
expect('V23 a string in_flight falls back to the N04 wording',
  legacyText.includes('已提交 · 已等待') && !legacyText.includes('上限 5:40 · '),
  legacyText.slice(0, 500))
serverState.in_flight = { roundId: ROUND.roundId, stage_key: 'mystery_stage' }
await click('更多')
await click('刷新数据')
await click('关闭')
const unknownStageText = textOf(await render(panel))
expect('V24 an unknown stage (no label) falls back to submitted + waited',
  unknownStageText.includes('已提交 · 已等待') && !unknownStageText.includes('mystery_stage'),
  unknownStageText.slice(0, 500))

// 05 N03: an unrecorded code never falls back to "未知原因".
serverState = makeState({
  lastRound: { ...ROUND, status: 'failed', outcome: 'no_trade', reasonCode: 'E_TOTALLY_NEW_CODE', headline: '本轮失败' },
  rounds: [ROUND_SUMMARY, NO_TRADE_SUMMARY, OLDER],
})
await click('更多')
await click('刷新数据')
await click('关闭')
const unmappedText = textOf(await render(panel))
expect('V16 unmapped code shows "未收录原因码 {code}", never 未知原因',
  unmappedText.includes('未收录原因码 E_TOTALLY_NEW_CODE') && !unmappedText.includes('未知原因'), unmappedText.slice(0, 700))

// F2: `E_SERVICE_HTTP_<n>` is a documented 03 §8.6 wildcard, not "unmapped".
serverState = makeState({
  lastRound: {
    ...ROUND, roundId: 'cyc-20261112T020000Z-http5o3', status: 'failed', outcome: 'no_trade',
    reasonCode: 'E_SERVICE_HTTP_503', headline: '本轮失败',
  },
  rounds: [ROUND_SUMMARY, NO_TRADE_SUMMARY, OLDER],
})
await click('更多')
await click('刷新数据')
await click('关闭')
const http503Text = textOf(await render(panel))
expect('V18 E_SERVICE_HTTP_503 renders the documented Chinese label',
  http503Text.includes('交易服务 HTTP 503') && !http503Text.includes('未收录原因码 E_SERVICE_HTTP_503'),
  http503Text.slice(0, 700))

// 05 N06: empty window branch.
serverState = makeState({ lastRound: null, rounds: [] })
await click('更多')
await click('刷新数据')
await click('关闭')
await click('AI 决策')
await click('分析')
const emptyReview = textOf(await render(panel))
expect('V17 attribution says the window has no rounds', emptyReview.includes('归因：该窗口无轮次'), emptyReview.slice(-500))
serverState = makeState()
await click('操作台')
await click('更多')
await click('刷新数据')
await click('关闭')


// ---- scenario CFG: 05 P1-1 model / effort configuration drawer ------------
flags.catalogUnavailable = false
flags.configSaveReject = false
savedConfig = null
serverState = makeState()
serverHealth = HEALTH
await click('AI 决策')
await click('模型配置')
const cfgText = textOf(await render(panel))
expect('CFG1 drawer opens with revision, presets and the default route',
  cfgText.includes('模型配置') && cfgText.includes('修订 1') && cfgText.includes('平衡') && cfgText.includes('省钱') && cfgText.includes('最强') && cfgText.includes('DeepSeek-Flash'),
  cfgText.slice(0, 600))
expect('CFG2 impact preview is the serial stage sum with a retry margin',
  cfgText.includes('预计墙钟 ∈ [310s, 340s]') && cfgText.includes('重试余量 30s'),
  cfgText.slice(-500))
const selectValue = (label) => findAll(currentTree, (node) => node.tag === 'select' && String(node.props?.['aria-label'] ?? '').includes(label))[0]?.props?.value
expect('CFG3 symmetric debate is a single locked row (P-01=A)',
  cfgText.includes('红蓝辩论（对称）') && !cfgText.includes('红队·开场 模型') && selectValue('红蓝辩论（对称） 模型') === 'qoder/dfmodel',
  String(selectValue('红蓝辩论（对称） 模型')))
await click('最强')
const presetText = textOf(await render(panel))
expect('CFG4 the strongest preset fills the role rows',
  selectValue('决策 模型') === 'qoder/qmodel_latest' && selectValue('决策 强度') === 'xhigh' && selectValue('操盘手 模型') === 'qoder/dfmodel',
  `${selectValue('决策 模型')}/${selectValue('决策 强度')}`)
expect('CFG4b the preset chip follows the draft', presetText.includes('预设 strongest'), presetText.slice(0, 300))
changeSelect('分析 模型', 'qoder/qmodel_latest')
await render(panel)
expect('CFG5 a per-role model override is reflected', selectValue('分析 模型') === 'qoder/qmodel_latest', String(selectValue('分析 模型')))
const asymToggle = findAll(currentTree, (node) => node.tag === 'input' && String(node.props?.['aria-label'] ?? '') === '允许红蓝异构（P-01=B）')[0]
runtime.current = null
asymToggle.props.onChange({ target: { checked: true } })
await render(panel)
const asymText = textOf(await render(panel))
expect('CFG6 asymmetric mode splits the debate rows and warns',
  asymText.includes('辩论将不对称') && selectValue('红队·开场 模型') !== undefined && selectValue('蓝队·开场 模型') !== undefined,
  asymText.slice(-500))
await click('保存配置')
const savePost = requestLog.filter((entry) => entry.method === 'POST' && entry.url.endsWith('/llm/config')).pop()
const saveBody = JSON.parse(savePost?.body || '{}')
expect('CFG7 save posts the draft to the plugin-local route',
  Boolean(savePost) && saveBody.config?.roleRoutes?.analysis?.model === 'qmodel_latest' && saveBody.config?.debateAsymmetric === true,
  JSON.stringify(saveBody.config?.roleRoutes?.analysis))
expect('CFG7b the drawer confirms the new revision applies next round',
  textOf(await render(panel)).includes('已保存 rev 2 · 下一轮生效'),
  textOf(await render(panel)).slice(-400))
flags.configSaveReject = true
await click('保存配置')
const rejectText = textOf(await render(panel))
expect('CFG8 a rejected save names the unsupported effort (no silent mapping)',
  rejectText.includes('保存失败：') && rejectText.includes('UNSUPPORTED_REASONING_EFFORT'),
  rejectText.slice(-500))
flags.configSaveReject = false
flags.catalogUnavailable = true
await click('刷新目录')
const downText = textOf(await render(panel))
const saveBtn = findClickable('保存配置')
expect('CFG9 an unavailable catalog shows the banner and disables save',
  downText.includes('目录暂不可用') && saveBtn?.props?.disabled === true,
  JSON.stringify({ hasBanner: downText.includes('目录暂不可用'), hasSave: Boolean(saveBtn), disabled: saveBtn?.props?.disabled, tail: downText.slice(-400) }))
// The AI page's model entry now opens the settings 自动化 group, so its close
// control is the settings one (there is no second model drawer any more).
await click('关闭设置')
flags.catalogUnavailable = false

// The system drawer keeps the three route facts side by side (02 §3.4).
serverState = makeState()
serverState.llm_config = {
  revision: 2,
  banner: null,
  default: { provider: 'qoder', model: 'dfmodel', effort: 'high' },
  last_route: { configRevision: 2, resolvedAt: '2026-11-12T05:30:00.000Z', default: { provider: 'qoder', model: 'dfmodel', requestedEffort: 'high', effectiveEffort: 'high', effortSource: 'roleRoutes' } },
}
await click('操作台')
await click('更多')
await click('刷新数据')
await click('关闭')
await click('更多')
await click('系统状态')
const sysCfgText = textOf(await render(panel))
// G2 (t6): one fact for "which model ran this round" (the round snapshot), the
// config revision that belongs to it, and an explicit source-difference note when
// the process default disagrees. The old three-caliber block is gone.
expect('CFG10 the drawer reports one executed route plus an explicit source difference',
  sysCfgText.includes('执行路由（本轮冻结）') && sysCfgText.includes('qoder / dfmodel') &&
    sysCfgText.includes('路由来源（唯一事实）') && sysCfgText.includes('持久化轮次快照') &&
    sysCfgText.includes('配置修订 2') &&
    !sysCfgText.includes('已保存角色默认') && !sysCfgText.includes('Harness 当前默认') &&
    sysCfgText.includes('来源差异：进程默认路由（未参与本轮）') &&
    sysCfgText.includes('本轮以上面的快照为准'),
  sysCfgText.slice(-700))
await click('关闭证据抽屉')


// ---- scenario P13: 05 P1-3 interaction / infrastructure convergence --------
flags.roundDetailNoId = false
const P13_ROUND = {
  ...ROUND, status: 'failed', outcome: 'no_trade', reasonCode: 'DEBATE_INCOMPLETE', headline: '本轮失败',
  reasonDetail: null,
  reasonParams: { kind: 'debate_incomplete', role: 'red_opening', error_code: 'LLM_TIMEOUT', timeout_ms: 40000 },
  reasonRaw: 'R1 opening failed: LLM call exceeded 40000 ms',
}
ROUNDS_BY_ID[ROUND.roundId] = P13_ROUND
serverState = makeState({
  lastRound: P13_ROUND,
  rounds: [ROUND_SUMMARY, NO_TRADE_SUMMARY, OLDER],
})
serverHealth = HEALTH
await click('AI 决策')
await click('本轮')
// Force a URL change so the detail fetch re-runs (the harness reuses state).
await pickRound(2)
await pickRound(0)
await flush()
await render(panel)
await flush()
const p13Text = textOf(await render(panel))
expect('P13a a Host failure renders a Chinese sentence from code+params',
  p13Text.includes('红队·开场 调用超时（40s）') && !p13Text.includes('R1 opening failed: LLM call exceeded 40000 ms ·'),
  p13Text.slice(0, 700))
const p13Parents = new Map()
const p13Walk = (tree) => {
  if (!tree || typeof tree !== 'object') return
  if (Array.isArray(tree)) { for (const item of tree) p13Walk(item); return }
  const children = Array.isArray(tree.children) ? tree.children : (tree.children ? [tree.children] : [])
  for (const child of children) {
    if (child && typeof child === 'object') { p13Parents.set(child, tree); p13Walk(child) }
  }
}
p13Walk(currentTree)
const p13Inside = (node, tag) => {
  let cursor = node
  while (cursor) { if (cursor.tag === tag) return true; cursor = p13Parents.get(cursor) }
  return false
}
const p13RawInDetail = findAll(currentTree, (node) => String(node.props?.className ?? '').split(' ').includes('twb-concl-detail') && textOf(node).includes('LLM call exceeded 40000 ms'))
expect('P13b the English engine string lives only in the 原文 fold',
  p13RawInDetail.length > 0 && p13RawInDetail.every((node) => p13Inside(node, 'details')) && p13Text.includes('原文（宿主工程串）'),
  JSON.stringify({ detailNodes: p13RawInDetail.length, folded: p13RawInDetail.map((node) => p13Inside(node, 'details')) }))
const p13S2 = findAll(currentTree, (node) => String(node.props?.className ?? '').split(' ').includes('twb-s2'))[0]
expect('P13c the blocking banner is announced (role=alert)',
  p13S2?.props?.role === 'alert' && p13S2?.props?.['aria-live'] === 'assertive',
  JSON.stringify(p13S2?.props ?? {}))

flags.roundDetailNoId = true
await click('本轮')
await pickRound(1)
const p13NoIdText = textOf(await render(panel))
expect('P13d a detail payload without roundId is diagnosable, not endless loading',
  p13NoIdText.includes('E_ROUND_ID_MISSING') && !p13NoIdText.includes('正在连接…'),
  p13NoIdText.slice(0, 600))
flags.roundDetailNoId = false

await click('操作台')
const p13Cockpit = textOf(await render(panel))
expect('P13e the day scope is labelled Beijing time',
  p13Cockpit.includes('今日 AI 摘要（北京时间）'), p13Cockpit.slice(0, 400))
await click('更多')
await click('系统状态')
const p13System = textOf(await render(panel))
expect('P13f the risk day and drawdown are explicitly UTC',
  p13System.includes('风控日亏损 / 阈值（UTC）') && p13System.includes('峰值回撤 / 阈值（UTC）'),
  p13System.slice(-900))
await click('关闭证据抽屉')
// ---- scenario F1: status / stop semantics / freshness / loaded version ----
// §2.1 four questions, §2.2 one main hint per fault, §3.2 stop = scheduler off +
// service pause (never emergency), §5.1 release identity vs load time.
function automationFixture(overrides = {}) {
  return {
    contract: 'trading-workbench/automation/1',
    state: 'running',
    reason_code: null,
    scheduler_enabled: true,
    scheduler_supported: true,
    last_success_at: new Date(Date.now() - 7 * 60 * 1000).toISOString(),
    next_at: new Date(Date.now() + 3 * 60 * 1000).toISOString(),
    interval_ms: 600000,
    last_result: { at: new Date(Date.now() - 7 * 60 * 1000).toISOString(), kind: 'round', code: 'NO_CANDIDATE' },
    paused_until: null,
    pause_reason: null,
    service_control: { state: 'normal', reason: 'default', updated_at: new Date().toISOString(), updated_by: 'operator', confirmed: true },
    stop: null,
    checked_at: new Date().toISOString(),
    ...overrides,
  }
}

const F1_HOST = {
  data_dir: 'C:\\Users\\Tao\\Desktop\\币安\\trading-workbench\\data',
  service_base_url: 'http://127.0.0.1:8791',
  environment: { venue: 'demo', demo_only: true, source: 'service-guard' },
  release: { release_id: 'twb-0.1.0-c44db84a30cd', source: 'manifest', source_digest: 'sha256:c44db84a30cdbeba753214f27bb5ad3708bc0abb35256f938b2651f628d7b928', generated_at: '2026-11-14T00:00:00.000Z' },
  loaded_at: new Date(Date.now() - 3600 * 1000).toISOString(),
}

serverState = makeState({
  automation: automationFixture(),
  host: F1_HOST,
  lastRound: { ...ROUND, freshness: { ...ROUND.freshness, produced_at: new Date(Date.now() - 8000).toISOString() } },
  rounds: [ROUND_SUMMARY, NO_TRADE_SUMMARY, OLDER],
})
const F1_BUILD = {
  buildId: 'twb-20261113T164633Z-2d449fc4',
  configRevision: 2,
  chain_version: 'ai-chain/1',
  loaded_at: new Date(Date.now() - 3600 * 1000).toISOString(),
  prompt_set: 'prompts/2d449fc4',
  release_id: 'twb-0.1.0-c44db84a30cd',
  release_source: 'manifest',
  release_source_digest: 'sha256:c44db84a30cdbeba753214f27bb5ad3708bc0abb35256f938b2651f628d7b928',
  release_generated_at: '2026-11-14T00:00:00.000Z',
  plugin_version: '0.1.0',
}
serverState.build = { ...F1_BUILD }
serverHealth = { ...HEALTH, build: serverState.build }
flags.hangAutomation = false
flags.automationPhase = 'stopped'
const f1LogStart = requestLog.length
await click('操作台')
await click('更多')
await click('刷新数据')
await click('关闭')
const f1Running = textOf(await render(panel))
expect('F1a the top bar answers "which environment + account"',
  f1Running.includes('模拟盘') && f1Running.includes('6512827b'), f1Running.slice(0, 260))
expect('F1b automation state is visible, not inferred',
  f1Running.includes('自动运行中') && Boolean(findClickable('停止自动交易')), f1Running.slice(0, 320))
expect('F1c last success and next run are shown next to the state',
  f1Running.includes('最近成功') && f1Running.includes('下次执行'),
  f1Running.slice(0, 400))
const f1StopButton = findAll(currentTree, (node) => node.tag === 'button' && node.props?.['data-auto-action'] === 'stop')[0]
expect('F1d the stop action is a real, focusable button with a label',
  Boolean(f1StopButton) && f1StopButton.props?.['aria-label'] === '停止自动交易' && f1StopButton.props?.disabled !== true,
  JSON.stringify({ aria: f1StopButton?.props?.['aria-label'], disabled: f1StopButton?.props?.disabled }))

// Pressure the stop: while the Host is still answering, only 正在停止 may show.
flags.hangAutomation = true
clickNoWait('停止自动交易')
await render(panel)
const f1Stopping = textOf(await render(panel))
expect('F1e the stop has a progress state (正在停止) before any confirmation',
  f1Stopping.includes('正在停止'), f1Stopping.slice(0, 320))
expect('F1f a stop in flight cannot be clicked twice',
  findAll(currentTree, (node) => node.tag === 'button' && node.props?.['data-auto-action'] === 'stop')[0]?.props?.disabled === true)
releaseAutomation()
await flush()
flags.hangAutomation = false
await render(panel)
const f1Stopped = textOf(await render(panel))
expect('F1g the chip only says 已停止自动交易 after the Host confirms the service pause',
  f1Stopped.includes('已停止自动交易') && f1Stopped.includes('服务端已确认暂停'), f1Stopped.slice(0, 400))
expect('F1h the stop notice says positions and protection orders are untouched',
  f1Stopped.includes('持仓与交易所保护单保持不变'), f1Stopped.slice(-500))
const f1StopPost = requestLog.filter((entry) => entry.method === 'POST' && entry.url.endsWith('/automation/stop')).pop()
const f1StopBody = JSON.parse(f1StopPost?.body || '{}')
expect('F1i the client stops through the Host automation route only',
  Boolean(f1StopPost) && typeof f1StopBody.reason === 'string' && typeof f1StopBody.request_id === 'string',
  JSON.stringify(f1StopBody))
const f1ForbiddenPosts = requestLog.slice(f1LogStart).filter((entry) => {
  if (entry.method !== 'POST') return false
  const body = JSON.parse(entry.body || '{}')
  if (body.state === 'emergency' || body.confirm === 'EMERGENCY') return true
  if (/\/control$/.test(entry.url)) return true
  return entry.url.includes('/positions/close') || entry.url.includes('/execution/order')
})
expect('F1j the stop never posts emergency and never closes a position',
  f1ForbiddenPosts.length === 0
    && !requestLog.slice(f1LogStart).some((entry) => entry.method === 'POST' && entry.url.includes('/positions/close')),
  JSON.stringify(requestLog.slice(f1LogStart).filter((entry) => entry.method === 'POST').map((entry) => entry.url)))
expect('F1k after the stop the primary action flips to 启动自动交易',
  Boolean(findClickable('启动自动交易')) && !findClickable('停止自动交易'), f1Stopped.slice(0, 260))
expect('F1l a deliberate stop does not raise a second control banner',
  !f1Stopped.includes('影响：新开仓被规则阻断'), f1Stopped.slice(0, 400))

// A refresh (fresh page load) must not lose the stopped state: it comes from
// /state.automation (service control row), not from local UI state.
// A fresh mount is exactly what a page refresh does: no component memory, only
// /state. The stop must still be there, because it lives in the service row.
runtime.instances.clear()
await render(panel)
const f1Reloaded = textOf(await render(panel))
expect('F1m a refresh keeps showing 已停止自动交易 (service control is the source)',
  f1Reloaded.includes('已停止自动交易'), f1Reloaded.slice(0, 320))

// Host answered, service unreachable: local stop only, never "已停止".
// A fresh mount keeps the previous action out of this scenario.
flags.automationPhase = 'pending_confirmation'
serverState = makeState({
  automation: automationFixture({ state: 'running', scheduler_enabled: true }),
  host: F1_HOST,
})
runtime.instances.clear()
await render(panel)
await click('停止自动交易')
const f1Pending = textOf(await render(panel))
expect('F1n Host stopped but Service unreachable reads as 待确认, not 已停止',
  f1Pending.includes('本机调度已停，服务端状态待确认') && !f1Pending.includes('服务端已确认暂停'),
  f1Pending.slice(-600))
flags.automationPhase = 'stopped'

// §2.2: overdue data shows the last success time and never falls back to 0/空仓/一切正常.
runtime.instances.clear()
await render(panel)
serverState = makeState({
  automation: automationFixture({ last_success_at: new Date(Date.now() - 3 * 3600 * 1000).toISOString() }),
  host: F1_HOST,
  positions: [],
  lastRound: { ...NO_TRADE, freshness: { ...NO_TRADE.freshness, produced_at: new Date(Date.now() - 3 * 3600 * 1000).toISOString() } },
  account: { empty: true, gap: 'E_ACCOUNT_EMPTY', empty_reason: 'E_ACCOUNT_EMPTY', source: null },
  equityCurve: { range: 'all', points: [], count: 0, empty: true, empty_reason: 'E_EQUITY_CURVE_UNAVAILABLE', gap: 'E_EQUITY_CURVE_UNAVAILABLE', source: 'empty' },
  rounds: [NO_TRADE_SUMMARY],
})
serverHealth = HEALTH
await click('更多')
await click('刷新数据')
await click('关闭')
const f1Stale = textOf(await render(panel))
expect('F1o overdue data is stated as 影响：数据已过期', f1Stale.includes('影响：数据已过期'), f1Stale.slice(0, 400))
expect('F1p the last success time stays visible when data is stale',
  f1Stale.includes('最近成功') && !f1Stale.includes('尚无成功轮次'), f1Stale.slice(0, 400))
expect('F1q no fabricated zero equity and no "一切正常" for unknown data',
  !f1Stale.includes('0.00 USDT') && !f1Stale.includes('一切正常'), f1Stale.slice(0, 400))
// A second, independently actionable fault makes the "N more" list reachable.
serverState = makeState({
  automation: automationFixture({ last_success_at: new Date(Date.now() - 3 * 3600 * 1000).toISOString() }),
  host: F1_HOST,
  positions: [POSITIONS[1]],
  lastRound: { ...NO_TRADE, freshness: { ...NO_TRADE.freshness, produced_at: new Date(Date.now() - 3 * 3600 * 1000).toISOString() } },
  rounds: [NO_TRADE_SUMMARY],
})
await click('更多')
await click('刷新数据')
await click('关闭')
await click('还有')
const f1StaleList = textOf(await render(panel))
expect('F1r the pending list carries 影响 + 下一步, not a bare code',
  f1StaleList.includes('下一步：刷新数据；仍失败时检查服务'), f1StaleList.slice(-700))
expect('F1r2 the same fault is not repeated as two different categories',
  (f1StaleList.match(/影响：数据已过期/g) ?? []).length === 1, f1StaleList.slice(-700))
await click('关闭')

// §5.1: the loaded release id and the load time are two separate facts.
serverState = makeState({ automation: automationFixture(), host: F1_HOST })
serverState.build = { ...F1_BUILD }
await click('操作台')
await click('更多')
await click('刷新数据')
await click('关闭')
await click('更多')
await click('系统状态')
const f1Version = textOf(await render(panel))
expect('F1s the drawer shows the source release id',
  f1Version.includes('源码发布标识') && f1Version.includes('twb-0.1.0-c44db84a30cd'), f1Version.slice(-900))
expect('F1t the drawer shows the release digest separately from the load time',
  f1Version.includes('源码摘要') && f1Version.includes('sha256:c44db84a30cd') && f1Version.includes('本进程加载时间'),
  f1Version.slice(-900))
expect('F1u the automation panel explains where the state comes from',
  f1Version.includes('自动交易明细') && f1Version.includes('依据服务端控制态'),
  f1Version.slice(-900))
await click('关闭证据抽屉')

// 1024: the state + stop button survive; optional chrome folds (static CSS).
expect('F1v 1024 keeps status + stop action visible and folds optional chrome',
  SOURCE.includes('@container (max-width:1199px)')
    && SOURCE.includes('@media (max-width:1199px)')
    && SOURCE.includes('.twb-precheck-toggle{display:none}')
    && SOURCE.includes('.twb-auto-short{display:inline}')
    && SOURCE.includes('.twb-auto-btn{max-width:150px}'),
  'container query missing')
expect('F1w the status region is announced and the buttons are keyboard reachable',
  SOURCE.includes("'data-auto-state': automation.state")
    && SOURCE.includes("role: 'status'")
    && SOURCE.includes("'aria-live': 'polite'")
    && SOURCE.includes("'aria-busy': String(autoPhase.phase === 'stopping'"),
  'accessibility attributes missing')
serverState = makeState()
serverHealth = HEALTH
runtime.instances.clear()
await render(panel)

// ---- scenario F2: one settings entry, five groups (§3.1 / §8·F) ----------
// F2 covers the entry, the five groups, read-only honesty, draft vs applied,
// revision conflict, the C fund calibers, G3 trigger distinction, G2 single
// route fact, pending (not failed) orders and the 1024/keyboard contract.
const F2_ROUND = {
  ...ROUND,
  route: {
    default: { provider: 'qoder', model: 'dfmodel', effort: 'high', effort_source: 'roleRoutes' },
    roles: { analysis: { provider: 'qoder', model: 'qmodel_latest', effort: 'xhigh' } },
    config_revision: 3,
    frozen_at: '2026-11-14T01:00:00.000Z',
    catalog_fingerprint: 'catalog-abc',
    unavailable: false,
    errors: null,
  },
}
const F2_SUMMARY = { ...ROUND_SUMMARY, route: F2_ROUND.route }
const F2_PORTFOLIO = {
  ...PORTFOLIO,
  outbox: {
    pending_count: 1,
    pending: [{ clientOrderId: 'tw-pend0001', businessKey: 'bk-1', symbol: 'BTCUSDT', side: 'long', status: 'UNKNOWN', display: 'PENDING_CONFIRMATION', pendingConfirmation: true, ageSeconds: 94 }],
    unknown: 1, submitting: 0, failed: 0, resolved: 0, checked: 1,
  },
  startup: { new_entry_blocked: false, recovered_at: '2026-11-14T00:30:00.000Z' },
  recovery: { recovered_at: '2026-11-14T00:30:00.000Z', recovered: true },
  protection: { ok: true, positions: 1, missing: [] },
}
flags.pnlFail = false
flags.pnlUnknown = false
flags.configSaveConflict = false
serverState = makeState({
  automation: automationFixture(),
  host: F1_HOST,
  lastRound: F2_ROUND,
  rounds: [F2_SUMMARY, { ...NO_TRADE_SUMMARY, trigger: 'auto' }, OLDER],
  portfolio: F2_PORTFOLIO,
})
serverState.build = { ...F1_BUILD }
serverHealth = { ...HEALTH, build: serverState.build }
runtime.instances.clear()
await click('操作台')
await click('设置')
const f2Open = textOf(await render(panel))
const f2Tabs = findAll(currentTree, (node) => node.tag === 'button' && node.props?.role === 'tab'
  && String(node.props?.id ?? '').startsWith('twb-drawer-tab-'))
expect('F2a one entry opens settings with exactly the five documented groups',
  f2Tabs.length === 5 &&
    f2Open.includes('连接与账户') && f2Open.includes('交易与风控') && f2Open.includes('自动化') &&
    f2Open.includes('外观与显示') && f2Open.includes('系统与诊断'),
  f2Tabs.map((tab) => textOf(tab)).join('|'))
expect('F2b the connection group answers environment / account / credentials',
  f2Open.includes('账本 ID') && f2Open.includes('6512827b') && f2Open.includes('凭证') && f2Open.includes('服务地址'),
  f2Open.slice(0, 700))
// §4.5 / live onboarding：连接组里唯一允许的可输入项是"实盘凭据录入"，
// 它写的是本机 0600 文件，不是服务端口径；其余输入框仍然必须为零。
const connectionInputs = findAll(currentTree, (node) => node.tag === 'input' && node.props?.type !== 'checkbox')
const liveCredentialInputs = connectionInputs.filter((node) => String(node.props?.['data-live-key'] ?? '').length > 0)
// Demo credentials are also a local 0600 file write, not a service-owned fact.
const demoCredentialInputs = connectionInputs.filter((node) => String(node.props?.['data-demo-key'] ?? '').length > 0)
expect('F2c service-owned facts are marked read-only, not fake editable',
  f2Open.includes('只读 · 来源服务端配置') && connectionInputs.length === liveCredentialInputs.length + demoCredentialInputs.length && liveCredentialInputs.length === 2,
  `inputs=${connectionInputs.length} live=${liveCredentialInputs.length}`)
expect('F2c2 live onboarding entry exists in the connection group',
  f2Open.includes('实盘接入') && f2Open.includes('填写实盘 API Key / Secret') &&
    f2Open.includes('仅本机') && f2Open.includes('不回显明文'),
  f2Open.slice(-600))
expect('F2c3 live onboarding never asks the browser to remember the key',
  !/localStorage[^\n]{0,80}(live|apiKey|secret)/i.test(f2Open),
  'no browser storage for credentials')
expect('F2d the C fund calibers are shown one by one, not merged into one equity',
  f2Open.includes('10,412.44') && f2Open.includes('10,453.64') && f2Open.includes('9,412.60') &&
    f2Open.includes('钱包余额') && f2Open.includes('保证金权益') && f2Open.includes('可用余额') &&
    f2Open.includes('本地账净盈亏'),
  f2Open.slice(0, 900))
expect('F2e reconcile shows state + diff + reason + last success + coverage',
  f2Open.includes('部分对平') && f2Open.includes('464.23') &&
    f2Open.includes('差额 -464.23000000 超出容差 0.01') &&
    f2Open.includes('最后成功对账') && f2Open.includes('统计覆盖'),
  f2Open.slice(-900))

// funding not backfilled: net must read as unknown, never as 0
flags.pnlUnknown = true
await click('刷新资金口径')
const f2Unknown = textOf(await render(panel))
expect('F2f an unbackfilled funding book shows 未回填 and never a zero net PnL',
  f2Unknown.includes('资金费未回填') && f2Unknown.includes('统计覆盖不完整') &&
    f2Unknown.includes('未知项不按 0 计入') && !f2Unknown.includes('本地账净盈亏（不含划转） 0.00'),
  f2Unknown.slice(-900))
flags.pnlUnknown = false

// the DTO being unavailable is an unknown, not a zero balance
flags.pnlFail = true
await click('刷新资金口径')
const f2Down = textOf(await render(panel))
expect('F2g an unavailable fund DTO says so instead of showing zeros or a spinner',
  f2Down.includes('资金口径暂不可用（E_SERVICE_TIMEOUT）') && !f2Down.includes('0.00 USDT'),
  f2Down.slice(-700))
flags.pnlFail = false
await click('刷新资金口径')

// trading & risk: read-only effective limits, no confidence amplification
await click('交易与风控')
const f2Trading = textOf(await render(panel))
expect('F2h trading group shows the effective caps only, with units and the legacy note',
  f2Trading.includes('当前生效限额') && f2Trading.includes('单笔风险上限') && f2Trading.includes('1.00%') &&
    f2Trading.includes('杠杆上限') && /\d+\.\d{2}×/.test(f2Trading) &&
    f2Trading.includes('60.00%') && f2Trading.includes('authorization.effective_caps') &&
    f2Trading.includes('不参与执行授权（legacy）') && f2Trading.includes('strong_max_risk_pct') &&
    f2Trading.includes('信心（分析字段）'),
  f2Trading.slice(Math.max(0, f2Trading.indexOf('当前生效限额')), Math.max(0, f2Trading.indexOf('当前生效限额')) + 900))
// P-1: no authorization block -> "不可用（来源缺失）", never a borrowed number.
const savedAuth = ENVELOPE.authorization
const savedLegacy = ENVELOPE.legacy_strong_tier
delete ENVELOPE.authorization
delete ENVELOPE.legacy_strong_tier
fireTimers(30000)
await flush()
await render(panel)
const f2NoCaps = textOf(await render(panel))
expect('F2h1 a missing effective_caps reads as unavailable instead of falling back',
  f2NoCaps.includes('不可用（来源缺失）') && !/单笔风险上限 1\.00%/.test(f2NoCaps) &&
    !/杠杆上限 10\.00×/.test(f2NoCaps),
  f2NoCaps.slice(Math.max(0, f2NoCaps.indexOf('当前生效限额')), Math.max(0, f2NoCaps.indexOf('当前生效限额')) + 700))
// P-3: an absent legacy block is "unknown", not "closed".
expect('F2h2 an absent legacy tier is reported as unknown, not as disabled',
  f2NoCaps.includes('未知（服务端未回报旧档状态）') && !f2NoCaps.includes('不参与执行授权（legacy）'),
  f2NoCaps.slice(Math.max(0, f2NoCaps.indexOf('旧强信心档')), Math.max(0, f2NoCaps.indexOf('旧强信心档')) + 200))
ENVELOPE.authorization = savedAuth
ENVELOPE.legacy_strong_tier = savedLegacy
fireTimers(30000)
await flush()
await render(panel)
// P-4 (must not regress): a non-current envelope still reads as 上一轮限额.
expect('F2h3 the limits group stays read-only (the service exposes no risk-write route)',
  !findAll(currentTree, (node) => node.tag === 'button' && textOf(node).includes('保存') &&
    String(node.props?.['data-settings-save'] ?? '') === 'trading').length &&
    f2Trading.includes('服务端没有风控写入路由'),
  'a save control appeared in the trading group')

// automation: scheduler + budget + model presets + auto/manual trigger distinction
await click('自动化')
const f2Auto = textOf(await render(panel))
expect('F2i automation group reuses the scheduler/budget panels and the model presets',
  f2Auto.includes('自动交易明细') && f2Auto.includes('自动调度明细') &&
    f2Auto.includes('平衡') && f2Auto.includes('保存配置'),
  f2Auto.slice(0, 900))
expect('F2j automatic slots and manual runs are distinguishable (G3 display only)',
  f2Auto.includes('最近轮次来源') && f2Auto.includes('自动槽') && f2Auto.includes('手工'),
  f2Auto.slice(0, 900))
expect('F2k the model form shows before / after / effective time and a draft marker',
  f2Auto.includes('修改前') && f2Auto.includes('修改后') && f2Auto.includes('生效时间') &&
    f2Auto.includes('下一轮') && f2Auto.includes('上次生效'),
  f2Auto.slice(0, 900))

// revision conflict: the draft survives and nothing is reported as saved
flags.configSaveConflict = true
await click('保存配置')
const f2Conflict = textOf(await render(panel))
expect('F2l a stale save returns a conflict, keeps the draft and does not claim success',
  f2Conflict.includes('保存冲突') && f2Conflict.includes('草稿已保留') &&
    f2Conflict.includes('采用最新配置') && !f2Conflict.includes('已保存 rev'),
  f2Conflict.slice(-900))
flags.configSaveConflict = false
await click('采用最新配置（放弃我的草稿）')

// appearance: instant local settings
await click('外观与显示')
const f2Look = textOf(await render(panel))
expect('F2m appearance group carries language, density and up/down colours as instant local settings',
  f2Look.includes('语言') && f2Look.includes('密度') && f2Look.includes('涨跌色') &&
    f2Look.includes('本机设置 · 即时生效'),
  f2Look.slice(0, 700))
await click('English')
const f2English = textOf(await render(panel))
expect('F2n switching language from the settings group re-renders the panel in English',
  f2English.includes('Connection & account') || f2English.includes('Settings'),
  f2English.slice(0, 400))
await click('中文')
await click('紧凑')
const f2Density = findAll(currentTree, (node) => node.props?.['data-density'] === 'compact')
expect('F2o the density preference is applied on the panel root', f2Density.length === 1, String(f2Density.length))
await click('标准')

// diagnostics: loaded version, data time, service state, pending orders, log, deep link
await click('系统与诊断')
const f2Diag = textOf(await render(panel))
expect('F2p diagnostics shows the loaded identity, data time, service state and the G5 schema report',
  f2Diag.includes('实际加载版本') && f2Diag.includes(F1_BUILD.release_id) &&
    f2Diag.includes('数据时间') && f2Diag.includes('服务状态') &&
    f2Diag.includes('declared=1') && f2Diag.includes('consistent=true'),
  f2Diag.slice(0, 900))
expect('F2q pending orders read as 待确认 with an age, never as a failure',
  f2Diag.includes('未决订单') && f2Diag.includes('1 条待确认') && f2Diag.includes('tw-pend0001') &&
    f2Diag.includes('待确认不等于失败'),
  f2Diag.slice(-900))
// The outbox block is the only evidence for "0 pending": without it the group
// says unavailable instead of claiming a clean queue.
serverState = makeState({ automation: automationFixture(), host: F1_HOST, lastRound: F2_ROUND, rounds: [F2_SUMMARY] })
runtime.instances.clear()
await click('操作台')
await click('设置')
await click('系统与诊断')
const f2NoOutbox = textOf(await render(panel))
expect('F2q2 a missing outbox block reads as unavailable, not "0 pending"',
  f2NoOutbox.includes('未决订单') && f2NoOutbox.includes('暂不可用') &&
    !f2NoOutbox.includes('没有待确认订单'),
  f2NoOutbox.slice(-600))
serverState = makeState({
  automation: automationFixture(),
  host: F1_HOST,
  lastRound: F2_ROUND,
  rounds: [F2_SUMMARY, { ...NO_TRADE_SUMMARY, trigger: 'auto' }, OLDER],
  portfolio: F2_PORTFOLIO,
})
runtime.instances.clear()
await click('操作台')
await click('设置')
await click('系统与诊断')
expect('F2r the compact log and the full-diagnostics deep link are reachable from the group',
  f2Diag.includes('精简日志') && f2Diag.includes('操作历史') && Boolean(findClickable('打开完整诊断')),
  f2Diag.slice(-500))
expect('F2s the executed route in diagnostics has one source (round snapshot) + revision',
  f2Diag.includes('执行路由（本轮冻结）') && f2Diag.includes('qoder / dfmodel') &&
    f2Diag.includes('Host 轮次快照') && f2Diag.includes('配置修订 3'),
  f2Diag.slice(0, 900))
await click('打开完整诊断')
const f2System = textOf(await render(panel))
expect('F2t the deep link opens the existing system drawer instead of duplicating it',
  f2System.includes('系统状态') && f2System.includes('缺口原文'),
  f2System.slice(0, 400))
await click('关闭证据抽屉')

// keyboard + 1024 contract (static): group tabs are a real tablist inside the dialog
// §2.2: one fault keeps one main hint. Opening settings on top of a stale-data
// fault must not paint a second alert, and 1440 must keep the drawer readable.
serverState = makeState({
  automation: automationFixture({ last_success_at: new Date(Date.now() - 3 * 3600 * 1000).toISOString() }),
  host: F1_HOST,
  lastRound: { ...F2_ROUND, freshness: { ...F2_ROUND.freshness, produced_at: new Date(Date.now() - 3 * 3600 * 1000).toISOString() } },
  rounds: [F2_SUMMARY],
  portfolio: F2_PORTFOLIO,
})
runtime.instances.clear()
await click('操作台')
const f2Fault = textOf(await render(panel))
await click('设置')
const f2FaultOpen = textOf(await render(panel))
const f2Alerts = findAll(currentTree, (node) => node.props?.role === 'alert')
expect('F2u one fault keeps exactly one main hint while settings is open',
  f2Alerts.length === 1 &&
    (f2FaultOpen.match(/影响：/g) ?? []).length === (f2Fault.match(/影响：/g) ?? []).length &&
    (f2Fault.match(/影响：/g) ?? []).length >= 1,
  JSON.stringify({ alerts: f2Alerts.length, before: (f2Fault.match(/影响：/g) ?? []).length, after: (f2FaultOpen.match(/影响：/g) ?? []).length }))
expect('F2u2 1440 keeps the settings drawer inside a readable width',
  SOURCE.includes('.twb-drawer{top:64px;bottom:28px;width:720px;max-width:94%') &&
    SOURCE.includes('@container (min-width:1390px)'),
  'drawer width / 1440 rule missing')
await click('关闭设置')
expect('F2u3 settings is a keyboard-reachable dialog with a tablist and roving tabindex',
  SOURCE.includes("'aria-modal': 'true'") && SOURCE.includes("className: 'twb-drawer-tabs twb-settings-tabs'") &&
    SOURCE.includes('tabIndex: (isSettings ? settingsGroup === tab : drawer.tab === tab) ? 0 : -1') &&
    SOURCE.includes("'ArrowRight'"),
  'tablist attributes missing')
expect('F2v 1024 folds the settings label but keeps the entry and the drawer usable',
  SOURCE.includes('.twb-settings-lbl{display:none}') &&
    SOURCE.includes('.twb-settings-btn{padding:0 8px}') &&
    SOURCE.includes('.twb-settings-kv{grid-template-columns:minmax(96px,42%) minmax(0,1fr)}'),
  '1024 rules missing')
expect('F2w every settings copy key exists in both languages',
  (() => {
    const keys = ['set.open', 'set.title', 'set.readonly', 'set.group.connection', 'set.group.trading',
      'set.group.automation', 'set.group.appearance', 'set.group.diagnostics', 'set.fundsTitle',
      'set.reconcileTitle', 'set.diagPending', 'set.diagLogs', 'set.conflict', 'ai.entryZone', 'ai.validUntil']
    return keys.every((key) => typeof LOCALE_DICT.zh?.[key] === 'string' && typeof LOCALE_DICT.en?.[key] === 'string')
  })(),
  'missing keys')

// F4: the plan's allowed entry range reaches the UI (round-view passthrough).
const f4View = toRoundView({
  roundId: 'cyc-f4', cycleId: 'cyc-f4', status: 'done', outcome: 'trade',
  result: { calls: [], outputs: { decision: { action: 'trade', plan: { entry: { style: 'MARKET', price: 68234.5, zone_low: 68100, zone_high: 68250 }, stop_loss: { price: 67650 }, take_profit: { price: 69400 }, risk_pct: 0.8, leverage: 3, rr_to_target: 1.66, time_stop_hours: 24, valid_until: '2026-11-14T08:00:00Z' } } } },
})
expect('F2x round-view passes the allowed entry range and the plan expiry (F4)',
  f4View.decision?.plan?.entry_zone_low === 68100 && f4View.decision?.plan?.entry_zone_high === 68250 &&
    f4View.decision?.plan?.valid_until === '2026-11-14T08:00:00Z',
  JSON.stringify(f4View.decision?.plan))
const f4NoZone = toRoundView({
  roundId: 'cyc-f4b', cycleId: 'cyc-f4b', status: 'done', outcome: 'trade',
  result: { calls: [], outputs: { decision: { action: 'trade', plan: { entry: { style: 'MARKET', price: 68234.5 }, stop_loss: { price: 67650 }, take_profit: { price: 69400 }, risk_pct: 0.8, leverage: 3, rr_to_target: 1.66, time_stop_hours: 24 } } } },
})
expect('F2y a plan without a zone stays null instead of widening the range',
  f4NoZone.decision?.plan?.entry_zone_low === null && f4NoZone.decision?.plan?.entry_zone_high === null &&
    f4NoZone.decision?.plan?.valid_until === null,
  JSON.stringify(f4NoZone.decision?.plan))

serverState = makeState()
serverHealth = HEALTH
runtime.instances.clear()
await render(panel)

// ---- scenario T15: gate blocked stage + exchange reachability --------------
// §8·D: a gate that blocks but renders as pending/ok is not a gate. And T20's
// exchange block is the only place reachability may be read from.
const T15_STAGES = [
  { stage_key: 'analysis', label: '分析', status: 'ok' },
  { stage_key: 'red_opening', label: '红队·开场', status: 'ok' },
  { stage_key: 'blue_opening', label: '蓝队·开场', status: 'ok' },
  { stage_key: 'decision', label: '决策', status: 'ok' },
  { stage_key: 'risk', label: '风控', status: 'ok' },
  {
    stage_key: 'execution_entry', label: '操盘手', status: 'blocked',
    error_code: 'PRICE_OUT_OF_ZONE',
    blocked_by_gate: {
      code: 'PRICE_OUT_OF_ZONE',
      sub_code: null,
      detail: '现价不在计划允许的入场区间内，按计划不执行（不改方向、不追价）。',
      mode: 'observe',
      enforced: false,
    },
  },
]
const T15_ROUND = {
  ...ROUND,
  status: 'done',
  outcome: 'no_trade',
  reasonCode: 'EXECUTION_BLOCKED:PRICE_OUT_OF_ZONE',
  gate: {
    ok: false,
    code: 'PRICE_OUT_OF_ZONE',
    sub_code: null,
    detail: '现价不在计划允许的入场区间内，按计划不执行（不改方向、不追价）。',
    checked_at: 1762000000000,
    mode: 'observe',
    enforced: false,
    quote: { last: 68999.5, age_ms: 1200, source: 'evidence_build' },
  },
  planGaps: {
    decision: [{ code: 'VALID_UNTIL_MISSING', field: 'valid_until', reason_zh: '计划没有给出失效时间，无法判断是否仍然有效。' }],
  },
  failureCategory: 'ENTRY_CONDITION_LOST',
  preSubmit: {
    ok: true, code: null, sub_code: null, detail: null, checked_at: 1762000001000,
    mode: 'observe', enforced: false, advisory_only: true, requote: 'unavailable',
    quote: { last: null, age_ms: null, source: 'evidence_build' },
  },
  stages: T15_STAGES,
}
ROUNDS_BY_ID[T15_ROUND.roundId] = T15_ROUND
serverState = makeState({
  lastRound: T15_ROUND,
  rounds: [{ ...ROUND_SUMMARY, roundId: T15_ROUND.roundId, status: 'done', outcome: 'no_trade', reasonCode: T15_ROUND.reasonCode, blockedByGate: true, gateCode: 'PRICE_OUT_OF_ZONE' }],
})
serverHealth = HEALTH
runtime.instances.clear()
await click('AI 决策')
await click('本轮')
const t15Text = textOf(await render(panel))
const t15Stage = findAll(currentTree, (node) => String(node.props?.['data-gate-blocked'] ?? '') === 'PRICE_OUT_OF_ZONE')
expect('T15a a gate-blocked execution stage is marked blocked, not pending/ok',
  t15Stage.length >= 1 && t15Stage.some((node) => node.props?.['data-state'] === 'blocked') &&
    t15Text.includes('被闸门阻断'),
  JSON.stringify(t15Stage.map((node) => node.props?.['data-state'])))
expect('T15b the blocked reason is the gate detail, shown once next to the stage',
  t15Text.includes('执行闸门阻断了本轮提交') && t15Text.includes('现价不在计划允许的入场区间内') &&
    t15Text.includes('PRICE_OUT_OF_ZONE'),
  t15Text.slice(-900))
expect('T15c the gate quote names its provenance and the observe-only mode',
  t15Text.includes('证据构建时冻结价（非提交价）') && t15Text.includes('仅记录，未拦截') &&
    t15Text.includes('68,999.50'),
  t15Text.slice(-700))
expect('T15c2 the pre-submit re-check is labelled as advisory (no re-quote)',
  t15Text.includes('提交前复检已执行：未重新报价'),
  t15Text.slice(-700))
expect('T15d plan gaps explain why the plan could not be verified',
  t15Text.includes('计划缺口（为什么无法验证）') && t15Text.includes('VALID_UNTIL_MISSING'),
  t15Text.slice(-700))
expect('T15e a gate-blocked round is annotated as such in the list/summary',
  T15_ROUND.gate.ok === false && toRoundView({ ...T15_ROUND, result: { calls: [], outputs: { gate: T15_ROUND.gate } } }).gate.code === 'PRICE_OUT_OF_ZONE',
  'gate projection missing')

// exchange reachability: readable / blocked / missing
const T15_EXCHANGE = {
  source: 'ledger',
  reachable: false,
  stale: true,
  last_success_at: new Date(Date.now() - 3600 * 1000).toISOString(),
  cooldown_until: new Date(Date.now() + 42000).toISOString(),
  cooldown_remaining_s: 42,
  read_timeout_s: 3,
  read_budget_s: 4,
  fetched_at: null,
  unknown: ['E_EXCHANGE_UNAVAILABLE：交易所不可达，当前没有可用快照'],
  error: 'ConnectTimeout',
}
serverState = makeState({
  automation: automationFixture(),
  host: F1_HOST,
  lastRound: F2_ROUND,
  rounds: [F2_SUMMARY],
  portfolio: { ...PORTFOLIO, exchange: T15_EXCHANGE, outbox: F2_PORTFOLIO.outbox },
})
runtime.instances.clear()
await click('操作台')
await click('设置')
await click('系统与诊断')
const t15Ex = textOf(await render(panel))
expect('T15f exchange reachability answers can-read / last-success / retry, from exchange.* only',
  t15Ex.includes('交易所可达性') && t15Ex.includes('现在不能取数') &&
    t15Ex.includes('上次成功取数') && t15Ex.includes('42.0 秒后（失败冷却中）') &&
    t15Ex.includes('3.0s / 4.0s'),
  t15Ex.slice(-900))
expect('T15g the failure reason is shown instead of a green check',
  t15Ex.includes('为什么取不到') && t15Ex.includes('E_EXCHANGE_UNAVAILABLE'),
  t15Ex.slice(-600))
serverState = makeState({ automation: automationFixture(), host: F1_HOST, lastRound: F2_ROUND, rounds: [F2_SUMMARY], portfolio: PORTFOLIO })
runtime.instances.clear()
await click('操作台')
await click('设置')
await click('系统与诊断')
const t15ExMissing = textOf(await render(panel))
expect('T15h a missing exchange block reads as unavailable',
  t15ExMissing.includes('暂不可用（服务端未回报 exchange 块）'),
  t15ExMissing.slice(-600))
await click('关闭设置')

serverState = makeState()
serverHealth = HEALTH
runtime.instances.clear()
await render(panel)

// ---- scenario T24: limits may only come from authorization.effective_caps --
// F21-1 (t24): the old static check only banned the literal inside
// TradingGroup, so a helper wrapping model.caps.maxRiskPct slipped through
// (T21 experiment B). This decoy case is behavioural: every OTHER source carries
// a distinctive wrong number, so any implementation that reads them shows up.
const T24_DECOYS = {
  effective: { max_risk_pct: 0.0123, max_leverage: 7, max_notional_multiple: 2.5, max_margin_usage: 0.55, max_open_positions: 2 },
  perTrade: { max_risk_pct: 0.045, max_leverage: 15, max_notional_multiple: 9.0 },
  flatLeverage: 15,
  margin: 0.9,
  positions: 4,
  roundRisk: { max_risk_pct: 0.09, max_leverage: 12 },
  budgetRiskPct: 0.09,
}
const T24_ENVELOPE = {
  ...ENVELOPE,
  limits: {
    per_trade: { ...T24_DECOYS.perTrade, strong_max_risk_pct: 0.02, strong_max_leverage: 15 },
    max_leverage: T24_DECOYS.flatLeverage,
    max_positions: T24_DECOYS.positions,
    portfolio: { max_margin_usage: T24_DECOYS.margin, max_open_positions: T24_DECOYS.positions },
    loss: { daily_loss_stop: 0.03, max_drawdown_from_peak: 0.08, consecutive_losses: 2 },
  },
  authorization: {
    ...ENVELOPE.authorization,
    effective_caps: { ...T24_DECOYS.effective },
  },
  approval: {
    ttl_s: 120,
    ttl_max_s: 300,
    single_use: true,
    plan_hash_bound: true,
    rules_version_bound: true,
    authorization_model: 'recomputed_at_p1_and_p2',
    policy: { bound: true },
  },
}
const savedEnvelope = { ...ENVELOPE }
Object.assign(ENVELOPE, T24_ENVELOPE)
serverState = makeState({
  automation: automationFixture(),
  host: F1_HOST,
  lastRound: { ...ROUND, risk: { ...(ROUND.risk ?? {}), max_risk_pct: T24_DECOYS.roundRisk.max_risk_pct, max_leverage: T24_DECOYS.roundRisk.max_leverage } },
  rounds: [ROUND_SUMMARY],
  budget: { limits: { per_trade: { max_risk_pct: T24_DECOYS.budgetRiskPct } } },
})
runtime.instances.clear()
await click('操作台')
await click('设置')
await click('交易与风控')
const t24Block = findAll(currentTree, (node) => String(node.props?.className ?? '').includes('twb-settings-block')
  && textOf(node).includes('当前生效限额'))
const t24Text = t24Block.length > 0 ? textOf(t24Block[0]) : textOf(await render(panel))
expect('T24a the effective caps are rendered from authorization.effective_caps',
  t24Text.includes('1.23%') && t24Text.includes('7.00×') && t24Text.includes('2.50×') && t24Text.includes('55.00%'),
  t24Text.slice(0, 700))
expect('T24b NO value from another snapshot leaks into the limits block',
  ['4.50%', '15.00×', '9.00×', '90.00%', '12.00×'].every((decoy) => !t24Text.includes(decoy)),
  t24Text.slice(0, 700))
expect('T24c the approval token TTL and its one-submission constraint are visible',
  t24Text.includes('批准令牌 TTL') && t24Text.includes('120s') && t24Text.includes('300s') &&
    t24Text.includes('单次有效') && t24Text.includes('plan_hash 绑定') &&
    t24Text.includes('rules_version 绑定') && t24Text.includes('bound=true') &&
    t24Text.includes('每次批准只对一次提交有效'),
  t24Text.slice(-700))
// A missing approval block reads as unavailable — never 0s, never a guess.
const savedApproval = ENVELOPE.approval
delete ENVELOPE.approval
fireTimers(30000)
await flush()
await render(panel)
const t24NoApproval = findAll(currentTree, (node) => String(node.props?.className ?? '').includes('twb-settings-block')
  && textOf(node).includes('当前生效限额'))
const t24NoApprovalText = t24NoApproval.length > 0 ? textOf(t24NoApproval[0]) : ''
const t24TtlRow = t24NoApprovalText.split('批准令牌 TTL（单次有效）')[1] ?? ''
expect('T24d a missing approval block says unavailable instead of 0s',
  t24NoApprovalText.includes('不可用（来源缺失）') && !t24TtlRow.trim().startsWith('0'),
  t24TtlRow.slice(0, 120))
ENVELOPE.approval = savedApproval
Object.keys(T24_ENVELOPE).forEach((key) => { if (key !== 'approval') ENVELOPE[key] = savedEnvelope[key] })
fireTimers(30000)
await flush()
await render(panel)
await click('关闭设置')
serverState = makeState()
serverHealth = HEALTH
runtime.instances.clear()
await render(panel)

// ---- T25: the reason-code surfaces follow the 中文/EN switch ---------------
// 03 §8 owns the code->label table (both languages). 01 §3.7 owns the
// attribution prose: the Host now sends one_liner_en / next_action_en next to
// the _zh columns, and the Client picks the column for the panel language.
// Before this round the attribution card kept its Chinese one-liner and
// stage/role/category in English mode, and the reason breakdown printed raw
// machine codes in both languages.
const T25_ATTR = {
  ...V2_ATTRIBUTION,
  one_liner_en: 'Risk AI produced no output; the round stays conservative and does not trade',
  next_action_en: 'Raise the risk output cap or drop a tier; wait for the next round',
}
const T25_ROUND = { ...V2_ROUND, attribution: T25_ATTR }
ROUNDS_BY_ID[T25_ROUND.roundId] = T25_ROUND
const T25_SUMMARY = { ...ROUND_SUMMARY, roundId: T25_ROUND.roundId, status: 'failed', outcome: 'no_trade', reasonCode: T25_ROUND.reasonCode, headline: T25_ROUND.headline }
serverState = makeState({ lastRound: T25_ROUND, rounds: [T25_SUMMARY] })
serverHealth = { ...HEALTH, build: V2_BUILD }
runtime.instances.clear()
await render(panel)
await click('AI 决策')
await click('本轮')
const t25Zh = textOf(await render(panel))
expect('T25a zh attribution card keeps the 01 Chinese prose',
  t25Zh.includes('失败归因') && t25Zh.includes('风控 AI 未产出，按保守策略不交易') && t25Zh.includes('链路'),
  t25Zh.slice(-900))
await click('EN')
const t25En = textOf(await render(panel))
expect('T25b en attribution card switches to the 01 English column',
  t25En.includes('Failure attribution') && t25En.includes('Risk AI produced no output') &&
    !t25En.includes('风控 AI 未产出，按保守策略不交易'),
  t25En.slice(-900))
expect('T25c en attribution stage/role/category come from the DTO keys',
  t25En.includes('Chain') && t25En.includes('Stage') && t25En.includes('Risk') && !t25En.includes('链路'),
  t25En.slice(-900))
await click('History')
const t25Hist = textOf(await render(panel))
expect('T25d en history reason column is the English label + the raw code',
  t25Hist.includes('Reason') && t25Hist.includes('Risk AI produced nothing (RISK_UNAVAILABLE)'),
  t25Hist.slice(-900))
await click('Analysis')
const t25Review = textOf(await render(panel))
expect('T25e en reason breakdown shows labels, not raw codes',
  t25Review.includes('Reason breakdown') && t25Review.includes('Risk AI produced nothing') && !t25Review.includes('RISK_UNAVAILABLE'),
  t25Review.slice(-900))
await click('中')
const t25Back = textOf(await render(panel))
expect('T25f switching back to 中文 restores the Chinese vocabulary',
  t25Back.includes('原因码构成') && t25Back.includes('风控 AI 未产出'),
  t25Back.slice(-900))
runtime.instances.clear()
serverState = makeState()
serverHealth = HEALTH
await render(panel)

// ---- T26: the round archive is queried one day at a time ------------------
// 15 §4/§5: /state carries today's partition; a past day is its own fetch
// (`/rounds?date=…`) and the history view must never mix two days.
const T26_TODAY = shanghaiToday()
const T26_PAST = '2026-11-07'
const T26_EMPTY = '2026-11-06'
const T26_PAST_ROUND = {
  roundId: 'cyc-pastday-01', trigger: 'auto', status: 'done', outcome: 'no_trade',
  reasonCode: 'NO_CANDIDATE', headline: '不交易：没有合格标的',
  startedAt: `${T26_PAST}T02:00:00Z`, finishedAt: `${T26_PAST}T02:00:30Z`, durationMs: 30000, symbol: 'BTCUSDT',
}
roundsByDate = { [T26_PAST]: [T26_PAST_ROUND] }
serverState = makeState({ roundsDate: T26_TODAY, availableDates: [T26_TODAY, T26_PAST, T26_EMPTY] })
serverHealth = HEALTH
runtime.instances.clear()
await render(panel)
await click('AI 决策')
await click('历史')
const t26Today = textOf(await render(panel))
expect('T26a the history view has an archive day bar with today selected',
  t26Today.includes('归档日期') && t26Today.includes('今天') && t26Today.includes(T26_TODAY) && t26Today.includes(T26_PAST),
  t26Today.slice(0, 500))
expect('T26b the scope copy no longer claims a 100-round cache',
  t26Today.includes('本地按日归档，保留全部轮次') && !t26Today.includes('最近 100 轮') && !t26Today.includes('last 100 local round'),
  t26Today.slice(0, 500))
expect('T26c today is served by /state, never by a date query',
  !requestLog.some((entry) => entry.url.includes(`/rounds?date=${T26_TODAY}`)),
  JSON.stringify(requestLog.filter((entry) => entry.url.includes('/rounds')).slice(-3)))

changeInput('选择日期', T26_PAST)
const t26Past = textOf(await render(panel))
expect('T26d picking an archived day fetches that day once',
  requestLog.some((entry) => entry.url.includes(`/rounds?date=${T26_PAST}`)),
  JSON.stringify(requestLog.filter((entry) => entry.url.includes('/rounds')).slice(-3)))
expect('T26e the archived day shows its own rounds and not today\'s',
  (() => {
    const card = findAll(currentTree, (node) => String(node.props?.className ?? '').includes('twb-report-card')
      && textOf(node).includes('归档日期'))[0]
    const text = card ? textOf(card) : ''
    return text.includes('#day-01') && text.includes('自动') && text.includes(T26_PAST) && !text.includes('#06c625')
  })(),
  t26Past.slice(-700))

changeInput('选择日期', T26_EMPTY)
const t26Empty = textOf(await render(panel))
expect('T26f an archived day without rounds says so and offers today',
  t26Empty.includes('这一天没有轮次记录') && t26Empty.includes('回到今天'),
  t26Empty.slice(-600))
await click('回到今天')
const t26Back = textOf(await render(panel))
expect('T26g "回到今天" restores today\'s rows',
  t26Back.includes('#06c625') && !t26Back.includes('#day-01'),
  t26Back.slice(-600))

flags.roundsFail = true
changeInput('选择日期', T26_PAST)
const t26Fail = textOf(await render(panel))
expect('T26h a rejected date query is reported, never papered over',
  t26Fail.includes('该日期读取失败') && t26Fail.includes('HTTP 400'),
  t26Fail.slice(-600))
flags.roundsFail = false
// A Host from before the archive answers /rounds without a `date`; the view must
// refuse the payload instead of labelling today's rounds as that day.
flags.roundsOldHost = true
changeInput('选择日期', '2026-11-05')
const t26Old = textOf(await render(panel))
const t26OldCard = findAll(currentTree, (node) => String(node.props?.className ?? '').includes('twb-report-card')
  && textOf(node).includes('归档日期'))[0]
const t26OldCardText = t26OldCard ? textOf(t26OldCard) : ''
expect('T26i a Host without the archive route is reported, not mislabelled',
  t26Old.includes('该日期读取失败') && t26Old.includes('E_ARCHIVE_UNSUPPORTED') && !t26OldCardText.includes('#06c625'),
  t26Old.slice(-600))
flags.roundsOldHost = false
runtime.instances.clear()
serverState = makeState()
serverHealth = HEALTH
await render(panel)

// ---- T27: a failed first /state retries fast (reload window) ---------------
// After a plugin reload the Host routes can be unregistered for a moment. The
// panel must retry in 0.8s, not wait a whole 8s poll tick, or it shows
// "Host 离线" long after the Host is back.
runtime.instances.clear()
serverState = makeState()
serverHealth = HEALTH
flags.stateFailOnce = 1
const t27StateCallsBefore = requestLog.filter((entry) => entry.url.endsWith('/state')).length
await render(panel)
const t27Down = textOf(currentTree)
expect('T27a the first failed /state reads as Host unavailable, not as empty data',
  t27Down.includes('Host 离线') || t27Down.includes('Host connection lost'),
  t27Down.slice(-300))
expect('T27b a failed fetch schedules the fast retry (0.8s), not the poll tick',
  timers.some((timer) => timer.delay === 800),
  JSON.stringify(timers.map((timer) => timer.delay)))
fireTimers(800)
await flush()
await render(panel)
const t27Up = textOf(currentTree)
const t27StateCalls = requestLog.filter((entry) => entry.url.endsWith('/state')).length
expect('T27c the fast retry reconnects on the same screen',
  t27StateCalls > t27StateCallsBefore && t27Up.includes('Host 在线'),
  `${t27StateCallsBefore}->${t27StateCalls} ${t27Up.slice(-300)}`)
flags.stateFailOnce = 0
runtime.instances.clear()
await render(panel)

// ---- T28: one-click start of the local service (15 §10) -------------------
// The Host owns the capability fact; the UI only renders the button when the
// Host says it can actually launch, and reports the Host's outcome verbatim.
const T28_OFFLINE = { ...SERVICE_NESTED, ready: false, error_code: 'E_SERVICE_OFFLINE', last_run: { ...SERVICE_SUMMARY, ready: false, error_code: 'E_SERVICE_OFFLINE' } }
runtime.instances.clear()
serverState = makeState({ serviceStart: { supported: true, reason: null, command: 'service/start' } })
serverState.service = T28_OFFLINE
serverHealth = { ...HEALTH, service: { ...T28_OFFLINE, state: 'offline' } }
await render(panel)
const t28Before = textOf(currentTree)
expect('T28a the service-down banner offers 启动服务 when the Host supports it',
  t28Before.includes('服务不可达') && Boolean(findClickable('启动服务')),
  t28Before.slice(0, 400))
await click('启动服务')
const t28After = textOf(currentTree)
expect('T28b the click posts /service/start and shows the measured wait',
  requestLog.some((entry) => entry.method === 'POST' && entry.url.endsWith('/service/start')) &&
    t28After.includes('交易服务已就绪（12s）'),
  t28After.slice(-400))
flags.serviceStartFail = true
runtime.instances.clear()
serverState = makeState({ serviceStart: { supported: true, reason: null, command: 'service/start' } })
serverState.service = T28_OFFLINE
serverHealth = { ...HEALTH, service: { ...T28_OFFLINE, state: 'offline' } }
await render(panel)
await click('启动服务')
const t28Fail = textOf(currentTree)
expect('T28c a rejected start is reported with its code, never as success',
  t28Fail.includes('无法启动：本机找不到服务启动器或已禁用（E_SERVICE_LAUNCHER_MISSING）') &&
    !t28Fail.includes('交易服务已就绪'),
  t28Fail.slice(-400))
flags.serviceStartFail = false
runtime.instances.clear()
serverState = makeState({ serviceStart: { supported: false, reason: 'PYTHON_MISSING', command: 'service/start' } })
serverState.service = T28_OFFLINE
serverHealth = { ...HEALTH, service: { ...T28_OFFLINE, state: 'offline' } }
await render(panel)
const t28Unsupported = textOf(currentTree)
expect('T28d an unsupported Host offers the environment wizard, not a dead button (16 §1)',
  !findClickable('启动服务') && Boolean(findClickable('初始化服务')),
  t28Unsupported.slice(0, 400))
runtime.instances.clear()
serverState = makeState()
serverHealth = HEALTH
await render(panel)

// ---- T29: first-run environment wizard (16 §1) ---------------------------
// No venv on this machine: the banner must offer 初始化服务 (never a dead
// 启动服务), and the wizard must report what it found before installing.
flags.setupReady = false
flags.setupPolls = 0
runtime.instances.clear()
serverState = makeState({ serviceStart: { supported: false, reason: 'PYTHON_MISSING', command: 'service/start' } })
serverState.service = T28_OFFLINE
serverHealth = { ...HEALTH, service: { ...T28_OFFLINE, state: 'offline' } }
await render(panel)
const t29Banner = textOf(currentTree)
expect('T29a without a launcher the banner offers the environment wizard',
  Boolean(findClickable('初始化服务')) && !findClickable('启动服务'),
  t29Banner.slice(0, 400))
await click('初始化服务')
const t29Dialog = textOf(currentTree)
expect('T29b the wizard names the four checks before installing anything',
  t29Dialog.includes('交易服务环境') && t29Dialog.includes('Python') && t29Dialog.includes('虚拟环境') &&
    t29Dialog.includes('依赖') && t29Dialog.includes('服务') && t29Dialog.includes('一键初始化'),
  t29Dialog.slice(0, 500))
await click('一键初始化')
const t29Running = textOf(currentTree)
expect('T29c the wizard posts the setup job and shows its steps',
  requestLog.some((entry) => entry.method === 'POST' && entry.url.endsWith('/service/setup')) &&
    t29Running.includes('创建虚拟环境') && t29Running.includes('初始化中…'),
  t29Running.slice(-500))
fireTimers(1200)
await flush()
await render(panel)
const t29Done = textOf(currentTree)
expect('T29d the job ends ready and the service row turns ready',
  t29Done.includes('交易服务已就绪') || (t29Done.includes('完成') && t29Done.includes('就绪')),
  t29Done.slice(-500))
runtime.instances.clear()
serverState = makeState()
serverHealth = HEALTH
flags.setupReady = false
await render(panel)

// Q: lightweight marks update a position without changing official UPnL.
runtime.instances.clear()
serverState = makeState()
marksOverride = { BTCUSDT: { mark: 69999.12, as_of_ms: Date.now(), stale: false } }
await render(panel)
expect('Q01 fresh mark overlays the position while account staleness stays visible',
  textOf(currentTree).includes('69,999.12') && textOf(currentTree).includes('实时标记价') && textOf(currentTree).includes('历史快照'))
expect('Q02 marks request carries held symbols on the independent route', requestLog.some(r => r.url.includes('/market/marks?symbols=BTCUSDT')))
marksOverride = null

// S: exercise real component handlers through draft, AI analysis, mount and enable.
runtime.instances.clear()
await render(panel)
await click('设置')
await click('自动化')
const strategySource = findAll(currentTree, n => n.tag === 'textarea' && n.props?.maxLength === 20000)[0]
expect('S30 strategy editor is in the existing automation group', Boolean(strategySource))
strategySource.props.onChange({ target: { value: 'BTCUSDT 标记价大于100做多，15m每周期一次，止损2%止盈4%，最多一仓' } })
await render(panel)
await click('创建草稿')
expect('S31 creating a draft does not enable or mount it', strategyRecords.length === 1 && !strategyRecords[0].enabled && !strategyRecords[0].current_version)
await click('AI 理解 / 继续澄清')
expect('S32 AI analysis remains a draft and renders the returned rules', textOf(currentTree).includes('价格阈值示例') && !strategyRecords[0].current_version)
const confirmation = findAll(currentTree, n => n.tag === 'input' && n.props?.type === 'checkbox').find(n => !n.props.disabled)
confirmation.props.onChange({ target: { checked: true } })
await render(panel)
await click('确认挂载（不自动启用）')
expect('S33 mount is separate from execution authorization', strategyRecords[0].current_version === 1 && !strategyRecords[0].enabled && textOf(currentTree).includes('已挂载 · 未启用'))
await click('启用已确认版本')
expect('S34 enabling only posts the explicit strategy authorization', strategyRecords[0].enabled && requestLog.some(r => r.url.endsWith('/strategy-ui/authorization') && JSON.parse(r.body).enabled === true))
const oldVersion = strategyRecords[0].current_version
const nameInput = findAll(currentTree, n => n.tag === 'input' && n.props?.maxLength === 100)[0]
nameInput.props.onChange({ target: { value: '我的阈值策略' } })
await render(panel)
await click('仅改名')
expect('S35 rename preserves version and strategy ID', strategyRecords[0].name === '我的阈值策略' && strategyRecords[0].current_version === oldVersion && strategyRecords[0].strategy_id === 'strategy-ui')

// ---- report --------------------------------------------------------------
for (const line of notes) console.log(line)
if (failures.length > 0) {
  console.error(`\nFAILED ${failures.length} render check(s):`)
  for (const failure of failures) console.error(` - ${failure}`)
  process.exit(1)
}
console.log(`\nAll ${notes.length} render checks passed.`)
