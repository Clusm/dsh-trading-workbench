/**
 * @local/dsh-trading-workbench AI decision-chain core (M1.3a).
 *
 * This module is intentionally service-free: no HTTP, no DB, no client view,
 * no tools. It can be exercised with a fake `llm.stream` in plain Node.
 */

export {
  CHAIN_VERSION,
  DEBATE_MODES,
  DEFAULT_DEBATE_MODE,
  ROLE_EFFORT,
  ROLE_MAX_TOKENS,
  ROLE_TIMEOUT_MS,
  EVIDENCE_SLICE_TOKENS,
} from './constants.js'
export { runChain, runChainFromContext, createChainRunner } from './chain.js'
export { buildEvidenceView, sliceEvidence, evidenceAgeErrors, evidenceSummary, hashEvidence, estimateTokens } from './evidence.js'
export { buildPrompt, buildSystemPrompt, buildUserPayload, COMMON_SYSTEM } from './prompts.js'
export { computeScorecard } from './scorecard.js'
export { SCHEMAS } from './schemas.js'
export { validateSchema, formatErrors } from './validate.js'
export { validateSemantics } from './semantics.js'
export { ChainError } from './errors.js'
