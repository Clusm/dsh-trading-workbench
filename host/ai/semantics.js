/**
 * Semantic checks that cannot be expressed cleanly in the JSON schema alone.
 * These mirror 02 §6.8 and §7.3: direction locks, round visibility, R2 plan
 * copy, decision plan copy, risk tightening, execution copy.
 */

function add(errors, path, message) {
  errors.push({ path, message, keyword: 'semantic' })
}

function num(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && /^[0-9]+(\.[0-9]+)?$/.test(value)) return Number(value)
  return null
}

function deepEqual(a, b) {
  if (a === b) return true
  if (typeof a !== typeof b) return false
  if (a === null || b === null) return false
  if (Array.isArray(a) || Array.isArray(b)) {
    if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false
    return a.every((item, index) => deepEqual(item, b[index]))
  }
  if (typeof a === 'object') {
    const keys = Object.keys(a)
    if (keys.length !== Object.keys(b).length) return false
    return keys.every((key) => Object.prototype.hasOwnProperty.call(b, key) && deepEqual(a[key], b[key]))
  }
  return false
}

function near(a, b, tolerance = 1e-9) {
  const x = num(a)
  const y = num(b)
  return x !== null && y !== null && Math.abs(x - y) <= tolerance
}

/** Exact plan equality, including entry zone, SL basis/distance and TP rr. */
function samePlan(a, b) {
  return Boolean(a && b) && deepEqual(a, b)
}

/** Decision copy: every plan field is locked except risk_pct/leverage, which
 * may only decrease. */
function copiedPlanWithRiskDown(a, b) {
  if (!a || !b) return false
  const restA = structuredClone(a)
  const restB = structuredClone(b)
  const riskA = num(restA.risk_pct)
  const riskB = num(restB.risk_pct)
  const leverageA = restA.leverage
  const leverageB = restB.leverage
  delete restA.risk_pct
  delete restA.leverage
  delete restB.risk_pct
  delete restB.leverage
  return deepEqual(restA, restB)
    && riskA !== null && riskB !== null && riskA <= riskB
    && Number.isInteger(leverageA) && Number.isInteger(leverageB) && leverageA <= leverageB
}

function evidenceStrength(caseValue) {
  const weights = { high: 1, medium: 0.6, low: 0.3 }
  const items = Array.isArray(caseValue?.evidence) ? caseValue.evidence : []
  if (items.length === 0) return 0
  return items.reduce((sum, item) => sum + (weights[item.weight] ?? 0), 0) / items.length
}

