/**
 * Round archive (15 §3): one JSONL partition per Asia/Shanghai day.
 *
 * Why this replaces the old 100-row JSON cache
 * --------------------------------------------
 * The old `RoundStore` kept a single `host-rounds.json` and rewrote it whole on
 * every round, so the list had to be capped at 100 (DEFAULT_STORE_MAX_ROUNDS) to
 * stay fast. The cap silently dropped the oldest decision chains. Here every
 * round is appended as one line to `rounds/YYYY-MM-DD.jsonl`:
 *
 *  - write cost is O(1) and independent of history size;
 *  - a crash can only lose the last partial line (bad lines are skipped on read);
 *  - a day is the natural partition the UI asks for ("today by default");
 *  - updates (stale-run repair, watchdog finalization) append a newer line for
 *    the same roundId; the reader keeps the last one, so the log is append-only
 *    and still converges to one record per round.
 *
 * Day boundary: Asia/Shanghai (UTC+8, no DST) — the same calendar the UI shows
 * as 时间（北京时间）, so a 23:59 round never appears under the next day.
 *
 * Disk: ~21 KB per round (result/aiCalls dominate). At the 96-round/day
 * schedule ceiling that is ~2 MB/day before update lines, so retention is
 * configurable (`retentionDays`) and default-off (15 §7).
 */

import fs from 'node:fs'
import path from 'node:path'

export const ROUND_ARCHIVE_CONTRACT = 'trading-workbench/round-archive/1'
export const ARCHIVE_FILE_SUFFIX = '.jsonl'
export const ARCHIVE_INDEX_FILE = '_migrated.json'
const SHANGHAI_OFFSET_MS = 8 * 3_600_000
const DAY_MS = 86_400_000
const DATE_KEYS_TTL_MS = 30_000

/** The archive day key for an instant: Asia/Shanghai, no DST. */
export function archiveDay(value = Date.now()) {
  const ms = typeof value === 'number'
    ? value
    : (value instanceof Date ? value.getTime() : Date.parse(String(value ?? '')))
  if (!Number.isFinite(ms)) return null
  return new Date(ms + SHANGHAI_OFFSET_MS).toISOString().slice(0, 10)
}

