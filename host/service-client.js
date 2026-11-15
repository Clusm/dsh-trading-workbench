/**
 * Local workbench-service client for the M1.3b Host integration.
 *
 * The service is loopback-only and requires `Authorization: Bearer <token>` on
 * every `/api/*` call. This module reads the same token the service uses
 * (`WORKBENCH_API_TOKEN` env first, then `data/.api-token`) and never exposes
 * the token in an error, DTO or log line.
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import path from 'node:path'

export const DEFAULT_SERVICE_URL = 'http://127.0.0.1:8791'
export const DEFAULT_DATA_DIR = fileURLToPath(new URL('../../data/', import.meta.url))

export const DEFAULT_TOKEN_FILENAME = '.api-token'

export class ServiceClientError extends Error {
  constructor(code, message, { status = 502, retryable = false, details = {}, cause = null } = {}) {
    super(message)
    this.name = 'ServiceClientError'
    this.code = code
    this.status = status
    this.retryable = retryable
    this.details = details
    if (cause) this.cause = cause
  }
}

/**
 * Resolve the API token without ever printing its value.
 * Environment wins so a service started with WORKBENCH_API_TOKEN is usable.
 */
export function loadApiToken({ dataDir = DEFAULT_DATA_DIR, tokenFile = null, env = process.env } = {}) {
  const envToken = typeof env?.WORKBENCH_API_TOKEN === 'string' ? env.WORKBENCH_API_TOKEN.trim() : ''
  if (envToken) {
    return { token: envToken, source: 'env', path: null, present: true, error: null }
  }
  const file = tokenFile ? path.resolve(tokenFile) : path.join(path.resolve(dataDir), DEFAULT_TOKEN_FILENAME)
  try {
    const raw = readFileSync(file, 'utf8').trim()
    return raw
      ? { token: raw, source: 'file', path: file, present: true, error: null }
      : { token: null, source: 'file', path: file, present: false, error: 'token file is empty' }
  } catch (error) {
    if (error?.code === 'ENOENT') {
      return { token: null, source: 'none', path: file, present: false, error: 'token file not found' }
    }
    return {
      token: null,
      source: 'file',
      path: file,
      present: false,
      error: `token file unreadable: ${error?.code ?? error?.name ?? 'error'}`,
    }
  }
}

function parseJson(text) {
  if (!text) return null
  try {
    return JSON.parse(text)
  } catch {
    return { raw: text }
  }
}

function errorFromResponse(status, body, pathLabel) {
  const envelope = body?.error && typeof body.error === 'object' ? body.error : {}
  const code = typeof envelope.code === 'string' && envelope.code
    ? envelope.code
    : `E_SERVICE_HTTP_${status}`
  const message = typeof envelope.message === 'string' && envelope.message
    ? envelope.message
    : `workbench-service ${pathLabel} returned HTTP ${status}`
  return new ServiceClientError(code, message, {
    status,
    retryable: Boolean(envelope.retryable),
    details: {
      service_status: status,
      trace_id: body?.trace_id ?? null,
      ...(envelope.details && typeof envelope.details === 'object' ? envelope.details : {}),
    },
  })
}

/** Default per-request budget for ordinary service calls. */
export const DEFAULT_TIMEOUT_MS = 12_000
// POST /api/cycles builds and persists a full md-evidence/1 snapshot (market
// probe + universe + klines) before it answers. Measured 2026-11-13: it exceeded
// the 12s data budget, so the Host timed out while the service had ALREADY
// created the RUNNING cycle — the round failed and that orphan blocked every
// later round with E_CYCLE_RUNNING. Cycle creation is slow by design; give it
// its own budget instead of tightening the data routes.
export const CYCLE_TIMEOUT_MS = 60_000
/**
 * 03/实测（2026-11-12）：/api/ready 带 token 实测 15.05s（交易所探测在链路里），
 * 12s 预算把"服务正常但慢"误报成 E_SERVICE_TIMEOUT。就绪探测单独放宽到 20s，
 * 与其它调用的 12s 解耦——不是把全局超时放大。
 */
