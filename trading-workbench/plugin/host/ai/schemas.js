/**
 * M1.3a output schemas (Draft-07 subset). These are the same contract names
 * used by docs/02-AI决策链.md. The service remains authoritative for M1.2
 * data; this module only validates model output inside the Host.
 */

const nonEmptyString = { type: 'string', minLength: 1, maxLength: 240 }
const symbol = { type: 'string', pattern: '^[A-Z0-9]{2,20}USDT$' }
const decimal = { type: 'string', pattern: '^[0-9]+(\\.[0-9]+)?$' }
const positive = { type: 'number', exclusiveMinimum: 0 }
const confidence = { type: 'number', minimum: 0, maximum: 1 }
const warnings = { type: 'array', maxItems: 5, items: { type: 'string', maxLength: 160 } }
const evidenceRefs = { type: 'array', maxItems: 8, items: { type: 'string', minLength: 1, maxLength: 120 } }

const plan = {
  type: 'object',
  additionalProperties: false,
  required: ['entry', 'stop_loss', 'take_profit', 'risk_pct', 'leverage', 'time_stop_hours', 'rr_to_target'],
  properties: {
    entry: {
      type: 'object',
      additionalProperties: false,
      required: ['style'],
      properties: {
        style: { const: 'MARKET' },
        price: positive,
        zone_low: positive,
        zone_high: positive,
      },
    },
    stop_loss: {
      type: 'object',
      additionalProperties: false,
      required: ['price', 'distance_pct', 'basis'],
      properties: {
        price: positive,
        distance_pct: { type: 'number', exclusiveMinimum: 0, maximum: 50 },
        basis: { enum: ['structure', 'atr', 'time'] },
      },
    },
    take_profit: {
      type: 'object',
      additionalProperties: false,
      required: ['price', 'rr'],
      properties: { price: positive, rr: { type: 'number', exclusiveMinimum: 0, maximum: 20 } },
    },
    risk_pct: { type: 'number', minimum: 0.05, maximum: 2.0 },
    leverage: { type: 'integer', minimum: 1, maximum: 15 },
    time_stop_hours: { type: 'number', minimum: 1, maximum: 168 },
    rr_to_target: { type: 'number', exclusiveMinimum: 0, maximum: 20 },
    // §8·D3：计划自带的失效时间（ISO 时间戳）。缺省时闸门判 VALID_UNTIL_MISSING，
    // 绝不补默认值来扩大旧计划的有效期。
    valid_until: { type: 'string', maxLength: 40 },
  },
}

const trigger = {
  type: 'object',
  additionalProperties: false,
  required: ['type', 'description'],
  properties: {
    type: { enum: ['breakout', 'retest', 'reclaim', 'rejection', 'time'] },
    description: { type: 'string', minLength: 5, maxLength: 240 },
    price: positive,
    valid_until: { type: 'string', minLength: 10, maxLength: 40 },
  },
}

const invalidation = {
  type: 'object',
  additionalProperties: false,
  required: ['price', 'condition'],
  properties: { price: positive, condition: { type: 'string', minLength: 5, maxLength: 240 } },
}

const candidate = {
  type: 'object',
  additionalProperties: false,
  required: ['symbol', 'side_bias', 'setup_type', 'thesis', 'trigger', 'invalidation', 'rubric', 'key_evidence_refs', 'flags'],
  properties: {
    symbol,
    side_bias: { enum: ['LONG', 'SHORT', 'BOTH'] },
    setup_type: { enum: ['breakout', 'retest', 'reclaim', 'trend_pullback', 'range_reversal', 'none'] },
    thesis: { type: 'string', minLength: 10, maxLength: 300 },
    trigger: {
      type: 'object',
      additionalProperties: false,
      required: ['description'],
      properties: {
        description: { type: 'string', minLength: 5, maxLength: 200 },
        price: positive,
        valid_until: { type: 'string', minLength: 10, maxLength: 40 },
      },
    },
    invalidation: {
      type: 'object',
      additionalProperties: false,
      required: ['price', 'condition'],
      properties: { price: positive, condition: { type: 'string', minLength: 5, maxLength: 200 } },
    },
    rubric: {
      type: 'object',
      additionalProperties: false,
      required: ['trend_alignment', 'level_confluence', 'momentum', 'volatility_fit', 'liquidity'],
      properties: {
        trend_alignment: confidence,
        level_confluence: confidence,
        momentum: confidence,
        volatility_fit: confidence,
        liquidity: confidence,
      },
    },
    key_evidence_refs: { type: 'array', minItems: 1, maxItems: 5, items: { type: 'string', minLength: 1, maxLength: 120 } },
    flags: { type: 'array', maxItems: 5, items: { type: 'string', maxLength: 64 } },
  },
}