/** Validate semantic constraints for a prompt role key. */
export function validateSemantics(roleKey, value, context = {}) {
  const errors = []
  const caps = context.caps ?? { maxRiskPct: 1, maxLeverage: 10, minRr: 1.2 }

  if (roleKey === 'analysis') {
    if (value.agent !== 'analysis') add(errors, '$.agent', 'agent must be analysis')
    if (context.cycleId && value.cycle_id !== context.cycleId) add(errors, '$.cycle_id', 'cycle_id drift')
    if (!Array.isArray(value.candidates)) return errors
    const symbols = value.candidates.map((item) => item.symbol)
    if (new Set(symbols).size !== symbols.length) add(errors, '$.candidates', 'duplicate candidate symbol')
    if (value.stand_aside) {
      if (value.primary_symbol !== null) add(errors, '$.primary_symbol', 'primary_symbol must be null when standing aside')
      if (typeof value.stand_aside_reason !== 'string' || value.stand_aside_reason.trim().length === 0) {
        add(errors, '$.stand_aside_reason', 'stand_aside_reason must be non-empty')
      }
    } else {
      if (!value.primary_symbol) add(errors, '$.primary_symbol', 'primary_symbol is required unless stand_aside')
      else if (!symbols.includes(value.primary_symbol)) add(errors, '$.primary_symbol', 'primary_symbol must be one of candidates')
      if (value.stand_aside_reason !== null) add(errors, '$.stand_aside_reason', 'stand_aside_reason must be null unless stand_aside')
    }
    return errors
  }

  if (roleKey === 'red_opening' || roleKey === 'blue_opening' || roleKey === 'red_response' || roleKey === 'blue_response') {
    const isRed = roleKey.startsWith('red')
    const expectedRound = roleKey.endsWith('response') ? 2 : 1
    const expectedAgent = isRed ? 'red_team' : 'blue_team'
    const expectedSide = isRed ? 'LONG' : 'SHORT'
    if (value.agent !== expectedAgent) add(errors, '$.agent', `expected ${expectedAgent}`)
    if (value.side !== expectedSide) add(errors, '$.side', `expected ${expectedSide}`)
    if (context.primarySymbol && value.symbol !== context.primarySymbol) {
      add(errors, '$.symbol', 'debate symbol must equal analysis.primary_symbol')
    }
    if (context.cycleId && value.cycle_id !== context.cycleId) add(errors, '$.cycle_id', 'cycle_id drift')
    if (value.round !== expectedRound) add(errors, '$.round', `expected round ${expectedRound}`)

    if (expectedRound === 1) {
      if (value.responds_to !== null) add(errors, '$.responds_to', 'opening must not respond')
      if ((value.rebuttal ?? []).length !== 0) add(errors, '$.rebuttal', 'opening must not contain rebuttal')
    } else {
      if (typeof value.responds_to !== 'string' || value.responds_to.length < 5) {
        add(errors, '$.responds_to', 'response must point to opponent opening call_id')
      } else if (context.opponentCallId && value.responds_to !== context.opponentCallId) {
        add(errors, '$.responds_to', 'response targets wrong call_id')
      }
      if (!Array.isArray(value.rebuttal) || value.rebuttal.length < 1 || value.rebuttal.length > 3) {
        add(errors, '$.rebuttal', 'response needs 1-3 rebuttal items')
      }
    }

    if (value.tradeable === true) {
      if (!value.plan_draft) add(errors, '$.plan_draft', 'tradeable case needs a plan')
      if ((value.evidence ?? []).length < 3) add(errors, '$.evidence', 'tradeable case needs >=3 evidence items')
      if (!value.trigger) add(errors, '$.trigger', 'tradeable case needs trigger')
      if (!value.invalidation) add(errors, '$.invalidation', 'tradeable case needs invalidation')
      if (context.ownOpening?.plan_draft && !samePlan(value.plan_draft, context.ownOpening.plan_draft)) {
        add(errors, '$.plan_draft', 'R2 plan must copy the same side R1 plan')
      }
    } else if (value.tradeable === false) {
      if (value.plan_draft !== null) add(errors, '$.plan_draft', 'non-tradeable case must have null plan')
      if (value.trigger !== null) add(errors, '$.trigger', 'non-tradeable case must have null trigger')
      if (value.invalidation !== null) add(errors, '$.invalidation', 'non-tradeable case must have null invalidation')
      if (!value.no_case_reason) add(errors, '$.no_case_reason', 'non-tradeable case needs a reason')
    }

    if (value.plan_draft) {
      // Gate thresholds (RR, stop distance, confidence, caps) are intentionally
      // NOT hard semantic errors here: a case may be valid but gate=FAIL_RR,
      // which is how the decision layer knows not to select it.
      const plan = value.plan_draft
      if (plan.entry?.style !== 'MARKET') add(errors, '$.plan_draft.entry.style', 'v1 entry.style must be MARKET')
      if (num(plan.risk_pct) === null || num(plan.risk_pct) <= 0) add(errors, '$.plan_draft.risk_pct', 'risk_pct must be > 0')
      if (plan.leverage < 1 || plan.leverage > 15) add(errors, '$.plan_draft.leverage', 'leverage must be 1..15')
    }
    return errors
  }

  if (roleKey === 'decision') {
    if (context.cycleId && value.cycle_id !== context.cycleId) add(errors, '$.cycle_id', 'cycle_id drift')
    const openings = context.openings ?? {}
    if (value.action === 'TRADE') {
      if (!['LONG', 'SHORT'].includes(value.side)) add(errors, '$.side', 'TRADE needs side')
      if (!['red', 'blue'].includes(value.selected_case)) add(errors, '$.selected_case', 'TRADE needs selected_case')
      if (!value.plan) add(errors, '$.plan', 'TRADE needs plan')
      if (value.no_trade_reason !== null) add(errors, '$.no_trade_reason', 'TRADE must not have no_trade_reason')
      if (num(value.confidence) < 0.55) add(errors, '$.confidence', 'TRADE confidence < 0.55')
      const expectedSide = value.selected_case === 'red' ? 'LONG' : 'SHORT'
      if (value.side !== expectedSide) add(errors, '$.side', 'side does not match selected_case')
      const selected = value.selected_case === 'red' ? openings.red : openings.blue
      if (selected) {
        if (value.symbol !== selected.symbol) add(errors, '$.symbol', 'symbol must equal selected case symbol')
        if (value.side !== selected.side) add(errors, '$.side', 'side must equal selected case side')
        if (context.primarySymbol && selected.symbol !== context.primarySymbol) {
          add(errors, '$.selected_case', 'selected case symbol must equal analysis.primary_symbol')
        }
        if (selected.plan_draft && value.plan && !copiedPlanWithRiskDown(value.plan, selected.plan_draft)) {
          add(errors, '$.plan', 'decision plan must copy the full R1 plan and only lower risk/leverage')
        }
      }
      if (context.scorecard) {
        const redGate = context.scorecard.red?.gate
        const blueGate = context.scorecard.blue?.gate
        if (value.selected_case === 'red' && redGate !== 'PASS') add(errors, '$.selected_case', 'cannot select red with gate FAIL')
        if (value.selected_case === 'blue' && blueGate !== 'PASS') add(errors, '$.selected_case', 'cannot select blue with gate FAIL')
        if (context.scorecard.tie === true) add(errors, '$.action', 'tie requires NO_TRADE')
        const selectedScore = value.selected_case === 'red' ? context.scorecard.red?.rubric_score : context.scorecard.blue?.rubric_score
        const otherScore = value.selected_case === 'red' ? context.scorecard.blue?.rubric_score : context.scorecard.red?.rubric_score
        if (Number.isFinite(selectedScore) && Number.isFinite(otherScore) && selectedScore < otherScore && value.score_override !== true) {
          add(errors, '$.score_override', 'lower-scoring case requires score_override=true')
        }
        if (value.score_override === true && typeof value.override_rationale !== 'string') {
          add(errors, '$.override_rationale', 'score_override needs rationale')
        }
        if (value.score_override === false && value.override_rationale !== null) {
          add(errors, '$.override_rationale', 'override_rationale must be null when score_override=false')
        }
      }
    } else if (value.action === 'NO_TRADE') {
      if (value.side !== null) add(errors, '$.side', 'NO_TRADE side must be null')
      if (value.selected_case !== null) add(errors, '$.selected_case', 'NO_TRADE selected_case must be null')
      if (value.symbol !== null) add(errors, '$.symbol', 'NO_TRADE symbol must be null')
      if (value.plan !== null) add(errors, '$.plan', 'NO_TRADE plan must be null')
      if (!value.no_trade_reason) add(errors, '$.no_trade_reason', 'NO_TRADE needs reason')
    }
    return errors
  }

  if (roleKey === 'risk') {
    if (context.cycleId && value.cycle_id !== context.cycleId) add(errors, '$.cycle_id', 'cycle_id drift')
    if (value.agent !== 'risk') add(errors, '$.agent', 'agent must be risk')
    if (num(value.max_risk_pct) > caps.maxRiskPct) add(errors, '$.max_risk_pct', 'AI cannot loosen risk cap')
    if (value.max_leverage > caps.maxLeverage) add(errors, '$.max_leverage', 'AI cannot loosen leverage cap')
    if (value.recommendation === 'APPROVE' && num(value.size_multiplier) !== 1) add(errors, '$.size_multiplier', 'APPROVE needs multiplier 1')
    if (value.recommendation === 'DOWNSIZE') {
      if (!(num(value.size_multiplier) > 0 && num(value.size_multiplier) < 1)) add(errors, '$.size_multiplier', 'DOWNSIZE needs 0 < multiplier < 1')
      if ((value.concerns ?? []).length === 0) add(errors, '$.concerns', 'DOWNSIZE needs at least one concern')
    }
    if (value.recommendation === 'REJECT') {
      if (num(value.size_multiplier) !== 0) add(errors, '$.size_multiplier', 'REJECT needs multiplier 0')
      if ((value.concerns ?? []).length === 0) add(errors, '$.concerns', 'REJECT needs at least one concern')
    }
    return errors
  }

  if (roleKey === 'execution_entry') {
    if (context.cycleId && value.cycle_id !== context.cycleId) add(errors, '$.cycle_id', 'cycle_id drift')
    if (value.agent !== 'execution') add(errors, '$.agent', 'agent must be execution')
    if (context.decision) {
      if (value.symbol !== context.decision.symbol) add(errors, '$.symbol', 'top-level symbol drift')
      if (value.side !== context.decision.side) add(errors, '$.side', 'top-level side drift')
    }
    if (value.executable === true) {
      if (!value.order_plan) add(errors, '$.order_plan', 'executable entry needs order_plan')
      if (!value.preflight) add(errors, '$.preflight', 'executable entry needs preflight')
      if (value.blocked_reason !== null) add(errors, '$.blocked_reason', 'executable entry has blocked_reason')
      if (value.order_plan && context.decision) {
        const plan = value.order_plan
        const d = context.decision
        if (plan.symbol !== d.symbol) add(errors, '$.order_plan.symbol', 'symbol drift')
        if (plan.side !== d.side) add(errors, '$.order_plan.side', 'side drift')
        if (d.plan && !near(plan.sl, d.plan.stop_loss?.price)) add(errors, '$.order_plan.sl', 'SL drift')
        if (d.plan && !near(plan.tp, d.plan.take_profit?.price)) add(errors, '$.order_plan.tp', 'TP drift')
        if (plan.entry_type !== 'MARKET') add(errors, '$.order_plan.entry_type', 'v1 entry_type must be MARKET')
      }
      for (const key of ['qty', 'sl', 'tp']) {
        if (typeof value.order_plan?.[key] !== 'string' || !/^[0-9]+(\.[0-9]+)?$/.test(value.order_plan[key])) {
          add(errors, `$.order_plan.${key}`, `${key} must be a decimal string`)
        }
      }
      if (value.order_plan && num(value.order_plan.qty) <= 0) add(errors, '$.order_plan.qty', 'qty must be > 0')
      if (value.order_plan && num(value.order_plan.leverage) > caps.maxLeverage) add(errors, '$.order_plan.leverage', 'leverage above cap')
      if ((value.preflight?.checks ?? []).some((item) => item.ok !== true)) add(errors, '$.preflight.checks', 'all preflight checks must be ok')
    } else {
      if (value.order_plan !== null) add(errors, '$.order_plan', 'blocked entry must have null order_plan')
      if (value.preflight !== null) add(errors, '$.preflight', 'blocked entry must have null preflight')
      if (!value.blocked_reason) add(errors, '$.blocked_reason', 'blocked entry needs reason')
    }
    return errors
  }

  if (roleKey === 'execution_manage') {
    if (value.agent !== 'execution') add(errors, '$.agent', 'agent must be execution')
    if (value.action === 'HOLD' || value.action === 'REPAIR_PROTECTION') {
      if ((value.orders ?? []).length !== 0) add(errors, '$.orders', `${value.action} must have no orders`)
    }
    if (value.action === 'CLOSE') {
      if ((value.orders ?? []).length !== 1 || value.orders[0].purpose !== 'CLOSE' || value.orders[0].qty !== null) {
        add(errors, '$.orders', 'CLOSE needs exactly one CLOSE order with null qty')
      }
    }
    if (value.action === 'REDUCE') {
      if ((value.orders ?? []).length < 1) add(errors, '$.orders', 'REDUCE needs at least one order')
      if ((value.orders ?? []).some((item) => item.purpose !== 'REDUCE')) add(errors, '$.orders', 'REDUCE orders must be REDUCE')
      for (const [index, order] of (value.orders ?? []).entries()) {
        if (typeof order.qty !== 'string' || !/^[0-9]+(\.[0-9]+)?$/.test(order.qty) || num(order.qty) <= 0) {
          add(errors, `$.orders[${index}].qty`, 'REDUCE qty must be a positive decimal string')
        }
      }
    }
    return errors
  }

  if (roleKey === 'review') {
    if (value.agent !== 'review') add(errors, '$.agent', 'agent must be review')
    return errors
  }

  return errors
}

