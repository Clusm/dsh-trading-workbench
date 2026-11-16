import test from 'node:test'
import assert from 'node:assert/strict'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { admittedWebRoutes } from '../host/http-admission.js'
import { apply, inject } from '../index.js'
import { httpFetch, startServer } from './http-fixture.mjs'

function response() {
  return {
    status: null, body: '',
    writeHead(status) { this.status = status },
    end(body) { this.body = body },
  }
}

test('admission rejects before invoking a route; failures never leak gate details', async () => {
  let route
  let invoked = 0
  const ctx = { webServer: { register(value) { route = value; return () => {} } } }
  admittedWebRoutes(ctx).register({ kind: 'prefix', path: '/test', handler() { invoked++ } })
  assert.equal(route.kind, 'prefix')
  for (const connection of [undefined, {}, { requestRejection() { throw new Error('secret') } },
    { requestRejection() { return null } }]) {
    ctx.connection = connection
    const res = response()
    await route.handler({}, res)
    assert.equal(res.status, 503)
    assert.ok(!res.body.includes('secret'))
  }
  assert.equal(invoked, 0)
  for (const status of [401, 403]) {
    ctx.connection = { requestRejection() { return status } }
    const res = response()
    await route.handler({}, res)
    assert.equal(res.status, status)
  }
  ctx.connection = { requestRejection() { assert.equal(this, ctx.connection); return undefined } }
  await route.handler({}, response())
  assert.equal(invoked, 1)
})

test('all registered plugin routes enforce Host/Origin/login admission before side effects', async () => {
  assert.ok(inject.includes('connection'))
  const routes = []
  const effects = []
  let modelCalls = 0
  const dataDir = mkdtempSync(path.join(tmpdir(), 'twb-admission-'))
  const ctx = {
    webServer: { register(route) { routes.push(route); return () => {} }, getOrigin() { return 'http://localhost' } },
    connection: {
      requestRejection(req) {
        if (req.headers.host !== 'localhost') return 403
        if (req.headers.origin && req.headers.origin !== 'http://localhost') return 403
        if (req.headers.authorization !== 'Bearer test-session') return 401
        return undefined
      },
    },
    agentDefaultModel: { currentSelection() { return null } },
    llm: { stream() { modelCalls++; throw new Error('must not run') } },
    effect(fn) { effects.push(fn()) },
  }
  let server
  try {
    apply(ctx, { dataDir, autoSchedule: { enabled: false } })
    assert.ok(routes.length > 10, 'includes integration routes as well as entry routes')
    // Every method on every registration must be rejected before body parsing,
    // service calls, credentials/config writes, model calls or schedule changes.
    for (const route of routes) {
      for (const method of ['GET', 'POST']) {
        for (const headers of [{ host: 'evil.example' }, { host: 'localhost', origin: 'https://evil.example' }, { host: 'localhost' }]) {
          const res = response()
          await route.handler({ method, headers, socket: { remoteAddress: '127.0.0.1' } }, res)
          assert.equal(res.status, headers.host !== 'localhost' || headers.origin ? 403 : 401, route.path)
        }
      }
    }
    assert.equal(modelCalls, 0)
    server = await startServer((req, res) => {
      const route = routes.find((item) => item.kind === 'exact' && item.path === new URL(req.url, 'http://localhost').pathname)
      if (!route) { res.writeHead(404); res.end(); return }
      Promise.resolve(route.handler(req, res)).catch(() => { res.writeHead(500); res.end() })
    })
    const headers = { host: 'localhost', authorization: 'Bearer test-session' }
    const health = await httpFetch(server.url + '/trading-workbench/api/health', { headers })
    assert.equal(health.status, 200)
    assert.equal((await health.json()).ok, true)
    const schedule = await httpFetch(server.url + '/trading-workbench/api/schedule', { headers })
    assert.equal(schedule.status, 200)
    assert.equal((await schedule.json()).schedule.enabled, false)
    ctx.connection = undefined
    assert.equal((await httpFetch(server.url + '/trading-workbench/api/health', { headers })).status, 503)
  } finally {
    for (const dispose of effects.reverse()) if (typeof dispose === 'function') dispose()
    if (server) await server.close()
    rmSync(dataDir, { recursive: true, force: true })
  }
})
