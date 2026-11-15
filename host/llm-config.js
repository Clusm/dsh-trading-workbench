/**
 * Per-role model / reasoning-effort configuration (02 §2–§3, 05 P1-N03/N04).
 *
 * Owner split: this module owns the *protocol* (schema, presets, persistence,
 * resolution, freeze snapshot) and the configRevision semantics. 01 owns the DTO
 * field names (/health buildId/chain_version, RoundView.build) and 03 only shows
 * the values.
 *
 * Hard rules implemented here (user decisions 2026-11-12):
 *  - U-08=B  : roles may choose model + effort (three presets + per-role override)
 *  - P-01=A  : red/blue must stay symmetric unless debateAsymmetric=true
 *  - P-03=A  : config is plugin-local data/llm-config.json, never the Harness profile
 *  - P-05=A  : an unverified/unsupported effort never reaches ctx.llm.stream;
 *              freeze returns ROUTE_UNAVAILABLE (a legal termination) instead
 *  - D-6=B   : first launch fixates the "balanced" preset (revision=1); if the
 *              catalog is unavailable or a preset model is missing, nothing is
 *              written and the UI shows a banner (never a silent replacement)
 *
 * No Service endpoint, no Harness config write, no credential read.
 */

import { createHash } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

import {
  CHAIN_DEADLINE_MS,
  ROLE_EFFORT,
  ROLE_TIMEOUT_MS,
} from './ai/constants.js'

export const LLM_CONFIG_SCHEMA = 'twb-llm-config/1'
export const LLM_CONFIG_FILE = 'llm-config.json'
export const CATALOG_TTL_MS = 60_000

/** The 10 role keys the chain and the daily review use (constants.js ROLE_AUDIT). */
export const ROLE_KEYS = Object.freeze([
  'analysis',
  'red_opening', 'blue_opening', 'red_response', 'blue_response',
  'decision', 'risk',
  'execution_entry', 'execution_manage',
  'review',
])

export const DEBATE_ROLES = Object.freeze(['red_opening', 'blue_opening', 'red_response', 'blue_response'])

/**
 * 02 §2.4 presets. They are declared by *model id only*: the provider is
 * resolved from the live catalog (qoder vs qoder-global) so a preset never
 * hard-codes a provider that is not enabled on this machine.
 */
export const PRESETS = Object.freeze({
  balanced: {
    label: '平衡',
    default: { model: 'dfmodel', effort: 'high' },
    roles: {
      analysis: { model: 'dfmodel', effort: 'high' },
      red_opening: { model: 'dfmodel', effort: 'high' },
      blue_opening: { model: 'dfmodel', effort: 'high' },
      decision: { model: 'dfmodel', effort: 'max' },
      risk: { model: 'dfmodel', effort: 'max' },
      execution_entry: { model: 'dfmodel', effort: 'low' },
      execution_manage: { model: 'dfmodel', effort: 'low' },
      review: { model: 'dfmodel', effort: 'high' },
    },
  },
  saver: {
    label: '省钱',
    default: { model: 'dfmodel', effort: 'low' },
    roles: {
      analysis: { model: 'dfmodel', effort: 'low' },
      red_opening: { model: 'dfmodel', effort: 'low' },
      blue_opening: { model: 'dfmodel', effort: 'low' },
      decision: { model: 'dfmodel', effort: 'high' },
      risk: { model: 'dfmodel', effort: 'high' },
      execution_entry: { model: 'dfmodel', effort: 'low' },
      execution_manage: { model: 'dfmodel', effort: 'low' },
      review: { model: 'dfmodel', effort: 'low' },
    },
  },
  strongest: {
    label: '最强',
    default: { model: 'qmodel_latest', effort: 'high' },
    roles: {
      analysis: { model: 'qmodel_latest', effort: 'high' },
      red_opening: { model: 'qmodel_latest', effort: 'high' },
      blue_opening: { model: 'qmodel_latest', effort: 'high' },
      decision: { model: 'qmodel_latest', effort: 'xhigh' },
      risk: { model: 'qmodel_latest', effort: 'xhigh' },
      execution_entry: { model: 'dfmodel', effort: 'low' },
      execution_manage: { model: 'dfmodel', effort: 'low' },
      review: { model: 'qmodel_latest', effort: 'high' },
    },
  },
})

