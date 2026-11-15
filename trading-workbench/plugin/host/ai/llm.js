/**
 * The only LLM transport used by the AI chain: ctx.llm.stream().
 *
 * There is no fetch/axios, no provider SDK, no tools, no subagent and no key
 * handling here. Tests inject a fake `llm` object with the same stream shape;
 * production passes ctx.llm from the Host.
 */

import { createHash } from 'node:crypto'
import { ChainError } from './errors.js'

function sha256(text) {
  return `sha256:${createHash('sha256').update(text).digest('hex')}`
}

export function resolveHarnessRoute(ctx) {
  const selection = ctx?.agentDefaultModel?.currentSelection?.()
  if (selection?.provider && selection?.model) {
    return { provider: selection.provider, model: selection.model, source: 'agentDefaultModel' }
  }
  return null
}

export function normalizeUsage(chunk) {
  const raw = chunk?.usage ?? chunk
  if (raw === null || typeof raw !== 'object') return null
  const inputTokens = raw.inputTokens ?? raw.input_tokens ?? raw.promptTokens ?? raw.prompt_tokens ?? null
  const outputTokens = raw.outputTokens ?? raw.output_tokens ?? raw.completionTokens ?? raw.completion_tokens ?? null
  const totalTokens = raw.totalTokens ?? raw.total_tokens
    ?? (typeof inputTokens === 'number' && typeof outputTokens === 'number' ? inputTokens + outputTokens : null)
  const cacheReadTokens = raw.cacheReadTokens ?? raw.cache_read_tokens ?? null
  const cacheWriteTokens = raw.cacheWriteTokens ?? raw.cache_write_tokens ?? null
  return { inputTokens, outputTokens, totalTokens, cacheReadTokens, cacheWriteTokens }
}

function normalizeFinish(chunk) {
  const reason = chunk?.reason ?? chunk
  if (reason === null || typeof reason !== 'object') return null
  const kind = reason.kind ?? 'unknown'
  return { kind, failure: reason.failure ?? null }
}

function timeoutError(timeoutMs) {
  return new ChainError('LLM_TIMEOUT', `LLM call exceeded ${timeoutMs} ms`, { timeoutMs })
}

/**
 * 05 N10 (G3): a route that rejects the requested *parameter* (reasoning
 * effort / temperature / max tokens shape) will reject it again on retry. It
 * must be classified and terminated after a single attempt instead of doubling
 * the failure with a repair prompt that cannot help.
 *
 * The Harness throws provider-shaped errors, so both an explicit code and the
 * human message are inspected. Keep this list narrow: a false positive turns a
 * transient failure into a hard failure.
 */
const UNSUPPORTED_PARAM_CODES = [
  'UNSUPPORTED_REASONING_EFFORT',
  'E_UNSUPPORTED_REASONING_EFFORT',
  'INVALID_REASONING_EFFORT',
  'UNSUPPORTED_EFFORT',
  'UNSUPPORTED_PARAMETER',
  'UNSUPPORTED_VALUE',
]
const UNSUPPORTED_PARAM_PATTERNS = [
  /unsupported reasoning effort/i,
  /reasoning effort[^.]{0,40}not supported/i,
  /invalid reasoning effort/i,
  /unsupported (?:parameter|value)/i,
]

export function isUnsupportedParamError(error) {
  if (!error) return false
  const code = String(error.code ?? error.name ?? '')
  if (UNSUPPORTED_PARAM_CODES.includes(code)) return true
  const message = String(error.message ?? '')
  return UNSUPPORTED_PARAM_PATTERNS.some((pattern) => pattern.test(message))
}

/** Wrap a provider parameter rejection into a chain error (never retried). */
export function unsupportedParamError(error, { role } = {}) {
  const sourceCode = String(error?.code ?? error?.name ?? 'UNSUPPORTED_REASONING_EFFORT')
  const message = String(error?.message ?? error)
  return new ChainError(
    'UNSUPPORTED_REASONING_EFFORT',
    `the model route rejected the requested reasoning effort/parameters: ${message}`,
    { role, sourceCode, cause: error },
  )
}

/**
 * Consume an async iterable with a whole-call timeout. The AbortController is
 * aborted on timeout; the caller owns it and may combine it with a chain-level
 * signal in the future.
 */
