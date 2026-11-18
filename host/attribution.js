/**
 * Reason-code judgement tree (01 §3.7 leaf table) — the single implementation
 * of `attribution` for the Host DTO.
 *
 * Ownership (05 §5): 01 owns category / stage / role / one_liner_zh /
 * next_action_zh; 03 owns "code -> Chinese label" and the Client never rebuilds
 * either table. This module only maps a finished round's facts onto the 01
 * table; it does not invent a label for a code the table does not cover.
 *
 * Fail-closed rules:
 *  - an unmapped code still yields *a* category/stage guess (marked mapped:false)
 *    so the UI can show something, but one_liner_zh / next_action_zh stay null
 *    and the Client falls back to 03's label or "未收录原因码 {code}";
 *  - stop_kind defaults to 'failed' for anything unrecognised (unknown = broken),
 *    never to 'completed'.
 */

/** 01 §3.7 leaf table: the 20 acceptance codes (18 measured + 2 new route codes). */
const ATTRIBUTION = {
  RISK_UNAVAILABLE: {
    category: '链路', categoryKey: 'chain', stage: '风控 AI', stageKey: 'risk', roleKey: 'risk',
    one: '风控 AI 未产出，按保守策略不交易',
    oneEn: 'Risk AI produced no output; the round stays conservative and does not trade',
    next: '提高风控输出上限或降档；等待下一轮',
    nextEn: 'Raise the risk output cap or drop a tier; wait for the next round',
  },
  DEBATE_INCOMPLETE: {
    category: '链路', categoryKey: 'chain', stage: '红蓝开场 R1', stageKey: 'red_opening', roleKey: 'red_opening',
    one: '红蓝开场未完成，按对称规则整轮失败',
    oneEn: 'The red/blue opening did not finish; symmetric rules fail the whole round',
    next: '等待下一轮；持续出现则检查模型负载',
    nextEn: 'Wait for the next round; if it repeats, check model load',
  },
  'EXECUTION_BLOCKED:PRICE_MOVED': {
    category: '执行', categoryKey: 'execution', stage: '操盘手', stageKey: 'execution_entry', roleKey: 'execution_entry',
    one: '现价已离开计划入场区，操盘手判定不可执行',
    oneEn: 'Price has left the planned entry zone; the executor judges it unexecutable',
    next: '等待下一轮重新评估',
    nextEn: 'Wait for the next round to re-evaluate',
  },
  'EXECUTION_BLOCKED:SPREAD_TOO_WIDE': {
    category: '执行', categoryKey: 'execution', stage: '操盘手', stageKey: 'execution_entry', roleKey: 'execution_entry',
    one: '点差过宽，操盘手判定不可执行',
    oneEn: 'Spread is too wide; the executor judges it unexecutable',
    next: '查看执行理由；等待下一轮',
    nextEn: 'Read the execution rationale; wait for the next round',
  },
  EXECUTION_BLOCKED: {
    category: '执行', categoryKey: 'execution', stage: '操盘手', stageKey: 'execution_entry', roleKey: 'execution_entry',
    one: '操盘手判定不可执行',
    oneEn: 'The executor judges the plan unexecutable',
    next: '查看执行理由；等待下一轮重新评估',
    nextEn: 'Read the execution rationale; wait for the next round to re-evaluate',
  },
  // §8·D / T15：确定性执行闸门（ai/semantics.js#evaluateEntryPlan）的拒绝码。
  // 每一条只写“一句话摘要 + 下一步”，逐轮细节（哪个字段缺、现价与区间、报价年龄）
  // 由闸门返回的 detail / plan_gaps 提供，Host 侧不重复写第二套文案。
  'EXECUTION_BLOCKED:PLAN_MISSING': {
    category: '执行闸门', categoryKey: 'gate', stage: '执行闸门（提交前）', stageKey: 'execution_entry', roleKey: null,
    one: '计划整体缺失，闸门无法验证任何执行前提，故阻断',
    oneEn: 'The plan is missing entirely, so the gate cannot verify any execution precondition',
    next: '看本轮闸门 plan_gaps；等待下一轮',
    nextEn: 'Read this round plan_gaps; wait for the next round',
  },
  'EXECUTION_BLOCKED:PLAN_INVALID': {
    category: '执行闸门', categoryKey: 'gate', stage: '执行闸门（提交前）', stageKey: 'execution_entry', roleKey: null,
    one: '计划缺少可验证的入场区间，闸门阻断',
    oneEn: 'The plan has no verifiable entry zone; the gate blocks it',
    next: '看 plan_gaps 的缺字段说明；等待下一轮',
    nextEn: 'Read the plan_gaps field notes; wait for the next round',
  },
  'EXECUTION_BLOCKED:VALID_UNTIL_MISSING': {
    category: '执行闸门', categoryKey: 'gate', stage: '执行闸门（提交前）', stageKey: 'execution_entry', roleKey: null,
    one: '计划没有失效时间，闸门不能判断是否仍有效，故阻断',
    oneEn: 'The plan has no expiry, so the gate cannot tell whether it is still valid',
    next: '看闸门 detail；等待下一轮',
    nextEn: 'Read the gate detail; wait for the next round',
  },
  'EXECUTION_BLOCKED:VALID_UNTIL_EXPIRED': {
    category: '执行闸门', categoryKey: 'gate', stage: '执行闸门（提交前）', stageKey: 'execution_entry', roleKey: null,
    one: '计划已过失效时间，闸门阻断（不追价、不续期）',
    oneEn: 'The plan has passed its expiry; the gate blocks it (no chasing, no renewal)',
    next: '等待下一轮重新评估',
    nextEn: 'Wait for the next round to re-evaluate',
  },
  'EXECUTION_BLOCKED:BACKGROUND_EVIDENCE_STALE': {
    category: '执行闸门', categoryKey: 'gate', stage: '执行闸门（提交前）', stageKey: 'execution_entry', roleKey: null,
    one: '背景证据超过有效期，方向判断可能已过时，故阻断',
    oneEn: 'Background evidence is past its validity window, so the direction read may be stale',
    next: '看闸门 detail 里的背景证据年龄；等待下一轮',
    nextEn: 'Read the evidence age in the gate detail; wait for the next round',
  },
  'EXECUTION_BLOCKED:QUOTE_STALE': {
    category: '执行闸门', categoryKey: 'gate', stage: '执行闸门（提交前）', stageKey: 'execution_entry', roleKey: null,
    one: '提交前报价超出有效期，无法确认真实入场价，故阻断',
    oneEn: 'The pre-submit quote is past its validity window; the real entry price cannot be confirmed',
    next: '看闸门 quote.age_ms 与来源；等待下一轮',
    nextEn: 'Read quote.age_ms and its source in the gate detail; wait for the next round',
  },
  'EXECUTION_BLOCKED:PRICE_OUT_OF_ZONE': {
    category: '执行闸门', categoryKey: 'gate', stage: '执行闸门（提交前）', stageKey: 'execution_entry', roleKey: null,
    one: '现价不在计划允许入场区间内，闸门阻断',
    oneEn: 'Price is outside the plan allowed entry zone; the gate blocks it',
    next: '看闸门 detail 的现价与区间；等待下一轮',
    nextEn: 'Read the price and zone in the gate detail; wait for the next round',
  },
  'EXECUTION_BLOCKED:SL_TP_INVALID': {
    category: '执行闸门', categoryKey: 'gate', stage: '执行闸门（提交前）', stageKey: 'execution_entry', roleKey: null,
    one: '止损/止盈方向或数值不合法，无法建立有效保护，故阻断',
    oneEn: 'Stop/target direction or value is invalid, so no valid protection can be placed',
    next: '看闸门 detail；等待下一轮',
    nextEn: 'Read the gate detail; wait for the next round',
  },
  // T15：轮内规则变更（生效上限或 rules_version 在本轮期间被改）——同一意图不能用
  // 变更后的规则去提交，否则“轮内改规则”就绕过了本轮冻结。
  'EXECUTION_BLOCKED:E_RULES_CHANGED': {
    category: '执行闸门', categoryKey: 'gate', stage: '执行闸门（提交前）', stageKey: 'execution_entry', roleKey: null,
    one: '本轮冻结的风控规则在提交前已变更，该轮阻断',
    oneEn: 'This round frozen risk rules changed before submission; the round is blocked',
    next: '确认是谁改了规则/限额；等待下一轮重新冻结',
    nextEn: 'Find out who changed the rules/caps; wait for the next round to re-freeze',
  },
  EXECUTION_PLAN_INVALID: {
    category: '链路', categoryKey: 'chain', stage: '操盘手', stageKey: 'execution_entry', roleKey: 'execution_entry',
    one: '操盘手未产出合格的订单计划',
    oneEn: 'The executor produced no valid order plan',
    next: '提高执行超时或输出上限；等待下一轮',
    nextEn: 'Raise the execution timeout or output cap; wait for the next round',
  },
  E_RISK_SYMBOL_POSITION: {
    category: '执行闸门', categoryKey: 'gate', stage: 'P1 风控闸门', stageKey: 'p1', roleKey: null,
    one: '该标的已有仓位，v1 不加仓/不反手（P1 拒绝）',
    oneEn: 'The symbol already has a position; v1 never adds or reverses (P1 reject)',
    next: '查看持仓；等待仓位变化',
    nextEn: 'Check positions; wait for the position to change',
  },
  DECISION_INVALID: {
    category: '链路', categoryKey: 'chain', stage: '决策', stageKey: 'decision', roleKey: 'decision',
    one: '决策者输出不合格',
    oneEn: 'The decision output is invalid',
    next: '等待下一轮；持续出现则检查 prompt 与强度',
    nextEn: 'Wait for the next round; if it repeats, check the prompt and effort',
  },
  LLM_FINISH_ERROR: {
    category: '链路', categoryKey: 'chain', stage: '首个失败阶段', stageKey: null, roleKey: null,
    one: '模型输出异常结束（截断/服务端错误/被中止）',
    oneEn: 'Model output ended abnormally (truncated / server error / aborted)',
    next: '检查模型与路由',
    nextEn: 'Check the model and route',
  },
  TRADE_PLAN_READY: {
    category: '成功', categoryKey: 'success', stage: '链尾', stageKey: 'chain_end', roleKey: null,
    one: '交易计划已通过 P1/P2',
    oneEn: 'The trade plan passed P1/P2',
    next: '查看订单与持仓',
    nextEn: 'Open orders and positions',
  },
  LLM_TIMEOUT: {
    category: '链路', categoryKey: 'chain', stage: '首个失败阶段', stageKey: null, roleKey: null,
    one: '模型调用超时',
    oneEn: 'The model call timed out',
    next: '等待下一轮；持续出现则考虑降档',
    nextEn: 'Wait for the next round; if it repeats, consider a lower tier',
  },
  E_RISK_LIQ_DISTANCE: {
    category: '执行闸门', categoryKey: 'gate', stage: 'P1 风控闸门', stageKey: 'p1', roleKey: null,
    one: '强平距离不足（P1 拒绝）',
    oneEn: 'Liquidation distance is too small (P1 reject)',
    next: '查看硬规则命中',
    nextEn: 'Check the hard-rule hits',
  },
  E_RISK_RR: {
    category: '执行闸门', categoryKey: 'gate', stage: 'P1 风控闸门', stageKey: 'p1', roleKey: null,
    one: '盈亏比低于硬规则下限（P1 拒绝）',
    oneEn: 'Reward/risk is below the hard-rule floor (P1 reject)',
    next: '查看硬规则命中',
    nextEn: 'Check the hard-rule hits',
  },
  E_EVIDENCE_UNAVAILABLE: {
    category: '数据', categoryKey: 'data', stage: '证据', stageKey: 'evidence', roleKey: null,
    one: '无可用证据快照',
    oneEn: 'No usable evidence snapshot',
    next: '检查 Service 证据供数',
    nextEn: 'Check service evidence supply',
  },
  E_CYCLE_DUPLICATE_SLOT: {
    category: '基础设施', categoryKey: 'infrastructure', stage: '调度', stageKey: 'schedule', roleKey: null,
    one: '该调度槽位已完成，本轮跳过',
    oneEn: 'This schedule slot already completed; the round is skipped',
    next: '无需处理（幂等保护）',
    nextEn: 'No action needed (idempotency guard)',
  },
  HOST_STALE_RUN: {
    category: '基础设施', categoryKey: 'infrastructure', stage: 'Host', stageKey: 'host', roleKey: null,
    one: '上一进程遗留的运行中轮次，已自动收口',
    oneEn: 'A running round left over from the previous process was closed automatically',
    next: '无需处理；频繁出现则检查 Host 稳定性',
    nextEn: 'No action needed; if it repeats, check Host stability',
  },
  AI_RISK_REJECT: {
    category: '链路', categoryKey: 'chain', stage: '风控 AI', stageKey: 'risk', roleKey: 'risk',
    one: '风控 AI 拒绝',
    oneEn: 'Risk AI rejected the plan',
    next: '查看风控顾虑',
    nextEn: 'Read the risk concerns',
  },
  NO_CANDIDATE: {
    category: '市场', categoryKey: 'market', stage: '分析', stageKey: 'analysis', roleKey: 'analysis',
    one: '分析判定无合格标的',
    oneEn: 'Analysis found no qualified symbol',
    next: '等待下一轮',
    nextEn: 'Wait for the next round',
  },
  ROUTE_UNAVAILABLE: {
    category: '基础设施（路由层）', categoryKey: 'infrastructure', stage: '轮次冻结复核', stageKey: 'route_freeze', roleKey: null,
    one: '路由不可用：本轮未启动任何 AI 调用',
    oneEn: 'Route unavailable: this round started no AI call',
    next: '打开模型配置改用可用模型或档位；或等待模型恢复',
    nextEn: 'Open model settings and pick an available model or tier; or wait for the model to recover',
  },
  ROUTE_FALLBACK_APPLIED: {
    category: '基础设施（路由层）', categoryKey: 'infrastructure', stage: '轮次冻结复核', stageKey: 'route_freeze', roleKey: null,
    one: '已回退到备用路由，本轮按备用路由执行',
    oneEn: 'Fell back to the standby route; this round runs on it',
    next: '检查原路由为何不可用；回退已记录在审计',
    nextEn: 'Check why the primary route was unavailable; the fallback is audited',
  },
  NO_ELIGIBLE_STRATEGY: {
    category: '策略', categoryKey: 'strategy', stage: '策略筛选', stageKey: 'strategy_select', roleKey: null,
    one: '当前无满足触发条件的已授权策略，本轮不交易',
    oneEn: 'No authorized strategy met its trigger conditions this round; no trade',
    next: '查看策略信号状态或等待下一轮行情触发',
    nextEn: 'Check strategy signal status or wait for the next market trigger',
  },
  STRATEGY_AI_CONFLICT: {
    category: '策略', categoryKey: 'strategy', stage: '决策', stageKey: 'decision', roleKey: 'decision',
    one: 'AI 辩论结论与策略固定方向相反，按冲突不开新仓原则跳过',
    oneEn: 'AI debate favoured the opposite direction of the authorized strategy; skipped per conflict rule',
    next: '查看红蓝辩论依据；等待方向一致的下一轮信号',
    nextEn: 'Review the red/blue debate arguments; wait for an aligned signal',
  },
  RULE_DIRECT_EXECUTED: {
    category: '成功', categoryKey: 'success', stage: '纯规则直连', stageKey: 'chain_end', roleKey: null,
    one: '纯规则直连策略已通过硬风控闸门（P1/P2）并提交执行',
    oneEn: 'Direct-rule strategy passed hard risk gates (P1/P2) and executed',
    next: '查看订单、持仓与策略归因统计',
    nextEn: 'Check orders, positions and strategy attribution stats',
  },
  TRADE_EXECUTED: {
    category: '成功', categoryKey: 'success', stage: '纯规则直连', stageKey: 'chain_end', roleKey: null,
    one: '纯规则直连策略已通过硬风控闸门（P1/P2）并提交执行',
    oneEn: 'Direct-rule strategy passed hard risk gates (P1/P2) and executed',
    next: '查看订单、持仓与策略归因统计',
    nextEn: 'Check orders, positions and strategy attribution stats',
  },
  RULE_DIRECT_BLOCKED: {
    category: '执行闸门', categoryKey: 'gate', stage: 'P1/P2 闸门', stageKey: 'p1', roleKey: null,
    one: '纯规则直连订单被风控或执行闸门拦截，未成交',
    oneEn: 'Direct-rule order was blocked by risk or execution gates',
    next: '查看 P1/P2 拦截原因与账户风控状态',
    nextEn: 'Inspect P1/P2 rejection details and account risk state',
  },
  E_STRATEGY_SIGNAL_FALSE: {
    category: '策略闸门', categoryKey: 'gate', stage: 'P1 策略复核', stageKey: 'p1', roleKey: null,
    one: '服务端提交前复核发现策略触发条件已不再满足，按规则拦截',
    oneEn: 'Server pre-submit recheck found the strategy signal no longer holds; blocked',
    next: '行情在评估期间已回落或反转，等待下一轮有效信号',
    nextEn: 'Market moved before submission; wait for the next valid signal',
  },
  E_STRATEGY_SIGNAL_DUPLICATE: {
    category: '策略闸门', categoryKey: 'gate', stage: 'P1 策略复核', stageKey: 'p1', roleKey: null,
    one: '该策略周期信号已消费，幂等去重拦截重复开仓',
    oneEn: 'This strategy period signal was already consumed; duplicate entry blocked',
    next: '无需处理，等待下一个 K 线周期新信号',
    nextEn: 'No action needed; wait for the next timeframe bar signal',
  },
  E_STRATEGY_PLAN_MISMATCH: {
    category: '策略闸门', categoryKey: 'gate', stage: 'P1 策略复核', stageKey: 'p1', roleKey: null,
    one: '订单计划与已确认策略的标的、方向或止损止盈边界不一致，P1 拒绝',
    oneEn: 'Order plan did not match the confirmed strategy symbol, side or SL/TP bounds (P1 reject)',
    next: '检查策略参数与硬风控边界是否冲突',
    nextEn: 'Check whether strategy parameters conflict with hard risk bounds',
  },
  E_STRATEGY_NOT_AUTHORIZED: {
    category: '策略闸门', categoryKey: 'gate', stage: 'P1 策略复核', stageKey: 'p1', roleKey: null,
    one: '策略未处于启用与运行授权状态（或已触发熔断自动停用），P1 拒绝',
    oneEn: 'Strategy is not enabled and authorized (or was auto-paused by circuit breaker); P1 rejected',
    next: '前往策略页检查启用授权或熔断状态',
    nextEn: 'Open Strategies to check authorization or circuit breaker status',
  },
}