/**
 * §8·D execution gate: a deterministic pre-submit check.
 *
 * The model only ever *proposes*. Before a plan can reach P1/P2 the chain must
 * verify facts it can actually check: the plan carries an entry zone and an
 * expiry, the evidence it was built on is still fresh, the live price is inside
 * the approved zone, and SL/TP are on the correct side.
 *
 * Order matters: "can this be verified at all" comes before "is it still valid",
 * so a plan missing a zone is reported as PLAN_INVALID rather than PRICE_MOVED —
 * the old code blamed price movement for plans that never had a zone.
 *
 * Returns { ok, code, detail, checked_at, checks } and never throws, so the
 * caller decides whether the verdict blocks (enforce) or is only recorded.
 */
export const GATE_CODES = Object.freeze({
  PLAN_MISSING: 'PLAN_MISSING',
  // 缺入场区间必须报 PLAN_INVALID（不是 PRICE_MOVED）：计划本身不完整，
  // 而不是“价格变了”。PRICE_MOVED 只用于“价格确实离开了已批准区间”。
  PLAN_INVALID: 'PLAN_INVALID',
  ENTRY_ZONE_MISSING: 'ENTRY_ZONE_MISSING',
  VALID_UNTIL_MISSING: 'VALID_UNTIL_MISSING',
  VALID_UNTIL_EXPIRED: 'VALID_UNTIL_EXPIRED',
  BACKGROUND_EVIDENCE_STALE: 'BACKGROUND_EVIDENCE_STALE',
  QUOTE_STALE: 'QUOTE_STALE',
  PRICE_OUT_OF_ZONE: 'PRICE_OUT_OF_ZONE',
  SL_TP_INVALID: 'SL_TP_INVALID',
})

