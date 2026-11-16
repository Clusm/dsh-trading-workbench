/** Bare WebServer routes do not inherit DSH Connection's admission checks. */
export function admittedWebRoutes(ctx) {
  return {
    register(route) {
      return ctx.webServer.register({
        ...route,
        async handler(req, res) {
          // Resolve per request: a missing/disposing Connection must fail closed.
          let rejection = 503
          try {
            const connection = ctx.connection
            if (typeof connection?.requestRejection === 'function') {
              rejection = connection.requestRejection(req)
            }
          } catch {
            // Never expose gate errors (which may contain deployment details).
            rejection = 503
          }
          if (rejection !== undefined) {
            const status = Number.isInteger(rejection) && rejection >= 400 && rejection <= 599
              ? rejection : 503
            res.writeHead(status, {
              'content-type': 'application/json; charset=utf-8',
              'cache-control': 'no-store',
            })
            res.end(JSON.stringify({
              ok: false,
              error: {
                code: status === 401 ? 'UNAUTHORIZED' : status === 403 ? 'FORBIDDEN' : 'ADMISSION_UNAVAILABLE',
                message: status === 401 ? 'Authentication required.'
                  : status === 403 ? 'Request rejected by DSH.' : 'DSH request admission unavailable.',
              },
            }))
            return
          }
          return route.handler(req, res)
        },
      })
    },
  }
}