export const PRESET_NAMES = Object.freeze(Object.keys(PRESETS))

/** 02 §2.7 G4: serial wall clock, not Σ(roleTimeout × attempts). */
export function budgetPreview() {
  const serial = ROLE_TIMEOUT_MS.analysis
    + Math.max(ROLE_TIMEOUT_MS.red_opening, ROLE_TIMEOUT_MS.blue_opening)
    + Math.max(ROLE_TIMEOUT_MS.red_response, ROLE_TIMEOUT_MS.blue_response)
    + ROLE_TIMEOUT_MS.decision
    + ROLE_TIMEOUT_MS.risk
    + ROLE_TIMEOUT_MS.execution_entry
  return {
    serial_ms: serial,
    deadline_ms: CHAIN_DEADLINE_MS,
    min_ms: serial,
    max_ms: CHAIN_DEADLINE_MS,
    retry_margin_ms: Math.max(0, CHAIN_DEADLINE_MS - serial),
    per_role_ms: Object.fromEntries(ROLE_KEYS.map((role) => [role, ROLE_TIMEOUT_MS[role] ?? null])),
  }
}

// ---- catalog adapter (ctx.llm.listProviders -> listModels -> resolveModelInfo) ----

/**
 * Wraps the three Harness catalog APIs with a 60s cache and per-provider failure
 * isolation (02 §2.3). Unavailable catalog is an explicit state, never an
 * exception: callers must show a banner and must not write any config.
 */
export function createLlmCatalog({ llm = null, ttlMs = CATALOG_TTL_MS, now = Date.now } = {}) {
  const usable = Boolean(llm)
    && typeof llm.listProviders === 'function'
    && typeof llm.listModels === 'function'
    && typeof llm.resolveModelInfo === 'function'
  let cache = null
  const infoCache = new Map()

  async function load() {
    if (!usable) return { available: false, fetchedAt: null, groups: [], failures: [], reason: 'CATALOG_UNAVAILABLE' }
    const fetchedAt = new Date(now()).toISOString()
    const groups = []
    const failures = []
    let providers = []
    try {
      providers = llm.listProviders() ?? []
    } catch (error) {
      return { available: false, fetchedAt, groups: [], failures: [{ id: '*', message: String(error?.message ?? error) }], reason: 'LIST_PROVIDERS_FAILED' }
    }
    for (const provider of providers) {
      const id = provider?.id
      if (!id) continue
      try {
        const listedRaw = await llm.listModels(id)
        const listed = Array.isArray(listedRaw) ? listedRaw : []
        const models = []
        for (const model of listed) {
          // listModels carries basic metadata; reasoning/efforts/defaultEffort
          // only come from resolveModelInfo (02 §1.2), so the catalog enriches
          // each entry (failures stay basic rather than breaking the provider).
          let info = null
          try { info = await llm.resolveModelInfo(id, model.id) } catch { info = null }
          models.push({
            id: model.id,
            name: model.name ?? model.id,
            inputModalities: model.inputModalities ?? info?.inputModalities ?? null,
            badge: model.badge ?? null,
            efforts: Array.isArray(info?.reasoning?.efforts)
              ? info.reasoning.efforts.map((effort) => (typeof effort === 'string' ? effort : effort?.id)).filter(Boolean)
              : (Array.isArray(model.reasoning?.efforts)
                ? model.reasoning.efforts.map((effort) => (typeof effort === 'string' ? effort : effort?.id)).filter(Boolean)
                : null),
            defaultEffort: info?.reasoning?.defaultEffort ?? model.reasoning?.defaultEffort ?? null,
            contextWindow: info?.context?.contextWindow ?? model.context?.contextWindow ?? null,
            defaultMaxTokens: info?.defaultMaxTokens ?? model.defaultMaxTokens ?? null,
          })
        }
        groups.push({ id, name: provider?.name ?? id, models })
      } catch (error) {
        failures.push({ id, name: provider?.name ?? id, message: String(error?.message ?? error) })
      }
    }
    return { available: groups.length > 0, fetchedAt, groups, failures, reason: groups.length > 0 ? null : 'NO_MODELS' }
  }

  async function snapshot(force = false) {
    if (!force && cache && now() - Date.parse(cache.fetchedAt ?? 0) < ttlMs) return cache
    cache = await load()
    return cache
  }

  /** model metadata with full reasoning info; null when the model is not listed. */
  async function resolve(provider, model) {
    const key = `${provider}/${model}`
    if (infoCache.has(key)) return infoCache.get(key)
    if (!usable) return null
    try {
      const info = await llm.resolveModelInfo(provider, model)
      if (!info || info.id !== model || info.provider !== provider) { infoCache.set(key, null); return null }
      const normalized = {
        provider,
        id: model,
        name: info.name ?? model,
        efforts: Array.isArray(info.reasoning?.efforts)
          ? info.reasoning.efforts.map((effort) => (typeof effort === 'string' ? effort : effort?.id)).filter(Boolean)
          : [],
        defaultEffort: info.reasoning?.defaultEffort ?? null,
        contextWindow: info.context?.contextWindow ?? null,
        defaultMaxTokens: info.defaultMaxTokens ?? null,
        inputModalities: info.inputModalities ?? null,
      }
      infoCache.set(key, normalized)
      return normalized
    } catch {
      infoCache.set(key, null)
      return null
    }
  }

  return {
    usable,
    snapshot,
    resolve,
    invalidate() { cache = null; infoCache.clear() },
    /** find a provider that lists the model (preset materialisation). */
    async providerForModel(model) {
      const snap = await snapshot()
      for (const group of snap.groups) {
        if (group.models.some((entry) => entry.id === model)) return group.id
      }
      return null
    },
  }
}

