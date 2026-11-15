#!/usr/bin/env node
/**
 * F1 evidence shots: render the real client bundle at 1024 (and 1440) against
 * Host-shaped fixtures, then read back layout metrics from the rendered page.
 *
 *   node trading-workbench/scripts/ui-shots-t05.mjs [--out DIR]
 *
 * It reuses plugin/client/screens/harness.html (fixtures + tiny DOM runtime) and
 * only injects the F1 state (automation/stop, release identity). The metrics
 * are printed as JSON so "1024 不横向溢出 + 状态/停止按钮可见" is measured, not
 * eyeballed.
 *
 * Requires Google Chrome at the default Windows path (headless).
 */

import { readFileSync } from 'node:fs'
import { spawn } from 'node:child_process'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..')
const HARNESS = path.join(ROOT, 'plugin', 'client', 'screens', 'harness.html')
const CLIENT = path.join(ROOT, 'plugin', 'client.js')
const CHROME = 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'

function arg(name, fallback) {
  const index = process.argv.indexOf(name)
  return index >= 0 && process.argv[index + 1] ? process.argv[index + 1] : fallback
}
const OUT_DIR = path.resolve(arg('--out', path.join(ROOT, 'docs', 'live-mode', 'evidence')))

const NOW = Date.now()
const iso = (offset) => new Date(NOW - offset).toISOString()
const HOST_BLOCK = {
  data_dir: 'C:\\Users\\Tao\\Desktop\\币安\\trading-workbench\\data',
  service_base_url: 'http://127.0.0.1:8791',
  environment: { venue: 'demo', demo_only: true, source: 'service-guard' },
  release: { release_id: 'twb-0.1.0-c44db84a30cd', source: 'manifest', source_digest: 'sha256:c44db84a30cdbeba753214f27bb5ad3708bc0abb35256f938b2651f628d7b928' },
  loaded_at: iso(3 * 3600 * 1000),
}
const BUILD_BLOCK = {
  buildId: 'twb-20261113T164633Z-2d449fc4',
  configRevision: 2,
  chain_version: 'ai-chain/1',
  loaded_at: iso(3 * 3600 * 1000),
  prompt_set: 'prompts/2d449fc4',
  release_id: 'twb-0.1.0-c44db84a30cd',
  release_source: 'manifest',
  release_source_digest: 'sha256:c44db84a30cdbeba753214f27bb5ad3708bc0abb35256f938b2651f628d7b928',
  plugin_version: '0.1.0',
}

function automationBlock(overrides = {}) {
  return {
    contract: 'trading-workbench/automation/1',
    state: 'running',
    reason_code: null,
    scheduler_enabled: true,
    scheduler_supported: true,
    last_success_at: iso(7 * 60 * 1000),
    next_at: new Date(NOW + 3 * 60 * 1000).toISOString(),
    interval_ms: 600000,
    last_result: { at: iso(7 * 60 * 1000), kind: 'round', code: 'NO_CANDIDATE' },
    service_control: { state: 'normal', reason: 'default', updated_at: iso(3600 * 1000), updated_by: 'operator', confirmed: true },
    stop: null,
    ...overrides,
  }
}

const CASES = [
  {
    name: 't05-1024-running',
    width: 1024,
    automation: automationBlock(),
    control: 'normal',
  },
  {
    name: 't05-1024-stopped',
    width: 1024,
    automation: automationBlock({
      state: 'stopped',
      reason_code: 'SERVICE_CONTROL_PAUSE',
      scheduler_enabled: false,
      next_at: null,
      service_control: {
        state: 'pause',
        reason: '顶栏停止自动交易',
        updated_at: iso(60 * 1000),
        updated_by: 'operator',
        confirmed: true,
      },
      stop: { request_id: 'req-stop-demo', phase: 'stopped', requested_at: iso(60 * 1000), confirmed_at: iso(60 * 1000) },
    }),
    control: 'pause',
  },
  {
    name: 't05-1024-pending',
    width: 1024,
    automation: automationBlock({
      state: 'pending',
      reason_code: 'SERVICE_UNREACHABLE',
      scheduler_enabled: false,
      next_at: null,
      service_control: { state: 'unknown', confirmed: false },
      stop: { request_id: 'req-stop-demo2', phase: 'pending_confirmation', requested_at: iso(30 * 1000), error: { code: 'E_SERVICE_OFFLINE' } },
    }),
    control: 'unknown',
    serviceDown: true,
  },
  {
    name: 't05-1440-running',
    width: 1440,
    automation: automationBlock(),
    control: 'normal',
  },
]

