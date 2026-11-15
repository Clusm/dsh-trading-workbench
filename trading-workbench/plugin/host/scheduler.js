/**
 * Host-side auto scheduler (M1.4).
 *
 * 需求基线 §12 写的是「手动点运行一轮 + 每 10 分钟自动跑」。M1.1–M1.3 期间被
 * Q5=B 决策显式推迟（`client.js` 里那行「自动调度：未启用（M2）」就是它的痕迹），
 * 结果是上线 24 小时只跑了 32 轮、活跃占比 2.43%。本模块把这条补上。
 *
 * 设计约束（都与线上事故一一对应）：
 * - **单飞**：同一时刻只跑一轮；与手动轮共用 `integration.inFlight` 闸门，
 *   重叠记 `OVERLAP_SKIPPED` 而不是失败。
 * - **幂等**：slot 按 epoch 对齐 `floor(now / intervalMs)`，`slot_id` 交给服务端
 *   唯一索引去重；同一 slot 重复创建返回 `E_CYCLE_DUPLICATE_SLOT`，视为
 *   "本 slot 已完成"，不计失败。
 * - **开链前硬门**：服务未就绪 / 控制态非 normal / 无 Demo 凭据 / 无账户权益时
 *   直接跳过，**不消耗 LLM 调用**（历史上 5 轮注定失败的轮次白烧了 788s）。
 * - **熔断**：连续 N 轮失败暂停 M 分钟；24h 轮数上限，防止 token 失控。
 * - **可关**：`WORKBENCH_AUTO_SCHEDULE=off` 立即停止；`setEnabled` 可在运行时切换。
 *
 * 该模块不 import Harness、不碰网络，只调用注入的 integration；因此可以离线单测。
 */

export const AUTO_SCHEDULE_CONTRACT = 'trading-workbench/auto-schedule/1'

export const AUTO_SCHEDULE_DEFAULTS = Object.freeze({
  enabled: true,
  intervalMs: 10 * 60_000,
  symbol: 'BTCUSDT',
  startDelayMs: 90_000,
  maxRoundsPerDay: 96,
  maxConsecutiveFailures: 3,
  pauseAfterFailuresMs: 30 * 60_000,
  historyLimit: 25,
})

const SYMBOL_RE = /^[A-Z0-9]{5,20}$/

function envText(env, name) {
  const raw = env?.[name]
  return typeof raw === 'string' ? raw.trim().toLowerCase() : ''
}

function envFlag(env, name, fallback) {
  const text = envText(env, name)
  if (text === '') return fallback
  if (['1', 'on', 'true', 'yes', 'enabled'].includes(text)) return true
  if (['0', 'off', 'false', 'no', 'disabled'].includes(text)) return false
  return fallback
}

function envInt(env, name, fallback, { min, max }) {
  const raw = env?.[name]
  if (raw === undefined || raw === null || String(raw).trim() === '') return fallback
  const value = Number.parseInt(String(raw).trim(), 10)
  if (!Number.isFinite(value)) return fallback
  return Math.min(max, Math.max(min, value))
}

export function readAutoScheduleConfig(env = {}) {
  const symbol = String(env?.WORKBENCH_AUTO_SYMBOL ?? '').trim().toUpperCase()
  return {
    enabled: envFlag(env, 'WORKBENCH_AUTO_SCHEDULE', AUTO_SCHEDULE_DEFAULTS.enabled),
    intervalMs: envInt(env, 'WORKBENCH_AUTO_INTERVAL_MS', AUTO_SCHEDULE_DEFAULTS.intervalMs, {
      min: 60_000,
      max: 3_600_000,
    }),
    symbol: SYMBOL_RE.test(symbol) ? symbol : AUTO_SCHEDULE_DEFAULTS.symbol,
    startDelayMs: envInt(env, 'WORKBENCH_AUTO_START_DELAY_MS', AUTO_SCHEDULE_DEFAULTS.startDelayMs, {
      min: 0,
      max: 600_000,
    }),
    maxRoundsPerDay: envInt(env, 'WORKBENCH_AUTO_MAX_ROUNDS_PER_DAY', AUTO_SCHEDULE_DEFAULTS.maxRoundsPerDay, {
      min: 1,
      max: 288,
    }),
    maxConsecutiveFailures: envInt(
      env,
      'WORKBENCH_AUTO_MAX_FAILURES',
      AUTO_SCHEDULE_DEFAULTS.maxConsecutiveFailures,
      { min: 1, max: 20 },
    ),
    pauseAfterFailuresMs: envInt(
      env,
      'WORKBENCH_AUTO_PAUSE_MS',
      AUTO_SCHEDULE_DEFAULTS.pauseAfterFailuresMs,
      { min: 60_000, max: 24 * 3_600_000 },
    ),
    historyLimit: AUTO_SCHEDULE_DEFAULTS.historyLimit,
  }
}

