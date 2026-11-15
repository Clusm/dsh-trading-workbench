#!/usr/bin/env node
/**
 * §8·E 链路价值对照（离线、只读、可重复）
 *
 * 目的：同一批「历史时点证据 + 同一次模型输出」上，比较两条固定链路变体
 *   baseline = analysis + R1 + R2 + decision + risk + execution（8 次 LLM 调用）
 *   lean     = R1 + R2 + risk + execution（6 次；decision 由 scorecard 确定性直选）
 * 只比较：计划质量、有效率、耗时、成本。**不产生任何回测收益/盈亏**，也不调用
 * 任何交易写接口（本脚本不 import 任何 HTTP 客户端，不读 token，不发请求）。
 *
 * 为什么用 replay：变体差异必须只来自角色数，不能混入"模型这次心情不同"。
 * 所以把每轮当时真实产生的 8 份模型输出原样喂回链条（同一文本、同一 usage、
 * 同一延迟），两个变体共用同一份输入。没有可靠成交模型，因此不算收益。
 *
 * 用法：
 *   node scripts/evaluate-agent-chain.mjs                      # 读 T01 只读库副本
 *   node scripts/evaluate-agent-chain.mjs --limit 20           # 只跑前 20 轮
 *   node scripts/evaluate-agent-chain.mjs --out /tmp/t17       # 写 JSON + CSV
 *   node scripts/evaluate-agent-chain.mjs --from-sample f.json # 用离线样本（CI/测试）
 *   node scripts/evaluate-agent-chain.mjs --json              # 只打印 JSON
 *
 * 退出码：0 = 正常（含"证据不足→保留 baseline"的结论）；2 = 参数/输入错误。
 */

import { createHash } from 'node:crypto'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath, pathToFileURL } from 'node:url'

import { CHAIN_VERSION, CHAIN_VARIANTS, PROMPT_VERSIONS, VARIANT_ROLES } from '../host/ai/constants.js'
import { runChain } from '../host/ai/chain.js'
import { buildEvidenceView } from '../host/ai/evidence.js'

const CONTRACT = 't17/chain-replay/1'
const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url))
const BASE_DIR = path.resolve(SCRIPT_DIR, '..')
const DEFAULT_DB = path.join(BASE_DIR, 'data-t01-baseline', 'workbench-v1.sqlite')
const DEFAULT_DATA = path.join(BASE_DIR, 'data')

const ROLE_KEYS = ['analysis', 'red_opening', 'blue_opening', 'red_response', 'blue_response', 'decision', 'risk', 'execution_entry']

/** 进行中的交易写接口一律不碰：这里只声明，供测试与审计核对。 */
export const SAFETY = Object.freeze({
  contract: CONTRACT,
  network: 'none (no http client imported; no network calls in this file)',
  trading_writes: 'none (execution, control and order endpoints are never called)',
  backtest_returns: 'not produced (no pnl/return fields; no fill model)',
  inputs: 'read-only sqlite (mode=ro) + read-only evidence json',
})

function sha256(text) {
  return 'sha256:' + createHash('sha256').update(text).digest('hex')
}

/** 稳定序列化：键排序，保证同一输入永远得到同一份 JSON。 */
export function stableStringify(value) {
  if (Array.isArray(value)) return '[' + value.map((item) => stableStringify(item)).join(',') + ']'
  if (value && typeof value === 'object') {
    return '{' + Object.keys(value).sort().map((key) => JSON.stringify(key) + ':' + stableStringify(value[key])).join(',') + '}'
  }
  return JSON.stringify(value ?? null)
}

function round2(value) {
  return Number.isFinite(value) ? Math.round(value * 100) / 100 : null
}

function median(list) {
  const sorted = list.filter((value) => Number.isFinite(value)).sort((a, b) => a - b)
  if (sorted.length === 0) return null
  const mid = Math.floor(sorted.length / 2)
  return sorted.length % 2 === 1 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2
}

function countBy(list) {
  return list.reduce((acc, item) => {
    const key = item ?? 'null'
    acc[key] = (acc[key] ?? 0) + 1
    return acc
  }, {})
}

function mean(list) {
  const valid = list.filter((value) => Number.isFinite(value))
  return valid.length === 0 ? null : valid.reduce((sum, value) => sum + value, 0) / valid.length
}

function textTokens(meta = {}) {
  const input = Number.isFinite(meta.input_tokens) ? meta.input_tokens : null
  const output = Number.isFinite(meta.output_tokens) ? meta.output_tokens : null
  return { input: input ?? 0, output: output ?? 0, complete: input !== null && output !== null }
}

// ---------------------------------------------------------------- 样本装载

function roleKeyOf(agent, roundIndex) {
  if (agent === 'analysis') return 'analysis'
  if (agent === 'decision') return 'decision'
  if (agent === 'risk') return 'risk'
  if (agent === 'execution') return 'execution_entry'
  if (agent === 'red') return roundIndex === 2 ? 'red_response' : 'red_opening'
  if (agent === 'blue') return roundIndex === 2 ? 'blue_response' : 'blue_opening'
  return null
}

function roundIndexOf(callId, attempt) {
  const match = typeof callId === 'string' ? callId.match(/:r(\d):/) : null
  if (match) return Number(match[1])
  return Number.isFinite(attempt) && attempt >= 2 ? 2 : 1
}