/** Non-failure terminal codes: skipping is a legal outcome, not a broken round. */
const SKIP_CODES = new Set([
  'E_CYCLE_DUPLICATE_SLOT', 'ROUND_IN_FLIGHT', 'OVERLAP_SKIPPED', 'AUTO_DISABLED',
  'DAILY_BUDGET_EXHAUSTED', 'PAUSED_AFTER_FAILURES', 'POSITION_OPEN', 'E_CYCLE_RUNNING',
  'RUN_IN_PROGRESS', 'NO_ELIGIBLE_STRATEGY', 'E_STRATEGY_SIGNAL_DUPLICATE',
])

const GATE_CODES = new Set([
  'BOTH_SIDES_UNTRADEABLE', 'CONTROL_LOCKED',
  'E_SYMBOL_NOT_ALLOWED', 'E_APPROVAL_INVALID', 'E_APPROVAL_REQUIRED', 'E_APPROVAL_REPLAY',
  'RULE_DIRECT_BLOCKED', 'E_STRATEGY_SIGNAL_FALSE', 'E_STRATEGY_PLAN_MISMATCH', 'E_STRATEGY_NOT_AUTHORIZED',
])

const GATE_PREFIXES = ['E_RISK_']

const RISK_REJECT_CODES = new Set(['AI_RISK_REJECT', 'HARD_RISK_REJECT'])

