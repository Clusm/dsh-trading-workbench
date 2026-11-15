#!/usr/bin/env node
/**
 * Runtime delivery verification (F1 / §5.1 / §8·F item 2).
 *
 * Proves, against the processes that are actually running:
 *   1. which absolute data directory + port the Host was started with;
 *   2. that the Host and the Service agree on environment and ledger identity;
 *   3. that the running Host loaded the packaged source release (release digest),
 *      and that the release manifest is still current for the checkout;
 *   4. what the automation state is and where it comes from (service control row).
 *
 *   node trading-workbench/scripts/verify-runtime.mjs \
 *     --host http://127.0.0.1:58707 \
 *     --service http://127.0.0.1:8791 \
 *     --data-dir C:\...\trading-workbench\data
 *
 * Exit codes: 0 every check passed (warnings allowed), 1 a check failed,
 * 2 the arguments or reachable endpoints were insufficient to verify at all.
 * Read-only: it never posts a command and never touches the ledger.
 */

import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { computeRelease, readManifest } from './lib/release.mjs'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PLUGIN_ROOT = path.resolve(HERE, '..')
const MANIFEST_PATH = path.join(HERE, '.release', 'plugin-release.json')
const DEFAULT_HOST = process.env.WORKBENCH_HOST_BASE_URL || 'http://127.0.0.1:58707'
const DEFAULT_SERVICE = process.env.WORKBENCH_SERVICE_URL || 'http://127.0.0.1:8791'
const HOST_API = '/trading-workbench/api'

function usage(message) {
  if (message) console.error(message)
  console.error('usage: node scripts/verify-runtime.mjs [--host URL] [--service URL] [--data-dir DIR]')
  console.error('                                        [--token-file FILE] [--json] [--timeout MS]')
  process.exit(2)
}

function parseArgs(argv) {
  const out = { host: DEFAULT_HOST, service: DEFAULT_SERVICE, dataDir: null, tokenFile: null, json: false, timeoutMs: 15000 }
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i]
    const value = () => {
      const next = argv[i + 1]
      if (!next || next.startsWith('--')) usage('missing value for ' + arg)
      i += 1
      return next
    }
    if (arg === '--host') out.host = value()
    else if (arg === '--service') out.service = value()
    else if (arg === '--data-dir') out.dataDir = value()
    else if (arg === '--token-file') out.tokenFile = value()
    else if (arg === '--json') out.json = true
    else if (arg === '--timeout') out.timeoutMs = Number(value())
    else if (arg === '--help' || arg === '-h') usage()
    else usage('unknown argument: ' + arg)
  }
  return out
}

function stripSlash(url) {
  return String(url).replace(/\/+$/, '')
}

async function getJson(url, { token = null, timeoutMs = 15000 } = {}) {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  try {
    const headers = { accept: 'application/json', 'sec-fetch-site': 'same-origin' }
    if (token) headers.authorization = 'Bearer ' + token
    const response = await fetch(url, { headers, signal: controller.signal })
    const text = await response.text()
    let body = null
    try { body = text ? JSON.parse(text) : null } catch (error) { body = { raw: text.slice(0, 400) } }
    return { ok: response.ok, status: response.status, body }
  } catch (error) {
    return { ok: false, status: 0, body: null, error: String(error?.code ?? error?.message ?? error) }
  } finally {
    clearTimeout(timer)
  }
}

function loadToken({ tokenFile, dataDir }) {
  const candidates = []
  if (tokenFile) candidates.push(path.resolve(tokenFile))
  if (dataDir) candidates.push(path.join(path.resolve(dataDir), '.api-token'))
  candidates.push(path.join(path.resolve(HERE, '..', 'data'), '.api-token'))
  for (const file of candidates) {
    try {
      const raw = readFileSync(file, 'utf8').trim()
      if (raw) return { token: raw, source: 'file', path: file }
    } catch (error) { /* try the next candidate */ }
  }
  const env = String(process.env.WORKBENCH_API_TOKEN ?? '').trim()
  if (env) return { token: env, source: 'env', path: null }
  return { token: null, source: 'none', path: candidates[0] ?? null }
}

function samePath(a, b) {
  if (!a || !b) return false
  const norm = (value) => path.resolve(String(value)).replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase()
  return norm(a) === norm(b)
}

const checks = []
function check(id, label, status, detail) {
  checks.push({ id, label, status, detail: detail ?? null })
}

