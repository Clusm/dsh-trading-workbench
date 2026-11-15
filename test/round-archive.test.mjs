/**
 * 15 §3: the round archive replaces the 100-row single-file cache.
 *
 * These tests pin the properties the rest of the Host relies on:
 *  - every round is kept (no cap, no eviction);
 *  - a day partition is the unit the UI queries, on the Asia/Shanghai calendar;
 *  - append-only updates converge to one record per round (running -> terminal);
 *  - the legacy single file is migrated once, idempotently, and never deleted;
 *  - a detail lookup spans partitions, so old rounds still open.
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import test from 'node:test'

import {
  RoundArchive,
  archiveDay,
  isArchiveDay,
  parseRoundDate,
} from '../host/round-archive.js'

// 2026-11-14 15:00Z == 2026-11-14 23:00 Shanghai
const NOW = Date.parse('2026-11-14T15:00:00Z')

function makeArchive(overrides = {}) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'twb-archive-'))
  let clock = overrides.nowAt ?? NOW
  const now = () => clock
  const legacyPath = path.join(dir, 'host-rounds.json')
  const archive = new RoundArchive({
    dir: path.join(dir, 'rounds'),
    legacyPath,
    now,
    ...overrides,
    now: () => clock,
  })
  return {
    dir,
    legacyPath,
    archive,
    setNow(value) { clock = value },
    roundsDir: path.join(dir, 'rounds'),
  }
}

function record(roundId, startedAt, extra = {}) {
  return { roundId, cycleId: roundId, startedAt, status: 'done', outcome: 'no_trade', ...extra }
}

test('archiveDay uses the Asia/Shanghai calendar (no UTC day leak)', () => {
  // 23:59 Shanghai is still the same day; 00:01 Shanghai is the next one.
  assert.equal(archiveDay('2026-11-14T15:59:00Z'), '2026-11-14')
  assert.equal(archiveDay('2026-11-14T16:01:00Z'), '2026-11-15')
  assert.equal(archiveDay('2026-11-14T00:00:00Z'), '2026-11-14')
  assert.equal(archiveDay('not-a-date'), null)
  assert.equal(isArchiveDay('2026-11-14'), true)
  assert.equal(isArchiveDay('2026-13-14'), false)
  assert.equal(isArchiveDay('2026-2-3'), false)
})

test('parseRoundDate defaults to today and rejects bad or future days', () => {
  assert.equal(parseRoundDate(null, { now: NOW }), '2026-11-14')
  assert.equal(parseRoundDate('', { now: NOW }), '2026-11-14')
  assert.equal(parseRoundDate('2026-11-13', { now: NOW }), '2026-11-13')
  assert.throws(() => parseRoundDate('2026-11-15', { now: NOW }), (error) => error.code === 'E_DATE_FUTURE')
  assert.throws(() => parseRoundDate('2026-02-30', { now: NOW }), (error) => error.code === 'E_DATE_INVALID')
  assert.throws(() => parseRoundDate('yesterday', { now: NOW }), (error) => error.code === 'E_DATE_INVALID')
})

test('every round is kept (there is no 100-row cap any more)', () => {
  const env = makeArchive()
  // 150 rounds on one day: the old store would have dropped the oldest 50.
  for (let i = 0; i < 150; i += 1) {
    env.archive.upsert(record(`r-${i}`, new Date(NOW - i * 1_000).toISOString()))
  }
  const day = env.archive.listDate('2026-11-14')
  assert.equal(day.length, 150)
  assert.equal(day[0].roundId, 'r-0', 'newest first')
  assert.equal(day[149].roundId, 'r-149')
  assert.equal(env.archive.listRecent(1)[0].roundId, 'r-0')
})

test('an append-only update converges to the last version of a round', () => {
  const env = makeArchive()
  const startedAt = new Date(NOW).toISOString()
  env.archive.upsert(record('r-live', startedAt, { status: 'running', outcome: null }))
  env.archive.upsert(record('r-live', startedAt, { status: 'failed', outcome: 'error', reasonCode: 'LLM_TIMEOUT' }))
  const day = env.archive.listDate('2026-11-14')
  assert.equal(day.length, 1, 'one record per round, not two')
  assert.equal(day[0].status, 'failed')
  assert.equal(day[0].reasonCode, 'LLM_TIMEOUT')
})

test('a day partition is what the date query reads, across midnight', () => {
  const env = makeArchive()
  env.archive.upsert(record('r-night', '2026-11-14T15:59:00Z')) // 23:59 Shanghai
  env.setNow(Date.parse('2026-11-14T16:30:00Z')) // 00:30 Shanghai, next day
  env.archive.upsert(record('r-morning', new Date(env.archive.now()).toISOString()))

  assert.deepEqual(env.archive.listDateKeys(), ['2026-11-15', '2026-11-14'])
  assert.deepEqual(env.archive.listDate('2026-11-14').map((row) => row.roundId), ['r-night'])
  assert.deepEqual(env.archive.listDate('2026-11-15').map((row) => row.roundId), ['r-morning'])
  // "newest anywhere" still spans the boundary for last_round / route snapshot.
  assert.deepEqual(env.archive.listRecent(2).map((row) => row.roundId), ['r-morning', 'r-night'])
})

test('a detail lookup finds an old round in its own partition', () => {
  const env = makeArchive()
  env.archive.upsert(record('r-13', '2026-11-13T02:00:00Z'))
  env.archive.upsert(record('r-14', '2026-11-14T02:00:00Z'))
  assert.equal(env.archive.find('r-13').roundId, 'r-13')
  assert.equal(env.archive.find('r-14').roundId, 'r-14')
  assert.equal(env.archive.find('r-missing'), null)
})

test('unreadable lines are skipped instead of breaking the day', () => {
  const env = makeArchive()
  env.archive.upsert(record('r-good', new Date(NOW).toISOString()))
  fs.appendFileSync(path.join(env.roundsDir, '2026-11-14.jsonl'), '{"roundId":"broken"\n', 'utf8')
  const day = env.archive.listDate('2026-11-14')
  assert.deepEqual(day.map((row) => row.roundId), ['r-good'])
})

test('the legacy single file is migrated once and kept for audit', () => {
  const env = makeArchive()
  fs.writeFileSync(env.legacyPath, JSON.stringify({
    contract: 'trading-workbench/host-rounds/1',
    rounds: [
      record('legacy-13', '2026-11-13T02:00:00Z'),
      record('legacy-14', '2026-11-14T02:00:00Z'),
      record('legacy-14b', '2026-11-14T03:00:00Z'),
    ],
  }), 'utf8')

  const stats = env.archive.stats()
  assert.equal(stats.migrated.rounds, 3)
  assert.equal(stats.migrated.days, 2)
  assert.equal(env.archive.listDate('2026-11-14').length, 2)
  assert.equal(env.archive.listDate('2026-11-13').length, 1)
  assert.equal(fs.existsSync(env.legacyPath), false, 'old file renamed, not deleted')
  assert.equal(fs.existsSync(path.join(env.dir, 'host-rounds.legacy.json')), true)

  // Idempotent: a second archive over the same directory never duplicates lines.
  const again = new RoundArchive({ dir: env.roundsDir, legacyPath: env.legacyPath, now: () => NOW })
  assert.equal(again.stats().migrated, null)
  assert.equal(again.listDate('2026-11-14').length, 2)
})

test('retention is off by default and only drops whole expired days when set', () => {
  const env = makeArchive()
  env.archive.upsert(record('r-old', '2026-11-01T02:00:00Z'))
  env.archive.upsert(record('r-new', '2026-11-14T02:00:00Z'))
  assert.equal(env.archive.stats().retention_days, 0)
  assert.deepEqual(env.archive.listDateKeys(), ['2026-11-14', '2026-11-01'])

  const trimmed = makeArchive({ retentionDays: 3 })
  trimmed.archive.upsert(record('r-old', '2026-11-01T02:00:00Z'))
  trimmed.archive.upsert(record('r-new', '2026-11-14T02:00:00Z'))
  assert.deepEqual(trimmed.archive.listDateKeys(), ['2026-11-14'])
})
