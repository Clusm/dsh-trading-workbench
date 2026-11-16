/**
 * Host half of @local/dsh-trading-workbench (M1.0 skeleton).
 *
 * Routes (loopback only):
 *  - GET  /trading-workbench/api/health
 *  - POST /trading-workbench/api/probe/llm
 *
 * The probe is POST-only so a simple GET can never trigger a model call.
 * It calls only Harness `ctx.llm.stream`, with a request-only user message:
 * no Session, no tools, no external HTTP, no API keys.
 */

const PLUGIN_ID = '@local/dsh-trading-workbench'
const PLUGIN_VERSION = '0.2.0-dev.1'
const API_PREFIX = '/trading-workbench/api'
const HEALTH_PATH = `${API_PREFIX}/health`
const PROBE_PATH = `${API_PREFIX}/probe/llm`
const PROBE_TIMEOUT_MS = 25_000

import { randomUUID } from 'node:crypto'
import {
  ROUNDS_PATH,
  RUN_PATH,
  STATE_PATH,
  createIntegration,
} from './host/integration.js'
import { createAutoScheduler } from './host/scheduler.js'
import { currentBuild, markHostLoad } from './host/build.js'
import { admittedWebRoutes } from './host/http-admission.js'

const SCHEDULE_PATH = `${API_PREFIX}/schedule`

// agentDefaultModel is required to resolve the route: without it the probe
// cannot know which provider/model Harness selected. It is a hard dependency,
// so it is declared here rather than read opportunistically.
export const inject = ['webServer', 'connection', 'llm', 'agentDefaultModel']

// Last probe result, kept in Host memory for /health. It is intentionally
// not persisted: after a Host restart it is "never" until the next probe.
let lastProbe = null

function isLoopback(address) {
  if (typeof address !== 'string') return false
  return address === '::1'
    || address === '::ffff:127.0.0.1'
    || address.startsWith('127.')
    || address.startsWith('::ffff:127.')
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
  res.writeHead(405, { allow: allowed.join(', '), 'content-type': 'application/json; charset=utf-8' })
  res.end(JSON.stringify({ ok: false, error: { code: 'METHOD_NOT_ALLOWED', message: `Allowed: ${allowed.join(', ')}` } }))
}

function normalizeFailure(error) {
  const failure = error?.failure ?? error
  const code = failure?.code ?? error?.code ?? error?.name ?? 'PROBE_FAILED'
  const message = failure?.message ?? error?.message ?? String(error)
  return { code: String(code), message: String(message) }
}

function normalizeUsage(chunk) {
  const raw = chunk?.usage ?? chunk
  if (raw === null || typeof raw !== 'object') return null
  const inputTokens = raw.inputTokens ?? raw.input_tokens ?? raw.promptTokens ?? raw.prompt_tokens ?? null
  const outputTokens = raw.outputTokens ?? raw.output_tokens ?? raw.completionTokens ?? raw.completion_tokens ?? null
  const totalTokens = raw.totalTokens ?? raw.total_tokens
    ?? (typeof inputTokens === 'number' && typeof outputTokens === 'number' ? inputTokens + outputTokens : null)
  const cacheReadTokens = raw.cacheReadTokens ?? raw.cache_read_tokens ?? null
  const cacheWriteTokens = raw.cacheWriteTokens ?? raw.cache_write_tokens ?? null
  return { inputTokens, outputTokens, totalTokens, cacheReadTokens, cacheWriteTokens, raw }
}

function normalizeFinish(chunk) {
  const reason = chunk?.reason ?? chunk
  if (reason === null || typeof reason !== 'object') return null
  const kind = reason.kind ?? 'unknown'
  const failure = reason.failure ? normalizeFailure(reason.failure) : null
  return { kind, failure, raw: chunk }
}

function resolveRoute(ctx) {
  const selection = ctx.agentDefaultModel.currentSelection()
  if (selection?.provider && selection?.model) {
    return { provider: selection.provider, model: selection.model, source: 'agentDefaultModel' }
  }
  return null
}

