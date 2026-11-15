/**
 * M1.4 P2 daily review: Host-side, on-demand only.
 *
 * The review is generated from the Host state snapshot (local rounds plus the
 * proxied service portfolio). It is never cron-scheduled, never touches the
 * service write paths, and never fabricates a report when the LLM fails: the
 * caller gets the error and no cache file is written.
 */

import { ChainError } from './ai/errors.js'
import { streamPrompt } from './ai/llm.js'

export const REVIEW_TIMEZONE = 'Asia/Shanghai'
export const DEFAULT_REVIEW_TIMEOUT_MS = 60_000

const MAX_CONTEXT_ROUNDS = 20
const MAX_GAPS = 10

const SYSTEM_PROMPT = [
  '你是“交易工作台”的中文复盘助手。你会收到 Host 汇总的某一自然日（Asia/Shanghai）的结构化数据。',
  '',
  '硬性规则：',
  '1. 只使用给定数据，不得编造数值、订单、成交、行情或原因码；数据缺失就写“未提供”。',
  '2. 输出中文 Markdown，建议结构：## 今日概览 / ## 决策与执行 / ## 风险与保护 / ## 数据缺口 / ## 明日关注。',
  '3. 不做收益承诺、不预测价格、不给出买卖建议；可以指出流程与记录问题。',
  '4. 明确列出数据缺口与口径限制（例如 Host 列表上限、权益点缺失），不要用 0 或猜测填充。',
  '5. 若当日没有轮次或权益数据，直接说明“本日无记录”，不要补写故事。',
].join('\n')

/** YYYY-MM-DD in Asia/Shanghai (no DST, but Intl keeps it honest). */
export function dateKeyInShanghai(now = Date.now()) {
  const ts = typeof now === 'number' ? now : Date.parse(String(now))
  if (!Number.isFinite(ts)) return null
  return new Intl.DateTimeFormat('en-CA', {
    timeZone: REVIEW_TIMEZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
  }).format(new Date(ts))
}

/** Strict YYYY-MM-DD (rejects 2026-02-30 etc.). */
export function isValidReviewDate(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const [year, month, day] = value.split('-').map(Number)
  const date = new Date(Date.UTC(year, month - 1, day))
  return date.getUTCFullYear() === year
    && date.getUTCMonth() === month - 1
    && date.getUTCDate() === day
}

function numberOrNull(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    if (Number.isFinite(parsed)) return parsed
  }
  return null
}

function roundDateKey(round) {
  const raw = round?.finishedAt ?? round?.startedAt ?? null
  if (typeof raw !== 'string') return null
  return dateKeyInShanghai(raw)
}

function durationStats(rounds) {
  const values = rounds
    .map((round) => numberOrNull(round?.durationMs))
    .filter((value) => value !== null)
    .sort((a, b) => a - b)
  if (values.length === 0) return { count: 0, p50: null, max: null }
  return {
    count: values.length,
    p50: values[Math.floor((values.length - 1) / 2)],
    max: values[values.length - 1],
  }
}

function equityValue(point) {
  if (!point) return null
  return numberOrNull(point.available) ?? numberOrNull(point.equity) ?? numberOrNull(point.total_equity) ?? null
}

/**
 * Build the only data the LLM is allowed to see for `date`.
 * No token, no base URL, no prompt text, no raw provider payloads.
 */