export const analysisSchema = {
  $schema: 'http://json-schema.org/draft-07/schema#',
  $id: 'https://trading-workbench.local/schemas/analysis-1.json',
  title: 'analysis/1',
  type: 'object',
  additionalProperties: false,
  required: ['schema_version', 'cycle_id', 'agent', 'mode', 'market_read', 'symbols_analyzed', 'candidates', 'primary_symbol', 'stand_aside', 'stand_aside_reason', 'confidence', 'evidence_refs', 'warnings'],
  properties: {
    schema_version: { const: 'analysis/1' },
    cycle_id: { type: 'string', pattern: '^cyc-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$' },
    agent: { const: 'analysis' },
    mode: { enum: ['FULL', 'MANAGE'] },
    market_read: {
      type: 'object',
      additionalProperties: false,
      required: ['regime', 'breadth', 'volatility', 'derivatives', 'notes'],
      properties: {
        regime: { enum: ['trend_up', 'trend_down', 'range', 'transition', 'risk_off', 'risk_on'] },
        breadth: {
          type: 'object',
          additionalProperties: false,
          required: ['up', 'down', 'median_pct_24h'],
          properties: {
            up: { type: 'integer', minimum: 0 },
            down: { type: 'integer', minimum: 0 },
            median_pct_24h: { type: 'number' },
          },
        },
        volatility: {
          type: 'object',
          additionalProperties: false,
          required: ['state', 'btc_atr_pct_1h'],
          properties: {
            state: { enum: ['low', 'normal', 'high', 'extreme'] },
            btc_atr_pct_1h: { type: ['number', 'null'], minimum: 0 },
          },
        },
        derivatives: {
          type: 'object',
          additionalProperties: false,
          required: ['funding_state', 'oi_state'],
          properties: {
            funding_state: { enum: ['neutral', 'crowded_long', 'crowded_short'] },
            oi_state: { enum: ['rising', 'falling', 'flat'] },
          },
        },
        notes: { type: 'array', maxItems: 3, items: { type: 'string', maxLength: 160 } },
      },
    },
    symbols_analyzed: { type: 'array', minItems: 1, maxItems: 20, items: symbol },
    candidates: { type: 'array', minItems: 0, maxItems: 3, items: candidate },
    primary_symbol: { type: ['string', 'null'], pattern: '^[A-Z0-9]{2,20}USDT$' },
    stand_aside: { type: 'boolean' },
    stand_aside_reason: { type: ['string', 'null'], enum: ['NO_CANDIDATE', 'MARKET_HOSTILE', 'DATA_DEGRADED', 'OTHER', null] },
    confidence,
    evidence_refs: evidenceRefs,
    warnings,
  },
}

export const debateCaseSchema = {
  $schema: 'http://json-schema.org/draft-07/schema#',
  $id: 'https://trading-workbench.local/schemas/debate-case-1.json',
  title: 'debate_case/1',
  type: 'object',
  additionalProperties: false,
  required: ['schema_version', 'cycle_id', 'agent', 'symbol', 'side', 'round', 'responds_to', 'rebuttal', 'tradeable', 'confidence', 'thesis', 'evidence', 'trigger', 'plan_draft', 'invalidation', 'opponent_steelman', 'no_case_reason', 'warnings'],
  properties: {
    schema_version: { const: 'debate_case/1' },
    cycle_id: { type: 'string', pattern: '^cyc-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$' },
    agent: { enum: ['red_team', 'blue_team'] },
    symbol,
    side: { enum: ['LONG', 'SHORT'] },
    round: { enum: [1, 2] },
    responds_to: { type: ['string', 'null'], maxLength: 160 },
    rebuttal: {
      type: 'array',
      maxItems: 3,
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['target_claim', 'counter'],
        properties: {
          target_claim: { type: 'string', minLength: 5, maxLength: 180 },
          counter: { type: 'string', minLength: 5, maxLength: 400 },
          evidence_ref: { type: 'string', maxLength: 120 },
        },
      },
    },
    tradeable: { type: 'boolean' },
    confidence,
    thesis: { type: 'string', minLength: 10, maxLength: 500 },
    evidence: {
      type: 'array',
      minItems: 1,
      maxItems: 5,
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['ref', 'claim', 'weight'],
        properties: {
          ref: { type: 'string', minLength: 1, maxLength: 120 },
          claim: { type: 'string', minLength: 5, maxLength: 180 },
          weight: { enum: ['high', 'medium', 'low'] },
        },
      },
    },
    trigger: { oneOf: [{ type: 'null' }, { $ref: '#/definitions/trigger' }] },
    plan_draft: { oneOf: [{ type: 'null' }, { $ref: '#/definitions/plan' }] },
    invalidation: { oneOf: [{ type: 'null' }, { $ref: '#/definitions/invalidation' }] },
    opponent_steelman: { type: 'string', minLength: 10, maxLength: 600 },
    no_case_reason: { type: ['string', 'null'], enum: ['NO_SETUP', 'TRIGGER_INVALID', 'RR_TOO_LOW', 'STOP_TOO_WIDE', 'VOLATILITY_UNSUITABLE', 'EVIDENCE_CONFLICT', 'LIQUIDITY_TOO_THIN', 'OTHER', null] },
    warnings,
  },
  definitions: { plan, trigger, invalidation },
}