/** 从只读 SQLite 装载回放样本（node:sqlite，readOnly 打开，绝不写）。 */
export async function loadSampleFromDb({ dbPath = DEFAULT_DB, dataDir = DEFAULT_DATA, limit = null } = {}) {
  const { DatabaseSync } = await import('node:sqlite')
  const db = new DatabaseSync(dbPath, { readOnly: true })
  try {
    const cycles = db.prepare('select cycle_id, started_at, finished_at, outcome, reason_code, symbol from cycle_runs order by cycle_id').all()
    const evidenceRows = db.prepare('select cycle_id, produced_at_ms, verdict, path, sha256 from evidence_snapshots').all()
    // 注意：服务端把 risk/execution 的 artifact 标成 valid=0（E_SCHEMA_MISSING:
    // risk.verdict / execution.action），但这两份文本是当时真实通过 Host 校验、并且
    // 链条继续走下去了的模型输出。回放必须用"真实输出"，不能用服务端那个与 Host
    // schema 不一致的 valid 标志来筛（否则可用轮次 = 0）。标志本身仍保留在样本里供审计。
    const outputRows = db.prepare('select cycle_id, call_id, agent, attempt, output_json, valid, created_at, output_id from ai_outputs order by cycle_id, created_at, output_id').all()
    // latency_ms 是账本自己记的单次调用耗时；started_at/ended_at 在早期 build 里
    // 被写成整轮起止（2026-11-09 那批 9 个角色共用同一对时间戳），不能当延迟用。
    const callRows = db.prepare('select call_id, status, started_at, ended_at, latency_ms, input_tokens, output_tokens from ai_calls').all()

    const callsById = new Map(callRows.map((row) => [row.call_id, row]))
    const cycleStarts = new Map()
    for (const row of callRows) {
      const seen = cycleStarts.get(row.cycle_id) ?? new Set()
      seen.add(String(row.started_at ?? ''))
      cycleStarts.set(row.cycle_id, seen)
    }
    // 同轮所有调用共用同一个 started_at → 该轮的起止是"整轮级"，不能当单次延迟。
    const roundLevelTimestamps = new Set(
      [...cycleStarts].filter(([, seen]) => seen.size <= 1).map(([cycleId]) => cycleId),
    )
    const evidenceByCycle = new Map(evidenceRows.map((row) => [row.cycle_id, row]))
    const rolesByCycle = new Map()
    for (const row of outputRows) {
      const roundIndex = roundIndexOf(row.call_id, row.attempt)
      const key = roleKeyOf(row.agent, roundIndex)
      if (!key) continue
      const call = callsById.get(row.call_id) ?? {}
      const startedMs = Date.parse(String(call.started_at ?? ''))
      const endedMs = Date.parse(String(call.ended_at ?? ''))
      const bucket = rolesByCycle.get(row.cycle_id) ?? {}
      // 早期 build 把整轮起止写进每一次调用（同轮所有角色共用一个 started_at），
      // 那时的时间戳差不是延迟。只有"同轮时间戳不共享"时才允许用差值兜底。
      const sharedStart = roundLevelTimestamps.has(row.cycle_id)
      const measured = Number.isFinite(call.latency_ms)
        ? call.latency_ms
        : (!sharedStart && Number.isFinite(startedMs) && Number.isFinite(endedMs) ? Math.max(0, endedMs - startedMs) : null)
      const next = {
        text: row.output_json,
        status: call.status ?? 'ok',
        ledger_valid: row.valid === 1,
        created_at: row.created_at ?? null,
        latency_ms: measured,
        latency_source: Number.isFinite(call.latency_ms)
          ? 'ai_calls.latency_ms'
          : (!sharedStart ? 'timestamp_diff' : 'unavailable'),
        input_tokens: call.input_tokens ?? null,
        output_tokens: call.output_tokens ?? null,
      }
      // 同一角色可能有重试：优先保留 call.status=ok 的那次，避免回放拿到 invalid 文本。
      const current = bucket[key]
      if (!current || current.status !== 'ok' || next.status === 'ok') bucket[key] = next
      rolesByCycle.set(row.cycle_id, bucket)
    }

    const rounds = []
    const missing = []
    for (const cycle of cycles) {
      if (limit && rounds.length >= limit) break
      const evidenceRow = evidenceByCycle.get(cycle.cycle_id)
      const roles = rolesByCycle.get(cycle.cycle_id) ?? {}
      const absent = ROLE_KEYS.filter((key) => !roles[key]?.text)
      if (!evidenceRow) { missing.push({ cycle_id: cycle.cycle_id, reason: 'no_evidence_snapshot' }); continue }
      if (absent.length > 0) { missing.push({ cycle_id: cycle.cycle_id, reason: 'missing_role_outputs', roles: absent }); continue }
      const evidencePath = path.join(dataDir, String(evidenceRow.path).replace(/\//g, path.sep))
      if (!fs.existsSync(evidencePath)) { missing.push({ cycle_id: cycle.cycle_id, reason: 'evidence_file_missing', path: evidenceRow.path }); continue }
      let evidence
      try {
        evidence = JSON.parse(fs.readFileSync(evidencePath, 'utf8'))
      } catch (error) {
        missing.push({ cycle_id: cycle.cycle_id, reason: 'evidence_unreadable', detail: String(error.message) })
        continue
      }
      // md-evidence/1 不带头号标的，生产代码由 Host 用 round.symbol / universe.deep 选；
      // 这里照同一优先级补上，并写进 round 供投影使用。
      const symbolKeys = Object.keys(evidence.symbols ?? {})
      const primary = (cycle.symbol && evidence.symbols?.[cycle.symbol] ? cycle.symbol : null)
        ?? (evidence.symbols?.BTCUSDT ? 'BTCUSDT' : symbolKeys[0] ?? null)
      if (!primary) {
        missing.push({ cycle_id: cycle.cycle_id, reason: 'no_primary_symbol' })
        continue
      }
      evidence.primary_symbol = primary
      rounds.push({
        cycle_id: cycle.cycle_id,
        started_at: cycle.started_at,
        finished_at: cycle.finished_at,
        recorded_reason_code: cycle.reason_code ?? null,
        recorded_outcome: cycle.outcome ?? null,
        primary_symbol: primary,
        produced_at_ms: evidenceRow.produced_at_ms ?? Date.parse(String(evidence.produced_at ?? '')),
        evidence_sha256: evidenceRow.sha256 ?? null,
        evidence_path: evidenceRow.path,
        evidence,
        roles,
      })
    }
    return {
      contract: CONTRACT,
      source: { kind: 'sqlite', db: path.basename(dbPath), evidence_root: path.basename(dataDir) },
      rounds,
      missing,
    }
  } finally {
    db.close()
  }
}

// ---------------------------------------------------------------- 回放与比较

/**
 * 把服务端 md-evidence/1 原文投影成 chain 需要的 ai-evidence-view/1。
 *
 * 这是 `host/integration.js:398-479 buildServiceEvidence` 的**同一套映射**
 * （脚本不能 import integration.js：那是 Host 运行时模块，import 会拉起服务客户端）。
 * 差异只有一处并已在此固定：服务端 `as_of` 是对象（bar_age/clock），
 * 生产代码在 `as_of` 非字符串时回落到 `produced_at`，这里照做。
 * 非数组的 candles/bars（列式容器）必须丢弃，否则 chain 的投影会拒绝。
 */
function serviceEvidenceView(round, now) {
  const md = round.evidence
  const symbolsForView = {}
  for (const [key, value] of Object.entries(md.symbols ?? {})) {
    if (!value || typeof value !== 'object' || Array.isArray(value)) continue
    const clone = structuredClone(value)
    if (clone.candles !== undefined && !Array.isArray(clone.candles)) delete clone.candles
    if (clone.bars !== undefined && !Array.isArray(clone.bars)) delete clone.bars
    if (clone.timeframes && typeof clone.timeframes === 'object') {
      for (const tf of Object.values(clone.timeframes)) {
        if (tf && typeof tf === 'object' && tf.candles !== undefined && !Array.isArray(tf.candles)) delete tf.candles
      }
    }
    symbolsForView[key] = clone
  }
  return buildEvidenceView({
    snapshot_id: String(md.snapshot_id),
    cycle_id: String(md.cycle_id),
    produced_at: md.produced_at,
    as_of: typeof md.as_of === 'string' ? md.as_of : md.produced_at,
    data_health: md.data_health,
    market: md.market,
    primary_symbol: round.primary_symbol,
    symbols: symbolsForView,
    delivery: md.delivery,
  }, { now })
}

function roleKeyFromPayload(options) {
  const raw = options?.messages?.[0]?.content?.[0]?.text ?? ''
  const payload = JSON.parse(raw.split('\n\n')[0])
  const role = payload?.task?.role
  const round = payload?.task?.round
  if (role === 'analysis') return 'analysis'
  if (role === 'red_team') return round === 2 ? 'red_response' : 'red_opening'
  if (role === 'blue_team') return round === 2 ? 'blue_response' : 'blue_opening'
  if (role === 'decision') return 'decision'
  if (role === 'risk') return 'risk'
  if (role === 'execution') return payload?.output_schema === 'execution_manage/1' ? 'execution_manage' : 'execution_entry'
  throw new Error('unknown replay role ' + String(role))
}

/** 用当轮真实模型输出回放：同一文本、同一 usage、同一 finish。 */
function makeReplayLlm(roles) {
  return {
    stream(options) {
      const roleKey = roleKeyFromPayload(options)
      const recorded = roles[roleKey]
      if (!recorded?.text) throw new Error('no recorded output for ' + roleKey)
      return (async function* replay() {
        yield { type: 'text-delta', text: recorded.text }
        yield {
          type: 'usage',
          usage: {
            inputTokens: recorded.input_tokens ?? 0,
            outputTokens: recorded.output_tokens ?? 0,
            totalTokens: (recorded.input_tokens ?? 0) + (recorded.output_tokens ?? 0),
          },
        }
        yield { type: 'finish', reason: { kind: 'stop' } }
      })()
    },
  }
}

function planSignature(plan) {
  if (!plan || typeof plan !== 'object') return null
  const copy = structuredClone(plan)
  // decision 角色被允许下调 risk_pct/leverage；这两项不算"计划质量差异"。
  delete copy.risk_pct
  delete copy.leverage
  return stableStringify(copy)
}

function planMetrics(plan, price) {
  const low = Number.isFinite(plan?.entry?.zone_low) ? plan.entry.zone_low : null
  const high = Number.isFinite(plan?.entry?.zone_high) ? plan.entry.zone_high : null
  const reference = Number.isFinite(price) ? price : (Number.isFinite(plan?.entry?.price) ? plan.entry.price : null)
  return {
    has_plan: Boolean(plan),
    has_zone: low !== null && high !== null,
    has_valid_until: typeof plan?.valid_until === 'string' && Number.isFinite(Date.parse(plan.valid_until)),
    zone_width_pct: low !== null && high !== null && reference ? round2(((high - low) / reference) * 100) : null,
    rr: Number.isFinite(plan?.take_profit?.rr) ? plan.take_profit.rr : (Number.isFinite(plan?.rr_to_target) ? plan.rr_to_target : null),
    sl_distance_pct: Number.isFinite(plan?.stop_loss?.distance_pct) ? plan.stop_loss.distance_pct : null,
    risk_pct: Number.isFinite(plan?.risk_pct) ? plan.risk_pct : null,
    leverage: Number.isFinite(plan?.leverage) ? plan.leverage : null,
  }
}

function criticalPathMs(roles) {
  const at = (key) => (Number.isFinite(roles[key]?.latency_ms) ? roles[key].latency_ms : Number.NaN)
  return {
    analysis: at('analysis'),
    r1: Math.max(at('red_opening'), at('blue_opening')),
    r2: Math.max(at('red_response'), at('blue_response')),
    decision: at('decision'),
    risk: at('risk'),
    execution_entry: at('execution_entry'),
  }
}

function variantCost(roles, variant) {
  const used = VARIANT_ROLES[variant]
  const latencies = used.map((roleKey) => (Number.isFinite(roles[roleKey]?.latency_ms) ? roles[roleKey].latency_ms : null))
  const tokenPairs = used.map((roleKey) => textTokens(roles[roleKey]))
  const latencyComplete = latencies.every((value) => value !== null)
  const tokensComplete = tokenPairs.every((pair) => pair.complete)
  const input = tokensComplete ? tokenPairs.reduce((sum, pair) => sum + pair.input, 0) : null
  const output = tokensComplete ? tokenPairs.reduce((sum, pair) => sum + pair.output, 0) : null
  const path = criticalPathMs(roles)
  const critical = variant === 'baseline'
    ? path.analysis + path.r1 + path.r2 + path.decision + path.risk + path.execution_entry
    : path.r1 + path.r2 + path.risk + path.execution_entry
  return {
    input_tokens: input,
    output_tokens: output,
    total_tokens: tokensComplete ? input + output : null,
    sum_latency_ms: latencyComplete ? latencies.reduce((sum, value) => sum + value, 0) : null,
    critical_path_ms: latencyComplete ? critical : null,
    latency_coverage: latencyComplete,
    token_coverage: tokensComplete,
  }
}

export async function replayRound(round, variant) {
  const nowMs = Number.isFinite(round.produced_at_ms) ? round.produced_at_ms + 5_000 : Date.parse(String(round.evidence?.produced_at ?? '')) + 5_000
  const result = await runChain({
    llm: makeReplayLlm(round.roles),
    route: { provider: 'replay', model: 'recorded-' + (round.cycle_id ?? 'unknown') },
    evidence: serviceEvidenceView(round, nowMs),
    hardRisk: { verdict: 'approved' },
    config: {
      now: () => nowMs,
      chainVariant: variant,
      executionGate: 'observe',
      debateMode: 'symmetric_two_round',
      chainDeadlineMs: 600_000,
      roleTimeoutsMs: { analysis: 600_000, red_opening: 600_000, blue_opening: 600_000, red_response: 600_000, blue_response: 600_000, decision: 600_000, risk: 600_000, execution_entry: 600_000 },
    },
  })
  return result
}

function roundRow(round, variant, result) {
  const decision = result?.outputs?.decision ?? null
  const execution = result?.outputs?.execution ?? null
  const gate = result?.outputs?.gate ?? null
  const price = round.evidence?.symbols?.[round.evidence?.primary_symbol]?.price?.last ?? null
  const cost = variantCost(round.roles, variant)
  return {
    cycle_id: round.cycle_id,
    date: typeof round.started_at === 'string' ? round.started_at.slice(0, 10) : null,
    variant,
    outcome: result?.outcome ?? 'failed',
    reason_code: result?.reasonCode ?? null,
    failure_category: result?.failureCategory ?? null,
    calls: Array.isArray(result?.calls) ? result.calls.length : 0,
    role_sequence: Array.isArray(result?.calls) ? result.calls.map((call) => call.role_key) : [],
    gate_ok: gate ? Boolean(gate.ok) : null,
    gate_code: gate?.code ?? null,
    gate_sub_code: gate?.sub_code ?? null,
    gate_quote_age_ms: gate?.quote?.age_ms ?? null,
    gate_quote_source: gate?.quote?.source ?? null,
    execution_executable: execution ? execution.executable === true : null,
    plan: planMetrics(decision?.plan, price),
    plan_signature: planSignature(decision?.plan),
    decision_action: decision?.action ?? null,
    decision_selected_case: decision?.selected_case ?? null,
    ...cost,
  }
}

function aggregate(rows) {
  if (rows.length === 0) {
    return {
      rounds: 0, trade: 0, no_trade: 0, failed: 0, gate_ok: 0,
      gate_pass_rate: null, effective_rate: null,
      critical_path_ms: { mean: null, median: null }, sum_latency_ms: { mean: null, median: null },
      tokens: { input: null, output: null, total: null },
      plan: { with_zone: 0, with_valid_until: 0, rr_mean: null, sl_distance_mean: null },
      gate_codes: {},
    }
  }
  return {
    rounds: rows.length,
    trade: rows.filter((row) => row.outcome === 'trade').length,
    no_trade: rows.filter((row) => row.outcome === 'no_trade').length,
    failed: rows.filter((row) => row.outcome === 'failed').length,
    gate_ok: rows.filter((row) => row.gate_ok === true).length,
    gate_pass_rate: round2(rows.filter((row) => row.gate_ok === true).length / rows.length),
    effective_rate: round2(rows.filter((row) => row.outcome === 'trade').length / rows.length),
    latency_rounds: rows.filter((row) => Number.isFinite(row.critical_path_ms)).length,
    token_rounds: rows.filter((row) => Number.isFinite(row.total_tokens)).length,
    critical_path_ms: { mean: Math.round(mean(rows.map((row) => row.critical_path_ms)) ?? 0), median: Math.round(median(rows.map((row) => row.critical_path_ms)) ?? 0) },
    sum_latency_ms: { mean: Math.round(mean(rows.map((row) => row.sum_latency_ms)) ?? 0), median: Math.round(median(rows.map((row) => row.sum_latency_ms)) ?? 0) },
    tokens: {
      input: Math.round(mean(rows.map((row) => row.input_tokens)) ?? 0),
      output: Math.round(mean(rows.map((row) => row.output_tokens)) ?? 0),
      total: Math.round(mean(rows.map((row) => row.total_tokens)) ?? 0),
    },
    plan: {
      with_zone: rows.filter((row) => row.plan.has_zone).length,
      with_valid_until: rows.filter((row) => row.plan.has_valid_until).length,
      rr_mean: round2(mean(rows.map((row) => row.plan.rr))),
      sl_distance_mean: round2(mean(rows.map((row) => row.plan.sl_distance_pct))),
    },
    gate_codes: countBy(rows.map((row) => row.gate_code ?? 'NO_GATE')),
  }
}

function pairedDeltas(rowsByVariant) {
  const baseline = new Map(rowsByVariant.baseline.map((row) => [row.cycle_id, row]))
  const lean = new Map(rowsByVariant.lean.map((row) => [row.cycle_id, row]))
  const pairs = [...baseline.keys()].filter((id) => lean.has(id)).sort()
  const deltas = pairs.map((id) => {
    const b = baseline.get(id)
    const l = lean.get(id)
    const num = (x) => (Number.isFinite(x) ? x : null)
    const diff = (left, right) => (Number.isFinite(left) && Number.isFinite(right) ? left - right : null)
    return {
      cycle_id: id,
      critical_path_ms: diff(l.critical_path_ms, b.critical_path_ms),
      sum_latency_ms: diff(l.sum_latency_ms, b.sum_latency_ms),
      total_tokens: diff(num(l.total_tokens), num(b.total_tokens)),
      input_tokens: diff(num(l.input_tokens), num(b.input_tokens)),
      output_tokens: diff(num(l.output_tokens), num(b.output_tokens)),
      plan_identical: b.plan_signature !== null && b.plan_signature === l.plan_signature,
      plan_identical_when_same_selection: b.decision_selected_case === l.decision_selected_case
        && b.plan_signature !== null && b.plan_signature === l.plan_signature,
      gate_identical: b.gate_code === l.gate_code,
      gate_identical_when_same_selection: b.decision_selected_case === l.decision_selected_case && b.gate_code === l.gate_code,
      outcome_identical: b.outcome === l.outcome,
      selection_identical: b.decision_selected_case === l.decision_selected_case,
      selection_baseline: b.decision_selected_case,
      selection_lean: l.decision_selected_case,
      gate_baseline: b.gate_code,
      gate_lean: l.gate_code,
      outcome_baseline: b.outcome,
      outcome_lean: l.outcome,
    }
  })
  const sameSelection = deltas.filter((item) => item.selection_identical)
  const latencyPairs = deltas.filter((item) => Number.isFinite(item.critical_path_ms))
  const tokenPairs = deltas.filter((item) => Number.isFinite(item.total_tokens))
  return {
    pairs: pairs.length,
    latency_pairs: latencyPairs.length,
    token_pairs: tokenPairs.length,
    critical_path_ms: {
      mean_delta: latencyPairs.length ? Math.round(mean(latencyPairs.map((item) => item.critical_path_ms))) : null,
      median_delta: latencyPairs.length ? Math.round(median(latencyPairs.map((item) => item.critical_path_ms))) : null,
    },
    sum_latency_ms: { mean_delta: latencyPairs.length ? Math.round(mean(latencyPairs.map((item) => item.sum_latency_ms))) : null },
    tokens: {
      mean_total_delta: tokenPairs.length ? Math.round(mean(tokenPairs.map((item) => item.total_tokens))) : null,
      mean_input_delta: tokenPairs.length ? Math.round(mean(tokenPairs.map((item) => item.input_tokens))) : null,
      mean_output_delta: tokenPairs.length ? Math.round(mean(tokenPairs.map((item) => item.output_tokens))) : null,
    },
    plan_identical_rate: round2(deltas.filter((item) => item.plan_identical).length / (pairs.length || 1)),
    plan_identical_rate_same_selection: sameSelection.length
      ? round2(sameSelection.filter((item) => item.plan_identical).length / sameSelection.length)
      : null,
    gate_identical_rate: round2(deltas.filter((item) => item.gate_identical).length / (pairs.length || 1)),
    gate_identical_rate_same_selection: sameSelection.length
      ? round2(sameSelection.filter((item) => item.gate_identical).length / sameSelection.length)
      : null,
    outcome_identical_rate: round2(deltas.filter((item) => item.outcome_identical).length / (pairs.length || 1)),
    selection_identical_rate: round2(deltas.filter((item) => item.selection_identical).length / (pairs.length || 1)),
    selection_disagreements: deltas.filter((item) => !item.selection_identical).map((item) => ({
      cycle_id: item.cycle_id,
      baseline: item.selection_baseline,
      lean: item.selection_lean,
      gate_baseline: item.gate_baseline,
      gate_lean: item.gate_lean,
      outcome_baseline: item.outcome_baseline,
      outcome_lean: item.outcome_lean,
    })),
    per_round: deltas,
  }
}

/**
 * 纯函数式比较：输入一份样本（DB 装载或 --from-sample），输出报告对象。
 * 同一输入 → 同一 JSON（无时间戳、无随机、轮次按 cycle_id 排序）。
 */
export async function buildComparison(sample, options = {}) {
  const rounds = [...(sample.rounds ?? [])].sort((a, b) => String(a.cycle_id).localeCompare(String(b.cycle_id)))
  const rowsByVariant = { baseline: [], lean: [] }
  const errors = []
  for (const round of rounds) {
    for (const variant of [CHAIN_VARIANTS.BASELINE, CHAIN_VARIANTS.LEAN]) {
      try {
        const result = await replayRound(round, variant)
        rowsByVariant[variant].push(roundRow(round, variant, result))
      } catch (error) {
        errors.push({ cycle_id: round.cycle_id, variant, error: String(error?.message ?? error) })
        rowsByVariant[variant].push({
          cycle_id: round.cycle_id, variant, outcome: 'replay_error', reason_code: error?.code ?? 'REPLAY_ERROR',
          failure_category: null, calls: 0, role_sequence: [], gate_ok: null, gate_code: null, gate_sub_code: null,
          gate_quote_age_ms: null, gate_quote_source: null, execution_executable: null,
          plan: { has_plan: false, has_zone: false, has_valid_until: false, zone_width_pct: null, rr: null, sl_distance_pct: null, risk_pct: null, leverage: null },
          plan_signature: null, decision_action: null, decision_selected_case: null,
          input_tokens: 0, output_tokens: 0, total_tokens: 0, sum_latency_ms: 0, critical_path_ms: 0,
        })
      }
    }
  }
  const aggregates = { baseline: aggregate(rowsByVariant.baseline), lean: aggregate(rowsByVariant.lean) }
  const deltas = pairedDeltas(rowsByVariant)
  // 选边与"当时真实 decision"一致的轮次才算可比较：回放的 risk/execution 文本是
  // 依据当时那一侧的决策产生的，lean 若选了另一侧，下游文本对不上（side drift），
  // 那不是质量差异而是回放假象。这类轮次单独列出，不参与有效率比较。
  const comparableIds = new Set(deltas.per_round.filter((item) => item.selection_identical).map((item) => item.cycle_id))
  const comparableRows = {
    baseline: rowsByVariant.baseline.filter((row) => comparableIds.has(row.cycle_id)),
    lean: rowsByVariant.lean.filter((row) => comparableIds.has(row.cycle_id)),
  }
  const effectiveRate = (rows) => (rows.length === 0 ? null : round2(rows.filter((row) => row.outcome === 'trade').length / rows.length))
  const comparable = {
    rounds: comparableIds.size,
    selection_divergent_rounds: rowsByVariant.baseline.length - comparableIds.size,
    baseline_trade: comparableRows.baseline.filter((row) => row.outcome === 'trade').length,
    lean_trade: comparableRows.lean.filter((row) => row.outcome === 'trade').length,
    baseline_effective_rate: effectiveRate(comparableRows.baseline),
    lean_effective_rate: effectiveRate(comparableRows.lean),
    baseline_gate: countBy(comparableRows.baseline.map((row) => row.gate_code ?? 'NO_GATE')),
    lean_gate: countBy(comparableRows.lean.map((row) => row.gate_code ?? 'NO_GATE')),
    note: 'selection-identical rounds only; divergent rounds cannot be judged by replay (recorded risk/execution belong to the original side)',
  }
  const byDate = {}
  for (const date of [...new Set(rowsByVariant.baseline.map((row) => row.date))].filter(Boolean).sort()) {
    const base = rowsByVariant.baseline.filter((row) => row.date === date)
    const leanRows = rowsByVariant.lean.filter((row) => row.date === date)
    byDate[date] = {
      rounds: base.length,
      baseline_trade: base.filter((row) => row.outcome === 'trade').length,
      lean_trade: leanRows.filter((row) => row.outcome === 'trade').length,
      baseline_critical_path_median: base.length ? Math.round(median(base.map((row) => row.critical_path_ms)) ?? 0) : null,
      lean_critical_path_median: leanRows.length ? Math.round(median(leanRows.map((row) => row.critical_path_ms)) ?? 0) : null,
    }
  }
  const sampleDigest = sha256(stableStringify(rounds.map((round) => ({
    cycle_id: round.cycle_id,
    produced_at_ms: round.produced_at_ms,
    evidence_sha256: round.evidence_sha256,
    roles: Object.fromEntries(ROLE_KEYS.map((key) => [key, sha256(String(round.roles?.[key]?.text ?? ''))])),
  }))))
  const report = {
    contract: CONTRACT,
    deterministic: true,
    sample: {
      digest: sampleDigest,
      source: sample.source ?? null,
      rounds: rounds.length,
      latency_source: rounds.reduce((acc, round) => {
        for (const role of ROLE_KEYS) {
          const source = round.roles?.[role]?.latency_source ?? 'missing'
          acc[source] = (acc[source] ?? 0) + 1
        }
        return acc
      }, {}),
      first_started_at: rounds.map((round) => round.started_at).filter(Boolean).sort()[0] ?? null,
      last_started_at: rounds.map((round) => round.started_at).filter(Boolean).sort().slice(-1)[0] ?? null,
      missing: sample.missing ?? [],
      missing_counts: countMissing(sample.missing ?? []),
    },
    versions: {
      chain_version: CHAIN_VERSION,
      prompt_versions: Object.fromEntries(ROLE_KEYS.map((key) => [key, PROMPT_VERSIONS[key] ?? null])),
      variants: {
        baseline: [ ...VARIANT_ROLES.baseline ],
        lean: [ ...VARIANT_ROLES.lean ],
      },
    },
    parameters: {
      evidence_gate: 'same evidence snapshot per round; chain now = produced_at + 5000ms',
      execution_gate: 'observe (same evaluateEntryPlan on both variants)',
      debate_mode: 'symmetric_two_round (recorded R1+R2 outputs replayed)',
      hard_risk: 'approved (variant comparison is about roles, not risk policy)',
      llm: 'replay: recorded model output text + recorded usage; no live model call',
    },
    safety: SAFETY,
    aggregates,
    comparable,
    by_date: byDate,
    deltas: { ...deltas, per_round: undefined },
    errors,
    rows: { baseline: rowsByVariant.baseline, lean: rowsByVariant.lean },
    conclusion: null,
  }
  report.conclusion = conclude(report, options)
  const canonical = stableStringify({ ...report, report_sha256: undefined })
  report.report_sha256 = sha256(canonical)
  return report
}

function countMissing(missing) {
  const counts = {}
  for (const item of missing) counts[item.reason] = (counts[item.reason] ?? 0) + 1
  return counts
}

/**
 * 结论规则（写死，避免"看着数字编故事"）：
 *   - 计划质量或闸门判定不一致 → 不能替换（reject）
 *   - 轮次少于 MIN_ROUNDS → 证据不足，保留 baseline
 *   - 计划一致、闸门一致、且 lean 的关键路径与成本确实更低、选择在 X% 轮次上相同
 *     → 建议保留 baseline 为默认，lean 作为可选（suggest_optional）
 * 任何情况下都**不**自动切换默认链路；切换需要人决策。
 */
/**
 * 结论规则（写死，避免"看着数字编故事"）。判定只在"选边相同"的轮次上比较
 * 计划与闸门，否则会把"选边不同"误读成"计划被改"。
 */
export function conclude(report, options = {}) {
  const minRounds = Number.isFinite(options.minRounds) ? options.minRounds : 20
  const samePlanThreshold = Number.isFinite(options.samePlanThreshold) ? options.samePlanThreshold : 0.95
  const sameSelectionThreshold = Number.isFinite(options.sameSelectionThreshold) ? options.sameSelectionThreshold : 0.95
  const rounds = report.sample.rounds
  const b = report.aggregates.baseline
  const l = report.aggregates.lean
  const d = report.deltas
  const findings = []
  const blockers = []
  if (rounds < minRounds) blockers.push('样本轮次 ' + rounds + ' < ' + minRounds + '，不构成分布')
  const planSame = d.plan_identical_rate_same_selection
  const gateSame = d.gate_identical_rate_same_selection
  const selectionRate = d.selection_identical_rate
  if (planSame === null || planSame < samePlanThreshold) {
    blockers.push('选边相同的轮次里计划一致率 ' + planSame + ' < ' + samePlanThreshold + '：decision 角色确实在改计划（不只是复制）')
  }
  if (gateSame === null || gateSame < 1) {
    blockers.push('选边相同的轮次里闸门判定一致率 ' + gateSame + ' < 1：同一份 evaluateEntryPlan 不应因变体而异')
  }
  // 可执行率只在"选边与当时真实决策一致"的轮次上比较：其余轮次的下游文本
  // 属于原侧，回放判不了 lean 的好坏（不是质量差异）。
  const cmp = report.comparable ?? null
  const tradeDelta = cmp
    ? (cmp.lean_trade ?? 0) - (cmp.baseline_trade ?? 0)
    : (l.trade ?? 0) - (b.trade ?? 0)
  if (tradeDelta < 0) {
    blockers.push('可比较轮次里 lean 少 ' + (-tradeDelta) + ' 笔可执行计划（可执行率 '
      + (cmp?.lean_effective_rate ?? l.effective_rate) + ' vs ' + (cmp?.baseline_effective_rate ?? b.effective_rate)
      + '）：精简损害了必要功能')
  }
  if (selectionRate === null || selectionRate < sameSelectionThreshold) {
    findings.push('选边一致率 ' + selectionRate + '：decision 与 scorecard 在 '
      + (d.selection_disagreements?.length ?? '?') + ' 轮里选了不同一侧，说明 decision 不是重复角色。'
      + '其中 ' + (cmp?.selection_divergent_rounds ?? '?') + ' 轮无法用回放判定（下游 risk/execution 文本属于原侧），'
      + '差异轮次见 selection_disagreements，需逐轮人工判定谁对')
  }
  const latencyGain = (b.critical_path_ms.median ?? 0) - (l.critical_path_ms.median ?? 0)
  const tokenGain = (b.tokens.total ?? 0) - (l.tokens.total ?? 0)
  if (latencyGain <= 0) blockers.push('关键路径中位数没有变短（' + latencyGain + 'ms），精简无收益')
  if (tokenGain <= 0) blockers.push('token 没有变少（' + tokenGain + '）')
  if (report.errors.length > 0) blockers.push('回放错误 ' + report.errors.length + ' 条，比较不完整')
  const clean = blockers.length === 0 && findings.length === 0
  const recommendation = clean ? 'keep_baseline_default_lean_optional' : 'keep_baseline'
  return {
    recommendation,
    default_variant: CHAIN_VARIANTS.BASELINE,
    automatic_switch: false,
    blocks_replacement: blockers,
    findings,
    gains: {
      critical_path_ms_median: Math.round(latencyGain),
      total_tokens_mean: Math.round(tokenGain),
      calls_per_round: (b.rounds ? 8 : 0) - (l.rounds ? 6 : 0),
      trade_delta: tradeDelta,
    },
    why: recommendation === 'keep_baseline'
      ? (blockers.length > 0
        ? '存在阻断项：' + blockers.join('；') + '。按 §8·E 第 5 条保留 baseline（默认链路不变）。'
        : '无阻断项，但 findings 表明精简没有证明自己是"无害的重复"：' + findings.join('；') + '。按 §8·E 第 5 条保留 baseline。')
      : '计划与闸门判定一致、且关键路径与成本确有下降：lean 可作为可选变体，但默认仍为 baseline，切换需人决策。',
  }
}

// ---------------------------------------------------------------- 输出

export function renderCsv(report) {
  const header = [
    'cycle_id', 'variant', 'outcome', 'reason_code', 'failure_category', 'calls',
    'gate_ok', 'gate_code', 'gate_sub_code', 'execution_executable',
    'decision_action', 'decision_selected_case',
    'plan_has_zone', 'plan_has_valid_until', 'plan_zone_width_pct', 'plan_rr', 'plan_sl_distance_pct', 'plan_risk_pct', 'plan_leverage',
    'critical_path_ms', 'sum_latency_ms', 'input_tokens', 'output_tokens', 'total_tokens',
  ]
  const lines = [header.join(',')]
  for (const variant of ['baseline', 'lean']) {
    for (const row of report.rows[variant]) {
      lines.push([
        row.cycle_id, row.variant, row.outcome, row.reason_code ?? '', row.failure_category ?? '', row.calls,
        row.gate_ok === null ? '' : row.gate_ok, row.gate_code ?? '', row.gate_sub_code ?? '', row.execution_executable === null ? '' : row.execution_executable,
        row.decision_action ?? '', row.decision_selected_case ?? '',
        row.plan.has_zone, row.plan.has_valid_until, row.plan.zone_width_pct ?? '', row.plan.rr ?? '', row.plan.sl_distance_pct ?? '', row.plan.risk_pct ?? '', row.plan.leverage ?? '',
        row.critical_path_ms, row.sum_latency_ms, row.input_tokens, row.output_tokens, row.total_tokens,
      ].map((value) => (typeof value === 'string' && value.includes(',') ? JSON.stringify(value) : value)).join(','))
    }
  }
  return lines.join('\n') + '\n'
}

function parseArgs(argv) {
  const args = { db: DEFAULT_DB, data: DEFAULT_DATA, out: null, limit: null, json: false, fromSample: null }
  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i]
    if (token === '--db') args.db = argv[++i]
    else if (token === '--data') args.data = argv[++i]
    else if (token === '--out') args.out = argv[++i]
    else if (token === '--limit') args.limit = Number(argv[++i])
    else if (token === '--json') args.json = true
    else if (token === '--from-sample') args.fromSample = argv[++i]
    else if (token === '--help' || token === '-h') args.help = true
    else throw new Error('unknown argument: ' + token)
  }
  return args
}