// ---- config file (plugin-local only, P-03=A) ----

export function configPath(dataDir) {
  return path.join(dataDir, LLM_CONFIG_FILE)
}

export function readConfig(dataDir) {
  const file = configPath(dataDir)
  try {
    const raw = fs.readFileSync(file, 'utf8')
    const parsed = JSON.parse(raw)
    if (!parsed || parsed.schema !== LLM_CONFIG_SCHEMA) {
      return { config: null, error: { code: 'E_CONFIG_SCHEMA', message: `${file} is not ${LLM_CONFIG_SCHEMA}` } }
    }
    return { config: parsed, error: null }
  } catch (error) {
    if (error?.code === 'ENOENT') return { config: null, error: null }
    return { config: null, error: { code: 'E_CONFIG_UNREADABLE', message: String(error?.message ?? error) } }
  }
}

function writeConfigFile(dataDir, config) {
  const file = configPath(dataDir)
  fs.mkdirSync(path.dirname(file), { recursive: true })
  const tmp = `${file}.${process.pid}.tmp`
  fs.writeFileSync(tmp, `${JSON.stringify(config, null, 2)}\n`, 'utf8')
  fs.renameSync(tmp, file)
  return file
}

// ---- presets -> config ----

/**
 * Materialise a preset against the live catalog. Returns either a complete
 * config body or { missing:[modelIds], reason } — callers must NOT write a
 * partial config or substitute the current global default (02 §2.4 R2-4).
 */
export async function materializePreset(name, { catalog }) {
  const preset = PRESETS[name]
  if (!preset) return { ok: false, code: 'E_PRESET_UNKNOWN', missing: [], reason: `unknown preset ${name}` }
  const models = new Set([preset.default.model, ...Object.values(preset.roles).map((entry) => entry.model)])
  const resolved = new Map()
  const missing = []
  for (const model of models) {
    const provider = await catalog.providerForModel(model)
    if (!provider) { missing.push(model); continue }
    resolved.set(model, provider)
  }
  if (missing.length > 0) {
    return {
      ok: false,
      code: 'E_PRESET_MODEL_MISSING',
      missing,
      reason: `preset model(s) not in the catalog: ${missing.join(', ')}`,
    }
  }
  const defaultProvider = resolved.get(preset.default.model)
  const roleRoutes = {}
  for (const [role, entry] of Object.entries(preset.roles)) {
    roleRoutes[role] = { provider: resolved.get(entry.model), model: entry.model, effort: entry.effort }
  }
  return {
    ok: true,
    config: {
      schema: LLM_CONFIG_SCHEMA,
      revision: 1,
      updatedAt: new Date().toISOString(),
      defaultRoute: { provider: defaultProvider, model: preset.default.model, effort: preset.default.effort, source: 'explicit' },
      roleRoutes,
      debateAsymmetric: false,
      fallback: { mode: 'fail' },
      preset: name,
    },
  }
}