function neverProbe() {
  return { status: 'never', code: null, at: null, latencyMs: null }
}

function baseBody(route) {
  return {
    ok: true,
    plugin: PLUGIN_ID,
    version: PLUGIN_VERSION,
    contract: 'trading-workbench/m1.0',
    serverTime: new Date().toISOString(),
    llm: {
      // routeSelected means only that Harness has a default provider/model.
      // It does not prove credentials, adapter health, or a successful call;
      // lastProbe carries that evidence.
      routeSelected: Boolean(route),
      provider: route?.provider ?? null,
      model: route?.model ?? null,
      source: route?.source ?? null,
      lastProbe: lastProbe ?? neverProbe(),
    },
  }
}

function recordProbe(result) {
  lastProbe = {
    status: result?.ok ? 'success' : 'error',
    code: result?.ok ? null : (result?.error?.code ?? 'PROBE_FAILED'),
    at: new Date().toISOString(),
    latencyMs: typeof result?.latencyMs === 'number' ? result.latencyMs : null,
  }
  return lastProbe
}

function isTrustedProbeRequest(req, ctx) {
  // Same-origin browser POSTs send `Sec-Fetch-Site: same-origin` and usually
  // an Origin header. CLI/curl requests send neither and remain allowed.
  const site = req.headers['sec-fetch-site']
  if (typeof site === 'string' && site !== 'same-origin' && site !== 'none') return false
  const origin = req.headers.origin
  if (typeof origin === 'string' && origin.length > 0) {
    try {
      if (origin !== ctx.webServer.getOrigin()) return false
    } catch {
      return false
    }
  }
  return true
}

async function runProbe(ctx) {
  const route = resolveRoute(ctx)
  if (route === null) {
    return {
      ...baseBody(null),
      ok: false,
      finish: null,
      usage: null,
      error: {
        code: 'NO_ADAPTER',
        message: 'No default Harness model is selected. Choose a model in Harness first.',
      },
    }
  }

  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), PROBE_TIMEOUT_MS)
  const started = Date.now()
  let text = ''
  let usage = null
  let finish = null

  try {
    // Some provider routes use this opaque request id for transport routing.
    // It is not a Session object and no Session is created or read.
    const probeRequestId = `twb-probe-${randomUUID()}`
    const stream = ctx.llm.stream({
      provider: route.provider,
      model: route.model,
      sessionId: probeRequestId,
      messages: [{
        role: 'user',
        content: [{ type: 'text', text: 'Reply with exactly the word OK.' }],
      }],
      maxTokens: 64,
      signal: controller.signal,
    })

    for await (const chunk of stream) {
      if (chunk?.type === 'text-delta') {
        const delta = chunk.text ?? chunk.delta ?? ''
        if (typeof delta === 'string' && text.length < 4000) text += delta
      } else if (chunk?.type === 'usage') {
        usage = normalizeUsage(chunk)
      } else if (chunk?.type === 'finish') {
        finish = normalizeFinish(chunk)
      }
    }
  } catch (error) {
    const failure = normalizeFailure(error)
    return {
      ...baseBody(route),
      ok: false,
      finish,
      usage,
      text,
      latencyMs: Date.now() - started,
      error: failure.code === 'AbortError' || controller.signal.aborted
        ? { code: 'PROBE_TIMEOUT', message: `Probe exceeded ${PROBE_TIMEOUT_MS} ms.` }
        : failure,
    }
  } finally {
    clearTimeout(timer)
  }

  if (finish === null) {
    return {
      ...baseBody(route),
      ok: false,
      finish: null,
      usage,
      text,
      latencyMs: Date.now() - started,
      error: { code: 'PROBE_INCOMPLETE', message: 'The LLM stream ended without a finish chunk.' },
    }
  }

  if (finish.kind === 'error' || finish.kind === 'aborted') {
    return {
      ...baseBody(route),
      ok: false,
      finish,
      usage,
      text,
      latencyMs: Date.now() - started,
      error: finish.failure ?? {
        code: finish.kind === 'aborted' ? 'PROBE_ABORTED' : 'PROBE_FAILED',
        message: `LLM stream finished with ${finish.kind}.`,
      },
    }
  }

  return {
    ...baseBody(route),
    ok: finish.kind === 'stop',
    finish,
    usage,
    text,
    latencyMs: Date.now() - started,
    error: null,
  }
}