export async function consumeStream(stream, { controller, timeoutMs, role }) {
  if (!stream || typeof stream[Symbol.asyncIterator] !== 'function') {
    throw new ChainError('LLM_TRANSPORT', 'ctx.llm.stream did not return an async iterable', { role })
  }
  const iterator = stream[Symbol.asyncIterator]()
  const started = Date.now()
  let text = ''
  let usage = null
  let finish = null

  try {
    while (true) {
      const remaining = timeoutMs - (Date.now() - started)
      if (remaining <= 0) {
        controller.abort()
        throw timeoutError(timeoutMs)
      }
      let timer
      const next = await Promise.race([
        iterator.next(),
        new Promise((_, reject) => {
          timer = setTimeout(() => {
            controller.abort()
            reject(timeoutError(timeoutMs))
          }, remaining)
        }),
      ]).finally(() => clearTimeout(timer))
      if (next.done) break
      const chunk = next.value
      if (chunk?.type === 'text-delta') {
        const delta = chunk.text ?? chunk.delta ?? ''
        if (typeof delta === 'string') text += delta
      } else if (chunk?.type === 'usage') {
        usage = normalizeUsage(chunk)
      } else if (chunk?.type === 'finish') {
        finish = normalizeFinish(chunk)
      }
    }
  } catch (error) {
    if (error instanceof ChainError) throw error
    if (isUnsupportedParamError(error)) throw unsupportedParamError(error, { role })
    if (controller.signal.aborted) throw timeoutError(timeoutMs)
    throw new ChainError('LLM_TRANSPORT', error?.message ?? String(error), { role, cause: error })
  } finally {
    try { await iterator.return?.() } catch { /* best effort */ }
  }

  if (finish === null) {
    throw new ChainError('LLM_INCOMPLETE', 'LLM stream ended without a finish chunk', {
      role,
      finishKind: null,
      finishFailure: null,
      usage,
    })
  }
  if (finish.kind === 'error' || finish.kind === 'aborted') {
    throw new ChainError('LLM_FINISH_ERROR', `LLM stream finished with ${finish.kind}`, {
      role,
      finishKind: finish.kind,
      finishFailure: finish.failure ?? null,
      usage,
    })
  }
  if (finish.kind !== 'stop') {
    throw new ChainError('LLM_FINISH_ERROR', `LLM stream finished with ${finish.kind}`, {
      role,
      finishKind: finish.kind === 'max-tokens' ? 'max_tokens' : finish.kind,
      finishFailure: finish.failure ?? null,
      usage,
    })
  }

  return {
    text,
    usage,
    finish,
    latencyMs: Date.now() - started,
    requestSha256: null,
  }
}

/**
 * Make one stream call. `prompt` is the object returned by buildPrompt().
 * Callers must pass exactly the options supported by Harness; no tools key.
 */
export async function streamPrompt({ llm, route, prompt, timeoutMs, role, userSuffix = '', sessionId = null }) {
  if (!llm || typeof llm.stream !== 'function') {
    throw new ChainError('NO_LLM', 'No Harness LLM seam was provided', { role })
  }
  if (!route?.provider || !route?.model) {
    throw new ChainError('NO_ADAPTER', 'No default Harness provider/model route', { role })
  }
  const controller = new AbortController()
  const userText = userSuffix ? `${prompt.user}\n\n${userSuffix}` : prompt.user
  const requestText = `${prompt.system}\n---USER---\n${userText}`
  const options = {
    provider: route.provider,
    model: route.model,
    system: prompt.system,
    messages: [{ role: 'user', content: [{ type: 'text', text: userText }] }],
    maxTokens: prompt.maxTokens,
    temperature: prompt.temperature,
    signal: controller.signal,
  }
  // 02 §3.1 rule 5: a model without reasoning gets no effort key at all (null
  // would read as "requested null" to some adapters).
  if (typeof prompt.effort === 'string' && prompt.effort) options.reasoningEffort = prompt.effort
  // `sessionId` is an opaque transport request id (some Harness routes need
  // one for routing); it is not a Session object and no Session is created.
  if (sessionId) options.sessionId = sessionId
  // Deliberately no tools/subagent/key: M1.3a has no tool loop.
  try {
    const stream = llm.stream(options)
    const result = await consumeStream(stream, { controller, timeoutMs, role })
    return { ...result, requestSha256: sha256(requestText), options }
  } catch (error) {
    if (error instanceof ChainError) throw error
    // A synchronous provider rejection surfaces here before any chunk arrives.
    if (isUnsupportedParamError(error)) throw unsupportedParamError(error, { role })
    throw new ChainError('LLM_TRANSPORT', error?.message ?? String(error), { role, cause: error })
  }
}

export function parseJsonText(text) {
  if (typeof text !== 'string' || text.trim().length === 0) {
    throw new ChainError('JSON_EMPTY', 'model returned empty text')
  }
  let cleaned = text.trim()
  const fence = cleaned.match(/^```(?:json)?\s*([\s\S]*?)```$/i)
  if (fence) cleaned = fence[1].trim()
  try {
    return JSON.parse(cleaned)
  } catch (error) {
    throw new ChainError('JSON_PARSE', `model output is not valid JSON: ${error.message}`, { text: cleaned.slice(0, 500) })
  }
}

/** Codes that must fail the call on the first attempt (05 N10 / 02 V5③). */
export const NON_RETRYABLE_LLM_CODES = Object.freeze([
  'UNSUPPORTED_REASONING_EFFORT',
  'NO_ADAPTER',
  'NO_LLM',
  'CHAIN_DEADLINE',
  'BUDGET_EXHAUSTED',
])

export function isRetryableLlmError(error) {
  if (!(error instanceof ChainError)) return true
  if (NON_RETRYABLE_LLM_CODES.includes(error.code)) return false
  return ['LLM_TIMEOUT', 'LLM_TRANSPORT', 'LLM_INCOMPLETE', 'LLM_FINISH_ERROR', 'JSON_PARSE', 'JSON_EMPTY'].includes(error.code)
}