const GATE_DETAIL = Object.freeze({
  PLAN_MISSING: '计划缺失，无法验证任何执行前提，故阻断。',
  PLAN_INVALID: '计划缺少入场区间，无法验证现价是否在允许入场范围内，故阻断。',
  ENTRY_ZONE_MISSING: '计划缺少入场区间，无法验证现价是否在允许入场范围内，故阻断。',
  VALID_UNTIL_MISSING: '计划缺少失效时间，无法判断是否仍然有效，故阻断。',
  VALID_UNTIL_EXPIRED: '计划已过失效时间，本轮不再执行。',
  BACKGROUND_EVIDENCE_STALE: '背景证据超过有效期，方向判断可能已过时，故阻断。',
  QUOTE_STALE: '执行盘口报价超过有效期，无法确认真实入场价，故阻断。',
  PRICE_OUT_OF_ZONE: '现价不在计划允许的入场区间内，按计划不执行（不改方向、不追价）。',
  SL_TP_INVALID: '止损/止盈方向或数值不合法，无法建立有效保护，故阻断。',
})

function gateIso(value) {
  if (typeof value !== 'string' || value.length === 0) return null
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : null
}

export function evaluateEntryPlan(context = {}) {
  const checkedAt = Number.isFinite(context.now) ? context.now : Date.now()
  const backgroundMaxAgeMs = Number.isFinite(context.backgroundMaxAgeMs)
    ? context.backgroundMaxAgeMs
    : 180_000
  const entryMaxAgeMs = Number.isFinite(context.entryMaxAgeMs) ? context.entryMaxAgeMs : 15_000
  const plan = context.plan ?? null
  const quote = context.quote ?? null
  const checks = []
  // §8·D / T16：缺哪个字段、为什么缺就不能执行，一律用中文写进结果，
  // 让 client/账本能直接显示，而不是只给一个英文关键码。
  let gapList = []
  const fail = (code, subCode = null) => {
    checks.push({ code, ok: false })
    return {
      ok: false,
      code,
      sub_code: subCode,
      detail: GATE_DETAIL[code] ?? code,
      checked_at: checkedAt,
      checks,
      gaps: gapList,
    }
  }
  const pass = (code) => checks.push({ code, ok: true })

  if (!plan || typeof plan !== 'object') {
    gapList = describePlanGaps(plan)
    return fail(GATE_CODES.PLAN_MISSING)
  }
  gapList = describePlanGaps(plan)

  // 1) 能否验证：入场区间
  const zoneLow = num(plan?.entry?.zone_low)
  const zoneHigh = num(plan?.entry?.zone_high)
  if (zoneLow === null || zoneHigh === null || zoneLow > zoneHigh) {
    // 统一对外原因码 PLAN_INVALID，具体子原因放 sub_code，避免下游只认字符串。
    return fail(GATE_CODES.PLAN_INVALID, GATE_CODES.ENTRY_ZONE_MISSING)
  }
  pass(GATE_CODES.ENTRY_ZONE_MISSING.replace('_MISSING', '_PRESENT'))

  // 2) 止损/止盈方向
  const side = plan?.side ?? null
  const sl = num(plan?.stop_loss?.price)
  const tp = num(plan?.take_profit?.price)
  if (sl === null || tp === null || sl <= 0 || tp <= 0) return fail(GATE_CODES.SL_TP_INVALID)
  if (side === 'LONG' && !(sl < zoneLow && tp > zoneHigh)) return fail(GATE_CODES.SL_TP_INVALID)
  if (side === 'SHORT' && !(sl > zoneHigh && tp < zoneLow)) return fail(GATE_CODES.SL_TP_INVALID)
  pass('SL_TP_VALID')

  // 3) 失效时间
  const validUntil = gateIso(plan?.valid_until)
  if (validUntil === null) return fail(GATE_CODES.VALID_UNTIL_MISSING)
  if (checkedAt > validUntil) return fail(GATE_CODES.VALID_UNTIL_EXPIRED)
  pass('VALID_UNTIL_OK')

  // 4) 背景证据（方向依据）时效：180s
  const backgroundMs = Number.isFinite(quote?.background_age_ms)
    ? quote.background_age_ms
    : num(context.backgroundAgeMs)
  if (backgroundMs === null || backgroundMs > backgroundMaxAgeMs) {
    return fail(GATE_CODES.BACKGROUND_EVIDENCE_STALE)
  }
  pass('BACKGROUND_EVIDENCE_FRESH')

  // 5) 执行盘口时效：15s（与背景证据分开，硬压成同一个会让每轮必 stale）
  const quoteMs = Number.isFinite(quote?.age_ms) ? quote.age_ms : num(context.quoteAgeMs)
  if (quoteMs === null || quoteMs > entryMaxAgeMs) return fail(GATE_CODES.QUOTE_STALE)
  pass('QUOTE_FRESH')

  // 6) 现价必须在允许入场区间内（不改方向、不追价）
  const price = num(quote?.last) ?? num(context.price)
  if (price === null) return fail(GATE_CODES.QUOTE_STALE)
  if (price < zoneLow || price > zoneHigh) return fail(GATE_CODES.PRICE_OUT_OF_ZONE)
  pass('PRICE_IN_ZONE')

  return { ok: true, code: null, detail: null, checked_at: checkedAt, checks, gaps: gapList }
}

