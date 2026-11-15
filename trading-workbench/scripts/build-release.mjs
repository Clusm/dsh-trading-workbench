#!/usr/bin/env node
/**
 * Build the packaged source release identity for the plugin (F1 / §5.1).
 *
 *   node trading-workbench/scripts/build-release.mjs            # write plugin/.release.json
 *   node trading-workbench/scripts/build-release.mjs --check    # fail if the manifest is stale
 *   node trading-workbench/scripts/build-release.mjs --dry-run  # print, write nothing
 *
 * The manifest is the deployment artifact: it is what the *running* process
 * reports (build.js reads it once at load) and what verify-runtime.mjs compares
 * against a fresh digest of the checkout. Editing sources after this runs makes
 * the manifest stale on purpose — that is the difference between "built" and
 * "loaded" that §5.1 asks for.
 */

import { mkdirSync, writeFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { computeRelease, readManifest } from './lib/release.mjs'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const PLUGIN_ROOT = path.resolve(HERE, '..', 'plugin')
const MANIFEST_PATH = path.join(HERE, '.release', 'plugin-release.json')

const args = new Set(process.argv.slice(2))
const check = args.has('--check')
const dryRun = args.has('--dry-run')

const release = computeRelease({ root: PLUGIN_ROOT })

if (check) {
  const existing = readManifest(MANIFEST_PATH)
  if (!existing) {
    console.error('release manifest is missing: ' + MANIFEST_PATH)
    console.error('run: node trading-workbench/scripts/build-release.mjs')
    process.exit(1)
  }
  if (existing.source_digest !== release.source_digest) {
    console.error('release manifest is stale')
    console.error('  manifest: ' + String(existing.release_id) + ' ' + String(existing.source_digest))
    console.error('  checkout: ' + release.release_id + ' ' + release.source_digest)
    process.exit(1)
  }
  console.log('release manifest is current: ' + release.release_id + ' (' + release.file_count + ' files)')
  process.exit(0)
}

const manifest = {
  contract: release.contract,
  release_id: release.release_id,
  source_digest: release.source_digest,
  plugin_version: release.plugin_version,
  file_count: release.file_count,
  generated_at: new Date().toISOString(),
  files: release.files,
}

console.log('release_id       ' + manifest.release_id)
console.log('source_digest    ' + manifest.source_digest)
console.log('files            ' + manifest.file_count)
if (dryRun) {
  console.log('dry run: ' + MANIFEST_PATH + ' was not written')
  process.exit(0)
}
mkdirSync(path.dirname(MANIFEST_PATH), { recursive: true })
writeFileSync(MANIFEST_PATH, JSON.stringify(manifest, null, 2) + '\n', 'utf8')
console.log('wrote            ' + MANIFEST_PATH)