const caseAssessment = {
  type: 'object',
  additionalProperties: false,
  required: ['strength', 'decisive_evidence', 'fatal_flaw'],
  properties: {
    strength: { enum: ['strong', 'moderate', 'weak'] },
    decisive_evidence: { type: 'string', minLength: 5, maxLength: 240 },
    fatal_flaw: { type: ['string', 'null'], maxLength: 240 },
  },
}

export const decisionSchema = {
  $schema: 'http://json-schema.org/draft-07/schema#',
  $id: 'https://trading-workbench.local/schemas/decision-1.json',
  title: 'decision/1',
  type: 'object',
  additionalProperties: false,
  required: ['schema_version', 'cycle_id', 'agent', 'action', 'side', 'selected_case', 'symbol', 'confidence', 'thesis', 'case_comparison', 'score_override', 'override_rationale', 'plan', 'no_trade_reason', 'invalidation', 'evidence_refs', 'warnings'],
  properties: {
    schema_version: { const: 'decision/1' },
    cycle_id: { type: 'string', pattern: '^cyc-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$' },
    agent: { const: 'decision' },
    action: { enum: ['TRADE', 'NO_TRADE'] },
    side: { type: ['string', 'null'], enum: ['LONG', 'SHORT', null] },
    selected_case: { type: ['string', 'null'], enum: ['red', 'blue', null] },
    symbol: { type: ['string', 'null'], pattern: '^[A-Z0-9]{2,20}USDT$' },
    confidence,
    thesis: { type: 'string', minLength: 10, maxLength: 500 },
    case_comparison: {
      type: 'object',
      additionalProperties: false,
      required: ['red', 'blue', 'decisive_factor', 'opposing_risk'],
      properties: {
        red: caseAssessment,
        blue: caseAssessment,
        decisive_factor: { type: 'string', minLength: 10, maxLength: 300 },
        opposing_risk: { type: 'string', minLength: 5, maxLength: 300 },
      },
    },
    score_override: { type: 'boolean' },
    override_rationale: { type: ['string', 'null'], maxLength: 240 },
    plan: { oneOf: [{ type: 'null' }, { $ref: '#/definitions/plan' }] },
    no_trade_reason: {
      oneOf: [
        { type: 'null' },
        {
          type: 'object',
          additionalProperties: false,
          required: ['code', 'detail'],
          properties: { code: { type: 'string', minLength: 1, maxLength: 80 }, detail: { type: 'string', minLength: 3, maxLength: 300 } },
        },
      ],
    },
    invalidation: {
      oneOf: [
        { type: 'null' },
        {
          type: 'object',
          additionalProperties: false,
          required: ['price_event', 'time', 'review_at'],
          properties: {
            price_event: { type: 'string', minLength: 5, maxLength: 240 },
            time: { type: 'string', maxLength: 120 },
            review_at: { type: 'string', maxLength: 120 },
          },
        },
      ],
    },
    evidence_refs: evidenceRefs,
    warnings,
  },
  definitions: { plan },
}

