#!/usr/bin/env node
/**
 * Copy the workbench project into the git worktree for publication.
 * One-off helper (not shipped): keeps runtime data, venvs and build output out.
 */
import { cpSync, existsSync, mkdirSync, readdirSync, rmSync, statSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const SOURCE = path.resolve(HERE, '..')
const TARGET = path.resolve(HERE, '..', '..', 'multi-agent-trader', 'trading-workbench')

const SKIP_DIRS = new Set(['node_modules', '.venv', '__pycache__', '.pytest_cache', 'data', 'dist', 'screens', 'backups', 'evidence', 'reviews', 'logs', '.codegraph', '.agent-teams', 'archive', 'data-t01-baseline', 'data-t10-copy'])
const SKIP_FILES = [/\.log$/i, /\.sqlite/i, /\.tmp$/i, /^host-rounds.*\.json$/i, /^\.state-probe/i, /^\.release$/i]

function copy(from, to) {
  mkdirSync(to, { recursive: true })
  for (const entry of readdirSync(from, { withFileTypes: true })) {
    const src = path.join(from, entry.name)
    const dst = path.join(to, entry.name)
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name)) continue
      copy(src, dst)
      continue
    }
    if (!entry.isFile()) continue
    if (SKIP_FILES.some((re) => re.test(entry.name))) continue
    cpSync(src, dst)
  }
}

if (!existsSync(SOURCE)) throw new Error(`missing source: ${SOURCE}`)
rmSync(TARGET, { recursive: true, force: true })
copy(SOURCE, TARGET)

// Public repo keeps the design docs (markdown) and the README screenshots only:
// review/evidence directories are working material, not documentation.
const DOCS = path.join(TARGET, 'docs')
if (existsSync(DOCS)) {
  for (const entry of readdirSync(DOCS, { withFileTypes: true })) {
    if (entry.isDirectory() && entry.name !== 'screenshots') rmSync(path.join(DOCS, entry.name), { recursive: true, force: true })
  }
}

let files = 0
let bytes = 0
const walk = (dir) => {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) walk(full)
    else if (entry.isFile()) { files += 1; bytes += statSync(full).size }
  }
}
walk(TARGET)
console.log(`copied ${files} files, ${(bytes / 1024 / 1024).toFixed(2)} MB -> ${TARGET}`)