// ---- validation + resolution (G1 save-time / G2 freeze-time share this) ----

const TOP_KEYS = new Set(['schema', 'revision', 'updatedAt', 'defaultRoute', 'roleRoutes', 'debateAsymmetric', 'fallback', 'preset'])
const ROUTE_KEYS = new Set(['provider', 'model', 'effort'])
const FORBIDDEN_KEYS = ['temperature', 'risk', 'threshold', 'score_override', 'size_multiplier', 'leverage']

function asString(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

/**
 * Resolve the 10 role routes and validate every effort against the model's
 * declared reasoning.efforts. Used by save AND by the round freeze (one
 * implementation, no bypass for the first-install path).
 */
export async function resolveConfigRoutes(config, { catalog }) {
  const errors = []
  const defaultRoute = config?.defaultRoute ?? {}
  const provider0 = asString(defaultRoute.provider)
  const model0 = asString(defaultRoute.model)
  const effort0 = defaultRoute.effort === undefined ? null : defaultRoute.effort
  if (!provider0 || !model0) {
    errors.push({ role: 'default', code: 'E_ROUTE_INVALID', message: 'defaultRoute.provider/model are required' })
  }
  const roleRoutes = config?.roleRoutes && typeof config.roleRoutes === 'object' ? config.roleRoutes : {}
  const plans = {}
  for (const role of ROLE_KEYS) {
    const own = roleRoutes[role] && typeof roleRoutes[role] === 'object' ? roleRoutes[role] : null
    let provider
    let model
    let requested
    let effortSource
    if ((role === 'red_response' || role === 'blue_response') && own === null) {
      const opening = role === 'red_response' ? 'red_opening' : 'blue_opening'
      const open = roleRoutes[opening] && typeof roleRoutes[opening] === 'object' ? roleRoutes[opening] : {}
      provider = asString(open.provider) ?? provider0
      model = asString(open.model) ?? model0
      requested = open.effort === undefined ? effort0 : open.effort
      effortSource = 'roleRoutes'
    } else {
      const route = own ?? {}
      provider = asString(route.provider) ?? provider0
      model = asString(route.model) ?? model0
      requested = route.effort === undefined ? effort0 : route.effort
      effortSource = route.effort !== undefined ? 'roleRoutes' : 'requested'
    }
    plans[role] = { provider, model, requestedEffort: requested ?? null, effortSource }
  }

  const models = new Map()
  const modelKeys = [...new Set(ROLE_KEYS.map((role) => `${plans[role].provider}/${plans[role].model}`))]
  for (const key of modelKeys) {
    const [provider, model] = key.split('/')
    const info = await catalog.resolve(provider, model)
    if (!info) {
      errors.push({ role: '*', code: 'E_MODEL_NOT_IN_CATALOG', provider, model, message: `${provider}/${model} is not in the live catalog` })
      models.set(key, null)
      continue
    }
    models.set(key, info)
  }

  const roles = {}
  for (const role of ROLE_KEYS) {
    const plan = plans[role]
    const info = models.get(`${plan.provider}/${plan.model}`)
    if (!info) {
      roles[role] = { ...plan, effectiveEffort: null, effortSource: 'unsupported', availableEfforts: [] }
      continue
    }
    const efforts = info.efforts ?? []
    const requested = plan.requestedEffort
    if (efforts.length === 0) {
      if (requested !== null && requested !== undefined) {
        errors.push({
          role,
          code: 'E_MODEL_HAS_NO_EFFORT',
          provider: plan.provider,
          model: plan.model,
          requestedEffort: requested,
          message: `${plan.provider}/${plan.model} exposes no reasoning efforts; role ${role} requested ${requested}`,
        })
        roles[role] = { ...plan, effectiveEffort: null, effortSource: 'unsupported', availableEfforts: [] }
      } else {
        roles[role] = { ...plan, effectiveEffort: null, effortSource: 'unsupported', availableEfforts: [] }
      }
      continue
    }
    if (requested === null || requested === undefined) {
      if (info.defaultEffort) {
        roles[role] = { ...plan, effectiveEffort: info.defaultEffort, effortSource: 'defaultEffort', availableEfforts: efforts }
      } else {
        errors.push({
          role,
          code: 'E_EFFORT_REQUIRED',
          provider: plan.provider,
          model: plan.model,
          availableEfforts: efforts,
          message: `${plan.provider}/${plan.model} has no default effort; role ${role} must choose one of ${efforts.join('/')}`,
        })
        roles[role] = { ...plan, effectiveEffort: null, effortSource: 'unsupported', availableEfforts: efforts }
      }
      continue
    }
    if (!efforts.includes(requested)) {
      // P-05=A (user decision): never map silently, never send an unverified
      // effort. The freeze reports ROUTE_UNAVAILABLE; save rejects the config.
      errors.push({
        role,
        code: 'UNSUPPORTED_REASONING_EFFORT',
        provider: plan.provider,
        model: plan.model,
        requestedEffort: requested,
        availableEfforts: efforts,
        message: `${plan.provider}/${plan.model} does not support effort ${requested}; available: ${efforts.join('/') || 'none'}`,
      })
      roles[role] = { ...plan, effectiveEffort: null, effortSource: 'unsupported', availableEfforts: efforts }
      continue
    }
    roles[role] = {
      ...plan,
      effectiveEffort: requested,
      effortSource: plan.effortSource === 'requested' ? 'requested' : plan.effortSource,
      availableEfforts: efforts,
    }
  }

  // P-01=A: symmetric debate unless explicitly allowed.
  if (config?.debateAsymmetric !== true) {
    const sig = (role) => `${roles[role].provider}/${roles[role].model}/${roles[role].requestedEffort}`
    const first = sig('red_opening')
    for (const role of DEBATE_ROLES) {
      if (sig(role) !== first) {
        errors.push({
          role,
          code: 'E_DEBATE_ASYMMETRIC',
          message: 'debateAsymmetric=false requires red and blue to share provider/model/effort',
        })
        break
      }
    }
  }

  const fingerprintInput = modelKeys
    .map((key) => {
      const info = models.get(key)
      return info
        ? `${key}|${info.name}|${(info.efforts ?? []).join(',')}|${info.defaultEffort ?? ''}|${info.contextWindow ?? ''}`
        : `${key}|missing`
    })
    .sort()
    .join('\n')

  return {
    ok: errors.length === 0,
    errors,
    roles,
    catalogFingerprint: `sha256:${createHash('sha256').update(fingerprintInput).digest('hex')}`,
  }
}

/** Fields a saved config may contain (schema whitelist, 02 §3.1). */
export function validateConfigShape(candidate) {
  const errors = []
  if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) {
    return [{ code: 'E_CONFIG_SHAPE', message: 'config must be an object' }]
  }
  for (const key of Object.keys(candidate)) {
    if (!TOP_KEYS.has(key)) errors.push({ code: 'E_CONFIG_UNKNOWN_FIELD', message: `unknown field ${key}` })
    if (FORBIDDEN_KEYS.includes(key)) errors.push({ code: 'E_CONFIG_FORBIDDEN_FIELD', message: `${key} is not a model-config field` })
  }
  const route = candidate.defaultRoute
  if (!route || typeof route !== 'object' || !asString(route.provider) || !asString(route.model)) {
    errors.push({ code: 'E_ROUTE_INVALID', role: 'default', message: 'defaultRoute.provider/model are required' })
  } else {
    for (const key of Object.keys(route)) if (!ROUTE_KEYS.has(key) && key !== 'source') errors.push({ code: 'E_ROUTE_UNKNOWN_FIELD', message: `unknown defaultRoute field ${key}` })
    if (route.source !== undefined && !['explicit', 'agentDefaultModel'].includes(route.source)) errors.push({ code: 'E_ROUTE_SOURCE_INVALID', message: 'defaultRoute.source must be explicit|agentDefaultModel' })
  }
  const roleRoutes = candidate.roleRoutes
  if (roleRoutes !== undefined) {
    if (!roleRoutes || typeof roleRoutes !== 'object' || Array.isArray(roleRoutes)) {
      errors.push({ code: 'E_ROLE_ROUTES_INVALID', message: 'roleRoutes must be an object' })
    } else {
      for (const [role, value] of Object.entries(roleRoutes)) {
        if (!ROLE_KEYS.includes(role)) { errors.push({ code: 'E_ROLE_UNKNOWN', message: `unknown role ${role}` }); continue }
        if (!value || typeof value !== 'object' || Array.isArray(value)) { errors.push({ code: 'E_ROLE_ROUTE_INVALID', role, message: 'role route must be an object' }); continue }
        for (const key of Object.keys(value)) {
          if (!ROUTE_KEYS.has(key)) errors.push({ code: 'E_ROLE_ROUTE_UNKNOWN_FIELD', role, message: `unknown field ${key}` })
          if (FORBIDDEN_KEYS.includes(key)) errors.push({ code: 'E_CONFIG_FORBIDDEN_FIELD', role, message: `${key} is not a model-config field` })
        }
      }
    }
  }
  if (candidate.debateAsymmetric !== undefined && typeof candidate.debateAsymmetric !== 'boolean') {
    errors.push({ code: 'E_CONFIG_SHAPE', message: 'debateAsymmetric must be a boolean' })
  }
  const fallback = candidate.fallback
  if (fallback !== undefined) {
    if (!fallback || typeof fallback !== 'object' || !['fail', 'explicit'].includes(fallback.mode)) {
      errors.push({ code: 'E_FALLBACK_INVALID', message: 'fallback.mode must be fail|explicit' })
    } else if (fallback.mode === 'explicit') {
      // P-05=A: mapped fallback is not approved, so explicit fallback is refused.
      errors.push({ code: 'E_FALLBACK_NOT_APPROVED', message: 'explicit fallback requires P-05=B (not approved); only fail is allowed' })
    }
  }
  if (candidate.preset !== undefined && !PRESET_NAMES.includes(candidate.preset) && candidate.preset !== 'custom') {
    errors.push({ code: 'E_PRESET_UNKNOWN', message: 'preset must be balanced|saver|strongest|custom' })
  }
  return errors
}