export function buildReviewContext({ state, date }) {
  const rounds = Array.isArray(state?.rounds) ? state.rounds : []
  const todays = rounds.filter((round) => roundDateKey(round) === date)

  const outcomes = { trade: 0, no_trade: 0, failed: 0, other: 0 }
  const reasonCounts = new Map()
  for (const round of todays) {
    const outcome = round?.outcome
    if (outcome === 'trade' || outcome === 'no_trade' || outcome === 'failed') outcomes[outcome] += 1
    else outcomes.other += 1
    const code = round?.reasonCode
    if (typeof code === 'string' && code.length > 0) {
      reasonCounts.set(code, (reasonCounts.get(code) ?? 0) + 1)
    }
  }
  const reasonCodes = [...reasonCounts.entries()]
    .sort((a, b) => b[1] - a[1])
    .map(([code, count]) => ({ code, count }))

  const orders = Array.isArray(state?.orders) ? state.orders : []
  const fills = Array.isArray(state?.fills) ? state.fills : []
  const filledOrders = orders.filter((order) => String(order?.status ?? '').toUpperCase() === 'FILLED')

  const equityPoints = Array.isArray(state?.equityCurve?.points) ? state.equityCurve.points : []
  const dayPoints = equityPoints.filter((point) => (
    typeof point?.ts === 'number' && dateKeyInShanghai(point.ts) === date
  ))
  const firstPoint = dayPoints[0] ?? null
  const latestPoint = dayPoints[dayPoints.length - 1] ?? null
  const startEquity = equityValue(firstPoint)
  const latestEquity = equityValue(latestPoint)
  const change = startEquity !== null && latestEquity !== null
    ? Number((latestEquity - startEquity).toFixed(8))
    : null
  const changePct = change !== null && startEquity !== null && startEquity !== 0
    ? Number((change / startEquity * 100).toFixed(4))
    : null

  const account = state?.account && typeof state.account === 'object' ? state.account : {}
  const service = state?.service && typeof state.service === 'object' ? state.service : {}
  const gaps = Array.isArray(state?.gaps) ? state.gaps.slice(0, MAX_GAPS) : []

  return {
    date,
    timezone: REVIEW_TIMEZONE,
    rounds: {
      total: todays.length,
      outcomes,
      duration: durationStats(todays),
      reasonCodes,
      items: todays.slice(0, MAX_CONTEXT_ROUNDS).map((round) => ({
        roundId: round?.roundId ?? null,
        status: round?.status ?? null,
        outcome: round?.outcome ?? null,
        reasonCode: round?.reasonCode ?? null,
        symbol: round?.symbol ?? null,
        durationMs: numberOrNull(round?.durationMs),
      })),
      capped: todays.length > MAX_CONTEXT_ROUNDS ? MAX_CONTEXT_ROUNDS : null,
    },
    orders: {
      count: orders.length,
      filled: filledOrders.length,
      source: 'Host /state.orders (limit 50)',
    },
    fills: {
      count: fills.length,
      source: 'Host /state.fills (limit 100)',
    },
    equity: {
      points: dayPoints.length,
      start: startEquity,
      latest: latestEquity,
      change,
      changePct,
      source: 'Host /state.equityCurve (available)',
    },
    account: {
      equity: numberOrNull(account.equity ?? account.total_equity),
      available: numberOrNull(account.available ?? account.available_balance),
      upnl: numberOrNull(account.upnl ?? account.unrealized_pnl),
      dayPnl: numberOrNull(account.day_pnl ?? account.today_pnl),
    },
    risk: {
      controlState: service.control_state ?? null,
      newEntryAllowed: typeof service.new_entry_allowed === 'boolean' ? service.new_entry_allowed : null,
      serviceReady: service.ready === true,
    },
    gaps,
  }
}

export function buildReviewPrompt(data, { effort = null } = {}) {
  return {
    system: SYSTEM_PROMPT,
    user: [
      `复盘日期：${data.date}（${REVIEW_TIMEZONE}）`,
      '',
      '以下是 Host 汇总的当日数据（JSON）；只依据它写复盘，缺失写“未提供”：',
      '',
      JSON.stringify(data, null, 2),
    ].join('\n'),
    maxTokens: 2200,
    temperature: 0.3,
    // 02 §2.6 / F2: the caller passes the configured review effort; null only
    // means "model without reasoning" (the transport then omits the key).
    effort,
  }
}

/**
 * One LLM call, no retry. Throws ChainError on empty output, timeout, transport
 * failure or non-stop finish; the caller must not write a cache file then.
 */
export async function generateDailyReview({
  llm,
  route,
  effort = null,
  date,
  data,
  timeoutMs = DEFAULT_REVIEW_TIMEOUT_MS,
  sessionId = null,
}) {
  const prompt = buildReviewPrompt(data, { effort })
  const call = await streamPrompt({ llm, route, prompt, timeoutMs, role: 'daily_review', sessionId })
  const markdown = typeof call.text === 'string' ? call.text.trim() : ''
  if (markdown.length === 0) {
    throw new ChainError('LLM_EMPTY', 'daily review returned empty text', { role: 'daily_review' })
  }
  return {
    markdown,
    date,
    model: route?.model ?? null,
    requestedEffort: effort,
    generatedAt: new Date().toISOString(),
    usage: call.usage ?? null,
    latencyMs: call.latencyMs ?? null,
  }
}