function splitCode(code) {
  const text = typeof code === 'string' ? code.trim() : ''
  if (!text) return { full: null, base: null, param: null }
  const index = text.indexOf(':')
  if (index <= 0) return { full: text, base: text, param: null }
  return { full: text, base: text.slice(0, index), param: text.slice(index + 1) || null }
}

/**
 * Category inheritance for codes the 01 leaf table does not list. Never used to
 * invent a sentence: mapped stays false.
 */
function classify(code) {
  const text = String(code ?? '')
  if (!text) return { category: '未收录', categoryKey: 'unmapped', stage: null, stageKey: null, roleKey: null }
  if (/^(E_EVIDENCE_|DATA_|SNAPSHOT_)/.test(text)) {
    return { category: '数据', categoryKey: 'data', stage: '证据', stageKey: 'evidence', roleKey: null }
  }
  if (/^(E_SERVICE_|E_CYCLE_|HOST_|ROUTE_|AUTO_|DAILY_|OVERLAP_|PAUSED_|ROUND_IN_FLIGHT|POSITION_OPEN)/.test(text)) {
    return { category: '基础设施', categoryKey: 'infrastructure', stage: '调度', stageKey: 'schedule', roleKey: null }
  }
  if (/^E_RISK_/.test(text)) {
    return { category: '执行闸门', categoryKey: 'gate', stage: 'P1 风控闸门', stageKey: 'p1', roleKey: null }
  }
  if (/^EXECUTION_/.test(text)) {
    return { category: '执行', categoryKey: 'execution', stage: '操盘手', stageKey: 'execution_entry', roleKey: 'execution_entry' }
  }
  if (text === 'NO_CANDIDATE') {
    return { category: '市场', categoryKey: 'market', stage: '分析', stageKey: 'analysis', roleKey: 'analysis' }
  }
  if (/^(LLM_|JSON_|SCHEMA_|BUDGET_|CHAIN_|NO_ADAPTER|NO_LLM|DEBATE_|DECISION_|RISK_UNAVAILABLE|AI_RISK_|HARD_RISK_|ROLE_)/.test(text)) {
    return { category: '链路', categoryKey: 'chain', stage: '首个失败阶段', stageKey: null, roleKey: null }
  }
  return { category: '未收录', categoryKey: 'unmapped', stage: null, stageKey: null, roleKey: null }
}