/** Save-time pipeline: shape -> catalog/effort -> persistence (gated on no errors). */
export async function saveConfig({ dataDir, candidate, catalog, now = Date.now }) {
  const shapeErrors = validateConfigShape(candidate)
  if (shapeErrors.length > 0) return { ok: false, code: 'E_CONFIG_INVALID', errors: shapeErrors }
  const resolved = await resolveConfigRoutes(candidate, { catalog })
  if (!resolved.ok) return { ok: false, code: 'ROUTE_UNAVAILABLE', errors: resolved.errors }
  const previous = readConfig(dataDir).config
  const revision = Number.isInteger(previous?.revision) ? previous.revision + 1 : 1
  const config = {
    schema: LLM_CONFIG_SCHEMA,
    revision,
    updatedAt: new Date(now()).toISOString(),
    defaultRoute: { ...candidate.defaultRoute, source: candidate.defaultRoute.source ?? 'explicit' },
    roleRoutes: candidate.roleRoutes ?? {},
    debateAsymmetric: candidate.debateAsymmetric === true,
    fallback: candidate.fallback ?? { mode: 'fail' },
    preset: candidate.preset ?? 'custom',
  }
  const file = writeConfigFile(dataDir, config)
  return { ok: true, config, resolved, file }
}

/**
 * D-6=B first launch: fixate "balanced" once. Nothing is written when the
 * catalog is unavailable or a preset model is missing — the caller shows a
 * banner and leaves configRevision at null.
 */
