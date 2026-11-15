#!/usr/bin/env node
/**
 * Build a distributable DSH bundle directory for the trading workbench.
 *
 *   node trading-workbench/scripts/build-bundle.mjs
 *   node trading-workbench/scripts/build-bundle.mjs --name dsh-trading-workbench --out dist
 *   node trading-workbench/scripts/build-bundle.mjs --tarball        # also run `npm pack`
 *
 * Why a separate step instead of publishing `plugin/` directly:
 *  - the runtime needs the Python service next to the Host half, and the service
 *    lives in `trading-workbench/service/` (a sibling of `plugin/`);
 *  - a published package must not carry tests, logs, `__pycache__` or a venv;
 *  - the bundle patch has to name the *installed* package, not `@local/...`.
 *
 * Output (default `trading-workbench/dist/<name>/`):
 *   index.js  client.js  host/**  locale/**  icon.svg  cordis.patch.yml
 *   service/**            (Python source + requirements.txt, no artefacts)
 *   package.json          (publish-ready manifest: dsh.bundle + dsh.client)
 *   README.md             (copied from docs/bundle-README.md when present)
 */

import { cpSync, existsSync, mkdirSync, readFileSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..')          // trading-workbench/
const PLUGIN = path.join(ROOT, 'plugin')
const SERVICE = path.join(ROOT, 'service')

const args = process.argv.slice(2)
const argValue = (flag, fallback) => {
  const index = args.indexOf(flag)
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback
}
const pluginPkg = JSON.parse(readFileSync(path.join(PLUGIN, 'package.json'), 'utf8'))
const bundleName = argValue('--name', pluginPkg.name.replace(/^@[^/]+\//, ''))
const outRoot = path.resolve(ROOT, argValue('--out', 'dist'))
const target = path.join(outRoot, bundleName)

// Runtime only: tests, screenshots and the probe scripts stay in the repo.
const PLUGIN_FILES = ['index.js', 'client.js', 'host', 'locale', 'icon.svg', 'cordis.patch.yml']
const SKIP_DIRS = new Set(['__pycache__', '.pytest_cache', '.venv', 'node_modules', 'screens', 'test'])
// service/tests_* are dev suites; never ship them.
const SKIP_PREFIX = ['tests_', 'test_']
const SKIP_SUFFIX = ['.pyc', '.log', '.tmp']

function copyTree(from, to) {
  mkdirSync(to, { recursive: true })
  for (const entry of readdirSync(from, { withFileTypes: true })) {
    const source = path.join(from, entry.name)
    const dest = path.join(to, entry.name)
    if (entry.isDirectory()) {
      if (SKIP_DIRS.has(entry.name) || SKIP_PREFIX.some((prefix) => entry.name.startsWith(prefix))) continue
      copyTree(source, dest)
      continue
    }
    if (!entry.isFile()) continue
    if (SKIP_SUFFIX.some((suffix) => entry.name.endsWith(suffix))) continue
    cpSync(source, dest)
  }
}

function dirSize(dir) {
  let total = 0
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) total += dirSize(full)
    else if (entry.isFile()) total += statSync(full).size
  }
  return total
}

// ---- 1. runtime files -------------------------------------------------------
rmSync(target, { recursive: true, force: true })
mkdirSync(target, { recursive: true })
for (const rel of PLUGIN_FILES) {
  const source = path.join(PLUGIN, rel)
  if (!existsSync(source)) throw new Error(`missing plugin file: ${rel}`)
  if (statSync(source).isDirectory()) copyTree(source, path.join(target, rel))
  else cpSync(source, path.join(target, rel))
}

// ---- 2. the Python service (source only, no venv/__pycache__/logs) ---------
copyTree(SERVICE, path.join(target, 'service'))

// ---- 3. patch must name the published package ------------------------------
const patchPath = path.join(target, 'cordis.patch.yml')
const patch = readFileSync(patchPath, 'utf8')
  .replace(/(\n\s+name:\s*)'[^']*'/, `$1'${bundleName}'`)
  .replace(/(\n\s+name:\s*)"[^"]*"/, `$1"${bundleName}"`)
writeFileSync(patchPath, patch, 'utf8')
if (!patch.includes(bundleName)) throw new Error('cordis.patch.yml still does not name the bundle')

// ---- 4. publish-ready manifest --------------------------------------------
const manifest = {
  name: bundleName,
  version: pluginPkg.version,
  description: 'Trading workbench for DeepSeek Harness: AI decision chain, per-day round archive and Demo trading over a local service.',
  type: 'module',
  main: 'index.js',
  exports: {
    '.': './index.js',
    './client': './client.js',
    './cordis.patch.yml': './cordis.patch.yml',
    './package.json': './package.json',
  },
  files: [
    'index.js', 'client.js', 'host', 'locale', 'icon.svg',
    'cordis.patch.yml', 'service', 'README.md', 'DISTRIBUTION.md',
  ],
  engines: { node: '>=22.19', ...(pluginPkg.engines && pluginPkg.engines.dsh ? { dsh: pluginPkg.engines.dsh } : {}) },
  keywords: ['dsh', 'dsh-plugin', 'deepseek-harness', 'trading', 'workbench'],
  peerDependencies: {
    '@deepseek-ai/dsh-client-locale': '*',
    '@deepseek-ai/dsh-client-ui-layout': '*',
    '@deepseek-ai/dsh-client-ui-sidebar': '*',
    react: '^18.2.0',
  },
  peerDependenciesMeta: {
    '@deepseek-ai/dsh-client-locale': { optional: true },
    '@deepseek-ai/dsh-client-ui-layout': { optional: true },
    '@deepseek-ai/dsh-client-ui-sidebar': { optional: true },
    react: { optional: true },
  },
  dsh: pluginPkg.dsh,
}
writeFileSync(path.join(target, 'package.json'), `${JSON.stringify(manifest, null, 2)}\n`, 'utf8')

// ---- 5. docs ---------------------------------------------------------------
for (const doc of ['README.md', 'DISTRIBUTION.md']) {
  const source = path.join(ROOT, 'docs', `bundle-${doc}`)
  if (existsSync(source)) cpSync(source, path.join(target, doc))
}

const size = dirSize(target)
console.log(`bundle: ${target}`)
console.log(`  files: ${manifest.files.join(', ')}`)
console.log(`  size : ${(size / 1024 / 1024).toFixed(2)} MB unpacked`)
console.log('  next : cd "' + outRoot + '" && npm pack ./' + bundleName)

if (args.includes('--tarball')) {
  const { spawnSync } = await import('node:child_process')
  const run = spawnSync('npm pack ./' + bundleName, { cwd: outRoot, stdio: 'inherit', shell: true })
  process.exit(run.status ?? 1)
}
