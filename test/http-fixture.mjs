/**
 * Test-side HTTP client for the Host loopback routes.
 *
 * Why this exists (t30 / 11-验证报告-第二批): Node v24.14.1 on Windows keeps an
 * undici async handle while a keep-alive socket closes. With
 * `node --test --test-force-exit`, the runner tears the process down during that
 * close and libuv hits `Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)`
 * (src/win/async.c:76) — a file-level failure even though every case passed.
 * Reproduced with zero repo code: server + 2x global fetch + force-exit.
 *
 * These helpers use node:http with `agent: false` (one connection per request,
 * `Connection: close`) and force `closeAllConnections()` before `server.close()`,
 * so no async handle is still closing when the process exits.
 */
import http from 'node:http'

/** fetch-shaped GET/POST against 127.0.0.1 (no undici, no keep-alive pool). */
export function httpFetch(url, options = {}) {
  const target = new URL(url)
  const method = String(options.method ?? 'GET').toUpperCase()
  const headers = { ...(options.headers ?? {}), connection: 'close' }
  let payload = options.body
  if (payload !== undefined && payload !== null && typeof payload !== 'string' && !Buffer.isBuffer(payload)) {
    payload = JSON.stringify(payload)
    if (!Object.keys(headers).some((key) => key.toLowerCase() === 'content-type')) {
      headers['content-type'] = 'application/json'
    }
  }
  return new Promise((resolve, reject) => {
    const request = http.request({
      hostname: target.hostname,
      port: target.port,
      path: `${target.pathname}${target.search}`,
      method,
      headers,
      agent: false,
    }, (response) => {
      const chunks = []
      response.on('data', (chunk) => chunks.push(chunk))
      response.on('end', () => {
        const text = Buffer.concat(chunks).toString('utf8')
        resolve({
          status: response.statusCode,
          ok: response.statusCode >= 200 && response.statusCode < 300,
          headers: response.headers,
          text: async () => text,
          json: async () => (text ? JSON.parse(text) : null),
        })
      })
    })
    request.on('error', reject)
    if (payload !== undefined) request.write(payload)
    request.end()
  })
}

/** Start a Host handler on an ephemeral port; close() drains connections first. */
export async function startServer(handler) {
  const server = http.createServer(handler)
  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve))
  const { port } = server.address()
  return {
    server,
    port,
    url: `http://127.0.0.1:${port}`,
    async close() {
      if (typeof server.closeAllConnections === 'function') server.closeAllConnections()
      await new Promise((resolve) => server.close(resolve))
    },
  }
}