export async function ensureFirstRunConfig({ dataDir, catalog, now = Date.now }) {
  const existing = readConfig(dataDir)
  if (existing.config) return { ok: true, created: false, config: existing.config }
  if (existing.error) return { ok: false, created: false, code: existing.error.code, banner: '配置不可读，未固化（可用抽屉重新保存）' }
  const snapshot = await catalog.snapshot()
  if (!snapshot.available) {
    return { ok: false, created: false, code: 'CATALOG_UNAVAILABLE', banner: '目录暂不可用，配置未固化（下一轮冻结时重试）' }
  }
  const materialized = await materializePreset('balanced', { catalog })
  if (!materialized.ok) {
    return { ok: false, created: false, code: materialized.code, missing: materialized.missing, banner: `预设模型不可用（缺 ${materialized.missing.join(', ')}），配置未固化` }
  }
  const resolved = await resolveConfigRoutes(materialized.config, { catalog })
  if (!resolved.ok) {
    return { ok: false, created: false, code: 'ROUTE_UNAVAILABLE', errors: resolved.errors, banner: '平衡预设未通过校验，配置未固化' }
  }
  const config = { ...materialized.config, revision: 1, updatedAt: new Date(now()).toISOString() }
  writeConfigFile(dataDir, config)
  return { ok: true, created: true, config, banner: null }
}