export const READY_TIMEOUT_MS = 20_000

export class WorkbenchServiceClient {
  constructor({ baseUrl = DEFAULT_SERVICE_URL, token, fetchImpl = globalThis.fetch, timeoutMs = DEFAULT_TIMEOUT_MS } = {}) {
    if (typeof fetchImpl !== 'function') throw new Error('fetchImpl is required')
    this.baseUrl = String(baseUrl).replace(/\/+$/, '')
    this.token = token
    this.fetchImpl = fetchImpl
    this.timeoutMs = timeoutMs
  }

  url(pathname) {
    return new URL(pathname, `${this.baseUrl}/`).toString()
  }

  async request(pathname, { method = 'GET', body = undefined, timeoutMs = this.timeoutMs, signal = undefined } = {}) {
    if (!this.token) {
      throw new ServiceClientError('E_SERVICE_TOKEN_MISSING', 'workbench-service API token is not configured', {
        status: 503,
        details: { hint: 'set WORKBENCH_API_TOKEN or create trading-workbench/data/.api-token' },
      })
    }
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), timeoutMs)
    const abort = () => controller.abort()
    if (signal) {
      if (signal.aborted) abort()
      else signal.addEventListener('abort', abort, { once: true })
    }
    const headers = {
      accept: 'application/json',
      authorization: `Bearer ${this.token}`,
    }
    if (body !== undefined) headers['content-type'] = 'application/json'
    try {
      const response = await this.fetchImpl(this.url(pathname), {
        method,
        headers,
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: controller.signal,
      })
      const text = await response.text()
      const parsed = parseJson(text)
      if (!response.ok) throw errorFromResponse(response.status, parsed, pathname)
      return parsed
    } catch (error) {
      if (error instanceof ServiceClientError) throw error
      if (error?.name === 'AbortError' || controller.signal.aborted) {
        throw new ServiceClientError('E_SERVICE_TIMEOUT', `workbench-service ${pathname} timed out`, {
          status: 503,
          retryable: true,
          details: { timeout_ms: timeoutMs },
        })
      }
      throw new ServiceClientError('E_SERVICE_OFFLINE', `cannot reach workbench-service at ${this.baseUrl}`, {
        status: 503,
        retryable: true,
        details: { path: pathname, cause: error?.code ?? error?.name ?? 'network' },
        cause: error,
      })
    } finally {
      clearTimeout(timer)
      if (signal) signal.removeEventListener('abort', abort)
    }
  }

  ready() {
    // Dedicated budget: readiness includes the exchange probe, which is slower
    // than data routes and must not masquerade as a timeout.
    return this.request('/api/ready', { timeoutMs: READY_TIMEOUT_MS })
  }

  health() {
    return this.request('/health')
  }

  status() {
    return this.request('/api/status')
  }

  positions() {
    return this.request('/api/positions')
  }

  orders(limit = 50) {
    return this.request(`/api/orders?limit=${encodeURIComponent(limit)}`)
  }

  fills(limit = 100) {
    return this.request(`/api/fills?limit=${encodeURIComponent(limit)}`)
  }

  equityCurve(range = 'all') {
    return this.request(`/api/equity-curve?range=${encodeURIComponent(range)}`)
  }

  reviews(limit = 20) {
    return this.request(`/api/reviews?limit=${encodeURIComponent(limit)}`)
  }

  riskEnvelope() {
    return this.request('/api/risk/envelope')
  }

  marketHealth() {
    return this.request('/api/market/health')
  }

  prices(symbols) {
    const list = Array.isArray(symbols) ? symbols.join(',') : String(symbols)
    return this.request(`/api/market/prices?symbols=${encodeURIComponent(list)}`)
  }

  evaluatePlan(plan, cycleId, leaseId = null) {
    const envelope = { plan }
    if (cycleId) envelope.cycle_id = cycleId
    if (leaseId) envelope.lease_id = leaseId
    return this.request('/api/risk/evaluate', { method: 'POST', body: envelope })
  }

  acquireLease(payload) {
    return this.request('/api/execution/lease/acquire', { method: 'POST', body: payload })
  }

  renewLease(payload) {
    return this.request('/api/execution/lease/renew', { method: 'POST', body: payload })
  }

  releaseLease(payload) {
    return this.request('/api/execution/lease/release', { method: 'POST', body: payload })
  }

  revokeLeaseRun(payload) {
    return this.request('/api/execution/lease/revoke_run', { method: 'POST', body: payload })
  }

  /**
   * @param {'auto'|'manual'|null} trigger  who asked for this cycle. The service
   * stores it in cycle_runs.trigger (default 'manual'); the Host must send it,
   * otherwise every automatic slot is recorded as a manual run (T01 G3: 155/155
   * rows were 'manual'). Omitted only by callers that genuinely are manual.
   */
  createCycle(cycleId, symbol = null, slotId = null, { timeoutMs = CYCLE_TIMEOUT_MS, trigger = null } = {}) {
    const body = { cycle_id: cycleId }
    if (symbol) body.symbol = symbol
    // The service rejects a duplicate slot_id with E_CYCLE_DUPLICATE_SLOT, which
    // the auto scheduler treats as "this slot is already done" instead of a failure.
    if (slotId) body.slot_id = String(slotId)
    if (trigger === 'auto' || trigger === 'manual') body.trigger = trigger
    return this.request('/api/cycles', { method: 'POST', body, timeoutMs })
  }

  listCycles(limit = 20, { timeoutMs = CYCLE_TIMEOUT_MS } = {}) {
    return this.request(`/api/cycles?limit=${encodeURIComponent(limit)}`, { timeoutMs })
  }

  finalizeCycle(cycleId, payload) {
    return this.request(`/api/cycles/${encodeURIComponent(cycleId)}/finalize`, { method: 'POST', body: payload })
  }

  // M1.3c AI audit endpoints. All are idempotent on the service side.
  createAiRun(cycleId, payload = {}) {
    return this.request(`/api/cycles/${encodeURIComponent(cycleId)}/ai-runs`, {
      method: 'POST',
      body: payload,
    })
  }

  putAiCall(aiRunId, callId, payload) {
    return this.request(
      `/api/ai-runs/${encodeURIComponent(aiRunId)}/calls/${encodeURIComponent(callId)}`,
      { method: 'PUT', body: payload },
    )
  }

  postAiEvent(aiRunId, payload) {
    return this.request(`/api/ai-runs/${encodeURIComponent(aiRunId)}/events`, {
      method: 'POST',
      body: payload,
    })
  }

  putArtifact(cycleId, agent, payload) {
    return this.request(`/api/cycles/${encodeURIComponent(cycleId)}/artifacts/${encodeURIComponent(agent)}`, {
      method: 'PUT',
      body: payload,
    })
  }

  submitOrder(payload) {
    return this.request('/api/execution/order', { method: 'POST', body: payload })
  }

  close(payload) {
    return this.request('/api/execution/close', { method: 'POST', body: payload })
  }

  repairProtection(payload = {}) {
    return this.request('/api/protection/repair', { method: 'POST', body: payload })
  }

  control(payload) {
    return this.request('/api/control', { method: 'POST', body: payload })
  }

  operations(limit = 100) {
    return this.request(`/api/operations?limit=${encodeURIComponent(limit)}`)
  }

  cycleEvidence(cycleId, agent = null) {
    const query = agent ? `?agent=${encodeURIComponent(agent)}` : ''
    return this.request(`/api/cycles/${encodeURIComponent(cycleId)}/evidence${query}`)
  }

  reconcile(payload = {}) {
    return this.request('/api/execution/reconcile', { method: 'POST', body: payload })
  }
}