export async function main(argv = process.argv.slice(2)) {
  const args = parseArgs(argv)
  if (args.help) {
    process.stdout.write([
      'usage: node scripts/evaluate-agent-chain.mjs [--db <sqlite>] [--data <dir>] [--out <dir>] [--limit N] [--json] [--from-sample <json>]',
      '',
      'Offline §8·E chain comparison. Read-only inputs; no trading API, no network, no backtest returns.',
    ].join('\n') + '\n')
    return 0
  }
  const sample = args.fromSample
    ? JSON.parse(fs.readFileSync(args.fromSample, 'utf8'))
    : await loadSampleFromDb({ dbPath: args.db, dataDir: args.data, limit: args.limit })
  if (!sample.rounds || sample.rounds.length === 0) {
    process.stderr.write('no usable rounds; missing: ' + JSON.stringify(sample.missing ?? []) + '\n')
    return 2
  }
  const report = await buildComparison(sample, {})
  const csv = renderCsv(report)
  const json = JSON.stringify(report, null, 2)
  if (args.out) {
    const resolved = args.out === true ? path.join(os.tmpdir(), 't17-eval') : path.resolve(args.out)
    fs.mkdirSync(resolved, { recursive: true })
    fs.writeFileSync(path.join(resolved, 't17-chain-comparison.json'), json + '\n', 'utf8')
    fs.writeFileSync(path.join(resolved, 't17-chain-comparison.csv'), csv, 'utf8')
    process.stderr.write('wrote ' + path.join(resolved, 't17-chain-comparison.json') + ' and .csv\n')
  }
  process.stdout.write(json + '\n')
  return 0
}

const invokedDirectly = process.argv[1] ? pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url : false
if (invokedDirectly) {
  main().then((code) => { process.exitCode = code }).catch((error) => {
    process.stderr.write('evaluate-agent-chain failed: ' + (error?.stack ?? error) + '\n')
    process.exitCode = 2
  })
}