/**
 * Freeze one round (02 §3.2): re-resolve every unique route against the *live*
 * catalog, compute effectiveEffort per role, and build the routeSnapshot. Any
 * unsupported/unverifiable effort -> ROUTE_UNAVAILABLE (P-05=A), no calls.
 */
export async function freezeRound({ config, catalog, now = Date.now }) {
  if (!config) {
    return { ok: true, legacy: true, snapshot: null, roleRoutes: null, effectiveEfforts: null }
  }
  const snapshot = await catalog.snapshot()
  if (!snapshot.available) {
    return {
      ok: false,
      code: 'ROUTE_UNAVAILABLE',
      detail: '模型目录不可用，无法校验本轮路由/档位；按 P-05=A 整轮不交易',
      routeSnapshot: { configRevision: config.revision ?? null, catalogFingerprint: null, unavailable: true },
    }
  }
  const resolved = await resolveConfigRoutes(config, { catalog })
  if (!resolved.ok) {
    const detail = resolved.errors.map((error) => `${error.role ?? '*'}: ${error.message}`).join('; ')
    return {
      ok: false,
      code: 'ROUTE_UNAVAILABLE',
      detail: `路由/档位校验失败（P-05=A，不映射）：${detail}`,
      errors: resolved.errors,
      routeSnapshot: { configRevision: config.revision ?? null, catalogFingerprint: resolved.catalogFingerprint, errors: resolved.errors },
    }
  }
  const roleRoutes = {}
  const effectiveEfforts = {}
  for (const role of ROLE_KEYS) {
    const route = resolved.roles[role]
    roleRoutes[role] = { provider: route.provider, model: route.model }
    effectiveEfforts[role] = route.effectiveEffort
  }
  const defaultRole = resolved.roles.analysis
  const snapshotBody = {
    configRevision: config.revision ?? null,
    resolvedAt: new Date(now()).toISOString(),
    default: {
      provider: config.defaultRoute.provider,
      model: config.defaultRoute.model,
      requestedEffort: config.defaultRoute.effort ?? null,
      effectiveEffort: defaultRole?.effectiveEffort ?? null,
      effortSource: defaultRole?.effortSource ?? 'unsupported',
    },
    roles: Object.fromEntries(ROLE_KEYS.map((role) => [role, {
      provider: resolved.roles[role].provider,
      model: resolved.roles[role].model,
      requestedEffort: resolved.roles[role].requestedEffort,
      effectiveEffort: resolved.roles[role].effectiveEffort,
      effortSource: resolved.roles[role].effortSource,
    }])),
    catalogFingerprint: resolved.catalogFingerprint,
    debateAsymmetric: config.debateAsymmetric === true,
    fallback: config.fallback ?? { mode: 'fail' },
  }
  return { ok: true, snapshot: snapshotBody, roleRoutes, effectiveEfforts, resolved }
}

/** Review role route (daily review is outside the chain but uses the same table). */
export function reviewRouteFromConfig(config) {
  if (!config) return { provider: null, model: null, effort: ROLE_EFFORT.review }
  const raw = config.roleRoutes?.review ?? {}
  return {
    provider: raw.provider ?? config.defaultRoute?.provider ?? null,
    model: raw.model ?? config.defaultRoute?.model ?? null,
    effort: raw.effort ?? config.defaultRoute?.effort ?? ROLE_EFFORT.review,
  }
}