export const riskAdviceSchema = {
  $schema: 'http://json-schema.org/draft-07/schema#',
  $id: 'https://trading-workbench.local/schemas/risk-advice-1.json',
  title: 'risk_advice/1',
  type: 'object',
  additionalProperties: false,
  required: ['schema_version', 'cycle_id', 'agent', 'recommendation', 'size_multiplier', 'max_risk_pct', 'max_leverage', 'concerns', 'conditions', 'reasoning', 'confidence', 'warnings'],
  properties: {
    schema_version: { const: 'risk_advice/1' },
    cycle_id: { type: 'string', pattern: '^cyc-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$' },
    agent: { const: 'risk' },
    recommendation: { enum: ['APPROVE', 'DOWNSIZE', 'REJECT'] },
    size_multiplier: { type: 'number', minimum: 0, maximum: 1 },
    max_risk_pct: { type: 'number', minimum: 0, maximum: 2.0 },
    max_leverage: { type: 'integer', minimum: 1, maximum: 15 },
    concerns: {
      type: 'array',
      maxItems: 5,
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['code', 'severity', 'detail'],
        properties: {
          code: { type: 'string', minLength: 1, maxLength: 64 },
          severity: { enum: ['low', 'medium', 'high'] },
          detail: { type: 'string', minLength: 5, maxLength: 200 },
          evidence_ref: { type: 'string', maxLength: 120 },
        },
      },
    },
    conditions: {
      type: 'array',
      maxItems: 3,
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['type', 'value', 'description'],
        properties: {
          type: { enum: ['PRICE_ABOVE', 'PRICE_BELOW', 'TIME_BEFORE', 'SPREAD_BELOW_BPS', 'VOLATILITY_BELOW_PCT', 'OTHER'] },
          value: { type: ['number', 'string'] },
          description: { type: 'string', minLength: 5, maxLength: 160 },
        },
      },
    },
    reasoning: { type: 'string', minLength: 10, maxLength: 500 },
    confidence,
    warnings,
  },
}

const preflight = {
  type: 'object',
  additionalProperties: false,
  required: ['reference_price', 'notional_usd', 'risk_usd', 'risk_pct', 'margin_required_usd', 'estimated_fee_usd', 'checks'],
  properties: {
    reference_price: positive,
    notional_usd: positive,
    risk_usd: positive,
    risk_pct: { type: 'number', exclusiveMinimum: 0, maximum: 2.0 },
    margin_required_usd: positive,
    estimated_fee_usd: { type: 'number', minimum: 0 },
    checks: {
      type: 'array',
      minItems: 5,
      maxItems: 14,
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['name', 'ok'],
        properties: {
          name: { type: 'string', minLength: 1, maxLength: 40 },
          ok: { type: 'boolean' },
          detail: { type: 'string', maxLength: 160 },
        },
      },
    },
  },
}

export const executionEntrySchema = {
  $schema: 'http://json-schema.org/draft-07/schema#',
  $id: 'https://trading-workbench.local/schemas/execution-entry-1.json',
  title: 'execution_entry/1',
  type: 'object',
  additionalProperties: false,
  required: ['schema_version', 'cycle_id', 'agent', 'mode', 'executable', 'blocked_reason', 'symbol', 'side', 'order_plan', 'preflight', 'rationale', 'warnings'],
  properties: {
    schema_version: { const: 'execution_entry/1' },
    cycle_id: { type: 'string', pattern: '^cyc-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$' },
    agent: { const: 'execution' },
    mode: { const: 'ENTRY' },
    executable: { type: 'boolean' },
    blocked_reason: { type: ['string', 'null'], enum: ['PRICE_MOVED', 'SPREAD_TOO_WIDE', 'SIZE_BELOW_MIN', 'BROKER_CONSTRAINT', 'PROTECTION_UNSUPPORTED', 'SNAPSHOT_STALE', 'ACCOUNT_UNAVAILABLE', 'OTHER', null] },
    symbol,
    side: { enum: ['LONG', 'SHORT'] },
    order_plan: {
      oneOf: [
        { type: 'null' },
        {
          type: 'object',
          additionalProperties: false,
          required: ['symbol', 'side', 'action', 'entry_type', 'leverage', 'qty', 'sl', 'tp', 'confidence', 'reasoning_ref', 'execution_policy'],
          properties: {
            symbol,
            side: { enum: ['LONG', 'SHORT'] },
            action: { const: 'open' },
            entry_type: { const: 'MARKET' },
            leverage: { type: 'integer', minimum: 1, maximum: 15 },
            qty: decimal,
            sl: decimal,
            tp: decimal,
            confidence,
            reasoning_ref: { type: 'string', minLength: 5, maxLength: 120 },
            execution_policy: {
              type: 'object',
              additionalProperties: false,
              required: ['max_slippage_bps', 'on_protection_failure', 'protection_retry_max'],
              properties: {
                max_slippage_bps: { type: 'number', exclusiveMinimum: 0, maximum: 100 },
                on_protection_failure: { const: 'CLOSE_POSITION' },
                protection_retry_max: { type: 'integer', minimum: 0, maximum: 2 },
              },
            },
          },
        },
      ],
    },
    preflight: { oneOf: [{ type: 'null' }, { $ref: '#/definitions/preflight' }] },
    rationale: { type: 'string', minLength: 10, maxLength: 600 },
    warnings,
  },
  definitions: { preflight },
}

