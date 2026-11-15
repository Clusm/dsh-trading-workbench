/**
 * Source release identity for the trading-workbench plugin (F1 / §5.1 / §8·F).
 *
 * Why this exists: buildId already mixes the load time with a prompt digest, so
 * it cannot prove that the files on disk are the files the process is running.
 * A packaged release digest can: build.js captures it once at load, reports it
 * through /health-equivalent routes, and scripts/verify-runtime.mjs recomputes
 * it from disk to prove "what is loaded" == "what was built".
 *
 * No dependencies; Windows/POSIX safe; the digest is content-only (line endings
 * normalised to LF) so a CRLF checkout does not change the release id.
 */

import { createHash } from 'node:crypto'
import { readFileSync, readdirSync, statSync } from 'node:fs'
import path from 'node:path'

export const RELEASE_CONTRACT = 'trading-workbench/release/1'

/** Shipped source files, by explicit location (never a recursive guess). */
const SOURCE_ROOTS = [
  { dir: '', files: ['index.js', 'client.js', 'package.json', 'cordis.patch.yml', 'icon.svg'] },
  { dir: 'host', files: null },
  { dir: 'host/ai', files: null },
  { dir: 'locale', files: null },
  { dir: 'client', files: null },
]

function listFiles(pluginRoot) {
  const out = []
  for (const root of SOURCE_ROOTS) {
    const absolute = path.join(pluginRoot, root.dir)
    let names
    if (root.files) {
      names = root.files.map((name) => ({ name, dir: false }))
    } else {
      names = readdirSync(absolute, { withFileTypes: true })
        .filter((entry) => entry.isFile())
        .map((entry) => ({ name: entry.name, dir: false }))
    }
    for (const entry of names) {
      const relative = path.posix.join(root.dir, entry.name)
      if (!/\.(js|mjs|json|yml|svg|md)$/i.test(relative)) continue
      // The test harness and runtime artifacts are not part of the loadable
      // plugin surface, and README files only change the digest noise.
      if (relative.startsWith('client/screens/')) continue
      if (relative.endsWith('.log')) continue
      out.push(relative)
    }
  }
  return [...new Set(out)].sort()
}

export function hashFile(file) {
  const raw = readFileSync(file)
  const text = raw.toString('utf8').replace(/\r\n/g, '\n')
  return createHash('sha256').update(text, 'utf8').digest('hex')
}

/**
 * Compute the release identity of a plugin checkout.
 * @param {{ root?: string }} options plugin root (…/trading-workbench/plugin)
 */
export function computeRelease({ root } = {}) {
  if (!root) throw new Error('computeRelease requires { root }')
  const pluginRoot = path.resolve(root)
  const files = listFiles(pluginRoot).map((relative) => {
    const absolute = path.join(pluginRoot, relative)
    const stat = statSync(absolute)
    return { path: relative, sha256: hashFile(absolute), bytes: stat.size }
  })
  const digest = createHash('sha256')
  for (const file of files) digest.update(file.path).update('\0').update(file.sha256).update('\n')
  const sourceDigest = 'sha256:' + digest.digest('hex')
  let pluginVersion = '0.0.0'
  try {
    const pkg = JSON.parse(readFileSync(path.join(pluginRoot, 'package.json'), 'utf8'))
    if (typeof pkg?.version === 'string' && pkg.version) pluginVersion = pkg.version
  } catch (error) { /* the version is a label, not the identity */ }
  return {
    contract: RELEASE_CONTRACT,
    release_id: 'twb-' + pluginVersion + '-' + sourceDigest.slice(7, 19),
    source_digest: sourceDigest,
    plugin_version: pluginVersion,
    file_count: files.length,
    files,
  }
}

export function readManifest(manifestPath) {
  try {
    const raw = JSON.parse(readFileSync(manifestPath, 'utf8'))
    return raw && typeof raw === 'object' ? raw : null
  } catch (error) {
    return null
  }
}