/** First failed call in audit order (attempt > 0: the R2 pseudo-call is not a call). */
export function firstFailedCall(calls) {
  if (!Array.isArray(calls)) return null
  for (const call of calls) {
    if (!call || typeof call !== 'object') continue
    if (Number(call.attempt) === 0) continue
    if (call.schema_ok === true) continue
    return call
  }
  return null
}

/**
 * attribution for one finished round.
 * @returns {null | {category, category_key, stage, stage_key, role, role_key,
 *                   one_liner_zh, next_action_zh, reason_code, mapped}}
 */
export function attributeRound({ reasonCode = null, calls = [], status = null } = {}) {
  const { full, base } = splitCode(reasonCode)
  if (!full) return null
  const failed = firstFailedCall(calls)
  const entry = ATTRIBUTION[full] ?? ATTRIBUTION[base] ?? null
  if (entry) {
    // Codes whose stage is "the first stage that broke" borrow it from the audit.
    const stageKey = entry.stageKey ?? failed?.role_key ?? failed?.role ?? null
    return {
      category: entry.category,
      category_key: entry.categoryKey,
      stage: entry.stage,
      stage_key: stageKey,
      role: entry.roleKey ? roleLabel(entry.roleKey) : null,
      role_key: entry.roleKey,
      one_liner_zh: entry.one,
      one_liner_en: entry.oneEn ?? null,
      next_action_zh: entry.next,
      next_action_en: entry.nextEn ?? null,
      reason_code: full,
      mapped: true,
    }
  }
  const guess = classify(base)
  return {
    category: guess.category,
    category_key: guess.categoryKey,
    stage: guess.stage,
    stage_key: guess.stageKey ?? failed?.role_key ?? failed?.role ?? null,
    role: guess.roleKey ? roleLabel(guess.roleKey) : null,
    role_key: guess.roleKey,
    one_liner_zh: null,
    one_liner_en: null,
    next_action_zh: null,
    next_action_en: null,
    reason_code: full,
    mapped: false,
  }
}