const METRICS = `
<script>
  setTimeout(function () {
    var root = document.querySelector('.twb-root')
    var s0 = document.querySelector('.twb-s0')
    var canvas = document.querySelector('.twb-canvas')
    var chip = document.querySelector('[data-auto-state]')
    var stop = document.querySelector('[data-auto-action]')
    var rect = function (node) {
      if (!node) return null
      var r = node.getBoundingClientRect()
      return { x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height), right: Math.round(r.right) }
    }
    var out = {
      inner: { w: window.innerWidth, h: window.innerHeight },
      doc: { client: document.documentElement.clientWidth, scroll: document.documentElement.scrollWidth },
      root: rect(root),
      header: s0 ? { client: s0.clientWidth, scroll: s0.scrollWidth } : null,
      canvas: canvas ? { client: canvas.clientWidth, scroll: canvas.scrollWidth } : null,
      autoChip: chip ? { state: chip.getAttribute('data-auto-state'), text: chip.textContent, rect: rect(chip) } : null,
      autoAction: stop ? { action: stop.getAttribute('data-auto-action'), text: stop.textContent, rect: rect(stop) } : null,
      positionsVisible: !!document.querySelector('.twb-table'),
      bodyText: (document.body.innerText || '').slice(0, 400),
    }
    var pre = document.createElement('pre')
    pre.id = 'twb-metrics'
    pre.textContent = JSON.stringify(out)
    document.body.appendChild(pre)
  }, 2500)
</script>
`

function render(caseSpec) {
  let html = readFileSync(HARNESS, 'utf8')
  // Serve everything over loopback HTTP: Chrome headless refuses to load a
  // file:// subresource from a temp file:// document.
  html = html.replace('<script src="../../client.js"></script>', '<script src="/client.js"></script>')
  const injection = `
  <script>
    (function () {
      STATE.build = ${JSON.stringify(BUILD_BLOCK)}
      STATE.host = ${JSON.stringify(HOST_BLOCK)}
      STATE.automation = ${JSON.stringify(caseSpec.automation)}
      STATE.service = Object.assign({}, STATE.service, { control_state: '${caseSpec.control}' }${caseSpec.serviceDown ? ", { ready: false, error_code: 'E_SERVICE_OFFLINE' }".replace(/^, /, ', ') : ''})
      STATE.positions = STATE.positions || []
    })()
  </script>
  `
  html = html.replace('<script src="/client.js"></script>', injection + '<script src="/client.js"></script>')
  html = html.replace('</body>', METRICS + '</body>')
  return html
}

/**
 * Async on purpose: the fixture server runs in THIS process, so a blocking
 * spawn would deadlock against Chrome's own requests for /client.js.
 */
function runChrome(args) {
  return new Promise((resolve) => {
    const child = spawn(CHROME, [
      '--headless=new',
      '--disable-gpu',
      '--hide-scrollbars',
      '--no-first-run',
      '--virtual-time-budget=6000',
      ...args,
    ], { stdio: ['ignore', 'pipe', 'pipe'] })
    let stdout = ''
    let stderr = ''
    child.stdout.on('data', (chunk) => { stdout += String(chunk) })
    child.stderr.on('data', (chunk) => { stderr += String(chunk) })
    child.on('close', (code) => resolve({ status: code, stdout, stderr }))
  })
}

const pages = new Map(CASES.map((caseSpec) => ['/' + caseSpec.name, render(caseSpec)]))
const clientSource = readFileSync(CLIENT, 'utf8')
const server = http.createServer((req, res) => {
  const pathname = String(req.url ?? '/').split('?')[0]
  if (pathname === '/client.js') {
    res.writeHead(200, { 'content-type': 'text/javascript; charset=utf-8', 'cache-control': 'no-store' })
    res.end(clientSource)
    return
  }
  const html = pages.get(pathname)
  if (!html) {
    res.writeHead(404, { 'content-type': 'text/plain' })
    res.end('not found')
    return
  }
  res.writeHead(200, { 'content-type': 'text/html; charset=utf-8', 'cache-control': 'no-store' })
  res.end(html)
})
await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
const port = server.address().port

const report = []
let failed = 0
for (const caseSpec of CASES) {
  const url = 'http://127.0.0.1:' + port + '/' + caseSpec.name
  console.log('render ' + caseSpec.name + ' -> ' + url)
  const png = path.join(OUT_DIR, caseSpec.name + '.png')
  const shot = await runChrome(['--window-size=' + caseSpec.width + ',860', '--screenshot=' + png, url])
  const dump = await runChrome(['--window-size=' + caseSpec.width + ',860', '--dump-dom', url])
  const match = String(dump.stdout ?? '').match(/<pre id="twb-metrics">([\s\S]*?)<\/pre>/)
  let metrics = null
  if (match) {
    try { metrics = JSON.parse(match[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&')) } catch (error) { metrics = null }
  }
  const overflow = metrics ? metrics.doc.scroll > metrics.doc.client + 1 : null
  const stopVisible = metrics?.autoAction ? metrics.autoAction.rect.right <= metrics.inner.w + 1 : null
  report.push({ case: caseSpec.name, width: caseSpec.width, screenshot: png, exit: shot.status, overflow, stopVisible, metrics })
  if (!metrics || overflow !== false || stopVisible !== true) failed += 1
}

if (typeof server.closeAllConnections === 'function') server.closeAllConnections()
server.close()
console.log(JSON.stringify({ out_dir: OUT_DIR, cases: report }, null, 2))
if (failed > 0) {
  console.error('\n' + failed + ' case(s) failed the layout assertions')
  process.exit(1)
}
console.log('\nAll ' + report.length + ' cases rendered without horizontal overflow and with the stop action visible.')
// Chrome's helper processes can hold the event loop; the report is complete.
process.exit(0)
