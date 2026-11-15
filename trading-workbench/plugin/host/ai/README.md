# AI decision-chain core (M1.3a)

Offline, dependency-free core for the seven-role chain described in
`docs/02-AI决策链.md` and `docs/v1-设计方案.md` §8.

## What is implemented

- **Roles**: analysis, red team (opening + response), blue team (opening +
  response), decision, risk AI, execution (ENTRY + MANAGE), and a placeholder
  daily review.
- **Default debate**: `symmetric_two_round`.
  - R1: red opening ∥ blue opening; same analysis/evidence; neither sees the
    other.
  - R2: red response ∥ blue response; each sees only the opponent's R1
    opening (not the opponent's R2 response).
  - Decision sees analysis + 2 openings + 2 responses + deterministic
    `case_scorecard`.
  - `debate.mode=independent` runs only R1 and passes 2 openings to decision.
- **LLM transport**: only the injected `llm.stream()`. The production seam
  (`runChainFromContext`) passes `ctx.llm`; tests pass a fake. No `tools`, no
  `fetch`, no subagent, no provider key.
- **Effort**: fixed `high` for analysis/red/blue, `max` for decision/risk,
  `low` for execution.
- **Evidence**: `ai-evidence-view/1` projection, role-specific slices with
  5,000/1,800/1,800/1,800/1,800/1,200/600/400 token targets. Each slice keeps
  `evidence_hash`, `age_ms`, `snapshot_id` and `delivery`.
- **Validation**: Draft-07 subset schema + semantic checks (side/round locks,
  R2 plan copy, decision plan copy, risk tightening, execution copy).
- **Repair**: one repair/retry per call; transport timeout/error gets one plain
  retry. After that the role fails closed (`no_trade`/`failed`).
- **Audit**: one entry per attempt with role, round, attempt, provider/model,
  effort, prompt/schema version, latency, usage, finish, schema_ok and
  validation errors. No DB write is performed here.

## Files

| file | purpose |
|---|---|
| `chain.js` | orchestrator, R1/R2/decision/risk/execution, fallbacks |
| `prompts.js` | common prefix + role prompts + role-specific user payload |
| `schemas.js` | 7 contract schemas (`analysis/1`, `debate_case/1`, ...) |
| `validate.js` | dependency-free schema validator |
| `semantics.js` | cross-field/round/plan-copy rules |
| `evidence.js` | projection, hash/age, role slices, budgets |
| `scorecard.js` | deterministic gate/score/tie calculation |
| `llm.js` | `ctx.llm.stream` wrapper with timeout/usage |
| `constants.js` | effort/timeouts/budgets/caps |

## Fake-LLM test

From the repository root:

```bash
node --check trading-workbench/plugin/index.js
node --check trading-workbench/plugin/client.js
node trading-workbench/plugin/test/ai-chain.test.mjs
```

The test uses `node:test` and a fake async-iterable stream. It covers:

- successful 8-call chain (analysis + R1×2 + R2×2 + decision + risk +
  execution);
- `independent` fallback (6 calls);
- NO_TRADE / hard-risk short-circuit;
- R2 timeout → both responses dropped → independent degradation;
- one schema repair;
- analysis timeout → failed/no_trade;
- positive and negative schema cases;
- no `tools` and no external transport in the recorded stream options.

## Integration boundary

M1.3a does not open a web route, call `workbench-service`, write a DB, or modify
the five client views. M1.3b is expected to:

1. feed a server-produced `md-evidence/1` / `ai-evidence-view/1` snapshot;
2. persist `calls[]` and `outputs` through the Host/service audit API;
3. map the result to `RoundView`/SSE and wire the execution result to 03 P1/P2.

The service remains the sole authority for risk caps, approval tokens,
execution, reconciliation and protection.