function isoAt(ms) {
  return Number.isFinite(ms) ? new Date(ms).toISOString() : null
}

/**
 * F1 (§3.2 / §8·F): "停止自动交易" must really close the local scheduler, and the
 * Host route that implements it lives in integration.js. Rather than have
 * index.js wire a second handle (two owners of one timer is how a scheduler
 * ends up armed after a stop), the scheduler publishes a narrow control facade
 * on the integration object it was built for. integration.js calls exactly
 * these methods and never reads this module's internals.
 */
export const SCHEDULER_CONTROL_CONTRACT = 'trading-workbench/scheduler-control/1'

export class AutoScheduler {
  constructor({ integration, env = {}, logger = null, now = Date.now, config = {} } = {}) {
    if (!integration || typeof integration.run !== 'function') {
      throw new TypeError('AutoScheduler requires an integration with run()')
    }
    this.integration = integration
    this.controlFacade = null
    this.logger = logger ?? null
    this.now = typeof now === 'function' ? now : Date.now
    this.config = { ...AUTO_SCHEDULE_DEFAULTS, ...readAutoScheduleConfig(env), ...config }
    this.enabled = Boolean(this.config.enabled)
    this.started = false
    this.busy = false
    this.timer = null
    this.nextAt = null
    this.lastTickAt = null
    this.lastSlot = null
    this.consecutiveFailures = 0
    this.pausedUntil = null
    this.pauseReason = null
    this.history = []
    this.disposed = false
    this._attachControl()
  }

  /**
   * Publish the control facade on the integration. A plain test double simply
   * keeps the assignment; production always succeeds.
   */
  _attachControl() {
    if (this.controlFacade) return this.controlFacade
    const integration = this.integration
    if (!integration || typeof integration !== 'object') return null
    const facade = {
      contract: SCHEDULER_CONTROL_CONTRACT,
      status: () => this.status(),
      setEnabled: (value) => this.setEnabled(value),
      pause: (ms, reason) => this.pause(ms, reason),
      resume: () => this.resume(),
    }
    this.controlFacade = facade
    try {
      integration.schedulerControl = facade
    } catch (error) {
      this.logger?.warn?.('[twb] cannot publish scheduler control: ' + String(error?.message ?? error))
    }
    return facade
  }

  _detachControl() {
    const integration = this.integration
    try {
      if (integration && integration.schedulerControl === this.controlFacade) delete integration.schedulerControl
    } catch (error) { /* best effort */ }
    this.controlFacade = null
  }

  // ------------------------------------------------------------------ lifecycle
  start() {
    if (this.disposed) return this.status()
    if (this.started) return this.status()
    this.started = true
    // Defensive: a reload or a second start() must not leave two timers armed,
    // which used to fire two rounds inside the same 10-minute slot.
    this._clearTimer()
    if (this.enabled) this._arm(this.config.startDelayMs)
    return this.status()
  }

  stop() {
    this.started = false
    this._clearTimer()
    this.nextAt = null
    return this.status()
  }

  dispose() {
    this.disposed = true
    this.stop()
    this._detachControl()
  }

  setEnabled(enabled) {
    const next = Boolean(enabled)
    if (next === this.enabled) return this.status()
    this.enabled = next
    if (!next) {
      this._clearTimer()
      this.nextAt = null
      this._record('disabled', 'AUTO_DISABLED', 'scheduler switched off', null)
    } else if (this.started) {
      this._arm(1_000)
    }
    return this.status()
  }

  pause(ms, reason = 'manual_pause') {
    const duration = Math.max(0, Number(ms) || 0)
    this.pausedUntil = this.now() + duration
    this.pauseReason = String(reason)
    return this.status()
  }

  resume() {
    this.pausedUntil = null
    this.pauseReason = null
    this.consecutiveFailures = 0
    if (this.started && this.enabled) this._arm(1_000)
    return this.status()
  }

  _clearTimer() {
    if (this.timer) {
      try { clearTimeout(this.timer) } catch { /* best effort */ }
    }
    this.timer = null
  }