/**
 * T16 §8·D：把"计划里缺了哪些可验证字段"写成中文原因。
 *
 * 只做说明，不做判定：evaluateEntryPlan 才是判定方。这样提示层硬化之后，
 * client、账本、repair 提示都能用同一段中文解释"为什么这条计划不能执行"，
 * 也不会因为多一层解释而放松任何 schema/semantic 校验。
 */
export function describePlanGaps(plan) {
  if (!plan || typeof plan !== 'object') {
    return [{
      code: 'PLAN_MISSING',
      field: 'plan',
      reason_zh: '计划整体缺失：没有入场区间与失效时间可供验证，闸门无法放行。',
    }]
  }
  const gaps = []
  const zoneLow = num(plan?.entry?.zone_low)
  const zoneHigh = num(plan?.entry?.zone_high)
  if (zoneLow === null || zoneHigh === null) {
    gaps.push({
      code: 'ENTRY_ZONE_MISSING',
      field: 'entry.zone_low/entry.zone_high',
      reason_zh: '计划没有给出入场区间 entry.zone_low/entry.zone_high。区间必须由 levels（支撑/阻力）或 ATR 推导，不能只用单个 entry.price 代替；缺了它就无法判断现价是否在允许入场范围内，故不能执行。',
    })
  } else if (zoneLow > zoneHigh) {
    gaps.push({
      code: 'ENTRY_ZONE_INVALID',
      field: 'entry.zone_low/entry.zone_high',
      reason_zh: '入场区间自相矛盾（zone_low 大于 zone_high），无法判断允许入场范围，故不能执行。',
    })
  }
  const validUntilRaw = plan?.valid_until
  if (gateIso(validUntilRaw) === null) {
    gaps.push({
      code: 'VALID_UNTIL_MISSING',
      field: 'valid_until',
      reason_zh: '计划没有给出失效时间 valid_until（ISO 时间戳）。它必须由本计划的失效条件推导：取 trigger.valid_until 与时间止损/失效时刻中的较早者；缺了它就无法判断计划是否仍然有效，故不能执行。',
    })
  }
  return gaps
}
export { evidenceStrength, samePlan, copiedPlanWithRiskDown }