export const executionManageSchema = {
  $schema: 'http://json-schema.org/draft-07/schema#',
  $id: 'https://trading-workbench.local/schemas/execution-manage-1.json',
  title: 'execution_manage/1',
  type: 'object',
  additionalProperties: false,
  required: ['schema_version', 'cycle_id', 'agent', 'mode', 'action', 'position_id', 'symbol', 'orders', 'reason', 'urgency', 'warnings'],
  properties: {
    schema_version: { const: 'execution_manage/1' },
    cycle_id: { type: 'string', pattern: '^cyc-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$' },
    agent: { const: 'execution' },
    mode: { const: 'MANAGE' },
    action: { enum: ['HOLD', 'CLOSE', 'REDUCE', 'REPAIR_PROTECTION'] },
    position_id: nonEmptyString,
    symbol,
    orders: {
      type: 'array',
      maxItems: 2,
      items: {
        type: 'object',
        additionalProperties: false,
        required: ['purpose', 'type', 'qty', 'reduce_only'],
        properties: {
          purpose: { enum: ['CLOSE', 'REDUCE'] },
          type: { const: 'MARKET' },
          qty: { oneOf: [{ type: 'null' }, { type: 'string', pattern: '^[0-9]+(\\.[0-9]+)?$' }] },
          reduce_only: { const: true },
        },
      },
    },
    reason: { type: 'string', minLength: 5, maxLength: 400 },
    urgency: { enum: ['low', 'normal', 'high'] },
    warnings,
  },
}

export const reviewReportSchema = {
  $schema: 'http://json-schema.org/draft-07/schema#',
  $id: 'https://trading-workbench.local/schemas/review-report-1.json',
  title: 'review_report/1',
  type: 'object',
  additionalProperties: false,
  required: ['schema_version', 'review_id', 'review_date', 'agent', 'period', 'summary', 'process_assessment', 'what_worked', 'what_failed', 'risk_events', 'execution_issues', 'lessons', 'proposed_adjustments', 'evidence_refs', 'warnings'],
  properties: {
    schema_version: { const: 'review_report/1' },
    review_id: nonEmptyString,
    review_date: { type: 'string', pattern: '^[0-9]{4}-[0-9]{2}-[0-9]{2}$' },
    agent: { const: 'review' },
    period: { type: 'object', additionalProperties: false, required: ['start', 'end'], properties: { start: nonEmptyString, end: nonEmptyString } },
    summary: { type: 'string', minLength: 20, maxLength: 1200 },
    process_assessment: { type: 'string', minLength: 10, maxLength: 800 },
    what_worked: { type: 'array', maxItems: 5, items: { type: 'string', minLength: 3, maxLength: 240 } },
    what_failed: { type: 'array', maxItems: 5, items: { type: 'string', minLength: 3, maxLength: 240 } },
    risk_events: { type: 'array', maxItems: 10, items: { type: 'string', minLength: 3, maxLength: 240 } },
    execution_issues: { type: 'array', maxItems: 10, items: { type: 'string', minLength: 3, maxLength: 240 } },
    lessons: { type: 'array', maxItems: 5, items: { type: 'string', minLength: 3, maxLength: 240 } },
    proposed_adjustments: { type: 'array', maxItems: 5, items: { type: 'string', minLength: 3, maxLength: 240 } },
    evidence_refs: evidenceRefs,
    warnings,
  },
}

export const SCHEMAS = Object.freeze({
  analysis: analysisSchema,
  debate_case: debateCaseSchema,
  decision: decisionSchema,
  risk_advice: riskAdviceSchema,
  execution_entry: executionEntrySchema,
  execution_manage: executionManageSchema,
  review_report: reviewReportSchema,
})
