/**
 * 16 §1: first-run service bootstrap.
 *
 * A stranger's machine has the plugin package but no Python venv, so the
 * workbench can render (read-only) yet never gets data. This module owns the
 * two honest halves of the fix:
 *
 *   readiness()  - what is actually on this machine (python? venv? deps? service?)
 *   start()      - one idempotent job: create venv -> pip install -> start -> verify
 *
 * Rules:
 *  - every step reports its own status + the last lines of output; a failure is a
 *    failure (no "probably fine");
 *  - the job is in-memory and single-flight: a second POST returns the running
 *    job instead of starting a second pip install;
 *  - nothing here guesses a python: candidates are probed by running them, and a
 *    candidate that cannot print its version is not offered as usable.
 */

import fs from 'node:fs'
import path from 'node:path'

export const SETUP_STEPS = Object.freeze([
  { id: 'create-venv', label: { zh: '创建虚拟环境', en: 'Create venv' } },
  { id: 'install-deps', label: { zh: '安装依赖', en: 'Install dependencies' } },
  { id: 'start-service', label: { zh: '启动服务', en: 'Start service' } },
  { id: 'verify', label: { zh: '校验就绪', en: 'Verify ready' } },
])

const LOG_TAIL_LINES = 40
const VENV_TIMEOUT_MS = 180_000
const PIP_TIMEOUT_MS = 900_000

function tailPush(list, text) {
  for (const line of String(text).split(/\r?\n/)) {
    if (!line.trim()) continue
    list.push(line.slice(0, 500))
  }
  while (list.length > LOG_TAIL_LINES) list.shift()
}

/** Run one command, capture the tail of stdout+stderr, resolve {code, signal, timedOut}. */
function runCommand(spawnImpl, { command, args, cwd, env, timeoutMs, onLine, now }) {
  return new Promise((resolve) => {
    let child
    const state = { code: null, signal: null, timedOut: false }
    let settled = false
    const finish = () => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(state)
    }
    const timer = setTimeout(() => {
      state.timedOut = true
      try { child?.kill() } catch { /* best effort */ }
      // Give the process a moment to die before reporting.
      setTimeout(finish, 500)
    }, timeoutMs)
    try {
      child = spawnImpl(command, args, {
        cwd,
        env,
        windowsHide: true,
        stdio: ['ignore', 'pipe', 'pipe'],
      })
    } catch (error) {
      state.code = -1
      onLine?.(`spawn failed: ${error?.message ?? error}`)
      finish()
      return
    }
    for (const stream of [child.stdout, child.stderr]) {
      stream?.on?.('data', (chunk) => onLine?.(String(chunk)))
    }
    child.on?.('error', (error) => {
      state.code = -1
      onLine?.(`error: ${error?.message ?? error}`)
      finish()
    })
    child.on?.('close', (code, signal) => {
      state.code = code
      state.signal = signal ?? null
      finish()
    })
    void now
  })
}