export function apply(ctx, config) {
  const pluginConfig = config ?? {}
  // Advance the exposed load identity on every apply(): ESM may cache
  // ./host/build.js across plugin reloads, and a frozen loaded_at would make
  // /health report a previous load while new routes are already serving.
  markHostLoad()
  lastProbe = null
  const integration = createIntegration({
    ctx,
    serviceBaseUrl: pluginConfig.serviceBaseUrl,
    dataDir: pluginConfig.dataDir,
    tokenFile: pluginConfig.tokenFile,
    config: pluginConfig.chain ?? {},
    // 15 §3: rounds are archived per day, so there is no round-count option to
    // pass any more. Retention is an environment choice:
    // WORKBENCH_ROUNDS_RETENTION_DAYS (0/unset = keep every day).
    logger: ctx.logger,
  })

  // 需求基线 §12：手动 + 每 10 分钟自动。默认开启，WORKBENCH_AUTO_SCHEDULE=off 关闭。
  const scheduler = createAutoScheduler({
    integration,
    env: pluginConfig.env ?? process.env,
    logger: ctx.logger,
    config: pluginConfig.autoSchedule ?? {},
  })
  integration.scheduleProvider = () => scheduler.status()

  ctx.effect(() => {
    const disposers = []
    const webRoutes = admittedWebRoutes(ctx)
    try {
      disposers.push(webRoutes.register({
        kind: 'exact',
        path: HEALTH_PATH,
        handler: (req, res) => {
          if (!isLoopback(req.socket?.remoteAddress)) {
            sendJson(res, 403, { ok: false, error: { code: 'LOOPBACK_ONLY', message: 'Loopback requests only.' } })
            return
          }
          if (req.method !== 'GET') {
            methodNotAllowed(res, ['GET'])
            return
          }
          const route = resolveRoute(ctx)
          // D1 (05 §4): one definition, two exposures. The three field names are
          // buildId / chain_version / configRevision — `revision` and
          // `host_build_id` are storage details and must never appear here.
          const build = currentBuild()
          sendJson(res, 200, {
            ...baseBody(route),
            // 分发前提（docs/bundle-DISTRIBUTION.md §4.2）：数据目录必须能被读出来，
            // 否则"我的归档去哪了"只能靠猜。
            dataDir: String(integration.dataDir ?? ''),
            build,
            buildId: build.buildId,
            chainVersion: build.chain_version,
            loadedConfigRevision: build.configRevision,
            service: {
              ...integration.stateSync().service,
              state: integration.lastService?.ready ? 'connected' : 'not_checked',
              detail: integration.lastService
                ? 'M1.3b service snapshot is available; see /state for the last round.'
                : 'M1.3b reads data/.api-token and /api/* on manual run; no background polling.',
            },
            routes: {
              health: HEALTH_PATH,
              probe: PROBE_PATH,
              run: RUN_PATH,
              state: STATE_PATH,
              rounds: ROUNDS_PATH,
              round: `${ROUNDS_PATH}/:id`,
            },
            probe: { method: 'POST', timeoutMs: PROBE_TIMEOUT_MS, transport: 'ctx.llm.stream' },
            config: { present: Object.keys(pluginConfig).length > 0 },
          })
        },
      }))

      for (const route of integration.routes()) {
        disposers.push(webRoutes.register(route))
      }

      disposers.push(webRoutes.register({
        kind: 'exact',
        path: PROBE_PATH,
        handler: async (req, res) => {
          if (!isLoopback(req.socket?.remoteAddress)) {
            sendJson(res, 403, { ok: false, error: { code: 'LOOPBACK_ONLY', message: 'Loopback requests only.' } })
            return
          }
          // POST-only: a GET (including a browser prefetch or a link) must
          // never trigger a model call.
          if (req.method !== 'POST') {
            methodNotAllowed(res, ['POST'])
            return
          }
          if (!isTrustedProbeRequest(req, ctx)) {
            sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin POST only.' } })
            return
          }
          req.resume()
          try {
            const result = await runProbe(ctx)
            const snapshot = recordProbe(result)
            result.llm.lastProbe = snapshot
            sendJson(res, 200, result)
          } catch (error) {
            const failure = normalizeFailure(error)
            const snapshot = recordProbe({ ok: false, error: failure })
            const body = {
              ...baseBody(resolveRoute(ctx)),
              ok: false,
              finish: null,
              usage: null,
              error: failure,
            }
            body.llm.lastProbe = snapshot
            sendJson(res, 500, body)
          }
        },
      }))

      disposers.push(webRoutes.register({
        kind: 'exact',
        path: SCHEDULE_PATH,
        handler: (req, res) => {
          if (!isLoopback(req.socket?.remoteAddress)) {
            sendJson(res, 403, { ok: false, error: { code: 'LOOPBACK_ONLY', message: 'Loopback requests only.' } })
            return
          }
          if (req.method === 'GET') {
            sendJson(res, 200, { ok: true, schedule: scheduler.status() })
            return
          }
          if (req.method !== 'POST') {
            methodNotAllowed(res, ['GET', 'POST'])
            return
          }
          if (!isTrustedProbeRequest(req, ctx)) {
            sendJson(res, 403, { ok: false, error: { code: 'CROSS_ORIGIN_BLOCKED', message: 'Same-origin POST only.' } })
            return
          }
          let action = 'status'
          try {
            const url = new URL(req.url ?? SCHEDULE_PATH, 'http://127.0.0.1')
            action = String(url.searchParams.get('action') ?? '').trim().toLowerCase()
            if (!action) action = url.searchParams.get('enabled') === '0' ? 'disable' : 'enable'
            if (action === 'enable' || action === 'on') {
              scheduler.setEnabled(true)
            } else if (action === 'disable' || action === 'off') {
              scheduler.setEnabled(false)
            } else if (action === 'pause') {
              const ms = Number.parseInt(url.searchParams.get('ms') ?? '', 10)
              scheduler.pause(Number.isFinite(ms) && ms > 0 ? ms : undefined, 'operator_pause')
            } else if (action === 'resume') {
              scheduler.resume()
            } else {
              sendJson(res, 422, {
                ok: false,
                error: { code: 'E_VALIDATION', message: 'action must be enable|disable|pause|resume' },
              })
              return
            }
          } catch (error) {
            sendJson(res, 500, {
              ok: false,
              error: { code: 'SCHEDULE_FAILED', message: String(error?.message ?? error) },
            })
            return
          }
          sendJson(res, 200, { ok: true, schedule: scheduler.status() })
        },
      }))

      scheduler.start()
      const scheduleState = scheduler.status()
      ctx.logger?.info?.(
        `[twb] auto-schedule ${scheduleState.enabled ? 'on' : 'off'} interval=${scheduleState.interval_ms}ms symbol=${scheduleState.symbol} next=${scheduleState.next_at ?? 'n/a'}`,
      )

      ctx.logger?.info?.(`[twb] ${PLUGIN_ID} routes: ${HEALTH_PATH}, ${PROBE_PATH}, ${RUN_PATH}, ${STATE_PATH}, ${ROUNDS_PATH}, ${SCHEDULE_PATH}`)
      return () => {
        scheduler.dispose()
        for (const dispose of disposers) {
          try { dispose() } catch { /* cleanup best effort */ }
        }
      }
    } catch (error) {
      for (const dispose of disposers) {
        try { dispose() } catch { /* cleanup best effort */ }
      }
      throw error
    }
  })
}