async function main() {
  const options = parseArgs(process.argv.slice(2))
  const hostBase = stripSlash(options.host)
  const serviceBase = stripSlash(options.service)
  const token = loadToken({ tokenFile: options.tokenFile, dataDir: options.dataDir })

  const [hostInfoR, hostStateR, serviceHealthR, serviceReadyR] = await Promise.all([
    getJson(hostBase + HOST_API + '/host', { timeoutMs: options.timeoutMs }),
    getJson(hostBase + HOST_API + '/state', { timeoutMs: options.timeoutMs }),
    getJson(serviceBase + '/health', { timeoutMs: options.timeoutMs }),
    getJson(serviceBase + '/api/ready', { token: token.token, timeoutMs: options.timeoutMs }),
  ])

  const hostBuildable = hostInfoR.ok && hostInfoR.body?.ok === true
  const serviceBuildable = serviceHealthR.ok && serviceHealthR.body?.service
  if (!hostBuildable && !serviceBuildable) {
    console.error('cannot verify: neither the Host (' + hostBase + HOST_API + '/host) nor the service (' + serviceBase + '/health) answered')
    console.error('  host: ' + hostBase + ' -> HTTP ' + hostInfoR.status + (hostInfoR.error ? ' ' + hostInfoR.error : ''))
    console.error('  service: ' + serviceBase + ' -> HTTP ' + serviceHealthR.status + (serviceHealthR.error ? ' ' + serviceHealthR.error : ''))
    process.exit(2)
  }

  const hostInfo = hostBuildable ? hostInfoR.body : null
  const build = hostInfo?.build ?? null
  const serviceHealth = serviceBuildable ? serviceHealthR.body : null
  const serviceReady = serviceReadyR.ok && serviceReadyR.body && serviceReadyR.body.ok !== false ? serviceReadyR.body : null
  const hostState = hostStateR.ok && hostStateR.body?.ok === true ? hostStateR.body : null

  // ---- 1. Host process identity -------------------------------------------
  if (!hostInfo) {
    check('host.reachable', 'Host /host endpoint answered', 'fail', 'HTTP ' + hostInfoR.status)
  } else {
    check('host.reachable', 'Host /host endpoint answered', 'pass', hostInfo.plugin ?? null)
    check('host.data_dir', 'Host data directory is absolute', path.isAbsolute(String(hostInfo.data_dir)) ? 'pass' : 'fail', hostInfo.data_dir)
    check('host.service_url', 'Host service base URL', hostInfo.service_base_url === serviceBase ? 'pass' : 'warn',
      'host=' + hostInfo.service_base_url + ' expected=' + serviceBase)
  }

  // ---- 2. service identity ------------------------------------------------
  if (!serviceHealth) {
    check('service.reachable', 'service /health answered', 'fail', 'HTTP ' + serviceHealthR.status)
  } else {
    check('service.reachable', 'service /health answered', 'pass',
      String(serviceHealth.service) + ' ' + String(serviceHealth.service_version) + ' pid=' + String(serviceHealth.pid))
    check('service.venue', 'service venue is demo', serviceHealth.venue === 'demo' ? 'pass' : 'fail', String(serviceHealth.venue))
    check('service.ready', 'service readiness', serviceHealth.ready === true ? 'pass' : 'warn',
      (serviceHealth.reasons ?? []).join(',') || 'ready')
  }

  // ---- 3. Host <-> Service identity agreement ----------------------------
  const serviceDbPath = serviceHealth?.db?.path ?? null
  const serviceDataDir = serviceDbPath ? path.dirname(String(serviceDbPath)) : null
  if (hostInfo && serviceDataDir) {
    check('identity.data_dir', 'Host and Service use the same data directory',
      samePath(hostInfo.data_dir, serviceDataDir) ? 'pass' : 'fail',
      'host=' + hostInfo.data_dir + ' service=' + serviceDataDir)
  } else {
    check('identity.data_dir', 'Host and Service use the same data directory', 'warn', 'one side did not report a data directory')
  }
  if (options.dataDir) {
    check('identity.expected_dir', 'both sides match --data-dir', samePath(hostInfo?.data_dir, options.dataDir) && samePath(serviceDataDir, options.dataDir) ? 'pass' : 'fail',
      'expected=' + options.dataDir)
  }
  const hostLedger = hostState?.service?.ledger_id ?? null
  const serviceLedger = serviceHealth?.db?.ledger_id ?? null
  check('identity.ledger', 'Host and Service report the same ledger id',
    hostLedger && serviceLedger && hostLedger === serviceLedger ? 'pass' : 'fail',
    'host=' + String(hostLedger) + ' service=' + String(serviceLedger))
  const servicePort = serviceHealth?.port ?? null
  let hostServicePort = null
  try { hostServicePort = hostInfo?.service_base_url ? Number(new URL(hostInfo.service_base_url).port) : null } catch (error) { hostServicePort = null }
  check('identity.port', 'Host was configured for the Service port that is actually listening',
    servicePort !== null && hostServicePort !== null && Number(servicePort) === hostServicePort ? 'pass' : 'fail',
    'host=' + String(hostServicePort) + ' service=' + String(servicePort))
  const controlHost = hostState?.service?.control_state ?? null
  const controlService = serviceReady?.control_state ?? null
  check('identity.control', 'Host and Service report the same control state',
    controlHost && controlService && controlHost === controlService ? 'pass' : 'warn',
    'host=' + String(controlHost) + ' service=' + String(controlService))

  // ---- 4. release identity ----------------------------------------------
  const disk = computeRelease({ root: PLUGIN_ROOT })
  const manifest = readManifest(MANIFEST_PATH)
  check('release.manifest', 'release manifest exists for this checkout', manifest ? 'pass' : 'fail',
    manifest ? String(manifest.release_id) : 'missing: run node scripts/build-release.mjs')
  if (manifest) {
    check('release.manifest_current', 'release manifest matches the checkout',
      manifest.source_digest === disk.source_digest ? 'pass' : 'warn',
      manifest.source_digest === disk.source_digest
        ? String(manifest.release_id)
        : 'manifest=' + String(manifest.release_id) + ' checkout=' + disk.release_id + ' (sources changed after the build)')
  }
  if (build) {
    check('release.loaded', 'running Host reports a source release', build.release_id ? 'pass' : 'fail',
      String(build.release_id ?? 'none') + ' source=' + String(build.release_source))
    check('release.loaded_matches_build', 'the running Host loaded the packaged release',
      build.release_source_digest && manifest && build.release_source_digest === manifest.source_digest ? 'pass' : 'warn',
      'loaded=' + String(build.release_source_digest) + ' packaged=' + String(manifest?.source_digest))
    check('release.loaded_at_separate', 'load time is reported separately from the release id',
      typeof build.loaded_at === 'string' && build.loaded_at.length > 0 ? 'pass' : 'fail', String(build.loaded_at))
    check('build.identity', 'buildId / chain_version / configRevision',
      Boolean(build.buildId) && Boolean(build.chain_version) ? 'pass' : 'fail',
      build.buildId + ' chain=' + build.chain_version + ' revision=' + String(build.configRevision))
  }

  // ---- 5. automation state (facts, not UI state) -------------------------
  const automation = hostState?.automation ?? hostInfo?.automation ?? null
  if (!automation) {
    check('automation.state', 'Host reports an automation projection', 'warn', 'no automation block (older Host)')
  } else {
    check('automation.state', 'automation state', 'pass',
      automation.state + ' reason=' + String(automation.reason_code)
      + ' scheduler=' + String(automation.scheduler_enabled)
      + ' control=' + String(automation.service_control?.state)
      + ' last_success=' + String(automation.last_success_at))
  }
  if (hostState?.schedule || hostInfo?.schedule) {
    const schedule = hostInfo?.schedule ?? hostState.schedule
    check('automation.schedule', 'scheduler projection', 'pass',
      'enabled=' + String(schedule.enabled) + ' next=' + String(schedule.next_at) + ' last=' + String(schedule.last_result?.kind ?? 'none'))
  }

  const failures = checks.filter((entry) => entry.status === 'fail')
  const warnings = checks.filter((entry) => entry.status === 'warn')

  if (options.json) {
    console.log(JSON.stringify({
      ok: failures.length === 0,
      host: hostBase,
      service: serviceBase,
      token_source: token.source,
      checks,
      release: { disk: disk.release_id, manifest: manifest?.release_id ?? null, loaded: build?.release_id ?? null },
      loaded_at: build?.loaded_at ?? null,
      automation,
    }, null, 2))
  } else {
    const mark = (status) => (status === 'pass' ? 'PASS' : status === 'warn' ? 'WARN' : 'FAIL')
    console.log('trading-workbench runtime verification')
    console.log('  host      ' + hostBase + '  (token via ' + token.source + ')')
    console.log('  service   ' + serviceBase)
    console.log('  release   checkout=' + disk.release_id + ' manifest=' + String(manifest?.release_id ?? 'missing') + ' loaded=' + String(build?.release_id ?? 'unknown'))
    console.log('  loaded_at ' + String(build?.loaded_at ?? 'unknown'))
    console.log('')
    for (const entry of checks) {
      console.log('  [' + mark(entry.status) + '] ' + entry.label + (entry.detail ? '\n         ' + entry.detail : ''))
    }
    console.log('')
    console.log(failures.length === 0
      ? 'result: ' + checks.length + ' checks, ' + warnings.length + ' warning(s)'
      : 'result: ' + failures.length + ' FAILED of ' + checks.length + ' checks')
    if (failures.length === 0 && automation) {
      console.log('automation: ' + automation.state + ' (' + String(automation.reason_code) + '), service control=' + String(automation.service_control?.state))
    }
  }
  process.exit(failures.length === 0 ? 0 : 1)
}

main().catch((error) => {
  console.error('verify-runtime failed: ' + String(error?.stack ?? error))
  process.exit(2)
})