  _arm(delayMs) {
    this._clearTimer()
    const delay = Math.max(0, Math.min(Number(delayMs) || 0, 3_600_000))
    this.nextAt = this.now() + delay
    this.timer = setTimeout(() => {
      this.timer = null
      void this._runTick()
    }, delay)
    // Never keep the Host process alive just for the scheduler.
    if (typeof this.timer?.unref === 'function') this.timer.unref()
  }

  async _runTick() {
    try {
      await this.tick('timer')
    } catch (error) {
      this.logger?.warn?.(`[twb] auto-schedule tick failed: ${error?.code ?? error?.message ?? error}`)
      this._record('error', error?.code ?? 'TICK_FAILED', error?.message ?? String(error), null)
    } finally {
      if (this.started && this.enabled && !this.disposed) this._arm(this.config.intervalMs)
    }
  }

  // ------------------------------------------------------------------- tick
  slotAt(at = this.now()) {
    const slot = Math.floor(at / this.config.intervalMs)
    return { slot, slotId: `auto-${this.config.intervalMs}-${slot}` }
  }

  async tick(reason = 'timer') {
    const at = this.now()
    this.lastTickAt = at
    const { slot, slotId } = this.slotAt(at)
    this.lastSlot = slot

    if (!this.enabled) return this._skip('AUTO_DISABLED', 'scheduler disabled', slotId)
    if (this.busy) return this._skip('OVERLAP_SKIPPED', 'previous auto round still running', slotId)
    if (this.lastRunSlot === slot) {
      // Two timers (reload) or a clock jump can land twice in one slot; the
      // service would reject the duplicate anyway, but skipping here saves the
      // round record and the HTTP round-trip.
      return this._skip('OVERLAP_SKIPPED', 'slot already attempted by this scheduler', slotId)
    }
    if (this.pausedUntil !== null && at < this.pausedUntil) {
      return this._skip('PAUSED_AFTER_FAILURES', this.pauseReason ?? 'circuit open', slotId)
    }
    if (this._autoRoundsLast24h() >= this.config.maxRoundsPerDay) {
      return this._skip('DAILY_BUDGET_EXHAUSTED', `max ${this.config.maxRoundsPerDay} auto rounds / 24h`, slotId)
    }
    if (this.integration.inFlight) {
      // A hung provider can leave inFlight set for hours; reclaim it so the loop
      // keeps running instead of skipping every later slot forever.
      if (typeof this.integration.recoverStaleInFlight === 'function'
        && this.integration.recoverStaleInFlight() === false) {
        return this._skip('ROUND_IN_FLIGHT', 'a round is already in flight', slotId)
      }
      if (this.integration.inFlight) {
        return this._skip('ROUND_IN_FLIGHT', 'a round is already in flight', slotId)
      }
      this._record('recovered', 'HOST_STALE_RUN', 'stale in-flight round was reclaimed', slotId)
    }

    let pre
    try {
      pre = await this.integration.preflight()
    } catch (error) {
      return this._skip(error?.code ?? 'PREFLIGHT_FAILED', error?.message ?? String(error), slotId)
    }
    if (!pre || pre.ok !== true) {
      return this._skip(pre?.code ?? 'PREFLIGHT_REJECTED', pre?.detail ?? 'preflight rejected', slotId, pre)
    }

    this.busy = true
    this.lastRunSlot = slot
    let guard = null
    try {
      this.logger?.info?.(`[twb] auto round start slot=${slotId} symbol=${this.config.symbol} reason=${reason}`)
      const runTimeout = Number(this.integration?.runTimeoutMs) || 400_000
      const guardMs = Number(this.config.runGuardMs) > 0 ? Number(this.config.runGuardMs) : runTimeout + 60_000
      // Second line of defence: even if the integration watchdog cannot fire
      // (e.g. a provider that ignores abort), the scheduler must not stay busy.
      const timedOut = Symbol('auto-schedule-timeout')
      const guardPromise = new Promise((resolve) => {
        guard = setTimeout(() => resolve(timedOut), guardMs)
        if (typeof guard?.unref === 'function') guard.unref()
      })
      const settled = await Promise.race([
        this.integration.run({ symbol: this.config.symbol, trigger: 'auto', slotId }),
        guardPromise,
      ])
      if (settled === timedOut) {
        if (typeof this.integration.abandonRun === 'function' && this.integration.inFlight) {
          this.integration.abandonRun(this.integration.inFlight, 'HOST_RUN_TIMEOUT', 'scheduler guard timeout')
        }
        this._onFailure('HOST_RUN_TIMEOUT')
        this._record('failed', 'HOST_RUN_TIMEOUT', `round exceeded ${guardMs} ms`, slotId)
        return null
      }
      const result = settled
      const round = result?.round ?? null
      const outcome = round?.outcome ?? null
      const failed = outcome === 'failed' || round?.status === 'failed'
      if (failed) {
        this._onFailure(round?.reasonCode ?? 'ROUND_FAILED')
      } else {
        this._onSuccess()
      }
      this._record(
        failed ? 'failed' : 'round',
        round?.reasonCode ?? (outcome === 'trade' ? 'TRADED' : 'OK'),
        `outcome=${outcome ?? 'unknown'}`,
        slotId,
      )
      return result
    } catch (error) {
      const code = error?.code ?? 'RUN_FAILED'
      // Idempotent slots and a competing manual run are not failures.
      if (code === 'E_CYCLE_DUPLICATE_SLOT' || code === 'RUN_IN_PROGRESS' || code === 'E_CYCLE_RUNNING') {
        return this._skip('OVERLAP_SKIPPED', code, slotId)
      }
      this._onFailure(code)
      this._record('failed', code, error?.message ?? String(error), slotId)
      return null
    } finally {
      try { if (guard) clearTimeout(guard) } catch { /* best effort */ }
      this.busy = false
    }
  }