const ROLE_ZH = {
  analysis: '分析',
  red_opening: '红队·开场',
  blue_opening: '蓝队·开场',
  red_response: '红队·回应',
  blue_response: '蓝队·回应',
  decision: '决策',
  risk: '风控',
  execution_entry: '操盘手',
  review: '复盘',
}

function roleLabel(roleKey) {
  return ROLE_ZH[roleKey] ?? null
}

/**
 * stop_kind for one round (01 §5.1). Unknown -> 'failed' (fail-closed); a
 * running round has no stop kind yet; an empty reason on a finished round is
 * only "completed" when the outcome says so.
 */
export function stopKindOf({ reasonCode = null, status = null, outcome = null } = {}) {
  const { full, base } = splitCode(reasonCode)
  if (String(status ?? '') === 'running') return null
  if (!full) {
    if (outcome === 'trade') return 'completed'
    if (String(status) === 'failed') return 'failed'
    return null
  }
  if (base === 'TRADE_PLAN_READY' || base === 'TRADE_EXECUTED' || base === 'RULE_DIRECT_EXECUTED') return 'completed'
  if (SKIP_CODES.has(base)) return 'skipped'
  if (base === 'BOTH_SIDES_UNTRADEABLE') return 'gate_reject'
  if (GATE_CODES.has(base)) return 'gate_reject'
  if (GATE_PREFIXES.some((prefix) => base.startsWith(prefix))) return 'gate_reject'
  if (RISK_REJECT_CODES.has(base)) return 'risk_reject'
  if (base === 'NO_CANDIDATE' || base === 'DECISION_NO_TRADE' || base === 'STRATEGY_AI_CONFLICT') return 'decision_no_trade'
  if (base === 'EXECUTION_BLOCKED') return 'execution_blocked'
  if (base === 'ROUTE_FALLBACK_APPLIED') return 'completed'
  // P-05=A: a freeze failure is a legal termination (no call was started), not
  // a broken round — the UI must not count it as a failure.
  if (base === 'ROUTE_UNAVAILABLE') return 'skipped'
  return 'failed'
}

export const ATTRIBUTION_CODES = Object.freeze(Object.keys(ATTRIBUTION))