export function createServiceSetup({
  dataDir,
  env = process.env,
  logger = null,
  spawnImpl = null,
  launcherPaths,
  probeServiceHealth,
  startService,
  now = Date.now,
} = {}) {
  if (!spawnImpl) throw new Error('createServiceSetup requires spawnImpl')
  const venvDir = path.join(dataDir, 'service-venv')
  const win = process.platform === 'win32'
  const venvPython = path.join(venvDir, win ? 'Scripts' : 'bin', win ? 'python.exe' : 'python')
  const requirementFile = path.join(path.dirname(launcherPaths.entry), 'requirements.txt')
  const jobs = new Map()
  let currentJobId = null

  function pythonCandidates() {
    const candidates = []
    if (env.WORKBENCH_SERVICE_PYTHON) candidates.push({ path: env.WORKBENCH_SERVICE_PYTHON, args: [], source: 'env' })
    candidates.push({ path: venvPython, args: [], source: 'venv' })
    const legacy = launcherPaths.python
    if (legacy && legacy !== venvPython) candidates.push({ path: legacy, args: [], source: 'workspace' })
    if (win) candidates.push({ path: 'py', args: ['-3'], source: 'py' })
    candidates.push({ path: 'python3', args: [], source: 'python3' })
    candidates.push({ path: 'python', args: [], source: 'python' })
    return candidates
  }

  /**
   * Find a usable system python. The venv python is NOT a system candidate: it
   * is the result, not the thing that creates it (a venv with no deps still
   * reports a version and would look "fine" while nothing is installed).
   */
  async function discoverPython() {
    for (const candidate of pythonCandidates()) {
      const exists = candidate.source === 'py' || candidate.source === 'python3' || candidate.source === 'python'
        ? true
        : fs.existsSync(candidate.path)
      if (!exists) continue
      const lines = []
      const result = await runCommand(spawnImpl, {
        command: candidate.path,
        args: [...candidate.args, '-c', 'import sys;print(sys.version.split()[0])'],
        env,
        timeoutMs: 8000,
        onLine: (line) => lines.push(line),
        now,
      })
      const version = (lines.join(' ').match(/(\d+\.\d+\.\d+)/) ?? [])[1] ?? null
      if (result.code === 0 && version) return { found: true, path: candidate.path, args: candidate.args, version, source: candidate.source }
    }
    return { found: false, path: null, args: [], version: null, source: null }
  }

  /** Does this interpreter have the service dependencies importable? */
  async function checkDepsOf(pythonPath) {
    if (!pythonPath || !fs.existsSync(pythonPath)) return { installed: false, checked: false, detail: 'interpreter missing' }
    const lines = []
    const result = await runCommand(spawnImpl, {
      command: pythonPath,
      args: ['-c', 'import uvicorn, fastapi, httpx; print("deps-ok")'],
      env,
      timeoutMs: 15_000,
      onLine: (line) => lines.push(line),
      now,
    })
    return {
      installed: result.code === 0 && lines.join(' ').includes('deps-ok'),
      checked: true,
      detail: (lines.slice(-3).join(' ').slice(0, 300)) || null,
    }
  }

  function checkDeps() {
    return checkDepsOf(venvPython)
  }

  let cachedReadiness = null
  let cachedAt = 0

  async function readiness({ force = false } = {}) {
    if (!force && cachedReadiness && now() - cachedAt < 20_000) return cachedReadiness
    const python = await discoverPython()
    const venvExists = fs.existsSync(venvPython)
    const deps = await checkDeps()
    // What the "启动服务" button would actually use. On the author's machine that
    // is the reference project's venv; on a fresh install there is none yet.
    const interpreter = launcherPaths.python && fs.existsSync(launcherPaths.python) ? launcherPaths.python : null
    const interpreterDeps = interpreter && interpreter !== venvPython
      ? await checkDepsOf(interpreter)
      : deps
    const usableEnv = venvExists ? deps.installed : interpreterDeps.installed
    const service = await probeServiceHealth({ timeoutMs: 1200 })
    const dotenvPath = launcherPaths.dotenv
    const blockers = []
    if (!python.found) blockers.push({ code: 'PYTHON_MISSING', message: 'no usable Python 3 found on this machine' })
    // A missing venv is only a blocker when nothing usable is installed: a
    // service that already runs from another venv needs no setup at all.
    if (!venvExists && !interpreterDeps.installed) blockers.push({ code: 'VENV_MISSING', message: `virtualenv missing at ${venvDir}` })
    else if (venvExists && !deps.installed) blockers.push({ code: 'DEPS_MISSING', message: 'service dependencies are not installed in the venv' })
    if (!service.ok) blockers.push({ code: 'SERVICE_DOWN', message: 'the trading service is not answering /health' })
    const result = {
      ready: service.ok === true,
      // Python is only needed when no usable environment exists yet.
      needsPython: !usableEnv,
      python,
      venv: {
        dir: venvDir,
        python: venvPython,
        exists: venvExists,
        in_use: interpreter === venvPython,
      },
      deps: { ...deps, installed: usableEnv, venv_installed: deps.installed, interpreter: interpreterDeps.installed },
      service,
      dotenv: { path: dotenvPath, exists: dotenvPath ? fs.existsSync(dotenvPath) : false },
      requirements: { path: requirementFile, exists: fs.existsSync(requirementFile) },
      blockers,
      checked_at: new Date(now()).toISOString(),
    }
    cachedReadiness = result
    cachedAt = now()
    return result
  }

  function newJob() {
    return {
      id: `setup-${Math.random().toString(16).slice(2, 10)}`,
      phase: 'running',
      created_at: new Date(now()).toISOString(),
      updated_at: new Date(now()).toISOString(),
      steps: SETUP_STEPS.map((step) => ({ id: step.id, label: step.label, status: 'pending', started_at: null, finished_at: null, exit_code: null, log_tail: [] })),
      error: null,
    }
  }

  function touch(job) {
    job.updated_at = new Date(now()).toISOString()
  }

  function stepOf(job, id) {
    return job.steps.find((step) => step.id === id)
  }

  function fail(job, stepId, code, message) {
    const step = stepOf(job, stepId)
    if (step) {
      step.status = 'failed'
      step.finished_at = new Date(now()).toISOString()
      step.exit_code = step.exit_code ?? null
    }
    job.phase = 'failed'
    job.error = { code, message }
    touch(job)
    return job
  }

  async function runSetup(job, { python }) {
    const stepRun = async (id, command, args, { timeoutMs, okCodes = [0] } = {}) => {
      const step = stepOf(job, id)
      step.status = 'running'
      step.started_at = new Date(now()).toISOString()
      touch(job)
      const result = await runCommand(spawnImpl, {
        command,
        args,
        cwd: path.dirname(launcherPaths.entry),
        env: { ...env, WORKBENCH_DOTENV_FILE: launcherPaths.dotenv },
        timeoutMs,
        onLine: (line) => tailPush(step.log_tail, line),
        now,
      })
      step.exit_code = result.code
      step.finished_at = new Date(now()).toISOString()
      if (result.timedOut || !okCodes.includes(result.code)) {
        step.status = 'failed'
        fail(job, id, result.timedOut ? 'E_SETUP_TIMEOUT' : 'E_SETUP_STEP_FAILED',
          `${id} exited with ${result.timedOut ? 'timeout' : `code ${result.code}`}`)
        return false
      }
      step.status = 'ok'
      touch(job)
      return true
    }

    // 1) venv (skipped when it already exists)
    if (fs.existsSync(venvPython)) {
      const step = stepOf(job, 'create-venv')
      step.status = 'skipped'
      step.finished_at = new Date(now()).toISOString()
      touch(job)
    } else {
      if (!python.found) {
        fail(job, 'create-venv', 'PYTHON_MISSING', 'no usable Python 3 found on this machine')
        return job
      }
      const ok = await stepRun('create-venv', python.path, [...(python.args ?? []), '-m', 'venv', venvDir], { timeoutMs: VENV_TIMEOUT_MS })
      if (!ok) return job
    }

    // 2) dependencies
    const venvOk = await stepRun('install-deps', venvPython, ['-m', 'pip', 'install', '--disable-pip-version-check', '-r', requirementFile], { timeoutMs: PIP_TIMEOUT_MS })
    if (!venvOk) return job

    // 3) start the service through the same path the button uses
    const startStep = stepOf(job, 'start-service')
    startStep.status = 'running'
    startStep.started_at = new Date(now()).toISOString()
    touch(job)
    try {
      const started = await startService()
      tailPush(startStep.log_tail, `started pid=${started?.pid ?? '—'} waited=${started?.waited_ms ?? 0}ms`)
      startStep.status = 'ok'
      startStep.finished_at = new Date(now()).toISOString()
      touch(job)
    } catch (error) {
      tailPush(startStep.log_tail, `${error?.code ?? 'E_START_FAILED'}: ${error?.message ?? error}`)
      startStep.status = 'failed'
      startStep.finished_at = new Date(now()).toISOString()
      fail(job, 'start-service', error?.code ?? 'E_SERVICE_START_FAILED', String(error?.message ?? error))
      return job
    }

    // 4) verify
    const verifyStep = stepOf(job, 'verify')
    verifyStep.status = 'running'
    verifyStep.started_at = new Date(now()).toISOString()
    touch(job)
    const probe = await probeServiceHealth({ timeoutMs: 2500 })
    if (!probe.ok) {
      fail(job, 'verify', 'E_SERVICE_NOT_READY', 'service did not answer /health after start')
      return job
    }
    verifyStep.status = 'ok'
    verifyStep.finished_at = new Date(now()).toISOString()
    job.phase = 'ready'
    job.error = null
    touch(job)
    cachedReadiness = null
    return job
  }

  function start() {
    if (currentJobId) {
      const running = jobs.get(currentJobId)
      if (running && running.phase === 'running') {
        // Single-flight: answer with the job already doing the work.
        return { job: running, reused: true }
      }
    }
    const job = newJob()
    jobs.set(job.id, job)
    currentJobId = job.id
    job.promise = (async () => {
      try {
        const ready = await readiness({ force: true })
        const python = ready.python
        await runSetup(job, { python })
      } catch (error) {
        job.phase = 'failed'
        job.error = { code: 'E_SETUP_CRASHED', message: String(error?.message ?? error) }
        touch(job)
      }
      // Drop the promise so status() stays a plain object.
      delete job.promise
      return job
    })()
    return { job, reused: false }
  }

  function status(jobId) {
    // An explicit unknown id is a 404, never a silent fallback to the latest job:
    // the UI polls a specific job and must not be shown another one's steps.
    if (jobId) return jobs.get(jobId) ?? null
    return currentJobId ? (jobs.get(currentJobId) ?? null) : null
  }

  return {
    readiness,
    start,
    status,
    venvDir,
    venvPython,
    pythonCandidates,
    _jobs: jobs,
  }
}
