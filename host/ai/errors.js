/** Typed chain failures. They are returned as no_trade/failed results, not
 * thrown out of runChain, so one bad model reply cannot crash the Host. */
export class ChainError extends Error {
  constructor(code, message, details = {}) {
    super(message)
    this.name = 'ChainError'
    this.code = code
    this.details = details
  }
}

export function asChainError(error, fallbackCode = 'CHAIN_FAILED') {
  if (error instanceof ChainError) return error
  const code = error?.code ?? error?.name ?? fallbackCode
  return new ChainError(String(code), error?.message ?? String(error), { cause: error })
}

export function isAbortLike(error) {
  return error?.name === 'AbortError'
    || error?.code === 'ABORT_ERR'
    || error?.code === 'TIMEOUT'
    || error?.code === 'LLM_TIMEOUT'
}