/** Strict `YYYY-MM-DD` check that also rejects impossible calendar days. */
export function isArchiveDay(value) {
  if (typeof value !== 'string' || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false
  const ms = Date.parse(`${value}T00:00:00Z`)
  return Number.isFinite(ms) && new Date(ms).toISOString().slice(0, 10) === value
}

/**
 * Validate the `?date=` query for GET /rounds (15 §4). A missing parameter means
 * "today"; anything else must be a real day that has already happened.
 */
export function parseRoundDate(raw, { now = Date.now() } = {}) {
  const text = raw === null || raw === undefined ? '' : String(raw).trim()
  if (text === '') return archiveDay(now)
  if (!isArchiveDay(text)) {
    const error = new Error('date must be YYYY-MM-DD')
    error.code = 'E_DATE_INVALID'
    error.status = 400
    throw error
  }
  const today = archiveDay(now)
  if (text > today) {
    const error = new Error(`date is in the future (today is ${today})`)
    error.code = 'E_DATE_FUTURE'
    error.status = 400
    throw error
  }
  return text
}

export class RoundArchive {
  constructor({ dir, legacyPath = null, retentionDays = 0, now = Date.now, logger = null, maxReadDays = 60 } = {}) {
    this.dir = String(dir)
    this.legacyPath = legacyPath === null || legacyPath === undefined ? null : String(legacyPath)
    const days = Number(retentionDays)
    this.retentionDays = Number.isFinite(days) && days > 0 ? Math.trunc(days) : 0
    this.now = typeof now === 'function' ? now : Date.now
    this.logger = logger
    // A detail/repair read must never walk a multi-year archive by accident.
    this.maxReadDays = Number.isFinite(maxReadDays) && maxReadDays > 0 ? Math.trunc(maxReadDays) : 60
    this.days = new Map() // date -> { mtimeMs, records }
    this.dateKeys = null
    this.dateKeysAt = 0
    this.idDates = new Map()
    this.ready = false
    this.migration = null
  }

  ensure() {
    if (this.ready) return this
    this.ready = true
    try {
      this.migration = this.migrateLegacy()
    } catch (error) {
      this.logger?.warn?.(`[twb] round archive migration failed: ${error?.message ?? error}`)
    }
    return this
  }

  fileOf(date) {
    return path.join(this.dir, `${date}${ARCHIVE_FILE_SUFFIX}`)
  }

  /**
   * Append the current version of a round. Append-only by design: a later line
   * for the same roundId wins on read, so repairs never rewrite history.
   */
  upsert(record) {
    this.ensure()
    if (!record || typeof record.roundId !== 'string' || !record.roundId) {
      this.logger?.warn?.('[twb] round archive ignored a record without roundId')
      return record
    }
    const day = archiveDay(record.startedAt ?? record.finishedAt ?? this.now())
    if (!day) {
      this.logger?.warn?.(`[twb] round archive ignored ${record.roundId}: no usable timestamp`)
      return record
    }
    try {
      fs.mkdirSync(this.dir, { recursive: true })
      fs.appendFileSync(this.fileOf(day), `${JSON.stringify(record)}\n`, 'utf8')
    } catch (error) {
      this.logger?.warn?.(`[twb] cannot append round ${record.roundId}: ${error?.message ?? error}`)
      return record
    }
    this.idDates.set(record.roundId, day)
    this.days.delete(day)
    this.dateKeys = null
    if (this.retentionDays > 0) this.applyRetention()
    return record
  }

  /** Today's partition (the UI default). Kept as `list()` for older callers. */
  list() {
    return this.listDate(archiveDay(this.now()))
  }

  listDate(date) {
    this.ensure()
    if (!isArchiveDay(date)) return []
    const file = this.fileOf(date)
    let stat
    try {
      stat = fs.statSync(file)
    } catch (error) {
      if (error?.code !== 'ENOENT') this.logger?.warn?.(`[twb] cannot stat ${file}: ${error?.message ?? error}`)
      return []
    }
    const cached = this.days.get(date)
    if (cached && cached.mtimeMs === stat.mtimeMs) return cached.records
    let raw
    try {
      raw = fs.readFileSync(file, 'utf8')
    } catch (error) {
      this.logger?.warn?.(`[twb] cannot read ${file}: ${error?.message ?? error}`)
      return []
    }
    return this.parseDay(date, raw, stat.mtimeMs)
  }

  parseDay(date, raw, mtimeMs) {
    const byId = new Map()
    let badLines = 0
    for (const line of String(raw).split('\n')) {
      const text = line.trim()
      if (!text) continue
      let row
      try {
        row = JSON.parse(text)
      } catch {
        badLines += 1
        continue
      }
      if (!row || typeof row !== 'object' || typeof row.roundId !== 'string') {
        badLines += 1
        continue
      }
      // Append-only updates: the last line for a round wins.
      byId.delete(row.roundId)
      byId.set(row.roundId, row)
    }
    const records = [...byId.values()].sort((a, b) => (Date.parse(b?.startedAt ?? '') || 0) - (Date.parse(a?.startedAt ?? '') || 0))
    if (badLines > 0) this.logger?.warn?.(`[twb] round archive ${date}: skipped ${badLines} unreadable line(s)`)
    for (const record of records) this.idDates.set(record.roundId, date)
    this.days.set(date, { mtimeMs, records, badLines })
    return records
  }

  /** Day keys, newest first. Cached briefly; invalidated by every append. */
  listDateKeys() {
    this.ensure()
    const now = this.now()
    if (this.dateKeys && now - this.dateKeysAt < DATE_KEYS_TTL_MS) return this.dateKeys
    let names = []
    try {
      names = fs.readdirSync(this.dir)
    } catch (error) {
      if (error?.code !== 'ENOENT') this.logger?.warn?.(`[twb] cannot list ${this.dir}: ${error?.message ?? error}`)
      names = []
    }
    this.dateKeys = names
      .filter((name) => name.endsWith(ARCHIVE_FILE_SUFFIX))
      .map((name) => name.slice(0, -ARCHIVE_FILE_SUFFIX.length))
      .filter(isArchiveDay)
      .sort()
      .reverse()
    this.dateKeysAt = now
    return this.dateKeys
  }

  /**
   * One record by id, across days. Scans newest day first and only parses a file
   * that mentions the id, so a detail open does not load the whole archive.
   */
  find(roundId) {
    this.ensure()
    if (typeof roundId !== 'string' || !roundId) return null
    const known = this.idDates.get(roundId)
    if (known) {
      const hit = this.listDate(known).find((record) => record.roundId === roundId)
      if (hit) return hit
      this.idDates.delete(roundId)
    }
    const dates = this.listDateKeys().slice(0, this.maxReadDays)
    for (const date of dates) {
      let raw
      try {
        raw = fs.readFileSync(this.fileOf(date), 'utf8')
      } catch {
        continue
      }
      if (!raw.includes(roundId)) continue
      const hit = this.parseDay(date, raw, statMtime(this.fileOf(date))).find((record) => record.roundId === roundId)
      if (hit) return hit
    }
    return null
  }

  /**
   * Most recent rounds across days, newest first. Used where "latest" is the
   * question (last route snapshot, stale-run repair, budget seeding) so those
   * call sites keep their old meaning instead of silently becoming "today only".
   */
  listRecent(limit = 50, { maxDays = 30 } = {}) {
    this.ensure()
    const want = Number.isFinite(limit) && limit > 0 ? Math.trunc(limit) : 50
    const days = Number.isFinite(maxDays) && maxDays > 0 ? Math.trunc(maxDays) : 30
    const out = []
    for (const date of this.listDateKeys().slice(0, days)) {
      for (const record of this.listDate(date)) {
        out.push(record)
        if (out.length >= want) return out
      }
    }
    return out
  }

  /** Read-only facts for /health: never counts by scanning every record. */
  stats() {
    this.ensure()
    const dates = this.listDateKeys()
    return {
      dir: this.dir,
      legacy_path: this.legacyPath,
      days: dates.length,
      first_date: dates.length > 0 ? dates[dates.length - 1] : null,
      last_date: dates.length > 0 ? dates[0] : null,
      today: archiveDay(this.now()),
      today_rounds: this.listDate(archiveDay(this.now())).length,
      retention_days: this.retentionDays,
      migrated: this.migration,
    }
  }

  applyRetention() {
    if (this.retentionDays <= 0) return
    const cutoff = archiveDay(this.now() - this.retentionDays * DAY_MS)
    if (!cutoff) return
    for (const date of this.listDateKeys()) {
      if (date >= cutoff) continue
      try {
        fs.unlinkSync(this.fileOf(date))
        this.days.delete(date)
        this.dateKeys = null
      } catch (error) {
        this.logger?.warn?.(`[twb] cannot drop expired archive day ${date}: ${error?.message ?? error}`)
      }
    }
  }

  /**
   * One-time migration of the old single-file cache (15 §3.2). Idempotent: the
   * marker + per-roundId dedupe mean a half-finished run can simply be retried.
   * The old file is renamed, never deleted, and never appended to again.
   */
  migrateLegacy() {
    if (!this.legacyPath) return null
    const marker = path.join(this.dir, ARCHIVE_INDEX_FILE)
    try {
      if (fs.existsSync(marker)) return null
    } catch { /* treat unreadable marker as absent */ }
    let raw
    try {
      raw = fs.readFileSync(this.legacyPath, 'utf8')
    } catch (error) {
      if (error?.code !== 'ENOENT') this.logger?.warn?.(`[twb] cannot read legacy rounds: ${error?.message ?? error}`)
      return null
    }
    let rows = []
    try {
      const parsed = JSON.parse(raw)
      rows = Array.isArray(parsed) ? parsed : (Array.isArray(parsed?.rounds) ? parsed.rounds : [])
    } catch (error) {
      this.logger?.warn?.(`[twb] legacy rounds are not valid JSON, left untouched: ${error?.message ?? error}`)
      return null
    }
    const valid = rows.filter((row) => row && typeof row.roundId === 'string')
    const byDay = new Map()
    for (const row of valid) {
      const day = archiveDay(row.startedAt ?? row.finishedAt ?? this.now())
      if (!day) continue
      if (!byDay.has(day)) byDay.set(day, [])
      byDay.get(day).push(row)
    }
    fs.mkdirSync(this.dir, { recursive: true })
    for (const [day, list] of [...byDay.entries()].sort(([a], [b]) => a.localeCompare(b))) {
      const existing = new Set()
      let previous = ''
      try {
        previous = fs.readFileSync(this.fileOf(day), 'utf8')
      } catch { previous = '' }
      for (const line of previous.split('\n')) {
        const text = line.trim()
        if (!text) continue
        try {
          const row = JSON.parse(text)
          if (row?.roundId) existing.add(row.roundId)
        } catch { /* ignore */ }
      }
      const ordered = list
        .filter((row) => !existing.has(row.roundId))
        .sort((a, b) => (Date.parse(a?.startedAt ?? '') || 0) - (Date.parse(b?.startedAt ?? '') || 0))
      if (ordered.length === 0) continue
      fs.appendFileSync(this.fileOf(day), `${ordered.map((row) => JSON.stringify(row)).join('\n')}\n`, 'utf8')
    }
    const result = {
      contract: ROUND_ARCHIVE_CONTRACT,
      source: this.legacyPath,
      rounds: valid.length,
      days: byDay.size,
      migrated_at: new Date(this.now()).toISOString(),
    }
    try {
      fs.writeFileSync(marker, JSON.stringify(result, null, 2), 'utf8')
    } catch (error) {
      this.logger?.warn?.(`[twb] cannot write archive migration marker: ${error?.message ?? error}`)
    }
    const archived = this.legacyPath.endsWith('.json')
      ? this.legacyPath.slice(0, -'.json'.length) + '.legacy.json'
      : `${this.legacyPath}.legacy`
    try {
      fs.renameSync(this.legacyPath, archived)
    } catch (error) {
      this.logger?.warn?.(`[twb] cannot rename legacy rounds to ${archived}: ${error?.message ?? error}`)
    }
    this.days.clear()
    this.dateKeys = null
    this.logger?.warn?.(`[twb] migrated ${valid.length} legacy round(s) into ${byDay.size} day partition(s)`)
    return result
  }
}

function statMtime(file) {
  try {
    return fs.statSync(file).mtimeMs
  } catch {
    return 0
  }
}