  _skip(code, detail, slotId, extra = null) {
    this._record('skip', code, detail, slotId, extra)
    return null
  }

  _onFailure(code) {
    this.consecutiveFailures += 1
    if (this.consecutiveFailures >= this.config.maxConsecutiveFailures) {
      this.pausedUntil = this.now() + this.config.pauseAfterFailuresMs
      this.pauseReason = `circuit open after ${this.consecutiveFailures} consecutive failures (${code})`
      this.consecutiveFailures = 0
      this.logger?.warn?.(`[twb] auto-schedule ${this.pauseReason}; resuming in ${this.config.pauseAfterFailuresMs}ms`)
    }
  }

  _onSuccess() {
    this.consecutiveFailures = 0
    this.pausedUntil = null
    this.pauseReason = null
  }

  _record(kind, code, detail, slotId, extra = null) {
    const entry = {
      at: isoAt(this.now()),
      kind,
      code: code ?? null,
      detail: detail === undefined || detail === null ? null : String(detail).slice(0, 240),
      slot_id: slotId ?? null,
      reason: extra ?? null,
    }
    this.history.unshift(entry)
    if (this.history.length > this.config.historyLimit) this.history.length = this.config.historyLimit
    return entry
  }

  _storeRounds() {
    const store = this.integration?.store
    if (!store || typeof store.list !== 'function') return []
    try {
      if (typeof store.load === 'function') store.load()
      return store.list() ?? []
    } catch {
      return []
    }
  }

  _autoRoundsLast24h() {
    // A05: prefer the durable sidecar; the round store is a UI cache and may
    // evict old auto rounds after manual rounds fill the 100-row window.
    if (typeof this.integration?.autoRoundsLast24h === 'function') {
      try {
        const count = this.integration.autoRoundsLast24h(this.now())
        if (Number.isFinite(count)) return count
      } catch (error) {
        this.logger?.warn?.(`[twb] auto budget sidecar failed: ${error?.message ?? error}`)
      }
    }
    const since = this.now() - 24 * 3_600_000
    let count = 0
    for (const round of this._storeRounds()) {
      if (round?.trigger !== 'auto') continue
      const started = Date.parse(round?.startedAt ?? '')
      if (Number.isFinite(started) && started >= since) count += 1
    }
    return count
  }

  status() {
    return {
      contract: AUTO_SCHEDULE_CONTRACT,
      enabled: this.enabled,
      started: this.started,
      busy: this.busy,
      interval_ms: this.config.intervalMs,
      symbol: this.config.symbol,
      start_delay_ms: this.config.startDelayMs,
      max_rounds_per_day: this.config.maxRoundsPerDay,
      rounds_last_24h: this._autoRoundsLast24h(),
      max_consecutive_failures: this.config.maxConsecutiveFailures,
      consecutive_failures: this.consecutiveFailures,
      paused_until: isoAt(this.pausedUntil),
      pause_reason: this.pauseReason,
      last_tick_at: isoAt(this.lastTickAt),
      last_slot: this.lastSlot,
      next_at: isoAt(this.nextAt),
      last_result: this.history[0] ?? null,
      history: this.history.slice(0, 10),
    }
  }
}

export function createAutoScheduler(options = {}) {
  return new AutoScheduler(options)
}
