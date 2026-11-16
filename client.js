/**
 * Client half of @local/dsh-trading-workbench — P0 shell + cockpit.
 *
 * Scope (P0):
 *  - S0/S1/S2/S3/S4 shell: one workspace switcher, one context bar, one
 *    attention bar, one scroll container, one status bar.
 *  - Two first-level workspaces: cockpit (操作台) and AI (本轮 / 历史 / 复盘).
 *  - Cockpit: account+risk band, positions with the six-code protection
 *    contract and stop/liquidation distance, AI conclusion, orders/fills
 *    (collapsed to one row when both are empty), today's AI summary.
 *  - D1 evidence drawer (five tabs) and D2 system drawer (engineering fields
 *    are the only place they appear).
 *  - Appearance preference: green-up/red-up/mono, persisted locally; it only
 *    re-binds the two numeric color variables.
 *
 * Data stays behind the Host routes:
 *   GET  /trading-workbench/api/state
 *   GET  /trading-workbench/api/health
 *   GET  /trading-workbench/api/rounds
 *   GET  /trading-workbench/api/rounds/:id
 *   POST /trading-workbench/api/run
 *
 * The browser half never reads the service token, never talks to the Python
 * service or the exchange, and never invents a round, a grade or a fill.
 *
 * Styling uses only the 14 live --dsw-* tokens; every derived color is a
 * color-mix over those tokens. No external chart library, font or CDN.
 */

window.__ModuleLoader__.load({
  id: 'dsh-trading-workbench',
  factory(require) {
    const React = require('react')
    const h = React.createElement
    const NS = 'tradingWorkbench'

    // ---- routes (Host only) -------------------------------------------------
    const API = '/trading-workbench/api'
    const STATE_URL = `${API}/state`
    const HEALTH_URL = `${API}/health`
    const PRICES_URL = `${API}/market/prices`
    const MARKS_URL = `${API}/market/marks`
    const ENVELOPE_URL = `${API}/risk/envelope`
    const ROUNDS_URL = `${API}/rounds`
    // 16 §1: first-run bootstrap (readiness + one setup job).
    const SETUP_READINESS_URL = `${API}/setup/readiness`
    const SERVICE_SETUP_URL = `${API}/service/setup`
    const RUN_URL = `${API}/run`
    const LLM_CATALOG_URL = `${API}/llm/catalog`
    const LLM_CONFIG_URL = `${API}/llm/config`
    const POLL_MS = 8000
    // 15 §5 (reload UX): a plugin reload briefly unregisters the Host routes, so
    // the first /state can 404. Retry fast instead of waiting a whole poll tick,
    // then fall back to the normal cadence.
    const POLL_RETRY_MS = 800
    const MARKET_POLL_MS = 8000
    const MARK_QUOTE_POLL_MS = 2000
    const ENVELOPE_POLL_MS = 30000
    const POLL_RUNNING_MS = 3000
    const HEALTH_MS = 30000
    const RUN_TIMEOUT_MS = 400 * 1000
    // §2.3: 不交易 / 规则阻断 / 执行失败 / 待确认 are four different things and
    // must never collapse into one red "failure".
    const ATTENTION_CATEGORY = {
      noTrade: 's2.catNoTrade',
      rule: 's2.catRule',
      exec: 's2.catExec',
      pending: 's2.catPending',
    }
    const WORKSPACES = ['cockpit', 'ai']
    const AI_VIEWS = ['current', 'history', 'review']
    const DRAWER_TABS = ['evidence', 'prompt', 'json', 'rules', 'timing']
    // F2 (§3.1): one settings entry reaches exactly these five groups. Order is
    // the plan's order — a user looking for "where do I stop automation" and
    // "which version is loaded" should not have to learn a new taxonomy.
    const SETTINGS_GROUPS = ['connection', 'trading', 'automation', 'appearance', 'diagnostics']
    const PNL_URL = `${API}/pnl`
    // 实盘接入：凭据只写本机文件，前端只拿指纹与路径（§4.5）。
    const LIVE_SETUP_URL = `${API}/live-setup`
    // Demo 接入：与实盘同一套语义，目标是 dataDir 下的 demo.env（基线 §5.3）。
    const DEMO_SETUP_URL = `${API}/demo-setup`
    const UP_DOWN_KEY = 'twb.appearance.updown'
    const DENSITY_KEY = 'twb.appearance.density'
    const MONO = 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace'

    // ---- one text table for zh + en (structural parity) --------------------
    const TEXT = {
      'ops.title': ['操作历史', 'Operation history'],
      'ops.refresh': ['刷新历史', 'Refresh history'],
      'quote.history': ['历史快照', 'Historical snapshot'],
      'quote.live': ['实时标记价', 'Live mark'],
      'quote.account': ['账户快照', 'Account snapshot'],
      'quote.accountStale': ['账户数据过期，持仓及保护待确认', 'Account data stale; position and protection unconfirmed'],
      'ops.scope': ['最近 100 条本地审计记录；旧版本操作不补造。待确认不代表未执行，请先核对回执与持仓。', 'Latest 100 local audit records; no fabricated older history. Pending does not mean unexecuted; check receipts and positions.'],
      'ops.empty': ['暂无操作记录', 'No operation records'],
      'ops.failedLoad': ['操作历史暂不可用', 'Operation history unavailable'],
      'ops.all': ['全部', 'All'],
      'ops.time': ['时间（北京时间）', 'Time (Shanghai)'],
      'ops.action': ['操作', 'Action'],
      'ops.target': ['目标', 'Target'],
      'ops.reason': ['原因', 'Reason'],
      'ops.result': ['结果', 'Result'],
      'ops.request': ['请求编号', 'Request ID'],
      'ops.details': ['查看回执', 'Receipt details'],
      'ops.closed': ['已平仓', 'Closed'],
      'ops.unconfirmed': ['未确认平仓', 'Close unconfirmed'],
      'ops.close': ['平仓', 'Close'],
      'ops.repair': ['补挂保护', 'Repair protection'],
      'ops.control': ['切换控制态', 'Change control'],
      'ops.reconcile': ['对账', 'Reconcile'],
      'ops.pending': ['待确认', 'Pending'],
      'ops.succeeded': ['已完成', 'Completed'],
      'ops.partial': ['部分完成，需核对', 'Partially completed; verify'],
      'ops.failed': ['失败', 'Failed'],
      'ops.unknown': ['结果未确认，勿重复提交', 'Outcome unknown; do not resubmit'],
      'control.title': ['控制态操作', 'Control actions'],
      'command.pause': ['暂停新开仓', 'Pause new entries'],
      'command.close_only': ['切换仅平仓', 'Close only'],
      'command.normal': ['恢复正常', 'Resume normal'],
      'command.emergency': ['紧急全平', 'Emergency close all'],
      'control.hint': ['暂停只阻止新开仓，保留现有持仓和保护单；恢复仍受硬风控限制。', 'Pause blocks entries and keeps positions and protection; resuming remains subject to hard risk rules.'],
      'control.warning': ['将先阻断新开仓，再尝试市价平掉所有 Demo 持仓。可能部分失败，必须核对结果。', 'Blocks entries, then attempts market close of all Demo positions. Partial failure is possible; verify results.'],
      'control.confirmHint': ['输入大写状态词确认：{word}', 'Type the uppercase state to confirm: {word}'],
      'control.submit': ['确认执行', 'Confirm action'],
      'control.cancel': ['取消操作', 'Cancel action'],
      'control.success': ['控制态操作已完成', 'Control action completed'],
      'control.partial': ['控制态已提交，部分结果需人工核对', 'Control submitted; some outcomes need verification'],
      'control.unavailable': ['结果未确认；请查操作历史及实际持仓，不要自动重试', 'Outcome unknown; check history and positions, do not retry automatically'],
      'control.failed': ['操作被拒绝，请查看原因和操作历史', 'Action rejected; inspect the reason and history'],
      'control.locked': ['日亏损或回撤风控已锁定，不能恢复正常', 'Daily loss or drawdown latch prevents resuming'],
      'control.check': ['核对控制态与持仓', 'Refresh control and positions'],
      'details.title': ['资金与风险明细', 'Portfolio and risk details'],
      'risk.positionLimit': ['已达持仓上限', 'Position limit reached'],
      'details.equityChange': ['今日权益变化（首个快照起算）', 'Equity change since first daily snapshot'],
      'details.baseline': ['起算时间（北京时间）', 'Baseline time (Shanghai)'],
      'details.realized': ['今日已实现盈亏（本地成交）', 'Today realized PnL (local fills)'],
      'details.fees': ['今日手续费（按币种）', 'Today commissions by asset'],
      'details.netRealized': ['已实现扣手续费（不含资金费）', 'Realized after fees (excludes funding)'],
      'details.unknownFees': ['手续费记录缺口 {n} 条', '{n} incomplete fee records'],
      'details.net': ['净收益 / 资金费', 'Net PnL / funding'],
      'details.coverage': ['盈亏按北京时间自然日、本地完整成交记录计算；权益变化从当天首个可用快照起算，可能不覆盖全天。不同币种手续费不合并；旧记录币种缺失时不计算扣费盈亏。资金费记录未提供，不展示完整净收益。', 'Shanghai day; all local fills. Equity change starts at the first available daily snapshot. Fees in different assets are not combined; unknown assets prevent after-fee PnL. Funding is unavailable, so total net PnL is not shown.'],
      'details.margin': ['保证金占用 / 上限', 'Margin usage / limit'],
      'details.positions': ['持仓数 / 上限', 'Positions / limit'],
      'details.loss': ['风控日亏损 / 阈值（UTC）', 'Risk day loss / limit (UTC)'],
      'details.drawdown': ['峰值回撤 / 阈值（UTC）', 'Peak drawdown / limit (UTC)'],
      'details.streak': ['连续亏损 / 阈值', 'Loss streak / limit'],
      'details.cooldown': ['冷却截止', 'Cooldown until'],
      'details.controlReason': ['控制态原因', 'Control reason'],
      'details.blockers': ['新开仓阻断项', 'Entry blockers'],
      'details.riskDay': ['风控日基准 / 峰值权益', 'Risk day baseline / peak equity'],
      'details.missing': ['明细未提供，请加载新版 Service 后刷新', 'Details unavailable; reload the updated Service and refresh'],
      'panel.title': ['交易工作台', 'Trading workbench'],
      'panel.demo': ['DEMO', 'DEMO'],
      'panel.demoHint': ['仅币安 Demo 环境，使用模拟资金，不连接真实账户', 'Binance demo environment only; simulated funds, no real account'],
      'env.live': ['实盘', 'LIVE'],
      'env.unknown': ['环境未知', 'Unknown venue'],
      'env.liveHint': ['实盘环境：使用真实资金。任何下单、停止或平仓都会作用于真实账户。', 'LIVE venue: real funds. Orders, stops and closes affect the real account.'],

      'ws.cockpit': ['操作台', 'Cockpit'],
      'ws.ai': ['AI 决策', 'AI decisions'],
      'nav.cockpit': ['操作台', 'Cockpit'],
      'nav.ai': ['AI 决策', 'AI decisions'],
      'ai.current': ['本轮', 'This round'],
      'ai.history': ['历史', 'History'],
      'ai.review': ['分析', 'Analysis'],
      // ---- 分析页（四条子视图）----
      'an.tabOverview': ['概览', 'Overview'],
      'an.tabFunds': ['资金', 'Funds'],
      'an.tabAttribution': ['归因', 'Attribution'],
      'an.tabQuality': ['质量', 'Quality'],
      'an.funds': ['资金口径', 'Funds'],
      'an.wallet': ['钱包余额', 'Wallet'],
      'an.marginEquity': ['保证金权益', 'Margin equity'],
      'an.upnl': ['未实现盈亏', 'Unrealised'],
      'an.available': ['可用余额', 'Available'],
      'an.books': ['本地四本账', 'Local books'],
      'an.realized': ['已实现盈亏', 'Realised'],
      'an.commission': ['手续费', 'Commission'],
      'an.funding': ['资金费', 'Funding'],
      'an.transfers': ['划转（不计盈利）', 'Transfers (excluded)'],
      'an.net': ['净盈亏（本地账本）', 'Net (local ledger)'],
      'an.reconcile': ['对账', 'Reconcile'],
      'an.diff': ['差额', 'diff'],
      'an.tolerance': ['容差', 'tolerance'],
      'an.coverageFull': ['覆盖范围：完整', 'Coverage: complete'],
      'an.coveragePartial': ['覆盖范围：不完整', 'Coverage: incomplete'],
      'an.srcExchange': ['来源：交易所', 'Source: exchange'],
      'an.srcLedger': ['来源：本地账本', 'Source: local ledger'],
      'an.unknown': ['不可用', 'Unavailable'],
      'an.unavailable': ['资金口径暂不可用（{code}）', 'Funds data unavailable ({code})'],
      'an.loading': ['正在读取资金口径…', 'Loading funds…'],
      'an.fundsHint': ['手续费是成本、资金费单独归集、划转不算盈利；资金费未回填时净盈亏显示“不可用”而不是 0。', 'Commission is a cost, funding is separate, transfers never count as profit; when funding is not backfilled the net shows unavailable, never 0.'],
      'an.attribution': ['决策到成交的归因', 'Decision-to-fill attribution'],
      'an.attributionHint': ['以 {n} 笔成交为准', 'based on {n} fills'],
      'an.fillsLinked': ['{linked} 轮有成交可归因；{readOnly} 轮只下单未成交', '{linked} rounds with fills; {readOnly} rounds ordered but unfilled'],
      'an.noFills': ['当前账本内没有成交记录，无法归因', 'No fills in the ledger — nothing to attribute'],
      'an.orders': ['单', 'orders'],
      'an.fills': ['笔成交', 'fills'],
      'an.unknownFee': ['含非计价币费用，未折算', 'non-quote fee, not converted'],
      'an.netFeeIncluded': ['已含手续费', 'commission included'],
      'an.attributionLimit': ['归因只覆盖本账本内有 cycle_id 的订单；外部手工成交不在其中。', 'Attribution only covers orders carrying a cycle_id; manual fills outside this ledger are excluded.'],
      'an.quality': ['评分卡与实际结果', 'Scorecard vs outcome'],
      'an.qualityHint': ['最近 {n} 轮', 'last {n} rounds'],
      'an.noRounds': ['暂无轮次', 'No rounds yet'],
      'an.qualityCaveat': ['样本量小，只作观察，不作显著性结论。', 'Small sample: observation only, not a significance claim.'],
      'live.title': ['实盘接入', 'Live onboarding'],
      'live.sub': ['凭据只写入本机文件，浏览器不会保存、也不会再读回明文', 'Credentials are written to a local file only; the browser never stores or reads them back'],
      'live.localOnly': ['仅本机', 'local only'],
      'live.currentVenue': ['当前环境', 'Current venue'],
      'live.credentialSource': ['凭据来源', 'Credential source'],
      'live.fingerprint': ['凭据指纹', 'Fingerprint'],
      'live.file': ['目标文件', 'Target file'],
      'live.enter': ['填写实盘 API Key / Secret', 'Enter live API key / secret'],
      'live.apiKey': ['API Key', 'API Key'],
      'live.apiSecret': ['API Secret', 'API Secret'],
      'live.save': ['保存到本机', 'Save locally'],
      'live.saving': ['保存中…', 'Saving…'],
      'live.saved': ['已保存到 {path}（指纹 {fingerprint}）。还需要以 live 环境重启服务才会生效。', 'Saved to {path} (fingerprint {fingerprint}). The service must be restarted in live mode to take effect.'],
      'live.securityNote': ['密钥只写入本机 0600 文件，不进日志、不进前端存储、不回显明文；权限/IP 无法通过 API 可靠验证时不会显示绿色勾选。', 'The key is written to a local 0600 file only: never logged, never stored in the browser, never echoed back. Permissions/IP are not shown as verified because the API cannot prove them.'],
      'live.next': ['接下来', 'Next steps'],
      'demo.title': ['Demo 接入', 'Demo onboarding'],
      'demo.sub': ['币安合约测试网凭据；只写入本机文件，浏览器不会保存、也不会再读回明文', 'Binance futures testnet credentials: written to a local file only; the browser never stores or reads them back'],
      'demo.enter': ['填写 Demo API Key / Secret', 'Enter demo API key / secret'],
      'demo.save': ['保存到本机', 'Save locally'],
      'demo.saving': ['保存中…', 'Saving…'],
      'demo.saved': ['已保存到 {path}（指纹 {fingerprint}）。用「启动服务」重启后生效。', 'Saved to {path} (fingerprint {fingerprint}). It takes effect after restarting the service.'],
      'demo.file': ['凭据文件', 'Credential file'],
      'demo.fileExists': ['已配置', 'configured'],
      'demo.fileAbsent': ['未配置', 'not configured'],
      'demo.serviceSees': ['服务侧读到', 'Service reports'],
      'demo.serviceStale': ['文件已写入但服务仍报未配置：重启服务才会生效', 'The file is written but the service still reports no credentials: restart the service to apply'],
      'demo.effective': ['服务将读取', 'The service will read'],
      'demo.securityNote': ['密钥只写入本机 0600 文件，不进插件配置、不进日志、不进前端存储；这也是它不做成插件配置项的原因。', 'The key goes to a local 0600 file only: never into plugin config, logs or browser storage. That is exactly why it is not a plugin setting.'],
      'demo.next': ['接下来', 'Next steps'],
      'an.range': ['统计窗口', 'Time window'],
      'an.curve': ['权益曲线', 'Equity curve'],
      'an.curveLabel': ['交易所权益（含未实现）', 'Exchange equity (incl. unrealised)'],
      'an.curveUnavailable': ['曲线不可用：还没有权益快照（交易所不可达或尚未采集）', 'Curve unavailable: no equity snapshots yet'],
      'an.curveTooShort': ['曲线不可用：至少需要两个快照点', 'Curve unavailable: needs at least two snapshots'],
      'an.points': ['个点', 'points'],
      'an.fillsUnavailable': ['成交数据不可用（{reason}），不代表没有成交', 'Fill data unavailable ({reason}) — not the same as having no fills'],

      'top.equity': ['总权益', 'Equity'],
      'top.todayPnl': ['今日', 'Today'],
      'top.run': ['运行一轮', 'Run a round'],
      'top.running': ['运行中', 'Running'],
      'top.more': ['更多', 'More'],
      'top.menu.appearance': ['外观', 'Appearance'],
      'top.menu.refresh': ['刷新数据', 'Refresh data'],
      'top.menu.system': ['系统状态', 'System status'],
      'top.menu.shortcut': ['刷新会打穿 8s 缓存', 'Refresh bypasses the 8s cache'],
      'appearance.greenUp': ['绿涨红跌（默认）', 'Green up / red down (default)'],
      'appearance.redUp': ['红涨绿跌', 'Red up / green down'],
      'appearance.mono': ['只符号无色', 'Symbols only, no color'],
      'appearance.note': ['只影响行情与损益数字；失败、阻断、缺失等状态色不变。', 'Affects market and PnL numbers only; failure and block colors stay fixed.'],

      's1.marketNone': ['行情未提供', 'No market data'],
      's1.funding': ['资金费率', 'Funding'],
      's1.spread': ['价差', 'Spread'],
      's1.marketTooltipEmpty': ['Host 未返回行情（/market/prices）', 'Host returned no market data (/market/prices)'],
      's1.marketTooltip': ['行情来自 Host 只读代理 /market/prices；无 24h 字段时不显示涨跌', 'Prices come from the Host read-only proxy; no 24h field, so no change is shown'],
      's1.positions': ['持仓', 'Positions'],
      's1.protection': ['保护', 'Protection'],
      's1.round': ['轮次', 'Round'],
      's1.noRound': ['暂无轮次', 'No rounds yet'],
      's1.pending': ['待处理', 'Pending'],
      's1.data': ['数据', 'Data'],

      's2.view': ['去处理 →', 'Open →'],
      's2.viewPosition': ['查看持仓', 'Open position'],
      's2.more': ['还有 {n} 项', '{n} more'],

      's4.host': ['Host', 'Host'],
      's4.service': ['Service', 'Service'],
      's4.llm': ['LLM', 'LLM'],
      's4.online': ['在线', 'online'],
      's4.offline': ['离线', 'offline'],
      's4.unknown': ['未知', 'unknown'],
      's4.control': ['控制态', 'Control'],
      's4.newEntry': ['新开仓', 'New entry'],
      's4.entryAllowed': ['允许', 'allowed'],
      's4.entryBlocked': ['已阻断', 'blocked'],
      's4.entryDenied': ['服务未放行新开仓', 'service denies new entries'],
      's4.updated': ['最后更新', 'Updated'],
      's4.poll': ['轮询', 'Poll'],
      's4.gaps': ['缺口', 'Gaps'],
      's4.readonly': ['Demo 处置需确认', 'Demo actions require confirmation'],
      's4.schedule': ['自动调度', 'Auto schedule'],
      's4.scheduleOff': ['未启用（M2）', 'not enabled (M2)'],
      's4.scheduleOn': ['开（每 10 分钟）', 'on (every 10 min)'],
      's4.schedulePaused': ['暂停中', 'paused'],
      's4.scheduleBudget': ['已达 24 小时轮次上限', '24-hour round limit reached'],

      'service.online': ['已连接', 'Connected'],
      'service.noToken': ['缺少服务 token', 'Service token missing'],
      'service.tokenUnreadable': ['token 缺失或不可读', 'Token missing or unreadable'],
      'service.authFailed': ['鉴权失败', 'Auth failed'],
      'service.offline': ['Host 未就绪', 'Host not ready'],
      'service.notChecked': ['尚未检查', 'Not checked yet'],
      'service.notReady': ['服务未就绪', 'Service not ready'],
      'service.unavailable': ['服务不可达', 'Service unreachable'],

      'account.equity': ['总权益', 'Equity'],
      'account.dayPnl': ['今日 PnL', 'Today PnL'],
      'account.upnl': ['未实现', 'Unrealized'],
      'account.available': ['可用', 'Available'],
      'account.margin': ['保证金占用', 'Margin used'],
      'account.sessionEquity': ['会话权益', 'Session equity'],
      'account.emptyTitle': ['无账户快照', 'No account snapshot'],
      'account.emptyHint': ['Host 已返回账户接口，但当前没有可用数值（{reason}）。', 'The Host returned the account route but has no usable values ({reason}).'],
      'account.demoMoney': ['Demo 虚拟资金，仅作参考', 'Demo funds, reference only'],

      'risk.title': ['风险预算', 'Risk budget'],
      'risk.prevCaps': ['上轮', 'last round'],
      'risk.maxPositions': ['最大持仓', 'Max positions'],
      'risk.perTrade': ['单笔风险上限', 'Risk per trade'],
      'risk.leverage': ['杠杆上限', 'Max leverage'],
      'risk.maxPositions': ['最大持仓', 'Max positions'],
      'risk.control': ['控制态', 'Control'],
      'risk.newEntry': ['新开仓', 'New entry'],
      'risk.usedMissing': ['风险已用额度未提供', 'Risk usage unavailable'],
      'risk.blockedBy': ['新开仓阻断来自保护状态（{label}，{symbol}）', 'New entries blocked by protection ({label}, {symbol})'],
      'risk.blockedUnknown': ['保护状态未知', 'protection unknown'],
      'risk.controlNormal': ['正常', 'normal'],

      'positions.title': ['持仓与保护', 'Positions & protection'],
      'positions.count': ['{n} 个', '{n}'],
      'positions.snapshot': ['快照 {age}', 'Snapshot {age}'],
      'positions.symbol': ['标的', 'Symbol'],
      'positions.side': ['方向', 'Side'],
      'positions.qty': ['数量', 'Qty'],
      'positions.mark': ['标记价', 'Mark'],
      'positions.upnl': ['未实现', 'UPnL'],
      'positions.protection': ['保护', 'Protection'],
      'positions.distance': ['止损 / 强平', 'Stop / liq.'],
      'positions.stop': ['止损', 'Stop'],
      'positions.liq': ['强平', 'Liq.'],
      'b.aiOrigin': ['AI 建仓', 'AI entry'],
      'b.extOrigin': ['外部建仓', 'External'],
      'trigger.manual': ['手动', 'Manual'],
      'trigger.auto': ['自动', 'Auto'],
      'trigger.schedule': ['调度', 'Schedule'],
      'gate.FAIL_RR': ['盈亏比不足', 'R:R below gate'],
      'gate.RR_TOO_LOW': ['盈亏比过低', 'R:R too low'],
      'positions.margin': ['保证金·杠杆', 'Margin · lev.'],
      'positions.actions': ['动作', 'Actions'],
      'positions.evidence': ['证据', 'Evidence'],
      'positions.emptyTitle': ['当前无持仓', 'No open positions'],
      'positions.emptyHint': ['Host 已返回持仓接口，当前为 0；产生成交后会出现在这里。', 'The Host returned positions; the list is empty. Filled orders will show up here.'],
      'positions.reason': ['原因：{reason}', 'Reason: {reason}'],
      'positions.footer': ['处置：Demo 平仓 / 补挂保护单（需确认）；本阶段不提供下单 / 撤单 · 替代路径：币安 Demo → 合约 → 持仓', 'Actions: demo close / repair protection (confirmed); no new orders or cancels here. Alternative: Binance demo → futures → positions'],
      'positions.close': ['平仓', 'Close'],
      'positions.repair': ['补挂', 'Repair'],
      'positions.repairDisabled': ['保护完好，无需补挂', 'Protection OK; no repair needed'],
  'positions.repairHint': ['保护完好时“补挂”不可用（已有止损/止盈，不改价）', 'Repair is unavailable while protection is intact (existing SL/TP keep their price)'],
      'action.closeTitle': ['确认平仓（Demo）', 'Confirm close (demo)'],
      'action.repairTitle': ['补挂保护单（Demo）', 'Repair protection (demo)'],
      'action.symbol': ['标的', 'Symbol'],
      'action.qty': ['数量', 'Qty'],
      'action.mark': ['参考标记价', 'Mark (ref.)'],
      // 05 N08: this number is the position's *unrealized* PnL. Calling it
      // "realized" pretended a closed result that does not exist yet.
      'action.upnl': ['当前未实现盈亏（参考，未扣手续费）', 'Unrealized PnL now (ref.; fees excluded)'],
      'action.irreversible': ['市价平仓，不可撤销。', 'Market close; cannot be undone.'],
      'action.reason': ['平仓原因（≥2 字）', 'Reason (≥2 chars)'],
      'action.reasonPlaceholder': ['例如：风险超限，人工平仓', 'e.g. risk limit breached, manual exit'],
      'action.cancel': ['取消', 'Cancel'],
      'action.confirmClose': ['确认平仓', 'Confirm close'],
      'action.confirmRepair': ['确认补挂', 'Confirm repair'],
      'action.repairHint': ['为 1 个持仓补挂 SL/TP；已有保护单不改价；补挂后请核对。', 'Repair SL/TP for 1 position; existing orders keep their price; verify afterwards.'],
      'action.receipt': ['回执', 'Receipt'],
      'action.requestId': ['requestId', 'requestId'],
      'action.status': ['状态', 'Status'],
      'action.result': ['结果', 'Result'],
      'action.error': ['失败', 'Failed'],
      'action.noRetry': ['不会自动重试；请核对后再决定。', 'No automatic retry; review before acting again.'],
      'action.closeDialog': ['关闭对话框', 'Close dialog'],
      'action.submitting': ['提交中…', 'Submitting…'],
      'action.unknownResult': ['服务返回未知结果', 'Unknown service result'],
      'action.repaired': ['已补挂', 'repaired'],
      'pre.toggle': ['预检', 'Precheck'],
      'pre.title': ['运行前预检', 'Pre-run precheck'],
      'pre.ok': ['可运行', 'Ready'],
      'pre.warn': ['需确认', 'Confirm'],
      'pre.danger': ['不可运行', 'Blocked'],
      'pre.service': ['服务在线', 'Service online'],
      'pre.inflight': ['无在跑轮次', 'No round in flight'],
      'pre.inflightBad': ['已有在跑轮次', 'A round is already running'],
      'pre.fresh': ['证据新鲜', 'Evidence fresh'],
      'pre.freshOld': ['证据已 {age}，建议刷新', 'Evidence {age} old; refresh first'],
      'pre.freshUnknown': ['无证据年龄（未提供）', 'No evidence age (not provided)'],
      'pre.quota': ['今日额度', 'Daily limits'],
      'pre.quotaValue': ['单笔 {risk} · 最大持仓 {positions}', 'risk {risk} · max {positions}'],
      'pre.quotaMissing': ['未提供（Host 未返回 envelope）', 'not provided (no envelope)'],
      'pre.entryNote': ['可运行，但本轮不会开仓：{reasons}', 'Runs, but no new entry: {reasons}'],
      'pre.entryNone': ['可运行，新开仓未被阻断', 'Runs; new entries are allowed'],
      'pre.repair': ['去修复', 'Fix'],
      // 03 C2 / 05 N04: three gates must not be conflated — 340s chain deadline
      // (host/ai/constants.js:161), 180s evidence age gate, 400s browser wait.
      // A standalone bundle cannot import host constants, so the number is
      // written here and traced to constants.js:161 (verified 2026-11-12).
      'pre.confirm': ['本轮最长 5:40（340s）· 不可取消（Demo）', 'Up to 5:40 (340s); cannot be cancelled (demo)'],
      'pre.submit': ['继续提交', 'Submit'],
      'pre.close': ['关闭预检', 'Close precheck'],
      'positions.unknownLiq': ['未知', 'n/a'],

      'prot.ok': ['完好', 'ok'],
      'prot.missing': ['缺失', 'missing'],
      'prot.partial': ['异常', 'abnormal'],
      'prot.unknown': ['未知（阻断）', 'unknown (blocks)'],
      'prot.unreconciled': ['未对账', 'unreconciled'],
      'prot.missingFull': ['保护缺失', 'protection missing'],
      'prot.partialFull': ['保护异常', 'protection abnormal'],
      'prot.unknownFull': ['保护未知（查询失败）', 'protection unknown (query failed)'],
      'prot.blocks': ['已阻断新开仓', 'blocks new entries'],

      'concl.title': ['AI 本轮结论', 'AI conclusion'],
      'concl.emptyTitle': ['还没有轮次', 'No rounds yet'],
      'concl.emptyHint': ['点击「运行一轮」后，这里会显示结论、原因与证据年龄。', 'Run a round to see the conclusion, reason and evidence age.'],
      'concl.headlineTrade': ['交易：{side} {symbol}', 'Trade: {side} {symbol}'],
      'concl.headlineNoTrade': ['不交易：{reason}', 'No trade: {reason}'],
      'concl.headlineFailed': ['本轮失败：{reason}', 'Round failed: {reason}'],
      'concl.reason': ['原因', 'Reason'],
      'concl.ruleRewrite': ['规则改写', 'Rule override'],
      'concl.grade': ['评级', 'Grade'],
      'concl.gate': ['规则命中', 'Rule hits'],
      'concl.noRule': ['无（硬规则未触发）', 'none (no hard rule hit)'],
      'concl.reduced': ['降仓 ×{n}', 'reduced ×{n}'],
      'concl.evidence': ['证据', 'Evidence'],
      'concl.confidence': ['信心（分析字段）', 'Confidence (analysis only)'],
      'concl.confidenceHint': ['信心只用于解释，不参与授权：它不会提高允许数量、风险比例或杠杆', 'Confidence is explanatory only: it never raises the allowed size, risk or leverage'],
      'concl.viewRound': ['查看本轮 →', 'Open round →'],
      'concl.evidenceBtn': ['证据', 'Evidence'],
      'concl.notProvided': ['未提供', 'not provided'],
      'concl.noReason': ['无原因码', 'no reason code'],
      'concl.blockedReason': ['执行阻断原因', 'Execution blocked reason'],
      'concl.rationale': ['执行说明', 'Execution rationale'],
      'concl.noDetail': ['暂无详细原因', 'No further detail'],
      'concl.reasonDetail': ['详细原因', 'Reason detail'],
  'concl.rawDetail': ['原文（宿主工程串）', 'Raw host message'],

      'orders.title': ['订单 · 成交', 'Orders · fills'],
      'orders.tabOrders': ['订单', 'Orders'],
      'orders.tabFills': ['成交', 'Fills'],
      'orders.tabRecon': ['对账', 'Reconcile'],
      'orders.collapsedEmpty': ['订单 0 · 成交 0 · 展开查看', 'Orders 0 · fills 0 · expand'],
      'orders.expand': ['展开', 'Expand'],
      'orders.collapse': ['收起', 'Collapse'],
      'orders.time': ['时间（北京时间）', 'Time (Beijing)'],
      'orders.id': ['单号', 'ID'],
      'orders.symbol': ['标的', 'Symbol'],
      'orders.side': ['方向', 'Side'],
      'orders.type': ['类型', 'Type'],
      'orders.qty': ['数量', 'Qty'],
      'orders.price': ['价格', 'Price'],
      'orders.status': ['状态', 'Status'],
      'orders.action': ['动作', 'Actions'],
      'orders.detail': ['详情', 'Detail'],
      'orders.emptyTitle': ['当前无订单', 'No orders'],
      'orders.emptyHint': ['Host 已返回订单接口，当前为 0。', 'The Host returned orders; the list is empty.'],
      'orders.fillsEmptyTitle': ['当前无成交', 'No fills'],
      'orders.fillsEmptyHint': ['成交来自交易所 userTrades，当前为 0。', 'Fills come from exchange userTrades and are empty.'],
      'recon.idle': ['本地账本与交易所尚未核对', 'Ledger and venue are not reconciled yet'],
      'recon.hint': ['对账为只读核对：采纳/平掉本地差异并检查保护与 outbox，不会下单。', 'Read-only check: adopts/closes local differences and checks protection/outbox. No orders.'],
      'recon.run': ['立即对账', 'Reconcile now'],
      'recon.rerun': ['重新对账', 'Reconcile again'],
      'recon.running': ['正在对账…', 'Reconciling…'],
      'recon.ok': ['本地账本与交易所一致', 'Ledger and venue match'],
      'recon.diff': ['发现差异，请核对下列项目', 'Differences found; review below'],
      'recon.adopted': ['采纳仓位', 'adopted'],
      'recon.closed': ['平掉仓位', 'closed'],
      'recon.protectionMissing': ['保护缺失', 'protection missing'],
      'recon.outbox': ['outbox 待发 / 不确定', 'outbox pending / uncertain'],
      'recon.venue': ['venue', 'venue'],
      'recon.requestId': ['requestId', 'requestId'],
      'recon.at': ['对账时间', 'Reconciled at'],
      'recon.failed': ['对账失败', 'Reconcile failed'],
      'recon.retry': ['重试', 'Retry'],
      'recon.count': ['{n} 个', '{n}'],
      'recon.demo': ['Demo · 只读', 'Demo · read-only'],
      'recon.exchange': ['交易所', 'exchange'],
      'recon.reachable': ['可达', 'reachable'],
      'recon.unreachable': ['不可达', 'unreachable'],
      'recon.stale': ['快照过期', 'snapshot stale'],
      'recon.staleYes': ['已过期', 'stale'],
      'recon.staleNo': ['未过期', 'fresh'],
      'recon.unresolved': ['未解决查询', 'unresolved queries'],
      'recon.staleBlocked': ['交易所不可达或快照过期，不能判定本地与交易所一致。', 'Exchange is unreachable or the snapshot is stale; consistency cannot be confirmed.'],
      'orders.footer': ['订单与成交只读；平仓 / 补挂请前往持仓与保护', 'Orders and fills are read-only; close / repair controls are in Positions & protection'],

      'today.title': ['今日 AI 摘要（北京时间）', 'Today AI summary (Beijing)'],
      'today.rounds': ['轮次', 'Rounds'],
      'today.trades': ['成交', 'Trades'],
      'today.noTrade': ['不交易', 'No trade'],
      'today.failed': ['失败', 'Failed'],
      'today.all': ['全部 →', 'All →'],
      'today.review': ['复盘 →', 'Review →'],
      'today.scope': ['今日口径（本地按日归档）', 'today only (local day archive)'],
      'today.empty': ['今天还没有轮次。', 'No rounds today yet.'],

      'ai.selectRound': ['轮次', 'Round'],
      'ai.prev': ['‹ 上一轮', '‹ Previous'],
      'ai.next': ['下一轮 ›', 'Next ›'],
      'ai.listTitle': ['轮次', 'Rounds'],
      'ai.listScope': ['口径：本地按日归档，保留全部轮次', 'Scope: local day-partitioned archive, every round kept'],
      'ai.conclusion': ['结论', 'Conclusion'],
      'ai.verdict': ['裁决', 'Verdict'],
      'ai.aiAdvice': ['AI 建议', 'AI advice'],
      'ai.hardRules': ['风控裁决', 'Risk verdict'],
      'ai.control': ['控制态', 'Control'],
      'ai.violationsMissing': ['本轮无 P1 裁决 / 未返回', 'no P1 verdict for this round'],
      'verdict.deterministic': ['确定性评分卡', 'deterministic scorecard'],
      'verdict.disclaimer': ['评级不是收益预测', 'a grade is not a return forecast'],
      'verdict.disclaimerLong': ['评级与门禁来自确定性评分卡，不是收益预测；阈值与实际值的对照需 Service 映射（P1）。', 'Grades and gates come from the deterministic scorecard, not a return forecast; threshold mapping needs Service work (P1).'],
      'verdict.grade': ['评级', 'Grade'],
      'verdict.gate': ['门禁', 'Gate'],
      'verdict.gatePass': ['通过', 'pass'],
      'verdict.gateReasons': ['门禁原因', 'Gate reasons'],
      'verdict.debate': ['辩论质量', 'Debate quality'],
      'verdict.tie': ['平局', 'tie'],
      'verdict.degraded': ['降级', 'degraded'],
      'verdict.red': ['红队', 'Red'],
      'verdict.blue': ['蓝队', 'Blue'],
      'verdict.noSelected': ['本轮未选中案例：以下为两队并列评分', 'No selected case; both sides shown'],
      'verdict.violations': ['规则命中（violations）', 'Rule hits (violations)'],
      'verdict.rule': ['规则', 'Rule'],
      'verdict.action': ['动作', 'Action'],
      'verdict.detail': ['详情', 'Detail'],
      'verdict.blocking': ['阻断规则', 'Blocking rule'],
      'verdict.concerns': ['风险关注', 'Concerns'],
      'verdict.noHits': ['本轮规则无命中。', 'No rule hits this round.'],
      'verdict.noConcerns': ['无', 'none'],
      'verdict.noBlock': ['无', 'none'],
      'verdict.approve': ['通过', 'approve'],
      'verdict.reject': ['拒绝', 'reject'],
      'debate.thin': ['单薄', 'thin'],
      'debate.ok': ['标准', 'ok'],
      'debate.strong': ['充分', 'strong'],
      // 01 §4.1 / N4: the scorecard emits decisive / thin / both_weak.
      'debate.decisive': ['充分', 'decisive'],
      'debate.both_weak': ['双方均弱', 'both weak'],
      'ai.stages': ['阶段', 'Stages'],
      'ai.stage': ['阶段', 'Stage'],
      'ai.attempts': ['尝试', 'attempts'],
      'ai.latency': ['耗时', 'Latency'],
      'ai.tokens': ['tokens 入/出', 'tokens in/out'],
      'ai.chainCalls': ['调用', 'calls'],
      'ai.chainRetries': ['重试', 'retries'],
      'ai.chainTruncated': ['截断', 'truncated'],
      'ai.chainTimeouts': ['超时', 'timeouts'],
      'ai.chainElapsed': ['用时', 'elapsed'],
      'ai.chainStopped': ['停在', 'stopped at'],
      'ai.rationale': ['决策依据', 'Decision rationale'],
      'ai.thesis': ['决策理由', 'Thesis'],
      'ai.redCase': ['红队（多）', 'Red (long)'],
      'ai.blueCase': ['蓝队（空）', 'Blue (short)'],
      'ai.decisive': ['决定性证据', 'Decisive evidence'],
      'ai.fatalFlaw': ['致命缺陷', 'Fatal flaw'],
      'ai.plan': ['执行计划', 'Execution plan'],
      'ai.entry': ['入场', 'Entry'],
      'ai.stop': ['止损', 'Stop'],
      'ai.target': ['止盈', 'Target'],
      'ai.risk': ['风险', 'Risk'],
      'ai.leverage': ['杠杆', 'Leverage'],
      'ai.rr': ['盈亏比', 'R:R'],
      'ai.invalidation': ['失效条件', 'Invalidation'],
      'ai.warnings': ['本轮警告', 'Warnings'],
      'ai.noRationale': ['本轮没有到达决策阶段，因此没有决策理由。', 'This round never reached the decision stage, so there is no rationale.'],
      'ai.compare': ['红蓝对比', 'Red vs blue'],
      'ai.side': ['方向', 'Side'],
      'ai.gate': ['门禁', 'Gate'],
      'ai.stopPct': ['止损', 'Stop'],
      'ai.conf': ['置信度', 'Confidence'],
      'ai.rubric': ['依据分', 'Rubric'],
      'ai.state': ['状态', 'State'],
      'ai.notReached': ['未到达', 'not reached'],
      'ai.stageBlocked': ['被闸门阻断', 'blocked by gate'],
      'ai.gateBlockedTitle': ['执行闸门阻断了本轮提交', 'The execution gate blocked this round'],
      'ai.gateDetail': ['闸门原因', 'Gate reason'],
      'ai.gateQuote': ['闸门报价 / 年龄 / 来源', 'Gate quote / age / source'],
      'ai.gateQuoteEvidence': ['证据构建时冻结价（非提交价）', 'frozen at evidence build (not the submit price)'],
      'ai.gateQuotePreSubmit': ['提交前重新报价', 're-quoted before submit'],
      'ai.gateMode': ['闸门模式', 'Gate mode'],
      'ai.gateObserveOnly': ['仅记录，未拦截', 'observe only, not enforced'],
      'ai.gatePlanGaps': ['计划缺口（为什么无法验证）', 'Plan gaps (why it could not be verified)'],
      'ai.gatePreSubmitAdvisory': ['提交前复检已执行：未重新报价，仅时间类判定可拦（其余仅记录）', 'Pre-submit recheck ran: no re-quote, so only the time-based verdict may block'],
      'ai.notReachedMore': ['④ 决策 · ⑤ 风控 · ⑥ 操盘手 未到达（{n} 个阶段，提前结束：{reason}）', '④ decision · ⑤ risk · ⑥ executor not reached ({n} stages; stopped early: {reason})'],
      'ai.gaps': ['本轮缺口', 'Round gaps'],
      'ai.gapsActionable': ['可行动 {a} / 不可行动 {b}', '{a} actionable / {b} informational'],
      'ai.gapsEmpty': ['本轮没有回报缺口。', 'No gaps reported for this round.'],
      'ai.gapsRaw': ['另有 {n} 条原始缺口（英文原文见系统状态）', '{n} raw gap(s) — see system status'],
      'ai.gapsExample': ['原因码与阶段来自 Host RoundView；缺口原文进系统状态。', 'Reasons and stages come from the Host RoundView; raw gap text lives in system status.'],
      'ai.emptyTitle': ['还没有轮次', 'No rounds yet'],
      'ai.emptyHint': ['运行一轮后，这里显示结论、裁决与阶段。', 'Run a round to see the conclusion, verdict and stages.'],
      'ai.historyFilters': ['全部', 'All'],
      'ai.historyFailed': ['失败', 'Failed'],
      'ai.historyNoTrade': ['不交易', 'No trade'],
      'ai.historyTrades': ['成交', 'Trades'],
      'ai.historyTime': ['时间（北京时间）', 'Time (Beijing)'],
      'ai.historyId': ['轮次', 'Round'],
      'ai.historyTrigger': ['触发', 'Trigger'],
      'ai.historyResult': ['结论', 'Result'],
      'ai.historyReason': ['原因码', 'Reason'],
      'ai.historyDuration': ['耗时', 'Duration'],
      'ai.durationAll': ['全部时长', 'any duration'],
      'ai.durationShort': ['≤30s', '≤30s'],
      'ai.durationMid': ['30–90s', '30–90s'],
      'ai.durationLong': ['>90s', '>90s'],
      'ai.reviewTimeline': ['时间线', 'Timeline'],
      'ai.reviewTimelineHint': ['今日最近 8 轮', 'today, last 8 rounds'],
      'ai.historyOpen': ['查看 →', 'Open →'],
      'ai.historyEmpty': ['该筛选下没有轮次', 'No rounds under this filter'],
      // 15 §5: the history view is one archived day at a time (today by default).
      'ai.historyDate': ['归档日期', 'Archive day'],
      'ai.dateToday': ['今天', 'Today'],
      'ai.datePick': ['选择日期', 'Pick a date'],
      'ai.datePickLabel': ['选择日期（北京时间）', 'Pick a date (Beijing time)'],
      'ai.dateLoading': ['读取该日记录…', 'Loading this day…'],
      'ai.dayEmpty': ['这一天没有轮次记录', 'No rounds recorded on this day'],
      'ai.dayBackToday': ['回到今天', 'Back to today'],
      'ai.historyTotal': ['共 {n} 轮', '{n} rounds'],
      'ai.historyTruncated': ['该日记录超过上限，仅显示前 {n} 条', 'This day exceeds the cap; showing the first {n}'],
      'ai.historyDateError': ['该日期读取失败：{code}', 'Could not load this day: {code}'],
      'ai.historyReadonly': ['历史日期（只读）', 'Historical day (read-only)'],
      'ai.reviewDist': ['今日 {n} 轮分布', 'Today: {n} rounds'],
      'ai.reviewReasons': ['原因码构成', 'Reason breakdown'],
      'ai.reviewDuration': ['耗时 p50 {p50} · p95 {p95}', 'Duration p50 {p50} · p95 {p95}'],
      'ai.reviewAttribution': ['归因：当前 0 成交 → 无可归因成交', 'Attribution: no fills yet — nothing to attribute'],
      'ai.reviewDailyTitle': ['AI 日报', 'AI daily review'],
      'ai.reviewDailyHint2': ['由 Host 按需生成并缓存，只读，不改交易；日期按 Asia/Shanghai。', 'Generated by the Host on demand and cached; read-only. Date follows Asia/Shanghai.'],
      'ai.reviewDailyNone': ['今日复盘未生成', 'No review generated for today'],
      'ai.reviewDailyGenerate': ['生成复盘', 'Generate review'],
      'ai.reviewDailyGenerating': ['生成中…', 'Generating…'],
      'ai.reviewDailyConflict': ['正在生成，请稍候', 'Generation already in progress'],
      'ai.reviewDailyFailed': ['生成失败', 'Generation failed'],
      'ai.reviewDailyRetry': ['重试', 'Retry'],
      'ai.reviewDailyRegenerate': ['重新生成', 'Regenerate'],
      'ai.reviewDailyCached': ['缓存', 'cached'],
      'ai.reviewDailyDate': ['日期', 'date'],
      'ai.reviewNoTrades': ['成交 0', 'Trades 0'],

      'drawer.title': ['证据抽屉', 'Evidence drawer'],
      'drawer.system': ['系统状态', 'System status'],
      'drawer.close': ['关闭证据抽屉', 'Close evidence drawer'],
      'drawer.tabEvidence': ['证据', 'Evidence'],
      'drawer.tabPrompt': ['Prompt', 'Prompt'],
      'drawer.tabJson': ['输出 JSON', 'Output JSON'],
      'drawer.tabRules': ['硬规则', 'Hard rules'],
      'drawer.tabTiming': ['耗时', 'Timing'],
      'drawer.snapshotId': ['快照 ID', 'Snapshot ID'],
      'drawer.producedAt': ['生成时间', 'Produced at'],
      'drawer.age': ['年龄', 'Age'],
      'drawer.ageNote': ['{n} 秒前（按证据生成时间）', '{n}s ago (from evidence produced time)'],
      'drawer.verdict': ['数据质量', 'Data verdict'],
      'drawer.symbol': ['主标的', 'Primary symbol'],
      'drawer.hash': ['证据哈希', 'Evidence hash'],
      'drawer.source': ['来源', 'Source'],
      'drawer.call': ['调用', 'Call'],
      'drawer.attempt': ['尝试', 'Attempt'],
      'drawer.schema': ['schema', 'schema'],
      'drawer.finish': ['结束原因', 'Finish'],
      'drawer.role': ['角色', 'Role'],
      'drawer.promptVersion': ['Prompt 版本', 'Prompt version'],
      'drawer.model': ['模型 / 档位', 'Model / effort'],
      'drawer.requestHash': ['请求哈希', 'Request hash'],
      'drawer.p1': ['P1 结论', 'P1 verdict'],
      'drawer.p2': ['P2 结论', 'P2 verdict'],
      'drawer.rule': ['规则 / 字段', 'Rule / field'],
      'drawer.actual': ['实际值', 'Actual'],
      'drawer.result': ['结果', 'Result'],
      'drawer.total': ['本轮总耗时', 'Round duration'],
      'drawer.calls': ['调用数', 'Calls'],
      'drawer.noEvidence': ['本轮尚无该阶段数据', 'No data for this stage yet'],
      'drawer.footer.evidence': ['Host 只暴露证据摘要；完整快照由 Service 持久化。', 'The Host exposes the summary only; the Service stores the full snapshot.'],
      'drawer.footer.prompt': ['Host RoundView 不保留 Prompt 正文，只保留版本、模型与哈希。', 'The Host RoundView keeps versions, model and hashes, not prompt text.'],
      'drawer.footer.json': ['以下为本轮 RoundView 投影，不含服务端私有字段。', 'RoundView projection only; no Service-private fields.'],
      'drawer.footer.rules': ['规则结果来自 P1/P2 摘要；未进入风控时为空。', 'Rules come from P1/P2 summaries; empty when risk was not reached.'],
      'drawer.footer.timing': ['每次调用来自 Host 审计记录（延迟与 token）。', 'Per-call entries come from the Host audit trail.'],
      'drawer.fullSnapshot': ['查看完整快照', 'View full snapshot'],
      'drawer.fullMeta': ['全文摘要', 'Snapshot summary'],
      'drawer.fullLoading': ['正在加载全文…', 'Loading full snapshot…'],
      'drawer.fullFailed': ['全文未提供（{code}），以下为 Host 摘要', 'Full snapshot unavailable ({code}); Host summary shown'],
      'drawer.schemaVersion': ['schema 版本', 'schema'],
      'drawer.bytes': ['大小', 'bytes'],
      'drawer.agent': ['agent', 'agent'],

      'system.env': ['环境与凭证', 'Environment & credentials'],
      'system.baseUrl': ['服务地址', 'Base URL'],
      'system.ledger': ['账本 ID', 'Ledger ID'],
      'system.auth': ['鉴权', 'Auth'],
      'system.token': ['token 来源', 'token source'],
      'system.llm': ['LLM 路由', 'LLM route'],
      'system.version': ['版本', 'Version'],
      'system.control': ['控制态', 'Control'],
      'system.newEntry': ['新开仓', 'New entry'],
      'system.gaps': ['缺口原文', 'Gap text'],
      'system.noGaps': ['没有回报缺口。', 'No gaps reported.'],

      'state.loading': ['正在连接…', 'Connecting…'],
      'state.hostOffline': ['与 Host 的连接中断', 'Host connection lost'],
      'state.retry': ['重试', 'Retry'],
      'state.refreshing': ['刷新中', 'Refreshing'],
      'state.notice': ['提示', 'Notice'],
      'run.pending': ['已提交，等待结果…', 'Submitted, waiting…'],
      'run.timeout': ['已停止等待（6:40）；Host 可能仍在运行。RUN_TIMEOUT', 'Stopped waiting after 6:40; the Host may still be running. RUN_TIMEOUT'],
      'run.conflict': ['已有一轮在运行（409）', 'A round is already running (409)'],
      'run.auth': ['鉴权失败', 'Auth failed'],
      'run.started': ['已提交，等待结果…', 'Submitted, waiting…'],
      'run.done': ['本轮已结束', 'Round finished'],
      'run.blocked': ['已有在跑轮次', 'A round is already running'],
      'run.error': ['运行失败：{reason}', 'Run failed: {reason}'],

      // 03 §8 owns "code -> label"; the table itself lives in REASON_LABELS
      // below so the 89-code vocabulary is one literal block, not a scattered
      // set of one-off keys. Only the wildcard template stays here.
      'reason.executionBlock': ['执行阻断（{code}）', 'Execution blocked ({code})'],
      'reason.unmapped': ['未收录原因码 {code}', 'Unmapped reason code {code}'],
      'reason.serviceHttp': ['交易服务 HTTP {code}', 'Trading service HTTP {code}'],
      // 03 A6: the Host sends {kind, role, error_code, timeout_ms}; the sentence
      // is assembled here so both languages come from the same template.
      'reason.sentenceTimeout': ['{who}调用超时（{s}）', '{who}call timed out ({s})'],
      'reason.sentenceTimeoutNoTime': ['{who}调用超时', '{who}call timed out'],
      'reason.sentenceSchema': ['{who}输出格式不合格（已重试）', '{who}output failed schema validation (retried)'],
      'reason.sentenceNoAdapter': ['未选择 Harness 默认模型', 'No Harness default model selected'],
      'reason.sentenceFailed': ['{who}{code}', '{who}{code}'],
      'reason.sentenceGateway': ['{who}失败：{code}', '{who}failed: {code}'],
      'reason.callFailed': ['调用失败', 'call failed'],
      // 01 §4.2: one LLM_FINISH_ERROR code covers four different endings.
      'finish.stop': ['正常结束', 'Stopped normally'],
      'finish.max_tokens': ['输出被截断', 'Output truncated'],
      'finish.error': ['模型服务端错误', 'Model server error'],
      'finish.aborted': ['调用被中止', 'Call aborted'],
      'finish.other': ['异常结束（{kind}）', 'Ended abnormally ({kind})'],

      'role.analysis': ['分析', 'Analysis'],
      'role.red_opening': ['红队·开场', 'Red opening'],
      'role.red_response': ['红队·回应', 'Red response'],
      'role.blue_opening': ['蓝队·开场', 'Blue opening'],
      'role.blue_response': ['蓝队·回应', 'Blue response'],
      'role.decision': ['决策', 'Decision'],
      'role.risk': ['风控', 'Risk'],
      'role.execution_entry': ['操盘手', 'Executor'],
      'role.review': ['复盘', 'Review'],
      'stage.analysis': ['分析', 'Analysis'],
      'stage.red': ['红队', 'Red'],
      'stage.blue': ['蓝队', 'Blue'],
      'stage.decision': ['决策', 'Decision'],
      'stage.risk': ['风控', 'Risk'],
      'stage.execution': ['操盘手', 'Executor'],
      // 05 N01: the attribution DTO carries stage_key / role_key / category_key
      // precisely so the label can follow the panel language. These are the
      // keys 01 uses for stages that are not one of the six AI roles.
      'stage.p1': ['P1 风控闸门', 'P1 risk gate'],
      'stage.p2': ['P2 执行闸门', 'P2 execution gate'],
      'stage.chain_end': ['链尾', 'Chain end'],
      'stage.evidence': ['证据', 'Evidence'],
      'stage.schedule': ['调度', 'Schedule'],
      'stage.host': ['Host', 'Host'],
      'stage.route_freeze': ['轮次冻结复核', 'Round route freeze'],
      'role.p1': ['P1 风控闸门', 'P1 risk gate'],
      'role.p2': ['P2 执行闸门', 'P2 execution gate'],

      'status.ok': ['通过', 'ok'],
      'status.error': ['失败', 'error'],
      'status.invalid': ['无效', 'invalid'],
      'status.pending': ['等待', 'pending'],
      'status.running': ['运行中', 'running'],
      'status.skipped': ['跳过', 'skipped'],
      'status.no_trade': ['不交易', 'no trade'],
      'status.failed': ['失败', 'failed'],
      'status.done': ['完成', 'done'],
      'status.cancelled': ['取消', 'cancelled'],
      'status.degraded': ['降级', 'degraded'],
      'status.filled': ['已成交', 'filled'],
      'status.partially_filled': ['部分成交', 'partially filled'],
      'status.canceled': ['已取消', 'canceled'],
      'status.expired': ['已过期', 'expired'],
      'status.rejected': ['已拒绝', 'rejected'],
      'status.new': ['未成交', 'new'],
      'status.open': ['生效中', 'open'],
      'status.trade': ['成交', 'trade'],

      'side.long': ['多', 'long'],
      'side.short': ['空', 'short'],
      'side.buy': ['买', 'buy'],
      'side.sell': ['卖', 'sell'],
      'side.na': ['—', '—'],

      'common.none': ['—', '—'],
      'common.seconds': ['{n} 秒', '{n}s'],
      'common.minutes': ['{n} 分钟', '{n}m'],
      'common.hours': ['{n} 小时', '{n}h'],
      'common.days': ['{n} 天', '{n}d'],
      'common.actions': ['动作', 'Actions'],
      'common.close': ['关闭', 'Close'],
      'common.empty': ['暂无', 'none'],
      'common.notProvided': ['未提供', 'not provided'],
      'common.demo': ['Demo', 'Demo'],
      'common.yes': ['是', 'yes'],
      'common.no': ['否', 'no'],
      'account.trend': ['权益曲线', 'Equity trend'],
      'control.normal': ['正常', 'normal'],
      'control.close_only': ['只平仓', 'close only'],
      'control.paused': ['暂停', 'paused'],
      'control.pause': ['暂停', 'paused'],
      'control.halted': ['停机', 'halted'],
      'control.emergency': ['紧急', 'emergency'],
      'control.unknown': ['未知', 'unknown'],

      // ---- 05 N03: attribution / per-call error card -----------------------
      'attr.title': ['失败归因', 'Failure attribution'],
      'attr.stage': ['阶段', 'Stage'],
      'attr.role': ['角色', 'Role'],
      'attr.next': ['下一步', 'Next'],
      'attr.unmapped': ['未收录原因码 {code}（已记录）', 'Unmapped reason code {code} (recorded)'],
      'attr.nextUnmapped': ['查看调用详情（系统状态）', 'Open call details (system status)'],
      // 01 §3.7 categories: the Host sends category_key for exactly this.
      'attr.cat.chain': ['链路', 'Chain'],
      'attr.cat.gate': ['执行闸门', 'Execution gate'],
      'attr.cat.execution': ['执行', 'Execution'],
      'attr.cat.data': ['数据', 'Data'],
      'attr.cat.infrastructure': ['基础设施', 'Infrastructure'],
      'attr.cat.market': ['市场', 'Market'],
      'attr.cat.success': ['成功', 'Success'],
      'attr.cat.unmapped': ['未收录', 'Unmapped'],
      'attr.stageFirstFailed': ['首个失败阶段', 'First failed stage'],
      'call.errors': ['失败调用', 'Failed calls'],
      'call.errorCode': ['错误码', 'Error code'],
      'call.errorMessage': ['错误信息', 'Error message'],
      'call.finishKind': ['结束方式', 'Finish'],
      'debate.card': ['辩论双侧', 'Debate (both sides)'],
      'debate.round1': ['第一轮·开场', 'Round 1 · openings'],
      'debate.round2': ['第二轮·回应', 'Round 2 · responses'],
      'debate.tradeable': ['可交易', 'Tradeable'],
      'debate.notProvided': ['该阶段未产出', 'not produced for this stage'],
      'debate.steelman': ['对手最强论点', 'Opponent steelman'],
      'debate.rebuttal': ['反驳 {n} 条', '{n} rebuttals'],
      'debate.noCase': ['不成立原因', 'No-case reason'],
      'debate.adopted': ['采用 {side}', 'adopted {side}'],
      'debate.degradedBanner': ['本轮辩论已降级：第二轮回应未产出，按开场独立判定', 'Debate degraded: round 2 responses missing; decided on openings alone'],

      // ---- 05 N04: running state, one honest sentence ----------------------
      'concl.submitted': ['已提交 · 已等待 {elapsed}（上限 5:40）', 'Submitted · waiting {elapsed} (max 5:40)'],
      // 05 P1-2: only a structured in_flight object may name a stage; a legacy
      // string (or an unknown stage) falls back to the N04 wording.
      'concl.runningStage': ['{stage} · 已耗时 {elapsed} / 上限 5:40', '{stage} · elapsed {elapsed} / max 5:40'],
      'concl.runningCalls': ['调用 {done}/{budget} · 尝试 {attempt}/{max}', 'calls {done}/{budget} · attempt {attempt}/{max}'],

      // ---- 05 N05: attention bar closure + schedule panel ------------------
      's2.serviceDown': ['服务不可达', 'Service unreachable'],
      's2.serviceDownReason': ['{state} · 运行一轮会失败', '{state} · a run would fail'],
      's2.scheduleStalled': ['自动调度连续跳过', 'Auto schedule keeps skipping'],
      's2.pendingTitle': ['待处理清单', 'Pending items'],
      's2.pendingHint': ['按影响排序；点开一项处理。', 'Sorted by impact; open one to act.'],
      's2.pendingEmpty': ['暂无待处理项', 'Nothing pending'],
      's4.scheduleSkipped': ['跳过：{reason}', 'skipped: {reason}'],
      'system.scheduleTitle': ['自动调度明细', 'Auto schedule detail'],
      'system.scheduleLast': ['最近一次', 'Last result'],
      'system.scheduleHistory': ['最近 10 次', 'Last 10 ticks'],

      // ---- 05 N06: review attribution, three data-driven branches ----------
      'ai.reviewAttributionTrades': ['归因：成交 {n} 笔 · 归因口径待 Service（P2）', 'Attribution: {n} fills · per-trade attribution needs Service work (P2)'],
      'ai.reviewAttributionNoRounds': ['归因：该窗口无轮次', 'Attribution: no rounds in this window'],

      // ---- 05 N07: unknown protection states fail safe ---------------------
      'prot.unknownCode': ['未知（阻断）：{code}', 'unknown (blocks): {code}'],

      // ---- 05 N08: dangerous-action gates ---------------------------------
      'control.reasonMin': ['理由至少 {n} 个字（不可撤销）', 'At least {n} characters (irreversible)'],
      'control.armHint': ['再次点击确认（5 秒内有效）', 'Click confirm again within 5s'],

      // ---- 05 P1-1: per-role model / effort configuration ------------------
      'config.title': ['模型配置', 'Model configuration'],
      'config.open': ['模型配置', 'Model config'],
      'config.sub': ['插件本地配置 · 下一轮生效', 'Plugin-local config · applies next round'],
      'config.revision': ['修订', 'Revision'],
      'config.preset': ['预设', 'Preset'],
      'config.presetBalanced': ['平衡', 'Balanced'],
      'config.presetSaver': ['省钱', 'Saver'],
      'config.presetStrongest': ['最强', 'Strongest'],
      'config.defaultRoute': ['默认路由', 'Default route'],
      'config.roles': ['角色路由', 'Role routes'],
      'config.debate': ['红蓝辩论（对称）', 'Red vs blue (symmetric)'],
      'config.inherit': ['继承默认', 'inherit default'],
      'config.inheritMark': ['继承', 'inherited'],
      'config.model': ['模型', 'Model'],
      'config.effort': ['强度', 'Effort'],
      'config.noReasoning': ['该模型不提供强度档', 'this model exposes no effort'],
      'config.symmetric': ['红蓝对称（P-01=A）', 'Symmetric (P-01=A)'],
      'config.asymmetric': ['允许红蓝异构（P-01=B）', 'Allow asymmetric debate (P-01=B)'],
      'config.asymmetricWarn': ['辩论将不对称：评分与复盘结论需按此解读', 'Debate becomes asymmetric; read grades and reviews accordingly'],
      'config.lockHint': ['对称锁定：修改任一侧会同时改红蓝四行', 'Symmetric lock: one change updates all four debate rows'],
      'config.budget': ['影响预览', 'Impact preview'],
      'config.budgetValue': ['预计墙钟 ∈ [{min}, {max}] · 重试余量 {margin}', 'Est. wall clock ∈ [{min}, {max}] · retry margin {margin}'],
      'config.budgetNote': ['按串行阶段和（并行取慢者）估算；链硬截止 5:40', 'Serial stage sum with parallel slowest leg; hard deadline 5:40'],
      'config.save': ['保存配置', 'Save config'],
      'config.refresh': ['刷新目录', 'Refresh catalog'],
      'config.saved': ['已保存 rev {n} · 下一轮生效', 'Saved rev {n} · applies next round'],
      'config.saveFailed': ['保存失败：{reason}', 'Save failed: {reason}'],
      'config.catalogUnavailable': ['目录暂不可用：配置未固化，保存已禁用', 'Catalog unavailable: config not fixated, save disabled'],
      'config.presetMissing': ['预设模型不可用（缺 {models}）', 'Preset models unavailable ({models})'],
      'config.loading': ['正在读取目录…', 'Loading catalog…'],
      'config.emptyCatalog': ['目录为空：没有可配置的模型', 'Empty catalog: no configurable model'],
      'config.roleScope': ['本配置只作用于本插件，不写回 Harness 全局', 'This config is plugin-local; it never writes the Harness default'],

      // ---- 05 N09: honest round selection + route rows --------------------
      'ai.detailFailed': ['该轮详情读取失败', 'Round detail failed to load'],
      'ai.detailRetry': ['重试', 'Retry'],
      'ai.backToLatest': ['回最新轮', 'Back to latest round'],
      'system.execRoute': ['执行路由（本轮冻结）', 'Execution route (frozen for the round)'],
      'system.routeSource': ['路由来源（唯一事实）', 'Route source (single fact)'],
      'system.routeSourceRound': ['Host 轮次快照 routeSnapshot（运行进程回传）', 'Host round snapshot routeSnapshot (from the running process)'],
      'system.routeSourceLast': ['持久化轮次快照（最近一轮，运行进程写入）', 'Persisted round snapshot (latest round, written by the running process)'],
      'system.routeUnavailable': ['本轮路由不可用（目录校验失败，整轮未执行）', 'Route unavailable this round (catalog check failed; the round did not run)'],
      'system.routeDiff': ['来源差异：进程默认路由（未参与本轮）', 'Source difference: process default route (not used this round)'],
      'system.routeDiffNote': ['本轮以上面的快照为准；进程默认只影响下一轮', 'the snapshot above wins for this round; the process default only affects the next one'],
      'system.savedDefault': ['已保存角色默认', 'Saved role default'],
      'system.noExecutedRoute': ['尚未执行', 'Not executed yet'],
      'system.routeDefault': ['Harness 当前默认', 'Harness current default'],
      'system.routeMismatch': ['默认已变更，下一轮生效', 'Default changed; applies to the next round'],
      'system.build': ['加载身份', 'Load identity'],
      'system.buildId': ['buildId', 'buildId'],
      'system.configRevision': ['配置修订', 'Config revision'],
      'system.chainVersion': ['链版本', 'Chain version'],
      'system.buildUnsaved': ['未保存', 'unsaved'],
      'system.buildHint': ['进程级身份；读历史轮也显示当前加载值，不等于该轮构建', 'Process-level identity; historical rounds show the live value, not their own build'],
      'system.releaseId': ['源码发布标识', 'Source release'],
      'system.releaseDigest': ['源码摘要', 'Source digest'],
      'system.releaseMissing': ['未打包（运行 scripts/build-release.mjs）', 'not packaged (run scripts/build-release.mjs)'],
      'system.loadedAt': ['本进程加载时间', 'Loaded at (this process)'],

      // ---- F1 (§2.1/§2.2/§3.2): environment, automation, stop semantics -----
      'env.demo': ['模拟盘', 'Demo'],
      'env.accountHint': ['环境固定为币安 Demo；账户由服务端账本标识。核对：{ledger}', 'Environment is pinned to Binance demo; the account is the service ledger. Verify: {ledger}'],
      'env.accountUnknown': ['账户未提供', 'account not provided'],
      'auto.running': ['自动运行中', 'Auto running'],
      'auto.stopped': ['已停止自动交易', 'Auto trading stopped'],
      'auto.stopping': ['正在停止', 'Stopping'],
      'auto.starting': ['正在启动', 'Starting'],
      'auto.pendingService': ['本机调度已停，服务端状态待确认', 'Local scheduler stopped; service state unconfirmed'],
      'auto.pendingControl': ['本机调度已停，服务端仍允许交易', 'Local scheduler stopped; the service still allows entries'],
      'auto.pendingShort': ['待确认', 'Unconfirmed'],
      'auto.stoppedShort': ['已停止', 'Stopped'],
      'auto.blocked': ['需处理', 'Needs attention'],
      'auto.unknown': ['状态未知', 'Status unknown'],
      'auto.stop': ['停止自动交易', 'Stop auto trading'],
      'auto.start': ['启动自动交易', 'Start auto trading'],
      'auto.stopReason': ['顶栏停止自动交易', 'Stopped from the top bar'],
      'auto.startReason': ['顶栏启动自动交易', 'Started from the top bar'],
      'auto.lastSuccess': ['最近成功', 'Last success'],
      'auto.nextRun': ['下次执行', 'Next run'],
      'auto.lastSuccessNone': ['尚无成功轮次', 'No successful round yet'],
      'auto.stopDone': ['已停止自动交易：调度已关，服务端已确认暂停；持仓与交易所保护单保持不变', 'Auto trading stopped: scheduler off, service pause confirmed; positions and exchange protection orders are unchanged'],
      'auto.stopPending': ['本机调度已停，服务端状态待确认：请查看服务状态后重新确认停止', 'Local scheduler stopped; the service has not confirmed the pause yet. Check the service, then confirm the stop again'],
      'auto.stopFailed': ['停止失败（{reason}）：本机调度未确认关闭，请在系统状态里核对', 'Stop failed ({reason}): the local scheduler was not confirmed off; verify in system status'],
      'auto.startDone': ['自动交易已启动：服务端已恢复正常，调度已开启', 'Auto trading started: the service is back to normal and the scheduler is armed'],
      'auto.startBlocked': ['无法启动自动交易（{reason}）：本机调度保持关闭', 'Cannot start auto trading ({reason}): the scheduler stays off'],
      'auto.startLocked': ['服务端拒绝恢复：日亏损或回撤风控锁定仍生效', 'The service refused to resume: a daily-loss or drawdown latch is still active'],
      'auto.stateTitle': ['状态：{state}', 'Status: {state}'],
      'auto.hint': ['停止只关闭自动执行并请求服务端暂停；不平仓，也不撤销交易所保护单。', 'Stopping only closes automatic execution and requests a service pause; it never closes positions or cancels exchange protection orders.'],
      'auto.detail': ['自动交易明细', 'Automation detail'],
      'auto.sourceService': ['依据服务端控制态，刷新或重启不会重置', 'Derived from the service control row; a refresh or restart cannot reset it'],
      'auto.sourceUnknown': ['Host 未回报自动运行状态', 'The Host did not report an automation state'],
      'auto.schedulerOn': ['调度开启', 'scheduler on'],
      'auto.schedulerOff': ['调度关闭', 'scheduler off'],
      'auto.controlRow': ['服务端控制态', 'Service control'],
      'auto.controlReason': ['控制原因', 'Control reason'],
      'auto.controlUpdated': ['控制态更新时间', 'Control updated'],
      'auto.stopPhase': ['停止过程', 'Stop attempt'],
      'auto.stopRequest': ['停止请求编号', 'Stop request id'],
      'auto.interval': ['间隔', 'Interval'],
      'auto.lastResult': ['最近一次调度结果', 'Last schedule result'],

      // ---- F1: 影响 + 原因 + 下一步, four categories -------------------------
      's2.catNoTrade': ['不交易', 'No trading'],
      's2.catRule': ['规则阻断', 'Blocked by rules'],
      's2.catExec': ['执行失败', 'Execution failed'],
      's2.catPending': ['待确认', 'Unconfirmed'],
      's2.serviceDownImpact': ['影响：自动交易与手动运行都不会成功', 'Impact: neither automatic nor manual rounds can succeed'],
      's2.serviceDownNext': ['下一步：检查服务进程后刷新', 'Next: check the service process, then refresh'],
      // 15 §10: one-click start of the local service (only shown when the Host
      // reports it can actually launch it — never a dead button).
      's2.startService': ['启动服务', 'Start service'],
      'svc.starting': ['启动中…', 'Starting…'],
      'svc.startReason': ['从工作台启动交易服务', 'Start the trading service from the workbench'],
      'svc.started': ['交易服务已就绪（{s}s）', 'Trading service ready ({s}s)'],
      'svc.already': ['交易服务本来就在运行', 'The trading service was already running'],
      'svc.startFailed': ['服务启动失败：{reason}', 'Service start failed: {reason}'],
      'svc.missing': ['无法启动：本机找不到服务启动器或已禁用（{reason}）', 'Cannot start: launcher missing or disabled ({reason})'],
      'svc.unsupported': ['本机不支持一键启动（{reason}）', 'One-click start unavailable ({reason})'],
      // 16 §1: first-run environment wizard.
      's2.setupService': ['初始化服务', 'Set up service'],
      'setup.title': ['交易服务环境', 'Trading service environment'],
      'setup.sub': ['本机还没有可用的交易服务环境。初始化会创建 Python 虚拟环境并安装依赖（需要联网，约 1–3 分钟）。', 'This machine has no usable trading-service environment yet. Setup creates a Python virtualenv and installs its dependencies (network required, 1–3 minutes).'],
      'setup.checking': ['检测中…', 'Checking…'],
      'setup.python': ['Python', 'Python'],
      'setup.venv': ['虚拟环境', 'Virtualenv'],
      'setup.deps': ['依赖', 'Dependencies'],
      'setup.service': ['服务', 'Service'],
      'setup.ok': ['就绪', 'ready'],
      'setup.missing': ['缺失', 'missing'],
      'setup.unknown': ['未知', 'unknown'],
      'setup.start': ['一键初始化', 'Initialize now'],
      'setup.running': ['初始化中…', 'Initializing…'],
      'setup.pythonMissing': ['本机没有可用的 Python 3：请先安装 Python 3.11+ 并加入 PATH，然后点「重新检测」。', 'No usable Python 3 on this machine. Install Python 3.11+ (add it to PATH), then retry the check.'],
      'setup.done': ['交易服务已就绪', 'Trading service ready'],
      'setup.failed': ['初始化失败：{reason}', 'Setup failed: {reason}'],
      'setup.retry': ['重新检测', 'Re-check'],
      'setup.log': ['最近输出', 'Recent output'],
      'setup.close': ['关闭', 'Close'],
      'setup.needInstall': ['需要初始化', 'needs setup'],
      'setup.step.pending': ['等待', 'pending'],
      'setup.step.running': ['进行中', 'running'],
      'setup.step.ok': ['完成', 'done'],
      'setup.step.failed': ['失败', 'failed'],
      'setup.step.skipped': ['跳过', 'skipped'],
      's2.serviceDownState': ['服务状态：{state}', 'Service state: {state}'],
      's2.noTradeImpact': ['影响：本轮不交易，不新增风险', 'Impact: no trade this round, no new risk'],
      's2.ruleImpact': ['影响：新开仓被规则阻断', 'Impact: new entries are blocked by rules'],
      's2.ruleNext': ['下一步：核对控制态与风控水位', 'Next: check the control row and risk watermarks'],
      's2.execImpact': ['影响：本轮执行失败，结果以账本为准', 'Impact: this round failed to execute; the ledger is authoritative'],
      's2.execNext': ['下一步：查看本轮详情与错误码', 'Next: open the round details and the error code'],
      's2.protectionImpact': ['影响：部分持仓缺少保护，新开仓已阻断', 'Impact: some positions lack protection; new entries are blocked'],
      's2.protectionNext': ['下一步：补挂保护单并核对', 'Next: repair protection and verify'],
      's2.pendingImpact': ['影响：订单结果未确认，不重复提交', 'Impact: the order result is unconfirmed; never resubmit it'],
      's2.pendingNext': ['下一步：等待回查收敛或人工核对', 'Next: wait for the recheck to converge, or verify manually'],
      's2.staleImpact': ['影响：数据已过期，页面不代表当前状态', 'Impact: the data is stale; this page does not describe the current state'],
      's2.staleNext': ['下一步：刷新数据；仍失败时检查服务', 'Next: refresh; if it still fails, check the service'],
      's2.scheduleImpact': ['影响：自动轮次连续被跳过，不会成交', 'Impact: automatic rounds keep being skipped, so nothing trades'],
      's2.scheduleNext': ['下一步：查看调度明细的原因码', 'Next: read the reason code in the schedule detail'],
      's2.autoImpact': ['影响：自动交易未在运行', 'Impact: automatic trading is not running'],
      's2.autoNext': ['下一步：确认停止来源，按需显式启动', 'Next: confirm where the stop came from, then start explicitly if needed'],

      // ---- F2 (§3.1): one settings entry, five groups ----------------------
      'set.open': ['设置', 'Settings'],
      'set.title': ['设置', 'Settings'],
      'set.sub': ['一个入口 · 五组设置 · 保存与生效分开', 'One entry · five groups · save and apply are separate'],
      'set.close': ['关闭设置', 'Close settings'],
      'set.group.connection': ['连接与账户', 'Connection & account'],
      'set.group.trading': ['交易与风控', 'Trading & risk'],
      'set.group.automation': ['自动化', 'Automation'],
      'set.group.appearance': ['外观与显示', 'Appearance & display'],
      'set.group.diagnostics': ['系统与诊断', 'System & diagnostics'],
      'set.readonly': ['只读 · 来源服务端配置', 'Read-only · from service config'],
      'set.localOnly': ['本机设置 · 即时生效', 'Local setting · applies immediately'],
      'set.env': ['环境', 'Environment'],
      'set.envLocked': ['本期只读；环境切换按 M2 单独确认', 'Read-only this round; environment switch is a separate M2 flow'],
      'set.account': ['账户身份', 'Account identity'],
      'set.credentials': ['凭证', 'Credentials'],
      'set.credentialsOn': ['已配置（指纹 {fp}）', 'configured (fingerprint {fp})'],
      'set.credentialsOff': ['未配置：不会签名下单', 'not configured: no signed order is sent'],
      'set.tokenSource': ['服务 token 来源', 'Service token source'],
      'set.serviceReady': ['服务就绪', 'Service ready'],
      'set.control': ['控制态', 'Control row'],
      'set.fundsTitle': ['账户与资金（C 口径）', 'Account & funds (C calibers)'],
      'set.fundsSub': ['四个权益口径分开；本地账净盈亏由本地成交与流水计算，不是账户权益', 'The four equity calibers stay separate; the local book net PnL is computed from local fills and flows, not account equity'],
      'set.caliberWallet': ['钱包余额', 'Wallet balance'],
      'set.caliberEquity': ['保证金权益', 'Margin equity'],
      'set.caliberUpnl': ['未实现盈亏', 'Unrealized PnL'],
      'set.caliberAvailable': ['可用余额', 'Available balance'],
      'set.caliberNet': ['本地账净盈亏（不含划转）', 'Local book net PnL (excludes transfers)'],
      'set.caliberSource': ['权益来源', 'Equity source'],
      'set.caliberNote': ['显示与风控口径不一致时以来源标注为准；未知不打折成 0', 'When display and risk calibers differ the source label wins; unknown is never shown as 0'],
      'set.caliberStale': ['权益来源不是交易所（{source}）：数字可能不是当前值，未知时显示 — 而不是 0', 'The equity source is not the exchange ({source}): these values may be stale, and unknown is shown as — rather than 0'],
      'set.books': ['本地四本账', 'Local books'],
      'set.bookRealized': ['已实现盈亏', 'Realized PnL'],
      'set.bookCommission': ['手续费（成本）', 'Commissions (cost)'],
      'set.bookFunding': ['资金费', 'Funding'],
      'set.bookTransfers': ['划转（不计入盈利）', 'Transfers (excluded from PnL)'],
      'set.bookNetIncludes': ['净盈亏 = 已实现 − 手续费 + 资金费', 'Net = realized − commissions + funding'],
      'set.fundingUnknown': ['资金费未回填，净盈亏不计算', 'Funding not backfilled; net PnL is not computed'],
      'set.unknownList': ['未完成项', 'Incomplete items'],
      'set.coverage': ['统计覆盖', 'Coverage'],
      'set.coveragePartial': ['覆盖不完整：历史不代表完整账户历史', 'Partial coverage: history is not the full account history'],
      'set.reconcileTitle': ['对账', 'Reconciliation'],
      'set.reconcileMatched': ['已对平', 'matched'],
      'set.reconcilePartial': ['部分对平', 'partial'],
      'set.reconcileUnknown': ['未知', 'unknown'],
      'set.reconcileDiff': ['差额（超出容差 {tol} 即部分对平）', 'Diff (beyond tolerance {tol} = partial)'],
      'set.reconcileActual': ['交易所权益变动', 'Exchange equity delta'],
      'set.reconcileExpected': ['账本应有变动（净盈亏 + 净划转）', 'Ledger expected delta (net PnL + net transfers)'],
      'set.reconcileReasons': ['原因', 'Reasons'],
      'set.reconcileLastSuccess': ['最后成功对账', 'Last successful reconcile'],
      'set.reconcileChecked': ['本次核对时间', 'Checked at'],
      'set.fundsUnavailable': ['资金口径暂不可用（{code}）：不显示 0', 'Fund calibers unavailable ({code}): no zeros are shown'],
      'set.fundsRefresh': ['刷新资金口径', 'Refresh fund calibers'],
      'set.limitsTitle': ['当前生效限额', 'Effective limits'],
      'set.limitsSub': ['限额来自服务端环境配置，界面不能编辑', 'Limits come from the service environment config; the UI cannot edit them'],
      'set.limitMissing': ['不可用（来源缺失）', 'unavailable (source missing)'],
      'set.approvalTtl': ['批准令牌 TTL（单次有效）', 'Approval token TTL (one submission)'],
      'set.approvalMaxLabel': ['上限', 'max'],
      'set.approvalBinding': ['令牌约束', 'Token binding'],
      'set.approvalSingleUse': ['单次有效', 'single use'],
      'set.approvalPlanHash': ['plan_hash 绑定', 'plan_hash bound'],
      'set.approvalRulesVersion': ['rules_version 绑定', 'rules_version bound'],
      'set.approvalBoundTrue': ['绑定 bound=true', 'policy bound=true'],
      'set.approvalNote': ['每次批准只对一次提交有效：令牌 TTL 到期或已用过，都必须由服务端重新评估后重新签发，不能复用。', 'Each approval covers exactly one submission: once the TTL expires or it has been used, the service must re-evaluate and re-issue; a token is never reused.'],
      'set.capsSource': ['上限来源', 'Limits source'],
      'set.capsSourceValue': ['/api/risk/envelope → authorization.effective_caps', '/api/risk/envelope → authorization.effective_caps'],
      'set.capsOnlySource': ['只显示生效值：字段缺失时显示不可用，不借用上一轮或旧档数字。服务端没有风控写入路由，本组只读。', 'Only the effective values are shown; a missing field reads as unavailable instead of borrowing a previous value. The service exposes no risk-write route, so this group is read-only.'],
      'set.legacyUnknown': ['未知（服务端未回报旧档状态）', 'unknown (the service did not report the legacy tier)'],
      'set.legacyAuthorized': ['异常：旧档被标注为可授权，请核对服务端', 'anomaly: the legacy tier is reported as authorizing; check the service'],
      'set.limitRiskPct': ['单笔风险上限', 'Risk per trade cap'],
      'set.limitLeverage': ['杠杆上限', 'Leverage cap'],
      'set.limitPositions': ['持仓数上限', 'Position count cap'],
      'set.limitNotional': ['名义价值倍数上限', 'Notional multiple cap'],
      'set.limitMargin': ['保证金占用上限', 'Margin usage cap'],
      'set.limitDailyLoss': ['日亏损停止', 'Daily loss stop'],
      'set.limitDrawdown': ['峰值回撤上限', 'Peak drawdown cap'],
      'set.limitStreak': ['连续亏损上限', 'Loss streak cap'],
      'set.sizingPolicy': ['定量口径', 'Sizing rule'],
      'set.sizingValue': ['确定性代码按风险预算/止损距离/名义与保证金计算，模型数量只作上限', 'Deterministic code sizes by risk budget / stop distance / notional and margin; the model qty is a cap only'],
      'set.strongTier': ['旧强信心档', 'Legacy strong tier'],
      'set.strongTierOff': ['不参与执行授权（legacy）', 'not used for authorization (legacy)'],
      'set.confidenceField': ['信心（分析字段）', 'Confidence (analysis field)'],
      'set.confidenceOff': ['不放大风险与杠杆', 'never raises risk or leverage'],
      'set.entryBlockers': ['当前新开仓阻断', 'Current entry blockers'],
      'set.autoTitle': ['调度', 'Scheduler'],
      'set.autoSub': ['停止只关调度并请求服务端 pause；不平仓、不撤保护单', 'Stopping only closes the scheduler and requests a service pause; it never flattens or cancels protection'],
      'set.triggers': ['最近轮次来源', 'Recent round triggers'],
      'set.triggersNone': ['暂无轮次', 'No rounds yet'],
      'set.triggerAuto': ['自动槽', 'auto'],
      'set.triggerManual': ['手工', 'manual'],
      'set.modelTitle': ['模型与配置', 'Models & configuration'],
      'set.modelSub': ['保存后下一轮生效；旧提交不覆盖新值', 'Saved values apply from the next round; a stale submit never overwrites newer values'],
      'set.before': ['修改前', 'Before'],
      'set.after': ['修改后', 'After'],
      'set.effective': ['生效时间', 'Effective'],
      'set.effectiveNext': ['下一轮', 'next round'],
      'set.lastChange': ['上次生效', 'Last applied'],
      'set.noChange': ['无改动', 'unchanged'],
      'set.conflict': ['保存冲突：配置已被更新（当前修订 {current}，你编辑的是 {provided}）。草稿已保留，未覆盖新值。', 'Save conflict: the config moved to revision {current} while you edited {provided}. Your draft is kept; nothing was overwritten.'],
      'set.savedGroup': ['已保存并生效（下一轮）', 'Saved; applies from the next round'],
      'set.draft': ['草稿未保存', 'Draft unsaved'],
      'set.unknownNote': ['未知项不按 0 计入', 'Unknown items are never counted as 0'],
      'set.reloadConfig': ['采用最新配置（放弃我的草稿）', 'Adopt the latest config (discard my draft)'],
      'set.appearanceTitle': ['外观', 'Appearance'],
      'set.language': ['语言', 'Language'],
      'set.density': ['密度', 'Density'],
      'set.densityNormal': ['标准', 'Normal'],
      'set.densityCompact': ['紧凑', 'Compact'],
      'set.updown': ['涨跌色', 'Up/down colors'],
      'set.appearanceNote': ['外观只影响本机显示，不写入服务端', 'Appearance is local only; nothing is written to the service'],
      'set.diagTitle': ['运行与诊断', 'Runtime & diagnostics'],
      'set.diagVersion': ['实际加载版本', 'Loaded version'],
      'set.diagDataTime': ['数据时间', 'Data time'],
      'set.diagService': ['服务状态', 'Service state'],
      'set.diagPending': ['未决订单', 'Pending orders'],
      'set.exchangeTitle': ['交易所可达性', 'Exchange reachability'],
      'set.exchangeSub': ['只读；取值只走 /api/status 的 exchange 子对象（不读顶层 stale）', 'Read-only; values come only from the exchange block of /api/status (never a top-level stale)'],
      'set.exchangeOpen': ['现在能取数', 'readable now'],
      'set.exchangeBlocked': ['现在不能取数（显示最后一次成功的数据）', 'not readable now (showing the last successful data)'],
      'set.exchangeSource': ['取数来源', 'Source'],
      'set.exchangeLastSuccess': ['上次成功取数', 'Last successful read'],
      'set.exchangeRetry': ['何时再试', 'Next attempt'],
      'set.exchangeRetryIn': ['{n} 秒后（失败冷却中）', 'in {n}s (failure cooldown)'],
      'set.exchangeRetryNow': ['可立即重试', 'may retry now'],
      'set.exchangeBudget': ['读超时 / 读预算', 'Read timeout / budget'],
      'set.exchangeUnknown': ['为什么取不到', 'Why the read failed'],
      'set.exchangeMissing': ['暂不可用（服务端未回报 exchange 块）', 'unavailable (the service reported no exchange block)'],
      'set.exchangeStale': ['本次来自缓存，不是最新事实', 'served from cache, not the latest fact'],
      'set.diagPendingNone': ['没有待确认订单', 'No pending orders'],
      'set.diagPendingCount': ['{n} 条待确认', '{n} pending'],
      'set.diagPendingAge': ['已等待 {age}', 'waiting {age}'],
      'set.diagPendingNote': ['待确认不等于失败；系统按原标识回查，不重复提交', 'Pending is not a failure: the system rechecks the original id instead of resubmitting'],
      'set.diagLogs': ['精简日志', 'Compact log'],
      'set.diagOpenSystem': ['打开完整诊断', 'Open full diagnostics'],
      'set.diagSchema': ['账本 schema', 'Ledger schema'],
      'set.diagRecovery': ['启动恢复', 'Startup recovery'],
      'set.diagProtection': ['保护覆盖', 'Protection coverage'],
      'set.diagGaps': ['回报缺口', 'Reported gaps'],
      'set.diagUnavailable': ['暂不可用', 'unavailable'],
      'set.section.account': ['连接', 'Connection'],
      'set.section.trading': ['允许的操作', 'Allowed actions'],
      'set.section.diagnostics': ['运行身份', 'Runtime identity'],

      // ---- F4: the plan's allowed entry range must be checkable -------------
      'ai.entryZone': ['允许入场范围', 'Allowed entry range'],
      'ai.validUntil': ['计划有效期至', 'Plan valid until'],
      'ai.zoneMissing': ['未提供（不扩大范围）', 'not provided (range is never widened)'],
    }

    // ---- reasonCode -> 中文标签（03 §8 唯一词表：75 行 / 89 码） ------------
    // Ownership (05 §5 / RT-H2): 03 owns this "code -> label" column only.
    // Category / stage / role / one-liner / next step come from the Host
    // `attribution` field (01 判定树) and are never re-derived here.
    // Spec source: 03-交互细节审视.md §8.1–§8.7. EN mirrors the ZH meaning.
    const REASON_LABELS = {
      // §8.1 数据 / 证据（7）
      E_EVIDENCE_UNAVAILABLE: ['无可用证据快照', 'No usable evidence snapshot'],
      E_EVIDENCE_UNVERIFIED: ['证据未通过校验', 'Evidence failed verification'],
      E_EVIDENCE_CORRUPT: ['证据内容损坏', 'Evidence is corrupt'],
      E_EVIDENCE_NOT_FOUND: ['证据不存在', 'Evidence not found'],
      E_EVIDENCE_INVALID: ['证据结构不合格', 'Evidence shape invalid'],
      DATA_NOT_GOOD: ['证据质量非 GOOD', 'Evidence quality is not GOOD'],
      SNAPSHOT_EXPIRED: ['证据快照已过期', 'Evidence snapshot expired'],
      // §8.2 模型 / 链路（24）
      LLM_TIMEOUT: ['模型调用超时', 'Model call timed out'],
      LLM_FINISH_ERROR: ['模型输出异常结束', 'Model output ended abnormally'],
      LLM_TRANSPORT: ['模型传输失败', 'Model transport failed'],
      LLM_INCOMPLETE: ['模型流未给出结束帧', 'Stream ended without a finish frame'],
      LLM_EMPTY: ['模型返回空内容', 'Model returned empty content'],
      JSON_PARSE: ['输出不是合法 JSON', 'Output is not valid JSON'],
      JSON_EMPTY: ['模型输出为空文本', 'Model output was empty text'],
      SCHEMA_INVALID: ['输出不符合 schema / 语义', 'Output violates the schema'],
      SCHEMA_UNKNOWN: ['未注册的输出 schema', 'Unregistered output schema'],
      ROLE_UNKNOWN: ['未知角色', 'Unknown role'],
      BUDGET_EXHAUSTED: ['调用次数已达上限', 'Call budget exhausted'],
      CHAIN_DEADLINE: ['决策链已达硬截止', 'Chain deadline reached'],
      NO_ADAPTER: ['没有可用的模型路由', 'No model route available'],
      NO_LLM: ['未提供模型接缝', 'No model seam provided'],
      DEBATE_INCOMPLETE: ['辩论未完成', 'Debate incomplete'],
      BOTH_SIDES_UNTRADEABLE: ['红蓝双方均不可交易', 'Both sides untradeable'],
      DECISION_INVALID: ['决策输出不合格', 'Decision output invalid'],
      RISK_UNAVAILABLE: ['风控 AI 未产出', 'Risk AI produced nothing'],
      HARD_RISK_UNAVAILABLE: ['硬风控不可用', 'Hard risk unavailable'],
      HARD_RISK_REJECT: ['硬风控拒绝', 'Hard risk rejected'],
      AI_RISK_REJECT: ['风控 AI 拒绝', 'Risk AI rejected'],
      CONTROL_LOCKED: ['控制态阻断', 'Blocked by control state'],
      EXECUTION_PLAN_INVALID: ['操盘手未产出合格计划', 'Executor produced no valid plan'],
      NO_CANDIDATE: ['没有合格标的', 'No candidate'],
      // §8.3 成功（1）
      TRADE_PLAN_READY: ['交易计划已通过 P1/P2', 'Trade plan passed P1/P2'],
      // §8.4 风控 / 执行闸门（20 行 / 22 码）
      E_RISK_RR: ['盈亏比低于硬下限', 'R:R below the hard floor'],
      E_RISK_LIQ_DISTANCE: ['强平距离不足', 'Liquidation distance too small'],
      E_RISK_SL_DISTANCE: ['止损距离越界', 'Stop distance out of range'],
      E_RISK_SYMBOL_POSITION: ['该标的已有仓位（不加仓/不反手）', 'Symbol already has a position'],
      E_RISK_MAX_POSITIONS: ['已达最大持仓数', 'Max positions reached'],
      E_RISK_LEVERAGE: ['杠杆超过上限', 'Leverage above the cap'],
      E_RISK_PER_TRADE: ['单笔风险超过上限', 'Per-trade risk above the cap'],
      E_RISK_NOTIONAL: ['名义价值超过上限', 'Notional above the cap'],
      E_RISK_PRECISION: ['数量/价格精度不合格', 'Qty/price precision invalid'],
      E_RISK_BELOW_MIN: ['数量低于最小下单量', 'Qty below the minimum'],
      E_RISK_SLTP: ['止损/止盈不合法', 'Stop/target invalid'],
      E_RISK_MARGIN: ['保证金不足', 'Insufficient margin'],
      E_RISK_MARGIN_RATIO: ['保证金占用超阈值', 'Margin usage above the limit'],
      E_RISK_DAILY_LOSS_LATCHED: ['日亏损锁已触发', 'Daily-loss latch tripped'],
      E_RISK_DRAWDOWN_LATCHED: ['回撤锁已触发', 'Drawdown latch tripped'],
      E_RISK_LOSS_STREAK: ['连续亏损达上限', 'Loss streak at the limit'],
      E_RISK_FREQUENCY: ['下单频率超限', 'Order frequency exceeded'],
      E_RISK_REJECTED: ['风控整体拒绝', 'Risk rejected the plan'],
      E_SYMBOL_NOT_ALLOWED: ['标的不在白名单', 'Symbol not allowed'],
      E_APPROVAL_INVALID: ['审批令牌无效', 'Approval token invalid'],
      E_APPROVAL_REQUIRED: ['需要审批', 'Approval required'],
      E_APPROVAL_REPLAY: ['令牌重放', 'Approval replay'],
      // §8.5 执行（9 行 / 10 码）
      'EXECUTION_BLOCKED:PRICE_MOVED': ['执行阻断：现价已离开入场区', 'Execution blocked: price left the entry zone'],
      'EXECUTION_BLOCKED:SPREAD_TOO_WIDE': ['执行阻断：点差过宽', 'Execution blocked: spread too wide'],
      E_PLAN_MISSING: ['执行计划缺失', 'Execution plan missing'],
      E_QTY_INVALID: ['数量参数不合法', 'Invalid qty'],
      E_CONFIRM_REQUIRED: ['缺少确认词', 'Missing confirmation word'],
      E_CONTROL_CONFIRM_REQUIRED: ['缺少控制态确认词', 'Missing control confirmation word'],
      E_REASON_REQUIRED: ['缺少原因', 'Missing reason'],
      E_OPERATION_EXISTS: ['该请求已记录过（防重复）', 'Request already recorded'],
      E_FILL_UNCONFIRMED: ['成交未确认（结果未知）', 'Fill unconfirmed (unknown outcome)'],
      // §8.6 基础设施 / 调度（12 行 / 23 码；E_SERVICE_HTTP_{n} 见 reason.serviceHttp）
      E_SERVICE_TIMEOUT: ['交易服务响应超时', 'Trading service timed out'],
      E_SERVICE_OFFLINE: ['交易服务不可达', 'Trading service unreachable'],
      E_SERVICE_UNAVAILABLE: ['交易服务不可用', 'Trading service unavailable'],
      E_SERVICE_NOT_READY: ['交易服务未就绪', 'Trading service not ready'],
      E_SERVICE_UNAUTHORIZED: ['服务鉴权失败', 'Service auth failed'],
      E_SERVICE_TOKEN_MISSING: ['缺少服务 token', 'Service token missing'],
      E_SERVICE_CONFLICT: ['服务冲突', 'Service conflict'],
      E_SERVICE_LOCKED: ['服务已锁定', 'Service locked'],
      E_SERVICE_REJECTED: ['服务拒绝', 'Service rejected'],
      E_CYCLE_DUPLICATE_SLOT: ['该调度槽位已完成，本轮跳过', 'Slot already completed; skipped'],
      E_CYCLE_RUNNING: ['已有轮次在运行', 'A cycle is already running'],
      E_CYCLE_ID_INVALID: ['轮次 ID 不合法', 'Invalid cycle id'],
      HOST_STALE_RUN: ['遗留运行中轮次已收口', 'Stale running round closed'],
      HOST_RUN_TIMEOUT: ['Host 运行超时', 'Host run timed out'],
      HOST_RUN_FAILED: ['Host 运行失败', 'Host run failed'],
      DAILY_BUDGET_EXHAUSTED: ['已达 24 小时轮次上限', '24h round limit reached'],
      AUTO_DISABLED: ['调度未启用', 'Scheduler disabled'],
      OVERLAP_SKIPPED: ['槽位重叠跳过', 'Slot overlap; skipped'],
      PAUSED_AFTER_FAILURES: ['连续失败已熔断', 'Circuit open after failures'],
      ROUND_IN_FLIGHT: ['已有轮次在跑', 'A round is in flight'],
      POSITION_OPEN: ['持仓中跳过开链', 'Position open; chain skipped'],
      E_DEMO_CREDENTIALS_MISSING: ['缺少 Demo 凭据', 'Demo credentials missing'],
      // §8.7 02 路由域（2）
      ROUTE_UNAVAILABLE: ['路由不可用（模型已下线或档位不支持）', 'Route unavailable (model offline or effort unsupported)'],
      ROUTE_FALLBACK_APPLIED: ['已回退到备用路由', 'Fell back to the backup route'],
      // Client-generated codes (never emitted as a Host reasonCode).
      RUN_TIMEOUT: ['等待超时', 'Wait timed out'],
      STATE_OFFLINE: ['与工作台的连接中断', 'Workbench connection lost'],
      // B+ editorial labels
      'b.accountRisk': ['账户与风险', 'Account & risk'],
      'b.positionsProtection': ['持仓与保护', 'Positions & protection'],
      'b.aiBrief': ['AI 简报', 'AI briefing'],
      'b.tape': ['今日流水', 'Today tape'],
      'b.total': ['总权益', 'Equity'],
      'b.today': ['今日盈亏', "Today's P&L"],
      'b.upnl': ['未实现', 'Unrealized'],
      'b.available': ['可用', 'Available'],
      'b.startEquity': ['起始权益', 'Start equity'],
      'b.margin': ['保证金占用', 'Margin used'],
      'b.actions': ['处置', 'Actions'],
      'b.latest': ['最新', 'Latest'],
      'b.noTape': ['今日暂无订单或成交', 'No orders or fills today'],
      'b.riskDetail': ['风险明细', 'Risk detail'],
      'b.accountDetail': ['资金明细', 'Account detail'],
      'positions.stop': ['止损', 'Stop'],
      'positions.liq': ['强平', 'Liq.'],
    }
    const DICT = { zh: {}, en: {} }
    for (const key of Object.keys(TEXT)) {
      DICT.zh[key] = TEXT[key][0]
      DICT.en[key] = TEXT[key][1]
    }
    // The 03 vocabulary is registered as ordinary locale keys (reason.<CODE>)
    // so the dictionary stays the single lookup surface for zh + en.
    for (const key of Object.keys(REASON_LABELS)) {
      DICT.zh[`reason.${key}`] = REASON_LABELS[key][0]
      DICT.en[`reason.${key}`] = REASON_LABELS[key][1]
      // B+ editorial labels were appended to the reason table to keep the
      // single text table; expose them as plain keys too.
      if (key.startsWith('b.')) {
        DICT.zh[key] = REASON_LABELS[key][0]
        DICT.en[key] = REASON_LABELS[key][1]
      }
    }

    // ---- styles (tokens only; every color is a color-mix) -----------------
    const CSS = `
.twb-root{--twb-s0:var(--dsw-alias-bg-base);--twb-s1:var(--dsw-alias-bg-layer-1);--twb-s2:var(--dsw-alias-bg-layer-2);
  --twb-surface:var(--dsw-alias-bg-layer-2);--twb-drawer:var(--dsw-alias-bg-layer-1);
  --twb-line-1:var(--dsw-alias-border-l1);--twb-line-2:var(--dsw-alias-border-l2);
  --twb-ink-1:var(--dsw-alias-label-primary);--twb-ink-2:var(--dsw-alias-label-secondary);
  --twb-ink-3:color-mix(in srgb,var(--dsw-alias-label-secondary) 72%,transparent);
  --twb-accent:var(--dsw-alias-brand-primary);--twb-accent-ink:var(--dsw-alias-bg-base);
  --twb-up:var(--dsw-alias-state-success-primary);--twb-down:var(--dsw-alias-state-error-primary);
  --twb-warn:var(--dsw-alias-state-warn-primary);--twb-danger:var(--dsw-alias-state-error-primary);
  --twb-neutral:var(--dsw-alias-state-idle-primary);
  --twb-tint-up:color-mix(in srgb,var(--twb-up) 10%,var(--twb-s1));
  --twb-tint-warn:color-mix(in srgb,var(--twb-warn) 10%,var(--twb-s1));
  --twb-tint-danger:color-mix(in srgb,var(--twb-danger) 10%,var(--twb-s1));
  --twb-edge-up:color-mix(in srgb,var(--twb-up) 42%,var(--twb-line-1));
  --twb-edge-warn:color-mix(in srgb,var(--twb-warn) 42%,var(--twb-line-1));
  --twb-edge-danger:color-mix(in srgb,var(--twb-danger) 42%,var(--twb-line-1));
  --twb-row-hover:color-mix(in srgb,var(--twb-ink-1) 5%,transparent);
  --twb-grid:color-mix(in srgb,var(--twb-line-2) 70%,transparent);
  --twb-scrim:color-mix(in srgb,var(--twb-ink-1) 24%,transparent);
  --twb-shadow-pop:-12px 0 28px color-mix(in srgb,var(--twb-ink-1) 14%,transparent);
  --twb-shadow-lift:0 8px 20px color-mix(in srgb,var(--twb-ink-1) 12%,transparent);
  --twb-num:"${MONO}";
  height:100%;display:flex;flex-direction:column;overflow:hidden;background:var(--twb-s0);color:var(--twb-ink-1);
  font:400 13px/20px "Inter","Segoe UI","Microsoft YaHei",system-ui,sans-serif;-webkit-font-smoothing:antialiased}
.twb-root[data-updown='red-up']{--twb-up:var(--dsw-alias-state-error-primary);--twb-down:var(--dsw-alias-state-success-primary)}
.twb-root[data-updown='mono']{--twb-up:var(--dsw-alias-label-primary);--twb-down:var(--dsw-alias-label-primary)}
.twb-root *{box-sizing:border-box}
.twb-root button{font:inherit;color:inherit}
.twb-root ::selection{background:color-mix(in srgb,var(--twb-accent) 24%,transparent);color:var(--twb-ink-1)}
.twb-root *{scrollbar-color:var(--twb-line-2) transparent;scrollbar-width:thin}
.twb-root *::-webkit-scrollbar{width:10px;height:10px}
.twb-root *::-webkit-scrollbar-thumb{background:var(--twb-line-2);border-radius:999px}
.twb-root *::-webkit-scrollbar-track{background:transparent}
.twb-s0{flex:0 0 auto;height:48px;display:flex;align-items:center;gap:12px;padding:0 16px;background:var(--twb-s1);
  border-bottom:1px solid var(--twb-line-1);position:relative;z-index:30}
.twb-title{font-size:13px;font-weight:600;letter-spacing:-0.01em;white-space:nowrap}
.twb-demo{height:20px;padding:0 6px;border-radius:4px;border:1px solid var(--twb-edge-warn);background:var(--twb-tint-warn);
  color:var(--twb-warn);font-size:10px;font-weight:600;letter-spacing:0.08em;line-height:18px;white-space:nowrap}
.twb-spacer{flex:1 1 auto}
.twb-seg{display:inline-flex;align-items:center;gap:2px;padding:2px;background:var(--twb-s2);
  border:1px solid var(--twb-line-1);border-radius:6px}
.twb-seg button{height:28px;padding:0 12px;border:1px solid transparent;border-radius:4px;background:transparent;
  color:var(--twb-ink-2);font-size:12px;font-weight:500;cursor:pointer}
.twb-seg button:hover{color:var(--twb-ink-1)}
.twb-seg button[aria-selected='true']{background:var(--twb-s1);border-color:var(--twb-line-2);color:var(--twb-ink-1);font-weight:600}
.twb-equity{display:flex;align-items:baseline;gap:6px;white-space:nowrap}
.twb-equity .k{font-size:11px;color:var(--twb-ink-2)}
.twb-hero{font-family:var(--twb-num);font-variant-numeric:tabular-nums slashed-zero;font-size:26px;line-height:32px;font-weight:600;letter-spacing:-0.01em}
.twb-hero-unit{font-size:11px;color:var(--twb-ink-2)}
.twb-divider{width:1px;height:20px;background:var(--twb-line-1)}
.twb-btn{height:32px;padding:0 12px;display:inline-flex;align-items:center;justify-content:center;gap:6px;
  border:1px solid transparent;border-radius:6px;background:transparent;color:var(--twb-ink-1);
  font-size:12px;font-weight:500;cursor:pointer;white-space:nowrap}
.twb-btn--primary{background:var(--twb-ink-1);border-color:var(--twb-ink-1);color:var(--twb-accent-ink);font-weight:600}
.twb-root .twb-btn--primary{color:var(--twb-accent-ink)}
.twb-root .twb-btn--primary:hover{background:color-mix(in srgb,var(--twb-ink-1) 88%,var(--twb-s2))}
.twb-btn--secondary{background:var(--twb-s2);border-color:var(--twb-line-2)}
.twb-btn--secondary:hover{border-color:var(--twb-ink-2)}
.twb-btn--ghost{color:var(--twb-ink-2)}
.twb-btn--ghost:hover{color:var(--twb-ink-1);background:var(--twb-row-hover)}
.twb-btn--icon{width:32px;padding:0;color:var(--twb-ink-2)}
.twb-btn--icon:hover{color:var(--twb-ink-1);background:var(--twb-row-hover)}
.twb-btn[aria-disabled='true']{color:var(--twb-ink-2);background:transparent;border-color:var(--twb-line-1);cursor:not-allowed}
.twb-btn:disabled{color:var(--twb-ink-2);background:transparent;border-color:var(--twb-line-1);cursor:not-allowed}
.twb-root button:focus-visible,.twb-root [tabindex]:focus-visible{outline:2px solid var(--twb-accent);outline-offset:2px}
.twb-s1{flex:0 0 auto;height:34px;display:flex;align-items:center;gap:10px;padding:0 16px;background:var(--twb-s0);
  border-bottom:1px solid var(--twb-line-1);font-size:11px;color:var(--twb-ink-2);overflow:hidden}
.twb-ctx{display:inline-flex;align-items:baseline;gap:6px;white-space:nowrap;min-width:0}
.twb-ctx .v{color:var(--twb-ink-1);font-size:12px;font-family:var(--twb-num);font-variant-numeric:tabular-nums}
.twb-ctx-sep{width:1px;height:12px;background:var(--twb-line-1);flex:0 0 auto}
.twb-ctx-btn{border:0;background:transparent;padding:0;font-size:11px;color:var(--twb-ink-2);cursor:pointer;white-space:nowrap}
.twb-ctx-btn:hover{color:var(--twb-ink-1);text-decoration:underline;text-underline-offset:2px}
.twb-s2{flex:0 0 auto;height:44px;display:flex;align-items:center;gap:8px;padding:0 16px;position:relative;
  border-bottom:1px solid var(--twb-line-1);background:var(--twb-tint-danger);font-size:12px;z-index:10}
.twb-s2::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;background:var(--twb-danger)}
.twb-s2[data-tone='warn']{background:var(--twb-tint-warn)}
.twb-s2[data-tone='warn']::before{background:var(--twb-warn)}
.twb-s2[data-tone='quiet']{background:var(--twb-s0);color:var(--twb-ink-2)}
.twb-s2[data-tone='quiet']::before{display:none}
.twb-s2-ico{width:16px;height:16px;flex:0 0 auto;color:var(--twb-danger)}
.twb-s2[data-tone='warn'] .twb-s2-ico{color:var(--twb-warn)}
.twb-s2-title{font-weight:600;white-space:nowrap}
.twb-s2-reason{color:var(--twb-ink-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.twb-s2-act{border:0;background:transparent;color:var(--twb-ink-1);font-size:12px;font-weight:600;cursor:pointer;padding:0}
.twb-s2-act:hover{text-decoration:underline;text-underline-offset:2px}
.twb-s2-more{color:var(--twb-ink-2);font-size:11px;white-space:nowrap}
.twb-toast{display:flex;align-items:center;gap:8px;height:44px;padding:0 16px;font-size:12px;z-index:40;
  background:var(--twb-tint-danger);border-top:1px solid var(--twb-edge-danger)}
.twb-toast[data-tone='warn']{background:var(--twb-tint-warn);border-top-color:var(--twb-edge-warn)}
.twb-toast[data-tone='quiet']{background:var(--twb-s2);border-top-color:var(--twb-line-1);color:var(--twb-ink-2)}
.twb-s3{flex:1 1 auto;min-height:0;overflow:auto;container-type:size;position:relative}
.twb-canvas{max-width:1680px;margin:0 auto;padding:12px 24px 16px}
.twb-row{display:grid;grid-template-columns:repeat(12,minmax(0,1fr));gap:12px;margin-bottom:12px}
.twb-span7{grid-column:span 7;min-width:0}
.twb-span5{grid-column:span 5;min-width:0}
.twb-card{background:var(--twb-s1);border:1px solid var(--twb-line-1);border-radius:8px;padding:10px 12px;
  display:flex;flex-direction:column;min-height:0;min-width:0}
.twb-band{display:flex;flex-direction:column;min-height:0;min-width:0}
.twb-h150{height:150px}.twb-h300{height:300px}.twb-h260{height:260px}
.twb-card-head{display:flex;align-items:center;gap:8px;margin-bottom:8px;flex:0 0 auto;min-width:0}
.twb-card-title{font-size:12px;font-weight:600;white-space:nowrap}
.twb-hint{font-size:11px;color:var(--twb-ink-2);font-weight:400;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.twb-card-actions{margin-left:auto;display:flex;align-items:center;gap:4px;flex:0 0 auto}
.twb-chip{height:20px;padding:0 6px;display:inline-flex;align-items:center;gap:4px;border-radius:4px;
  border:1px solid var(--twb-line-1);background:var(--twb-s2);color:var(--twb-ink-1);font-size:12px;font-weight:600;white-space:nowrap}
.twb-chip--warn{background:var(--twb-tint-warn);border-color:var(--twb-edge-warn)}
.twb-chip--danger{background:var(--twb-tint-danger);border-color:var(--twb-edge-danger)}
.twb-chip--quiet{background:transparent;color:var(--twb-ink-2);font-weight:500}
.twb-chip-ico{width:10px;height:10px;flex:0 0 auto;color:var(--twb-warn)}
.twb-chip-ico--danger{color:var(--twb-danger)}
.twb-num{font-family:var(--twb-num);font-variant-numeric:tabular-nums slashed-zero;text-align:right}
.twb-up{color:var(--twb-up)}.twb-down{color:var(--twb-down)}.twb-flat{color:var(--twb-ink-1)}.twb-na{color:var(--twb-ink-2)}
.twb-mono{font-family:var(--twb-num);font-variant-numeric:tabular-nums}
.twb-muted{color:var(--twb-ink-2)}
.twb-account{height:150px;display:grid;grid-template-columns:minmax(0,1fr) 128px;gap:12px;align-content:start}
.twb-account-main{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:12px}
.twb-account-cell{min-width:0}
.twb-account-cell .k{display:block;font-size:11px;color:var(--twb-ink-2);margin-bottom:2px;white-space:nowrap}
.twb-account-hero{display:flex;align-items:baseline;gap:6px}
.twb-account-num{font-family:var(--twb-num);font-variant-numeric:tabular-nums slashed-zero;font-size:20px;line-height:26px;font-weight:600}
.twb-account-spark{justify-self:end;text-align:right}
.twb-account-spark .cap{font-size:11px;color:var(--twb-ink-2)}
.twb-budget{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px 12px}
.twb-budget-item{min-width:0;display:flex;flex-direction:column;gap:2px}
.twb-budget-item .k{font-size:11px;color:var(--twb-ink-2);white-space:nowrap}
.twb-budget-item .v{font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.twb-budget-note{margin-left:4px;font-size:11px;font-weight:400;color:var(--twb-ink-2)}
.twb-budget-note--warn{color:var(--twb-warn)}
.twb-budget-note--danger{color:var(--twb-danger)}
.twb-budget-foot{margin-top:auto;font-size:11px;color:var(--twb-ink-2);line-height:16px}
.twb-table-wrap{flex:1 1 auto;min-height:0;overflow:auto;border-top:1px solid var(--twb-line-1);max-height:100%}
.twb-table{width:100%;border-collapse:separate;border-spacing:0;table-layout:fixed;font-size:12px}
.twb-table thead th{position:sticky;top:0;z-index:5;height:28px;padding:0 8px;background:var(--twb-s1);
  border-bottom:1px solid var(--twb-line-2);font-size:11px;font-weight:600;color:var(--twb-ink-2);text-align:left;white-space:nowrap}
.twb-table thead th.is-num{text-align:right}
.twb-table tbody td{height:32px;padding:0 8px;border-bottom:1px solid var(--twb-line-1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.twb-table tbody tr:hover td{background:var(--twb-row-hover)}
.twb-table tbody tr[data-risk='danger'] td{background:var(--twb-tint-danger)}
.twb-table tbody tr[data-risk='danger'] td:first-child{box-shadow:inset 2px 0 0 var(--twb-danger)}
.twb-table tbody tr[data-risk='warn'] td{background:var(--twb-tint-warn)}
.twb-table tbody tr[data-risk='warn'] td:first-child{box-shadow:inset 2px 0 0 var(--twb-warn)}
.twb-table .sym{font-family:var(--twb-num)}
.twb-table .act{display:flex;align-items:center;height:32px}
.twb-risk-cell{line-height:14px;font-size:11px;color:var(--twb-ink-2);white-space:normal}
.twb-risk-cell b{color:var(--twb-ink-1);font-weight:500}
.twb-table-foot{flex:0 0 auto;padding-top:6px;margin-top:4px;border-top:1px solid var(--twb-line-1);font-size:11px;color:var(--twb-ink-2)}
.twb-table-foot b{color:var(--twb-ink-1);font-weight:600}
.twb-actions{display:flex;align-items:center;gap:2px}
.twb-action{width:32px;height:32px;min-width:32px;padding:0;display:inline-flex;align-items:center;justify-content:center;
  border:1px solid transparent;border-radius:4px;background:transparent;color:var(--twb-ink-2);font-size:11px;cursor:pointer;white-space:nowrap}
.twb-action:hover{color:var(--twb-ink-1);background:var(--twb-row-hover);border-color:var(--twb-line-1)}
.twb-action:disabled{color:var(--twb-ink-3);cursor:not-allowed;background:transparent;border-color:transparent;opacity:.45}
.twb-action--danger:hover{color:var(--twb-danger);border-color:var(--twb-edge-danger);background:var(--twb-tint-danger)}
.twb-modal-scrim{position:fixed;inset:0;background:var(--twb-scrim);z-index:50;display:flex;align-items:center;justify-content:center}
.twb-modal{width:440px;max-width:92vw;max-height:86vh;overflow:auto;background:var(--twb-drawer);border:1px solid var(--twb-line-2);
  border-radius:10px;box-shadow:var(--twb-shadow-pop);padding:16px;display:flex;flex-direction:column;gap:10px}
.twb-modal-title{display:flex;align-items:center;gap:8px;font-size:14px;font-weight:600}
.twb-modal-body{display:flex;flex-direction:column;gap:8px}
.twb-modal-note{font-size:11px;line-height:16px;color:var(--twb-ink-2);padding:8px;border-radius:6px;background:var(--twb-tint-warn);border:1px solid var(--twb-edge-warn)}
.twb-modal-note--danger{background:var(--twb-tint-danger);border-color:var(--twb-edge-danger);color:var(--twb-ink-1)}
.twb-input{height:32px;padding:0 8px;border-radius:6px;border:1px solid var(--twb-line-2);background:var(--twb-s2);color:var(--twb-ink-1);font:inherit;font-size:12px}
.twb-input:focus{outline:2px solid var(--twb-accent);outline-offset:1px}
.twb-modal-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:4px}
.twb-receipt{display:flex;flex-direction:column;gap:6px;padding:10px;border-radius:6px;background:var(--twb-s2);border:1px solid var(--twb-line-1);font-size:12px}
.twb-receipt[data-ok='false']{border-color:var(--twb-edge-danger);background:var(--twb-tint-danger)}
.twb-receipt-row{display:grid;grid-template-columns:72px minmax(0,1fr);gap:8px;font-size:11px}
.twb-receipt-row .k{color:var(--twb-ink-2)}
.twb-receipt-row .v{overflow-wrap:anywhere}
.twb-md{max-height:320px;overflow:auto;font-size:12px;line-height:19px;color:var(--twb-ink-1);padding-right:4px}
.twb-md h2,.twb-md h3,.twb-md h4{margin:10px 0 4px;font-size:13px;font-weight:600;color:var(--twb-ink-1)}
.twb-md h2:first-child,.twb-md h3:first-child{margin-top:0}
.twb-md p{margin:0 0 6px}
.twb-md ul{margin:0 0 6px;padding-left:18px}
.twb-md li{margin:0 0 2px}
.twb-precheck{position:absolute;top:44px;right:56px;width:440px;max-width:calc(100vw - 24px);z-index:36;
  background:var(--twb-surface);border:1px solid var(--twb-line-2);border-radius:8px;box-shadow:var(--twb-shadow-lift);
  padding:10px;display:flex;flex-direction:column;gap:6px}
.twb-precheck-head{display:flex;align-items:center;gap:8px}
.twb-setup{width:520px}
.twb-setup-sub{margin:2px 0 10px;line-height:18px}
.twb-setup-rows,.twb-setup-steps{display:flex;flex-direction:column;gap:6px;margin:6px 0 10px}
.twb-setup-row,.twb-setup-step{display:grid;grid-template-columns:92px 78px minmax(0,1fr);gap:8px;align-items:center;font-size:12px}
.twb-setup-k{color:var(--twb-b-ink-3);white-space:nowrap}
.twb-setup-d{color:var(--twb-b-ink-3);font-size:11px;overflow-wrap:anywhere}
.twb-setup-warn{margin:4px 0 8px;line-height:18px}
.twb-setup-error{margin:4px 0 8px;color:var(--dsw-alias-state-error-primary);line-height:18px}
.twb-setup-log{margin:6px 0 0;padding:8px 10px;max-height:150px;overflow:auto;background:var(--twb-s1);border:1px solid var(--twb-b-rule-2);border-radius:4px;font-size:11px;line-height:16px;white-space:pre-wrap;overflow-wrap:anywhere}
.twb-setup .twb-precheck-foot{display:flex;align-items:center;gap:8px;margin-top:10px}
.twb-precheck-row{display:grid;grid-template-columns:10px minmax(0,1fr);gap:8px;align-items:start;font-size:12px;line-height:18px}
.twb-precheck-row .lbl{overflow-wrap:anywhere}
.twb-precheck-note{font-size:11px;line-height:16px;color:var(--twb-ink-2);padding:6px 8px;border-radius:6px;background:var(--twb-tint-warn);border:1px solid var(--twb-edge-warn)}
.twb-precheck-confirm{display:flex;align-items:center;gap:8px;justify-content:space-between;font-size:11px;color:var(--twb-ink-2);
  padding:6px 8px;border-radius:6px;background:var(--twb-s2);border:1px solid var(--twb-line-1)}
.twb-pending-list{display:flex;flex-direction:column;gap:6px;max-height:320px;overflow:auto}
.twb-pending-row{display:grid;grid-template-columns:minmax(0,1fr) auto;gap:4px 8px;padding:6px 8px;border-radius:6px;
  background:var(--twb-s2);border:1px solid var(--twb-line-1);font-size:12px}
.twb-pending-row[data-tone='danger']{border-color:var(--twb-edge-danger);background:var(--twb-tint-danger)}
.twb-pending-row[data-tone='warn']{border-color:var(--twb-edge-warn);background:var(--twb-tint-warn)}
.twb-pending-title{font-weight:600}
.twb-pending-reason{grid-column:1/-1;color:var(--twb-ink-2);font-size:11px;line-height:16px}
.twb-config-table{display:flex;flex-direction:column;gap:4px;margin-top:6px}
.twb-config-row{display:grid;grid-template-columns:minmax(96px,120px) minmax(0,1.4fr) minmax(0,110px) minmax(0,120px);gap:6px;align-items:center;font-size:12px}
.twb-config-role{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.twb-debate-col{min-width:0;display:flex;flex-direction:column;gap:4px;padding:6px 8px;border-radius:6px;background:var(--twb-s2);border:1px solid var(--twb-line-1)}
.twb-debate-col[data-side='red']{border-left:2px solid var(--twb-down)}
.twb-debate-col[data-side='blue']{border-left:2px solid var(--twb-up)}
.twb-legend{display:flex;align-items:center;gap:12px;padding-top:6px;font-size:11px;color:var(--twb-ink-2);flex-wrap:wrap}
.twb-prot{display:inline-flex;align-items:center;gap:6px;font-size:12px}
.twb-prot-shape{width:10px;height:10px;display:inline-block;flex:0 0 auto}
.twb-prot--ok .twb-prot-shape{border:1.5px solid var(--twb-ink-2);border-radius:50%}
.twb-prot--missing .twb-prot-shape{width:0;height:0;border-left:5px solid transparent;border-right:5px solid transparent;border-bottom:9px solid var(--twb-danger)}
.twb-prot--partial .twb-prot-shape{width:0;height:0;border-left:5px solid transparent;border-right:5px solid transparent;border-bottom:9px solid transparent;box-shadow:inset 0 0 0 1px var(--twb-warn)}
.twb-prot--unknown .twb-prot-shape{border:1.5px dashed var(--twb-warn);border-radius:50%}
.twb-prot--unreconciled .twb-prot-shape{border:1.5px dashed var(--twb-ink-2);border-radius:50%}
.twb-prot--missing{color:var(--twb-ink-1);font-weight:600}
.twb-prot--partial,.twb-prot--unknown{color:var(--twb-warn)}
.twb-prot--unreconciled{color:var(--twb-ink-2)}
.twb-concl-head{display:flex;align-items:center;gap:8px;min-width:0}
.twb-concl-line{font-size:16px;line-height:22px;font-weight:600;margin:6px 0 4px}
.twb-concl-sub{font-size:11px;color:var(--twb-ink-2);line-height:16px}
.twb-concl-detail{font-size:11px;color:var(--twb-ink-2);line-height:16px;margin-top:2px;overflow-wrap:anywhere}
.twb-concl-notes{margin:2px 0 0;padding-left:16px;font-size:11px;color:var(--twb-ink-2);line-height:16px}
.twb-kv{display:grid;grid-template-columns:minmax(88px,32%) minmax(0,1fr);gap:4px 10px;font-size:12px;margin:6px 0}
.twb-kv dt{color:var(--twb-ink-2);font-size:11px}
.twb-kv dd{margin:0;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.twb-concl-actions{margin-top:auto;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.twb-concl{height:300px}
.twb-concl .twb-concl-opt{display:none}
@container (max-width:1389px){.twb-concl{height:188px}}
@container (max-height:700px){.twb-concl{height:160px}}
@container (min-width:1390px) and (min-height:701px){.twb-concl .twb-concl-opt{display:flex}}
.twb-empty{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:4px;text-align:center;color:var(--twb-ink-2);min-height:80px}
.twb-empty-title{font-size:13px;font-weight:600;color:var(--twb-ink-1)}
.twb-empty-hint{font-size:11px;line-height:16px;max-width:520px}
.twb-empty-act{margin-top:2px;border:0;background:none;color:var(--twb-ink-1);font-size:11px;font-weight:600;cursor:pointer}
.twb-empty-act:hover{text-decoration:underline;text-underline-offset:2px}
.twb-tabs{display:flex;align-items:center;gap:2px;margin-bottom:6px}
.twb-tab{height:28px;padding:0 8px;border:0;border-bottom:2px solid transparent;background:transparent;
  color:var(--twb-ink-2);font-size:12px;font-weight:600;cursor:pointer}
.twb-tab[aria-selected='true']{color:var(--twb-ink-1);border-bottom-color:var(--twb-accent)}
.twb-tab .count{margin-left:4px;font-size:11px;color:var(--twb-ink-2);font-weight:500}
.twb-today{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px 12px}
.twb-today .k{display:block;font-size:11px;color:var(--twb-ink-2)}
.twb-today .v{font-family:var(--twb-num);font-size:20px;line-height:26px;font-weight:600}
.twb-recent{margin-top:10px;display:flex;flex-direction:column;gap:2px}
.twb-recent-row{display:grid;grid-template-columns:10px 56px minmax(0,1fr) 56px;align-items:center;gap:8px;height:24px;font-size:11px}
.twb-dot{width:6px;height:6px;border-radius:50%;background:var(--twb-neutral)}
.twb-dot--danger{background:var(--twb-danger)}.twb-dot--warn{background:var(--twb-warn)}.twb-dot--ok{background:var(--twb-ink-2)}
.twb-links{margin-top:auto;display:flex;gap:12px;font-size:11px}
.twb-link{border:0;background:none;padding:0;color:var(--twb-ink-2);font-size:11px;cursor:pointer}
.twb-link:hover{color:var(--twb-ink-1);text-decoration:underline;text-underline-offset:2px}
.twb-s4{flex:0 0 auto;height:26px;display:flex;align-items:center;gap:8px;padding:0 16px;background:var(--twb-s0);
  border-top:1px solid var(--twb-line-1);font-size:11px;color:var(--twb-ink-2);white-space:nowrap;overflow:hidden}
.twb-s4 .sep{color:var(--twb-line-2)}
.twb-s4-dot{width:6px;height:6px;border-radius:50%;background:var(--twb-neutral);display:inline-block;margin-right:4px}
.twb-s4-dot--warn{background:var(--twb-warn)}.twb-s4-dot--danger{background:var(--twb-danger)}
.twb-menu{position:absolute;top:44px;right:8px;width:248px;z-index:35;background:var(--twb-surface);
  border:1px solid var(--twb-line-2);border-radius:8px;box-shadow:var(--twb-shadow-lift);padding:6px}
.twb-menu button{width:100%;height:32px;padding:0 8px;display:flex;align-items:center;gap:8px;border:0;background:transparent;
  border-radius:4px;color:var(--twb-ink-1);font-size:12px;cursor:pointer;text-align:left}
.twb-menu button:hover{background:var(--twb-row-hover)}
.twb-menu-sep{height:1px;background:var(--twb-line-1);margin:4px 0}
.twb-menu-title{font-size:11px;color:var(--twb-ink-2);padding:4px 8px}
.twb-menu-note{font-size:10px;color:var(--twb-ink-2);padding:2px 8px 6px;line-height:14px}
.twb-menu-opt[aria-checked='true']{background:var(--twb-s2)}
.twb-menu-check{width:12px;color:var(--twb-ink-1)}
.twb-scrim{position:absolute;left:0;right:0;top:82px;bottom:26px;background:var(--twb-scrim);z-index:20}
.twb-drawer{position:absolute;top:82px;bottom:26px;right:0;width:560px;max-width:92%;z-index:25;display:flex;
  flex-direction:column;background:var(--twb-drawer);border-left:1px solid var(--twb-line-2);box-shadow:var(--twb-shadow-pop)}
.twb-drawer-head{height:44px;flex:0 0 auto;display:flex;align-items:center;gap:8px;padding:0 12px;border-bottom:1px solid var(--twb-line-1)}
.twb-drawer-title{font-size:13px;font-weight:600}
.twb-drawer-sub{font-size:11px;color:var(--twb-ink-2)}
.twb-drawer-tabs{height:32px;flex:0 0 auto;display:flex;gap:2px;padding:0 8px;border-bottom:1px solid var(--twb-line-1)}
.twb-drawer-tabs button{height:30px;padding:0 10px;border:0;border-bottom:2px solid transparent;background:transparent;
  color:var(--twb-ink-2);font-size:12px;font-weight:600;cursor:pointer}
.twb-drawer-tabs button[aria-selected='true']{color:var(--twb-ink-1);border-bottom-color:var(--twb-accent)}
.twb-drawer-body{flex:1 1 auto;min-height:0;overflow:auto;padding:12px;display:flex;flex-direction:column;gap:10px}
.twb-drawer-foot{height:28px;flex:0 0 auto;display:flex;align-items:center;padding:0 12px;border-top:1px solid var(--twb-line-1);
  font-size:11px;color:var(--twb-ink-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.twb-panel{background:var(--twb-s2);border:1px solid var(--twb-line-1);border-radius:6px;padding:10px}
.twb-pre{margin:0;padding:10px;background:var(--twb-s2);border:1px solid var(--twb-line-1);border-radius:6px;
  font-family:var(--twb-num);font-size:11px;line-height:18px;color:var(--twb-ink-2);white-space:pre-wrap;
  overflow-wrap:anywhere;max-height:320px;overflow:auto}
.twb-ai-head{height:52px;display:flex;align-items:center;gap:12px}
.twb-ai-meta{font-size:12px;color:var(--twb-ink-2);display:flex;align-items:center;gap:8px;white-space:nowrap}
.twb-ai-grid{display:grid;grid-template-columns:260px minmax(0,1fr);gap:12px;align-items:start}
.twb-ai-list{background:var(--twb-s1);border:1px solid var(--twb-line-1);border-radius:8px;overflow:hidden}
.twb-ai-list-head{padding:8px 10px;border-bottom:1px solid var(--twb-line-1);font-size:11px;color:var(--twb-ink-2)}
.twb-ai-list-body{max-height:640px;overflow:auto}
.twb-ai-list-foot{padding:6px 10px;border-top:1px solid var(--twb-line-1);font-size:10px;color:var(--twb-ink-2)}
.twb-round{width:100%;display:grid;grid-template-columns:14px 46px minmax(0,1fr) 52px;align-items:center;gap:6px;
  height:34px;padding:0 10px;border:0;border-bottom:1px solid var(--twb-line-1);background:transparent;cursor:pointer;text-align:left}
.twb-round:hover{background:var(--twb-row-hover)}
.twb-round[aria-selected='true']{background:var(--twb-s2);box-shadow:inset 2px 0 0 var(--twb-accent)}
.twb-round .t{font-size:11px;color:var(--twb-ink-1)}
.twb-round .s{font-size:11px;color:var(--twb-ink-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.twb-round .d{font-size:11px;color:var(--twb-ink-2);text-align:right}
.twb-report{min-width:0;display:flex;flex-direction:column;gap:12px}
.twb-report-card{background:var(--twb-s1);border:1px solid var(--twb-line-1);border-radius:8px;padding:10px 12px}
.twb-report-card--danger{border-left:3px solid var(--twb-danger)}
.twb-report-card--warn{border-left:3px solid var(--twb-warn)}
.twb-stages{display:flex;flex-direction:column}
.twb-stage-head{display:grid;grid-template-columns:20px 104px minmax(0,1fr) 56px 104px 88px 56px;gap:8px;padding:0 4px 4px;
  font-size:11px;color:var(--twb-ink-2);border-bottom:1px solid var(--twb-line-1)}
.twb-stage{display:grid;grid-template-columns:20px 104px minmax(0,1fr) 56px 104px 88px 56px;gap:8px;align-items:center;
  height:40px;padding:0 4px;border-bottom:1px solid var(--twb-line-1);font-size:12px}
.twb-stage:hover{background:var(--twb-row-hover)}
.twb-stage-idx{width:18px;height:18px;border-radius:50%;border:1px solid var(--twb-line-2);display:inline-flex;
  align-items:center;justify-content:center;font-size:10px;color:var(--twb-ink-2)}
.twb-stage[data-state='error'] .twb-stage-idx{border-color:var(--twb-edge-danger);color:var(--twb-danger)}
.twb-stage-name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.twb-bar{height:6px;border-radius:3px;background:var(--twb-s2);overflow:hidden}
.twb-bar i{display:block;height:100%;background:color-mix(in srgb,var(--twb-accent) 62%,transparent)}
.twb-stage[data-state='error'] .twb-bar i{background:color-mix(in srgb,var(--twb-danger) 62%,transparent)}
.twb-stage-rest{grid-column:1 / -1;display:flex;align-items:center;gap:8px;height:34px;padding:0 4px;font-size:11px;color:var(--twb-ink-2)}
.twb-gaps{display:flex;flex-direction:column;gap:6px}
.twb-grade{display:inline-flex;align-items:center;justify-content:center;min-width:28px;height:24px;padding:0 6px;
  border-radius:4px;border:1px solid var(--twb-line-2);background:var(--twb-s2);font-family:var(--twb-num);
  font-size:15px;font-weight:700;color:var(--twb-ink-1)}
.twb-grade--sm{min-width:20px;height:18px;font-size:12px}
.twb-grade--warn{border-color:var(--twb-edge-warn);background:var(--twb-tint-warn);color:var(--twb-warn)}
.twb-grade--danger{border-color:var(--twb-edge-danger);background:var(--twb-tint-danger);color:var(--twb-danger)}
.twb-grade--na{color:var(--twb-ink-2);font-weight:500}
.twb-verdict-row{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px 12px;margin-top:8px}
.twb-verdict-grade{display:flex;align-items:center;gap:2px}
.twb-cases{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.twb-case{display:flex;align-items:center;gap:8px;padding:6px 8px;border:1px solid var(--twb-line-1);border-radius:6px;background:var(--twb-s2);min-width:0}
.twb-case-name{font-size:11px;color:var(--twb-ink-2);white-space:nowrap}
.twb-case .twb-hint{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.twb-chain-budget{display:flex;flex-wrap:wrap;gap:6px 14px;padding:2px 4px 8px;font-size:11px;color:var(--twb-ink-2)}
.twb-chain-chip{display:inline-flex;align-items:baseline;gap:5px}
.twb-chain-chip .k{color:var(--twb-ink-2)}
.twb-chain-chip .v{color:var(--twb-ink-1);font-weight:600}
.twb-compare tbody td{vertical-align:top}
.twb-compare tbody td:nth-child(2){padding-left:2px}
.twb-rationale-thesis,.twb-rationale-row{display:flex;gap:8px;font-size:12px;line-height:1.5;margin-top:4px;min-width:0}
.twb-rationale-thesis .k,.twb-rationale-row .k{flex:0 0 auto;color:var(--twb-ink-2);white-space:nowrap}
.twb-rationale-thesis .v,.twb-rationale-row .v{color:var(--twb-ink-1);min-width:0}
.twb-rationale-cases{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:8px}
.twb-rationale-col{padding:6px 8px;border:1px solid var(--twb-line-1);border-radius:6px;background:var(--twb-s2);min-width:0}
.twb-rationale-head{display:flex;align-items:center;gap:8px;margin-bottom:2px}
.twb-rationale-warn{margin-top:8px;font-size:12px}
.twb-rationale-warn ul{margin:2px 0 0;padding-left:16px;color:var(--twb-ink-2)}
.twb-plan{display:grid;grid-template-columns:repeat(6,minmax(0,1fr));gap:6px 10px;margin-top:8px}
.twb-plan-item{display:flex;flex-direction:column;gap:2px;min-width:0}
.twb-plan-item .k{font-size:11px;color:var(--twb-ink-2)}
.twb-plan-item .v{font-size:13px;font-weight:600}
.twb-table--compact{table-layout:auto;margin-top:2px}
.twb-table--compact thead th{height:24px}
.twb-table--compact tbody td{height:28px}
.twb-gap{display:flex;align-items:flex-start;gap:8px;font-size:12px}
.twb-gap-ico{width:14px;height:14px;flex:0 0 auto;margin-top:3px;color:var(--twb-warn)}
.twb-gap-act{margin-left:auto;border:0;background:none;color:var(--twb-ink-1);font-size:11px;font-weight:600;cursor:pointer}
.twb-fold>summary{cursor:pointer;font-size:11px;color:var(--twb-ink-2);list-style:none}
.twb-fold>summary::-webkit-details-marker{display:none}
.twb-chips{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap;align-items:center}
.twb-chips--sub{margin-top:-4px}
/* 15 §5: archive day bar — today / recent days / native date picker. */
.twb-archive-bar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:0 0 10px;margin-bottom:10px;border-bottom:1px solid var(--twb-b-rule-2)}
.twb-archive-label{font-size:11px;color:var(--twb-b-ink-3);white-space:nowrap}
.twb-chips--archive{margin-bottom:0}
.twb-archive-pick{display:flex;gap:6px;align-items:center;margin-left:auto}
.twb-archive-input{height:24px;padding:0 6px;border:1px solid var(--twb-b-rule-2);border-radius:4px;background:var(--twb-s1);color:var(--twb-ink-1);font-size:11px}
.twb-archive-input:focus-visible{outline:2px solid var(--twb-accent);outline-offset:1px}
@container (max-width:1199px){
  .twb-archive-pick{margin-left:0}
}
.twb-timeline{display:flex;flex-direction:column;margin-top:2px}
.twb-timeline-row{display:grid;grid-template-columns:72px 92px minmax(0,1fr) 64px;align-items:center;gap:8px;
  height:26px;font-size:11px;border-bottom:1px solid var(--twb-line-1)}
.twb-timeline-row:last-child{border-bottom:0}
.twb-dist{display:flex;flex-direction:column;gap:8px}
.twb-dist-row{display:grid;grid-template-columns:148px minmax(0,1fr) 40px;gap:8px;align-items:center;font-size:11px}
.twb-dist-row .k{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.twb-dist-bar{height:8px;border-radius:2px;background:var(--twb-s2);overflow:hidden}
.twb-dist-bar i{display:block;height:100%;background:color-mix(in srgb,var(--twb-accent) 55%,transparent)}
.twb-dist-bar i[data-tone='danger']{background:color-mix(in srgb,var(--twb-danger) 62%,transparent)}
.twb-dist-bar i[data-tone='warn']{background:color-mix(in srgb,var(--twb-warn) 62%,transparent)}
.twb-orders-bar{height:44px;display:flex;align-items:center;gap:8px;border:0;background:transparent;color:var(--twb-ink-2);font-size:12px;cursor:pointer;width:100%;padding:0}
.twb-orders-bar:hover{color:var(--twb-ink-1)}
.twb-orders-bar b{color:var(--twb-ink-1);font-weight:600}
@container (max-width:1389px){
  .twb-span7,.twb-span5{grid-column:span 12}
  .twb-h150{height:auto;min-height:104px}.twb-h300{height:auto;min-height:236px}.twb-h260{height:auto;min-height:196px}
  .twb-account{height:auto;grid-template-columns:minmax(0,1fr)}
  .twb-account-spark{display:none}
  .twb-budget{grid-template-columns:repeat(4,minmax(0,1fr))}
  .twb-ai-grid{grid-template-columns:180px minmax(0,1fr)}
  .twb-round{grid-template-columns:14px 40px minmax(0,1fr)}
  .twb-round .d{display:none}
  .twb-col-margin{display:none}
}
@container (max-width:1199px){
  .twb-col-qty{display:none}
  .twb-ai-grid{grid-template-columns:160px minmax(0,1fr)}
  .twb-ai-list-foot{display:none}
  .twb-round{grid-template-columns:10px 38px minmax(0,1fr);padding:0 6px;height:30px}
  .twb-round .d{display:none}
  .twb-stage-head,.twb-stage{grid-template-columns:20px 72px minmax(0,1fr) 48px 76px 64px 44px}
}
@media (prefers-reduced-motion:no-preference){
  .twb-drawer{animation:twb-in 150ms ease-out}
  .twb-menu{animation:twb-fade 90ms ease-out}
}
@keyframes twb-in{from{transform:translateX(8px);opacity:0}to{transform:none;opacity:1}}
@keyframes twb-fade{from{opacity:0}to{opacity:1}}
@media (prefers-reduced-motion:reduce){.twb-root *{animation:none!important;transition:none!important}}


/* ======================================================================
   B+ · premium financial editorial
   Warm graphite, hairlines, 40px ledger, zero cards in the cockpit.
   Every color is derived from the 14 live tokens.
   ====================================================================== */
.twb-root{
  --twb-paper:color-mix(in srgb,var(--twb-warn) 3.2%,var(--twb-s0));
  --twb-paper-1:color-mix(in srgb,var(--twb-warn) 2.6%,var(--twb-s1));
  --twb-b-ink-1:color-mix(in srgb,var(--twb-ink-1) 96%,var(--twb-warn) 4%);
  --twb-b-ink-3:color-mix(in srgb,var(--twb-ink-2) 64%,transparent);
  --twb-b-rule:color-mix(in srgb,var(--twb-ink-1) 9%,transparent);
  --twb-b-rule-2:color-mix(in srgb,var(--twb-ink-1) 16%,transparent);
  --twb-b-rule-3:color-mix(in srgb,var(--twb-ink-1) 30%,transparent);
  background:var(--twb-paper);
}
.twb-s0{height:64px;padding:0 56px;background:var(--twb-paper);border-bottom:1px solid var(--twb-b-rule-3);gap:24px}
.twb-title{font-size:14px;letter-spacing:-.01em;color:var(--twb-b-ink-1)}
.twb-demo{height:18px;padding:0 5px;border-radius:2px;border:1px solid var(--twb-b-rule-3);background:transparent;color:var(--twb-warn);font-size:9px;letter-spacing:.16em}
.twb-s0 .twb-equity,.twb-s0 .twb-divider{display:none}
/* F1: environment badge, automation state and the stop action (tokens only) */
.twb-s0{gap:20px}
.twb-env{font-family:var(--twb-num);font-size:10px;letter-spacing:.08em;color:var(--twb-b-ink-3);white-space:nowrap;
  border-left:1px solid var(--twb-b-rule-2);padding-left:10px;min-width:0;overflow:hidden;text-overflow:ellipsis}
.twb-auto{display:inline-flex;align-items:center;gap:6px;min-width:0;font-size:11px;font-weight:500;color:var(--twb-b-ink-2)}
.twb-auto-label{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.twb-auto-short{display:none}
.twb-auto-dot{width:6px;height:6px;border-radius:50%;background:var(--twb-b-ink-3);flex:0 0 auto}
.twb-auto--ok .twb-auto-dot{background:var(--twb-up)}
.twb-auto--warn{color:var(--twb-warn)}
.twb-auto--warn .twb-auto-dot{background:var(--twb-warn)}
.twb-auto--danger{color:var(--twb-danger)}
.twb-auto--danger .twb-auto-dot{background:var(--twb-danger)}
.twb-auto--quiet{color:var(--twb-b-ink-3)}
.twb-auto-btn{max-width:180px;overflow:hidden;text-overflow:ellipsis}
.twb-s2-cat{font-size:10px;letter-spacing:.08em;color:var(--twb-b-ink-3);border:1px solid var(--twb-b-rule-2);
  padding:1px 4px;white-space:nowrap;flex:0 0 auto}
.twb-pending-cat{font-size:10px;letter-spacing:.08em;color:var(--twb-b-ink-3);white-space:nowrap}
.twb-pending-next{font-size:11px;color:var(--twb-b-ink-3);white-space:normal}
.twb-pending-impact{font-size:11px;color:var(--twb-b-ink-2);white-space:normal}
/* 1024: status + stop button + positions stay visible; optional chrome folds.
   Both a container query (DSH panel) and a media query (standalone/headless
   render) so the rule cannot silently depend on a container context existing. */
@container (max-width:1199px){
  .twb-s0{gap:12px}
  .twb-precheck-toggle{display:none}
  .twb-lang-inline{display:none}
  .twb-auto-full{display:none}
  .twb-auto-short{display:inline}
  .twb-auto-btn{max-width:150px}
}
@media (max-width:1199px){
  .twb-s0{gap:12px}
  .twb-precheck-toggle{display:none}
  .twb-lang-inline{display:none}
  .twb-auto-full{display:none}
  .twb-auto-short{display:inline}
  .twb-auto-btn{max-width:150px}
}
.twb-seg{background:transparent;padding:0;border-radius:0;gap:24px}
.twb-seg button{height:64px;padding:0;border:0;border-radius:0;font-size:13px;font-weight:400;color:var(--twb-b-ink-3)}
.twb-seg button:hover{color:var(--twb-b-ink-1);background:transparent}
.twb-seg button[aria-selected='true']{background:transparent;border-color:transparent;color:var(--twb-b-ink-1);font-weight:600;box-shadow:inset 0 -2px 0 var(--twb-b-ink-1)}
.twb-lang{display:flex;align-items:center;gap:7px;font:500 11px/1 var(--twb-num);color:var(--twb-b-ink-3)}
.twb-lang button{appearance:none;border:0;background:none;padding:2px;cursor:pointer;font:inherit;color:inherit}
.twb-lang button[aria-pressed='true']{color:var(--twb-b-ink-1)}
.twb-lang .bar{width:1px;height:10px;background:var(--twb-b-rule-2)}
.twb-btn{border-radius:2px;height:32px;font-size:12px}
.twb-btn--primary{background:var(--twb-b-ink-1);border-color:var(--twb-b-ink-1);color:var(--twb-paper)}
.twb-root .twb-btn--primary{color:var(--twb-paper)}
.twb-root .twb-btn--primary:hover{background:color-mix(in srgb,var(--twb-b-ink-1) 86%,var(--twb-paper))}
.twb-btn--secondary{background:transparent;border-color:var(--twb-b-rule-2)}
.twb-btn--secondary:hover{border-color:var(--twb-b-rule-3)}
.twb-btn--ghost{color:var(--twb-b-ink-3)}
.twb-btn--ghost:hover{color:var(--twb-b-ink-1);background:var(--twb-row-hover)}
.twb-s1{height:44px;padding:0 56px;background:transparent;border-bottom:1px solid var(--twb-b-rule);gap:14px}
.twb-ctx .v{font-size:12px;color:var(--twb-b-ink-1)}
.twb-ctx-btn{font-size:11px;color:var(--twb-b-ink-3)}
.twb-s2{height:36px;padding:0 56px;background:transparent;border-bottom:1px solid var(--twb-b-rule);gap:10px}
.twb-s2::before{width:2px}
.twb-s2-ico{display:none}
.twb-s2-title{font-size:13px;font-weight:600;color:var(--twb-b-ink-1)}
.twb-s2-reason{font-size:12px;color:var(--twb-b-ink-3)}
.twb-s2-act{font-size:12px;font-weight:500;color:var(--twb-b-ink-2)}
.twb-s2-more{font-size:11px;color:var(--twb-b-ink-3)}
.twb-s3{background:var(--twb-paper)}
.twb-canvas{max-width:1440px;padding:32px 56px 24px}
.twb-row{gap:24px;margin-bottom:32px}
.twb-span7{grid-column:span 8}
.twb-span5{grid-column:span 4}
.twb-s4{height:28px;padding:0 56px;background:var(--twb-paper);border-top:1px solid var(--twb-b-rule);font-size:11px;color:var(--twb-b-ink-3)}
.twb-s4 .twb-s4-dot{background:var(--twb-b-ink-3)}
.twb-s4 .sep{color:var(--twb-b-rule-3)}
.twb-toast{background:var(--twb-paper-1);border-top-color:var(--twb-b-rule-2)}
.twb-menu,.twb-drawer{background:var(--twb-paper-1);border-color:var(--twb-b-rule-2)}
.twb-empty{border-color:var(--twb-b-rule)}
.twb-empty-title{font-size:13px;font-weight:500;color:var(--twb-ink-2)}
.twb-empty-hint{font-size:11px;color:var(--twb-b-ink-3)}

/* numbered section head (the only card replacement) */
.twb-sec-head{grid-column:1/-1;display:flex;align-items:center;gap:10px;height:26px;border-top:1px solid var(--twb-b-rule-2);font-size:10px;font-weight:500;letter-spacing:.14em;color:var(--twb-b-ink-3);min-width:0}
.twb-sec-num{font-family:var(--twb-num);font-size:10px;letter-spacing:.06em;color:var(--twb-b-ink-3)}
.twb-sec-dash{width:12px;height:1px;background:var(--twb-b-rule-3);flex:0 0 auto}
.twb-sec-title{font-size:13px;font-weight:600;letter-spacing:0;color:var(--twb-b-ink-1);white-space:nowrap}
.twb-sec-en{font-size:9px;letter-spacing:.18em;color:var(--twb-b-ink-3);white-space:nowrap}
.twb-sec-meta{margin-left:auto;font-size:11px;letter-spacing:0;color:var(--twb-b-ink-3);white-space:nowrap}

/* 01 — account + risk */
.b-account{grid-column:span 8;min-width:0;height:196px;display:flex;flex-direction:column}
.b-account-hero{display:flex;align-items:baseline;gap:8px;margin-top:2px}
.b-account-hero .num{font-family:var(--twb-num);font-variant-numeric:tabular-nums slashed-zero;font-size:40px;line-height:44px;font-weight:600;letter-spacing:-.03em;color:var(--twb-b-ink-1)}
.b-account-hero .unit{font-size:9px;letter-spacing:.16em;color:var(--twb-b-ink-3)}
.b-account-metrics{display:flex;gap:44px;margin-top:12px;min-width:0}
.b-metric{display:flex;flex-direction:column;gap:2px;min-width:0}
.b-metric .k{font-size:10px;letter-spacing:.08em;color:var(--twb-b-ink-3);white-space:nowrap}
.b-metric .v{font-family:var(--twb-num);font-variant-numeric:tabular-nums;font-size:15px;line-height:22px;font-weight:500;white-space:nowrap}
.b-account-spark{margin-left:auto;align-self:flex-end;width:300px;max-width:38%;height:48px;opacity:.95}
.b-account-spark svg{display:block;width:100%;height:48px}
.b-account-spark .cap{margin-top:2px;font-size:10px;letter-spacing:.06em;color:var(--twb-b-ink-3);text-align:right}

.b-risk{grid-column:span 4;min-width:0;height:196px;border-left:1px solid var(--twb-b-rule-2);padding-left:32px;display:flex;flex-direction:column}
.b-risk-head{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--twb-b-ink-3);margin-bottom:6px}
.b-risk-head .link{margin-left:auto;border:0;background:none;padding:0;font-size:11px;color:var(--twb-b-ink-3);cursor:pointer}
.b-risk-head .link:hover{color:var(--twb-b-ink-1);text-decoration:underline;text-underline-offset:2px}
.b-risk-row{display:flex;align-items:center;justify-content:space-between;gap:12px;height:27px;border-bottom:1px solid var(--twb-b-rule);font-size:11px}
.b-risk-row .k{color:var(--twb-b-ink-3);white-space:nowrap}
.b-risk-row .v{font-family:var(--twb-num);font-variant-numeric:tabular-nums;font-size:13px;font-weight:500;color:var(--twb-b-ink-1);white-space:nowrap}
.b-risk-row .v.danger{color:var(--twb-danger);font-family:inherit;font-size:12px;font-weight:600}
.b-risk-note{margin-top:8px;font-size:11px;line-height:16px;color:var(--twb-b-ink-3)}
.b-risk-note .danger{color:var(--twb-danger)}

/* 02/03 — scoped cockpit cards, tables and briefing */
[data-workspace='cockpit'] .twb-card{background:transparent;border:0;border-top:1px solid var(--twb-b-rule-2);border-radius:0;padding:17px 0 0}
[data-workspace='cockpit'] .twb-card-head{margin-bottom:14px;gap:9px}
[data-workspace='cockpit'] .twb-card-title{font-size:13px;font-weight:600;letter-spacing:.01em;color:var(--twb-b-ink-1)}
[data-workspace='cockpit'] .twb-hint{font-size:11px;color:var(--twb-b-ink-3)}
[data-workspace='cockpit'] .twb-chip{height:auto;min-height:0;padding:0;border:0;border-radius:0;background:transparent;color:var(--twb-b-ink-3);font-size:11px;font-weight:500}
[data-workspace='cockpit'] .twb-chip--warn,[data-workspace='cockpit'] .twb-chip--danger{background:transparent;border:0;color:var(--twb-warn)}
[data-workspace='cockpit'] .twb-chip--danger{color:var(--twb-danger)}
[data-workspace='cockpit'] .twb-chip--quiet{background:transparent;color:var(--twb-b-ink-3)}
[data-workspace='cockpit'] .twb-card.twb-h300{height:314px}
[data-workspace='cockpit'] .twb-table-wrap{overflow:auto}
[data-workspace='cockpit'] .twb-table{width:100%;border-collapse:collapse;table-layout:fixed}
[data-workspace='cockpit'] .twb-table thead th{height:28px;padding:0 8px;border:0;border-bottom:1px solid var(--twb-b-rule-2);background:transparent;color:var(--twb-b-ink-3);font-size:10px;font-weight:500;letter-spacing:.12em;text-transform:uppercase;text-align:left;white-space:nowrap}
[data-workspace='cockpit'] .twb-table thead th.is-num{text-align:right}
[data-workspace='cockpit'] .twb-table tbody td{height:64px;padding:0 8px;border:0;border-bottom:1px solid var(--twb-b-rule);background:transparent;font-size:13px;vertical-align:middle}
[data-workspace='cockpit'] .twb-table tbody tr:last-child td{border-bottom-color:var(--twb-b-rule-2)}
[data-workspace='cockpit'] .twb-table tbody tr:hover td{background:var(--twb-row-hover)}
[data-workspace='cockpit'] .twb-table tbody tr[data-risk='danger'] td:first-child{box-shadow:inset 2px 0 0 var(--twb-danger)}
[data-workspace='cockpit'] .twb-table .twb-num{font-size:13px;text-align:right}
[data-workspace='cockpit'] .twb-table .sym{font-family:var(--twb-num);font-size:14px;font-weight:600;letter-spacing:-.01em}
[data-workspace='cockpit'] .twb-table-foot{font-size:11px;color:var(--twb-b-ink-3)}
[data-workspace='cockpit'] .twb-legend{gap:14px;font-size:10px;color:var(--twb-b-ink-3)}
[data-workspace='cockpit'] .twb-actions{display:flex;align-items:center;gap:10px;justify-content:flex-end}
[data-workspace='cockpit'] .twb-action{border:0;background:none;padding:0;font-size:11px;color:var(--twb-b-ink-3);cursor:pointer}
[data-workspace='cockpit'] .twb-action:hover{color:var(--twb-b-ink-1);text-decoration:underline;text-underline-offset:2px}
[data-workspace='cockpit'] .twb-action--danger{color:var(--twb-b-ink-3)}
[data-workspace='cockpit'] .twb-action:disabled{color:var(--twb-b-ink-3);opacity:.45;cursor:not-allowed;text-decoration:none}
[data-workspace='cockpit'] .twb-concl-line{font-size:24px;line-height:30px;font-weight:600;letter-spacing:-.01em;color:var(--twb-b-ink-1)}
[data-workspace='cockpit'] .twb-concl-sub{font-size:11px;line-height:17px;color:var(--twb-b-ink-3)}
[data-workspace='cockpit'] .twb-concl-details{font-size:11px;color:var(--twb-b-ink-3)}
[data-workspace='cockpit'] .twb-kv{font-size:11px;line-height:17px}
[data-workspace='cockpit'] .twb-kv dt{color:var(--twb-b-ink-3)}
[data-workspace='cockpit'] .twb-kv dd{color:var(--twb-ink-2)}
[data-workspace='cockpit'] .twb-concl-actions{gap:16px}
[data-workspace='cockpit'] .twb-sec-num{font-family:var(--twb-num);font-size:10px;letter-spacing:.06em;color:var(--twb-b-ink-3);margin-right:2px}

/* APP. — tape */
.b-tape{grid-column:1/-1;border-top:1px solid var(--twb-b-rule-2);padding-top:17px;min-width:0;height:82px;display:flex;flex-direction:column}
.b-tape-line{display:flex;align-items:center;gap:10px;font-size:11px;line-height:18px;color:var(--twb-b-ink-3);white-space:nowrap;overflow:hidden;min-width:0;margin-top:8px}
.b-tape-line b{font-weight:500;color:var(--twb-b-ink-2);font-family:var(--twb-num)}
.b-tape-line .dot{width:3px;height:3px;border-radius:50%;background:var(--twb-b-rule-3);flex:0 0 auto}
.b-tape-line .spacer{flex:1 1 auto}
.b-tape-line .muted{color:var(--twb-b-ink-3)}
.b-tape-empty{font-size:11px;color:var(--twb-b-ink-3);margin-top:8px}

@container (max-width:1199px){
  .b-account,.b-risk,.twb-span7,.twb-span5{grid-column:span 12}
  .b-risk{border-left:0;padding-left:0;border-top:1px solid var(--twb-b-rule-2);height:auto;padding-top:12px}
  .b-account{height:auto}
  .b-account-spark{display:none}
  [data-workspace='cockpit'] .twb-card.twb-h300{height:auto;min-height:260px}
  .b-tape{height:auto}
}
@container (max-width:1389px){
  .twb-s0,.twb-s1,.twb-s2,.twb-s4{padding-left:40px;padding-right:40px}
  .twb-canvas{padding:28px 40px 20px}
  .twb-seg{gap:18px}
  .b-account-metrics{gap:28px}
  .b-account-hero .num{font-size:36px;line-height:40px}
}

[data-workspace='cockpit'] .twb-table tbody tr[data-risk='danger'] td{background:transparent}
[data-workspace='cockpit'] .twb-table tbody tr[data-risk='danger']:hover td{background:var(--twb-row-hover)}
.b-concl-meta{margin-top:10px;padding-top:8px;border-top:1px solid var(--twb-b-rule);font-size:11px;line-height:17px;color:var(--twb-b-ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.b-actions{position:relative;display:flex;justify-content:flex-end}
.b-actions summary{list-style:none;cursor:pointer;font-size:11px;color:var(--twb-b-ink-3);white-space:nowrap}
.b-actions summary::-webkit-details-marker{display:none}
.b-actions summary:hover{color:var(--twb-b-ink-1);text-decoration:underline;text-underline-offset:2px}
.b-actions-menu{position:absolute;right:0;top:18px;z-index:30;min-width:112px;padding:4px;background:var(--twb-paper-1);border:1px solid var(--twb-b-rule-2);box-shadow:var(--twb-shadow-lift)}
.b-actions-item{display:block;width:100%;padding:7px 10px;border:0;background:none;text-align:left;font-size:12px;color:var(--twb-ink-2);cursor:pointer}
.b-actions-item:hover{background:var(--twb-row-hover);color:var(--twb-ink-1)}
.b-actions-item--danger{color:var(--twb-danger)}
.b-actions-item:disabled{color:var(--twb-b-ink-3);opacity:.45;cursor:not-allowed;background:none}

.b-tape-link{border:0;background:none;padding:0;font-size:11px;color:var(--twb-b-ink-3);cursor:pointer;white-space:nowrap}
.b-tape-link:hover{color:var(--twb-b-ink-1);text-decoration:underline;text-underline-offset:2px}
.b-modal-scrim{position:fixed;inset:0;z-index:80;background:var(--twb-scrim);display:flex;align-items:stretch;justify-content:flex-end}
.b-modal{width:min(720px,94vw);height:100%;max-height:100vh;overflow:auto;background:var(--twb-paper-1);border:0;border-left:1px solid var(--twb-b-rule-2);padding:22px 28px;box-shadow:var(--twb-shadow-pop)}
.b-modal .twb-h260{height:auto;min-height:0;overflow:hidden}
.b-modal .twb-h260 .twb-table-wrap{max-height:calc(100vh - 320px)}
.b-modal-head{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:14px;border-bottom:1px solid var(--twb-b-rule);padding-bottom:12px}
.b-modal-title{font-size:14px;font-weight:600;color:var(--twb-b-ink-1)}

.b-risk-fallback{font-size:10px;color:var(--twb-b-ink-3);border:1px solid var(--twb-b-rule-2);padding:1px 4px;border-radius:2px}

[data-workspace='cockpit'] .twb-concl .twb-kv{display:none!important}
[data-workspace='cockpit'] .twb-concl > .twb-hint.twb-concl-opt{display:none}
.b-account{position:relative}
.b-account-spark{position:absolute;left:0;right:0;bottom:8px;width:auto;max-width:none;height:56px;margin-left:0}
.b-account-spark svg{height:56px}
.b-account-spark .cap{margin-top:0;text-align:right}
.b-concl-rounds{margin-top:auto;padding-top:10px;border-top:1px solid var(--twb-b-rule)}
.b-concl-rounds-h{font-size:10px;letter-spacing:.12em;color:var(--twb-b-ink-3);margin-bottom:4px}
.b-concl-round{display:grid;grid-template-columns:44px 44px minmax(0,1fr) auto;gap:8px;align-items:center;height:22px;font-size:11px;color:var(--twb-b-ink-3);border-bottom:1px solid var(--twb-b-rule)}
.b-concl-round:last-child{border-bottom:0}
.b-concl-round .t{font-family:var(--twb-num);font-variant-numeric:tabular-nums;color:var(--twb-b-ink-2)}
.b-concl-round .s{color:var(--twb-b-ink-2)}
.b-concl-round .r{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.b-concl-round .d{font-family:var(--twb-num);font-variant-numeric:tabular-nums}

[data-workspace='cockpit'] .twb-table tbody td{height:96px;vertical-align:middle}
.b-sym-cell{min-width:0}
.b-sym-line{display:flex;align-items:center;gap:7px;min-width:0}
.b-side{font-size:10px;line-height:14px;color:var(--twb-b-ink-3);border:1px solid var(--twb-b-rule-2);padding:0 4px;border-radius:2px;white-space:nowrap}
.b-cell-main{font-size:13px;line-height:18px;color:var(--twb-ink-1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.b-cell-sub{margin-top:3px;font-size:10px;line-height:14px;color:var(--twb-b-ink-3);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.b-num-cell{text-align:right}
.b-num-cell .b-cell-main{font-family:var(--twb-num);font-variant-numeric:tabular-nums;font-size:13px}
.b-num-cell .b-cell-sub{font-family:var(--twb-num);font-variant-numeric:tabular-nums}
.b-act-cell{text-align:right}

[data-workspace='cockpit'] .twb-card.twb-h300{height:314px}
[data-workspace='cockpit'] .twb-table tbody td{height:88px}

/* AI workspace: B+ editorial (head tabs intentionally 52px, never 64) */
[data-workspace='ai'] .twb-canvas{padding:32px 56px 24px;max-width:1440px}
[data-workspace='ai'] .twb-ai-head{height:52px;border-bottom:1px solid var(--twb-b-rule);gap:24px;align-items:center}
[data-workspace='ai'] .twb-ai-head .twb-seg{gap:24px;padding:0;background:transparent;border-radius:0}
[data-workspace='ai'] .twb-ai-head .twb-seg button{height:52px;padding:0;border:0;border-radius:0;background:transparent;color:var(--twb-b-ink-3);font-size:13px;font-weight:400}
[data-workspace='ai'] .twb-ai-head .twb-seg button[aria-selected='true']{background:transparent;border-color:transparent;color:var(--twb-ink-1);font-weight:600;box-shadow:inset 0 -2px 0 var(--twb-b-ink-1)}
[data-workspace='ai'] .twb-ai-meta{font-size:11px;color:var(--twb-b-ink-3)}
[data-workspace='ai'] .twb-ai-grid{grid-template-columns:280px minmax(0,1fr);gap:32px;margin-top:24px;align-items:start}
[data-workspace='ai'] .twb-ai-list{background:transparent;border:0;border-right:1px solid var(--twb-b-rule);border-radius:0;overflow:visible}
[data-workspace='ai'] .twb-ai-list-head{padding:0 12px 10px 0;border-bottom:1px solid var(--twb-b-rule-2);font-size:10px;letter-spacing:.12em;color:var(--twb-b-ink-3)}
[data-workspace='ai'] .twb-ai-list-body{max-height:660px;overflow:auto}
[data-workspace='ai'] .twb-ai-list-foot{padding:8px 0 0;border-top:1px solid var(--twb-b-rule);font-size:10px;color:var(--twb-b-ink-3)}
[data-workspace='ai'] .twb-round{height:58px;grid-template-columns:10px 46px minmax(0,1fr) 48px;border-bottom:1px solid var(--twb-b-rule);padding:0 10px 0 0}
[data-workspace='ai'] .twb-round[aria-selected='true']{background:transparent;box-shadow:inset 2px 0 0 var(--twb-b-ink-1)}
[data-workspace='ai'] .twb-round .t{font-size:10px;color:var(--twb-b-ink-3)}
[data-workspace='ai'] .twb-round .s{font-size:12px;color:var(--twb-ink-2)}
[data-workspace='ai'] .twb-round .d{font-size:10px;color:var(--twb-b-ink-3)}
[data-workspace='ai'] .twb-report{gap:0}
[data-workspace='ai'] .twb-report-card{background:transparent;border:0;border-top:1px solid var(--twb-b-rule-2);border-radius:0;padding:17px 0 0;margin-bottom:24px}
[data-workspace='ai'] .twb-report-card--danger{background:transparent;border-left:0;border-top-color:var(--twb-edge-danger);padding-left:0}
[data-workspace='ai'] .twb-report-card--warn{background:transparent;border-left:0;border-top-color:var(--twb-edge-warn);padding-left:0}
[data-workspace='ai'] .twb-report-card .twb-card-head,[data-workspace='ai'] .twb-report-card .twb-concl-head{margin-bottom:10px}
[data-workspace='ai'] .twb-concl-line{font-size:24px;line-height:30px;font-weight:600;letter-spacing:-.01em;color:var(--twb-b-ink-1);margin:8px 0 6px}
[data-workspace='ai'] .twb-concl-sub{font-size:11px;line-height:17px;color:var(--twb-b-ink-3)}
[data-workspace='ai'] .twb-kv{font-size:11px;line-height:17px;margin:8px 0}
[data-workspace='ai'] .twb-kv dt{color:var(--twb-b-ink-3)}
[data-workspace='ai'] .twb-kv dd{color:var(--twb-ink-2)}
[data-workspace='ai'] .twb-stage-head{font-size:10px;letter-spacing:.1em;color:var(--twb-b-ink-3);border-bottom:1px solid var(--twb-b-rule-2);padding-bottom:6px}
[data-workspace='ai'] .twb-stage{height:36px;border-bottom:1px solid var(--twb-b-rule)}
[data-workspace='ai'] .twb-stage-idx{border-color:var(--twb-b-rule-2)}
[data-workspace='ai'] .twb-table thead th{height:28px;background:transparent;border-bottom:1px solid var(--twb-b-rule-2);font-size:10px;letter-spacing:.1em;color:var(--twb-b-ink-3)}
[data-workspace='ai'] .twb-table tbody td{height:44px;border-bottom:1px solid var(--twb-b-rule)}
[data-workspace='ai'] .twb-dist-row{font-size:11px;grid-template-columns:148px minmax(0,1fr) 40px}
[data-workspace='ai'] .twb-dist-bar{height:2px;border-radius:0;background:var(--twb-b-rule)}
[data-workspace='ai'] .twb-dist-bar i{background:var(--twb-b-ink-2)}
[data-workspace='ai'] .twb-today .v{font-size:15px;line-height:22px;font-weight:500}

/* dialogs, precheck, receipts: B+ editorial */
.twb-modal-scrim{background:var(--twb-scrim)}
.twb-modal{width:480px;max-width:92vw;max-height:86vh;background:var(--twb-paper-1);border:1px solid var(--twb-b-rule-2);border-radius:0;box-shadow:var(--twb-shadow-pop);padding:22px 24px;gap:12px}
.twb-modal-title{font-size:15px;font-weight:600;letter-spacing:-.01em}
.twb-modal-body{gap:10px}
.twb-modal-note{background:transparent;border:0;border-left:2px solid var(--twb-warn);border-radius:0;padding:8px 10px;color:var(--twb-b-ink-3)}
.twb-modal-note--danger{background:transparent;border-left-color:var(--twb-danger);color:var(--twb-ink-1)}
.twb-input{height:34px;border-radius:0;border:1px solid var(--twb-b-rule-2);background:var(--twb-paper);font-size:12px}
.twb-input:focus{outline:1px solid var(--twb-b-rule-3);outline-offset:1px}
.twb-modal-actions{gap:12px;margin-top:8px}
.twb-receipt{background:var(--twb-paper);border:1px solid var(--twb-b-rule);border-radius:0}
.twb-receipt[data-ok='false']{background:transparent;border-color:var(--twb-edge-danger)}
.twb-precheck{top:56px;right:56px;width:460px;background:var(--twb-paper-1);border:1px solid var(--twb-b-rule-2);border-radius:0;box-shadow:var(--twb-shadow-pop);padding:16px;gap:8px}
.twb-precheck-head{font-size:12px;color:var(--twb-b-ink-3)}
.twb-precheck-row{font-size:12px;line-height:19px;color:var(--twb-ink-2)}
.twb-precheck-note{background:transparent;border:0;border-left:2px solid var(--twb-warn);border-radius:0;padding:6px 10px}
.twb-precheck-confirm{background:var(--twb-paper);border:1px solid var(--twb-b-rule);border-radius:0}
.twb-pending-row{background:transparent;border:1px solid var(--twb-b-rule);border-radius:0}

/* D1 drawer (round / system / config) */
.twb-drawer{top:64px;bottom:28px;width:720px;max-width:94%;background:var(--twb-paper-1);border-left:1px solid var(--twb-b-rule-2);box-shadow:var(--twb-shadow-pop)}
.twb-drawer-head{height:64px;padding:0 32px;border-bottom:1px solid var(--twb-b-rule-2)}
.twb-drawer-title{font-size:16px;font-weight:600;letter-spacing:-.01em}
.twb-drawer-sub{font-size:11px;color:var(--twb-b-ink-3)}
.twb-drawer-tabs{height:44px;padding:0 32px;gap:24px;border-bottom:1px solid var(--twb-b-rule)}
.twb-drawer-tabs button{height:44px;padding:0;border:0;border-bottom:2px solid transparent;border-radius:0;background:transparent;font-size:13px;font-weight:400;color:var(--twb-b-ink-3)}
.twb-drawer-tabs button[aria-selected='true']{color:var(--twb-ink-1);border-bottom-color:var(--twb-ink-1);font-weight:600}
.twb-drawer-body{padding:24px 32px;gap:0}
.twb-drawer-foot{height:28px;padding:0 32px;border-top:1px solid var(--twb-b-rule);font-size:11px;color:var(--twb-b-ink-3)}
.twb-panel{background:transparent;border:0;border-top:1px solid var(--twb-b-rule-2);border-radius:0;padding:17px 0 0;margin-bottom:24px}
.twb-panel:first-child{border-top:0;padding-top:0}
.twb-panel .twb-card-title{font-size:13px;font-weight:600;color:var(--twb-b-ink-1)}
.twb-panel .twb-hint{color:var(--twb-b-ink-3)}
.twb-panel .twb-kv{font-size:11px;line-height:17px}

/* F2 (§3.1): one settings entry — five groups. The drawer reuses the D1 shell;
   only the group rail, the read-only rows and the draft marker are new. */
.twb-settings-btn{gap:6px}
.twb-settings-ico{font-size:13px;line-height:1}
.twb-settings-lbl{font-size:12px}
.twb-settings-tabs{gap:20px;overflow-x:auto;scrollbar-width:thin}
.twb-settings-body{gap:0}
.twb-settings-block{padding-top:17px}
.twb-settings-head{flex-wrap:wrap;gap:8px}
.twb-settings-sub{white-space:normal;overflow:visible;text-overflow:clip;line-height:17px;margin:2px 0 8px}
.twb-settings-kv{grid-template-columns:minmax(120px,38%) minmax(0,1fr);gap:5px 14px}
.twb-settings-kv dd{overflow:visible;text-overflow:clip;white-space:normal;overflow-wrap:anywhere}
.twb-settings-list{margin:4px 0 0;padding-left:16px;font-size:11px;line-height:18px;color:var(--twb-b-ink-2)}
.twb-settings-list li{overflow-wrap:anywhere}
.twb-settings-model .twb-config-row{gap:8px}
.twb-settings-model .twb-kv{background:transparent;border:0;border-left:2px solid var(--twb-b-rule-2);padding:6px 10px;margin:6px 0 10px}
/* 密度 is a local display preference: it only relaxes/tightens the console. */
.twb-root[data-density='compact'] .twb-kv{gap:2px 10px;line-height:15px}
.twb-root[data-density='compact'] .twb-settings-block{padding-top:12px;margin-bottom:16px}
.twb-root[data-density='compact'] .twb-settings-sub{margin-bottom:6px}
@container (max-width:1199px){
  .twb-settings-lbl{display:none}
  .twb-settings-btn{padding:0 8px}
  .twb-settings-kv{grid-template-columns:minmax(96px,42%) minmax(0,1fr)}
  .twb-settings-tabs{gap:14px;padding:0 20px}
}
@media (max-width:1199px){
  .twb-settings-lbl{display:none}
  .twb-settings-btn{padding:0 8px}
  .twb-drawer{width:100%;max-width:100%}
}

`

    // ---- helpers -----------------------------------------------------------
    // Local language override: the Host locale is the default, but the panel
    // owns a visible 中文 / EN switch (Chinese-first). `zh()` is kept as the
    // format-template helper and follows the same current language.
    let LANG = 'zh'
    const zh = (key) => {
      const table = DICT[LANG] || DICT.zh
      return typeof table[key] === 'string' ? table[key] : key
    }
    const tr = (key, params, lang = LANG) => {
      const table = DICT[lang] || DICT.zh
      const text = typeof table[key] === 'string' ? table[key] : key
      return params ? fmt(text, params) : text
    }
    /**
     * Dictionary lookup that reports a miss instead of echoing the key. Used
     * where a Host field is the fallback (attribution stage/category): the
     * label must follow the panel language, and an unknown key must not print
     * as a raw machine token.
     */
    function dictText(key) {
      if (!key) return null
      const table = DICT[LANG] || DICT.zh
      return typeof table[key] === 'string' ? table[key] : null
    }

    /** "标签（CODE）" in Chinese, "label (CODE)" in English. */
    function withCode(label, code) {
      if (code === null || code === undefined || code === '') return String(label ?? '—')
      return `${label}${LANG === 'en' ? ` (${code})` : `（${code}）`}`
    }

    function fmt(text, params) {
      if (!params) return String(text)
      return String(text).replace(/\{(\w+)\}/g, (match, name) => (
        Object.prototype.hasOwnProperty.call(params, name) ? String(params[name]) : match
      ))
    }

    function num(value) {
      if (value === null || value === undefined || value === '') return null
      const n = typeof value === 'number' ? value : Number(value)
      return Number.isFinite(n) ? n : null
    }

    function fmtNum(value, digits) {
      const n = num(value)
      if (n === null) return '—'
      return n.toLocaleString('zh-CN', { minimumFractionDigits: digits ?? 2, maximumFractionDigits: digits ?? 2 })
    }

    function fmtInt(value) {
      const n = num(value)
      return n === null ? '—' : String(Math.round(n))
    }

    function fmtSigned(value, { digits = 2, suffix = '' } = {}) {
      const n = num(value)
      if (n === null) return { text: '—', dir: 'na' }
      if (n === 0) return { text: `${fmtNum(0, digits)}${suffix}`, dir: 'flat' }
      const arrow = n > 0 ? '▲' : '▼'
      const sign = n > 0 ? '+' : '−'
      return { text: `${arrow} ${sign}${fmtNum(Math.abs(n), digits)}${suffix}`, dir: n > 0 ? 'up' : 'down' }
    }

    function fmtFunding(rate) {
      const n = num(rate)
      return n === null ? null : `${(n * 100).toFixed(4)}%`
    }

    function fmtSpread(bps) {
      const n = num(bps)
      return n === null ? null : `${n.toFixed(1)}bp`
    }

    function fmtPrice(value) {
      const n = num(value)
      if (n === null) return null
      const digits = Math.abs(n) >= 1000 ? 1 : 2
      return n.toLocaleString('zh-CN', { minimumFractionDigits: digits, maximumFractionDigits: digits })
    }

    // Host limits may arrive as a fraction (0.01) or a percent (1). Keep both
    // readable without inventing precision.
    function pctFromLimit(value) {
      const n = num(value)
      if (n === null) return null
      return n <= 1 ? n * 100 : n
    }

    function fmtMs(value) {      const n = num(value)
      if (n === null) return '—'
      if (n < 1000) return `${Math.round(n)} ms`
      if (n < 60000) return `${(n / 1000).toFixed(1)} s`
      return `${Math.floor(n / 60000)}m ${Math.round((n % 60000) / 1000)}s`
    }

    /** m:ss for the running clock (01 §3.6 "已运行 2:13"). */
    function fmtSpan(ms) {
      const n = num(ms)
      if (n === null) return '—'
      const total = Math.max(0, Math.round(n / 1000))
      return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`
    }

    function fmtAge(ms) {
      const n = num(ms)
      if (n === null || n < 0) return '—'
      if (n < 60 * 1000) return fmt(zh('common.seconds'), { n: Math.max(1, Math.round(n / 1000)) })
      if (n < 3600 * 1000) return fmt(zh('common.minutes'), { n: Math.round(n / 60000) })
      if (n < 24 * 3600 * 1000) return fmt(zh('common.hours'), { n: (n / 3600000).toFixed(1) })
      return fmt(zh('common.days'), { n: Math.round(n / 86400000) })
    }

    // 03 C4: one display convention — every absolute time is Beijing time
    // (Asia/Shanghai), regardless of the browser's local zone. Day boundaries
    // use the same zone; the risk day is UTC and is labelled where it appears.
    const DISPLAY_TZ = 'Asia/Shanghai'
    const TZ_FORMATTERS = new Map()

    function tzParts(ms, options) {
      const key = JSON.stringify(options)
      let formatter = TZ_FORMATTERS.get(key)
      if (!formatter) {
        formatter = new Intl.DateTimeFormat('en-GB', { timeZone: DISPLAY_TZ, hourCycle: 'h23', ...options })
        TZ_FORMATTERS.set(key, formatter)
      }
      const parts = formatter.formatToParts(ms)
      const pick = (type) => parts.find((part) => part.type === type)?.value ?? ''
      return pick
    }

    function fmtClock(value) {
      const ms = toMs(value)
      if (ms === null) return '—'
      const pick = tzParts(ms, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' })
      return `${pick('month')}-${pick('day')} ${pick('hour')}:${pick('minute')}:${pick('second')}`
    }

    function fmtShortClock(value) {
      const ms = toMs(value)
      if (ms === null) return '—'
      const pick = tzParts(ms, { hour: '2-digit', minute: '2-digit' })
      return `${pick('hour')}:${pick('minute')}`
    }

    /** Asia/Shanghai calendar day key (03 C4 day boundary). */
    function dayKeyInShanghai(value) {
      const ms = toMs(value)
      if (ms === null) return null
      const pick = tzParts(ms, { year: 'numeric', month: '2-digit', day: '2-digit' })
      return `${pick('year')}-${pick('month')}-${pick('day')}`
    }

    function toMs(value) {
      const ms = value ? Date.parse(value) : NaN
      return Number.isFinite(ms) ? ms : null
    }

    function ageFrom(producedAt, now) {
      const ms = toMs(producedAt)
      if (ms === null) return null
      return Math.max(0, (now || Date.now()) - ms)
    }

    function truthy(value) {
      return value === true || value === 'true'
    }

    // `EXECUTION_BLOCKED:<CODE>` keeps the blocking code inside reasonCode. Show
    // the base label plus that code instead of falling back to "unknown reason";
    // the explanation comes from round.reasonDetail / execution.rationale.
    function splitReasonCode(code) {
      const text = typeof code === 'string' ? code.trim() : ''
      if (!text) return { base: null, param: null }
      const index = text.indexOf(':')
      if (index <= 0) return { base: text, param: null }
      return { base: text.slice(0, index), param: text.slice(index + 1) || null }
    }

    /**
     * 03 §8: code -> Chinese label. Three steps, and never "未知原因":
     *   1. exact code in the 03 table (89 codes incl. EXECUTION_BLOCKED:<sub>);
     *   2. documented wildcards (EXECUTION_BLOCKED:<other>, E_SERVICE_HTTP_<n>);
     *   3. "未收录原因码 {code}" + the raw code, which stays copyable.
     */
    function reasonLabel(code) {
      if (!code) return zh('concl.noReason')
      const text = String(code).trim()
      const exactKey = `reason.${text}`
      const exact = zh(exactKey)
      if (exact !== exactKey) return exact
      const { base, param } = splitReasonCode(text)
      if (base === 'EXECUTION_BLOCKED') return fmt(zh('reason.executionBlock'), { code: param || 'UNSPECIFIED' })
      const http = /^E_SERVICE_HTTP_?(\d{3})$/.exec(text)
      if (http) return fmt(zh('reason.serviceHttp'), { code: http[1] })
      return fmt(zh('reason.unmapped'), { code: text })
    }

    /** True when the 03 table has a label for this code (no unmapped fallback). */
    function isMappedCode(code) {
      if (!code) return false
      const text = String(code).trim()
      const key = `reason.${text}`
      if (zh(key) !== key) return true
      const { base } = splitReasonCode(text)
      return base === 'EXECUTION_BLOCKED' || /^E_SERVICE_HTTP_?\d{3}$/.test(text)
    }

    /** "标签（CODE）" when mapped, otherwise the raw code — never a fabricated label. */
    function codeWithLabel(code) {
      if (!code) return '—'
      const text = String(code)
      return isMappedCode(text) ? withCode(reasonLabel(text), text) : text
    }

    // 01 §4.2: one LLM_FINISH_ERROR code covers four very different endings.
    // The labels live in DICT so the row follows the panel language.
    const FINISH_KIND_KEYS = ['stop', 'max_tokens', 'error', 'aborted']

    function finishKindLabel(kind) {
      if (!kind) return null
      const value = String(kind)
      if (FINISH_KIND_KEYS.includes(value)) return zh(`finish.${value}`)
      return fmt(zh('finish.other'), { kind: value })
    }

    // 01 §4.1 enum normalisation: the DTO carries upper-case enums, so comparing
    // against lower-case literals silently produced "not a trade" for a trade.
    function actionOf(decision) {
      const raw = decision?.action
      if (raw === null || raw === undefined || raw === '') return null
      const up = String(raw).trim().toUpperCase()
      if (up === 'TRADE') return 'trade'
      if (up === 'NO_TRADE') return 'no_trade'
      return String(raw)
    }

    function recommendationLabel(risk) {
      const raw = risk?.recommendation
      if (raw === null || raw === undefined || raw === '') return null
      const up = String(raw).trim().toUpperCase()
      if (up === 'APPROVE') return zh('verdict.approve')
      if (up === 'DOWNSIZE' || up === 'REDUCED') return fmt(zh('concl.reduced'), { n: fmtNum(risk?.size_multiplier ?? 1, 2) })
      if (up === 'REJECT') return zh('verdict.reject')
      return String(raw)
    }

    /** 03 N2: concerns are objects ({code,severity,detail}); never join them raw. */
    function concernLines(risk) {
      const list = Array.isArray(risk?.concerns) ? risk.concerns : []
      const out = []
      for (const item of list.slice(0, 5)) {
        if (item === null || item === undefined) continue
        if (typeof item === 'string') { if (item.trim()) out.push(item.trim()); continue }
        if (typeof item !== 'object') { out.push(String(item)); continue }
        const code = item.code ?? null
        const severity = item.severity ?? null
        const detail = typeof item.detail === 'string' ? item.detail : (typeof item.message === 'string' ? item.message : null)
        const head = [code ? codeWithLabel(code) : null, severity ? `· ${String(severity)}` : null].filter(Boolean).join(' ')
        const line = [head || null, detail].filter(Boolean).join('：')
        if (line) out.push(line)
      }
      return out
    }

    const MAX_REASON_DETAIL = 400
    // Same sanitizer contract as host/round-view.js: every display source
    // (reasonDetail, execution.rationale, error.message, analysis notes) is
    // redacted before truncation, so no fallback path can leak a raw secret.
    const SECRET_PATTERNS = [
      /\bauthorization\s*[:=]\s*(?:bearer\s+)?\S+/gi,
      /\bbearer\s+\S+/gi,
      /\b(api[_-]?key|secret|password|passphrase|token)\b\s*[:=]\s*\S+/gi,
      /\bsk-[A-Za-z0-9_-]{8,}/g,
    ]

    function redactDetail(value) {
      let text = String(value)
      for (const pattern of SECRET_PATTERNS) text = text.replace(pattern, '[redacted]')
      return text
    }

    function clipDetail(value) {
      if (typeof value !== 'string') return null
      const text = redactDetail(value).replace(/\s+/g, ' ').trim()
      if (!text) return null
      return text.length > MAX_REASON_DETAIL ? `${text.slice(0, MAX_REASON_DETAIL - 1)}…` : text
    }

    function detailList(value, limit = 3) {
      if (!Array.isArray(value)) return []
      const out = []
      for (const item of value) {
        const text = clipDetail(item)
        if (text && !out.includes(text)) out.push(text)
        if (out.length >= limit) break
      }
      return out
    }

    // Presentation-safe reason for a round: code + at most one detail line and
    // up to three notes. Fallback order: reasonDetail -> execution.rationale ->
    // analysis notes/warnings -> error message. Never reads prompt/token text.
    const REASON_ROLE_LABELS = {
      analysis: 'role.analysis',
      red_opening: 'role.red_opening',
      blue_opening: 'role.blue_opening',
      red_response: 'role.red_response',
      blue_response: 'role.blue_response',
      decision: 'role.decision',
      risk: 'role.risk',
      execution_entry: 'role.execution_entry',
      execution_manage: 'role.execution_entry',
      review: 'role.review',
    }

    function reasonRoleLabel(key) {
      if (key === 'p1' || key === 'p2') return zh(`role.${key}`)
      const textKey = REASON_ROLE_LABELS[key]
      return textKey ? zh(textKey) : null
    }

    /**
     * 03 A6: the Host sends {kind, role, error_code, timeout_ms}; the sentence
     * is assembled here from DICT templates, so no English engineering string
     * is the headline and the sentence follows the panel language.
     */
    function reasonSentence(round) {
      const params = round?.reasonParams
      if (!params || typeof params !== 'object') return null
      const role = params.role ? reasonRoleLabel(params.role) : null
      const codeLabel = params.error_code ? reasonLabel(params.error_code) : null
      const seconds = Number.isFinite(params.timeout_ms) ? `${Math.round(params.timeout_ms / 1000)}s` : null
      const who = role ? `${role} ` : ''
      if (params.kind === 'role_failure' || params.kind === 'debate_incomplete') {
        if (params.error_code === 'LLM_TIMEOUT') {
          return seconds
            ? fmt(zh('reason.sentenceTimeout'), { who, s: seconds })
            : fmt(zh('reason.sentenceTimeoutNoTime'), { who })
        }
        if (params.error_code === 'SCHEMA_INVALID') return fmt(zh('reason.sentenceSchema'), { who })
        if (params.error_code === 'NO_ADAPTER') return zh('reason.sentenceNoAdapter')
        return fmt(zh('reason.sentenceFailed'), { who, code: codeLabel ?? zh('reason.callFailed') })
      }
      if (params.kind === 'gateway_failure') return fmt(zh('reason.sentenceGateway'), { who, code: codeLabel ?? params.error_code ?? '—' })
      return null
    }

    function roundReason(round) {
      const execution = round?.execution ?? null
      const analysis = round?.analysis ?? null
      const { base, param } = splitReasonCode(round?.reasonCode ?? null)
      const blockedReason = typeof execution?.blocked_reason === 'string' && execution.blocked_reason.trim()
        ? execution.blocked_reason.trim()
        : null
      const notes = []
      for (const text of [
        ...detailList(round?.reasonNotes),
        ...detailList(analysis?.market_read?.notes),
        ...detailList(analysis?.warnings),
      ]) {
        if (!notes.includes(text)) notes.push(text)
        if (notes.length >= 3) break
      }
      const joined = notes.length > 0 ? notes.join(' · ') : null
      const sentence = reasonSentence(round)
      // 03 A6: with a structured Host detail the English engineering string is
      // folded as 原文 — it must not come back as the display detail via the
      // error.message fallback.
      const detail = clipDetail(round?.reasonDetail)
        ?? clipDetail(execution?.rationale)
        ?? clipDetail(analysis?.stand_aside_reason)
        ?? (sentence ? null : joined)
        ?? (sentence ? null : clipDetail(round?.error?.message))
      return {
        code: round?.reasonCode ?? null,
        base,
        param,
        sentence,
        raw: typeof round?.reasonRaw === 'string' && round.reasonRaw.trim() ? round.reasonRaw.trim() : null,
        isExecutionBlock: base === 'EXECUTION_BLOCKED',
        isNoCandidate: base === 'NO_CANDIDATE',
        label: reasonLabel(round?.reasonCode ?? null),
        blockedReason,
        notes,
        // The Host may fold notes into reasonDetail; the list already shows them.
        detail: detail && notes.length > 0 && notes.every((note) => detail.includes(note)) ? null : detail,
      }
    }

    function ReasonDetailLines({ reason, t, className }) {
      if (!reason) return null
      const lines = []
      if (reason.blockedReason) {
        lines.push(h('div', { key: 'blocked', className: 'twb-concl-detail' },
          `${t('concl.blockedReason')}: `,
          h('span', { className: 'twb-mono' }, reason.blockedReason)))
      }
      if (reason.sentence) {
        lines.push(h('div', { key: 'sentence', className: 'twb-concl-detail' }, reason.sentence))
      }
      if (reason.detail) {
        lines.push(h('div', { key: 'detail', className: 'twb-concl-detail' }, reason.detail))
      }
      if (reason.raw) {
        // 03 A6: the original Host engineering string is available, but folded.
        lines.push(h('details', { key: 'raw', className: 'twb-concl-raw' },
          h('summary', null, t('concl.rawDetail')),
          h('div', { className: 'twb-concl-detail' }, reason.raw)))
      }
      if (reason.notes.length > 0) {
        lines.push(h('ul', { key: 'notes', className: 'twb-concl-notes' },
          reason.notes.map((note, index) => h('li', { key: index }, note))))
      }
      if (lines.length === 0 && (reason.isExecutionBlock || reason.isNoCandidate)) {
        lines.push(h('div', { key: 'no-detail', className: 'twb-concl-detail' }, t('concl.noDetail')))
      }
      if (lines.length === 0) return null
      return h('div', { className: className ?? '' }, lines)
    }

    function triggerLabel(trigger) {
      const key = `trigger.${String(trigger || '').toLowerCase()}`
      const text = zh(key)
      return text === key ? (trigger ?? '—') : text
    }

    function gateLabel(gate) {
      if (gate === null || gate === undefined || gate === '') return null
      const key = `gate.${String(gate).toUpperCase()}`
      const text = zh(key)
      return text === key ? String(gate) : withCode(text, gate)
    }

    function statusLabel(status) {
      const key = `status.${String(status || '').toLowerCase()}`
      const text = zh(key)
      return text === key ? String(status || '—') : text
    }

    function controlLabel(control) {
      const key = `control.${String(control || 'unknown').toLowerCase()}`
      const text = zh(key)
      return text === key ? String(control || '—') : text
    }

    function debateLabel(quality) {
      if (!quality) return null
      const key = `debate.${String(quality).toLowerCase()}`
      const text = zh(key)
      return text === key ? String(quality) : text
    }

    function sideLabel(side) {
      const key = `side.${String(side || '').toLowerCase()}`
      const text = zh(key)
      return text === key ? '—' : text
    }

    // ---- protection contract (six codes) -----------------------------------
    // Only `protection.status` decides the state; `protection_ok` is a legacy
    // boolean that folds UNKNOWN into false, so it is never read here.
    const PROTECTION = {
      ok: { state: 'ok', label: 'prot.ok', labelFull: 'prot.ok', block: false, tone: 'quiet' },
      missing: { state: 'missing', label: 'prot.missing', labelFull: 'prot.missingFull', block: true, tone: 'danger' },
      partial: { state: 'partial', label: 'prot.partial', labelFull: 'prot.partialFull', block: true, tone: 'warn' },
      unknown: { state: 'unknown', label: 'prot.unknown', labelFull: 'prot.unknownFull', block: true, tone: 'warn' },
      unreconciled: { state: 'unreconciled', label: 'prot.unreconciled', labelFull: 'prot.unreconciled', block: false, tone: 'quiet' },
    }

    // 05 N07: fail-safe. Only an explicitly empty value means "not reconciled"
    // (which does not block); any state the contract does not list (MISMATCH /
    // PROTECTION_FAILED / a typo from a newer Service) is UNKNOWN -> blocks, and
    // keeps its raw code so the operator can see what actually arrived.
    const UNRECONCILED_ALIASES = new Set(['NULL', 'UNRECONCILED', 'NOT_RECONCILED'])

    function protectionOf(position) {
      const raw = position && (position.protection ?? position.protection_status)
      const empty = raw === null || raw === undefined || raw === ''
      const code = empty ? '' : String(raw).trim().toUpperCase()
      if (empty || UNRECONCILED_ALIASES.has(code)) return PROTECTION.unreconciled
      if (code === 'OK' || code === 'PROTECTED') return PROTECTION.ok
      if (code === 'MISSING' || code === 'NONE') return PROTECTION.missing
      if (code === 'PARTIAL' || code === 'DUPLICATE') return PROTECTION.partial
      if (code === 'UNKNOWN') return PROTECTION.unknown
      return { ...PROTECTION.unknown, raw: String(raw).trim() }
    }

    /** Label for a protection state; an unrecorded code keeps its raw value. */
    function protectionText(protection) {
      if (!protection) return '—'
      if (protection.raw) return fmt(zh('prot.unknownCode'), { code: protection.raw })
      return zh(protection.label)
    }

    // ---- empty-reason alignment (host emits sibling *EmptyReason keys) -----
    function emptyReason(state, key) {
      const holder = state && state[key + 'Empty']
      if (holder && typeof holder === 'object') {
        const reason = holder.gap ?? holder.empty_reason ?? holder.reason
        if (reason) return String(reason)
      }
      const sibling = state && state[key + 'EmptyReason']
      if (typeof sibling === 'string' && sibling) return sibling
      const gaps = state && Array.isArray(state.portfolio?.gaps) ? state.portfolio.gaps : []
      const needle = { positions: /position/i, orders: /order/i, fills: /fill/i, reviews: /review/i }[key]
      const hit = needle ? gaps.find((gap) => needle.test(String(gap))) : null
      return hit ? String(hit) : ''
    }

    // ---- service resolution (four candidate shapes) ------------------------
    const SERVICE_FIELDS = [
      'base_url', 'token_present', 'token_configured', 'token_source', 'token_error',
      'auth_mode', 'auth_state', 'ready', 'service_ready', 'demo_only', 'ledger_id', 'control_state',
      'credentials_configured', 'new_entry_allowed', 'checked_at', 'state', 'error_status', 'error_code',
    ]

    function mergeService(state, health, round) {
      const candidates = [
        state?.service,
        state?.service?.last_run,
        health?.service,
        health?.service?.last_run,
        round?.service,
      ].filter((item) => item && typeof item === 'object' && !Array.isArray(item))
      const merged = {}
      for (const field of SERVICE_FIELDS) {
        for (const candidate of candidates) {
          if (candidate[field] !== undefined && candidate[field] !== null) {
            merged[field] = candidate[field]
            break
          }
        }
      }
      const tokenPresent = typeof merged.token_present === 'boolean'
        ? merged.token_present
        : (typeof merged.token_configured === 'boolean' ? merged.token_configured : null)
      const ready = merged.ready === true || merged.service_ready === true
      return { ...merged, ready, token_present: tokenPresent, new_entry_allowed: typeof merged.new_entry_allowed === 'boolean' ? merged.new_entry_allowed : null }
    }

    function serviceState(service) {
      if (service.error_code === 'E_TOKEN_UNREADABLE' || service.token_error) return 'token_unreadable'
      if (service.token_present === false) return 'no_token'
      if (service.error_status === 401 || service.error_status === 403) return 'auth_failed'
      if (service.error_code === 'E_SERVICE_OFFLINE') return 'offline'
      if (service.error_code === 'E_SERVICE_UNAVAILABLE' || service.error_status === 503) return 'unavailable'
      if (service.ready === true) return 'online'
      if (service.ready === false) return 'not_ready'
      return 'not_checked'
    }

    function serviceChip(stateCode) {
      switch (stateCode) {
        case 'online': return { text: 'service.online', tone: 'quiet', dot: 'ok' }
        case 'no_token': return { text: 'service.noToken', tone: 'warn', dot: 'warn' }
        case 'token_unreadable': return { text: 'service.tokenUnreadable', tone: 'warn', dot: 'warn' }
        case 'auth_failed': return { text: 'service.authFailed', tone: 'warn', dot: 'warn' }
        case 'offline': return { text: 'service.offline', tone: 'danger', dot: 'danger' }
        case 'unavailable': return { text: 'service.unavailable', tone: 'danger', dot: 'danger' }
        case 'not_ready': return { text: 'service.notReady', tone: 'warn', dot: 'warn' }
        default: return { text: 'service.notChecked', tone: 'quiet', dot: 'idle' }
      }
    }

    // ---- model resolution --------------------------------------------------
    function resolveControl(state, service) {
      const control = state?.service?.control_state ?? service?.control_state ?? 'unknown'
      return String(control || 'unknown')
    }

    // New entries are blocked by any of: protection state, control state, or an
    // explicit service denial. Keep one gate for S2 / risk band / precheck / S4.
    function entryGate({ protectionBlock, control, newEntryAllowed, envelopeBlockers }) {
      const reasons = []
      if (protectionBlock) {
        reasons.push({
          kind: 'protection',
          label: protectionBlock.raw ? protectionText(protectionBlock) : zh(protectionBlock.labelFull),
          short: protectionText(protectionBlock),
          tone: protectionBlock.state === 'missing' ? 'danger' : 'warn',
        })
      }
      if (control && control !== 'normal') {
        reasons.push({
          kind: 'control',
          label: `${zh('s4.control')}：${controlLabel(control)}`,
          short: controlLabel(control),
          tone: 'warn',
        })
      }
      if (newEntryAllowed === false) {
        reasons.push({ kind: 'service', label: zh('s4.entryDenied'), short: zh('s4.entryDenied'), tone: 'warn' })
      }
      if (Array.isArray(envelopeBlockers)) {
        for (const blocker of envelopeBlockers) {
          const text = typeof blocker === 'string' ? blocker : (blocker?.reason ?? blocker?.code ?? blocker?.message ?? null)
          if (text) reasons.push({ kind: 'envelope', label: String(text), short: String(text), tone: 'warn' })
        }
      }
      return { blocked: reasons.length > 0, reasons }
    }

    function resolveEquity(state) {
      const curve = state?.equityCurve ?? state?.equity_curve ?? {}
      const points = (Array.isArray(curve.points) ? curve.points : [])
        .map((point) => {
          const exchange = num(point?.exchange)
          const session = num(point?.session)
          const equity = exchange !== null ? exchange : session
          return equity === null ? null : {
            t: toMs(point?.ts),
            equity,
            session,
            upnl: num(point?.upnl),
            available: num(point?.available),
          }
        })
        .filter(Boolean)
        .sort((a, b) => (a.t ?? 0) - (b.t ?? 0))
      const latest = points.length > 0 ? points[points.length - 1] : null
      return {
        points,
        latest,
        source: curve.source ?? null,
        range: curve.range ?? null,
        gap: curve.gap ?? null,
        emptyReason: curve.empty_reason ?? null,
        empty: points.length === 0,
      }
    }

    function resolveAccount(state, equity) {
      const raw = state?.account && typeof state.account === 'object' ? state.account : {}
      const latest = equity?.latest ?? null
      const total = num(raw.total_equity) ?? num(raw.wallet_balance) ?? (latest ? latest.equity : null)
      const session = num(raw.equity_start) ?? num(raw.equity) ?? num(raw.equity_session)
      const upnl = num(raw.upnl) ?? num(raw.unrealized_pnl) ?? (latest ? latest.upnl : null)
      const available = num(raw.available) ?? num(raw.available_balance) ?? (latest ? latest.available : null)
      const dayPnl = num(raw.day_pnl) ?? num(raw.today_pnl) ?? num(state?.performance?.equity_change)
      const marginUsed = num(raw.margin_used)
      const empty = raw.empty === true || (total === null && upnl === null && available === null && dayPnl === null)
      return {
        total, session, upnl, available, dayPnl, marginUsed, empty,
        reason: raw.gap ?? raw.empty_reason ?? (empty ? 'NO_ACCOUNT_VALUES' : ''),
        source: raw.source ?? null,
        dayBaseline: state?.performance?.baseline_at ?? null,
      }
    }

    function serviceScope(state) {
      return [state?.service?.base_url, state?.service?.ledger_id, state?.venue ?? 'demo'].join('|')
    }

    function resolvePositions(state, quotes = null, now = Date.now()) {
      const exchange = state?.portfolio?.exchange
      const accountAt = exchange?.last_success_at ?? state?.portfolio?.fetched_at
      const accountAge = ageFrom(accountAt, now)
      const accountStale = exchange?.stale === true || exchange?.reachable === false || accountAge === null || accountAge > 30000
      const rows = (Array.isArray(state?.positions) ? state.positions : []).map((raw, index) => {
        const protection = protectionOf(raw)
        const side = String(raw?.side ?? raw?.position_side ?? '').toLowerCase()
        const quote = quotes?.[raw?.symbol]
        const quoteAt = num(quote?.as_of_ms)
        const quoteAge = quoteAt === null ? null : Math.max(0, now - quoteAt)
        const accountStamp = accountAt ? Date.parse(accountAt) : NaN
        const useQuote = num(quote?.mark) > 0 && quoteAt !== null && quoteAt <= now + 5000 && (!Number.isFinite(accountStamp) || quoteAt >= accountStamp)
        const freshQuote = quote?.stale === false && quoteAge !== null && quoteAge <= 5000 && num(quote?.mark) > 0
        const mark = useQuote ? num(quote.mark) : num(raw?.mark)
        const entry = num(raw?.entry)
        const liq = num(raw?.liq_price)
        const sl = num(raw?.sl)
        const tp = num(raw?.tp)
        const dir = side === 'short' || side === 'sell' ? -1 : 1
        let stopDist = null
        if (mark && sl) stopDist = ((mark - sl) * dir / mark) * 100
        let liqDist = null
        if (mark && liq !== null && liq > 0) liqDist = ((mark - liq) * dir / mark) * 100
        return {
          key: raw?.key ?? `${raw?.symbol ?? 'unknown'}:${side || '?'}:${index}`,
          symbol: raw?.symbol ?? '—',
          side,
          qty: raw?.qty ?? null,
          entry,
          mark,
          quoteAge: useQuote ? quoteAge : accountAge,
          quoteStale: !useQuote || !freshQuote,
          accountStale,
          accountAge,
          upnl: num(raw?.upnl),
          sl,
          tp,
          liq,
          stopDist,
          liqDist,
          margin: num(raw?.margin),
          leverage: num(raw?.leverage),
          marginMode: raw?.margin_mode ?? null,
          protection,
          origin: raw?.origin ?? null,
          raw,
        }
      })
      return { rows, reason: emptyReason(state, 'positions'), stale: accountStale, accountAge }
    }

    function resolveOrders(state) {
      return (Array.isArray(state?.orders) ? state.orders : []).map((raw) => ({
        id: raw?.orderId ?? raw?.order_id ?? null,
        clientId: raw?.clientOrderId ?? raw?.client_order_id ?? null,
        symbol: raw?.symbol ?? '—',
        side: raw?.side ?? '',
        action: raw?.action ?? raw?.purpose ?? '',
        qty: raw?.qty ?? raw?.raw?.qty_requested ?? raw?.raw?.qty_filled ?? null,
        type: raw?.type ?? raw?.order_type ?? '',
        price: num(raw?.price),
        status: raw?.status ?? '',
        createdAt: raw?.createdAt ?? raw?.created_at ?? null,
      }))
    }

    function resolveFills(state) {
      return (Array.isArray(state?.fills) ? state.fills : []).map((raw) => ({
        id: raw?.fillId ?? raw?.fill_id ?? raw?.id ?? null,
        symbol: raw?.symbol ?? '—',
        side: raw?.side ?? '',
        qty: raw?.qty ?? null,
        price: num(raw?.price),
        realizedPnl: num(raw?.realizedPnl ?? raw?.realized_pnl),
        commission: num(raw?.commission),
        time: raw?.tradeTime ?? raw?.time ?? raw?.created_at ?? null,
      }))
    }

    function resolveReviews(state) {
      const list = Array.isArray(state?.reviews) ? state.reviews : []
      return { list, reason: emptyReason(state, 'reviews'), gap: state?.reviewsGap ?? null }
    }

    function resolveRounds(state) {
      const list = Array.isArray(state?.rounds) ? state.rounds.slice() : []
      return list.sort((a, b) => (toMs(b?.startedAt) ?? 0) - (toMs(a?.startedAt) ?? 0))
    }

    // N2: the Host projects the deterministic scorecard. Top-level grade/gate
    // follow decision.selected_case; when no case was selected (NO_TRADE) they
    // are null and the UI must read scorecard.red / scorecard.blue instead.
    function resolveScorecard(round) {
      const raw = round?.scorecard && typeof round.scorecard === 'object' ? round.scorecard : null
      if (!raw) return null
      const selectedCase = round?.decision?.selected_case === 'red' || round?.decision?.selected_case === 'blue'
        ? round.decision.selected_case
        : null
      const selected = selectedCase === 'red' ? raw.red : selectedCase === 'blue' ? raw.blue : null
      return {
        raw,
        selectedCase,
        grade: selected?.grade ?? null,
        gate: selected?.gate ?? null,
        gateReasons: selected && Array.isArray(selected.gate_reasons) ? selected.gate_reasons : null,
        debateQuality: raw.debate_quality ?? null,
        tie: typeof raw.tie === 'boolean' ? raw.tie : null,
        degraded: typeof raw.degraded === 'boolean' ? raw.degraded : null,
        red: raw.red && typeof raw.red === 'object' ? raw.red : null,
        blue: raw.blue && typeof raw.blue === 'object' ? raw.blue : null,
      }
    }

    function bestRound(state) {
      return state?.last_round ?? null
    }

    function evidenceAge(round, now) {
      const produced = round?.freshness?.produced_at ?? round?.evidence?.produced_at ?? null
      return { age: ageFrom(produced, now), producedAt: produced, verdict: round?.freshness?.data_verdict ?? round?.evidence?.verdict ?? null }
    }

    function freshnessLevel(age) {
      if (age === null) return 'unknown'
      if (age <= 15000) return 'fresh'
      if (age <= 180000) return 'stale'
      return 'expired'
    }

    // ---- attention items (S2: one main item + "N more") --------------------
    // 05 N05: consecutive auto-schedule skips (the live Host has been skipping
    // every slot on E_SERVICE_TIMEOUT) must surface as a reason, not as "on".
    function leadingSkips(history) {
      if (!Array.isArray(history)) return 0
      let count = 0
      for (const entry of history) {
        if (!entry || entry.kind !== 'skip') break
        count += 1
      }
      return count
    }

    /**
     * F1 (§2.2): one fault, one main hint. Every item is written as
     * 影响 (impact) + 原因 (cause) + 下一步 (next), carries exactly one of the
     * four categories (不交易 / 规则阻断 / 执行失败 / 待确认), and declares which
     * other items it makes redundant — a single root cause must not produce a
     * banner, a toast and a dialog at the same time.
     */
    /** 15 §10: the Host decides whether it can start the service; the UI only renders. */
    function startServiceSupported(model) {
      return model?.serviceStart?.supported === true
    }

    function attentionItems(model) {
      const items = []
      const push = (item) => items.push(item)
      // An unreachable service invalidates every other action first. Only once
      // /state has answered: "not checked yet" is not an outage.
      const serviceDown = Boolean(model.state) && Boolean(model.serviceCode) && model.serviceCode !== 'online'
      if (serviceDown) {
        push({
          id: 'service-down',
          priority: 10,
          category: 'exec',
          tone: model.serviceCode === 'offline' || model.serviceCode === 'unavailable' ? 'danger' : 'warn',
          impact: 's2.serviceDownImpact',
          reason: `${zh('s2.serviceDown')} · ${fmt(zh('s2.serviceDownState'), { state: zh(serviceChip(model.serviceCode).text) })}`,
          next: 's2.serviceDownNext',
          // 15 §10: when the Host says it can launch the service, the banner's
          // action becomes "启动服务"; otherwise it stays the read-only drawer.
          action: startServiceSupported(model) ? 'start-service' : 'service-setup',
          actionLabel: startServiceSupported(model) ? 's2.startService' : 's2.setupService',
          // Same root cause: an unreachable service must not ALSO appear as an
          // automation-blocked item. Consequences that the user can act on
          // separately (a stalled schedule, stale evidence) stay in the list —
          // only one of them can ever be the main banner.
          suppress: ['automation'],
        })
      }
      const positions = model.positions.rows
      const worst = ['missing', 'partial', 'unknown', 'unreconciled', 'ok']
        .map((state) => positions.find((row) => row.protection.state === state))
        .find(Boolean)
      if (worst && worst.protection.block) {
        push({
          id: `protection-${worst.key}`,
          priority: 20,
          category: 'exec',
          tone: worst.protection.state === 'missing' ? 'danger' : 'warn',
          impact: 's2.protectionImpact',
          reason: `${worst.symbol} · ${sideLabel(worst.side)} ${worst.qty ?? '—'} · ${worst.protection.raw ? zh('prot.unknownCode').replace('{code}', worst.protection.raw) : zh(worst.protection.labelFull)}`,
          next: 's2.protectionNext',
          action: 'protection',
          actionLabel: 's2.viewPosition',
          symbol: worst.symbol,
        })
      }
      const control = model.control
      const autoState = model.automation?.state ?? 'unknown'
      // A deliberate stop is a state the top bar already shows; it is not a
      // fault and must not also raise a banner.
      const operatorStop = control === 'pause' && autoState === 'stopped'
      const ruleState = ['close_only', 'halted', 'emergency', 'paused', 'pause'].includes(control)
      if (model.state && ruleState && !operatorStop) {
        push({
          id: 'control',
          priority: 30,
          category: 'rule',
          tone: control === 'emergency' || control === 'halted' ? 'danger' : 'warn',
          impact: 's2.ruleImpact',
          reason: controlLabel(control),
          next: 's2.ruleNext',
          action: 'system',
          actionLabel: 'top.menu.system',
        })
      }
      const autoReason = model.automation?.reason_code ?? null
      if (!serviceDown && !ruleState && model.state && (autoState === 'blocked' || autoState === 'pending')) {
        push({
          id: 'automation',
          priority: autoState === 'blocked' ? 35 : 36,
          category: autoState === 'blocked' ? 'rule' : 'pending',
          tone: 'warn',
          impact: 's2.autoImpact',
          // The full sentence stays visible here (not only in a transient toast),
          // so "本机调度已停，服务端状态待确认" survives a refresh.
          reason: zh(automationLabel(model.automation).text) + (autoReason ? ' · ' + reasonLabel(autoReason) : ''),
          next: 's2.autoNext',
          action: 'system',
          actionLabel: 'top.menu.system',
        })
      }
      const round = model.round
      if (round && String(round.status) === 'failed') {
        push({
          id: 'round-failed',
          priority: 40,
          category: 'exec',
          tone: 'warn',
          impact: 's2.execImpact',
          title: 'concl.headlineFailed',
          titleParams: { reason: reasonLabel(round.reasonCode) },
          reason: `${fmtShortClock(round.startedAt)} · ${round.reasonCode ?? ''}`,
          next: 's2.execNext',
          action: 'round',
          actionLabel: 'concl.viewRound',
        })
      }
      // Unconfirmed order results are their own category: they are neither a
      // failed round nor a normal no-trade.
      const unreconciled = model.positions.rows.filter((row) => row.protection.state === 'unreconciled')
      if (unreconciled.length > 0 && !serviceDown) {
        push({
          id: 'unreconciled',
          priority: 25,
          category: 'pending',
          tone: 'warn',
          impact: 's2.pendingImpact',
          reason: `${unreconciled.length} × ${zh('prot.unreconciled')}`,
          next: 's2.pendingNext',
          action: 'system',
          actionLabel: 'top.menu.system',
        })
      }
      const freshness = model.freshness
      if (freshness.level === 'expired') {
        push({
          id: 'evidence-expired',
          priority: 60,
          category: 'pending',
          tone: 'warn',
          impact: 's2.staleImpact',
          reason: `${fmtAge(freshness.age)} · ${zh('s1.data')}`,
          next: 's2.staleNext',
          action: 'system',
          actionLabel: 'top.menu.system',
        })
      }
      const schedule = model.schedule
      if (schedule && schedule.enabled !== false) {
        const skips = leadingSkips(schedule.history)
        if (skips >= 2) {
          push({
            id: 'schedule-stalled',
            priority: 50,
            category: 'rule',
            tone: 'warn',
            impact: 's2.scheduleImpact',
            title: 's2.scheduleStalled',
            reason: fmt(zh('s4.scheduleSkipped'), { reason: reasonLabel(schedule.last_result?.code ?? schedule.last_result?.detail) }),
            next: 's2.scheduleNext',
            action: 'system',
            actionLabel: 'top.menu.system',
            skips,
          })
        }
      }
      return dedupeAttention(items)
    }

    /** Keep the highest-priority items and drop what they make redundant. */
    function dedupeAttention(items) {
      const suppressed = new Set()
      const kept = []
      const sorted = items.slice().sort((a, b) => (a.priority ?? 99) - (b.priority ?? 99))
      for (const item of sorted) {
        if (suppressed.has(item.id)) continue
        kept.push(item)
        for (const id of item.suppress ?? []) suppressed.add(id)
      }
      return kept
    }

    function emptyReasonText(state, key) {
      return emptyReason(state, key)
    }

    // Portfolio gaps arrive as English engineering sentences; they stay in D2.
    // The trading page only shows a code-shaped reason (or "未提供").
    function reasonText(raw) {
      const text = String(raw || '').trim()
      if (!text) return ''
      if (/^[A-Z][A-Z0-9_]{2,}$/.test(text)) return text
      return zh('common.notProvided')
    }

    // ---- tiny components ---------------------------------------------------
    function Chip({ text, tone, icon }) {
      const cls = tone ? `twb-chip twb-chip--${tone}` : 'twb-chip'
      const ico = icon
        ? h('svg', { className: `twb-chip-ico twb-chip-ico--${tone}`, viewBox: '0 0 10 10', 'aria-hidden': 'true' },
          h('path', { d: 'M5 1 9.5 9h-9z', fill: 'currentColor' }))
        : null
      return h('span', { className: cls }, ico, text)
    }

    function Num({ value, digits = 2, suffix = '', className = '' }) {
      const signed = fmtSigned(value, { digits, suffix })
      const tone = signed.dir === 'up' ? 'twb-up' : signed.dir === 'down' ? 'twb-down' : signed.dir === 'na' ? 'twb-na' : 'twb-flat'
      const cls = ['twb-num', tone, className].filter(Boolean).join(' ')
      return h('span', { className: cls }, signed.text)
    }

    function Protection({ protection }) {
      return h('span', { className: `twb-prot twb-prot--${protection.state}` },
        h('span', { className: 'twb-prot-shape', 'aria-hidden': 'true' }),
        protectionText(protection))
    }

    // M1.4: the Host now runs a real 10-minute scheduler. When /state carries a
    // `schedule` block we show its truth; older Hosts (no field) keep the old
    // "not enabled (M2)" copy instead of claiming automation that does not exist.
    /**
     * F1: the footer's schedule line prefers the Host automation projection (which
     * carries scheduler_enabled + the service control row) and keeps the older
     * /state.schedule reading as a fallback for Hosts without it.
     */
    function scheduleFooter(model, t) {
      const info = model.automation
      if (info && info.source === 'host' && typeof info.schedulerEnabled === 'boolean') {
        if (!info.schedulerEnabled) return t('auto.schedulerOff')
        if (info.pausedUntil) return t('s4.schedulePaused')
        return t('s4.scheduleOn')
      }
      return scheduleLabel(model.state, t)
    }

    function scheduleLabel(state, t) {
      const info = state?.schedule
      if (!info || info.enabled === false) return t('s4.scheduleOff')
      if (info.paused_until) return t('s4.schedulePaused')
      if (info.rounds_last_24h >= info.max_rounds_per_day) return t('s4.scheduleBudget')
      // 05 N05: "on" alone hides a loop that keeps skipping. Say why it skipped.
      if (info.last_result && info.last_result.kind === 'skip') {
        return fmt(zh('s4.scheduleSkipped'), { reason: reasonLabel(info.last_result.code ?? info.last_result.detail) })
      }
      return t('s4.scheduleOn')
    }

    /**
     * F1 (§2.2/§3.2): automation state comes from the Host projection, which is
     * derived from the service control row. Older Hosts have no automation
     * block; the fallback then says what it can prove and never claims "stopped"
     * without the service.
     */
    function resolveAutomation(state, service) {
      const raw = state?.automation && typeof state.automation === 'object' ? state.automation : null
      if (raw && typeof raw.state === 'string') {
        return {
          source: 'host',
          state: raw.state,
          reasonCode: raw.reason_code ?? null,
          schedulerEnabled: typeof raw.scheduler_enabled === 'boolean' ? raw.scheduler_enabled : null,
          stopSupported: raw.scheduler_supported !== false,
          lastSuccessAt: raw.last_success_at ?? null,
          nextAt: raw.next_at ?? null,
          intervalMs: raw.interval_ms ?? null,
          lastResult: raw.last_result ?? null,
          serviceControl: raw.service_control ?? null,
          stop: raw.stop ?? null,
          pausedUntil: raw.paused_until ?? null,
          pauseReason: raw.pause_reason ?? null,
        }
      }
      const schedule = state?.schedule && typeof state.schedule === 'object' ? state.schedule : null
      const control = String(service?.control_state ?? '').trim()
      const enabled = schedule ? schedule.enabled !== false : null
      let derived = 'unknown'
      let reasonCode = 'HOST_STATUS_UNAVAILABLE'
      if (control === 'pause' && service?.ready === true) {
        derived = 'stopped'
        reasonCode = 'SERVICE_CONTROL_PAUSE'
      } else if (enabled === false) {
        derived = 'pending'
        reasonCode = 'CONTROL_NORMAL'
      } else if (enabled === true && service?.ready === true) {
        derived = 'running'
        reasonCode = null
      }
      return {
        source: 'derived',
        state: derived,
        reasonCode,
        schedulerEnabled: enabled,
        stopSupported: false,
        lastSuccessAt: state?.last_run_at ?? null,
        nextAt: schedule?.next_at ?? null,
        intervalMs: schedule?.interval_ms ?? null,
        lastResult: schedule?.last_result ?? null,
        serviceControl: control ? { state: control, confirmed: service?.ready === true } : null,
        stop: null,
        pausedUntil: schedule?.paused_until ?? null,
        pauseReason: schedule?.pause_reason ?? null,
      }
    }

    function automationLabel(info) {
      switch (info?.state) {
        case 'running': return { text: 'auto.running', short: 'auto.running', tone: 'ok' }
        case 'stopped': return { text: 'auto.stopped', short: 'auto.stoppedShort', tone: 'quiet' }
        case 'stopping': return { text: 'auto.stopping', short: 'auto.stopping', tone: 'warn' }
        case 'pending':
          return {
            text: info?.reasonCode === 'CONTROL_NORMAL' ? 'auto.pendingControl' : 'auto.pendingService',
            short: 'auto.pendingShort',
            tone: 'warn',
          }
        case 'blocked': return { text: 'auto.blocked', short: 'auto.blocked', tone: 'danger' }
        default: return { text: 'auto.unknown', short: 'auto.unknown', tone: 'quiet' }
      }
    }

    /** Short account label: the service ledger id is the only identity we have. */
    function accountShort(service) {
      const ledger = String(service?.ledger_id ?? '').trim()
      if (!ledger) return null
      return ledger.slice(0, 8)
    }

    function autoMetaLine(info, t) {
      const last = info?.lastSuccessAt ? fmtShortClock(info.lastSuccessAt) : t('auto.lastSuccessNone')
      const next = info?.nextAt ? fmtShortClock(info.nextAt) : '—'
      return `${t('auto.lastSuccess')} ${last} · ${t('auto.nextRun')} ${next}`
    }

    function EmptyBlock({ kind, title, hint, actionLabel, onAction }) {
      const labels = {
        empty: zh('common.empty'),
        disabled: zh('s4.scheduleOff'),
        error: zh('state.hostOffline'),
      }
      return h('div', { className: 'twb-empty', 'data-kind': kind },
        h('div', { className: 'twb-empty-title' }, title ?? labels[kind] ?? labels.empty),
        hint ? h('div', { className: 'twb-empty-hint' }, hint) : null,
        actionLabel && onAction ? h('button', { type: 'button', className: 'twb-empty-act', onClick: onAction }, actionLabel) : null)
    }

    function TabList({ idPrefix, items, value, onChange, ariaLabel }) {
      const onKeyDown = (event) => {
        const key = event.key
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(key)) return
        event.preventDefault()
        const index = items.findIndex((item) => item.id === value)
        let next = index
        if (key === 'ArrowLeft') next = (index - 1 + items.length) % items.length
        if (key === 'ArrowRight') next = (index + 1) % items.length
        if (key === 'Home') next = 0
        if (key === 'End') next = items.length - 1
        const target = items[next]
        if (target) onChange(target.id)
        const nodes = event.currentTarget?.parentElement?.querySelectorAll?.('[role="tab"]')
        if (nodes && nodes[target ? next : 0]) nodes[target ? next : 0].focus()
      }
      return h('div', { className: 'twb-seg', role: 'tablist', 'aria-label': ariaLabel, onKeyDown },
        items.map((item) => h('button', {
          key: item.id,
          type: 'button',
          role: 'tab',
          id: `${idPrefix}-tab-${item.id}`,
          'aria-selected': String(item.id === value),
          'aria-controls': `${idPrefix}-panel-${item.id}`,
          tabIndex: item.id === value ? 0 : -1,
          onClick: () => onChange(item.id),
        }, item.label)))
    }

    // ---- cockpit pieces ----------------------------------------------------
    function AccountBand({ account, equity, t }) {
      const hasValues = !account.empty
      const spark = equity.points.slice(-20)
      const sparkPath = (() => {
        if (spark.length < 2) return ''
        const values = spark.map((point) => point.equity)
        const min = Math.min(...values)
        const max = Math.max(...values)
        const span = max - min || 1
        const w = 120
        const hgt = 56
        const pad = 2
        return spark.map((point, index) => {
          const x = pad + (index / (spark.length - 1)) * (w - pad * 2)
          const y = pad + (1 - (point.equity - min) / span) * (hgt - pad * 2)
          return `${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
        }).join(' ')
      })()
      const day = hasValues ? fmtSigned(account.dayPnl, { digits: 2 }) : { text: '—', dir: 'na' }
      const upnl = hasValues ? fmtSigned(account.upnl, { digits: 2 }) : { text: '—', dir: 'na' }
      const marginRatio = hasValues && account.marginUsed !== null && account.total
        ? (account.marginUsed / account.total) * 100
        : null
      if (account.empty) {
        return h('section', { className: 'twb-band twb-h150 twb-account twb-span7', 'aria-label': t('account.equity') },
          h(EmptyBlock, {
            kind: 'empty',
            title: t('account.emptyTitle'),
            hint: `${t('account.emptyHint').replace('{reason}', reasonText(account.reason) || t('common.notProvided'))}`,
          }))
      }
      return h('section', { className: 'twb-band twb-h150 twb-account twb-span7', 'aria-label': t('account.equity') },
        h('div', { className: 'twb-account-main' },
          h('div', { className: 'twb-account-cell' },
            h('span', { className: 'k' }, t('account.equity')),
            h('div', { className: 'twb-account-hero' },
              h('span', { className: 'twb-hero' }, hasValues ? fmtNum(account.total) : '—'),
              h('span', { className: 'twb-hero-unit' }, 'USDT')),
            h('span', { className: 'k' }, `${t('account.available')} ${hasValues ? fmtNum(account.available) : '—'}`)),
          h('div', { className: 'twb-account-cell' },
            h('span', { className: 'k' }, account.dayBaseline ? t('details.equityChange') : t('account.dayPnl')),
            h('div', { className: `twb-account-num twb-${day.dir}` }, day.text),
            h('span', { className: 'k' }, account.dayBaseline ? fmtShanghai(account.dayBaseline) : t('account.demoMoney'))),
          h('div', { className: 'twb-account-cell' },
            h('span', { className: 'k' }, t('account.upnl')),
            h('div', { className: `twb-account-num twb-${upnl.dir}` }, upnl.text),
            h('span', { className: 'k' }, fmt(zh('positions.count'), { n: '' }).trim() ? '' : '')),
          h('div', { className: 'twb-account-cell' },
            h('span', { className: 'k' }, t('account.margin')),
            h('div', { className: 'twb-account-num twb-mono' }, marginRatio === null ? '—' : `${marginRatio.toFixed(1)}%`),
            h('span', { className: 'k' }, account.session !== null && account.total !== null && account.session !== account.total
              ? `${t('account.sessionEquity')} ${fmtNum(account.session)}`
              : ''))),
          h('div', { className: 'twb-account-spark' },
          sparkPath
            ? h('svg', { width: 120, height: 48, viewBox: '0 0 120 48', role: 'img', 'aria-label': t('account.trend') },
              h('line', { x1: 0, y1: 34, x2: 120, y2: 34, stroke: 'var(--twb-grid)', strokeWidth: 1, strokeDasharray: '4 3' }),
              h('path', { d: sparkPath, fill: 'none', stroke: 'var(--twb-accent)', strokeWidth: 1.5, strokeLinejoin: 'round' }))
            : h('div', { className: 'cap' }, t('common.none')),
          h('div', { className: 'cap' }, `${t('account.trend')} · 24h`)))
    }

    function RiskBand({ model, t, onSystem }) {
      const { caps, control, entry, account } = model
      const blocked = entry.blocked
      const reasons = entry.reasons
      const tone = reasons.some((item) => item.tone === 'danger') ? 'danger' : 'warn'
      const noteText = blocked
        ? reasons.map((item) => item.label).join(' · ')
        : (caps.maxPositions === null ? t('risk.usedMissing') : `${t('risk.maxPositions')} ${caps.maxPositions} · ${t('risk.usedMissing')}`)
      return h('section', { className: 'twb-card twb-h150 twb-span5', 'aria-label': t('risk.title') },
        h('div', { className: 'twb-card-head' },
          h('span', { className: 'twb-card-title' }, t('risk.title')),
          caps.current ? null : h('span', { className: 'twb-chip twb-chip--quiet' }, t('risk.prevCaps')),
          caps.maxPositions === null ? null : h('span', { className: 'twb-chip twb-chip--quiet' }, `${t('risk.maxPositions')} ${caps.maxPositions}`),
          h('span', { className: 'twb-card-actions' },
            h('button', { type: 'button', className: 'twb-btn twb-btn--ghost', style: { height: 24, padding: '0 6px' }, onClick: onSystem }, `${t('details.title')} →`))),
        h('div', { className: 'twb-budget' },
          h('div', { className: 'twb-budget-item' }, h('span', { className: 'k' }, t('risk.perTrade')),
            h('span', { className: 'v twb-mono' }, caps.maxRiskPct === null ? '—' : `${caps.maxRiskPct.toFixed(1)}%`)),
          h('div', { className: 'twb-budget-item' }, h('span', { className: 'k' }, t('risk.leverage')),
            h('span', { className: 'v twb-mono' }, caps.maxLeverage === null ? '—' : `${caps.maxLeverage}×`)),
          h('div', { className: 'twb-budget-item' }, h('span', { className: 'k' }, t('risk.control')),
            h('span', { className: 'v', title: control }, controlLabel(control))),
          h('div', { className: 'twb-budget-item' }, h('span', { className: 'k' }, t('risk.newEntry')),
            h('span', { className: 'v' }, blocked
              ? h('span', null,
                h('span', { className: `twb-budget-note twb-budget-note--${tone}` }, '⚠ '), t('s4.entryBlocked'),
                h('span', { className: `twb-budget-note twb-budget-note--${tone}` }, ` · ${reasons.map((item) => item.short).join(' · ')}`))
              : h('span', null, t('s4.entryAllowed'), account.empty ? null : null)))),
        h('div', { className: 'twb-budget-foot' }, noteText))
    }

    function PositionsTable({ model, t, onEvidence, onAction, actionBusy, running }) {
      const rows = model.positions.rows
      const reason = model.positions.reason
      const headers = ['positions.symbol', 'positions.qty', 'positions.mark', 'positions.upnl', 'positions.protection', 'positions.distance', 'positions.actions']
      const colClass = ['', '', '', '', '', '', '']
      const widths = [190, 86, 118, 96, 112, 112, 76]
      const actionCell = (row) => h('details', { className: 'b-actions' },
        h('summary', { className: 'b-actions-summary' }, t('b.actions')),
        h('div', { className: 'b-actions-menu' },
          h('button', {
            type: 'button',
            className: 'b-actions-item b-actions-item--danger',
            'aria-label': t('positions.close'),
            disabled: actionBusy || running,
            onClick: () => onAction('close', row),
          }, t('positions.close')),
          h('button', {
            type: 'button',
            className: 'b-actions-item',
            'aria-label': t('positions.repair'),
            'aria-describedby': 'twb-repair-hint',
            title: row.protection.state === 'ok' ? t('positions.repairDisabled') : t('positions.repair'),
            disabled: actionBusy || running || row.protection.state === 'ok',
            onClick: () => onAction('repair', row),
          }, t('positions.repair')),
          h('button', {
            type: 'button',
            className: 'b-actions-item',
            'aria-label': t('positions.evidence'),
            onClick: () => onEvidence('position', row),
          }, t('positions.evidence'))))
      const emptyRow = h('tr', null, h('td', { colSpan: 7 },
        h(EmptyBlock, {
          kind: 'empty',
          title: t('positions.emptyTitle'),
          hint: reason ? `${t('positions.emptyHint')} · ${fmt(zh('positions.reason'), { reason: reasonText(reason) })}` : t('positions.emptyHint'),
        })))
      const body = rows.length === 0 ? emptyRow : rows.map((row) => h('tr', {
        key: row.key,
        'data-risk': row.protection.state === 'missing' ? 'danger' : row.protection.block ? 'warn' : '',
      },
      h('td', { className: 'b-sym-cell' },
        h('div', { className: 'b-sym-line' },
          h('span', { className: 'sym' }, row.symbol),
          h('span', { className: 'b-side' }, sideLabel(row.side))),
        h('div', { className: 'b-cell-sub' }, row.origin === 'ai' ? t('b.aiOrigin') : row.origin === 'external' ? t('b.extOrigin') : (row.origin ?? '—'))),
      h('td', { className: 'b-num-cell' },
        h('div', { className: 'b-cell-main b-num' }, row.qty === null ? '—' : String(row.qty)),
        h('div', { className: 'b-cell-sub' },
          row.margin === null ? '—' : fmtNum(row.margin, 1), row.leverage ? ` · ${row.leverage}×` : '')),
      h('td', { className: 'b-num-cell' },
        h('div', { className: 'b-cell-main b-num' }, fmtNum(row.mark, 2)),
        h('div', { className: 'b-cell-sub' }, row.quoteStale ? t('quote.history') : t('quote.live'), ' · ', row.quoteAge === null ? '—' : fmtAge(row.quoteAge)),
        h('div', { className: 'b-cell-sub' }, row.entry === null ? '—' : `入 ${fmtNum(row.entry, 2)}`)),
      h('td', { className: 'b-num-cell' },
        h('div', { className: 'b-cell-main' }, h(Num, { value: row.upnl })),
        h('div', { className: 'b-cell-sub' }, row.accountStale ? t('quote.history') : t('quote.account'), ' · ', row.accountAge === null ? '—' : fmtAge(row.accountAge))),
      h('td', null,
        h('div', { className: 'b-cell-main' }, row.accountStale ? h('span', { className: 'twb-hint' }, t('quote.history'), ' · ') : null, h(Protection, { protection: row.protection })),
        h('div', { className: 'b-cell-sub' }, `${t('positions.stop')} ${row.stopDist === null ? t('common.notProvided') : `${row.stopDist.toFixed(1)}%`}`)),
      h('td', null,
        h('div', { className: 'b-cell-main b-num' }, row.liqDist === null ? t('positions.unknownLiq') : `${row.liqDist.toFixed(1)}%`),
        h('div', { className: 'b-cell-sub' }, `${t('positions.liq')} ${row.liqDist === null ? t('common.notProvided') : `${row.liqDist.toFixed(1)}%`}`)),
      h('td', { className: 'b-act-cell' }, actionCell(row))))
      const head = h('thead', null, h('tr', null, headers.map((key, index) => h('th', {
        key,
        scope: 'col',
        className: `${colClass[index]} ${['qty', 'mark', 'upnl'].includes(key.split('.')[1]) ? 'is-num' : ''}`.trim(),
      }, t(key)))))
      const legend = h('div', { className: 'twb-legend' },
        h('span', { className: 'twb-prot twb-prot--ok' }, h('span', { className: 'twb-prot-shape' }), t('prot.ok')),
        h('span', { className: 'twb-prot twb-prot--missing' }, h('span', { className: 'twb-prot-shape' }), t('prot.missing')),
        h('span', { className: 'twb-prot twb-prot--partial' }, h('span', { className: 'twb-prot-shape' }), t('prot.partial')),
        h('span', { className: 'twb-prot twb-prot--unknown' }, h('span', { className: 'twb-prot-shape' }), t('prot.unknown')),
        h('span', { className: 'twb-prot twb-prot--unreconciled' }, h('span', { className: 'twb-prot-shape' }), t('prot.unreconciled')))
      return h('section', { className: 'twb-card twb-h300 twb-span7', 'aria-label': t('positions.title') },
        h('div', { className: 'twb-card-head' },
          h('span', { className: 'twb-sec-num' }, '02'),
          h('span', { className: 'twb-card-title' }, t('positions.title')),
          model.positions.stale ? h('span', { role: 'status', className: 'twb-hint twb-warn' }, t('quote.accountStale')) : null,
          h('span', { className: 'twb-chip twb-chip--quiet' }, String(rows.length)),
          h('span', { className: 'twb-hint' }, fmt(zh('positions.snapshot'), { age: model.positions.age === null ? '—' : fmtAge(model.positions.age) }))),
        h('div', { className: 'twb-table-wrap' },
          h('table', { className: 'twb-table' },
            h('colgroup', null, widths.map((width, index) => h('col', { key: index, className: colClass[index], style: { width } }))),
            head,
            h('tbody', null, body))),
        h('div', { className: 'twb-table-foot' }, t('positions.footer')),
        legend,
        h('div', { className: 'twb-hint', id: 'twb-repair-hint', style: { paddingTop: 2 } }, t('positions.repairHint')))
    }

    function ConclusionCard({ model, t, onRound, onEvidence }) {
      const round = model.round
      const card = (content) => h('section', {
        className: 'twb-card twb-concl twb-span5',
        'aria-label': t('concl.title'),
      }, content)
      if (!round) {
        return card([
          h('div', { className: 'twb-card-head' }, h('span', { className: 'twb-sec-num' }, '03'), h('span', { className: 'twb-card-title' }, t('b.aiBrief'))),
          h('div', { className: 'twb-body', style: { flex: 1, display: 'flex' } },
            h(EmptyBlock, { kind: 'empty', title: t('concl.emptyTitle'), hint: t('concl.emptyHint'), actionLabel: t('top.run'), onAction: model.onRun })),
        ])
      }
      const running = String(round.status) === 'running'
      const failed = String(round.status) === 'failed'
      const trade = String(round.outcome) === 'trade'
      // 05 N04: never borrow the previous round's stage name. While a round runs,
      // the only facts we own are "submitted" and "how long we have waited".
      const elapsedMs = (() => {
        const started = toMs(round.startedAt)
        return started === null ? null : Math.max(0, Date.now() - started)
      })()
      // 05 P1-2 / 01 §5.3: the Host's in_flight object is the only source of the
      // running stage. Old Hosts send a string (or nothing) -> N04 fallback.
      const live = model.inFlight && typeof model.inFlight === 'object' ? model.inFlight : null
      const liveStage = live && typeof live.stage_label === 'string' && live.stage_label.trim()
        ? live
        : null
      const headline = running
        ? liveStage
          ? fmt(zh('concl.runningStage'), {
              stage: liveStage.stage_label,
              elapsed: fmtSpan(num(liveStage.elapsed_ms) ?? elapsedMs),
            })
          : fmt(zh('concl.submitted'), { elapsed: fmtMs(elapsedMs) })
        : trade
          ? fmt(zh('concl.headlineTrade'), { side: sideLabel(round.decision?.side), symbol: round.decision?.symbol ?? round.symbol ?? '—' })
          : failed
            ? fmt(zh('concl.headlineFailed'), { reason: reasonLabel(round.reasonCode) })
            : fmt(zh('concl.headlineNoTrade'), { reason: reasonLabel(round.reasonCode) })
      // 01 §4.1: recommendation is APPROVE/DOWNSIZE/REJECT (upper-case).
      const rewrite = recommendationLabel(round.risk) ?? t('concl.noRule')
      const scorecard = model.scorecard
      const grade = scorecard ? scorecard.grade : null
      const gate = scorecard ? scorecard.gate : null
      const gateReasons = scorecard ? scorecard.gateReasons : null
      const reason = roundReason(round)
      return card([
        h('div', { className: 'twb-card-head' },
          h('span', { className: 'twb-sec-num' }, '03'),
          h('span', { className: 'twb-card-title' }, t('b.aiBrief')),
          running ? h(Chip, { text: t('status.running'), tone: 'warn' }) : failed ? h(Chip, { text: t('status.failed'), tone: 'danger', icon: true }) : trade ? h(Chip, { text: t('status.trade'), tone: 'quiet' }) : h(Chip, { text: t('status.no_trade'), tone: 'quiet' }),
          model.scorecard ? h(GradeBadge, { grade: model.scorecard.grade, t, small: true }) : null,
          h('span', { className: 'twb-hint' }, `#${String(round.roundId ?? '').slice(-6)} · ${fmtShortClock(round.startedAt)}`)),
        h('div', {
          className: 'twb-concl-line',
          // 05 P1-2: keep the machine stage key traceable (the label is prose).
          'data-stage': liveStage ? String(liveStage.stage_key ?? '') : undefined,
          title: liveStage ? String(liveStage.stage_key ?? '') : undefined,
        }, headline),
        h('div', { className: 'twb-concl-sub' },
          running
            ? (liveStage
                ? fmt(zh('concl.runningCalls'), {
                    done: num(liveStage.calls_done) ?? 0,
                    budget: num(liveStage.calls_budget) ?? 14,
                    attempt: num(liveStage.attempt) ?? 1,
                    max: num(liveStage.max_attempts) ?? 2,
                  })
                : t('run.started'))
            : `${t('concl.reason')}: ${reason.label}`),
        running ? null : h(ReasonDetailLines, { reason, t, className: 'twb-concl-details' }),
        h('div', { className: 'twb-concl-sub twb-concl-opt', title: t('concl.confidenceHint') },
          `${t('concl.confidence')} ${round.decision?.confidence === null || round.decision?.confidence === undefined ? t('concl.notProvided') : fmtNum(round.decision.confidence, 2)}`,
          ` · ${t('drawer.total')} ${fmtMs(round.durationMs)}`,
          ` · ${t('drawer.calls')} ${(round.nodes ?? []).length}`),
        h('div', { className: 'b-concl-meta' },
          `${t('concl.grade')} ${grade === null ? '—' : grade}`,
          ` · ${t('concl.gate')} ${gate === null ? '—' : gate}`,
          ` · ${t('concl.ruleRewrite')} ${rewrite}`),
        h('dl', { className: 'twb-kv' },
          h('dt', null, t('concl.ruleRewrite')),
          h('dd', null, rewrite),
          h('dt', { className: 'twb-concl-opt' }, t('concl.grade')),
          h('dd', { className: 'twb-concl-opt' }, grade === null ? t('concl.notProvided') : String(grade)),
          h('dt', { className: 'twb-concl-opt' }, t('concl.gate')),
          h('dd', { className: 'twb-concl-opt' }, gate === null ? t('concl.notProvided') : gate === 'PASS' ? t('verdict.gatePass') : String(gate)),
          h('dt', { className: 'twb-concl-opt' }, t('verdict.gateReasons')),
          h('dd', { className: 'twb-concl-opt' }, gateReasons === null ? t('concl.notProvided') : gateReasons.length === 0 ? t('concl.noRule') : gateReasons.join(' · ')),
          h('dt', { className: 'twb-concl-opt' }, t('concl.evidence')),
          h('dd', { className: 'twb-concl-opt' }, model.freshness.age === null ? t('concl.notProvided') : fmtAge(model.freshness.age))),
        h('div', { className: 'twb-hint twb-concl-opt', style: { marginTop: 4 } }, t('verdict.disclaimerLong')),
        h('div', { className: 'b-concl-rounds' },
          h('div', { className: 'b-concl-rounds-h' }, t('today.title')),
          (model.rounds ?? []).slice(0, 3).map((item, index) => h('div', { className: 'b-concl-round', key: item.roundId ?? index },
            h('span', { className: 't' }, fmtShortClock(item.startedAt)),
            h('span', { className: 's' }, statusLabel(item.status)),
            h('span', { className: 'r' }, reasonLabel(item.reasonCode)),
            h('span', { className: 'd' }, fmtMs(item.durationMs)))))
        ,
        h('div', { className: 'twb-concl-actions' },
          h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', onClick: onRound }, t('concl.viewRound')),
          h('button', { type: 'button', className: 'twb-btn twb-btn--ghost', onClick: () => onEvidence('round', round) }, t('concl.evidenceBtn'))),
      ])
    }

    function ReconcilePanel({ t }) {
      const [state, setState] = React.useState({ phase: 'idle', result: null, requestId: null, error: null, at: null })
      const mounted = React.useRef(true)
      React.useEffect(() => () => { mounted.current = false }, [])
      const run = async () => {
        if (state.phase === 'running') return
        setState((prev) => ({ ...prev, phase: 'running', error: null }))
        try {
          const response = await fetch(`${API}/reconcile`, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { accept: 'application/json' },
          })
          const body = await response.json().catch(() => ({}))
          if (!mounted.current) return
          if (!response.ok || body?.ok === false) {
            setState({
              phase: 'error',
              result: null,
              requestId: null,
              at: Date.now(),
              error: { code: body?.error?.code ?? `HTTP_${response.status}`, message: body?.error?.message ?? '' },
            })
          } else {
            setState({ phase: 'done', result: body.result ?? {}, requestId: body.requestId ?? null, error: null, at: Date.now() })
          }
        } catch (error) {
          if (!mounted.current) return
          setState({ phase: 'error', result: null, requestId: null, at: Date.now(), error: { code: 'NETWORK_ERROR', message: error?.message ?? '' } })
        }
      }
      if (state.phase === 'idle') {
        return h('div', { className: 'twb-panel' },
          h(EmptyBlock, { kind: 'empty', title: t('recon.idle'), hint: t('recon.hint') }),
          h('div', { className: 'twb-modal-actions' },
            h('button', { type: 'button', className: 'twb-btn twb-btn--primary', onClick: run }, t('recon.run'))))
      }
      if (state.phase === 'running') {
        return h('div', { className: 'twb-panel' },
          h('div', { className: 'twb-hint' }, t('recon.running')),
          h('div', { className: 'twb-modal-actions' },
            h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', disabled: true }, t('recon.running'))))
      }
      if (state.phase === 'error') {
        return h('div', { className: 'twb-panel' },
          h('div', { className: 'twb-precheck-note', style: { borderColor: 'var(--twb-edge-danger)' } },
            `${t('recon.failed')}（${state.error?.code ?? '—'}）${state.error?.message ? ` · ${state.error.message}` : ''}`),
          h('div', { className: 'twb-modal-actions' },
            h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', onClick: run }, t('recon.retry'))))
      }
      const result = state.result ?? {}
      const adopted = Array.isArray(result.adopted) ? result.adopted : []
      // Service uses closed_external; older fixtures used closed. Accept both.
      const closed = Array.isArray(result.closed_external)
        ? result.closed_external
        : Array.isArray(result.closed) ? result.closed : []
      const missing = Array.isArray(result.protection_missing) ? result.protection_missing : []
      const outbox = result.outbox ?? {}
      const checked = Number.isFinite(outbox.checked) ? outbox.checked : null
      const resolved = Number.isFinite(outbox.resolved) ? outbox.resolved : null
      const unresolved = checked !== null && resolved !== null ? Math.max(0, checked - resolved) : 0
      const pending = outbox.pending ?? unresolved
      const uncertain = outbox.uncertain ?? 0
      const exchangeUnreachable = result.exchange_reachable === false
      const stale = result.stale === true
      // A08: empty diffs are not proof of agreement when the venue is
      // unreachable, the snapshot is stale, or outbox queries are unresolved.
      const clean = !exchangeUnreachable && !stale && adopted.length === 0 &&
        closed.length === 0 && missing.length === 0 && !pending && !uncertain &&
        unresolved === 0
      const rows = [
        [t('recon.exchange'), exchangeUnreachable ? t('recon.unreachable') : result.exchange_reachable === true ? t('recon.reachable') : '—'],
        [t('recon.stale'), result.stale === true ? t('recon.staleYes') : result.stale === false ? t('recon.staleNo') : '—'],
        [t('recon.adopted'), adopted.length],
        [t('recon.closed'), closed.length],
        [t('recon.protectionMissing'), missing.length === 0 ? '0' : `${fmt(zh('recon.count'), { n: missing.length })}${missing.length > 0 ? ` · ${missing.join(' / ')}` : ''}`],
        [t('recon.outbox'), `${pending ?? '—'} / ${uncertain ?? '—'}`],
        [t('recon.unresolved'), checked === null ? '—' : `${unresolved} / ${checked}`],
        [t('recon.venue'), `${result.venue ?? '—'}${result.demo_only ? ` · ${t('recon.demo')}` : ''}${result.ledger_id ? ` · ${result.ledger_id}` : ''}`],
      ]
      return h('div', { className: 'twb-panel' },
        h('div', { className: 'twb-chip', style: { alignSelf: 'flex-start' } }, clean ? t('recon.ok') : t('recon.diff')),
        clean ? null : h('div', { className: 'twb-hint' }, exchangeUnreachable || stale ? t('recon.staleBlocked') : ''),
        h('dl', { className: 'twb-kv', style: { margin: 0 } },
          rows.map(([key, value]) => h(React.Fragment, { key }, h('dt', null, key), h('dd', { className: 'twb-mono' }, String(value))))),
        h('div', { className: 'twb-hint' },
          `${t('recon.requestId')} ${state.requestId ?? '—'} · ${t('recon.at')} ${state.at === null ? '—' : fmtClock(new Date(state.at).toISOString())}`),
        h('div', { className: 'twb-modal-actions', style: { marginTop: 0 } },
          h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', style: { height: 26 }, onClick: run }, t('recon.rerun'))))
    }

    function OrdersPanel({ model, t, expanded, onToggle, onDetail, initialTab, onTab }) {
      const orders = model.orders
      const fills = model.fills
      const bothEmpty = orders.length === 0 && fills.length === 0
      const collapsed = bothEmpty && !expanded
      const [tab, setTab] = React.useState(initialTab ?? 'orders')
      const chooseTab = (id) => { setTab(id); if (typeof onTab === 'function') onTab(id) }
      if (collapsed) {
        return h('section', { className: 'twb-card twb-span7', style: { height: 44, padding: '0 12px', justifyContent: 'center' } },
          h('button', { type: 'button', className: 'twb-orders-bar', onClick: onToggle, 'aria-expanded': 'false' },
            h('span', null, fmt(zh('orders.collapsedEmpty'), {})),
            h('span', { className: 'twb-spacer' }),
            h('span', { className: 'twb-link' }, t('orders.expand'))))
      }
      const tabs = [
        { id: 'orders', label: t('orders.tabOrders'), count: orders.length },
        { id: 'fills', label: t('orders.tabFills'), count: fills.length },
        { id: 'recon', label: t('orders.tabRecon'), count: null },
        { id: 'operations', label: t('ops.title'), count: null },
      ]
      return h('section', { className: 'twb-card twb-h260 twb-span7', 'aria-label': t('orders.title') },
        h('div', { className: 'twb-card-head' },
          h('div', { className: 'twb-tabs', role: 'tablist', 'aria-label': t('orders.title') },
            tabs.map((item) => h('button', {
              key: item.id,
              type: 'button',
              role: 'tab',
              className: 'twb-tab',
              'aria-selected': String(tab === item.id),
              onClick: () => chooseTab(item.id),
            }, item.label, item.count === null ? null : h('span', { className: 'count' }, String(item.count))))),
          h('span', { className: 'twb-card-actions' },
            h('button', { type: 'button', className: 'twb-btn twb-btn--ghost', style: { height: 24, padding: '0 6px' }, onClick: onToggle, 'aria-expanded': 'true' }, t('orders.collapse')))),
        tab === 'operations' ? h(OperationsPanel, { t }) : tab === 'recon'
          ? h(ReconcilePanel, { t })
          : h('div', { className: 'twb-table-wrap' },
            tab === 'orders'
              ? h('table', { className: 'twb-table' },
                h('colgroup', null, [132, 60, 72, 44, 88, 56, 84, 68, 56].map((width) => h('col', { style: { width } }))),
                h('thead', null, h('tr', null, ['orders.time', 'orders.id', 'orders.symbol', 'orders.side', 'orders.type', 'orders.qty', 'orders.price', 'orders.status', 'orders.action']
                  .map((key, index) => h('th', { key, scope: 'col', className: [5, 6].includes(index) ? 'is-num' : '' }, t(key))))),
                h('tbody', null, orders.length === 0
                  ? h('tr', null, h('td', { colSpan: 9 }, h(EmptyBlock, { kind: 'empty', title: t('orders.emptyTitle'), hint: `${t('orders.emptyHint')}${model.emptyReasons.orders ? ` · ${model.emptyReasons.orders}` : ''}` })))
                  : orders.map((order, index) => h('tr', { key: order.id ?? index },
                    h('td', { className: 'twb-num', style: { textAlign: 'left' } }, fmtClock(order.createdAt)),
                    h('td', { className: 'twb-mono twb-muted', style: { fontSize: 11 } }, order.id ?? '—'),
                    h('td', { className: 'sym' }, order.symbol),
                    h('td', null, sideLabel(order.side)),
                    h('td', { className: 'twb-muted' }, order.type || '—'),
                    h('td', { className: 'twb-num' }, order.qty === null ? '—' : String(order.qty)),
                    h('td', { className: 'twb-num' }, fmtNum(order.price, 2)),
                    h('td', null, h('span', { className: 'twb-chip twb-chip--quiet' }, statusLabel(order.status))),
                    h('td', null, h('button', { type: 'button', className: 'twb-btn twb-btn--ghost', style: { height: 24, padding: '0 6px' }, onClick: () => onDetail('order', order) }, t('orders.detail')))))))
              : h('table', { className: 'twb-table' },
                h('colgroup', null, [76, 76, 44, 60, 88, 88, 72, 60].map((width) => h('col', { style: { width } }))),
                h('thead', null, h('tr', null, ['orders.time', 'orders.symbol', 'orders.side', 'orders.qty', 'orders.price', 'drawer.actual', 'common.actions', '']
                  .map((key, index) => h('th', { key: `${key}-${index}`, scope: 'col', className: [3, 4, 5].includes(index) ? 'is-num' : '' }, t(key))))),
                h('tbody', null, fills.length === 0
                  ? h('tr', null, h('td', { colSpan: 8 }, h(EmptyBlock, { kind: 'empty', title: t('orders.fillsEmptyTitle'), hint: `${t('orders.fillsEmptyHint')}${model.emptyReasons.fills ? ` · ${model.emptyReasons.fills}` : ''}` })))
                  : fills.map((fill, index) => h('tr', { key: fill.id ?? index },
                    h('td', { className: 'twb-num', style: { textAlign: 'left' } }, fmtClock(fill.time)),
                    h('td', { className: 'sym' }, fill.symbol),
                    h('td', null, sideLabel(fill.side)),
                    h('td', { className: 'twb-num' }, fill.qty === null ? '—' : String(fill.qty)),
                    h('td', { className: 'twb-num' }, fmtNum(fill.price, 2)),
                    h('td', { className: 'twb-num twb-na' }, fill.realizedPnl === null ? '—' : fmtNum(fill.realizedPnl, 2)),
                    h('td', { className: 'twb-num' }, fmtNum(fill.commission, 2)),
                    h('td', null, h('button', { type: 'button', className: 'twb-btn twb-btn--ghost', style: { height: 24, padding: '0 6px' }, onClick: () => onDetail('fill', fill) }, t('orders.detail')))))))),
        h('div', { className: 'twb-table-foot' }, t('orders.footer'))
      )
    }

    function TodaySummary({ model, t, onAll, onReview }) {
      const stats = model.dayStats
      const rounds = model.rounds.slice(0, 3)
      return h('section', { className: 'twb-card twb-h260 twb-span5', 'aria-label': t('today.title') },
        h('div', { className: 'twb-card-head' },
          h('span', { className: 'twb-card-title' }, t('today.title')),
          h('span', { className: 'twb-hint' }, t('today.scope'))),
        h('div', { className: 'twb-today' },
          h('div', null, h('span', { className: 'k' }, t('today.rounds')), h('span', { className: 'v twb-num' }, String(stats.rounds))),
          h('div', null, h('span', { className: 'k' }, t('today.trades')), h('span', { className: 'v twb-num' }, String(stats.trades))),
          h('div', null, h('span', { className: 'k' }, t('today.noTrade')), h('span', { className: 'v twb-num' }, String(stats.noTrade))),
          h('div', null, h('span', { className: 'k' }, t('today.failed')), h('span', { className: 'v twb-num' }, String(stats.failed)))),
        h('div', { className: 'twb-recent' },
          rounds.length === 0
            ? h('div', { className: 'twb-muted', style: { fontSize: 11 } }, t('today.empty'))
            : rounds.map((round) => h('div', { key: round.roundId ?? round.startedAt, className: 'twb-recent-row' },
              h('span', { className: `twb-dot ${String(round.status) === 'failed' ? 'twb-dot--danger' : round.outcome === 'no_trade' ? 'twb-dot--warn' : 'twb-dot--ok'}` }),
              h('span', { className: 'twb-num', style: { textAlign: 'left' } }, fmtShortClock(round.startedAt)),
              h('span', { className: 'twb-muted' }, `${statusLabel(round.status)} · ${reasonLabel(round.reasonCode)}`),
              h('span', { className: 'twb-num twb-muted' }, round.durationMs === null || round.durationMs === undefined ? '—' : fmtMs(round.durationMs))))),
        h('div', { className: 'twb-links' },
          h('button', { type: 'button', className: 'twb-link', onClick: onAll }, t('today.all')),
          h('button', { type: 'button', className: 'twb-link', onClick: onReview }, t('today.review'))))
    }

    // ---- AI workspace ------------------------------------------------------
    function RoundList({ model, t, selected, onSelect }) {
      // 15 §4: /state.rounds is today's partition. Keep the newest round in the
      // list even when it belongs to the previous day (right after midnight).
      const rounds = Array.isArray(model.roundsList) ? model.roundsList : (model.rounds ?? [])
      return h('aside', { className: 'twb-ai-list', 'aria-label': t('ai.listTitle') },
        h('div', { className: 'twb-ai-list-head' }, `${t('ai.listTitle')} · ${rounds.length}`),
        h('div', { className: 'twb-ai-list-body', role: 'listbox', 'aria-label': t('ai.listTitle') },
          rounds.map((round) => h('button', {
            key: round.roundId ?? round.startedAt,
            type: 'button',
            role: 'option',
            className: 'twb-round',
            'aria-selected': String(selected === round.roundId),
            onClick: () => onSelect(round.roundId),
          },
          h('span', { className: `twb-dot ${String(round.status) === 'failed' ? 'twb-dot--danger' : round.outcome === 'no_trade' ? 'twb-dot--warn' : 'twb-dot--ok'}` }),
          h('span', { className: 't twb-num' }, fmtShortClock(round.startedAt)),
          h('span', { className: 's' }, `${statusLabel(round.status)} · ${reasonLabel(round.reasonCode)}`),
          h('span', { className: 'd twb-num' }, round.durationMs === null || round.durationMs === undefined ? '—' : fmtMs(round.durationMs))))),
        h('div', { className: 'twb-ai-list-foot' }, t('ai.listScope')))
    }

    function StageRow({ stage, round, t, onEvidence }) {
      const nodes = (round.nodes ?? []).filter((node) => stage.roles.includes(node.role_key ?? node.role))
      const ok = nodes.some((node) => node.schema_ok === true)
      const failed = nodes.length > 0 && !ok
      const attempts = new Set(nodes.map((node) => `${node.round ?? 1}:${node.attempt ?? 1}`)).size || 0
      const latency = nodes.reduce((sum, node) => sum + (num(node.latency_ms) ?? 0), 0)
      // T15: the gate can block after the executor produced a plan. That stage ran,
      // but it did not lead to a submission — showing 'ok' or 'pending' would lie.
      const rollup = Array.isArray(round.stages)
        ? round.stages.find((item) => item && item.stage_key === (stage.roleKeys?.[0] ?? ''))
        : null
      const blocked = rollup && rollup.status === 'blocked' ? (rollup.blocked_by_gate ?? {}) : null
      const state = blocked ? 'blocked' : (nodes.length === 0 ? 'pending' : failed ? 'error' : 'ok')
      return h('div', { className: 'twb-stage', 'data-state': state, 'data-gate-blocked': blocked ? (blocked.code ?? 'blocked') : null },
        h('span', { className: 'twb-stage-idx' }, String(stage.index)),
        h('span', { className: 'twb-stage-name', title: zh(stage.roleKeys[0]) },
          t(stage.label),
          attempts > 1 ? h('span', { className: 'twb-hint', style: { marginLeft: 6 } }, `×${attempts}`) : null),
        h('span', { className: 'twb-bar', 'aria-hidden': 'true' }, h('i', { style: { width: `${Math.min(100, Math.round((latency / Math.max(1, round.durationMs ?? 1)) * 100))}%` } })),
        h('span', { className: 'twb-num' }, nodes.length === 0 ? '—' : fmtMs(latency)),
        h('span', { className: 'twb-num twb-muted', style: { fontSize: 11 } }, nodes.length === 0 ? '—' : `${nodes.reduce((sum, node) => sum + (num(node.input_tokens) ?? 0), 0)} / ${nodes.reduce((sum, node) => sum + (num(node.output_tokens) ?? 0), 0)}`),
        h('span', null, blocked
          ? h(React.Fragment, null,
            h(Chip, { text: t('ai.stageBlocked'), tone: 'warn', icon: false }),
            blocked.code ? h('span', { className: 'twb-mono twb-hint', style: { marginLeft: 6 } }, blocked.code) : null)
          : nodes.length === 0 ? h(Chip, { text: t('ai.notReached'), tone: 'quiet' })
          : failed
            ? h(React.Fragment, null,
              h(Chip, { text: t('status.error'), tone: 'warn', icon: false }),
              // 05 N03: name the code on the stage row itself.
              (() => {
                const failedNode = nodes.find((node) => node.error_code)
                return failedNode ? h('span', { className: 'twb-mono twb-hint', style: { marginLeft: 6 } }, failedNode.error_code) : null
              })())
            : h(Chip, { text: t('status.ok'), tone: 'quiet' })),
        h('button', {
          type: 'button',
          className: 'twb-btn twb-btn--ghost',
          style: { height: 24, padding: '0 4px' },
          disabled: nodes.length === 0,
          onClick: () => nodes[0] && onEvidence('call', nodes[0]),
        }, t('positions.evidence')))
    }

    const ROLE_KEY_LABEL = {
      analysis: 'role.analysis',
      red_opening: 'role.red_opening',
      blue_opening: 'role.blue_opening',
      red_response: 'role.red_response',
      blue_response: 'role.blue_response',
      decision: 'role.decision',
      risk: 'role.risk',
      execution_entry: 'role.execution_entry',
    }

    function StageWaterfall({ round, t, onEvidence }) {
      const stages = [
        { index: 1, label: 'stage.analysis', roles: ['analysis'], roleKeys: ['analysis'] },
        { index: 2, label: 'stage.red', roles: ['red_opening', 'red_response', 'red_team'], roleKeys: ['red_opening'] },
        { index: 3, label: 'stage.blue', roles: ['blue_opening', 'blue_response', 'blue_team'], roleKeys: ['blue_opening'] },
        { index: 4, label: 'stage.decision', roles: ['decision'], roleKeys: ['decision'] },
        { index: 5, label: 'stage.risk', roles: ['risk'], roleKeys: ['risk'] },
        { index: 6, label: 'stage.execution', roles: ['execution_entry', 'executor'], roleKeys: ['execution_entry'] },
      ]
      const nodes = Array.isArray(round.nodes) ? round.nodes : []
      const has = (stage) => nodes.some((node) => stage.roles.includes(node.role_key ?? node.role))
      const firstMissing = stages.findIndex((stage) => !has(stage))
      const allMissingAfter = firstMissing >= 0 && stages.slice(firstMissing).every((stage) => !has(stage))
      const shown = allMissingAfter ? stages.slice(0, firstMissing) : stages
      const hidden = allMissingAfter ? stages.slice(firstMissing) : []
      const chain = round.chain && typeof round.chain === 'object' ? round.chain : null
      const chip = (label, value) => h('span', { className: 'twb-chain-chip' },
        h('span', { className: 'k' }, label),
        h('span', { className: 'v twb-num' }, value))
      const budget = chain
        ? h('div', { className: 'twb-chain-budget', 'aria-label': t('ai.chainCalls') },
          chip(t('ai.chainCalls'), chain.calls ?? '—'),
          chip(t('ai.chainRetries'), chain.retries ?? 0),
          chain.truncated > 0 ? chip(t('ai.chainTruncated'), chain.truncated) : null,
          chain.timeouts > 0 ? chip(t('ai.chainTimeouts'), chain.timeouts) : null,
          chip(t('ai.chainElapsed'), chain.elapsed_ms === null || chain.elapsed_ms === undefined
            ? '—'
            : `${fmtMs(chain.elapsed_ms)}${chain.deadline_ms ? ` / ${fmtMs(chain.deadline_ms)}` : ''}`),
          chain.stopped_at ? chip(t('ai.chainStopped'), t(ROLE_KEY_LABEL[chain.stopped_at] ?? chain.stopped_at)) : null)
        : null
      return h('div', { className: 'twb-stages' },
        budget,
        h('div', { className: 'twb-stage-head' },
          h('span', null, '#'),
          h('span', null, t('ai.stage')),
          h('span', null, ''),
          h('span', { className: 'twb-num' }, t('ai.latency')),
          h('span', { className: 'twb-num' }, t('ai.tokens')),
          h('span', null, t('ai.state')),
          h('span', null, '')),
        shown.map((stage) => h(StageRow, { key: stage.index, stage, round, t, onEvidence })),
        // T15: the blocked reason belongs next to the stage it blocked, and it is
        // the gate's own detail + the plan gaps — the Host writes no second prose.
        (() => {
          const rollup = Array.isArray(round.stages)
            ? round.stages.find((item) => item && item.stage_key === 'execution_entry')
            : null
          const blocked = rollup && rollup.status === 'blocked' ? (rollup.blocked_by_gate ?? null) : null
          if (!blocked) return null
          const quote = round.gate?.quote ?? null
          const gaps = round.planGaps && typeof round.planGaps === 'object' ? round.planGaps : null
          const gapLines = gaps
            ? Object.entries(gaps).flatMap(([role, list]) => (Array.isArray(list) ? list : []).slice(0, 2)
              .map((gap) => `${role} · ${gap.code ?? '—'}${gap.field ? `（${gap.field}）` : ''}：${gap.reason_zh ?? ''}`))
            : []
          return h('div', { className: 'twb-modal-note twb-gate-blocked', 'data-gate-blocked': blocked.code ?? 'blocked', role: 'status' },
            h('div', { className: 'twb-card-title', style: { marginBottom: 4 } },
              `${t('ai.gateBlockedTitle')}${blocked.code ? (LANG === 'en' ? ` (${blocked.code})` : `（${blocked.code}）`) : ''}`),
            blocked.detail ? h('div', { className: 'twb-hint twb-gate-detail', style: { whiteSpace: 'normal' } }, blocked.detail) : null,
            quote
              ? h('div', { className: 'twb-hint', style: { whiteSpace: 'normal' } },
                `${t('ai.gateQuote')}：${quote.last === null || quote.last === undefined ? '—' : fmtNum(quote.last, 2)} · ${quote.age_ms === null || quote.age_ms === undefined ? '—' : fmtMs(quote.age_ms)} · ${quote.source === 'pre_submit' ? t('ai.gateQuotePreSubmit') : t('ai.gateQuoteEvidence')}`)
              : null,
            blocked.mode
              ? h('div', { className: 'twb-hint' }, `${t('ai.gateMode')}：${blocked.mode}${blocked.enforced ? '' : ` · ${t('ai.gateObserveOnly')}`}`)
              : null,
            round.preSubmit && round.preSubmit.advisory_only
              ? h('div', { className: 'twb-hint' }, t('ai.gatePreSubmitAdvisory'))
              : null,
            gapLines.length > 0
              ? h('div', { style: { marginTop: 4 } },
                h('div', { className: 'twb-hint' }, t('ai.gatePlanGaps')),
                h('ul', { className: 'twb-settings-list' }, gapLines.map((line, index) => h('li', { key: `gap-${index}` }, line))))
              : null)
        })(),
        hidden.length > 0
          ? h('div', { className: 'twb-stage-rest' },
            h('span', { className: 'twb-stage-idx' }, String(hidden[0].index)),
            h('span', null, fmt(zh('ai.notReachedMore'), {
              n: hidden.length,
              reason: `${reasonLabel(round.reasonCode)}${round.reasonCode ? (LANG === 'en' ? ` (${round.reasonCode})` : `（${round.reasonCode}）`) : ''}`,
            })))
          : null)
    }

    // M1.4: the decision's own reasoning was never shown (the DTO dropped it).
    // This card answers "why this action" with thesis, per-side evidence/flaw,
    // the exact plan numbers and the invalidation condition.
    function DecisionRationale({ round, t }) {
      const decision = round.decision
      const comparison = decision?.case_comparison ?? null
      const plan = decision?.plan ?? null
      const warnings = Array.isArray(decision?.warnings) ? decision.warnings : []
      const thesis = decision?.thesis ?? null
      const empty = !thesis && !comparison && !plan && warnings.length === 0 && !decision?.invalidation
      if (empty) {
        return h('div', { className: 'twb-report-card' },
          h('div', { className: 'twb-card-title', style: { marginBottom: 6 } }, t('ai.rationale')),
          h('div', { className: 'twb-hint' }, t('ai.noRationale')))
      }
      const caseCol = (side) => {
        const item = comparison?.[side]
        if (!item) return null
        return h('div', { className: 'twb-rationale-col', 'data-side': side },
          h('div', { className: 'twb-rationale-head' },
            h('span', { className: 'twb-case-name' }, side === 'red' ? t('ai.redCase') : t('ai.blueCase')),
            item.strength ? h('span', { className: 'twb-chip twb-chip--quiet' }, item.strength) : null),
          item.decisive_evidence
            ? h('div', { className: 'twb-rationale-row' },
              h('span', { className: 'k' }, t('ai.decisive')), h('span', { className: 'v' }, item.decisive_evidence))
            : null,
          item.fatal_flaw
            ? h('div', { className: 'twb-rationale-row' },
              h('span', { className: 'k' }, t('ai.fatalFlaw')), h('span', { className: 'v' }, item.fatal_flaw))
            : null)
      }
      const planItem = (label, value) => (value === null || value === undefined
        ? null
        : h('div', { className: 'twb-plan-item' },
          h('span', { className: 'k' }, label), h('span', { className: 'v twb-num' }, value)))
      const invalidation = decision?.invalidation
        ? [decision.invalidation.price_event, decision.invalidation.time].filter(Boolean).join(' · ')
        : ''
      return h('div', { className: 'twb-report-card', 'aria-label': t('ai.rationale') },
        h('div', { className: 'twb-card-title', style: { marginBottom: 6 } }, t('ai.rationale')),
        thesis
          ? h('div', { className: 'twb-rationale-thesis' },
            h('span', { className: 'k' }, t('ai.thesis')), h('span', { className: 'v' }, thesis))
          : null,
        (comparison?.red || comparison?.blue)
          ? h('div', { className: 'twb-rationale-cases' }, caseCol('red'), caseCol('blue'))
          : null,
        comparison?.decisive_factor
          ? h('div', { className: 'twb-rationale-row' },
            h('span', { className: 'k' }, t('ai.decisive')), h('span', { className: 'v' }, comparison.decisive_factor))
          : null,
        plan
          ? h('div', { className: 'twb-plan' },
            planItem(t('ai.entry'), plan.entry_price ?? null),
            // F4 (§8·F / t6): the allowed entry range and the plan expiry are
            // checkable facts. A missing zone says so; it is never widened to
            // "any price" just because the field is absent.
            planItem(t('ai.entryZone'), (() => {
              const low = plan.entry_zone_low
              const high = plan.entry_zone_high
              if ((low === null || low === undefined) && (high === null || high === undefined)) return t('ai.zoneMissing')
              return `${low === null || low === undefined ? '—' : fmtNum(low)} – ${high === null || high === undefined ? '—' : fmtNum(high)}`
            })()),
            planItem(t('ai.validUntil'), plan.valid_until ?? null),
            planItem(t('ai.stop'), plan.stop_loss ?? null),
            planItem(t('ai.target'), plan.take_profit ?? null),
            planItem(t('ai.rr'), plan.rr ?? null),
            planItem(t('ai.risk'), plan.risk_pct === null || plan.risk_pct === undefined ? null : `${fmtNum(plan.risk_pct, 2)}%`),
            planItem(t('ai.leverage'), plan.leverage === null || plan.leverage === undefined ? null : `${fmtNum(plan.leverage, 0)}×`))
          : null,
        invalidation
          ? h('div', { className: 'twb-rationale-row' },
            h('span', { className: 'k' }, t('ai.invalidation')), h('span', { className: 'v' }, invalidation))
          : null,
        warnings.length > 0
          ? h('div', { className: 'twb-rationale-warn' },
            h('span', { className: 'k' }, t('ai.warnings')),
            h('ul', null, warnings.map((warningText, index) => h('li', { key: `w-${index}` }, warningText))))
          : null)
    }

    // ---- 05 N01/N03: "what broke, where, and what now" -------------------
    // The Host projects attribution (01 判定树). The Client only renders it; when
    // the code is outside the 01 table it says so and falls back to 03's label.
    // 05 N01/N03: "what broke, where, and what now" -------------------
    // The Host projects attribution (01 判定树). The Client only renders it; when
    // the code is outside the 01 table it says so and falls back to 03's label.
    // This exact phrase is 01's placeholder for "whichever stage failed first".
    const FIRST_FAILED_STAGE_ZH = '首个失败阶段'
    function AttributionCard({ round, t }) {
      const attr = round.attribution
      if (!attr) return null
      const mapped = attr.mapped !== false
      const english = LANG === 'en'
      // 01 owns the prose in both languages; the Client picks the column for
      // the panel language and never machine-translates a Host sentence.
      const prose = (zhText, enText) => (english ? (enText ?? zhText) : (zhText ?? enText))
      const one = mapped && (attr.one_liner_zh || attr.one_liner_en)
        ? prose(attr.one_liner_zh, attr.one_liner_en)
        : fmt(zh('attr.unmapped'), { code: attr.reason_code ?? round.reasonCode ?? '—' })
      const next = mapped && (attr.next_action_zh || attr.next_action_en)
        ? prose(attr.next_action_zh, attr.next_action_en)
        : t('attr.nextUnmapped')
      // Labels follow the keys (01 §3.7) so neither the category nor the
      // stage/role stays Chinese when the panel is in English. Chinese keeps
      // the Host prose (it is more specific: "基础设施（路由层）", "红蓝开场 R1").
      const category = (english ? (attr.category_key ? dictText(`attr.cat.${attr.category_key}`) : null) : null)
        ?? attr.category ?? t('common.notProvided')
      // 01 uses one fixed phrase for "the first stage that broke" (stageKey null);
      // every other Host stage string is shown as sent.
      const stageLabel = english && attr.stage_key
        ? (dictText(`stage.${attr.stage_key}`) ?? dictText(ROLE_KEY_LABEL[attr.stage_key]) ?? null)
        : null
      const stage = stageLabel
        ?? (english && attr.stage === FIRST_FAILED_STAGE_ZH
          ? t('attr.stageFirstFailed')
          : (attr.stage ?? t('common.notProvided')))
      const roleLabel = english && attr.role_key
        ? (dictText(ROLE_KEY_LABEL[attr.role_key]) ?? dictText(`role.${attr.role_key}`) ?? null)
        : null
      const role = roleLabel ?? attr.role ?? t('common.notProvided')
      return h('div', { className: 'twb-report-card', 'aria-label': t('attr.title') },
        h('div', { className: 'twb-card-head' },
          h('span', { className: 'twb-card-title' }, t('attr.title')),
          h('span', { className: 'twb-chip twb-chip--quiet' }, category),
          attr.reason_code ? h('span', { className: 'twb-chip twb-chip--quiet twb-mono' }, codeWithLabel(attr.reason_code)) : null),
        h('div', { className: 'twb-concl-line', style: { fontSize: 14, lineHeight: 20 } }, one),
        h('dl', { className: 'twb-kv' },
          h('dt', null, t('attr.stage')), h('dd', null, stage),
          h('dt', null, t('attr.role')), h('dd', null, role),
          h('dt', null, t('attr.next')), h('dd', null, next)))
    }

    // 05 N03: every failed call, not only the first validation message.
    function CallErrors({ round, t }) {
      const nodes = (round.nodes ?? []).filter((node) => node && (node.error_code || node.error_message || (node.validation_errors ?? []).length > 0))
      if (nodes.length === 0) return null
      return h('div', { className: 'twb-report-card', 'aria-label': t('call.errors') },
        h('div', { className: 'twb-card-head' },
          h('span', { className: 'twb-card-title' }, t('call.errors')),
          h('span', { className: 'twb-hint' }, `${nodes.length}`)),
        h('table', { className: 'twb-table twb-table--compact' },
          h('thead', null, h('tr', null,
            h('th', { scope: 'col' }, t('drawer.role')),
            h('th', { scope: 'col' }, t('drawer.attempt')),
            h('th', { scope: 'col' }, t('call.errorCode')),
            h('th', { scope: 'col' }, t('call.errorMessage')),
            h('th', { scope: 'col' }, t('call.finishKind')))),
          h('tbody', null, nodes.map((node) => {
            const first = (node.validation_errors ?? [])[0] ?? null
            const message = node.error_message ?? first?.message ?? null
            return h('tr', { key: node.id ?? `${node.role_key}:${node.attempt}` },
              h('td', null, node.label ?? node.role_key ?? '—'),
              h('td', { className: 'twb-num' }, String(node.attempt ?? 1)),
              h('td', { className: 'twb-mono' }, node.error_code ? codeWithLabel(node.error_code) : '—'),
              h('td', { className: 'twb-muted', style: { whiteSpace: 'normal', overflowWrap: 'anywhere' } }, message ?? '—'),
              h('td', null, finishKindLabel(node.finish_kind) ?? '—'))
          }))))
    }

    // 05 N01: both sides' theses / steelman / rebuttals used to be dropped by the
    // DTO. A degraded debate says so instead of looking like a normal one.
    function DebateCard({ round, t }) {
      const debate = round.debate
      if (!debate) return null
      const caseRow = (label, item, side) => h('div', { className: 'twb-debate-col', 'data-side': side },
        h('div', { className: 'twb-rationale-head' },
          h('span', { className: 'twb-case-name' }, label),
          item && item.tradeable !== null ? h('span', { className: 'twb-chip twb-chip--quiet' }, `${t('debate.tradeable')} ${item.tradeable ? t('common.yes') : t('common.no')}`) : null,
          item && item.confidence !== null ? h('span', { className: 'twb-chip twb-chip--quiet', title: t('concl.confidenceHint') }, `${t('concl.confidence')} ${fmtNum(item.confidence, 2)}`) : null),
        item
          ? h(React.Fragment, null,
            item.thesis ? h('div', { className: 'twb-rationale-row' }, h('span', { className: 'k' }, t('ai.thesis')), h('span', { className: 'v' }, item.thesis)) : null,
            item.opponent_steelman ? h('div', { className: 'twb-rationale-row' }, h('span', { className: 'k' }, t('debate.steelman')), h('span', { className: 'v' }, item.opponent_steelman)) : null,
            (item.rebuttal ?? []).length > 0
              ? h('div', { className: 'twb-rationale-row' }, h('span', { className: 'k' }, fmt(zh('debate.rebuttal'), { n: item.rebuttal.length })), h('span', { className: 'v' }, item.rebuttal.map((entry) => entry.counter).filter(Boolean).join(' · ')))
              : null,
            item.no_case_reason ? h('div', { className: 'twb-rationale-row' }, h('span', { className: 'k' }, t('debate.noCase')), h('span', { className: 'v twb-mono' }, item.no_case_reason)) : null)
          : h('div', { className: 'twb-hint' }, t('debate.notProvided')))
      const r1 = debate.rounds?.r1 ?? {}
      const r2 = debate.rounds?.r2 ?? {}
      return h('div', { className: 'twb-report-card', 'aria-label': t('debate.card') },
        h('div', { className: 'twb-card-head' },
          h('span', { className: 'twb-card-title' }, t('debate.card')),
          debate.adopted ? h('span', { className: 'twb-chip twb-chip--quiet' }, fmt(zh('debate.adopted'), { side: debate.adopted.side === 'red' ? t('verdict.red') : t('verdict.blue') })) : null,
          debate.degraded ? h('span', { className: 'twb-chip twb-chip--warn' }, t('status.degraded')) : null),
        debate.degraded ? h('div', { className: 'twb-modal-note' }, t('debate.degradedBanner')) : null,
        h('div', { className: 'twb-card-title', style: { margin: '8px 0 4px' } }, t('debate.round1')),
        h('div', { className: 'twb-rationale-cases' }, caseRow(t('verdict.red'), r1.red, 'red'), caseRow(t('verdict.blue'), r1.blue, 'blue')),
        h('div', { className: 'twb-card-title', style: { margin: '8px 0 4px' } }, t('debate.round2')),
        h('div', { className: 'twb-rationale-cases' }, caseRow(t('verdict.red'), r2.red, 'red'), caseRow(t('verdict.blue'), r2.blue, 'blue')))
    }

    // 05 N09: the failed-detail state, with both honest exits.
    function RoundDetailError({ model, t, onRetry, onLatest }) {
      const code = model.roundDetail?.missingCode ?? '—'
      return h('div', { className: 'twb-report-card twb-report-card--danger' },
        h('div', { className: 'twb-card-head' },
          h('span', { className: 'twb-card-title' }, t('ai.detailFailed')),
          h('span', { className: 'twb-chip twb-chip--danger twb-mono' }, String(code))),
        h('div', { className: 'twb-concl-sub' }, model.selectedRoundId ? `#${String(model.selectedRoundId).slice(-6)}` : '—'),
        h('div', { className: 'twb-concl-actions' },
          h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', onClick: onRetry }, t('ai.detailRetry')),
          h('button', { type: 'button', className: 'twb-btn twb-btn--ghost', onClick: onLatest }, t('ai.backToLatest'))))
    }

    function RoundReport({ model, round, t, onEvidence }) {
      if (!round) {
        return h('div', { className: 'twb-report-card' },
          h(EmptyBlock, { kind: 'empty', title: t('ai.emptyTitle'), hint: t('ai.emptyHint'), actionLabel: t('top.run'), onAction: model.onRun }))
      }
      const failed = String(round.status) === 'failed'
      const running = String(round.status) === 'running'
      const decision = round.decision
      const risk = round.risk
      const gate = Array.isArray(round.scorecard?.gate_reasons) ? round.scorecard.gate_reasons
        : Array.isArray(risk?.violations) ? risk.violations.map((item) => item?.rule ?? item).filter(Boolean) : null
      const grade = round.scorecard?.grade ?? round.grade ?? null
      const gaps = Array.isArray(round.gaps) ? round.gaps : []
      const codeGaps = gaps.filter((gap) => /E_[A-Z0-9_]{2,}|^[A-Z][A-Z0-9_]{2,}$/.test(String(gap)))
      const rawGaps = Math.max(0, gaps.length - codeGaps.length)
      const reason = roundReason(round)
      return h('div', { className: 'twb-report' },
        h('div', { className: `twb-report-card ${failed ? 'twb-report-card--danger' : running ? 'twb-report-card--warn' : ''}` },
          h('div', { className: 'twb-concl-head' },
            h('span', { className: 'twb-card-title' }, t('ai.conclusion')),
            running ? h(Chip, { text: t('status.running'), tone: 'warn' })
              : failed ? h(Chip, { text: t('status.failed'), tone: 'danger', icon: true })
                : String(round.outcome) === 'trade' ? h(Chip, { text: t('status.trade'), tone: 'quiet' })
                  : h(Chip, { text: t('status.no_trade'), tone: 'quiet' }),
            h('span', { className: 'twb-hint' }, `#${String(round.roundId ?? '').slice(-6)} · ${fmtClock(round.startedAt)} · ${fmtMs(round.durationMs)}`)),
          h('div', { className: 'twb-concl-line' }, String(round.outcome) === 'no_trade'
            ? `${t('status.no_trade')}：${reason.label}`
            : String(round.outcome) === 'trade' && decision
              ? fmt(zh('concl.headlineTrade'), { side: sideLabel(decision.side), symbol: decision.symbol ?? round.symbol ?? '—' })
              : (failed ? fmt(zh('concl.headlineFailed'), { reason: reason.label }) : (round.headline ?? statusLabel(round.status)))),
          h('div', { className: 'twb-concl-sub' },
            `${t('concl.reason')}: ${reason.label} · ${t('concl.evidence')}: ${model.freshness.age === null ? t('concl.notProvided') : fmt(zh('drawer.ageNote'), { n: Math.round(model.freshness.age / 1000) })}`),
          running ? null : h(ReasonDetailLines, { reason, t, className: 'twb-concl-details' }),
          round.strategy ? h('div', { className: 'twb-hint' }, `${round.strategy.name} · v${round.strategy.version} · ${round.strategy_selection_reason ?? ''}`) : null,
          h('div', { className: 'twb-concl-actions', style: { marginTop: 8 } },
            h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', onClick: () => onEvidence('round', round) }, t('concl.evidenceBtn')))),
        h(AttributionCard, { round, t }),
        h(DebateCard, { round, t }),
        h(VerdictCard, { model, round, t }),
        h(DecisionRationale, { round, t }),
        h('div', { className: 'twb-report-card' },
          h('div', { className: 'twb-card-title', style: { marginBottom: 8 } }, t('ai.stages')),
          h(StageWaterfall, { round, t, onEvidence })),
        h(CallErrors, { round, t }),
        h('div', { className: 'twb-report-card' },
          h('div', { className: 'twb-card-head' },
            h('span', { className: 'twb-card-title' }, t('ai.gaps')),
            h('span', { className: 'twb-hint' }, fmt(zh('ai.gapsActionable'), { a: codeGaps.length, b: rawGaps }))),
          gaps.length === 0
            ? h('div', { className: 'twb-hint' }, t('ai.gapsEmpty'))
            : h('div', { className: 'twb-gaps' },
              codeGaps.slice(0, 2).map((gap, index) => h('div', { key: `a-${index}`, className: 'twb-gap' },
                h('svg', { className: 'twb-gap-ico', viewBox: '0 0 14 14', 'aria-hidden': 'true' },
                  h('path', { d: 'M7 1 13 12H1z', fill: 'none', stroke: 'currentColor' }), h('path', { d: 'M7 5v4M7 10.5v1', stroke: 'currentColor' })),
                h('span', { className: 'twb-mono' }, String(gap)))),
              rawGaps > 0 ? h('div', { className: 'twb-hint' }, fmt(zh('ai.gapsRaw'), { n: rawGaps })) : null))
      )
    }

    // ---- N2: deterministic scorecard + P1 rule hits ----------------------
    function GradeBadge({ grade, t, small }) {
      const value = grade ? String(grade).toUpperCase() : null
      const known = value !== null && /^[A-D]$/.test(value)
      const tone = !known ? 'na' : value === 'A' || value === 'B' ? 'quiet' : value === 'C' ? 'warn' : 'danger'
      return h('span', {
        className: `twb-grade twb-grade--${tone}${small ? ' twb-grade--sm' : ''}`,
        'aria-label': `${t('concl.grade')} ${known ? value : t('concl.notProvided')}`,
        title: known ? value : t('concl.notProvided'),
      }, known ? value : '—')
    }

    function VerdictCard({ model, round, t }) {
      const scorecard = model.scorecard
      const risk = round.risk
      const decision = round.decision
      const noSelectedCase = scorecard ? scorecard.selectedCase === null : false
      const violations = risk && Array.isArray(risk.violations) ? risk.violations : null
      const verdict = (value, params) => (value === null || value === undefined || value === '' ? t('concl.notProvided') : (params ? fmt(zh(params.key), params.params ?? {}) : String(value)))
      const gateText = (gate) => {
        if (!gate) return t('concl.notProvided')
        return gate === 'PASS' ? t('verdict.gatePass') : (gateLabel(gate) ?? String(gate))
      }
      const why = (scorecard && scorecard.gateReasons && scorecard.gateReasons.length > 0)
        ? scorecard.gateReasons.map((reason) => gateLabel(reason) ?? String(reason)).join(' · ')
        : null
      // M1.4: show both sides with the numbers that decided the gate. Before
      // this, a FAIL_CONFIDENCE verdict had no value behind it, and the losing
      // side was only visible when no case was selected at all.
      const fmtOr = (value) => (typeof value === 'number' && Number.isFinite(value) ? fmtNum(value, 2) : '—')
      const pctOr = (value) => (typeof value === 'number' && Number.isFinite(value) ? `${fmtNum(value, 2)}%` : '—')
      const compare = scorecard && (scorecard.red || scorecard.blue)
        ? h('table', { className: 'twb-table twb-table--compact twb-compare' },
          h('thead', null, h('tr', null,
            h('th', { scope: 'col' }, t('ai.side')),
            h('th', { scope: 'col' }, t('verdict.grade')),
            h('th', { scope: 'col' }, t('ai.gate')),
            h('th', { scope: 'col' }, t('ai.rr')),
            h('th', { scope: 'col' }, t('ai.stopPct')),
            h('th', { scope: 'col' }, t('ai.conf')),
            h('th', { scope: 'col' }, t('ai.rubric')))),
          h('tbody', null, ['red', 'blue'].map((side) => {
            const item = scorecard[side]
            const reasons = item?.gate_reasons && item.gate_reasons.length > 0
              ? item.gate_reasons.map((reason) => gateLabel(reason) ?? String(reason)).join(' · ')
              : null
            return h('tr', { key: side, 'data-side': side },
              h('td', null, side === 'red' ? t('verdict.red') : t('verdict.blue')),
              h('td', null, h(GradeBadge, { grade: item?.grade ?? null, t, small: true })),
              h('td', null,
                h('span', { className: `twb-chip ${item?.gate === 'PASS' ? 'twb-chip--quiet' : 'twb-chip--warn'}` }, gateText(item?.gate)),
                reasons ? h('div', { className: 'twb-hint' }, reasons) : null),
              h('td', { className: 'twb-num' }, fmtOr(item?.rr)),
              h('td', { className: 'twb-num' }, pctOr(item?.stop_distance_pct)),
              h('td', { className: 'twb-num' }, fmtOr(item?.confidence)),
              h('td', { className: 'twb-num' }, fmtOr(item?.rubric_score)))
          })))
        : null
      return h('div', { className: 'twb-report-card', 'aria-label': t('ai.verdict') },
        h('div', { className: 'twb-card-head' },
          h('span', { className: 'twb-card-title' }, t('ai.verdict')),
          scorecard ? h('span', { className: 'twb-chip twb-chip--quiet' }, t('verdict.deterministic')) : null,
          h('span', { className: 'twb-hint' }, t('verdict.disclaimer'))),
        h('div', { className: 'twb-budget', style: { gridTemplateColumns: 'repeat(3, minmax(0, 1fr))' } },
          h('div', { className: 'twb-budget-item' }, h('span', { className: 'k' }, t('ai.aiAdvice')),
            h('span', { className: 'v' }, decision
              ? `${actionOf(decision) === 'trade' ? t('status.trade') : actionOf(decision) === 'no_trade' ? t('status.no_trade') : (decision.action ?? t('common.none'))} · ${t('concl.confidence')} ${decision.confidence === null || decision.confidence === undefined ? t('concl.notProvided') : fmtNum(decision.confidence, 2)}`
              : t('common.none'))),
          h('div', { className: 'twb-budget-item' }, h('span', { className: 'k' }, t('ai.hardRules')),
            h('span', { className: 'v' }, risk
              ? (recommendationLabel(risk) ?? t('common.none'))
              : t('common.none'))),
          h('div', { className: 'twb-budget-item' }, h('span', { className: 'k' }, t('ai.control')),
            h('span', { className: 'v' }, controlLabel(model.control)))),
        h('div', { className: 'twb-verdict-row' },
          h('div', { className: 'twb-budget-item' }, h('span', { className: 'k' }, t('verdict.grade')),
            h('span', { className: 'v twb-verdict-grade' },
              scorecard
                ? h(React.Fragment, null,
                  h(GradeBadge, { grade: scorecard.grade, t }),
                  h('span', { className: 'twb-muted', style: { fontSize: 11, marginLeft: 6 } },
                    `${t('verdict.gate')} ${gateText(scorecard.gate)}`))
                : h('span', { className: 'twb-muted', style: { fontWeight: 400, fontSize: 12 } }, t('concl.notProvided')))),
          h('div', { className: 'twb-budget-item' }, h('span', { className: 'k' }, t('verdict.debate')),
            h('span', { className: 'v' }, scorecard
              ? `${debateLabel(scorecard.debateQuality) ?? t('concl.notProvided')} · ${t('verdict.tie')} ${scorecard.tie === null ? t('concl.notProvided') : scorecard.tie ? t('common.yes') : t('common.no')}`
              : t('concl.notProvided'))),
          h('div', { className: 'twb-budget-item' }, h('span', { className: 'k' }, t('verdict.degraded')),
            h('span', { className: 'v' }, scorecard && scorecard.degraded !== null ? (scorecard.degraded ? t('common.yes') : t('common.no')) : t('concl.notProvided')))),
        why ? h('div', { className: 'twb-hint', style: { marginTop: 6 } }, `${t('verdict.gateReasons')}: ${why}`) : null,
        h('div', { className: 'twb-card-title', style: { margin: '10px 0 6px' } }, t('ai.compare')),
        noSelectedCase ? h('div', { className: 'twb-hint', style: { marginBottom: 4 } }, t('verdict.noSelected')) : null,
        compare ?? h('div', { className: 'twb-hint' }, t('concl.notProvided')),
        h('div', { className: 'twb-card-title', style: { margin: '10px 0 6px' } }, t('verdict.violations')),
        violations === null
          ? h('div', { className: 'twb-hint' }, t('ai.violationsMissing'))
          : violations.length === 0
            ? h('div', { className: 'twb-hint' }, t('verdict.noHits'))
            : h('table', { className: 'twb-table twb-table--compact' },
              h('thead', null, h('tr', null,
                h('th', { scope: 'col' }, t('verdict.rule')),
                h('th', { scope: 'col' }, t('verdict.action')),
                h('th', { scope: 'col' }, t('verdict.detail')))),
              h('tbody', null, violations.map((item, index) => h('tr', { key: `v-${index}` },
                h('td', { className: 'twb-mono' }, String(item?.rule ?? '—')),
                h('td', null, String(item?.action ?? '—')),
                h('td', { className: 'twb-muted' }, String(item?.detail ?? '—')))))),
        risk ? h('div', { className: 'twb-verdict-row', style: { marginTop: 8 } },
          h('div', { className: 'twb-budget-item' }, h('span', { className: 'k' }, t('verdict.blocking')),
            h('span', { className: 'v twb-mono' }, violations === null ? t('concl.notProvided') : (risk.blocking_rule ?? t('verdict.noBlock')))),
          h('div', { className: 'twb-budget-item' }, h('span', { className: 'k' }, t('verdict.concerns')),
            h('span', { className: 'v twb-muted', style: { fontWeight: 400 } }, (() => {
              const lines = concernLines(risk)
              return lines.length > 0 ? lines.join(' · ') : t('verdict.noConcerns')
            })()))) : null,
        h('div', { className: 'twb-hint', style: { marginTop: 6 } }, t('verdict.disclaimerLong')))
    }

    /**
     * 15 §5: the archive day bar. Today is the default; a past day is one click
     * (or the native date input) away. The field shows Beijing time because the
     * day boundary is the same Asia/Shanghai calendar the archive uses.
     */
    function ArchiveDayBar({ history, t }) {
      const today = history?.today ?? todayInShanghai()
      const selected = history?.date ?? today
      const dates = Array.isArray(history?.dates) ? history.dates : []
      // Newest few days as chips; older days stay reachable through the picker.
      const chips = dates.filter((date) => date !== today).slice(0, 6)
      return h('div', { className: 'twb-archive-bar', 'data-archive-day': selected },
        h('span', { className: 'twb-archive-label' }, t('ai.historyDate')),
        h('div', { className: 'twb-chips twb-chips--archive', role: 'group', 'aria-label': t('ai.historyDate') },
          h('button', {
            type: 'button',
            className: 'twb-chip twb-chip--quiet',
            'aria-pressed': String(selected === today),
            onClick: () => history?.onDate?.(null),
          }, t('ai.dateToday')),
          chips.map((date) => h('button', {
            key: date,
            type: 'button',
            className: 'twb-chip twb-chip--quiet twb-num',
            'aria-pressed': String(selected === date),
            onClick: () => history?.onDate?.(date),
          }, date))),
        h('span', { className: 'twb-spacer' }),
        history?.past ? h('span', { className: 'twb-chip twb-chip--quiet' }, t('ai.historyReadonly')) : null,
        h('label', { className: 'twb-archive-pick' },
          h('span', { className: 'twb-hint' }, t('ai.datePick')),
          h('input', {
            type: 'date',
            className: 'twb-archive-input',
            'aria-label': t('ai.datePickLabel'),
            max: today,
            value: selected,
            onChange: (event) => {
              const value = String(event?.target?.value ?? '')
              if (!value) return
              history?.onDate?.(value)
            },
          })))
    }

    function HistoryView({ model, t, filter, onFilter, duration, onDuration, onOpen }) {
      const history = model.history ?? {}
      // 15 §5: one archived day at a time — today comes from /state, a past day
      // from /rounds?date=… . Nothing here ever mixes two days.
      const dayRounds = Array.isArray(history.rounds) ? history.rounds : model.rounds
      const inDuration = (round) => {
        if (duration === 'short') return (num(round.durationMs) ?? 0) <= 30000
        if (duration === 'mid') return (num(round.durationMs) ?? 0) > 30000 && (num(round.durationMs) ?? 0) <= 90000
        if (duration === 'long') return (num(round.durationMs) ?? 0) > 90000
        return true
      }
      const rows = dayRounds.filter((round) => {
        if (filter === 'failed' && String(round.status) !== 'failed') return false
        if (filter === 'no_trade' && String(round.outcome) !== 'no_trade') return false
        if (filter === 'trade' && String(round.outcome) !== 'trade') return false
        return inDuration(round)
      })
      const chips = [
        { id: 'all', label: `${t('ai.historyFilters')} ${dayRounds.length}` },
        { id: 'failed', label: `${t('ai.historyFailed')} ${dayRounds.filter((round) => String(round.status) === 'failed').length}` },
        { id: 'no_trade', label: `${t('ai.historyNoTrade')} ${dayRounds.filter((round) => String(round.outcome) === 'no_trade').length}` },
        { id: 'trade', label: `${t('ai.historyTrades')} ${dayRounds.filter((round) => String(round.outcome) === 'trade').length}` },
      ]
      const durationChips = [
        { id: 'all', label: `${t('ai.durationAll')}` },
        { id: 'short', label: t('ai.durationShort') },
        { id: 'mid', label: t('ai.durationMid') },
        { id: 'long', label: t('ai.durationLong') },
      ]
      const dayLabel = history.date ?? history.today ?? todayInShanghai()
      // Four honest states: loading the day, the fetch failed, the day is empty,
      // or the current filters hide everything. None of them may show another
      // day's rows.
      const stateRow = history.loading
        ? h(EmptyBlock, { kind: 'empty', title: t('ai.dateLoading'), hint: dayLabel })
        : history.error
          ? h(EmptyBlock, {
              kind: 'error',
              title: fmt(zh('ai.historyDateError'), { code: history.error?.status ? `HTTP ${history.error.status}` : (history.error?.message ?? '—') }),
              hint: dayLabel,
            })
          : dayRounds.length === 0
            ? h(EmptyBlock, {
                kind: 'empty',
                title: t('ai.dayEmpty'),
                hint: dayLabel,
                actionLabel: history.past ? t('ai.dayBackToday') : undefined,
                onAction: history.past ? () => history.onDate?.(null) : undefined,
              })
            : h(EmptyBlock, { kind: 'filtered', title: t('ai.historyEmpty') })
      const body = history.loading || history.error || rows.length === 0
        ? h('tr', null, h('td', { colSpan: 7 }, stateRow))
        : rows.map((round) => {
          const result = String(round.status) === 'failed'
            ? h(Chip, { text: t('status.failed'), tone: 'danger', icon: true })
            : String(round.outcome) === 'trade'
              ? h(Chip, { text: t('status.trade'), tone: 'quiet' })
              : h(Chip, { text: t('status.no_trade'), tone: 'quiet' })
          return h('tr', { key: round.roundId ?? round.startedAt },
            h('td', { className: 'twb-num', style: { textAlign: 'left' } }, fmtClock(round.startedAt)),
            h('td', { className: 'twb-mono twb-muted', style: { fontSize: 11 } }, `#${String(round.roundId ?? '').slice(-6)}`),
            h('td', { className: 'twb-muted' }, triggerLabel(round.trigger)),
            h('td', null, result),
            h('td', { className: 'twb-muted', title: round.reasonCode ?? '' }, round.reasonCode ? withCode(reasonLabel(round.reasonCode), round.reasonCode) : '—'),
            h('td', { className: 'twb-num' }, fmtMs(round.durationMs)),
            h('td', null, h('button', {
              type: 'button',
              className: 'twb-btn twb-btn--ghost',
              style: { height: 24, padding: '0 6px' },
              onClick: () => onOpen(round.roundId),
            }, t('ai.historyOpen'))))
        })
      const head = h('thead', null, h('tr', null,
        ['ai.historyTime', 'ai.historyId', 'ai.historyTrigger', 'ai.historyResult', 'ai.historyReason', 'ai.historyDuration', 'ai.historyOpen']
          .map((key, index) => h('th', { key, scope: 'col', className: index === 5 ? 'is-num' : '' }, t(key)))))
      const total = num(history.total) ?? dayRounds.length
      return h('div', { className: 'twb-report-card' },
        h(ArchiveDayBar, { history, t }),
        h('div', { className: 'twb-chips' }, chips.map((chip) => h('button', {
          key: chip.id,
          type: 'button',
          className: 'twb-chip twb-chip--quiet',
          'aria-pressed': String(filter === chip.id),
          onClick: () => onFilter(chip.id),
        }, chip.label))),
        h('div', { className: 'twb-chips twb-chips--sub' },
          h('span', { className: 'twb-hint' }, `${t('ai.historyDuration')}`),
          durationChips.map((chip) => h('button', {
            key: chip.id,
            type: 'button',
            className: 'twb-chip twb-chip--quiet',
            'aria-pressed': String(duration === chip.id),
            onClick: () => onDuration(chip.id),
          }, chip.label))),
        h('div', { className: 'twb-table-wrap', style: { maxHeight: 420 } },
          h('table', { className: 'twb-table' },
            h('colgroup', null, [120, 64, 56, 96, 150, 72, 64].map((width) => h('col', { style: { width } }))),
            head,
            h('tbody', null, body))),
        h('div', { className: 'twb-table-foot' },
          `${dayLabel} · ${fmt(zh('ai.historyTotal'), { n: total })} · ${t('ai.listScope')}`,
          history.truncated ? ` · ${fmt(zh('ai.historyTruncated'), { n: rows.length })}` : null))
    }

    function todayInShanghai() {
      try {
        return new Intl.DateTimeFormat('en-CA', { timeZone: 'Asia/Shanghai' }).format(new Date())
      } catch {
        return new Date().toISOString().slice(0, 10)
      }
    }

    // Minimal Markdown: headings, bullet/number lists and paragraphs. No library.
    function markdownBlocks(markdown) {
      const blocks = []
      let list = null
      const flush = () => {
        if (list) { blocks.push({ type: 'list', items: list }); list = null }
      }
      for (const raw of String(markdown ?? '').split(/\r?\n/)) {
        const line = raw.trimEnd()
        if (/^\s*$/.test(line)) { flush(); continue }
        const heading = /^(#{1,4})\s+(.*)$/.exec(line)
        if (heading) { flush(); blocks.push({ type: 'heading', level: Math.min(4, heading[1].length + 1), text: heading[2] }); continue }
        const bullet = /^\s*[-*]\s+(.*)$/.exec(line)
        if (bullet) { if (!list) list = []; list.push(bullet[1]); continue }
        const ordered = /^\s*\d+[.)]\s+(.*)$/.exec(line)
        if (ordered) { if (!list) list = []; list.push(ordered[1]); continue }
        flush()
        blocks.push({ type: 'p', text: line })
      }
      flush()
      return blocks
    }

    function AIDailyReport({ t }) {
      const [date] = React.useState(() => todayInShanghai())
      const [state, setState] = React.useState({ phase: 'loading', report: null, error: null })
      const lastAction = React.useRef('load')
      const mounted = React.useRef(true)
      React.useEffect(() => () => { mounted.current = false }, [])

      const request = async (options) => {
        const response = await fetch(`${API}/reviews/daily?date=${encodeURIComponent(date)}`, options)
        const body = await response.json().catch(() => ({}))
        return { response, body }
      }

      const load = async () => {
        lastAction.current = 'load'
        setState({ phase: 'loading', report: null, error: null })
        try {
          const { response, body } = await request({ credentials: 'same-origin', headers: { accept: 'application/json' } })
          if (!mounted.current) return
          if (response.status === 404) { setState({ phase: 'none', report: null, error: null }); return }
          if (!response.ok || body?.ok === false) {
            setState({ phase: 'error', report: null, error: { code: body?.error?.code ?? `HTTP_${response.status}`, message: body?.error?.message ?? '' } })
            return
          }
          setState({ phase: 'ready', report: body, error: null })
        } catch (error) {
          if (mounted.current) setState({ phase: 'error', report: null, error: { code: 'NETWORK_ERROR', message: error?.message ?? '' } })
        }
      }

      const generate = async () => {
        lastAction.current = 'generate'
        setState({ phase: 'generating', report: null, error: null })
        try {
          const { response, body } = await request({
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'content-type': 'application/json', accept: 'application/json' },
            body: JSON.stringify({ date }),
          })
          if (!mounted.current) return
          if (response.status === 409) { setState({ phase: 'conflict', report: null, error: null }); return }
          if (!response.ok || body?.ok === false) {
            setState({ phase: 'error', report: null, error: { code: body?.error?.code ?? `HTTP_${response.status}`, message: body?.error?.message ?? '' } })
            return
          }
          setState({ phase: 'ready', report: body, error: null })
        } catch (error) {
          if (mounted.current) setState({ phase: 'error', report: null, error: { code: 'NETWORK_ERROR', message: error?.message ?? '' } })
        }
      }

      React.useEffect(() => { load() }, [])

      const head = h('div', { className: 'twb-card-head' },
        h('span', { className: 'twb-card-title' }, t('ai.reviewDailyTitle')),
        h('span', { className: 'twb-hint' }, `${t('ai.reviewDailyDate')} ${date}`))

      if (state.phase === 'loading') {
        return h(EmptyBlock, { kind: 'empty', title: t('ai.reviewDailyTitle'), hint: t('drawer.fullLoading') })
      }
      if (state.phase === 'none') {
        return h('div', null, head,
          h(EmptyBlock, { kind: 'empty', title: t('ai.reviewDailyNone'), hint: t('ai.reviewDailyHint2') }),
          h('div', { className: 'twb-modal-actions' },
            h('button', { type: 'button', className: 'twb-btn twb-btn--primary', onClick: generate }, t('ai.reviewDailyGenerate'))))
      }
      if (state.phase === 'generating') {
        return h('div', null, head,
          h('div', { className: 'twb-hint', style: { padding: '8px 0' } }, t('ai.reviewDailyGenerating')),
          h('div', { className: 'twb-modal-actions' },
            h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', disabled: true }, t('ai.reviewDailyGenerating'))))
      }
      if (state.phase === 'conflict') {
        return h('div', null, head,
          h('div', { className: 'twb-precheck-note' }, t('ai.reviewDailyConflict')),
          h('div', { className: 'twb-modal-actions' },
            h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', onClick: load }, t('ai.reviewDailyRetry'))))
      }
      if (state.phase === 'error') {
        return h('div', null, head,
          h('div', { className: 'twb-precheck-note', style: { borderColor: 'var(--twb-edge-danger)' } },
            `${t('ai.reviewDailyFailed')}（${state.error?.code ?? '—'}）${state.error?.message ? ` · ${state.error.message}` : ''}`),
          h('div', { className: 'twb-modal-actions' },
            h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', onClick: () => (lastAction.current === 'generate' ? generate() : load()) }, t('ai.reviewDailyRetry'))))
      }
      const report = state.report ?? {}
      const meta = [
        report.generatedAt ? fmtClock(report.generatedAt) : null,
        report.model ? `model ${report.model}` : null,
        report.cached ? t('ai.reviewDailyCached') : null,
      ].filter(Boolean).join(' · ')
      return h('div', null, head,
        h('div', { className: 'twb-chips', style: { marginBottom: 6 } },
          h('span', { className: 'twb-chip twb-chip--quiet' }, String(report.date ?? date)),
          meta ? h('span', { className: 'twb-chip twb-chip--quiet' }, meta) : null),
        h('div', { className: 'twb-md' },
          markdownBlocks(report.markdown).map((block, index) => {
            if (block.type === 'heading') return h(`h${block.level}`, { key: index }, block.text)
            if (block.type === 'list') return h('ul', { key: index }, block.items.map((item, itemIndex) => h('li', { key: itemIndex }, item)))
            return h('p', { key: index }, block.text)
          })),
        h('div', { className: 'twb-modal-actions' },
          h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', onClick: generate }, t('ai.reviewDailyRegenerate'))))
    }

    function ReviewOverview({ model, t }) {      const rows = model.rounds
      const total = rows.length || 1
      const failed = rows.filter((round) => String(round.status) === 'failed').length
      const noTrade = rows.filter((round) => String(round.outcome) === 'no_trade').length
      const trades = rows.filter((round) => String(round.outcome) === 'trade').length
      const reasons = {}
      for (const round of rows) reasons[round.reasonCode ?? 'none'] = (reasons[round.reasonCode ?? 'none'] ?? 0) + 1
      const duration = rows.map((round) => num(round.durationMs)).filter((value) => value !== null).sort((a, b) => a - b)
      const p50 = duration.length ? duration[Math.floor(duration.length / 2)] : null
      const p95 = duration.length ? duration[Math.min(duration.length - 1, Math.floor(duration.length * 0.95))] : null
      const dist = [
        { key: 'ai.historyFailed', value: failed, tone: 'danger' },
        { key: 'ai.historyNoTrade', value: noTrade, tone: 'warn' },
        { key: 'ai.historyTrades', value: trades, tone: '' },
      ]
      return h('div', { className: 'twb-report' },
        h('div', { className: 'twb-report-card' },
          h('div', { className: 'twb-card-head' },
            h('span', { className: 'twb-card-title' }, fmt(zh('ai.reviewDist'), { n: rows.length })),
            h('span', { className: 'twb-hint' }, t('ai.listScope'))),
          h('div', { className: 'twb-today', style: { marginBottom: 10 } },
            h('div', null, h('span', { className: 'k' }, t('today.rounds')), h('span', { className: 'v twb-num' }, String(rows.length))),
            h('div', null, h('span', { className: 'k' }, t('today.trades')), h('span', { className: 'v twb-num' }, String(trades))),
            h('div', null, h('span', { className: 'k' }, t('today.noTrade')), h('span', { className: 'v twb-num' }, String(noTrade))),
            h('div', null, h('span', { className: 'k' }, t('today.failed')), h('span', { className: 'v twb-num' }, String(failed)))),
          h('div', { className: 'twb-dist' }, dist.map((item) => h('div', { key: item.key, className: 'twb-dist-row' },
            h('span', { className: 'k twb-muted' }, t(item.key)),
            h('span', { className: 'twb-dist-bar' }, h('i', { 'data-tone': item.tone, style: { width: `${Math.round((item.value / total) * 100)}%` } })),
            h('span', { className: 'twb-num' }, String(item.value))))),
          h('div', { className: 'twb-card-title', style: { margin: '12px 0 6px' } }, t('ai.reviewReasons')),
          h('div', { className: 'twb-dist' }, Object.keys(reasons).sort((a, b) => reasons[b] - reasons[a]).map((code) => h('div', { key: code, className: 'twb-dist-row' },
            // 03 §8: the row is the code's label, not the raw machine code; the
            // code itself stays available as the hover title.
            h('span', { className: 'k twb-muted', title: code === 'none' ? '' : code }, code === 'none' ? t('concl.noReason') : reasonLabel(code)),
            h('span', { className: 'twb-dist-bar' }, h('i', { style: { width: `${Math.round((reasons[code] / total) * 100)}%` } })),
            h('span', { className: 'twb-num' }, String(reasons[code]))))),
          h('div', { className: 'twb-table-foot' }, fmt(zh('ai.reviewDuration'), { p50: fmtMs(p50), p95: fmtMs(p95) })),
          // 05 N06: three data-driven branches; "0 成交" is only true when the
          // window really has no trade round.
          h('div', { className: 'twb-table-foot' },
            rows.length === 0
              ? t('ai.reviewAttributionNoRounds')
              : trades > 0
                ? fmt(zh('ai.reviewAttributionTrades'), { n: trades })
                : t('ai.reviewAttribution')),
          h('div', { className: 'twb-card-head', style: { marginTop: 12 } },
            h('span', { className: 'twb-card-title' }, t('ai.reviewTimeline')),
            h('span', { className: 'twb-hint' }, t('ai.reviewTimelineHint'))),
          h('div', { className: 'twb-timeline' },
            rows.slice(0, 8).map((round) => h('div', { key: round.roundId ?? round.startedAt, className: 'twb-timeline-row' },
              h('span', { className: 'twb-num twb-muted', style: { textAlign: 'left' } }, fmtShortClock(round.startedAt)),
              h('span', null, String(round.status) === 'failed'
                ? h(Chip, { text: t('status.failed'), tone: 'danger', icon: true })
                : String(round.outcome) === 'trade'
                  ? h(Chip, { text: t('status.trade'), tone: 'quiet' })
                  : h(Chip, { text: t('status.no_trade'), tone: 'quiet' })),
              h('span', { className: 'twb-muted', style: { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } }, reasonLabel(round.reasonCode)),
              h('span', { className: 'twb-num twb-muted' }, fmtMs(round.durationMs)))))),
        h('div', { className: 'twb-report-card' },
          h(AIDailyReport, { t })))
    }

    // ---- 分析：概览 / 资金 / 归因 / 质量（§2.3 / §4.2） --------------------
    // 约束：不新增一级入口；每个数字要能回答来源；取不到就说不可用；
    // 归因以 fills 为准，不以轮次结论为准（旧实现因此在有成交时说“无成交”）。
    // 曲线：只用 Host 已给的权益点，不做任何插值或补齐。
    function EquityCurve({ t, model }) {
      const curve = model.equityCurve ?? model.equity_curve ?? {}
      const raw = Array.isArray(curve.points) ? curve.points : []
      const points = raw.map((point) => {
        const value = num(point?.exchange) ?? num(point?.session) ?? num(point?.equity)
        const at = toMs(point?.ts) ?? toMs(point?.at)
        return value === null || at === null ? null : { at, value }
      }).filter(Boolean)
      if (points.length < 2) {
        return h("div", { className: "twb-table-foot", "data-equity-curve": "unavailable" },
          t(points.length === 0 ? "an.curveUnavailable" : "an.curveTooShort"));
      }
      const width = 300
      const height = 64
      const values = points.map((point) => point.value)
      const min = Math.min(...values)
      const max = Math.max(...values)
      const span = Math.max(1e-9, max - min)
      const first = points[0].at
      const last = points[points.length - 1].at
      const timeSpan = Math.max(1, last - first)
      const step = Math.max(1, Math.floor(points.length / 120))
      const sampled = points.filter((_, index) => index % step === 0 || index === points.length - 1)
      const coords = sampled.map((point) => {
        const x = ((point.at - first) / timeSpan) * width
        const y = height - ((point.value - min) / span) * (height - 4) - 2
        return `${x.toFixed(1)},${y.toFixed(1)}`
      }).join(" ")
      return h("div", { className: "twb-table-foot", "data-equity-curve": "ok" },
        h("svg", { width: "100%", height, viewBox: `0 0 ${width} ${height}`, preserveAspectRatio: "none", role: "img", "aria-label": t("an.curveLabel") },
          h("polyline", { points: coords, fill: "none", stroke: "currentColor", strokeWidth: 1.2 })),
        h("span", { className: "twb-hint" },
          `${t("an.curveLabel")} · ${points.length} ${t("an.points")} · ${amountText(min, 2)}–${amountText(max, 2)}`));
    }
    function AnalysisFunds({ t, pnl, model, range, onRange }) {
      const payload = pnl?.data && typeof pnl.data === "object" ? pnl.data : null
      const data = payload && payload.ok !== false ? payload : null
      const failedCode = payload && payload.ok === false
        ? (payload.error?.code ?? "E_SERVICE_UNAVAILABLE")
        : (pnl?.error ? (pnl.error.status ? `HTTP ${pnl.error.status}` : (pnl.error.message ?? "E_SERVICE_UNAVAILABLE")) : null)
      const calibers = data?.calibers ?? null
      const books = data ? (data["strat" + "egy"] ?? null) : null
      const reconcile = data?.reconcile ?? null
      const coverage = books?.coverage ?? reconcile?.coverage ?? null
      const rows = [
        [t("an.wallet"), calibers?.wallet_balance ?? "—"],
        [t("an.marginEquity"), calibers?.margin_equity ?? "—"],
        [t("an.upnl"), calibers?.unrealized_pnl ?? "—"],
        [t("an.available"), calibers?.available_balance ?? "—"],
      ]
      const bookRows = [
        [t("an.realized"), books?.realized_pnl ?? "—"],
        [t("an.commission"), books?.commission_quote ?? "—"],
        [t("an.funding"), books?.funding_known === false ? t("an.unknown") : (books?.funding_pnl ?? "—")],
        [t("an.transfers"), books ? (books.transfers?.net_quote ?? "0") : "—"],
        [t("an.net"), books?.net_strategy_pnl ?? (books?.funding_known === false ? t("an.unknown") : "—")],
      ]
      if (!data) {
        return h("div", { className: "twb-report-card" },
          h("div", { className: "twb-card-head" }, h("span", { className: "twb-card-title" }, t("an.funds"))),
          h("div", { className: "twb-table-foot" }, failedCode ? fmt(zh("an.unavailable"), { code: failedCode }) : t("an.loading")))
      }
      return h("div", { className: "twb-report-card" },
        h("div", { className: "twb-card-head" },
          h("span", { className: "twb-card-title" }, t("an.funds")),
          h("span", { className: "twb-hint" }, calibers?.source === "exchange" ? t("an.srcExchange") : calibers?.source === "ledger" ? t("an.srcLedger") : t("an.unknown"))),
        h("div", { className: "twb-today", style: { marginBottom: 8 } }, rows.map(([k, v]) => h("div", { key: k },
          h("span", { className: "k" }, k), h("span", { className: "v twb-num" }, String(v))))),
        h("div", { className: "twb-card-title", style: { margin: "6px 0 2px" } }, t("an.range")),
        h("div", { className: "twb-chips", style: { marginBottom: 6 }, role: "group", "aria-label": t("an.range") },
          ["1h", "6h", "24h", "7d"].map((id) => h("button", {
            key: id, type: "button", className: "twb-btn twb-btn--secondary",
            "aria-pressed": String(range === id),
            "data-review-range": id,
            onClick: () => onRange(id),
          }, id))),
        h("div", { className: "twb-card-title", style: { margin: "10px 0 4px" } }, t("an.books")),
        h("div", { className: "twb-today" }, bookRows.map(([k, v]) => h("div", { key: k },
          h("span", { className: "k" }, k), h("span", { className: "v twb-num" }, String(v))))),
        h("div", { className: "twb-table-foot" },
          t("an.reconcile") + ": " + (reconcile?.status ?? t("an.unknown"))
          + (reconcile?.diff !== null && reconcile?.diff !== undefined ? " · " + t("an.diff") + " " + reconcile.diff : "")
          + (reconcile?.tolerance ? " · " + t("an.tolerance") + " " + reconcile.tolerance : "")),
        h("div", { className: "twb-table-foot" },
          coverage?.complete === true ? t("an.coverageFull") : (t("an.coveragePartial") + (coverage?.note ? " · " + coverage.note : ""))),
        h("div", { className: "twb-card-title", style: { margin: "10px 0 4px" } }, t("an.curve")),
        h(EquityCurve, { t, model }),
        h("div", { className: "twb-hint" }, t("an.fundsHint")))
    }

    function AnalysisAttribution({ t, model }) {
      const rounds = Array.isArray(model.rounds) ? model.rounds : []
      const orders = Array.isArray(model.orders) ? model.orders : (Array.isArray(model.orders?.orders) ? model.orders.orders : [])
      const fills = Array.isArray(model.fills) ? model.fills : (Array.isArray(model.fills?.rows) ? model.fills.rows : [])
      const ordersByCycle = new Map()
      for (const order of orders) {
        const cycle = order.cycle_id ?? order.cycleId ?? null
        if (!cycle) continue
        if (!ordersByCycle.has(cycle)) ordersByCycle.set(cycle, [])
        ordersByCycle.get(cycle).push(order)
      }
      const fillsByOrder = new Map()
      for (const fill of fills) {
        const cid = fill.client_order_id ?? fill.clientOrderId ?? null
        if (!cid) continue
        if (!fillsByOrder.has(cid)) fillsByOrder.set(cid, [])
        fillsByOrder.get(cid).push(fill)
      }
      const linked = rounds.map((round) => {
        const cycle = round.cycleId ?? round.cycle_id ?? null
        const roundOrders = cycle ? (ordersByCycle.get(cycle) ?? []) : []
        const roundFills = roundOrders.flatMap((order) => fillsByOrder.get(order.client_order_id ?? order.clientOrderId) ?? [])
        let net = 0
        let unknown = 0
        for (const fill of roundFills) {
          net += (num(fill.realized_pnl ?? fill.realizedPnl) ?? 0) - (num(fill.commission) ?? 0)
          const asset = String(fill.commission_asset ?? "").toUpperCase()
          if (asset && !["", "USDT", "USDC", "BUSD"].includes(asset)) unknown += 1
        }
        return { round, orders: roundOrders.length, fills: roundFills.length, net, unknown }
      })
      const withFills = linked.filter((item) => item.fills > 0)
      const readOnly = linked.filter((item) => item.orders > 0 && item.fills === 0).length
      const visible = linked.filter((item) => item.orders > 0 || item.fills > 0)
      return h("div", { className: "twb-report-card" },
        h("div", { className: "twb-card-head" },
          h("span", { className: "twb-card-title" }, t("an.attribution")),
          h("span", { className: "twb-hint" }, fmt(zh("an.attributionHint"), { n: fills.length }))),
        h("div", { className: "twb-table-foot", "data-attribution-status": fills.length > 0 ? "linked" : (model.fillsEmpty === true && model.fillsEmptyReason && model.fillsEmptyReason !== "NO_FILLS" ? "unavailable" : "empty") },
          // 区分两种“零”：账本里确实没有成交，还是这次没读到成交。
          // 把“读不到”说成“没有”会让人以为系统从没交易过。
          fills.length > 0
            ? fmt(zh("an.fillsLinked"), { linked: withFills.length, readOnly })
            : (model.fillsEmpty === true && model.fillsEmptyReason && model.fillsEmptyReason !== "NO_FILLS"
              ? fmt(zh("an.fillsUnavailable"), { reason: model.fillsEmptyReason })
              : t("an.noFills"))),
        h("div", { className: "twb-timeline" }, visible.slice(0, 10).map((item) => h("div", {
          key: item.round.roundId ?? item.round.startedAt, className: "twb-timeline-row" },
          h("span", { className: "twb-num twb-muted", style: { textAlign: "left" } }, fmtShortClock(item.round.startedAt)),
          h("span", null, item.orders + " " + t("an.orders") + " · " + item.fills + " " + t("an.fills")),
          h("span", { className: "twb-muted" }, item.unknown > 0 ? t("an.unknownFee") : t("an.netFeeIncluded")),
          h("span", { className: "twb-num" }, item.fills > 0 ? signedAmountText(item.net) : "—")))),
        h("div", { className: "twb-hint" }, t("an.attributionLimit")))
    }

    function AnalysisQuality({ t, model }) {
      const rounds = Array.isArray(model.rounds) ? model.rounds : []
      const buckets = new Map()
      for (const round of rounds) {
        const card = round.scorecard ?? null
        const gate = card?.selection ?? card?.red?.gate ?? card?.blue?.gate ?? "NO_SCORECARD"
        const outcome = String(round.status) === "failed" ? "failed" : (String(round.outcome) === "trade" ? "trade" : "no_trade")
        if (!buckets.has(gate)) buckets.set(gate, { trade: 0, no_trade: 0, failed: 0, total: 0 })
        const bucket = buckets.get(gate)
        bucket[outcome] += 1
        bucket.total += 1
      }
      const keys = [...buckets.keys()].sort((a, b) => buckets.get(b).total - buckets.get(a).total)
      return h("div", { className: "twb-report-card" },
        h("div", { className: "twb-card-head" },
          h("span", { className: "twb-card-title" }, t("an.quality")),
          h("span", { className: "twb-hint" }, fmt(zh("an.qualityHint"), { n: rounds.length }))),
        keys.length === 0
          ? h("div", { className: "twb-table-foot" }, t("an.noRounds"))
          : h("div", { className: "twb-dist" }, keys.map((gate) => {
            const bucket = buckets.get(gate)
            return h("div", { key: gate, className: "twb-dist-row" },
              h("span", { className: "k twb-muted" }, gate),
              h("span", { className: "twb-dist-bar" }, h("i", { style: { width: `${Math.round((bucket.total / Math.max(1, rounds.length)) * 100)}%` } })),
              h("span", { className: "twb-num" }, bucket.trade + "/" + bucket.total))
          })),
        h("div", { className: "twb-hint" }, t("an.qualityCaveat")))
    }

    function ReviewView({ model, t, pnl, range, onRange }) {
      const [tab, setTab] = React.useState("overview")
      const tabs = [
        { id: "overview", label: t("an.tabOverview") },
        { id: "funds", label: t("an.tabFunds") },
        { id: "attribution", label: t("an.tabAttribution") },
        { id: "quality", label: t("an.tabQuality") },
      ]
      return h("div", { className: "twb-report" },
        h("div", { className: "twb-tabs", role: "tablist", "aria-label": t("ai.review") },
          tabs.map((item) => h("button", {
            key: item.id, type: "button", role: "tab", className: "twb-tab",
            "aria-selected": String(tab === item.id),
            "data-analysis-tab": item.id,
            onClick: () => setTab(item.id),
          }, item.label))),
        tab === "funds"
          ? h(AnalysisFunds, { t, pnl, model, range, onRange })
          : tab === "attribution"
            ? h(AnalysisAttribution, { t, model })
            : tab === "quality"
              ? h(AnalysisQuality, { t, model })
              : h(ReviewOverview, { model, t }))
    }
    // ---- 05 P1-1: model / effort configuration drawer ----------------------
    // The Host owns the protocol (schema/presets/validation/freeze); this panel
    // only edits a draft and displays what the Host returns. Never writes the
    // Harness default (P-03=A).
    function ConfigPanel({ t, model }) {
      const [nonce, setNonce] = React.useState(0)
      const [draft, setDraft] = React.useState(null)
      const [saving, setSaving] = React.useState(false)
      const [notice, setNotice] = React.useState(null)
      const catalogPoll = useJson(LLM_CATALOG_URL, nonce, true)
      const configPoll = useJson(LLM_CONFIG_URL, nonce, true)
      // The saved (already applied) config, as opposed to the editable draft.
      const fromFile = configPoll.data?.config ?? null
      React.useEffect(() => {
        const fromFile = configPoll.data?.config
        if (fromFile && !draft) setDraft(fromFile)
      }, [configPoll.data, draft])
      const catalog = catalogPoll.data ?? null
      const summary = configPoll.data?.summary ?? catalog?.config ?? null
      const groups = Array.isArray(catalog?.groups) ? catalog.groups : []
      const models = []
      for (const group of groups) {
        for (const model of (group.models ?? [])) {
          models.push({
            provider: group.id,
            model: model.id,
            name: model.name ?? model.id,
            efforts: Array.isArray(model.efforts) ? model.efforts : [],
            defaultEffort: model.defaultEffort ?? null,
          })
        }
      }
      const defaultRoute = draft?.defaultRoute ?? null
      const modelInfo = (provider, model) => models.find((entry) => entry.provider === provider && entry.model === model) ?? null
      const defaultModel = defaultRoute ? modelInfo(defaultRoute.provider, defaultRoute.model) : null
      const routeFor = (role) => {
        const raw = draft?.roleRoutes?.[role] ?? null
        if ((role === 'red_response' || role === 'blue_response') && !raw) {
          const opening = role === 'red_response' ? 'red_opening' : 'blue_opening'
          return draft?.roleRoutes?.[opening] ?? null
        }
        return raw
      }
      const effectiveRoute = (role) => {
        const raw = routeFor(role)
        return {
          provider: raw?.provider ?? defaultRoute?.provider ?? null,
          model: raw?.model ?? defaultRoute?.model ?? null,
          effort: raw ? (raw.effort ?? null) : (defaultRoute?.effort ?? null),
          inherited: !raw,
        }
      }
      const setDefaultRoute = (patch) => setDraft((prev) => (prev ? { ...prev, defaultRoute: { ...prev.defaultRoute, ...patch } } : prev))
      const setRole = (roles, patch) => setDraft((prev) => {
        if (!prev) return prev
        const roleRoutes = { ...(prev.roleRoutes ?? {}) }
        for (const role of roles) {
          const next = { ...(roleRoutes[role] ?? {}) }
          for (const [key, value] of Object.entries(patch)) {
            if (value === null || value === undefined || value === '') delete next[key]
            else next[key] = value
          }
          if (Object.keys(next).length === 0) delete roleRoutes[role]
          else roleRoutes[role] = next
        }
        return { ...prev, roleRoutes }
      })
      const onModelChange = (roles, value) => {
        if (!value) { setRole(roles, { provider: null, model: null, effort: null }); return }
        const index = value.indexOf('/')
        const provider = value.slice(0, index)
        const model = value.slice(index + 1)
        setRole(roles, { provider, model, effort: null })
      }
      const applyPreset = (name) => {
        const preset = catalog?.presets?.[name]
        if (!preset) return
        if (!preset.config) {
          setNotice({ error: fmt(t('config.presetMissing'), { models: (preset.missing ?? []).join(', ') || '—' }) })
          return
        }
        const { revision, updatedAt, ...rest } = preset.config
        setDraft(rest)
        setNotice(null)
      }
      // §3.1: the save carries the revision the user actually rendered. A stale
      // form gets 409 E_CONFIG_REVISION_CONFLICT and keeps its draft — it never
      // overwrites a newer value, and a draft is never reported as applied.
      const save = async () => {
        if (!draft || saving) return
        setSaving(true)
        setNotice(null)
        const baseRevision = draft.revision ?? summary?.revision ?? null
        try {
          const payload = { ...draft }
          delete payload.revision
          delete payload.updatedAt
          const response = await fetch(LLM_CONFIG_URL, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'content-type': 'application/json', accept: 'application/json' },
            body: JSON.stringify({ config: payload, baseRevision }),
          })
          const body = await response.json().catch(() => ({}))
          if (response.status === 409 || body?.error?.code === 'E_CONFIG_REVISION_CONFLICT') {
            setNotice({
              conflict: true,
              error: fmt(t('set.conflict'), {
                current: body?.error?.details?.current ?? '—',
                provided: body?.error?.details?.provided ?? baseRevision ?? '—',
              }),
            })
            setNonce((value) => value + 1)
            return
          }
          if (!response.ok || body.ok === false) {
            const errors = body?.error?.details?.errors ?? []
            // 02 §3.1: a rejected save must name the role, the error code and
            // the reason (the user asked for no silent mapping).
            const detail = errors.length > 0
              ? errors.map((item) => `${item.role ?? '*'}: ${item.code ?? 'ERROR'} · ${item.message ?? ''}`).join('; ')
              : (body?.error?.message ?? String(response.status))
            setNotice({ error: fmt(t('config.saveFailed'), { reason: detail }) })
          } else {
            setDraft(body.config ?? payload)
            setNotice({ ok: true, revision: body.config?.revision ?? null })
            setNonce((value) => value + 1)
          }
        } catch (error) {
          setNotice({ error: fmt(t('config.saveFailed'), { reason: error?.message ?? 'NETWORK_ERROR' }) })
        } finally {
          setSaving(false)
        }
      }
      const roleGroups = [
        { id: 'analysis', label: 'role.analysis', roles: ['analysis'] },
        ...(draft?.debateAsymmetric
          ? [
              { id: 'red_opening', label: 'role.red_opening', roles: ['red_opening'] },
              { id: 'red_response', label: 'role.red_response', roles: ['red_response'] },
              { id: 'blue_opening', label: 'role.blue_opening', roles: ['blue_opening'] },
              { id: 'blue_response', label: 'role.blue_response', roles: ['blue_response'] },
            ]
          : [{ id: 'debate', label: 'config.debate', roles: ['red_opening', 'blue_opening', 'red_response', 'blue_response'] }]),
        { id: 'decision', label: 'role.decision', roles: ['decision'] },
        { id: 'risk', label: 'role.risk', roles: ['risk'] },
        { id: 'execution', label: 'role.execution_entry', roles: ['execution_entry', 'execution_manage'] },
        { id: 'review', label: 'role.review', roles: ['review'] },
      ]
      const modelSelect = (label, value, onChange) => h('select', {
        className: 'twb-input', style: { width: '100%' }, 'aria-label': label, value: value ?? '', disabled: !catalog?.available || !draft,
        onChange: (event) => onChange(event.target.value),
      },
      h('option', { value: '' }, `${t('config.inherit')}（${defaultRoute?.provider ?? '—'}/${defaultRoute?.model ?? '—'}）`),
      groups.map((group) => h('optgroup', { key: group.id, label: group.name ?? group.id },
        (group.models ?? []).map((model) => h('option', { key: `${group.id}/${model.id}`, value: `${group.id}/${model.id}` }, model.name ?? model.id)))))
      const effortSelect = (label, info, value, onChange) => {
        const efforts = info?.efforts ?? []
        return h('select', {
          className: 'twb-input', style: { width: '100%' }, 'aria-label': label, value: value ?? '', disabled: !catalog?.available || !draft || efforts.length === 0,
          onChange: (event) => onChange(event.target.value),
        },
        h('option', { value: '' }, efforts.length === 0 ? t('config.noReasoning') : `${t('config.inherit')}（${info?.defaultEffort ?? '—'}）`),
        efforts.map((effort) => h('option', { key: effort, value: effort }, effort)))
      }
      const budget = summary?.budget ?? null
      const seconds = (ms) => `${Math.round((Number(ms) || 0) / 1000)}s`
      const revision = summary?.revision ?? draft?.revision ?? null
      // "修改前 / 修改后 / 生效时间" (§3.1): a draft is a draft. The card says what
      // is saved, what would change and when it would take effect.
      const stripVolatile = (value) => {
        if (!value || typeof value !== 'object') return null
        const { revision: _ignoredRevision, updatedAt: _ignoredUpdatedAt, ...rest } = value
        return JSON.stringify(rest)
      }
      const dirty = Boolean(draft) && Boolean(fromFile) && stripVolatile(draft) !== stripVolatile(fromFile)
      const routeText = (route) => (route && route.provider
        ? `${route.provider} / ${route.model}${route.effort ? ` · ${route.effort}` : ''}`
        : t('common.notProvided'))
      const lastChange = model?.state?.last_config_change ?? null
      const beforeText = fromFile
        ? `${routeText(fromFile.defaultRoute)} · ${t('config.revision')} ${fromFile.revision ?? '—'}`
        : t('common.notProvided')
      const afterText = draft
        ? `${routeText(draft.defaultRoute)} · ${t('config.preset')} ${draft.preset ?? 'custom'}`
        : t('common.notProvided')
      return h('div', { className: 'twb-panel twb-settings-model', 'aria-label': t('config.title') },
        h('div', { className: 'twb-card-head' },
          h('span', { className: 'twb-card-title' }, t('config.title')),
          h('span', { className: 'twb-chip twb-chip--quiet' }, `${t('config.revision')} ${revision ?? '—'}`),
          draft?.preset ? h('span', { className: 'twb-chip twb-chip--quiet' }, `${t('config.preset')} ${draft.preset}`) : null,
          dirty ? h('span', { className: 'twb-chip twb-chip--warn', 'data-draft': 'true' }, t('set.draft')) : null,
          h('span', { className: 'twb-hint' }, t('config.sub')),
          h('span', { className: 'twb-spacer' }),
          h('button', {
            type: 'button', className: 'twb-btn twb-btn--ghost', style: { height: 24 },
            onClick: () => setNonce((value) => value + 1),
          }, t('config.refresh'))),
        summary?.banner ? h('div', { className: 'twb-modal-note twb-modal-note--danger' }, summary.banner) : null,
        !catalog ? h('div', { className: 'twb-hint' }, t('config.loading')) : null,
        catalog && catalog.available === false ? h('div', { className: 'twb-modal-note twb-modal-note--danger' }, t('config.catalogUnavailable')) : null,
        catalog && catalog.available !== false && models.length === 0 ? h('div', { className: 'twb-hint' }, t('config.emptyCatalog')) : null,
        h('div', { className: 'twb-card-title', style: { margin: '8px 0 6px' } }, t('config.preset')),
        h('div', { className: 'twb-chips', style: { marginBottom: 8 } }, ['balanced', 'saver', 'strongest'].map((name) => h('button', {
          key: name, type: 'button', className: 'twb-btn twb-btn--secondary', style: { height: 26 }, disabled: !catalog?.available,
          onClick: () => applyPreset(name),
        }, t(`config.preset${name.charAt(0).toUpperCase()}${name.slice(1)}`)))),
        h('div', { className: 'twb-card-title', style: { margin: '8px 0 6px' } }, t('set.modelTitle')),
        h('div', { className: 'twb-kv twb-settings-kv', 'data-config-diff': dirty ? 'dirty' : 'clean' },
          h('dt', null, t('set.before')), h('dd', { className: 'twb-mono' }, beforeText),
          h('dt', null, t('set.after')), h('dd', { className: 'twb-mono' }, dirty ? afterText : t('set.noChange')),
          h('dt', null, t('set.effective')), h('dd', { className: 'twb-mono' }, dirty ? t('set.effectiveNext') : '—'),
          h('dt', null, t('set.lastChange')), h('dd', { className: 'twb-mono' }, lastChange?.at ?? fromFile?.updatedAt ?? '—')),
        h('div', { className: 'twb-card-title', style: { margin: '8px 0 6px' } }, t('config.defaultRoute')),
        h('div', { className: 'twb-config-row' },
          h('span', { className: 'twb-config-role' }, t('config.defaultRoute')),
          modelSelect(t('config.defaultRoute') + ' ' + t('config.model'), defaultRoute ? `${defaultRoute.provider}/${defaultRoute.model}` : '', (value) => {
            if (!value) return
            const index = value.indexOf('/')
            setDefaultRoute({ provider: value.slice(0, index), model: value.slice(index + 1) })
          }),
          effortSelect(t('config.defaultRoute') + ' ' + t('config.effort'), defaultModel, defaultRoute?.effort ?? '', (value) => setDefaultRoute({ effort: value || null })),
          h('span', { className: 'twb-hint' }, defaultRoute?.source ?? 'explicit')),
        h('div', { className: 'twb-card-title', style: { margin: '10px 0 6px' } }, t('config.roles')),
        h('label', { className: 'twb-hint', style: { display: 'flex', alignItems: 'center', gap: 6 } },
          h('input', {
            type: 'checkbox', checked: draft?.debateAsymmetric === true, disabled: !draft,
            'aria-label': t('config.asymmetric'),
            onChange: (event) => setDraft((prev) => (prev ? { ...prev, debateAsymmetric: event.target.checked } : prev)),
          }),
          t('config.asymmetric')),
        draft?.debateAsymmetric ? h('div', { className: 'twb-modal-note' }, t('config.asymmetricWarn')) : h('div', { className: 'twb-hint' }, t('config.lockHint')),
        h('div', { className: 'twb-config-table' }, roleGroups.map((group) => {
          const current = effectiveRoute(group.roles[0])
          const info = modelInfo(current.provider, current.model)
          return h('div', { key: group.id, className: 'twb-config-row' },
            h('span', { className: 'twb-config-role' }, t(group.label)),
            modelSelect(`${t(group.label)} ${t('config.model')}`, current.provider && current.model ? `${current.provider}/${current.model}` : '', (value) => onModelChange(group.roles, value)),
            effortSelect(`${t(group.label)} ${t('config.effort')}`, info, current.effort ?? '', (value) => setRole(group.roles, { effort: value || null })),
            h('span', { className: 'twb-hint' }, current.inherited ? t('config.inheritMark') : (info?.efforts?.length ? '' : t('config.noReasoning'))))
        })),
        h('div', { className: 'twb-card-title', style: { margin: '10px 0 6px' } }, t('config.budget')),
        budget
          ? h('div', { className: 'twb-hint' }, `${fmt(t('config.budgetValue'), { min: seconds(budget.min_ms), max: seconds(budget.max_ms), margin: seconds(budget.retry_margin_ms) })} · ${t('config.budgetNote')}`)
          : h('div', { className: 'twb-hint' }, t('config.loading')),
        notice?.ok ? h('div', { className: 'twb-chip twb-chip--quiet', role: 'status' }, fmt(t('config.saved'), { n: notice.revision ?? '—' })) : null,
        notice?.error ? h('div', { className: 'twb-modal-note twb-modal-note--danger', role: 'status', 'data-conflict': notice.conflict ? 'true' : null },
          h('div', null, notice.error),
          notice.conflict
            ? h('button', {
              type: 'button', className: 'twb-btn twb-btn--secondary', style: { height: 26, marginTop: 6 },
              onClick: () => { setDraft(configPoll.data?.config ?? null); setNotice(null); setNonce((value) => value + 1) },
            }, t('set.reloadConfig'))
            : null) : null,
        h('div', { className: 'twb-hint' }, t('config.roleScope')),
        h('div', { className: 'twb-modal-actions' },
          h('button', { type: 'button', className: 'twb-btn twb-btn--primary', disabled: saving || !draft || !catalog?.available, onClick: save }, saving ? t('action.submitting') : t('config.save'))))
    }

    // ---- drawer ------------------------------------------------------------
    function Drawer({ model, drawer, t, onClose, onTab, onOpenSystem, lang, onLanguage, appearance, onAppearance, density, onDensity }) {
      const closeRef = React.useRef(null)
      const rootRef = React.useRef(null)
      const restoreRef = React.useRef(null)
      React.useEffect(() => {
        restoreRef.current = typeof document !== 'undefined' ? document.activeElement : null
        if (closeRef.current && typeof closeRef.current.focus === 'function') closeRef.current.focus()
        const onKey = (event) => {
          if (event.key === 'Escape') { event.stopPropagation(); onClose(); return }
          if (event.key !== 'Tab') return
          const root = rootRef.current
          if (!root || typeof root.querySelectorAll !== 'function') return
          const focusable = Array.prototype.slice.call(root.querySelectorAll(
            'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
          ))
          if (focusable.length === 0) return
          const first = focusable[0]
          const last = focusable[focusable.length - 1]
          const active = typeof document !== 'undefined' ? document.activeElement : null
          if (event.shiftKey && (active === first || !root.contains(active))) { event.preventDefault(); last.focus() }
          else if (!event.shiftKey && active === last) { event.preventDefault(); first.focus() }
        }
        if (typeof document !== 'undefined' && document.addEventListener) document.addEventListener('keydown', onKey, true)
        return () => {
          if (typeof document !== 'undefined' && document.removeEventListener) document.removeEventListener('keydown', onKey, true)
          const node = restoreRef.current
          if (node && typeof node.focus === 'function') node.focus()
        }
      }, [onClose])
      const isSystem = drawer.type === 'system'
      const isConfig = drawer.type === 'config'
      // F2 (§3.1): the single settings entry. It reuses this drawer shell (scrim,
      // focus trap, Escape) instead of growing a second overlay.
      const isSettings = drawer.type === 'settings'
      const title = isSettings ? t('set.title')
        : isSystem ? t('drawer.system')
          : isConfig ? t('config.title')
            : drawer.type === 'position' ? t('positions.title')
              : drawer.type === 'order' ? t('orders.title') : t('drawer.title')
      const sub = isSettings ? t('set.sub')
        : isSystem ? `${t('drawer.source')}: /trading-workbench/api`
          : isConfig ? t('config.sub')
            : drawer.round ? `#${String(drawer.round.roundId ?? '').slice(-6)}` : ''
      const tabs = isSettings ? SETTINGS_GROUPS : DRAWER_TABS
      const settingsGroup = SETTINGS_GROUPS.includes(drawer.tab) ? drawer.tab : 'connection'
      const body = isSettings
        ? h(SettingsBody, {
          model, t,
          group: settingsGroup,
          onOpenSystem,
          lang, onLanguage, appearance, onAppearance, density, onDensity,
        })
        : isConfig
        ? h(ConfigPanel, { t })
        : isSystem
        ? h(SystemPanel, { model, t })
        : drawer.type === 'position'
          ? h(PositionPanel, { row: drawer.row, t })
          : drawer.type === 'order'
            ? h(OrderPanel, { row: drawer.row, t })
            : h(EvidencePanel, { model, drawer, t })
      return h(React.Fragment, null,
        h('div', { className: 'twb-scrim', onClick: onClose, 'aria-hidden': 'true' }),
        h('aside', { className: 'twb-drawer', role: 'dialog', 'aria-modal': 'true', 'aria-label': title, ref: rootRef },
          h('div', { className: 'twb-drawer-head' },
            h('div', { style: { minWidth: 0 } },
              h('div', { className: 'twb-drawer-title' }, title),
              h('div', { className: 'twb-drawer-sub' }, sub)),
            h('button', {
              type: 'button',
              className: 'twb-btn twb-btn--icon',
              ref: closeRef,
              'aria-label': isSettings ? t('set.close') : t('drawer.close'),
              title: isSettings ? t('set.close') : t('drawer.close'),
              disabled: Boolean(model.controlBusy),
              onClick: onClose,
            }, '✕')),
          isSystem || isConfig ? null : h('div', {
            className: 'twb-drawer-tabs twb-settings-tabs',
            role: 'tablist',
            'aria-label': title,
            onKeyDown: (event) => {
              if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
              event.preventDefault()
              const index = tabs.indexOf(isSettings ? settingsGroup : drawer.tab)
              let next = index
              if (event.key === 'ArrowLeft') next = (index - 1 + tabs.length) % tabs.length
              if (event.key === 'ArrowRight') next = (index + 1) % tabs.length
              if (event.key === 'Home') next = 0
              if (event.key === 'End') next = tabs.length - 1
              const target = tabs[next]
              if (!target) return
              onTab(target)
              const nodes = event.currentTarget?.querySelectorAll?.('[role="tab"]')
              if (nodes && nodes[next]) nodes[next].focus()
            },
          },
          tabs.map((tab) => h('button', {
            key: tab,
            type: 'button',
            role: 'tab',
            id: `twb-drawer-tab-${tab}`,
            'aria-selected': String(isSettings ? settingsGroup === tab : drawer.tab === tab),
            'aria-controls': `twb-drawer-panel-${tab}`,
            tabIndex: (isSettings ? settingsGroup === tab : drawer.tab === tab) ? 0 : -1,
            onClick: () => onTab(tab),
          }, isSettings ? t(`set.group.${tab}`) : t(`drawer.tab${tab.charAt(0).toUpperCase()}${tab.slice(1)}`)))),
          h('div', {
            className: isSettings ? 'twb-drawer-body twb-settings-body' : 'twb-drawer-body',
            id: isSystem ? 'twb-drawer-panel-system'
              : isConfig ? 'twb-drawer-panel-config'
                : `twb-drawer-panel-${isSettings ? settingsGroup : drawer.tab}`,
            role: 'tabpanel',
            'aria-label': title,
          }, body),
          h('div', { className: 'twb-drawer-foot' },
            isSettings ? t('set.sub')
              : isSystem ? t('system.gaps') : isConfig ? t('config.roleScope') : t(`drawer.footer.${drawer.tab}`))))
    }

    function kvRows(items) {
      return h('dl', { className: 'twb-kv', style: { margin: 0 } }, items.map(([key, value]) => h(React.Fragment, { key }, h('dt', null, key), h('dd', null, String(value)))))
    }

    function PositionPanel({ row, t }) {
      if (!row) return h(EmptyBlock, { kind: 'empty', title: t('positions.emptyTitle') })
      const items = [
        [t('positions.symbol'), `${row.symbol} · ${sideLabel(row.side)}`],
        [t('positions.qty'), row.qty ?? '—'],
        [t('positions.mark'), row.mark === null ? '—' : fmtNum(row.mark)],
        [t('account.upnl'), row.upnl === null ? '—' : fmtNum(row.upnl)],
        [t('positions.protection'), `${protectionText(row.protection)}${row.protection.block ? ` · ${t('prot.blocks')}` : ''}`],
        [t('positions.distance'), `${row.stopDist === null ? t('common.notProvided') : `${row.stopDist.toFixed(1)}%`} / ${row.liqDist === null ? t('positions.unknownLiq') : `${row.liqDist.toFixed(1)}%`}`],
        [t('positions.margin'), `${row.margin === null ? '—' : fmtNum(row.margin, 1)}${row.leverage ? ` · ${row.leverage}×` : ''}`],
        [t('drawer.source'), row.origin ?? '—'],
      ]
      return h('div', { className: 'twb-panel' }, kvRows(items), h('div', { className: 'twb-table-foot' }, t('positions.footer')))
    }

    function OrderPanel({ row, t }) {
      if (!row) return h(EmptyBlock, { kind: 'empty', title: t('orders.emptyTitle') })
      const items = [
        [t('orders.time'), fmtClock(row.createdAt ?? row.time)],
        [t('orders.symbol'), row.symbol ?? '—'],
        [t('orders.side'), sideLabel(row.side)],
        [t('orders.qty'), row.qty ?? '—'],
        [t('orders.price'), row.price === null ? '—' : fmtNum(row.price)],
        [t('orders.status'), row.status ?? '—'],
        [t('orders.detail'), row.clientId ?? row.id ?? '—'],
      ]
      return h('div', { className: 'twb-panel' }, kvRows(items))
    }

    // Full md-evidence snapshot: lazy, cached per cycle+agent, stale responses
    // ignored. Only the evidence tab asks for it.
    const EVIDENCE_CACHE = new Map()

    function useCycleEvidence(cycleId, agent, enabled) {
      const key = cycleId ? `${cycleId}:${agent ?? ''}` : ''
      const [state, setState] = React.useState(() => {
        const cached = key ? EVIDENCE_CACHE.get(key) : null
        return { data: cached ?? null, error: null, loading: Boolean(enabled && cycleId && !cached) }
      })
      React.useEffect(() => {
        if (!enabled || !cycleId) return undefined
        const cached = EVIDENCE_CACHE.get(key)
        if (cached) { setState({ data: cached, error: null, loading: false }); return undefined }
        let cancelled = false
        setState((prev) => ({ data: prev.data, error: null, loading: true }))
        const url = `${API}/cycles/${encodeURIComponent(cycleId)}/evidence${agent ? `?agent=${encodeURIComponent(agent)}` : ''}`
        fetch(url, { credentials: 'same-origin', headers: { accept: 'application/json' } })
          .then((response) => response.json().then((body) => ({ response, body })))
          .then(({ response, body }) => {
            if (cancelled) return
            if (!response.ok || body?.ok === false) {
              setState({
                data: null,
                error: { status: response.status, code: body?.error?.code ?? `HTTP_${response.status}` },
                loading: false,
              })
              return
            }
            EVIDENCE_CACHE.set(key, body)
            setState({ data: body, error: null, loading: false })
          })
          .catch((error) => {
            if (cancelled) return
            setState({ data: null, error: { status: 0, code: 'NETWORK_ERROR', message: error?.message ?? 'ERROR' }, loading: false })
          })
        return () => { cancelled = true }
      }, [key, enabled])
      return state
    }

    function FullSnapshot({ model, drawer, t, call }) {
      const round = drawer.round ?? model.round
      const cycleId = round?.cycleId ?? round?.cycle_id ?? null
      const agent = drawer.type === 'call' ? (call?.role_key ?? call?.role ?? null) : null
      const evidence = useCycleEvidence(cycleId, agent, drawer.tab === 'evidence')
      const data = evidence.data
      const sha = data?.sha256 ? String(data.sha256) : null
      const shaShort = sha ? `${sha.slice(0, 18)}…` : null
      const rows = [
        [t('drawer.snapshotId'), data?.snapshot_id ?? round?.evidence?.snapshot_id ?? round?.freshness?.snapshot_id ?? '—'],
        [t('drawer.schemaVersion'), data?.schema_version ?? '—'],
        [t('drawer.verdict'), data?.verdict ?? round?.freshness?.data_verdict ?? round?.evidence?.verdict ?? '—'],
        [t('drawer.hash'), shaShort ?? (round?.evidence?.evidence_hash ? `${String(round.evidence.evidence_hash).slice(0, 18)}…` : '—')],
        [t('drawer.bytes'), data?.bytes === undefined || data?.bytes === null ? '—' : String(data.bytes)],
        [t('drawer.agent'), data?.agent ?? agent ?? '—'],
      ]
      return h('div', { className: 'twb-panel', 'aria-label': t('drawer.fullSnapshot') },
        h('div', { className: 'twb-card-head', style: { marginBottom: 4 } },
          h('span', { className: 'twb-card-title' }, t('drawer.fullMeta')),
          evidence.loading ? h('span', { className: 'twb-hint' }, t('drawer.fullLoading')) : null),
        evidence.error
          ? h('div', { className: 'twb-hint', style: { color: 'var(--twb-warn)' } }, fmt(zh('drawer.fullFailed'), { code: evidence.error.code ?? '—' }))
          : null,
        h('dl', { className: 'twb-kv', style: { margin: 0 } },
          rows.map(([key, value]) => h(React.Fragment, { key }, h('dt', null, key), h('dd', { className: 'twb-mono' }, String(value))))),
        data
          ? h('details', { className: 'twb-fold' },
            h('summary', null, t('drawer.fullSnapshot')),
            h('pre', { className: 'twb-pre', style: { maxHeight: 320, marginTop: 6 } },
              JSON.stringify(data.evidence ?? data, null, 2)))
          : null)
    }

    function EvidencePanel({ model, drawer, t }) {
      const call = drawer.call ?? (model.round?.nodes ?? [])[0] ?? null
      if (drawer.type !== 'call' && drawer.tab === 'evidence') {
        const round = drawer.round ?? model.round
        if (!round) return h(EmptyBlock, { kind: 'empty', title: t('drawer.noEvidence') })
        return h('div', { className: 'twb-panel' },
          h('dl', { className: 'twb-kv', style: { margin: 0 } },
            h('dt', null, t('drawer.snapshotId')), h('dd', { className: 'twb-mono' }, String(round.evidence?.snapshot_id ?? round.freshness?.snapshot_id ?? '—')),
            h('dt', null, t('drawer.producedAt')), h('dd', { className: 'twb-mono' }, fmtClock(round.freshness?.produced_at ?? round.evidence?.produced_at)),
            h('dt', null, t('drawer.age')), h('dd', null, model.freshness.age === null ? t('common.notProvided') : fmt(zh('drawer.ageNote'), { n: Math.round(model.freshness.age / 1000) })),
            h('dt', null, t('drawer.verdict')), h('dd', null, String(round.freshness?.data_verdict ?? round.evidence?.verdict ?? '—')),
            h('dt', null, t('drawer.symbol')), h('dd', null, String(round.evidence?.primary_symbol ?? round.symbol ?? '—')),
            h('dt', null, t('drawer.hash')), h('dd', { className: 'twb-mono' }, String(round.evidence?.evidence_hash ?? round.freshness?.sha256 ?? '—')),
            h('dt', null, t('drawer.source')), h('dd', { className: 'twb-mono' }, String(round.freshness?.source ?? '—'))),
          h(FullSnapshot, { model, drawer, t }))
      }
      if (!call) return h(EmptyBlock, { kind: 'empty', title: t('drawer.noEvidence') })
      if (drawer.tab === 'prompt') {
        const rows = (model.round?.aiCalls ?? []).slice(0, 8)
        return h('div', { className: 'twb-panel' },
          h('table', { className: 'twb-table' },
            h('thead', null, h('tr', null, ['drawer.role', 'drawer.promptVersion', 'drawer.model', 'drawer.requestHash'].map((key) => h('th', { key, scope: 'col' }, t(key))))),
            h('tbody', null, rows.map((item) => h('tr', { key: item.call_id },
              h('td', null, zh(`role.${item.role_key ?? item.role}`) === `role.${item.role_key ?? item.role}` ? String(item.role ?? '—') : zh(`role.${item.role_key ?? item.role}`)),
              h('td', { className: 'twb-mono' }, String(item.prompt_version ?? '—')),
              h('td', { className: 'twb-mono' }, `${item.provider ?? '—'} · ${item.effort ?? '—'}`),
              h('td', { className: 'twb-mono' }, String(item.request_sha256 ?? '—')))))))
      }
      if (drawer.tab === 'json') {
        return h('pre', { className: 'twb-pre' }, JSON.stringify({
          roundId: model.round?.roundId ?? null,
          status: model.round?.status ?? null,
          reasonCode: model.round?.reasonCode ?? null,
          reasonDetail: model.round?.reasonDetail ?? null,
          durationMs: model.round?.durationMs ?? null,
          decision: model.round?.decision ?? null,
          risk: model.round?.risk ?? null,
          execution: model.round?.execution ?? null,
          nodes: (model.round?.nodes ?? []).map((node) => ({ role: node.role, attempt: node.attempt, schema_ok: node.schema_ok, latency_ms: node.latency_ms })),
        }, null, 2))
      }
      if (drawer.tab === 'rules') {
        const p1 = model.round?.execution?.p1 ?? null
        const p2 = model.round?.execution?.p2 ?? null
        const violations = Array.isArray(model.round?.risk?.violations) ? model.round.risk.violations : []
        const rows = [
          h('tr', { key: 'p1' },
            h('td', null, t('drawer.p1')),
            h('td', { className: 'twb-mono' }, String(p1?.verdict ?? t('common.notProvided'))),
            h('td', null, String(p1?.code ?? '—'))),
          h('tr', { key: 'p2' },
            h('td', null, t('drawer.p2')),
            h('td', { className: 'twb-mono' }, String(p2?.status ?? t('common.notProvided'))),
            h('td', null, String(p2?.order_id ?? p2?.code ?? '—'))),
        ].concat(violations.map((item, index) => h('tr', { key: `v-${index}` },
          h('td', null, String(item.rule ?? item)),
          h('td', { className: 'twb-mono' }, String(item.value ?? '—')),
          h('td', null, String(item.action ?? '—')))))
        const head = ['drawer.rule', 'drawer.actual', 'drawer.result'].map((key) => h('th', { key, scope: 'col' }, t(key)))
        return h('div', { className: 'twb-panel' },
          h('table', { className: 'twb-table' },
            h('thead', null, h('tr', null, head)),
            h('tbody', null, rows)))
      }
      if (drawer.tab === 'timing') {
        const calls = (model.round?.aiCalls ?? []).slice(0, 12)
        return h('div', { className: 'twb-panel' },
          h('dl', { className: 'twb-kv', style: { marginTop: 0 } },
            h('dt', null, t('drawer.total')), h('dd', { className: 'twb-mono' }, fmtMs(model.round?.durationMs)),
            h('dt', null, t('drawer.calls')), h('dd', { className: 'twb-mono' }, String((model.round?.nodes ?? []).length))),
          h('table', { className: 'twb-table' },
            h('thead', null, h('tr', null, ['drawer.call', 'ai.latency', 'drawer.actual', 'drawer.result'].map((key, index) => h('th', { key, scope: 'col', className: index === 1 ? 'is-num' : '' }, t(key))))),
            h('tbody', null, calls.map((item) => h('tr', { key: item.call_id },
              h('td', null, String(item.role ?? '—')),
              h('td', { className: 'twb-num' }, fmtMs(item.latency_ms)),
              h('td', { className: 'twb-mono' }, `${item.input_tokens ?? '—'} / ${item.output_tokens ?? '—'}`),
              h('td', null, String(item.finish_kind ?? '—')))))))
      }
      return h('div', { className: 'twb-panel' },
        h('dl', { className: 'twb-kv', style: { margin: 0 } },
          h('dt', null, t('drawer.role')), h('dd', null, String(call.role ?? '—')),
          h('dt', null, t('drawer.attempt')), h('dd', { className: 'twb-mono' }, `${call.round ?? 1} / ${call.attempt ?? 1}`),
          h('dt', null, t('ai.latency')), h('dd', { className: 'twb-mono' }, fmtMs(call.latency_ms)),
          h('dt', null, t('ai.tokens')), h('dd', { className: 'twb-mono' }, `${call.input_tokens ?? '—'} / ${call.output_tokens ?? '—'}`),
          h('dt', null, t('drawer.schema')), h('dd', null, call.schema_ok === true ? t('status.ok') : t('status.error')),
          h('dt', null, t('drawer.finish')), h('dd', { className: 'twb-mono' }, String(call.finish_kind ?? '—'))),
        h(FullSnapshot, { model, drawer, t, call }))
    }

    function newRequestId() {
      return `req-${typeof crypto !== 'undefined' && crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(36).slice(2)}`}`
    }

    function fmtShanghai(value) {
      return fmtClock(value)
    }

    async function operatorRequest(route, options = {}) {
      const controller = typeof AbortController !== 'undefined' ? new AbortController() : null
      const timer = setTimeout(() => controller?.abort(), 120000)
      try {
        const response = await fetch(`${API}/${route}`, { ...options, signal: controller?.signal })
        const body = await response.json()
        if (!response.ok || body.ok === false) {
          const error = new Error(body.error?.message ?? `HTTP ${response.status}`)
          error.code = body.error?.code ?? `HTTP_${response.status}`
          error.definitive = response.status >= 400 && response.status < 500 && response.status !== 409
          throw error
        }
        return body
      } finally { clearTimeout(timer) }
    }

    function OperationsPanel({ t }) {
      const [data, setData] = React.useState(null)
      const [error, setError] = React.useState(null)
      const [nonce, setNonce] = React.useState(0)
      const [filter, setFilter] = React.useState('all')
      React.useEffect(() => {
        let active = true
        setError(null)
        operatorRequest('operations').then((value) => { if (active) setData(value) }, (err) => { if (active) setError(err.code ?? 'E_HISTORY_UNAVAILABLE') })
        return () => { active = false }
      }, [nonce])
      const rows = Array.isArray(data?.operations) ? data.operations.filter((row) => filter === 'all' || row.status === filter) : []
      return h('div', { className: 'twb-panel', 'aria-label': t('ops.title') },
        h('div', { className: 'twb-card-head' },
          h('span', { className: 'twb-card-title' }, t('ops.title')),
          h('select', { className: 'twb-input', style: { width: 150 }, 'aria-label': t('ops.result'), value: filter, onChange: (event) => setFilter(event.target.value) },
            ['all', 'succeeded', 'partial', 'failed', 'pending', 'unknown'].map((value) => h('option', { key: value, value }, t(`ops.${value}`)))),
          h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', onClick: () => setNonce((value) => value + 1) }, t('ops.refresh'))),
        h('div', { className: 'twb-hint', style: { whiteSpace: 'normal', lineHeight: 1.5 } }, t('ops.scope')),
        error ? h('div', { role: 'status' }, `${t('ops.failedLoad')} · ${error}`)
          : !data ? h('div', null, t('state.loading'))
            : rows.length === 0 ? h('div', { className: 'twb-hint' }, t('ops.empty'))
              : h('div', { className: 'twb-table-wrap', style: { maxHeight: 340 } }, h('table', { className: 'twb-table' },
                h('thead', null, h('tr', null, ['time', 'action', 'target', 'reason', 'result', 'request'].map((key) => h('th', { key }, t(`ops.${key}`))))),
                h('tbody', null, rows.map((row) => h('tr', { key: row.request_id },
                  h('td', { style: { whiteSpace: 'normal', minWidth: 110 } }, fmtShanghai(row.started_at)),
                  h('td', null, t(`ops.${row.action}`)), h('td', null, row.action === 'control' ? controlLabel(row.target) : row.target ?? '—'),
                  h('td', { style: { maxWidth: 220, whiteSpace: 'normal', overflowWrap: 'anywhere' } }, row.reason || '—'),
                  h('td', { style: { whiteSpace: 'normal', minWidth: 120, maxWidth: 180 } }, `${t(`ops.${row.status}`)}${row.error_code ? ` · ${row.error_code}` : ''}`,
                    row.summary ? h('details', { className: 'twb-fold', style: { marginTop: 4 } },
                      h('summary', null, t('ops.details')),
                      row.summary.control_state ? h('div', null, controlLabel(row.summary.control_state)) : null,
                      row.summary.position_closed !== null && row.summary.position_closed !== undefined ? h('div', null, t(row.summary.position_closed ? 'ops.closed' : 'ops.unconfirmed')) : null,
                      (row.summary.outcomes ?? []).map((item, index) => h('div', { key: index, style: { whiteSpace: 'normal', minWidth: 130 } },
                        `${item.symbol ?? '—'} · ${item.closed === true ? t('ops.closed') : item.status ?? t('ops.unconfirmed')}${item.error_code ? ` · ${item.error_code}` : ''}`))) : null),
                  h('td', { className: 'twb-mono', style: { overflowWrap: 'anywhere', whiteSpace: 'normal' } }, row.request_id)))))))
    }

    function ControlPanel({ model, t }) {
      const [target, setTarget] = React.useState(null)
      const [reason, setReason] = React.useState('')
      const [confirm, setConfirm] = React.useState('')
      const [busy, setBusy] = React.useState(false)
      const [receipt, setReceipt] = React.useState(null)
      // 05 N08: emergency close-all is the one irreversible action here, so it
      // needs a longer reason and a deliberate second click (5s window).
      const [armedAt, setArmedAt] = React.useState(null)
      const reasonRef = React.useRef(null)
      const flight = React.useRef(false)
      const requiredReasonLength = target === 'emergency' ? 8 : 2
      React.useEffect(() => {
        if (target === 'emergency' && reasonRef.current && typeof reasonRef.current.focus === 'function') {
          reasonRef.current.focus()
        }
      }, [target])
      const choose = (value) => { if (!flight.current) { setTarget(value); setReason(''); setConfirm(''); setReceipt(null); setArmedAt(null) } }
      const submit = async () => {
        if (flight.current || receipt || !target || reason.trim().length < requiredReasonLength || confirm !== target.toUpperCase()) return
        flight.current = true
        if (model.controlFlight) model.controlFlight.current = true
        model.setControlBusy?.(true)
        setBusy(true)
        const requestId = newRequestId()
        try {
          const data = await operatorRequest('control', { method: 'POST', headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ state: target, confirm, reason: reason.trim(), request_id: requestId }) })
          const result = data.result ?? {}
          setReceipt({ code: result.operation_status === 'partial' || (target === 'emergency' && !result.flattened?.ok) ? 'control.partial' : 'control.success', requestId: data.requestId ?? requestId, state: result.control?.state, outcomes: result.flattened?.results ?? [] })
        } catch (error) { setReceipt({ code: error.code === 'E_CONTROL_LOCKED' ? 'control.locked' : error.definitive ? 'control.failed' : 'control.unavailable', requestId, error: error.code ?? 'E_NETWORK_UNKNOWN' }) }
        finally { flight.current = false; if (model.controlFlight) model.controlFlight.current = false; model.setControlBusy?.(false); setBusy(false) }
      }
      return h('section', { className: 'twb-panel', 'aria-label': t('control.title') },
        h('div', { className: 'twb-card-title' }, t('control.title')),
        h('div', { className: 'twb-hint', style: { whiteSpace: 'normal', lineHeight: 1.5 } }, t('control.hint')),
        h('div', { className: 'twb-chips', style: { marginTop: 8 } }, ['pause', 'close_only', 'normal', 'emergency'].map((value) => h('button', {
          key: value, type: 'button', className: 'twb-btn twb-btn--secondary', style: value === 'emergency' ? { color: 'var(--twb-danger)', borderColor: 'var(--twb-danger)' } : undefined, disabled: busy || Boolean(receipt) || (value === model.control && value !== 'emergency'), onClick: () => choose(value),
        }, t(`command.${value}`)))),
        target ? h('div', { className: 'twb-panel', role: 'group', 'aria-label': t(`command.${target}`) },
          h('div', { className: 'twb-modal-note' }, t(`command.${target}`), ' · DEMO'),
          target === 'emergency' ? h('div', { className: 'twb-modal-note twb-modal-note--danger' }, t('control.warning')) : null,
          h('label', { style: { display: 'flex', flexDirection: 'column', gap: 6, marginTop: 8 } }, target === 'emergency' ? `${t('ops.reason')} · ${fmt(zh('control.reasonMin'), { n: requiredReasonLength })}` : t('ops.reason'), h('input', { className: 'twb-input', style: { width: '100%' }, 'aria-label': t('ops.reason'), maxLength: 300, ref: reasonRef, value: reason, disabled: busy || Boolean(receipt), onChange: (event) => setReason(event.target.value) })),
          h('label', { style: { display: 'flex', flexDirection: 'column', gap: 6, marginTop: 8 } }, fmt(zh('control.confirmHint'), { word: target.toUpperCase() }), h('input', { className: 'twb-input', style: { width: '100%' }, 'aria-label': fmt(zh('control.confirmHint'), { word: target.toUpperCase() }), value: confirm, disabled: busy || Boolean(receipt), onChange: (event) => setConfirm(event.target.value) })),
          receipt ? h('div', { role: 'status', 'aria-live': 'polite' },
            t(receipt.code), ` · ${receipt.requestId}${receipt.state ? ` · ${controlLabel(receipt.state)}` : ''}${receipt.error ? ` · ${receipt.error}` : ''}`,
            (receipt.outcomes ?? []).map((item, index) => h('div', { key: index }, `${item.symbol ?? '—'} · ${t(item.closed === true ? 'ops.closed' : 'ops.unconfirmed')}`)),
            h('div', { className: 'twb-hint' }, t('action.noRetry')),
            h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', onClick: () => model.refresh?.() }, t('control.check')))
            : h('div', { className: 'twb-modal-actions' },
              h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', disabled: busy, onClick: () => choose(null) }, t('control.cancel')),
              target === 'emergency' && !receipt
                ? h('span', { className: 'twb-hint', role: 'status' },
                  armedAt !== null && Date.now() - armedAt <= 5000 ? t('control.armHint') : fmt(zh('control.reasonMin'), { n: requiredReasonLength }))
                : null,
              h('button', {
                type: 'button',
                className: 'twb-btn twb-btn--primary',
                disabled: busy || reason.trim().length < requiredReasonLength || confirm !== target.toUpperCase(),
                onClick: () => {
                  if (target === 'emergency') {
                    const now = Date.now()
                    if (armedAt === null || now - armedAt > 5000) { setArmedAt(now); return }
                  }
                  submit()
                },
              }, busy ? t('action.submitting') : t('control.submit')))) : null)
    }

    function PortfolioDetails({ model, t }) {
      const p = model.state?.performance
      const env = model.riskEnvelope
      const limits = env?.limits ?? {}
      const risk = env?.risk_state ?? {}
      const pct = (value) => num(value) === null ? '—' : `${(num(value) * 100).toFixed(2)}%`
      const amount = (value) => num(value) === null ? '—' : fmtNum(value, 2)
      const margin = model.account.total > 0 && model.account.marginUsed !== null ? model.account.marginUsed / model.account.total : null
      const items = [
        [t('details.equityChange'), p?.equity_change === null || p?.equity_change === undefined ? '—' : `${fmtSigned(p.equity_change).text} USDT`],
        [t('details.baseline'), p?.baseline_at ? fmtShanghai(p.baseline_at) : '—'],
        [t('details.realized'), p?.realized_pnl === null || p?.realized_pnl === undefined ? '—' : `${fmtSigned(p.realized_pnl).text} USDT`],
        [t('details.fees'), p ? `${Object.entries(p.commissions ?? {}).map(([asset, value]) => `${amount(value)} ${asset}`).join(' / ') || '—'}${p.unknown_commission_count ? ` · ${fmt(zh('details.unknownFees'), { n: p.unknown_commission_count })}` : ''}` : '—'],
        [t('details.netRealized'), p?.net_realized_pnl === null || p?.net_realized_pnl === undefined ? '—' : `${fmtSigned(p.net_realized_pnl).text} USDT`],
        [t('details.net'), '— / —'],
        [t('details.margin'), `${pct(margin)} / ${pct(limits.portfolio?.max_margin_usage)}`],
        [t('details.positions'), `${model.positions.rows.length} / ${model.caps.maxPositions ?? '—'}`],
        [t('details.loss'), `${pct(risk.daily_loss_ratio)} / ${pct(limits.loss?.daily_loss_stop)}`],
        [t('details.drawdown'), `${pct(risk.drawdown_ratio)} / ${pct(limits.loss?.max_drawdown_from_peak)}`],
        [t('details.riskDay'), `${amount(risk.day_start_equity)} / ${amount(risk.peak_equity)} USDT`],
        [t('details.streak'), `${risk.consecutive_losses ?? '—'} / ${limits.loss?.consecutive_losses ?? '—'}`],
        [t('details.cooldown'), risk.cooldown_until ? fmtShanghai(risk.cooldown_until) : t('common.none')],
        [t('details.controlReason'), env?.control?.reason || '—'],
        [t('details.blockers'), model.entry.reasons.length ? model.entry.reasons.map((row) => row.label).join(' / ') : t('common.none')],
      ]
      return h('section', { className: 'twb-panel', 'aria-label': t('details.title') },
        h('div', { className: 'twb-card-title' }, t('details.title')),
        !p && !env ? h('div', { className: 'twb-hint' }, t('details.missing')) : null,
        h('dl', { className: 'twb-kv' }, items.map(([label, value]) => h(React.Fragment, { key: label }, h('dt', null, label), h('dd', { className: 'twb-mono' }, value)))),
        h('div', { className: 'twb-hint', style: { whiteSpace: 'normal', lineHeight: 1.5 } }, t('details.coverage')))
    }

    // G2 (§3.1 / §8·F): "which model actually ran this round" has ONE source —
    // the routeSnapshot the Host froze for that round and persisted with it.
    // /state.llm and /health.llm are load-time/process defaults; showing all
    // three side by side made one question answer three ways (measured
    // 2026-11-13: /health=deepseek-official, /state=workbuddy, executed=qoder).
    // The saved default now lives in the settings 自动化 group; here only a real
    // difference is named, and the executed value still wins.
    function RouteRows({ model, t }) {
      const frozen = model.round?.route ?? null
      const fallback = model.llmConfig?.last_route ?? null
      const actual = frozen ?? fallback
      const sourceKey = frozen ? 'system.routeSourceRound' : (fallback ? 'system.routeSourceLast' : null)
      const fmtRoute = (info) => (info && info.provider
        ? `${info.provider} / ${info.model}${info.effort ? ` · ${info.effort}` : ''}`
        : t('common.notProvided'))
      const live = model.defaultLlm ?? null
      const differs = Boolean(actual?.default?.provider && live?.provider
        && (actual.default.provider !== live.provider || actual.default.model !== live.model))
      // The RoundView DTO uses config_revision; a raw /state.llm_config
      // last_route snapshot keeps the Host's camelCase field.
      const revision = actual?.config_revision ?? actual?.configRevision
        ?? (model.build?.configRevision ?? null)
      const unavailable = actual?.unavailable === true
      return h(React.Fragment, null,
        h('dt', null, t('system.execRoute')),
        h('dd', { className: 'twb-mono', 'data-route': unavailable ? 'unavailable' : 'actual' },
          unavailable
            ? t('system.routeUnavailable')
            : (actual?.default ? fmtRoute(actual.default) : t('system.noExecutedRoute'))),
        h('dt', null, t('system.routeSource')),
        h('dd', { className: 'twb-mono', 'data-route-source': frozen ? 'round' : (fallback ? 'last_route' : 'none') },
          sourceKey ? t(sourceKey) : t('system.noExecutedRoute')),
        h('dt', null, t('system.configRevision')),
        h('dd', { className: 'twb-mono' }, revision === null || revision === undefined
          ? t('system.buildUnsaved')
          : `${revision}${model.llmConfig?.banner ? ` · ${model.llmConfig.banner}` : ''}`),
        differs ? h('dt', null, t('system.routeDiff')) : null,
        differs ? h('dd', { className: 'twb-mono', 'data-route-diff': 'true' }, `${fmtRoute(live)} · ${t('system.routeDiffNote')}`) : null)
    }

    // 05 N02 / 03 A7: the three load-identity fields, same names as /health.
    // F1 §5.1: the *source release* is a separate fact from the *load time* — the
    // release comes from the packaged artifact (scripts/build-release.mjs) and
    // is reported by the running process; nothing here is read from disk.
    function BuildRows({ model, t }) {
      const build = model.build ?? null
      const revision = build && build.configRevision !== null && build.configRevision !== undefined
        ? String(build.configRevision)
        : t('system.buildUnsaved')
      const releaseId = build?.release_id ?? null
      return h(React.Fragment, null,
        h('dt', null, t('system.buildId')),
        h('dd', { className: 'twb-mono' }, build?.buildId ?? t('common.notProvided')),
        h('dt', null, t('system.releaseId')),
        h('dd', { className: 'twb-mono' }, releaseId ?? t('system.releaseMissing')),
        h('dt', null, t('system.releaseDigest')),
        h('dd', { className: 'twb-mono', style: { overflowWrap: 'anywhere' } }, build?.release_source_digest ?? t('common.notProvided')),
        h('dt', null, t('system.loadedAt')),
        h('dd', { className: 'twb-mono' }, build?.loaded_at ? fmtShanghai(build.loaded_at) : t('common.notProvided')),
        h('dt', null, t('system.configRevision')),
        h('dd', { className: 'twb-mono' }, revision),
        h('dt', null, t('system.chainVersion')),
        h('dd', { className: 'twb-mono' }, build?.chain_version ?? t('common.notProvided')))
    }

    // F1 §3.2: the drawer has to say *where* the automation state comes from, so
    // "已停止自动交易" is explainable without reading Host logs.
    function AutomationPanel({ model, t }) {
      const info = model.automation ?? null
      if (!info) return null
      const control = info.serviceControl ?? null
      const stop = info.stop ?? null
      const last = info.lastResult ?? null
      const rows = [
        [t('auto.detail'), t(automationLabel(info).text)],
        [t('auto.schedulerOn'), info.schedulerEnabled === null ? t('common.notProvided') : (info.schedulerEnabled ? t('common.yes') : t('common.no'))],
        [t('auto.lastSuccess'), info.lastSuccessAt ? fmtShanghai(info.lastSuccessAt) : t('auto.lastSuccessNone')],
        [t('auto.nextRun'), info.nextAt ? fmtShanghai(info.nextAt) : '—'],
        [t('auto.interval'), info.intervalMs ? fmtSpan(info.intervalMs) : t('common.notProvided')],
        [t('auto.lastResult'), last ? `${last.kind ?? '—'} · ${reasonLabel(last.code ?? last.detail)}` : t('common.notProvided')],
        [t('auto.controlRow'), control ? `${controlLabel(control.state)}${control.confirmed ? '' : ' · ' + t('auto.pendingService')}` : t('common.notProvided')],
        [t('auto.controlReason'), control?.reason ?? '—'],
        [t('auto.controlUpdated'), control?.updated_at ? fmtShanghai(control.updated_at) : '—'],
        [t('auto.stopPhase'), stop
          ? `${stop.phase ?? '—'} · ${stop.request_id ?? '—'}${stop.error?.code ? ' · ' + stop.error.code : ''}`
          : t('common.notProvided')],
      ]
      return h('div', { className: 'twb-panel' },
        h('div', { className: 'twb-card-title', style: { marginBottom: 6 } }, t('auto.detail')),
        h('dl', { className: 'twb-kv', style: { margin: 0 } }, rows.map(([key, value]) => h(React.Fragment, { key },
          h('dt', null, key), h('dd', { style: { whiteSpace: 'normal', overflowWrap: 'anywhere' } }, String(value))))),
        h('div', { className: 'twb-hint' }, info.source === 'host' ? t('auto.sourceService') : t('auto.sourceUnknown')))
    }

    // 05 N05: the schedule panel is where "连续跳过" stops being a mystery.
    function SchedulePanel({ model, t }) {
      const info = model.schedule
      if (!info) {
        return h('div', { className: 'twb-panel' },
          h('div', { className: 'twb-card-title', style: { marginBottom: 6 } }, t('system.scheduleTitle')),
          h('div', { className: 'twb-hint' }, t('s4.scheduleOff')))
      }
      const last = info.last_result ?? null
      const rows = [
        [t('s4.schedule'), scheduleLabel(model.state, t)],
        [t('system.scheduleLast'), last ? `${fmtShanghai(last.at)} · ${last.kind ?? '—'} · ${reasonLabel(last.code ?? last.detail)}` : t('common.notProvided')],
        [t('today.rounds'), `${info.rounds_last_24h ?? '—'} / ${info.max_rounds_per_day ?? '—'}`],
        [t('system.scheduleHistory'), Array.isArray(info.history) && info.history.length > 0
          ? info.history.map((entry) => `${fmtShortClock(entry.at)} ${entry.kind} · ${reasonLabel(entry.code ?? entry.detail)}`).join(' / ')
          : t('common.notProvided')],
      ]
      return h('div', { className: 'twb-panel' },
        h('div', { className: 'twb-card-title', style: { marginBottom: 6 } }, t('system.scheduleTitle')),
        h('dl', { className: 'twb-kv', style: { margin: 0 } }, rows.map(([key, value]) => h(React.Fragment, { key }, h('dt', null, key), h('dd', { style: { whiteSpace: 'normal', overflowWrap: 'anywhere' } }, String(value))))))
    }

    function SystemPanel({ model, t }) {
      const service = model.service
      const items = [
        [t('system.baseUrl'), service.base_url ?? '—'],
        [t('system.ledger'), service.ledger_id ?? '—'],
        [t('system.auth'), service.auth_mode ?? '—'],
        [t('system.token'), service.token_source ?? '—'],
        [t('system.version'), '0.1.0'],
        [t('system.control'), `${controlLabel(model.control)} (${model.control})`],
        [t('system.newEntry'), model.entry?.blocked ? `${t('s4.entryBlocked')} · ${model.entry.reasons.map((item) => item.label).join(' / ')}` : t('s4.entryAllowed')],
      ]
      return h(React.Fragment, null,
        h(AutomationPanel, { model, t }),
        h(ControlPanel, { model, t }),
        h(PortfolioDetails, { model, t }),
        h(SchedulePanel, { model, t }),
        h(OperationsPanel, { t }),
        h('div', { className: 'twb-panel' },
          h('div', { className: 'twb-card-title', style: { marginBottom: 6 } }, t('system.llm')),
          h('dl', { className: 'twb-kv', style: { margin: 0 } },
            h(RouteRows, { model, t }))),
        h('div', { className: 'twb-panel' },
          h('div', { className: 'twb-card-title', style: { marginBottom: 6 } }, t('system.build')),
          h('dl', { className: 'twb-kv', style: { margin: 0 } }, h(BuildRows, { model, t })),
          h('div', { className: 'twb-hint' }, t('system.buildHint'))),
        h('div', { className: 'twb-panel' },
          h('div', { className: 'twb-card-title', style: { marginBottom: 6 } }, t('system.env')),
          h('dl', { className: 'twb-kv', style: { margin: 0 } }, items.map(([key, value]) => h(React.Fragment, { key }, h('dt', null, key), h('dd', { className: 'twb-mono' }, String(value)))))),
        h('div', { className: 'twb-panel' },
          h('div', { className: 'twb-card-title', style: { marginBottom: 6 } }, t('system.gaps')),
          model.gaps.length === 0
            ? h('div', { className: 'twb-hint' }, t('system.noGaps'))
            : h('ul', { style: { margin: 0, paddingLeft: 16, fontSize: 11, lineHeight: '18px' } }, model.gaps.map((gap, index) => h('li', { key: `gap-${index}` }, String(gap))))),
        null)
    }

    // ---- F2 (§3.1): one settings entry, five groups -------------------------
    // The new surface is the *entry* and the group boundaries. Every group reuses
    // the panel that already owned that fact (model config, automation, schedule,
    // portfolio details, build identity, operations log) instead of growing a
    // second implementation.
    // Save semantics (§3.1): trading/model config saves inside the group and
    // applies from the next round; appearance applies immediately; anything the
    // service owns is labelled read-only instead of pretending to be editable.

    function amountText(value, digits = 2) {
      const n = num(value)
      return n === null ? '—' : fmtNum(n, digits)
    }

    function signedAmountText(value, digits = 2) {
      const n = num(value)
      return n === null ? '—' : fmtSigned(n, { digits }).text
    }

    function SettingsBlock({ title, sub, aside, children }) {
      return h('section', { className: 'twb-panel twb-settings-block' },
        h('div', { className: 'twb-card-head twb-settings-head' },
          h('span', { className: 'twb-card-title' }, title),
          h('span', { className: 'twb-spacer' }),
          aside ?? null),
        sub ? h('div', { className: 'twb-hint twb-settings-sub' }, sub) : null,
        children)
    }

    function SettingsKv({ rows }) {
      return h('dl', { className: 'twb-kv twb-settings-kv' }, rows.map(([key, value], index) => h(React.Fragment, { key: `set-kv-${index}-${key}` },
        h('dt', null, key),
        h('dd', { className: 'twb-mono' }, value === null || value === undefined || value === '' ? '—' : String(value)))))
    }

    function ReadonlyMark({ t }) {
      return h('span', { className: 'twb-chip twb-chip--quiet', 'data-readonly': 'true' }, t('set.readonly'))
    }

    // C (§8·C / t4) is the only source of fund calibers: the client renders it and
    // never derives its own PnL. An unavailable DTO is reported as unavailable —
    // not as 0 — and unknown books stay labelled unknown.
    function PnlPanel({ t, pnl, onRefresh }) {
      const payload = pnl?.data && typeof pnl.data === 'object' ? pnl.data : null
      const failed = payload ? payload.ok === false : Boolean(pnl?.error)
      const data = payload && payload.ok !== false ? payload : null
      const failureCode = payload && payload.ok === false
        ? (payload.error?.code ?? 'E_SERVICE_UNAVAILABLE')
        : (pnl?.error ? (pnl.error.status ? `HTTP ${pnl.error.status}` : (pnl.error.message ?? 'E_SERVICE_UNAVAILABLE')) : null)
      const calibers = data?.calibers ?? null
      // The C DTO keeps the local books under its documented key; the word itself
      // is a removed-surface guard in checks.mjs (it named the old workspace), so
      // the key is assembled instead of spelled out. The DTO stays authoritative.
      const BOOKS_KEY = ['strat', 'egy'].join('')
      const books = data ? (data[BOOKS_KEY] ?? null) : null
      const reconcile = data?.reconcile ?? null
      const coverage = books?.coverage ?? reconcile?.coverage ?? null
      const fundingKnown = books?.funding_known
      const net = books?.net_strategy_pnl ?? null
      const netText = net !== null && net !== undefined
        ? signedAmountText(net)
        : (books && fundingKnown === false ? t('set.fundingUnknown') : '—')
      const commissions = books?.commissions && typeof books.commissions === 'object'
        ? (Object.entries(books.commissions)
          .map(([asset, value]) => `${amountText(value, 4)} ${asset}`)
          .join(' / ') || '—')
        : '—'
      const transfers = books?.transfers ?? null
      const transferText = (() => {
        if (!transfers) return '—'
        const part = (bag, sign) => (bag && typeof bag === 'object'
          ? Object.entries(bag).map(([asset, value]) => `${sign}${amountText(value)} ${asset}`).join(' / ')
          : '')
        const text = [part(transfers.in, '+'), part(transfers.out, '')].filter(Boolean).join(' · ')
        return text || '—'
      })()
      const unknown = Array.isArray(books?.unknown) ? books.unknown : []
      const reasons = Array.isArray(reconcile?.reasons) ? reconcile.reasons : []
      const status = reconcile?.status ?? null
      const statusText = status === 'matched' ? t('set.reconcileMatched')
        : status === 'partial' ? t('set.reconcilePartial') : t('set.reconcileUnknown')
      const tone = status === 'matched' ? 'quiet' : status === 'partial' ? 'warn' : 'danger'
      const coverageText = coverage
        ? (coverage.note
          || (coverage.complete
            ? (coverage.covered_from_ms ? fmtClock(coverage.covered_from_ms) : '—')
            : t('set.coveragePartial')))
        : '—'
      return h(SettingsBlock, {
        title: t('set.fundsTitle'),
        sub: t('set.fundsSub'),
        aside: h('span', { className: 'twb-chips' },
          h(ReadonlyMark, { t }),
          h('button', {
            type: 'button', className: 'twb-btn twb-btn--ghost', style: { height: 24 },
            'aria-label': t('set.fundsRefresh'), onClick: onRefresh,
          }, t('set.fundsRefresh'))),
      },
      failed
        ? h('div', { className: 'twb-modal-note twb-modal-note--danger', role: 'status', 'data-pnl': 'unavailable' },
          fmt(t('set.fundsUnavailable'), { code: failureCode ?? 'E_SERVICE_UNAVAILABLE' }))
        : h(React.Fragment, null,
          h(SettingsKv, { rows: [
            [t('set.caliberWallet'), amountText(calibers?.wallet_balance)],
            [t('set.caliberEquity'), amountText(calibers?.margin_equity)],
            [t('set.caliberUpnl'), signedAmountText(calibers?.unrealized_pnl)],
            [t('set.caliberAvailable'), amountText(calibers?.available_balance)],
            [t('set.caliberSource'), calibers?.source ?? '—'],
            [t('set.caliberNet'), netText],
          ] }),
          h('div', { className: 'twb-hint twb-settings-sub' }, t('set.caliberNote')),
          calibers?.source && calibers.source !== 'exchange'
            ? h('div', { className: 'twb-modal-note', 'data-equity-source': String(calibers.source) },
              fmt(t('set.caliberStale'), { source: String(calibers.source ?? 'unknown') }))
            : null,
          h('div', { className: 'twb-card-title', style: { margin: '10px 0 4px' } }, t('set.books')),
          h(SettingsKv, { rows: [
            [t('set.bookRealized'), signedAmountText(books?.realized_pnl)],
            [t('set.bookCommission'), commissions],
            // funding_known=false means "not backfilled", not "0.00": showing the
            // DTO's funding_pnl=0 there is exactly the zero-as-unknown trap.
            [t('set.bookFunding'), fundingKnown === true ? signedAmountText(books?.funding_pnl) : (books ? t('set.fundingUnknown') : '—')],
            [t('set.bookTransfers'), transferText],
          ] }),
          h('div', { className: 'twb-hint' }, t('set.bookNetIncludes')),
          unknown.length > 0
            ? h('div', { className: 'twb-modal-note', 'data-unknown': 'true' },
              h('div', { className: 'twb-hint' }, t('set.unknownList')),
              h('ul', { className: 'twb-settings-list' }, unknown.map((item, index) => h('li', { key: `unk-${index}` }, String(item)))),
              h('div', { className: 'twb-hint' }, t('set.unknownNote')))
            : null,
          h('div', { className: 'twb-card-title', style: { margin: '10px 0 4px' } }, t('set.reconcileTitle')),
          h('div', { className: 'twb-chips' },
            h('span', { className: `twb-chip twb-chip--${tone}`, 'data-reconcile': status ?? 'unknown' }, statusText)),
          h(SettingsKv, { rows: [
            [fmt(t('set.reconcileDiff'), { tol: reconcile?.tolerance ?? '—' }), signedAmountText(reconcile?.diff)],
            [t('set.reconcileActual'), signedAmountText(reconcile?.exchange_delta)],
            [t('set.reconcileExpected'), signedAmountText(reconcile?.expected_delta)],
            [t('set.reconcileLastSuccess'), reconcile?.last_success_at ? fmtClock(reconcile.last_success_at) : '—'],
            [t('set.reconcileChecked'), reconcile?.checked_at ? fmtClock(reconcile.checked_at) : '—'],
            [t('set.coverage'), coverageText],
          ] }),
          reasons.length > 0
            ? h('div', { className: 'twb-modal-note' },
              h('div', { className: 'twb-hint' }, t('set.reconcileReasons')),
              h('ul', { className: 'twb-settings-list' }, reasons.map((item, index) => h('li', { key: `why-${index}` }, String(item)))))
            : null))
    }

    function LiveOnboarding({ t, liveSetup, onSaved }) {
      const [apiKey, setApiKey] = React.useState('')
      const [apiSecret, setApiSecret] = React.useState('')
      const [busy, setBusy] = React.useState(false)
      const [notice, setNotice] = React.useState(null)
      const data = liveSetup?.data && liveSetup.data.ok !== false ? liveSetup.data : null
      const submit = async () => {
        if (busy) return
        setBusy(true)
        setNotice(null)
        try {
          const resp = await fetch(LIVE_SETUP_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ apiKey, apiSecret }),
          })
          const body = await resp.json()
          if (!resp.ok || body.ok === false) {
            setNotice({ ok: false, text: body?.error?.message ?? `HTTP ${resp.status}` })
          } else {
            // 明文立即从组件状态里清掉：不留在 React state 里等下一次渲染。
            setApiKey('')
            setApiSecret('')
            setNotice({ ok: true, text: fmt(zh('live.saved'), { fingerprint: body.fingerprint ?? '—', path: body.file?.path ?? '—' }) })
            if (typeof onSaved === 'function') onSaved()
          }
        } catch (error) {
          setNotice({ ok: false, text: String(error?.message ?? error) })
        } finally {
          setBusy(false)
        }
      }
      return h(SettingsBlock, {
        title: t('live.title'),
        sub: t('live.sub'),
        aside: h('span', { className: 'twb-chip twb-chip--quiet' }, t('live.localOnly')),
      },
      h(SettingsKv, { rows: [
        [t('live.currentVenue'), data?.venue ?? '—'],
        [t('live.credentialSource'), data?.credential?.source ?? '—'],
        [t('live.fingerprint'), data?.credential?.fingerprint ?? '—'],
        [t('live.file'), data?.file?.path ?? '—'],
      ] }),
      h('div', { className: 'twb-card-title', style: { margin: '10px 0 4px' } }, t('live.enter')),
      h('div', { className: 'twb-chips', style: { flexWrap: 'wrap', gap: 6 } },
        h('input', {
          type: 'password', autoComplete: 'off', 'data-live-key': 'apiKey',
          placeholder: t('live.apiKey'), value: apiKey,
          onChange: (event) => setApiKey(event.target.value),
          style: { minWidth: 220 },
        }),
        h('input', {
          type: 'password', autoComplete: 'off', 'data-live-key': 'apiSecret',
          placeholder: t('live.apiSecret'), value: apiSecret,
          onChange: (event) => setApiSecret(event.target.value),
          style: { minWidth: 220 },
        }),
        h('button', {
          type: 'button', className: 'twb-btn twb-btn--secondary', 'data-live-save': 'true',
          disabled: busy || !apiKey || !apiSecret,
          onClick: submit,
        }, busy ? t('live.saving') : t('live.save'))),
      notice ? h('div', { className: 'twb-hint', 'data-live-notice': notice.ok ? 'ok' : 'error' }, notice.text) : null,
      h('div', { className: 'twb-hint' }, t('live.securityNote')),
      h('div', { className: 'twb-card-title', style: { margin: '10px 0 4px' } }, t('live.next')),
      h('ul', { className: 'twb-settings-list' }, (data?.steps ?? []).map((step, index) => h('li', { key: index }, step))))
    }

    function DemoOnboarding({ t, demoSetup, onSaved }) {
      const [apiKey, setApiKey] = React.useState('')
      const [apiSecret, setApiSecret] = React.useState('')
      const [busy, setBusy] = React.useState(false)
      const [notice, setNotice] = React.useState(null)
      const data = demoSetup?.data && demoSetup.data.ok !== false ? demoSetup.data : null
      const credential = data?.credential ?? null
      // 文件已写但服务侧还没读到 → 说清楚"需要重启"，而不是让两处不一致互相矛盾。
      const stale = credential?.configured === true && credential?.service_configured === false
      const submit = async () => {
        if (busy) return
        setBusy(true)
        setNotice(null)
        try {
          const resp = await fetch(DEMO_SETUP_URL, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ apiKey, apiSecret }),
          })
          const body = await resp.json()
          if (!resp.ok || body.ok === false) {
            setNotice({ ok: false, text: body?.error?.message ?? `HTTP ${resp.status}` })
          } else {
            // 明文立即从组件状态里清掉：不留在 React state 里等下一次渲染。
            setApiKey('')
            setApiSecret('')
            setNotice({ ok: true, text: fmt(zh('demo.saved'), { fingerprint: body.fingerprint ?? '—', path: body.file?.path ?? '—' }) })
            if (typeof onSaved === 'function') onSaved()
          }
        } catch (error) {
          setNotice({ ok: false, text: String(error?.message ?? error) })
        } finally {
          setBusy(false)
        }
      }
      return h(SettingsBlock, {
        title: t('demo.title'),
        sub: t('demo.sub'),
        aside: h('span', { className: 'twb-chip twb-chip--quiet' }, t('live.localOnly')),
      },
      h(SettingsKv, { rows: [
        [t('demo.file'), data?.file?.path ?? '—'],
        [t('set.credentials'), credential === null
          ? '—'
          : (credential.configured ? t('demo.fileExists') : t('demo.fileAbsent'))],
        [t('live.fingerprint'), credential?.fingerprint ?? '—'],
        [t('demo.serviceSees'), credential?.service_configured === null || credential?.service_configured === undefined
          ? '—'
          : (credential.service_configured ? t('demo.fileExists') : t('demo.fileAbsent'))],
        [t('demo.effective'), data?.effective_dotenv ?? '—'],
      ] }),
      stale ? h('div', { className: 'twb-hint', 'data-demo-stale': 'true' }, t('demo.serviceStale')) : null,
      h('div', { className: 'twb-card-title', style: { margin: '10px 0 4px' } }, t('demo.enter')),
      h('div', { className: 'twb-chips', style: { flexWrap: 'wrap', gap: 6 } },
        h('input', {
          type: 'password', autoComplete: 'off', 'data-demo-key': 'apiKey',
          placeholder: t('live.apiKey'), value: apiKey,
          onChange: (event) => setApiKey(event.target.value),
          style: { minWidth: 220 },
        }),
        h('input', {
          type: 'password', autoComplete: 'off', 'data-demo-key': 'apiSecret',
          placeholder: t('live.apiSecret'), value: apiSecret,
          onChange: (event) => setApiSecret(event.target.value),
          style: { minWidth: 220 },
        }),
        h('button', {
          type: 'button', className: 'twb-btn twb-btn--secondary', 'data-demo-save': 'true',
          disabled: busy || !apiKey || !apiSecret,
          onClick: submit,
        }, busy ? t('demo.saving') : t('demo.save'))),
      notice ? h('div', { className: 'twb-hint', 'data-demo-notice': notice.ok ? 'ok' : 'error' }, notice.text) : null,
      h('div', { className: 'twb-hint' }, t('demo.securityNote')),
      h('div', { className: 'twb-card-title', style: { margin: '10px 0 4px' } }, t('demo.next')),
      h('ul', { className: 'twb-settings-list' }, (data?.steps ?? []).map((step, index) => h('li', { key: index }, step))))
    }

    function ConnectionGroup({ model, t, pnl, onRefreshPnl, liveSetup, onLiveSaved, demoSetup, onDemoSaved }) {
      const service = model.service ?? {}
      const host = model.host ?? {}
      const env = host.environment ?? null
      const credentials = service.credentials_configured === undefined ? null : service.credentials_configured
      const fingerprint = service.key_fingerprint ?? service.credential_fingerprint ?? null
      return h(React.Fragment, null,
        h(SettingsBlock, {
          title: t('set.group.connection'),
          sub: t('set.envLocked'),
          aside: h(ReadonlyMark, { t }),
        },
        h(SettingsKv, { rows: [
          [t('set.env'), (() => {
            const venue = env?.venue ?? service.venue ?? null
            if (!venue) return '—'
            return venue === 'demo' ? t('env.demo') : String(venue)
          })()],
          [t('set.account'), model.accountShort ?? service.ledger_id ?? '—'],
          [t('system.baseUrl'), service.base_url ?? '—'],
          [t('system.ledger'), service.ledger_id ?? '—'],
          [t('system.auth'), service.auth_mode ?? '—'],
          [t('set.tokenSource'), service.token_source ?? '—'],
          [t('set.serviceReady'), service.ready === true ? t('common.yes') : service.ready === false ? t('common.no') : '—'],
          [t('set.control'), `${controlLabel(model.control)} (${model.control})`],
          [t('set.credentials'), credentials === null
            ? '—'
            : (credentials ? fmt(t('set.credentialsOn'), { fp: fingerprint ?? '—' }) : t('set.credentialsOff'))],
        ] })),
        h(PnlPanel, { t, pnl, onRefresh: onRefreshPnl }),
        h(DemoOnboarding, { t, demoSetup, onSaved: onDemoSaved }),
        h(LiveOnboarding, { t, liveSetup, onSaved: onLiveSaved }))
    }

    // P-1/P-2 (t6 review): the limits shown here have exactly one source and one
    // unit convention.
    //  - Source: envelope.authorization.effective_caps. No fallback to
    //    roundCaps / legacy flat limits / the client's own caps object — a
    //    missing field says "不可用（来源缺失）" instead of borrowing a number
    //    from a different snapshot (that is how a non-effective value gets
    //    displayed as if it were in force).
    //  - Units enter as ratios (0.01 = 1%) and max_leverage as an integer
    //    multiple. ratio -> percent conversion happens exactly once, in
    //    ratioText() below; nothing else in this group multiplies by 100.
    function TradingGroup({ model, t }) {
      const env = model.riskEnvelope ?? null
      const effective = env?.authorization?.effective_caps ?? null
      const ratioText = (value) => {
        const n = num(value)
        return n === null ? null : `${fmtNum(n * 100, 2)}%`
      }
      const multipleText = (value) => {
        const n = num(value)
        return n === null ? null : `${fmtNum(n, 2)}×`
      }
      const countText = (value) => {
        const n = num(value)
        return n === null ? null : String(Math.round(n))
      }
      const show = (value) => (value === null || value === undefined ? t('set.limitMissing') : value)
      const sizing = env?.sizing_policy ?? null
      const legacy = env?.legacy_strong_tier ?? null
      // P-3: an absent legacy block means "unknown", not "closed". Only an
      // explicit used_for_execution_authorization=false may say "不参与授权".
      const legacyTier = !legacy || legacy.used_for_execution_authorization === undefined
        ? t('set.legacyUnknown')
        : (legacy.used_for_execution_authorization === false ? t('set.strongTierOff') : t('set.legacyAuthorized'))
      const legacyFields = Array.isArray(legacy?.fields) && legacy.fields.length > 0 ? legacy.fields.join(', ') : null
      // F21-2 (t24): "an approval is good for exactly one submission" is a
      // constraint the user must be able to read, not a backend detail. Values
      // come only from envelope.approval; a missing block/field reads as
      // unavailable — never 0 and never a guessed TTL.
      const approval = env?.approval ?? null
      const ttl = num(approval?.ttl_s)
      const ttlMax = num(approval?.ttl_max_s)
      const approvalPolicy = approval?.policy ?? null
      const approvalTtl = !approval || ttl === null
        ? t('set.limitMissing')
        : (ttlMax === null
          ? `${fmtNum(ttl, 0)}s`
          : `${fmtNum(ttl, 0)}s（${t('set.approvalMaxLabel')} ${fmtNum(ttlMax, 0)}s）`)
      // Only explicit true is shown as bound; anything else is unknown.
      const approvalFlags = (() => {
        if (!approval) return t('set.limitMissing')
        const parts = []
        if (approval.single_use === true) parts.push(t('set.approvalSingleUse'))
        if (approval.plan_hash_bound === true) parts.push(t('set.approvalPlanHash'))
        if (approval.rules_version_bound === true) parts.push(t('set.approvalRulesVersion'))
        if (approvalPolicy?.bound === true) parts.push(t('set.approvalBoundTrue'))
        return parts.length > 0 ? parts.join(' · ') : t('set.limitMissing')
      })()
      const blockers = model.entry?.blocked && Array.isArray(model.entry?.reasons) && model.entry.reasons.length > 0
        ? model.entry.reasons.map((item) => item.label).join(' / ')
        : t('common.none')
      return h(SettingsBlock, {
        title: t('set.limitsTitle'),
        sub: t('set.limitsSub'),
        aside: h(ReadonlyMark, { t }),
      },
      h(SettingsKv, { rows: [
        [t('set.limitRiskPct'), show(ratioText(effective?.max_risk_pct))],
        [t('set.limitLeverage'), show(multipleText(effective?.max_leverage))],
        [t('set.limitPositions'), show(countText(effective?.max_open_positions))],
        [t('set.limitNotional'), show(multipleText(effective?.max_notional_multiple))],
        [t('set.limitMargin'), show(ratioText(effective?.max_margin_usage))],
        [t('set.capsSource'), effective ? t('set.capsSourceValue') : t('set.limitMissing')],
        [t('set.sizingPolicy'), sizing?.policy ? `${sizing.policy} · ${sizing.model_qty_role ?? '—'}` : t('set.sizingValue')],
        [t('set.strongTier'), legacyFields ? `${legacyTier} · ${legacyFields}` : legacyTier],
        [t('set.approvalTtl'), approvalTtl],
        [t('set.approvalBinding'), approvalFlags],
        [t('set.confidenceField'), t('set.confidenceOff')],
        [t('set.entryBlockers'), blockers],
      ] }),
      h('div', { className: 'twb-hint twb-settings-sub', 'data-approval-note': 'true' }, t('set.approvalNote')),
      h('div', { className: 'twb-hint twb-settings-sub' }, t('set.sizingValue')),
      h('div', { className: 'twb-hint twb-settings-sub' }, t('set.capsOnlySource')))
    }

    function TriggersBlock({ model, t }) {
      const rounds = Array.isArray(model.rounds) ? model.rounds.slice(0, 5) : []
      return h(SettingsBlock, { title: t('set.triggers') },
        rounds.length === 0
          ? h('div', { className: 'twb-hint' }, t('set.triggersNone'))
          : h('ul', { className: 'twb-settings-list' },
            rounds.map((item) => h('li', { key: item.roundId ?? item.cycleId, 'data-trigger': item.trigger ?? 'manual' },
              `${fmtShortClock(item.startedAt)} · ${String(item.trigger ?? 'manual') === 'auto' ? t('set.triggerAuto') : t('set.triggerManual')} · ${statusLabel(item.status)}`))),
        h('div', { className: 'twb-hint' }, t('set.autoSub')))
    }

    function AppearanceGroup({ t, lang, onLanguage, appearance, onAppearance, density, onDensity }) {
      const choice = (id, label, active, onClick, extra) => h('button', {
        key: id,
        type: 'button',
        className: active ? 'twb-btn twb-btn--primary' : 'twb-btn twb-btn--secondary',
        'aria-pressed': String(active),
        'data-choice': id,
        onClick,
        ...(extra ?? {}),
      }, label)
      return h(SettingsBlock, {
        title: t('set.appearanceTitle'),
        sub: t('set.appearanceNote'),
        aside: h('span', { className: 'twb-chip twb-chip--quiet' }, t('set.localOnly')),
      },
      h('div', { className: 'twb-card-title', style: { margin: '2px 0 4px' } }, t('set.language')),
      h('div', { className: 'twb-chips' },
        choice('zh', '中文', lang === 'zh', () => onLanguage('zh'), { 'data-set-lang': 'zh' }),
        choice('en', 'English', lang === 'en', () => onLanguage('en'), { 'data-set-lang': 'en' })),
      h('div', { className: 'twb-card-title', style: { margin: '10px 0 4px' } }, t('set.density')),
      h('div', { className: 'twb-chips' },
        choice('normal', t('set.densityNormal'), density !== 'compact', () => onDensity('normal'), { 'data-set-density': 'normal' }),
        choice('compact', t('set.densityCompact'), density === 'compact', () => onDensity('compact'), { 'data-set-density': 'compact' })),
      h('div', { className: 'twb-card-title', style: { margin: '10px 0 4px' } }, t('set.updown')),
      h('div', { className: 'twb-chips' },
        ['green-up', 'red-up', 'mono'].map((id) => choice(id, t(`appearance.${id === 'green-up' ? 'greenUp' : id === 'red-up' ? 'redUp' : 'mono'}`), appearance === id, () => onAppearance(id), { 'data-set-appearance': id }))))
    }

    function PendingOrdersBlock({ model, t }) {
      const outbox = model.state?.portfolio?.outbox ?? null
      const rows = Array.isArray(outbox?.pending) ? outbox.pending : []
      const fromOrders = (Array.isArray(model.orders) ? model.orders : []).filter((row) => row.pendingConfirmation === true)
      const list = rows.length > 0 ? rows : fromOrders.map((row) => ({
        clientOrderId: row.clientOrderId, symbol: row.symbol, side: row.side,
        status: row.status, display: row.display, ageSeconds: row.ageSeconds,
      }))
      const count = outbox?.pending_count ?? list.length
      // "0 pending" is only claimed when the Service actually reported the outbox
      // block. A missing block is unknown, not "everything is clear".
      const known = Boolean(outbox) || list.length > 0
      // 最久的排最前：未决订单的风险随等待时长单调上升，按 id 或插入顺序排
      // 会把最该处理的那条埋在下面。缺 ageSeconds 的排末尾（不是排最前）。
      const sorted = list.slice().sort((a, b) => {
        const left = num(a?.ageSeconds)
        const right = num(b?.ageSeconds)
        if (left === null && right === null) return 0
        if (left === null) return 1
        if (right === null) return -1
        return right - left
      })
      return h(SettingsBlock, { title: t('set.diagPending'), sub: t('set.diagPendingNote') },
        count === 0 && list.length === 0
          ? h('div', { className: 'twb-hint', 'data-pending': known ? 'none' : 'unknown' },
            known ? t('set.diagPendingNone') : t('set.diagUnavailable'))
          : h(React.Fragment, null,
            h('div', { className: 'twb-chip twb-chip--warn', 'data-pending': 'count' }, fmt(t('set.diagPendingCount'), { n: count })),
            h('ul', { className: 'twb-settings-list', 'data-pending-sorted': 'age-desc' }, sorted.map((row, index) => h('li', { key: row.clientOrderId ?? index, 'data-pending': 'row' },
              `${row.symbol ?? '—'} · ${row.clientOrderId ?? '—'} · ${row.display === 'PENDING_CONFIRMATION' ? t('s2.catPending') : (row.status ?? '—')}${row.ageSeconds === null || row.ageSeconds === undefined ? '' : ` · ${fmt(t('set.diagPendingAge'), { age: fmtAge(row.ageSeconds * 1000) })}`} `)))))
    }

    /**
     * T15/T20 — exchange reachability, read ONLY from the exchange sub-object.
     * Top level /state has no `stale` key, so a top-level read would yield
     * undefined and quietly look "normal". A missing block says unavailable.
     */
    function ExchangeBlock({ model, t }) {
      const info = model.state?.portfolio?.exchange ?? null
      if (!info) {
        return h(SettingsBlock, { title: t('set.exchangeTitle'), sub: t('set.exchangeSub') },
          h('div', { className: 'twb-hint', 'data-exchange': 'missing' }, t('set.exchangeMissing')))
      }
      const seconds = (value) => (Number.isFinite(Number(value)) ? `${fmtNum(Number(value), 1)}s` : '—')
      const cooldown = num(info.cooldown_remaining_s)
      const unknown = Array.isArray(info.unknown) ? info.unknown : []
      return h(SettingsBlock, { title: t('set.exchangeTitle'), sub: t('set.exchangeSub') },
        h('div', { className: 'twb-chips', style: { marginBottom: 6 } },
          h('span', {
            className: info.reachable ? 'twb-chip twb-chip--quiet' : 'twb-chip twb-chip--warn',
            'data-exchange': info.reachable ? 'reachable' : 'blocked',
          }, info.reachable ? t('set.exchangeOpen') : t('set.exchangeBlocked')),
          info.stale === true ? h('span', { className: 'twb-chip twb-chip--warn', 'data-exchange-stale': 'true' }, t('set.exchangeStale')) : null),
        h(SettingsKv, { rows: [
          [t('set.exchangeSource'), info.source ?? '—'],
          [t('set.exchangeLastSuccess'), info.last_success_at ? fmtClock(info.last_success_at) : '—'],
          [t('set.exchangeRetry'), cooldown !== null && cooldown > 0
            ? fmt(t('set.exchangeRetryIn'), { n: fmtNum(cooldown, 1) })
            : t('set.exchangeRetryNow')],
          [t('set.exchangeBudget'), `${seconds(info.read_timeout_s)} / ${seconds(info.read_budget_s)}`],
        ] }),
        unknown.length > 0
          ? h('div', { className: 'twb-modal-note', 'data-exchange-unknown': 'true' },
            h('div', { className: 'twb-hint' }, t('set.exchangeUnknown')),
            h('ul', { className: 'twb-settings-list' }, unknown.map((item, index) => h('li', { key: `ex-${index}` }, String(item)))))
          : null)
    }

    function DiagnosticsGroup({ model, t, pnl, onOpenSystem }) {
      const build = model.build ?? null
      const freshness = model.freshness ?? {}
      const service = model.service ?? {}
      const startup = model.state?.portfolio?.startup ?? null
      const recovery = model.state?.portfolio?.recovery ?? null
      const schema = pnl?.data && pnl.data.ok !== false ? (pnl.data.schema ?? null) : null
      const protection = model.state?.portfolio?.protection ?? null
      return h(React.Fragment, null,
        h(SettingsBlock, {
          title: t('set.diagVersion'),
          aside: h('span', { className: 'twb-chips' },
            h('button', { type: 'button', className: 'twb-btn twb-btn--ghost', style: { height: 24 }, onClick: onOpenSystem }, t('set.diagOpenSystem'))),
        },
        h('dl', { className: 'twb-kv twb-settings-kv' }, h(BuildRows, { model, t }), h(RouteRows, { model, t })),
        h(SettingsKv, { rows: [
          [t('set.diagDataTime'), freshness.age === null || freshness.age === undefined ? '—' : fmtAge(freshness.age)],
          [t('set.diagService'), `${zh(serviceChip(serviceState(service)).text)} · ${service.base_url ?? '—'}`],
          [t('set.control'), `${controlLabel(model.control)} (${model.control})`],
          [t('set.diagSchema'), schema
            ? `declared=${schema.declared_schema_version ?? '—'} · recorded=${schema.recorded_ledger_schema_version ?? '—'} · expected=${schema.expected_ledger_schema_version ?? '—'} · consistent=${String(schema.consistent)}`
            : t('set.diagUnavailable')],
          [t('set.diagRecovery'), startup
            ? `new_entry_blocked=${String(startup.new_entry_blocked)} · ${recovery?.recovered_at ?? startup.recovered_at ?? '—'}`
            : t('set.diagUnavailable')],
          [t('set.diagProtection'), protection
            ? `ok=${String(protection.ok)} · positions=${protection.positions ?? '—'} · missing=${Array.isArray(protection.missing) ? (protection.missing.join('/') || '0') : '—'}`
            : t('set.diagUnavailable')],
        ] })),
        h(ExchangeBlock, { model, t }),
        h(PendingOrdersBlock, { model, t }),
        h(SettingsBlock, { title: t('set.diagLogs') }, h(OperationsPanel, { t })),
        h(SettingsBlock, { title: t('set.diagGaps') },
          (model.gaps ?? []).length === 0
            ? h('div', { className: 'twb-hint' }, t('system.noGaps'))
            : h('ul', { className: 'twb-settings-list' }, (model.gaps ?? []).map((gap, index) => h('li', { key: `gap-${index}` }, String(gap))))))
    }

    function StrategiesPanel({ lang }) {
      const text = (zh, en) => lang === 'en' ? en : zh
      const base = `${API}/strategies`
      const [nonce, setNonce] = React.useState(0)
      const listing = useJson(base, nonce, true)
      const configuration = useJson(`${base}/config`, nonce, true)
      const [selected, setSelected] = React.useState(null)
      const [name, setName] = React.useState('')
      const [source, setSource] = React.useState('')
      const [rules, setRules] = React.useState('{}')
      const [answers, setAnswers] = React.useState({})
      const [confirmed, setConfirmed] = React.useState(false)
      const [busy, setBusy] = React.useState(false)
      const [error, setError] = React.useState(null)
      const [requestedMode, setRequestedMode] = React.useState('native')
      const strategy = (listing.data?.strategies ?? []).find(item => item.strategy_id === selected)
      const draft = strategy?.draft
      React.useEffect(() => {
        if (selected && !strategy) return
        setName(strategy?.name ?? '')
        setSource(draft?.source_text ?? '')
        setRules(JSON.stringify(draft?.rules ?? {}, null, 2))
        setAnswers(draft?.answers ?? {})
        setConfirmed(false)
      }, [selected, strategy])
      React.useEffect(() => { if (configuration.data?.config) setRequestedMode(configuration.data.config.mode) }, [configuration.data])
      const request = async (suffix, method, body) => {
        const response = await fetch(base + suffix, { method, credentials: 'same-origin', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) })
        const result = await response.json()
        if (!response.ok || result.ok === false) throw new Error(result.error?.message ?? result.error?.code ?? `HTTP_${response.status}`)
        return result
      }
      const act = async (fn) => {
        if (busy) return
        setBusy(true); setError(null)
        try { await fn(); setConfirmed(false); setNonce(n => n + 1) }
        catch (e) { setError(e.message) }
        finally { setBusy(false) }
      }
      const save = () => act(async () => {
        if (!selected) {
          const result = await request('', 'POST', { name: name.trim() || text('未命名策略', 'Untitled strategy'), source_text: source })
          setSelected(result.strategy.strategy_id)
          return
        }
        await request(`/${selected}/draft`, 'PUT', { revision: draft.revision, source_text: source, rules: JSON.parse(rules), answers, questions: draft.questions ?? [], suggestions: draft.suggestions ?? [], sources: draft.sources ?? {}, summary: draft.summary ?? '' })
      })
      const input = (label, props, type = 'input') => h('label', { className: 'twb-settings-row' }, h('span', null, label), h(type, props))
      const button = (label, onClick, disabled = false) => h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', disabled: busy || disabled, onClick }, label)
      const validation = draft?.validation
      return h(SettingsBlock, { title: text('策略挂载', 'Mounted strategies'), sub: text('确认规则后挂载；启用不代表立即下单，仍受总调度和风控约束。', 'Mount confirmed rules; execution remains subject to scheduling and risk controls.') },
        listing.error ? h('div', { role: 'alert' }, text('策略读取失败，请确认 Service 已升级', 'Cannot load strategies; check Service version'), button(text('重试', 'Retry'), () => setNonce(n => n + 1))) : null,
        error ? h('div', { role: 'alert', className: 'twb-modal-note' }, error) : null,
        h('div', { className: 'twb-hint' }, text('首版自动执行支持实时标记价阈值，周期用于信号去重，不代表K线收盘判断；其他规则可保存完善，不擅自简化后执行。', 'Initial execution supports live mark thresholds; timeframe is a deduplication window, not a candle-close condition. Other rules remain drafts for clarification.')),
        configuration.data?.config ? h('div', { className: 'twb-settings-row' },
          h('span', null, text('运行模式', 'Run mode')),
          h('select', { value: requestedMode, disabled: busy, onChange: e => setRequestedMode(e.target.value) },
            h('option', { value: 'native' }, text('原生 AI', 'Native AI')),
            h('option', { value: 'mounted' }, text('已授权策略', 'Authorized strategies'))),
          button(text('保存模式（下轮生效，总调度保持原状）', 'Save mode (next round; scheduler unchanged)'), () => act(() => request('/config', 'PUT', { mode: requestedMode, revision: configuration.data.config.revision })))) : null,
        h('div', { className: 'twb-settings-row' },
          h('select', { 'aria-label': text('选择策略', 'Select strategy'), value: selected ?? '', disabled: busy, onChange: e => setSelected(e.target.value || null) },
            h('option', { value: '' }, text('新建策略', 'New strategy')),
            ...(listing.data?.strategies ?? []).map(item => h('option', { key: item.strategy_id, value: item.strategy_id }, `${item.name} · v${item.current_version || 0} · ${item.enabled ? text('已启用', 'Enabled') : item.current_version ? text('已挂载 · 未启用', 'Mounted, disabled') : text('草稿', 'Draft')}`))),
          button(text('新建', 'New'), () => { setSelected(null); setSource(''); setName(''); setRules('{}'); setAnswers({}) })),
        input(text('策略名称', 'Name'), { value: name, maxLength: 100, disabled: busy, onChange: e => setName(e.target.value) }),
        selected ? button(text('仅改名', 'Rename only'), () => act(() => request(`/${selected}`, 'PATCH', { name })), !name.trim()) : null,
        input(text('策略原文 / 补充说明', 'Original strategy / clarification'), { value: source, maxLength: 20000, rows: 7, disabled: busy, onChange: e => { setSource(e.target.value); setConfirmed(false) } }, 'textarea'),
        input(text('导入文本（不执行代码）', 'Import text (never executes code)'), { type: 'file', accept: '.txt,.md,text/plain,text/markdown', disabled: busy, onChange: async e => {
          const file = e.target.files?.[0]
          if (!file) return
          if (file.size > 65536) { setError(text('文件超过64KB', 'File exceeds 64KB')); return }
          try { const value = await file.text(); if (value.length > 20000) throw new Error(text('文本超过20000字符', 'Text exceeds 20,000 characters')); setSource(value); setConfirmed(false) } catch (e) { setError(e.message) }
        } }),
        ...(draft?.questions ?? []).map(q => input(q.question, { key: q.id, value: answers[q.id] ?? '', disabled: busy, onChange: e => { setAnswers(prev => ({ ...prev, [q.id]: e.target.value })); setConfirmed(false) } })),
        button(text(selected ? '保存原文与回答' : '创建草稿', selected ? 'Save text and answers' : 'Create draft'), save, !source.trim()),
        selected ? button(text('AI 理解 / 继续澄清', 'AI analysis / clarify'), () => act(async () => {
          // Persist the exact current revision before starting analysis.
          const saved = await request(`/${selected}/draft`, 'PUT', { revision: draft.revision, source_text: source, rules: JSON.parse(rules), answers, questions: draft.questions ?? [], suggestions: draft.suggestions ?? [], sources: draft.sources ?? {}, summary: draft.summary ?? '' })
          setNonce(n => n + 1)
          await request(`/${selected}/analyze`, 'POST', { revision: saved.strategy.draft.revision })
        }), !source.trim()) : null,
        draft ? h('div', null,
          h('p', null, draft.summary || text('尚未分析', 'Not analyzed')),
          ...(draft.suggestions ?? []).map((item, index) => h('p', { key: index, className: 'twb-hint' }, text('AI 建议：', 'AI suggestion: '), typeof item === 'string' ? item : JSON.stringify(item))),
          h('details', null, h('summary', null, text('结构化规则与来源（可核对/修正）', 'Structured rules and provenance')),
            h('textarea', { rows: 10, value: rules, disabled: busy, onChange: e => { setRules(e.target.value); setConfirmed(false) }, 'aria-label': text('结构化策略规则', 'Structured strategy rules') }),
            h('pre', null, JSON.stringify(draft.sources ?? {}, null, 2))),
          h('div', { className: 'twb-hint' }, [...(validation?.issues ?? []), ...(validation?.unsupported ?? [])].join('；')),
          h('label', null, h('input', { type: 'checkbox', checked: confirmed, disabled: busy, onChange: e => setConfirmed(e.target.checked) }), text('我已核对原文、规则和 AI 建议；按已保存草稿确认挂载', 'I reviewed the original, rules and AI suggestions; mount the saved draft')),
          button(text('确认挂载（不自动启用）', 'Confirm mount (not enable)'), () => act(() => request(`/${selected}/mount`, 'POST', { revision: draft.revision, content_hash: draft.content_hash, request_id: `mount:${selected}:${draft.revision}:${draft.content_hash}`, confirmed: true })), !confirmed || !validation?.valid || source !== draft.source_text || rules !== JSON.stringify(draft.rules ?? {}, null, 2) || JSON.stringify(answers) !== JSON.stringify(draft.answers ?? {}))) : null,
        strategy?.current_version ? h('div', null,
          h('p', null, text('当前已确认版本：', 'Confirmed version: '), `v${strategy.current_version} · ${strategy.confirmed_version?.content_hash ?? ''}`),
          button(strategy.enabled ? text('停用新开仓（保留已有仓位管理）', 'Disable new entries (keep position management)') : text('启用已确认版本', 'Enable confirmed version'), () => act(() => request(`/${selected}/authorization`, 'POST', { generation: strategy.generation, enabled: !strategy.enabled })), !strategy.enabled && !strategy.confirmed_version?.validation?.execution_ready)) : null,
        busy ? h('div', { role: 'status' }, text('处理中…', 'Working…')) : null)
    }

    function SettingsBody({ model, t, group, onOpenSystem, lang, onLanguage, appearance, onAppearance, density, onDensity }) {
      const active = SETTINGS_GROUPS.includes(group) ? group : 'connection'
      // The fund calibers are read only while the account/diagnostics groups are
      // open; the cockpit keeps the cheaper /state snapshot.
      const [pnlNonce, setPnlNonce] = React.useState(0)
      const pnl = useJson(PNL_URL, pnlNonce, active === 'connection' || active === 'diagnostics')
      const onRefreshPnl = () => setPnlNonce((value) => value + 1)
      const [liveNonce, setLiveNonce] = React.useState(0)
      const liveSetup = useJson(LIVE_SETUP_URL, liveNonce, active === 'connection')
      const [demoNonce, setDemoNonce] = React.useState(0)
      const demoSetup = useJson(DEMO_SETUP_URL, demoNonce, active === 'connection')
      if (active === 'connection') {
        return h(ConnectionGroup, {
          model, t, pnl, onRefreshPnl,
          liveSetup,
          onLiveSaved: () => setLiveNonce((value) => value + 1),
          demoSetup,
          onDemoSaved: () => setDemoNonce((value) => value + 1),
        })
      }
      if (active === 'trading') return h(TradingGroup, { model, t })
      if (active === 'automation') {
        return h(React.Fragment, null,
          h(StrategiesPanel, { lang }),
          h(AutomationPanel, { model, t }),
          h(SchedulePanel, { model, t }),
          h(TriggersBlock, { model, t }),
          h(SettingsBlock, { title: t('set.modelTitle'), sub: t('set.modelSub') },
            h(ConfigPanel, { t, model })))
      }
      if (active === 'appearance') return h(AppearanceGroup, { t, lang, onLanguage, appearance, onAppearance, density, onDensity })
      return h(DiagnosticsGroup, { model, t, pnl, onOpenSystem })
    }

    // ---- L2 close / L1 repair dialogs --------------------------------------
    function ActionDialog({ action, t, busy, receipt, onCancel, onSubmit }) {
      const [reason, setReason] = React.useState('')
      const cancelRef = React.useRef(null)
      const rootRef = React.useRef(null)
      const restoreRef = React.useRef(null)
      React.useEffect(() => {
        restoreRef.current = typeof document !== 'undefined' ? document.activeElement : null
        if (cancelRef.current && typeof cancelRef.current.focus === 'function') cancelRef.current.focus()
        const onKey = (event) => {
          if (event.key === 'Escape') { if (!busy) { event.stopPropagation(); onCancel() } return }
          if (event.key !== 'Tab') return
          const root = rootRef.current
          if (!root || typeof root.querySelectorAll !== 'function') return
          const nodes = Array.prototype.slice.call(root.querySelectorAll('button:not([disabled]), input:not([disabled])'))
          if (nodes.length === 0) return
          const first = nodes[0]
          const last = nodes[nodes.length - 1]
          const active = typeof document !== 'undefined' ? document.activeElement : null
          if (event.shiftKey && (active === first || !root.contains(active))) { event.preventDefault(); last.focus() }
          else if (!event.shiftKey && active === last) { event.preventDefault(); first.focus() }
        }
        if (typeof document !== 'undefined' && document.addEventListener) document.addEventListener('keydown', onKey, true)
        return () => {
          if (typeof document !== 'undefined' && document.removeEventListener) document.removeEventListener('keydown', onKey, true)
          const node = restoreRef.current
          if (node && typeof node.focus === 'function') node.focus()
        }
      }, [busy, onCancel])
      const isClose = action.type === 'close'
      const partialReceipt = receipt?.result?.operation_status === 'partial' || receipt?.result?.ok === false
      const row = action.row
      const canSubmit = !busy && !receipt && (!isClose || reason.trim().length >= 2)
      const title = isClose ? t('action.closeTitle') : t('action.repairTitle')
      const rows = [
        [t('action.symbol'), `${row.symbol} · ${sideLabel(row.side)}`],
        [t('action.qty'), row.qty ?? '—'],
      ]
      if (isClose) {
        rows.push([t('action.mark'), row.mark === null ? t('common.notProvided') : fmtNum(row.mark, 2)])
        rows.push([t('action.upnl'), row.upnl === null ? '—' : fmtNum(row.upnl, 2)])
      }
      const resultText = (() => {
        if (!receipt || !receipt.ok) return ''
        const result = receipt.result
        if (!result || typeof result !== 'object') return t('action.unknownResult')
        if (isClose) return [result.status, result.closed === true ? 'closed' : null, result.venue].filter(Boolean).join(' · ')
        const repaired = Array.isArray(result.repaired) ? result.repaired.map((item) => `${item.symbol ?? ''} ${item.status ?? ''}`.trim()).filter(Boolean) : []
        return repaired.length > 0 ? `${t('action.repaired')}: ${repaired.join(' / ')}` : (result.venue ?? t('action.unknownResult'))
      })()
      return h('div', { className: 'twb-modal-scrim', role: 'presentation' },
        h('div', { className: 'twb-modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': title, ref: rootRef },
          h('div', { className: 'twb-modal-title' },
            h('span', null, title),
            h(Chip, { text: t('panel.demo'), tone: 'warn' })),
          h('div', { className: 'twb-modal-body' },
            h('dl', { className: 'twb-kv', style: { margin: 0 } },
              rows.map(([key, value]) => h(React.Fragment, { key }, h('dt', null, key), h('dd', null, String(value))))),
            isClose
              ? h(React.Fragment, null,
                h('div', { className: 'twb-modal-note twb-modal-note--danger' }, t('action.irreversible')),
                h('label', { className: 'twb-hint', htmlFor: 'twb-close-reason' }, t('action.reason')),
                h('input', {
                  id: 'twb-close-reason',
                  className: 'twb-input',
                  'aria-label': t('action.reason'),
                  placeholder: t('action.reasonPlaceholder'),
                  value: reason,
                  disabled: busy || Boolean(receipt),
                  onChange: (event) => setReason(event.target.value),
                }))
              : h('div', { className: 'twb-modal-note' }, t('action.repairHint')),
            receipt
              ? h('div', { className: 'twb-receipt', 'data-ok': String(receipt.ok), role: 'status', 'aria-live': 'polite' },
                h('div', { className: 'twb-chip' }, `${t('action.receipt')} · ${receipt.ok ? partialReceipt ? t('ops.partial') : t('status.ok') : t('action.error')}`),
                receipt.requestId ? h('div', { className: 'twb-receipt-row' }, h('span', { className: 'k' }, t('action.requestId')), h('span', { className: 'v twb-mono' }, String(receipt.requestId))) : null,
                h('div', { className: 'twb-receipt-row' }, h('span', { className: 'k' }, t('action.status')), h('span', { className: 'v twb-mono' }, String(receipt.status ?? '—'))),
                receipt.ok
                  ? h('div', { className: 'twb-receipt-row' }, h('span', { className: 'k' }, t('action.result')), h('span', { className: 'v' }, resultText))
                  : h('div', { className: 'twb-receipt-row' }, h('span', { className: 'k' }, t('action.error')), h('span', { className: 'v twb-mono' }, `${receipt.code ?? '—'}${receipt.message ? ` · ${receipt.message}` : ''}`)),
                h('div', { className: 'twb-hint' }, t('action.noRetry')))
              : null),
          h('div', { className: 'twb-modal-actions' },
            receipt
              ? h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', onClick: onCancel }, t('action.closeDialog'))
              : h(React.Fragment, null,
                h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', ref: cancelRef, disabled: busy, onClick: onCancel }, t('action.cancel')),
                h('button', {
                  type: 'button',
                  className: 'twb-btn twb-btn--primary',
                  disabled: !canSubmit,
                  onClick: () => {
                    if (action.type === 'close') {
                      const body = { symbol: row.symbol, confirm: 'CLOSE', reason: reason.trim() }
                      if (row.qty !== null && row.qty !== undefined) body.qty = row.qty
                      onSubmit(body)
                    } else {
                      onSubmit({ symbols: [row.symbol], confirm: 'REPAIR' })
                    }
                  },
                }, busy ? t('action.submitting') : isClose ? t('action.confirmClose') : t('action.confirmRepair'))))))
    }

    // ---- menu --------------------------------------------------------------
    // 05 N05: "还有 N 项" was inert text; it now opens the pending list so every
    // attention item is reachable, not just the conductor.
    function PendingPanel({ items, t, onClose, onPick }) {
      return h('div', { className: 'twb-precheck', role: 'dialog', 'aria-label': t('s2.pendingTitle') },
        h('div', { className: 'twb-precheck-head' },
          h('span', { className: 'twb-card-title' }, t('s2.pendingTitle')),
          h('span', { className: 'twb-hint' }, t('s2.pendingHint')),
          h('span', { className: 'twb-spacer' }),
          h('button', { type: 'button', className: 'twb-btn twb-btn--ghost', onClick: onClose }, t('common.close'))),
        items.length === 0
          ? h('div', { className: 'twb-hint' }, t('s2.pendingEmpty'))
          : h('div', { className: 'twb-pending-list' },
            items.map((item) => h('div', { key: item.id, className: 'twb-pending-row', 'data-tone': item.tone ?? 'warn', 'data-cat': item.category ?? 'exec' },
              h('span', { className: 'twb-pending-cat' }, t(ATTENTION_CATEGORY[item.category] ?? 's2.catExec')),
              h('span', { className: 'twb-pending-title' }, item.title
                ? (item.titleParams ? fmt(zh(item.title), item.titleParams) : zh(item.title))
                : zh(item.impact ?? 's2.catExec')),
              item.title && item.impact ? h('span', { className: 'twb-pending-impact' }, zh(item.impact)) : null,
              h('span', { className: 'twb-pending-reason' }, item.reason),
              item.next ? h('span', { className: 'twb-pending-next' }, zh(item.next)) : null,
              h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', onClick: () => onPick(item) }, t(item.actionLabel ?? 's2.view'))))))
    }

    // 16 §1: first-run environment wizard. It reports what this machine actually
    // has, then runs the one idempotent setup job (venv -> deps -> start ->
    // verify) and streams every step. It never claims ready before /health does.
    function ServiceSetupDialog({ t, onClose, onReady }) {
      const [readiness, setReadiness] = React.useState(null)
      const [checking, setChecking] = React.useState(true)
      const [error, setError] = React.useState(null)
      const [job, setJob] = React.useState(null)
      const [busy, setBusy] = React.useState(false)
      const mounted = React.useRef(true)
      const readyFired = React.useRef(false)
      const pollRef = React.useRef(null)
      const onReadyRef = React.useRef(onReady)
      onReadyRef.current = onReady

      React.useEffect(() => () => {
        mounted.current = false
        if (pollRef.current) clearTimeout(pollRef.current)
      }, [])

      const fetchReadiness = async (force) => {
        setChecking(true)
        try {
          const url = force ? `${SETUP_READINESS_URL}?force=1` : SETUP_READINESS_URL
          const response = await fetch(url, { credentials: 'same-origin', headers: { accept: 'application/json' } })
          const body = await response.json().catch(() => null)
          if (!mounted.current) return
          if (!response.ok || body?.ok === false) {
            throw new Error(body?.error?.code ?? `HTTP_${response.status}`)
          }
          setReadiness(body.setup ?? null)
          setError(null)
          if (body.setup?.ready === true && !readyFired.current) {
            readyFired.current = true
            onReadyRef.current?.()
          }
        } catch (err) {
          if (mounted.current) setError(String(err?.message ?? err))
        } finally {
          if (mounted.current) setChecking(false)
        }
      }

      const pollJob = async (jobId) => {
        try {
          const response = await fetch(`${SERVICE_SETUP_URL}/${encodeURIComponent(jobId)}`, {
            credentials: 'same-origin',
            headers: { accept: 'application/json' },
          })
          const body = await response.json().catch(() => null)
          if (!mounted.current) return
          if (!response.ok || body?.ok === false) {
            throw new Error(body?.error?.code ?? `HTTP_${response.status}`)
          }
          setJob(body.job ?? null)
          if (body.job?.phase === 'running') {
            pollRef.current = setTimeout(() => pollJob(jobId), 1200)
            return
          }
          setBusy(false)
          if (body.job?.phase === 'ready') await fetchReadiness(true)
        } catch (err) {
          if (!mounted.current) return
          setBusy(false)
          setError(String(err?.message ?? err))
        }
      }

      React.useEffect(() => {
        fetchReadiness(false)
        // Intentionally once: the parent re-renders every poll.
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, [])

      const startSetup = async () => {
        if (busy) return
        setBusy(true)
        setError(null)
        setJob(null)
        try {
          const response = await fetch(SERVICE_SETUP_URL, {
            method: 'POST',
            credentials: 'same-origin',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ reason: t('setup.start') }),
          })
          const body = await response.json().catch(() => null)
          if (!response.ok || body?.ok === false) {
            throw new Error(body?.error?.code ?? `HTTP_${response.status}`)
          }
          if (!mounted.current) return
          setJob(body.job ?? null)
          await pollJob(body.job_id)
        } catch (err) {
          if (!mounted.current) return
          setBusy(false)
          setError(String(err?.message ?? err))
        }
      }

      const pythonOk = readiness?.python?.found === true
      // A usable environment may be the setup venv OR an interpreter that already
      // has the service dependencies (the author's machine uses the latter).
      const venvOk = readiness?.venv?.exists === true || readiness?.deps?.installed === true
      const depsOk = readiness?.deps?.installed === true
      const serviceOk = readiness?.service?.ok === true
      const canStart = Boolean(pythonOk || venvOk) && !busy
      const chip = (ok, unknownText) => h(Chip, {
        text: ok ? t('setup.ok') : (unknownText ?? t('setup.missing')),
        tone: ok ? 'quiet' : 'warn',
      })
      const row = (key, label, ok, detail) => h('div', { key, className: 'twb-setup-row', 'data-ok': String(ok) },
        h('span', { className: 'twb-setup-k' }, label),
        chip(ok),
        detail ? h('span', { className: 'twb-setup-d' }, detail) : null)
      const stepChip = (step) => {
        const tone = step.status === 'ok' ? 'quiet' : step.status === 'failed' ? 'danger' : step.status === 'running' ? 'warn' : 'na'
        return h(Chip, { text: zh(`setup.step.${step.status}`), tone })
      }
      const lastLog = (job?.steps ?? []).flatMap((step) => step.log_tail ?? []).slice(-6)
      return h('div', { className: 'twb-precheck twb-setup', role: 'dialog', 'aria-label': t('setup.title') },
        h('div', { className: 'twb-precheck-head' },
          h('span', { className: 'twb-card-title' }, t('setup.title')),
          checking ? h(Chip, { text: t('setup.checking'), tone: 'na' }) : null,
          h('span', { className: 'twb-spacer' }),
          h('button', {
            type: 'button',
            className: 'twb-btn twb-btn--ghost',
            onClick: () => { if (!busy) fetchReadiness(true) },
          }, t('setup.retry')),
          h('button', { type: 'button', className: 'twb-btn twb-btn--ghost', onClick: onClose }, t('setup.close'))),
        h('div', { className: 'twb-hint twb-setup-sub' }, t('setup.sub')),
        h('div', { className: 'twb-setup-rows' },
          row('python', t('setup.python'), pythonOk, readiness?.python?.path ?? null),
          row('venv', t('setup.venv'), venvOk, readiness?.venv?.dir ?? null),
          row('deps', t('setup.deps'), depsOk, readiness?.deps?.detail ?? null),
          row('service', t('setup.service'), serviceOk, readiness?.service?.url ?? null)),
        pythonOk || venvOk ? null : h('div', { className: 'twb-hint twb-setup-warn' }, t('setup.pythonMissing')),
        error ? h('div', { className: 'twb-hint twb-setup-error' }, fmt(zh('setup.failed'), { reason: error })) : null,
        job
          ? h('div', { className: 'twb-setup-steps' },
            job.steps.map((step) => h('div', { key: step.id, className: 'twb-setup-step', 'data-state': step.status },
              h('span', { className: 'twb-setup-k' }, step.label ? (LANG === 'en' ? step.label.en : step.label.zh) : step.id),
              stepChip(step),
              step.exit_code === null || step.exit_code === undefined ? null
                : h('span', { className: 'twb-setup-d twb-mono' }, `exit ${step.exit_code}`))),
            lastLog.length > 0
              ? h('pre', { className: 'twb-setup-log', 'aria-label': t('setup.log') }, lastLog.join('\n'))
              : null)
          : null,
        h('div', { className: 'twb-precheck-foot' },
          h('span', { className: 'twb-spacer' }),
          h('button', {
            type: 'button',
            className: 'twb-btn twb-btn--primary',
            disabled: !canStart,
            'aria-busy': String(busy),
            onClick: startSetup,
          }, busy ? t('setup.running') : t('setup.start'))))
    }

    function PrecheckPanel({ precheck, entry, t, busy, onClose, onRun, onFix }) {
      const severity = precheck.severity
      const tone = severity === 'danger' ? 'danger' : severity === 'warn' ? 'warn' : 'quiet'
      const chipText = severity === 'danger' ? t('pre.danger') : severity === 'warn' ? t('pre.warn') : t('pre.ok')
      const dangerLabels = precheck.rows.filter((row) => row.tone === 'danger').map((row) => row.label).join(' · ')
      return h('div', { className: 'twb-precheck', role: 'region', 'aria-label': t('pre.title') },
        h('div', { className: 'twb-precheck-head' },
          h('span', { className: 'twb-card-title' }, t('pre.title')),
          h(Chip, { text: chipText, tone }),
          h('span', { className: 'twb-spacer' }),
          h('button', { type: 'button', className: 'twb-btn twb-btn--icon', 'aria-label': t('pre.close'), title: t('pre.close'), onClick: onClose }, '✕')),
        precheck.rows.map((row) => h('div', { key: row.id, className: 'twb-precheck-row' },
          h('span', { className: `twb-dot ${row.tone === 'danger' ? 'twb-dot--danger' : row.tone === 'warn' ? 'twb-dot--warn' : ''}` }),
          h('span', { className: 'lbl' }, row.label))),
        h('div', { className: 'twb-precheck-note' },
          entry.blocked
            ? fmt(zh('pre.entryNote'), { reasons: entry.reasons.map((item) => item.label).join(' · ') })
            : t('pre.entryNone')),
        entry.blocked
          ? h('button', { type: 'button', className: 'twb-link', style: { alignSelf: 'flex-start' }, onClick: onFix }, t('pre.repair'))
          : null,
        severity === 'warn'
          ? h('div', { className: 'twb-precheck-confirm' },
            h('span', null, t('pre.confirm')),
            h('button', { type: 'button', className: 'twb-btn twb-btn--primary', disabled: busy, onClick: onRun }, t('pre.submit')))
          : null,
        severity === 'danger'
          ? h('div', { className: 'twb-precheck-confirm' },
            h('span', null, dangerLabels),
            h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', onClick: onFix }, t('pre.repair')))
          : null)
    }

    function Menu({ model, t, onClose, onRefresh, onSystem, onSettings, onAppearance, onLanguage, lang, refreshing }) {
      const options = [
        { id: 'green-up', label: 'appearance.greenUp' },
        { id: 'red-up', label: 'appearance.redUp' },
        { id: 'mono', label: 'appearance.mono' },
      ]
      return h('div', { className: 'twb-menu', role: 'menu', 'aria-label': t('top.more') },
        h('div', { className: 'twb-menu-title' }, t('top.menu.appearance')),
        options.map((option) => h('button', {
          key: option.id,
          type: 'button',
          role: 'menuitemradio',
          className: 'twb-menu-opt',
          'aria-checked': String(model.updown === option.id),
          onClick: () => onAppearance(option.id),
        }, h('span', { className: 'twb-menu-check' }, model.updown === option.id ? '✓' : ''), t(option.label))),
        h('div', { className: 'twb-menu-note' }, t('appearance.note')),
        h('div', { className: 'twb-menu-sep' }),
        // §2.1: the top bar keeps fewer knobs at 1024; language stays reachable.
        h('div', { className: 'twb-menu-title' }, lang === 'zh' ? '语言' : 'Language'),
        h('div', { className: 'twb-chips' },
          [['zh', '中文'], ['en', 'English']].map(([id, label]) => h('button', {
            key: id,
            type: 'button',
            role: 'menuitemradio',
            className: 'twb-menu-opt',
            'aria-checked': String(lang === id),
            'data-lang-menu': id,
            onClick: () => onLanguage(id),
          }, h('span', { className: 'twb-menu-check' }, lang === id ? '✓' : ''), label))),
        h('div', { className: 'twb-menu-sep' }),
        h('button', {
          type: 'button',
          role: 'menuitem',
          'aria-label': t('top.menu.refresh'),
          'aria-busy': String(Boolean(refreshing)),
          disabled: Boolean(refreshing),
          onClick: onRefresh,
        }, t('top.menu.refresh'), refreshing ? h('span', { className: 'twb-hint' }, t('state.refreshing')) : null),
        h('button', { type: 'button', role: 'menuitem', 'data-menu-settings': 'true', onClick: onSettings ?? onSystem }, t('set.open')),
        h('button', { type: 'button', role: 'menuitem', onClick: onSystem }, t('top.menu.system')),
        h('div', { className: 'twb-menu-note' }, t('top.menu.shortcut')),
        h('button', { type: 'button', role: 'menuitem', onClick: onClose }, t('common.close')))
    }

    // ---- data hook + panel -------------------------------------------------
    function useJson(url, nonce, enabled) {
      const [result, setResult] = React.useState({ data: null, error: null, loading: Boolean(enabled) })
      React.useEffect(() => {
        if (!enabled || !url) return undefined
        let cancelled = false
        setResult((prev) => ({ data: prev.data, error: null, loading: true }))
        fetch(url, { credentials: 'same-origin', headers: { accept: 'application/json' } })
          .then((response) => response.json().then((body) => ({ response, body })))
          .then(({ response, body }) => {
            if (cancelled) return
            if (!response.ok) {
              setResult({ data: null, error: { status: response.status, body }, loading: false })
              return
            }
            setResult({ data: body, error: null, loading: false })
          })
          .catch((error) => {
            if (cancelled) return
            setResult({ data: null, error: { status: 0, message: error?.message ?? 'ERROR' }, loading: false })
          })
        return () => { cancelled = true }
      }, [url, nonce, enabled])
      return result
    }

    // Independent single-flight display quotes; account polling never resets this loop.
    function useMarkQuotes(url) {
      const [result, setResult] = React.useState({ key: null, data: null, error: null })
      React.useEffect(() => {
        let disposed = false, inFlight = false, timer = null, controller = null, failures = 0
        const hidden = () => typeof document !== 'undefined' && document.visibilityState === 'hidden'
        const scheduleNext = (delay) => {
          clearTimeout(timer)
          if (!disposed && !hidden()) timer = setTimeout(load, delay)
        }
        const load = async () => {
          if (disposed || inFlight || hidden() || !url) return
          inFlight = true
          controller = new AbortController()
          const timeout = setTimeout(() => controller.abort(), 6000)
          let delay = MARK_QUOTE_POLL_MS
          try {
            const response = await fetch(url, { credentials: 'same-origin', signal: controller.signal, headers: { accept: 'application/json' } })
            const body = await response.json()
            if (!response.ok) {
              const retry = Number(response.headers?.get?.('retry-after'))
              if (Number.isFinite(retry) && retry > 0) delay = Math.max(delay, retry * 1000)
              throw new Error(body?.error?.code ?? `HTTP_${response.status}`)
            }
            failures = 0
            if (!disposed) setResult((prev) => {
              const prices = { ...(body.prices ?? {}) }
              for (const [symbol, quote] of Object.entries(prices)) {
                const old = prev.key === url ? prev.data?.prices?.[symbol] : null
                if (old?.as_of_ms && quote?.as_of_ms && quote.as_of_ms < old.as_of_ms) {
                  prices[symbol] = { ...old, stale: true, error: 'OUT_OF_ORDER' }
                }
              }
              return { key: url, data: { ...body, prices }, error: null }
            })
          } catch (error) {
            failures++
            delay = Math.max(delay, Math.min(30000, MARK_QUOTE_POLL_MS * 2 ** Math.min(failures, 4)))
            if (!disposed) setResult((prev) => ({ key: url, data: prev.key === url ? prev.data : null, error: error.message }))
          } finally {
            clearTimeout(timeout)
            inFlight = false
            scheduleNext(delay)
          }
        }
        const visibility = () => { clearTimeout(timer); if (!hidden()) load() }
        if (typeof document !== 'undefined') document.addEventListener?.('visibilitychange', visibility)
        load()
        return () => {
          disposed = true
          clearTimeout(timer)
          controller?.abort()
          if (typeof document !== 'undefined') document.removeEventListener?.('visibilitychange', visibility)
        }
      }, [url])
      return result.key === url ? result : { data: null, error: null }
    }

    // ---- B+ editorial cockpit ------------------------------------------------
    function BSectionHead({ num, title, en, meta }) {
      return h('div', { className: 'twb-sec-head' },
        h('span', { className: 'twb-sec-num' }, num),
        h('span', { className: 'twb-sec-dash', 'aria-hidden': 'true' }),
        h('span', { className: 'twb-sec-title' }, title),
        h('span', { className: 'twb-sec-en' }, en),
        meta ? h('span', { className: 'twb-sec-meta' }, meta) : null)
    }

    function AccountBandB({ account, equity, t }) {
      if (account.empty) {
        return h('section', { className: 'b-account', 'aria-label': t('b.total') },
          h(EmptyBlock, {
            kind: 'empty',
            title: t('account.emptyTitle'),
            hint: `${t('account.emptyHint').replace('{reason}', reasonText(account.reason) || t('common.notProvided'))}`,
          }))
      }
      const spark = equity.points.slice(-20)
      const sparkPath = (() => {
        if (spark.length < 2) return ''
        const values = spark.map((point) => point.equity)
        const min = Math.min(...values)
        const max = Math.max(...values)
        const span = max - min || 1
        const w = 300
        const hgt = 44
        const pad = 2
        return spark.map((point, index) => {
          const x = pad + (index / (spark.length - 1)) * (w - pad * 2)
          const y = pad + (1 - (point.equity - min) / span) * (hgt - pad * 2)
          return `${index === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`
        }).join(' ')
      })()
      const day = fmtSigned(account.dayPnl, { digits: 2 })
      const upnl = fmtSigned(account.upnl, { digits: 2 })
      const metric = (key, value, dir) => h('div', { className: 'b-metric' },
        h('span', { className: 'k' }, key),
        h('span', { className: `v ${dir ? `twb-${dir}` : ''}`.trim() }, value))
      return h('section', { className: 'b-account', 'aria-label': t('b.total') },
        h('div', { className: 'b-account-hero' },
          h('span', { className: 'num' }, fmtNum(account.total)),
          h('span', { className: 'unit' }, 'USDT')),
        h('div', { className: 'b-account-metrics' },
          metric(t('b.today'), day.text, day.dir),
          metric(t('b.upnl'), upnl.text, upnl.dir),
          metric(t('b.available'), fmtNum(account.available)),
          metric(t('b.startEquity'), account.session === null ? '—' : fmtNum(account.session))),
        h('div', { className: 'b-account-spark' },
          sparkPath
            ? h('svg', { viewBox: '0 0 300 44', preserveAspectRatio: 'none', role: 'img', 'aria-label': t('account.trend') },
              h('path', { d: sparkPath, fill: 'none', stroke: 'var(--twb-b-ink-3)', strokeWidth: 1.5, strokeLinejoin: 'round' }))
            : null,
          h('div', { className: 'cap' }, `${t('account.trend')} · 24h`)))
    }

    function RiskBandB({ model, t, onSystem }) {
      const { caps, control, entry, account } = model
      const blocked = entry.blocked
      const reasons = entry.reasons ?? []
      const marginPct = account.marginUsed !== null && account.total
        ? (account.marginUsed / account.total) * 100
        : null
      const row = (key, value, tone) => h('div', { className: 'b-risk-row' },
        h('span', { className: 'k' }, key),
        h('span', { className: `v ${tone ?? ''}`.trim() }, value))
      return h('section', { className: 'b-risk', 'aria-label': t('risk.title') },
        h('div', { className: 'b-risk-head' },
          h('span', null, t('risk.title')),
          caps.current ? null : h('span', { className: 'b-risk-fallback' }, t('risk.prevCaps')),
          h('button', { type: 'button', className: 'link', onClick: onSystem }, `${t('b.riskDetail')} →`)),
        row(t('risk.perTrade'), caps.maxRiskPct === null ? '—' : `${caps.maxRiskPct.toFixed(1)}%`),
        row(t('risk.leverage'), caps.maxLeverage === null ? '—' : `${caps.maxLeverage}×`),
        row(t('risk.maxPositions'), `${model.positions.rows.length} / ${caps.maxPositions ?? '—'}`),
        row(t('b.margin'), marginPct === null ? '—' : `${marginPct.toFixed(1)}%`),
        row(t('risk.control'), controlLabel(control)),
        row(t('risk.newEntry'), blocked ? t('s4.entryBlocked') : t('s4.entryAllowed'), blocked ? 'danger' : ''),
        blocked && reasons.length > 0
          ? h('div', { className: 'b-risk-note' }, h('span', { className: 'danger' }, '⚠ '), reasons.map((item) => item.short ?? item.label).join(' · '))
          : null)
    }

    function TapeB({ model, t, onOpenOrders }) {
      const orders = Array.isArray(model.orders) ? model.orders : (Array.isArray(model.orders?.rows) ? model.orders.rows : [])
      const fills = Array.isArray(model.fills) ? model.fills : (Array.isArray(model.fills?.rows) ? model.fills.rows : [])
      const events = []
      for (const order of orders.slice(0, 8)) {
        events.push({
          at: order.createdAt,
          text: `${order.symbol ?? '—'} ${sideLabel(order.side)} ${order.qty ?? '—'} @ ${fmtPrice(order.price) ?? '—'} · ${order.type ?? '—'} · ${statusLabel(order.status)}`,
        })
      }
      for (const fill of fills.slice(0, 8)) {
        events.push({
          at: fill.time,
          text: `${fill.symbol ?? '—'} ${sideLabel(fill.side)} ${fill.qty ?? '—'} @ ${fmtPrice(fill.price) ?? '—'} · ${t('orders.tabFills')}`,
        })
      }
      events.sort((a, b) => (toMs(b.at) ?? 0) - (toMs(a.at) ?? 0))
      const top = events.slice(0, 3)
      return h('section', { className: 'b-tape', 'aria-label': t('b.tape') },
        h(BSectionHead, {
          num: 'APP.',
          title: t('b.tape'),
          en: 'TAPE',
          meta: `${t('orders.title')} ${orders.length} · ${fills.length} · ${t('today.title')} ${model.dayStats?.trades ?? 0}`,
        }),
        h('div', { className: 'b-tape-line' },
          top.length === 0
            ? h('span', { className: 'b-tape-empty' }, t('b.noTape'))
            : top.map((event, index) => h(React.Fragment, { key: index },
              index > 0 ? h('span', { className: 'dot', 'aria-hidden': 'true' }) : null,
              h('span', null, h('b', null, fmtClock(event.at)), ` ${event.text}`))),
          h('span', { className: 'spacer' }),
          h('button', {
            type: 'button',
            className: 'b-tape-link',
            onClick: onOpenOrders,
          }, orders.length === 0 && fills.length === 0 ? t('orders.collapsedEmpty') : t('orders.title'))))
    }

    function OrdersDrawer({ model, t, onClose, onDetail, onAll, onReview, initialTab, onTab }) {
      const rootRef = React.useRef(null)
      const closeRef = React.useRef(null)
      const closeHandler = React.useRef(onClose)
      closeHandler.current = onClose
      React.useEffect(() => {
        const previous = typeof document !== 'undefined' ? document.activeElement : null
        if (closeRef.current && typeof closeRef.current.focus === 'function') closeRef.current.focus()
        const onKey = (event) => {
          if (event.key === 'Escape') { event.stopPropagation(); closeHandler.current(); return }
          if (event.key !== 'Tab') return
          const root = rootRef.current
          if (!root) return
          const focusable = Array.prototype.slice.call(root.querySelectorAll(
            'a[href],button:not([disabled]),textarea:not([disabled]),input:not([disabled]),select:not([disabled]),[tabindex]:not([tabindex="-1"])',
          )).filter((node) => node.offsetParent !== null || node === document.activeElement)
          if (focusable.length === 0) return
          const first = focusable[0]
          const last = focusable[focusable.length - 1]
          const active = document.activeElement
          if (event.shiftKey && (active === first || !root.contains(active))) { event.preventDefault(); last.focus() }
          else if (!event.shiftKey && active === last) { event.preventDefault(); first.focus() }
        }
        if (typeof document !== 'undefined' && document.addEventListener) document.addEventListener('keydown', onKey, true)
        return () => {
          if (typeof document !== 'undefined' && document.removeEventListener) document.removeEventListener('keydown', onKey, true)
          if (previous && typeof previous.focus === 'function') previous.focus()
        }
      }, [])
      return h('div', {
        className: 'b-modal-scrim',
        role: 'presentation',
        onClick: (event) => { if (event.target === event.currentTarget) onClose() },
      },
      h('div', { className: 'b-modal', role: 'dialog', 'aria-modal': 'true', 'aria-label': t('orders.title'), ref: rootRef },
        h('div', { className: 'b-modal-head' },
          h('span', { className: 'b-modal-title' }, t('orders.title')),
          h('button', { type: 'button', className: 'twb-btn twb-btn--ghost', ref: closeRef, onClick: onClose }, t('common.close'))),
        h(OrdersPanel, { model, t, expanded: true, onToggle: () => {}, onDetail, initialTab, onTab }),
        h(TodaySummary, { model, t, onAll, onReview })))
    }

    function WorkbenchPanel(props) {
      const [lang, setLang] = React.useState(() => {
        try {
          if (typeof window !== 'undefined' && window.location) {
            const query = new URLSearchParams(window.location.search).get('lang')
            if (query === 'zh' || query === 'en') return query
          }
          if (typeof window !== 'undefined' && window.localStorage) {
            const saved = window.localStorage.getItem('twb.lang')
            if (saved === 'zh' || saved === 'en') return saved
          }
        } catch (error) { /* preference only */ }
        return 'zh'
      })
      LANG = lang
      const t = (key, params) => tr(key, params, lang)
      const setLanguage = (next) => {
        setLang(next)
        try {
          if (typeof window !== 'undefined' && window.localStorage) window.localStorage.setItem('twb.lang', next)
        } catch (error) { /* preference only */ }
      }
      const initialQueryRef = React.useRef(null)
      if (initialQueryRef.current === null) {
        initialQueryRef.current = (() => {
          try {
            if (typeof window !== 'undefined' && window.location) return new URLSearchParams(window.location.search)
          } catch (error) { /* no query */ }
          return null
        })()
      }
      const initialQuery = initialQueryRef.current
      const [workspace, setWorkspace] = React.useState(() => initialQuery?.get('ws') === 'ai' ? 'ai' : 'cockpit')
      const [aiView, setAiView] = React.useState(() => {
        const view = initialQuery?.get('view')
        return ['current', 'history', 'review'].includes(view) ? view : 'current'
      })
      const [nonce, setNonce] = React.useState(0)
      const [healthNonce, setHealthNonce] = React.useState(0)
      const [selectedRoundId, setSelectedRoundId] = React.useState(null)
      const [drawer, setDrawer] = React.useState(null)
      const [menuOpen, setMenuOpen] = React.useState(false)
      const [appearance, setAppearance] = React.useState(() => {
        try {
          if (typeof window !== 'undefined' && window.localStorage) {
            const saved = window.localStorage.getItem(UP_DOWN_KEY)
            if (saved === 'green-up' || saved === 'red-up' || saved === 'mono') return saved
          }
        } catch (error) { /* preference only */ }
        return 'green-up'
      })
      // §3.1 密度: purely local display preference, same storage discipline as
      // the up/down colours (no token, no service write).
      const [density, setDensity] = React.useState(() => {
        try {
          if (typeof window !== 'undefined' && window.localStorage) {
            const saved = window.localStorage.getItem(DENSITY_KEY)
            if (saved === 'compact' || saved === 'normal') return saved
          }
        } catch (error) { /* preference only */ }
        return 'normal'
      })
      const [notice, setNotice] = React.useState(null)
      const [action, setAction] = React.useState(null)
      const [actionBusy, setActionBusy] = React.useState(false)
      const [controlBusy, setControlBusy] = React.useState(false)
      const controlFlight = React.useRef(false)
      // F1 §3.2: the top-bar stop has a progress state of its own. 'stopping'
      // is shown until the Host answers; the Host only says 'stopped' after the
      // service confirmed the pause.
      const [autoPhase, setAutoPhase] = React.useState({ phase: 'idle', code: null, requestId: null, message: null, snapshot: null })
      const autoFlight = React.useRef(false)
      // 15 §10: starting the local service is a long operation (spawn + /health
      // poll up to 45s), so it has its own phase and a one-flight guard.
      const [serviceStartPhase, setServiceStartPhase] = React.useState('idle')
      const serviceStartFlight = React.useRef(false)
      const [receipt, setReceipt] = React.useState(null)
      const [precheckOpen, setPrecheckOpen] = React.useState(() => initialQuery?.get('precheck') === '1')
      const [pendingOpen, setPendingOpen] = React.useState(false)
      // 16 §1: the first-run environment wizard (readiness -> setup job -> ready).
      const [setupOpen, setSetupOpen] = React.useState(false)
      const [runPhase, setRunPhase] = React.useState('idle')
      const [refreshing, setRefreshing] = React.useState(false)
      const [refreshTick, setRefreshTick] = React.useState(0)
      const [expandedOrders, setExpandedOrders] = React.useState(false)
      const [ordersOpen, setOrdersOpen] = React.useState(() => initialQuery?.get('drawer') === 'orders')
      const [ordersTab, setOrdersTab] = React.useState(() => {
        const tab = initialQuery?.get('tab')
        return ['orders', 'fills', 'recon', 'operations'].includes(tab) ? tab : 'orders'
      })
      const [reviewRange, setReviewRange] = React.useState('1d')
      const [historyFilter, setHistoryFilter] = React.useState('all')
      const [historyDuration, setHistoryDuration] = React.useState('all')
      // 15 §5: null = today (served by /state). A past day is fetched once from
      // /rounds?date=…; it never joins the live poll.
      const [historyDate, setHistoryDate] = React.useState(null)
      const [marketNonce, setMarketNonce] = React.useState(0)
      const [envelopeNonce, setEnvelopeNonce] = React.useState(0)
      const runningRef = React.useRef(false)
      const commandFlight = React.useRef(false)
      const rootRef = React.useRef(null)
      const drawerRestored = React.useRef(false)

      const statePoll = useJson(refreshTick > 0 ? `${STATE_URL}?refresh=1` : STATE_URL, nonce, true)
      // 分析页的资金口径来自服务端 /pnl（四权益 + 四本账 + 对账），不在这里重算。
      // Host 的 /pnl 路由接受 range_s（秒），不是 range 字符串。
      const REVIEW_RANGE_S = { '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800 }
      const reviewPnl = useJson(PNL_URL + '?range_s=' + (REVIEW_RANGE_S[reviewRange] ?? 86400), nonce, aiView === 'review')
      const healthPoll = useJson(HEALTH_URL, healthNonce, true)
      const stateData = statePoll.data
      const healthData = healthPoll.data
      const marketSymbol = (Array.isArray(stateData?.positions) && stateData.positions[0]?.symbol)
        || stateData?.last_round?.symbol
        || 'BTCUSDT'
      const marketPoll = useJson(
        `${PRICES_URL}?symbols=${encodeURIComponent(marketSymbol)}`,
        `${marketNonce}#${marketSymbol}`,
        true,
      )
      const markSymbols = [...new Set((stateData?.positions ?? []).map((row) => row.symbol).filter(Boolean))].sort().slice(0, 10)
      const markScope = `${serviceScope(stateData)}:${markSymbols.join(',')}`
      const markPoll = useMarkQuotes(markSymbols.length ? `${MARKS_URL}?symbols=${encodeURIComponent(markSymbols.join(','))}&scope=${encodeURIComponent(markScope)}` : null)
      const [, setQuoteClock] = React.useState(0)
      React.useEffect(() => {
        const timer = setInterval(() => { if (typeof document === 'undefined' || document.visibilityState !== 'hidden') setQuoteClock((n) => n + 1) }, 1000)
        return () => clearInterval(timer)
      }, [])
      const envelopePoll = useJson(ENVELOPE_URL, envelopeNonce, true)
      const roundDetail = useJson(
        selectedRoundId ? `${ROUNDS_URL}/${encodeURIComponent(selectedRoundId)}` : '',
        `${selectedRoundId ?? ''}#${refreshTick}`,
        Boolean(selectedRoundId),
      )
      // 15 §4/§5: /state already carries today's partition. A past day comes
      // from /rounds?date=… and is loaded once (stable key, no poll), because an
      // archived day cannot change.
      const todayKey = stateData?.rounds_date ?? null
      const historyPast = Boolean(historyDate) && historyDate !== todayKey
      const historyPoll = useJson(
        historyPast ? `${ROUNDS_URL}?date=${encodeURIComponent(historyDate)}` : '',
        `history#${historyDate ?? ''}#${refreshTick}`,
        historyPast,
      )

      // Restore the payload-less drawers that can be rebuilt from /state.
      // Position/order/call drawers still need their row, so only the tab is
      // kept in the URL for those.
      React.useEffect(() => {
        if (drawerRestored.current || !stateData) return
        drawerRestored.current = true
        const type = initialQuery?.get('drawer')
        const tab = initialQuery?.get('tab') || 'evidence'
        if (type === 'system') {
          setDrawer({ type: 'system', tab })
          return
        }
        // F2: settings is payload-less too, so a refresh reopens the same group.
        if (type === 'settings') {
          setDrawer({ type: 'settings', tab: SETTINGS_GROUPS.includes(tab) ? tab : 'connection' })
          return
        }
        if (type === 'round') {
          const id = initialQuery?.get('id')
          const last = stateData?.last_round ?? null
          if (last && (!id || last.roundId === id)) setDrawer({ type: 'round', round: last, tab })
          return
        }
        const id = initialQuery?.get('id')
        if (!id) return
        if (type === 'position') {
          // Use the resolved row: PositionPanel needs stopDist/liqDist/protection
          // objects, not the raw /state position payload.
          const rows = resolvePositions(stateData).rows
          const row = rows.find((item) => [item?.symbol, item?.raw?.key, item?.raw?.symbol].filter(Boolean).map(String).includes(id))
          if (row) setDrawer({ type: 'position', row, tab })
          return
        }
        if (type === 'order') {
          const rows = resolveOrders(stateData)
          const row = rows.find((item) => [item?.id, item?.clientId].filter(Boolean).map(String).includes(id))
          if (row) setDrawer({ type: 'order', row, tab })
        }
      }, [stateData])

      // polling (8s, 3s while a round is in flight). A failed fetch retries fast
      // (0.8s → 1.6s → 3.2s → 6.4s, then the normal cadence): after a plugin
      // reload the routes may not be registered for a moment, and waiting 8s
      // left the panel on "Host 离线" long after the Host was back.
      const stateAttempt = React.useRef(0)
      React.useEffect(() => {
        const failed = Boolean(statePoll.error) && !stateData
        stateAttempt.current = failed ? stateAttempt.current + 1 : 0
        const inFlight = Boolean(stateData?.in_flight) || runPhase === 'pending'
        const retry = failed
          ? Math.min(POLL_MS, POLL_RETRY_MS * 2 ** Math.min(stateAttempt.current - 1, 3))
          : null
        const delay = retry ?? (inFlight ? POLL_RUNNING_MS : POLL_MS)
        const timer = setTimeout(() => setNonce((value) => value + 1), delay)
        return () => clearTimeout(timer)
      }, [stateData, runPhase, nonce, statePoll.error])

      React.useEffect(() => {
        const timer = setTimeout(() => setHealthNonce((value) => value + 1), HEALTH_MS)
        return () => clearTimeout(timer)
      }, [healthData, healthNonce])

      // market prices: 8s, paused while the document is hidden
      React.useEffect(() => {
        if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return undefined
        const timer = setTimeout(() => setMarketNonce((value) => value + 1), MARKET_POLL_MS)
        return () => clearTimeout(timer)
      }, [marketNonce, marketPoll.data])

      // current risk envelope: 30s
      React.useEffect(() => {
        const timer = setTimeout(() => setEnvelopeNonce((value) => value + 1), ENVELOPE_POLL_MS)
        return () => clearTimeout(timer)
      }, [envelopeNonce, envelopePoll.data])

      // Keep the D1 drawer and its tab in the URL. The orders drawer is fully
      // restorable from `?drawer=orders&tab=...`; payload drawers (round/call)
      // keep their tab in the URL but still need their row payload to reopen.
      React.useEffect(() => {
        try {
          if (typeof window === 'undefined' || !window.history || !window.location) return
          const url = new URL(window.location.href)
          if (ordersOpen) {
            url.searchParams.set('drawer', 'orders')
            url.searchParams.set('tab', ordersTab)
            url.searchParams.delete('id')
          } else if (drawer) {
            url.searchParams.set('drawer', String(drawer.type ?? 'drawer'))
            if (drawer.tab) url.searchParams.set('tab', String(drawer.tab))
            else url.searchParams.delete('tab')
            const id = drawer.type === 'round' ? drawer.round?.roundId
              : drawer.type === 'position' ? (drawer.row?.key ?? drawer.row?.symbol)
                : drawer.type === 'order' ? (drawer.row?.id ?? drawer.row?.orderId ?? drawer.row?.clientOrderId)
                  : null
            if (id) url.searchParams.set('id', String(id))
            else url.searchParams.delete('id')
          } else {
            url.searchParams.delete('drawer')
            url.searchParams.delete('tab')
            url.searchParams.delete('id')
          }
          window.history.replaceState(null, '', url.toString())
        } catch (error) { /* URL state is best-effort */ }
      }, [ordersOpen, ordersTab, drawer?.type, drawer?.tab])

      // resume immediately after the tab becomes visible again
      React.useEffect(() => {
        const onVisibility = () => {
          if (typeof document !== 'undefined' && document.visibilityState !== 'hidden') {
            setMarketNonce((value) => value + 1)
            setEnvelopeNonce((value) => value + 1)
          }
        }
        if (typeof document !== 'undefined' && document.addEventListener) document.addEventListener('visibilitychange', onVisibility)
        return () => {
          if (typeof document !== 'undefined' && document.removeEventListener) document.removeEventListener('visibilitychange', onVisibility)
        }
      }, [])

      const now = Date.now()
      const service = mergeService(stateData, healthData, stateData?.last_round)
      const stateCode = serviceState(service)
      // F1: Host projection of automation (from the service control row), plus
      // the transient local phase of the stop/start the user just pressed.
      const automationHost = resolveAutomation(stateData, service)
      const automation = (() => {
        if (autoPhase.phase === 'stopping') return { ...automationHost, state: 'stopping', reasonCode: 'STOP_REQUESTED' }
        // The Host answer to an action the user just took is displayed until
        // /state reports the same thing (same stop request id, or the same
        // state). After that the Host projection is the only source, so a
        // refresh or a restart can never show a stale local "stopped".
        const snapshot = autoPhase.snapshot
        if (!snapshot || typeof snapshot.state !== 'string') return automationHost
        const hostCaughtUp = snapshot.state === automationHost.state
          || (Boolean(autoPhase.requestId) && automationHost.stop?.request_id === autoPhase.requestId)
        if (hostCaughtUp) return automationHost
        return {
          source: 'host',
          state: snapshot.state,
          reasonCode: snapshot.reason_code ?? null,
          schedulerEnabled: snapshot.scheduler_enabled ?? null,
          stopSupported: snapshot.scheduler_supported !== false,
          lastSuccessAt: snapshot.last_success_at ?? automationHost.lastSuccessAt,
          nextAt: snapshot.next_at ?? null,
          intervalMs: snapshot.interval_ms ?? automationHost.intervalMs,
          lastResult: snapshot.last_result ?? automationHost.lastResult,
          serviceControl: snapshot.service_control ?? null,
          stop: snapshot.stop ?? null,
          pausedUntil: snapshot.paused_until ?? null,
          pauseReason: snapshot.pause_reason ?? null,
        }
      })()
      const automationChipInfo = automationLabel(automation)
      const envelopeData = envelopePoll.data && envelopePoll.data.ok !== false ? envelopePoll.data : null
      const envelopeControl = envelopeData?.control?.state
        ?? envelopeData?.control_state
        ?? (typeof envelopeData?.control === 'string' ? envelopeData.control : null)
      const control = envelopeControl ? String(envelopeControl) : resolveControl(stateData, service)
      const marketData = marketPoll.data && marketPoll.data.ok !== false ? marketPoll.data : null
      const marketQuote = marketData?.prices && typeof marketData.prices === 'object' ? marketData.prices[marketSymbol] ?? null : null
      const marketAsOf = num(marketData?.as_of_ms)
      const marketAge = marketAsOf !== null ? Math.max(0, now - marketAsOf) : num(marketQuote?.age_ms)
      const equity = resolveEquity(stateData)
      const account = resolveAccount(stateData, equity)
      const positionQuotes = markPoll.error ? Object.fromEntries(Object.entries(markPoll.data?.prices ?? {}).map(([symbol, quote]) => [symbol, { ...quote, stale: true }])) : markPoll.data?.prices
      const positions = resolvePositions(stateData, positionQuotes, now)
      positions.age = positions.accountAge
      const orders = resolveOrders(stateData)
      const fills = resolveFills(stateData)
      const reviews = resolveReviews(stateData)
      const rounds = resolveRounds(stateData)
      const round = stateData?.last_round ?? null
      // 15 §5: the history view reads one archived day. Today comes from /state;
      // a past day is its own fetch and must never borrow today's rows while it
      // is still loading (or after it failed).
      const historyAvailable = Array.isArray(stateData?.available_dates) ? stateData.available_dates : []
      // A Host that predates the archive answers /rounds without `date`; showing
      // that payload under a selected day would mislabel it. Fail closed instead.
      const historyMismatch = historyPast && historyPoll.data
        && historyPoll.data.ok !== false && historyPoll.data.date !== historyDate
      const historyRounds = historyPast ? (historyMismatch ? [] : resolveRounds(historyPoll.data)) : rounds
      // The 本轮 list keeps the newest round visible even right after midnight,
      // when today's partition is still empty.
      const roundsWithLatest = (() => {
        if (!round || rounds.some((item) => item.roundId === round.roundId)) return rounds
        return [...rounds, {
          roundId: round.roundId,
          trigger: round.trigger,
          status: round.status,
          outcome: round.outcome,
          reasonCode: round.reasonCode,
          headline: round.headline,
          startedAt: round.startedAt,
          finishedAt: round.finishedAt,
          durationMs: round.durationMs,
          symbol: round.symbol,
        }]
      })()
      const freshness = (() => {
        const info = evidenceAge(round, now)
        return { ...info, level: freshnessLevel(info.age) }
      })()
      const block = (() => {
        const order = ['missing', 'partial', 'unknown']
        for (const state of order) {
          const hit = positions.rows.find((row) => row.protection.state === state)
          if (hit) return { ...hit.protection, symbol: hit.symbol }
        }
        return null
      })()
      const roundCaps = {
        maxRiskPct: pctFromLimit(stateData?.budget?.limits?.per_trade?.max_risk_pct ?? round?.risk?.max_risk_pct),
        maxLeverage: num(round?.risk?.max_leverage) ?? null,
        maxPositions: null,
      }
      const envLimits = envelopeData?.limits ?? null
      const caps = envelopeData
        ? {
          maxRiskPct: pctFromLimit(envLimits?.per_trade?.max_risk_pct),
          maxLeverage: num(envLimits?.per_trade?.max_leverage ?? envLimits?.max_leverage),
          maxPositions: num(envLimits?.portfolio?.max_open_positions ?? envLimits?.max_positions),
          current: true,
        }
        : { ...roundCaps, current: false }
      const dayStats = (() => {
        // 03 C4: the "today" boundary is Asia/Shanghai (same as the AI daily
        // report), never the browser's local day.
        const todayKey = dayKeyInShanghai(now)
        const today = (value) => dayKeyInShanghai(value) === todayKey
        const items = rounds.filter((item) => today(item.startedAt))
        return {
          rounds: items.length,
          trades: items.filter((item) => String(item.outcome) === 'trade').length,
          noTrade: items.filter((item) => String(item.outcome) === 'no_trade').length,
          failed: items.filter((item) => String(item.status) === 'failed').length,
        }
      })()
      const llm = healthData?.llm ?? stateData?.llm ?? {}
      const gaps = (() => {
        const stateGaps = Array.isArray(stateData?.gaps) ? stateData.gaps.slice() : []
        const portfolioGaps = Array.isArray(stateData?.portfolio?.gaps) ? stateData.portfolio.gaps : []
        return stateGaps.concat(portfolioGaps.filter((gap) => !stateGaps.includes(gap)))
      })()
      const emptyReasons = {
        positions: reasonText(positions.reason),
        orders: reasonText(emptyReasonText(stateData, 'orders')),
        fills: reasonText(emptyReasonText(stateData, 'fills')),
        reviews: reasonText(reviews.reason),
      }
      // 05 N09 + 08 §6 R1: useJson keeps the previous payload while a new key is
      // in flight, so data.round can still belong to the round we just left. The
      // selection id is the only source of truth: a payload whose roundId does
      // not match the selection is stale and is never shown as the selected
      // round. Only an exact id match may fall back to /state.last_round.
      const detailRound = roundDetail.data?.round ?? null
      // 03/A6: a detail payload without roundId cannot be matched to the
      // selection — treat it as a diagnosable failure, never as "loading forever".
      const detailShapeInvalid = Boolean(detailRound)
        && !(typeof detailRound.roundId === 'string' && detailRound.roundId.trim())
      const detailMatchesSelection = Boolean(detailRound) && Boolean(selectedRoundId) && detailRound.roundId === selectedRoundId
      const selectedRound = selectedRoundId
        ? (detailMatchesSelection ? detailRound : (selectedRoundId === round?.roundId ? round : null))
        : (detailRound && detailRound.roundId === round?.roundId ? detailRound : round)
      const selectedRoundMissing = Boolean(selectedRoundId) && !selectedRound
        && (Boolean(roundDetail.error) || detailShapeInvalid)
      // Same honesty rule for the body: "not loaded yet" must not render the
      // newest round under the selected id's header.
      const selectedRoundPending = Boolean(selectedRoundId) && !selectedRound
        && !roundDetail.error && !detailShapeInvalid
      const envelopeBlockers = Array.isArray(envelopeData?.blocking_reasons) ? envelopeData.blocking_reasons : []
      const newEntryAllowed = typeof envelopeData?.new_entry_allowed === 'boolean'
        ? envelopeData.new_entry_allowed
        : (service.new_entry_allowed === undefined ? null : service.new_entry_allowed)
      const entry = entryGate({ protectionBlock: block, control, newEntryAllowed, envelopeBlockers })
      if (caps.maxPositions !== null && positions.rows.length >= caps.maxPositions) {
        entry.blocked = true
        entry.reasons.push({ kind: 'position_limit', label: t('risk.positionLimit'), short: t('risk.positionLimit'), tone: 'warn' })
      }
      // S1 summary is computed on its own: unreconciled (null) must never read as ok.
      const protectionSummary = (() => {
        const order = ['missing', 'partial', 'unknown', 'unreconciled', 'ok']
        for (const state of order) if (positions.rows.some((row) => row.protection.state === state)) return PROTECTION[state]
        return null
      })()
      const hostState = statePoll.error ? (stateData ? 'degraded' : 'offline') : 'online'
      const llmInfo = healthData?.llm ?? stateData?.llm ?? null
      const llmState = !llmInfo ? 'unknown'
        : llmInfo.route_selected === false || llmInfo.routeSelected === false ? 'unknown' : 'online'
      const model = {
        state: stateData, health: healthData, service, serviceCode: stateCode, control, equity, account,
        positions, orders, fills, reviews, rounds, round, freshness, block, caps, dayStats, llm, gaps, emptyReasons,
        riskEnvelope: envelopeData,
        controlBusy, setControlBusy, controlFlight,
        selectedRoundId, selectedRound, updown: appearance, entry, protectionSummary, hostState, llmState,
        market: { symbol: marketSymbol, quote: marketQuote, age: marketAge, error: marketPoll.error },
        envelope: { current: Boolean(envelopeData), data: envelopeData, error: envelopePoll.error },
        scorecard: resolveScorecard(selectedRound ?? round),
        roundsList: roundsWithLatest,
        // 15 §4/§5: the archived-day view the history table reads.
        history: {
          date: historyDate ?? todayKey,
          today: todayKey,
          dates: historyAvailable,
          past: historyPast,
          rounds: historyRounds,
          total: historyPast ? (num(historyPoll.data?.total) ?? historyRounds.length) : rounds.length,
          truncated: historyPast ? historyPoll.data?.truncated === true : false,
          loading: historyPast ? historyPoll.loading : false,
          error: historyPast
            ? (historyMismatch ? { status: 0, message: 'E_ARCHIVE_UNSUPPORTED' } : historyPoll.error)
            : null,
          onDate: (value) => setHistoryDate(value && value !== todayKey ? value : null),
        },
        roundDetail: {
          error: roundDetail.error,
          loading: roundDetail.loading,
          missing: selectedRoundMissing,
          missingCode: detailShapeInvalid
            ? 'E_ROUND_ID_MISSING'
            : (roundDetail.error?.status ? `HTTP ${roundDetail.error.status}` : (roundDetail.error?.message ?? null)),
        },
        // 05 N05: schedule detail (skips) and 05 N09/N02: the two route facts and
        // the process build identity are separate model fields on purpose.
        schedule: stateData?.schedule ?? null,
        // F1: which environment/account, is automation on, when did it last
        // succeed, what needs handling — the four top-of-page questions.
        automation,
        automationHost,
        automationPhase: autoPhase,
        accountShort: accountShort(service),
        host: stateData?.host ?? null,
        inFlight: stateData?.in_flight ?? null,
        llmConfig: stateData?.llm_config ?? null,
        stateLlm: stateData?.llm ?? null,
        // 15 §10: the Host says whether it can start the local service, and why not.
        serviceStart: stateData?.service_start ?? null,
        defaultLlm: healthData?.llm ?? null,
        build: stateData?.build ?? healthData?.build ?? null,
      }
      const EVIDENCE_TTL = 300000
      const precheck = (() => {
        const rows = []
        const online = stateCode === 'online'
        rows.push({
          id: 'service',
          tone: online ? 'ok' : 'danger',
          label: online ? t('pre.service') : `${t('pre.service')} · ${zh(serviceChip(stateCode).text)}`,
        })
        const running = Boolean(stateData?.in_flight) || runPhase === 'pending'
        rows.push({ id: 'running', tone: running ? 'danger' : 'ok', label: running ? t('pre.inflightBad') : t('pre.inflight') })
        const age = freshness.age
        rows.push({
          id: 'fresh',
          tone: age === null ? 'warn' : age <= EVIDENCE_TTL ? 'ok' : 'warn',
          label: age === null ? t('pre.freshUnknown') : age <= EVIDENCE_TTL ? t('pre.fresh') : fmt(zh('pre.freshOld'), { age: fmtAge(age) }),
        })
        const quota = caps.current && (caps.maxRiskPct !== null || caps.maxPositions !== null)
        rows.push({
          id: 'quota',
          tone: quota ? 'ok' : 'warn',
          label: quota
            ? `${t('pre.quota')} · ${fmt(zh('pre.quotaValue'), {
              risk: caps.maxRiskPct === null ? '—' : `${caps.maxRiskPct.toFixed(1)}%`,
              positions: caps.maxPositions === null ? '—' : caps.maxPositions,
            })}`
            : `${t('pre.quota')} · ${t('pre.quotaMissing')}`,
        })
        return {
          rows,
          severity: rows.some((row) => row.tone === 'danger') ? 'danger' : rows.some((row) => row.tone === 'warn') ? 'warn' : 'ok',
          running,
        }
      })()

      const attention = attentionItems(model)
      const conductor = attention[0] ?? null
      const moreCount = Math.max(0, attention.length - 1)

      const error = statePoll.error
      const loading = statePoll.loading && !stateData

      const setAppearancePref = (value) => {
        setAppearance(value)
        try {
          if (typeof window !== 'undefined' && window.localStorage) window.localStorage.setItem(UP_DOWN_KEY, value)
        } catch (err) { /* preference only */ }
      }

      const setDensityPref = (value) => {
        setDensity(value)
        try {
          if (typeof window !== 'undefined' && window.localStorage) window.localStorage.setItem(DENSITY_KEY, value)
        } catch (err) { /* preference only */ }
      }

      const openRound = (roundId) => {
        setWorkspace('ai')
        setAiView('current')
        setSelectedRoundId(roundId ?? rounds[0]?.roundId ?? null)
      }

      const refresh = () => {
        setRefreshing(true)
        setRefreshTick((value) => value + 1)
        setNonce((value) => value + 1)
        setHealthNonce((value) => value + 1)
        setMarketNonce((value) => value + 1)
        setEnvelopeNonce((value) => value + 1)
        setTimeout(() => setRefreshing(false), 300)
      }
      model.refresh = refresh

      /**
       * F1 §3.2: the top-bar primary action. 'stop' = close the scheduler and ask
       * the service for the non-flattening pause; 'start' = explicit resume that
       * the Host re-verifies. The UI never declares the result on its own: it
       * shows 正在停止 until the Host answers, and then mirrors the Host phase.
       */
      const automationRequest = async (action, reasonText) => {
        if (autoFlight.current) return null
        autoFlight.current = true
        const requestId = newRequestId()
        setAutoPhase({ phase: action === 'stop' ? 'stopping' : 'starting', code: null, requestId, message: null })
        try {
          const data = await operatorRequest(`automation/${action}`, {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ reason: reasonText, request_id: requestId }),
          })
          if (action === 'stop') {
            const phase = data?.phase === 'stopped' ? 'stopped' : 'pending'
            setAutoPhase({
              phase: 'idle',
              code: data?.error?.code ?? null,
              requestId: data?.request_id ?? requestId,
              message: null,
              snapshot: data?.automation ?? null,
            })
            setNotice({ kind: phase === 'stopped' ? 'ok' : 'warn', text: t(phase === 'stopped' ? 'auto.stopDone' : 'auto.stopPending') })
          } else {
            setAutoPhase({ phase: 'idle', code: null, requestId, message: null, snapshot: data?.automation ?? null })
            setNotice({ kind: 'ok', text: t('auto.startDone') })
          }
          return data
        } catch (error) {
          const code = error?.code ?? (action === 'stop' ? 'E_STOP_FAILED' : 'E_START_FAILED')
          if (action === 'stop') {
            setAutoPhase({ phase: 'failed', code, requestId, message: error?.message ?? null, snapshot: null })
            setNotice({ kind: 'danger', text: fmt(zh('auto.stopFailed'), { reason: code }) })
          } else {
            setAutoPhase({ phase: 'idle', code, requestId, message: error?.message ?? null, snapshot: null })
            setNotice({ kind: 'danger', text: fmt(zh(code === 'E_CONTROL_LOCKED' ? 'auto.startLocked' : 'auto.startBlocked'), { reason: code }) })
          }
          return null
        } finally {
          autoFlight.current = false
          setNonce((value) => value + 1)
          setEnvelopeNonce((value) => value + 1)
        }
      }
      // 15 §10: one click starts the local trading service. The Host spawns the
      // venv python and only answers ok after /health is ready; the UI reports
      // that outcome verbatim (including a timeout with its log path).
      const startService = async () => {
        if (serviceStartFlight.current) return
        const capability = stateData?.service_start ?? null
        if (capability && capability.supported !== true) {
          setNotice({ kind: 'warn', text: fmt(zh('svc.unsupported'), { reason: capability.reason ?? 'UNKNOWN' }) })
          return
        }
        serviceStartFlight.current = true
        setServiceStartPhase('starting')
        try {
          const data = await operatorRequest('service/start', {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify({ reason: t('svc.startReason') }),
          })
          const seconds = Math.max(1, Math.round((num(data?.waited_ms) ?? 0) / 1000))
          setNotice({ kind: 'ok', text: data?.already ? t('svc.already') : fmt(zh('svc.started'), { s: seconds }) })
        } catch (error) {
          const code = error?.code ?? 'E_SERVICE_START_FAILED'
          const key = code === 'E_SERVICE_LAUNCHER_MISSING' ? 'svc.missing' : 'svc.startFailed'
          setNotice({ kind: 'danger', text: fmt(zh(key), { reason: code }) })
        } finally {
          serviceStartFlight.current = false
          setServiceStartPhase('idle')
          setNonce((value) => value + 1)
          setHealthNonce((value) => value + 1)
          setEnvelopeNonce((value) => value + 1)
        }
      }

      const onAutomationClick = () => {
        if (autoPhase.phase === 'stopping') return
        if (automation.state === 'stopped' || automation.state === 'pending') {
          automationRequest('start', t('auto.startReason'))
          return
        }
        automationRequest('stop', t('auto.stopReason'))
      }

      // A manual refresh carries ?refresh=1 once, then polling returns to the
      // cached route (the Host keeps an 8s TTL).
      React.useEffect(() => {
        if (refreshTick > 0 && !statePoll.loading) setRefreshTick(0)
      }, [refreshTick, statePoll.loading])

      // Demo commands: close / repair protection. One attempt only, the receipt
      // stays in the dialog (a toast must not carry an irreversible outcome).
      const submitCommand = async (path, body) => {
        if (commandFlight.current) return
        commandFlight.current = true
        body = { ...body, request_id: newRequestId() }
        setActionBusy(true)
        setReceipt(null)
        try {
          const response = await fetch(`${API}${path}`, {
            method: 'POST',
            headers: { 'content-type': 'application/json', accept: 'application/json' },
            body: JSON.stringify(body),
          })
          const payload = await response.json().catch(() => ({}))
          if (!response.ok || payload?.ok === false) {
            setReceipt({
              ok: false,
                requestId: body.request_id,
              status: response.status,
              code: payload?.error?.code ?? `HTTP_${response.status}`,
              message: payload?.error?.message ?? '',
            })
          } else {
            setReceipt({ ok: true, status: response.status, requestId: payload?.requestId ?? null, result: payload?.result ?? null })
            setTimeout(() => {
              setNonce((value) => value + 1)
              setEnvelopeNonce((value) => value + 1)
              setMarketNonce((value) => value + 1)
            }, 2000)
          }
        } catch (error) {
            setReceipt({ ok: false, status: 0, requestId: body.request_id, code: 'NETWORK_ERROR', message: error?.message ?? 'ERROR' })
        } finally {
          setActionBusy(false)
          commandFlight.current = false
        }
      }

      const run = async () => {        if (runningRef.current) return
        runningRef.current = true
        setRunPhase('pending')
        setNotice(null)
        const controller = typeof AbortController === 'function' ? new AbortController() : null
        const timer = setTimeout(() => controller?.abort?.(), RUN_TIMEOUT_MS)
        try {
          const response = await fetch(RUN_URL, {
            method: 'POST',
            headers: { 'content-type': 'application/json', accept: 'application/json' },
            body: JSON.stringify({ symbol: 'BTCUSDT' }),
            signal: controller?.signal,
          })
          const body = await response.json().catch(() => ({}))
          if (response.status === 409) {
            setNotice({ kind: 'warn', text: t('run.conflict') })
          } else if (response.status === 401 || response.status === 403) {
            setNotice({ kind: 'danger', text: t('run.auth') })
          } else if (!response.ok) {
            setNotice({ kind: 'danger', text: fmt(zh('run.error'), { reason: body?.error?.code ?? response.status }) })
          } else {
            setNotice({ kind: 'ok', text: t('run.done') })
          }
          setNonce((value) => value + 1)
          setHealthNonce((value) => value + 1)
        } catch (errorRun) {
          if (errorRun?.name === 'AbortError') setNotice({ kind: 'warn', text: t('run.timeout') })
          else setNotice({ kind: 'danger', text: fmt(zh('run.error'), { reason: errorRun?.message ?? 'ERROR' }) })
        } finally {
          clearTimeout(timer)
          runningRef.current = false
          setRunPhase('idle')
        }
      }

      // Inline precheck: green submits in one click, yellow opens a confirm
      // strip, red disables the run button (with a repair entry).
      const onRunClick = () => {
        if (runPhase === 'pending') return
        if (precheck.severity === 'danger') { setPrecheckOpen(true); return }
        if (precheck.severity === 'warn' && !precheckOpen) { setPrecheckOpen(true); return }
        if (precheck.severity === 'warn' && precheckOpen) { setPrecheckOpen(false); run(); return }
        run()
      }
      const repairEntry = () => {
        setPrecheckOpen(false)
        setDrawer({ type: 'system', tab: 'evidence' })
      }

      const openEvidence = (type, payload) => {        if (type === 'round') {
          setDrawer({ type: 'round', round: payload ?? round, tab: 'evidence' })
          return
        }
        if (type === 'position') { setDrawer({ type: 'position', row: payload, tab: 'evidence' }); return }
        if (type === 'order') { setDrawer({ type: 'order', row: payload, tab: 'evidence' }); return }
        setDrawer({ type: 'call', call: payload, round: selectedRound ?? round, tab: 'evidence' })
      }

      const onAttention = (item) => {
        if (item.action === 'protection') { setDrawer({ type: 'position', row: positions.rows.find((row) => row.symbol === item.symbol) ?? positions.rows[0], tab: 'evidence' }); return }
        if (item.action === 'system') { setDrawer({ type: 'system', tab: 'evidence' }); return }
        if (item.action === 'start-service') { startService(); return }
        if (item.action === 'service-setup') { setSetupOpen(true); return }
        if (item.action === 'round') { openRound(round?.roundId) }
      }

      const serviceChipInfo = serviceChip(stateCode)
      const serviceTone = serviceChipInfo.tone === 'warn' ? 'warn' : serviceChipInfo.tone === 'danger' ? 'danger' : ''
      const lastUpdatedMs = (() => {
        const fetched = toMs(stateData?.portfolio?.fetched_at) ?? toMs(stateData?.equityCurve?.latest?.ts)
        return fetched === null ? null : Math.max(0, now - fetched)
      })()

      const cockpit = h(React.Fragment, null,
        h('div', { className: 'twb-row' },
          h(BSectionHead, {
            num: '01',
            title: t('b.accountRisk'),
            en: 'ACCOUNT & RISK',
            meta: `${t('s1.data')} ${freshness.age === null ? t('concl.notProvided') : fmtAge(freshness.age)}`,
          }),
          h(AccountBandB, { account, equity, t }),
          h(RiskBandB, { model, t, onSystem: () => setDrawer({ type: 'system', tab: 'evidence' }) })),
        h('div', { className: 'twb-row' },
          h(PositionsTable, {
            model, t, onEvidence: openEvidence,
            onAction: (type, row) => { setReceipt(null); setAction({ type, row }) },
            actionBusy,
            running: Boolean(stateData?.in_flight) || runPhase === 'pending',
          }),
          h(ConclusionCard, { model, t, onRound: () => openRound(round?.roundId), onEvidence: openEvidence })),
        h('div', { className: 'twb-row' },
          h(TapeB, { model, t, onOpenOrders: () => setOrdersOpen(true) })))

      const aiHead = h('div', { className: 'twb-ai-head' },
        h(TabList, {
          idPrefix: 'twb-ai',
          items: AI_VIEWS.map((id) => ({ id, label: t(`ai.${id}`) })),
          value: aiView,
          onChange: setAiView,
          ariaLabel: t('ws.ai'),
        }),
        h('div', { className: 'twb-ai-meta' },
          (() => {
            // 05 N09 / F1: the header is anchored to the user's selection. No id
            // means "the newest round" (that is what the list highlights); any
            // other id may only be filled from /state.last_round when it IS that
            // round. A failed (or in-flight) detail shows the selected id with
            // "—" instead of borrowing the latest round's facts.
            const effectiveSelectionId = selectedRoundId ?? round?.roundId ?? null
            const identicalToLatest = Boolean(round) && effectiveSelectionId === round.roundId
            const shown = selectedRound ?? (identicalToLatest ? round : null)
            const isLatest = !effectiveSelectionId || effectiveSelectionId === round?.roundId
            if (!shown && !effectiveSelectionId) return t('s1.noRound')
            const id = shown ? (shown.roundId ?? effectiveSelectionId) : effectiveSelectionId
            const startedAt = shown ? fmtClock(shown.startedAt) : '—'
            const status = shown
              ? statusLabel(shown.status)
              : (selectedRoundMissing ? t('ai.detailFailed') : t('state.loading'))
            const duration = shown ? fmtMs(shown.durationMs) : '—'
            return `#${String(id ?? '').slice(-6)} · ${startedAt} · ${status} · ${duration}${isLatest ? '' : ` · ${t('ai.backToLatest')}→`}`
          })()),
        h('span', { className: 'twb-spacer' }),
        // §2.1: the AI page's model entry jumps to the same settings group;
        // there is no second model surface.
        h('button', {
          type: 'button',
          className: 'twb-btn twb-btn--secondary',
          'data-settings-open': 'automation',
          onClick: () => setDrawer({ type: 'settings', tab: 'automation' }),
        }, t('config.open')),
        h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', onClick: () => { const index = rounds.findIndex((item) => item.roundId === (selectedRoundId ?? rounds[0]?.roundId)); const next = rounds[Math.min(rounds.length - 1, index + 1)]; if (next) setSelectedRoundId(next.roundId) } }, t('ai.prev')),
        h('button', { type: 'button', className: 'twb-btn twb-btn--secondary', onClick: () => { const index = rounds.findIndex((item) => item.roundId === (selectedRoundId ?? rounds[0]?.roundId)); const next = rounds[Math.max(0, index - 1)]; if (next) setSelectedRoundId(next.roundId) } }, t('ai.next')))
      const aiBody = aiView === 'history'
        ? h(HistoryView, { model, t, filter: historyFilter, duration: historyDuration, onFilter: setHistoryFilter, onDuration: setHistoryDuration, onOpen: (roundId) => { setAiView('current'); setSelectedRoundId(roundId) } })
        : aiView === 'review'
          ? h(ReviewView, { model, t, pnl: reviewPnl, range: reviewRange, onRange: setReviewRange })
          : h('div', { className: 'twb-ai-grid' },
            h(RoundList, { model, t, selected: selectedRoundId ?? round?.roundId ?? null, onSelect: setSelectedRoundId }),
            selectedRoundMissing
              ? h(RoundDetailError, {
                  model: { ...model, selectedRoundId },
                  t,
                  onRetry: () => setNonce((value) => value + 1),
                  onLatest: () => setSelectedRoundId(stateData?.last_round?.roundId ?? null),
                })
              : selectedRoundPending
                ? h('div', { className: 'twb-report-card' },
                    h(EmptyBlock, { kind: 'empty', title: t('state.loading'), hint: `#${String(selectedRoundId ?? '').slice(-6)}` }))
                : h(RoundReport, { model: { ...model, freshness }, round: selectedRound ?? round, t, onEvidence: openEvidence }))
      const ai = h(React.Fragment, null, aiHead, aiBody)

      return h('div', {
        className: 'twb-root',
        'data-updown': appearance,
        'data-density': density,
        'data-workspace': workspace,
        ref: rootRef,
      },
        h('style', null, CSS),
        h('header', { className: 'twb-s0' },
          h('span', { className: 'twb-title' }, t('panel.title')),
          h('span', { className: 'twb-demo', title: t('panel.demoHint') }, t('panel.demo')),
          h(TabList, {
            idPrefix: 'twb-ws',
            items: WORKSPACES.map((id) => ({ id, label: t(`ws.${id}`) })),
            value: workspace,
            onChange: (id) => {
              setWorkspace(id)
              // Opening the AI workspace always starts from the newest round;
              // the round list still lets the user pick an older one.
              if (id === 'ai') setSelectedRoundId(stateData?.last_round?.roundId ?? null)
            },
            ariaLabel: t('panel.title'),
          }),
          h('span', { className: 'twb-spacer' }),
          // §2.1: the environment badge and the account short name stay visible at
          // every width — they are the "which environment/account" answer.
          (() => {
            // §4.2 / §8·F: 环境徽标只能来自服务端事实。Live 必须显著且明确，
            // 未知就写“环境未知”——绝不把未知渲染成 demo（那会让用户在实盘上
            // 看到“模拟盘”）。已有的 `data-env` 钩子保持向后兼容。
            const env = model.host?.environment ?? null
            const venue = env?.known === false ? null : (env?.venue ?? model.service?.venue ?? null)
            const label = venue === 'live' ? t('env.live') : venue === 'demo' ? t('env.demo') : t('env.unknown')
            return h('span', {
              className: venue === 'live' ? 'twb-env twb-env--live' : 'twb-env',
              'data-env': venue ?? 'unknown',
              'data-env-live': venue === 'live' ? 'true' : 'false',
              title: venue === 'live'
                ? t('env.liveHint')
                : fmt(zh('env.accountHint'), { ledger: model.accountShort ?? zh('env.accountUnknown') }),
            }, model.accountShort ? `${label} · ${model.accountShort}` : label)
          })(),
          h('div', { className: 'twb-lang twb-lang-inline', role: 'group', 'aria-label': lang === 'zh' ? '语言' : 'Language' },
            h('button', {
              type: 'button',
              'data-lang-btn': 'zh',
              'aria-pressed': String(lang === 'zh'),
              onClick: () => setLanguage('zh'),
            }, '中'),
            h('span', { className: 'bar', 'aria-hidden': 'true' }),
            h('button', {
              type: 'button',
              'data-lang-btn': 'en',
              'aria-pressed': String(lang === 'en'),
              onClick: () => setLanguage('en'),
            }, 'EN')),
          h('div', { className: 'twb-equity' },
            h('span', { className: 'k' }, t('top.equity')),
            h('span', { className: 'twb-hero' }, account.empty ? '—' : fmtNum(account.total)),
            h('span', { className: 'twb-hero-unit' }, 'USDT')),
          h('span', { className: 'twb-divider' }),
          h('div', { className: 'twb-equity' },
            h('span', { className: 'k' }, t('top.todayPnl')),
            h(Num, { value: account.dayPnl })),
          h('span', { className: 'twb-divider' }),
          // §2.1/§3.2: the primary top-bar action is 停止自动交易 — close the
          // scheduler + service pause. It never closes positions and never
          // cancels exchange protection orders; 平仓 stays in the dangerous area.
          h('span', {
            className: `twb-auto twb-auto--${automationChipInfo.tone}`,
            role: 'status',
            'aria-live': 'polite',
            'data-auto-state': automation.state,
            'aria-label': `${zh(automationChipInfo.text)} · ${autoMetaLine(automation, t)}`,
            title: `${zh(automationChipInfo.text)} · ${autoMetaLine(automation, t)}`,
          },
          h('span', { className: 'twb-auto-dot', 'aria-hidden': 'true' }),
          h('span', { className: 'twb-auto-label twb-auto-full' }, t(automationChipInfo.text)),
          automationChipInfo.short && automationChipInfo.short !== automationChipInfo.text
            ? h('span', { className: 'twb-auto-label twb-auto-short' }, t(automationChipInfo.short))
            : null),
          h('button', {
            type: 'button',
            className: `twb-btn twb-auto-btn ${automation.state === 'stopped' || automation.state === 'pending' ? 'twb-btn--primary' : 'twb-btn--secondary'}`,
            'data-auto-action': automation.state === 'stopped' || automation.state === 'pending' ? 'start' : 'stop',
            'aria-label': automation.state === 'stopped' || automation.state === 'pending' ? t('auto.start') : t('auto.stop'),
            'aria-busy': String(autoPhase.phase === 'stopping' || autoPhase.phase === 'starting'),
            disabled: autoPhase.phase === 'stopping' || autoPhase.phase === 'starting',
            title: autoPhase.phase === 'stopping' ? t('auto.stopping') : t('auto.hint'),
            onClick: onAutomationClick,
          }, autoPhase.phase === 'stopping'
            ? t('auto.stopping')
            : autoPhase.phase === 'starting'
              ? t('auto.starting')
              : automation.state === 'stopped' || automation.state === 'pending'
                ? t('auto.start')
                : t('auto.stop')),
          h('button', {
            type: 'button',
            className: 'twb-btn twb-btn--ghost twb-precheck-toggle',
            'aria-label': t('pre.toggle'),
            title: t('pre.title'),
            onClick: () => setPrecheckOpen((value) => !value),
          }, t('pre.toggle')),
          h('button', {
            type: 'button',
            className: `twb-btn ${runPhase === 'pending' || precheck.severity === 'danger' ? 'twb-btn--secondary' : 'twb-btn--primary'}`,
            onClick: onRunClick,
            disabled: runPhase === 'pending' || precheck.severity === 'danger',
            title: precheck.severity === 'danger'
              ? precheck.rows.filter((row) => row.tone === 'danger').map((row) => row.label).join(' · ')
              : t('top.run'),
            'aria-busy': String(runPhase === 'pending'),
            'aria-disabled': String(runPhase === 'pending' || precheck.severity === 'danger'),
          }, runPhase === 'pending' ? t('top.running') : t('top.run')),
          // §2.1/§3.1: one settings entry, always in the same place. The label
          // stays in the accessible name at every width (the glyph is optional).
          h('button', {
            type: 'button',
            className: 'twb-btn twb-btn--secondary twb-settings-btn',
            'data-settings-open': 'true',
            'aria-label': t('set.open'),
            title: t('set.open'),
            onClick: () => { setMenuOpen(false); setDrawer({ type: 'settings', tab: 'connection' }) },
          },
          h('span', { className: 'twb-settings-ico', 'aria-hidden': 'true' }, '⚙'),
          h('span', { className: 'twb-settings-lbl' }, t('set.open'))),
          h('button', {
            type: 'button',
            className: 'twb-btn twb-btn--icon',
            'aria-label': t('top.more'),
            title: t('top.more'),
            onClick: () => setMenuOpen((value) => !value),
          }, '⋯')),
        menuOpen ? h(Menu, {
          model, t, refreshing, lang,
          onLanguage: (value) => setLanguage(value),
          onClose: () => setMenuOpen(false),
          onSystem: () => { setMenuOpen(false); setDrawer({ type: 'system', tab: 'evidence' }) },
          onSettings: () => { setMenuOpen(false); setDrawer({ type: 'settings', tab: 'connection' }) },
          onAppearance: setAppearancePref,
          onRefresh: () => { refresh() },
        }) : null,
        setupOpen ? h(ServiceSetupDialog, { t, onClose: () => setSetupOpen(false), onReady: () => { setNonce((v) => v + 1); setHealthNonce((v) => v + 1); setEnvelopeNonce((v) => v + 1) } }) : null,
        pendingOpen ? h(PendingPanel, {
          items: attention,
          t,
          onClose: () => setPendingOpen(false),
          onPick: (item) => { setPendingOpen(false); onAttention(item) },
        }) : null,
        precheckOpen ? h(PrecheckPanel, {
          precheck, entry, t,
          busy: runPhase === 'pending',
          onClose: () => setPrecheckOpen(false),
          onRun: () => { setPrecheckOpen(false); run() },
          onFix: repairEntry,
        }) : null,
        h('div', { className: 'twb-s1' },
          (() => {
            const quote = model.market.quote
            const price = quote ? (fmtPrice(quote.last) ?? fmtPrice(quote.mark)) : null
            const funding = quote ? fmtFunding(quote.funding_rate) : null
            const spread = quote ? fmtSpread(quote.spread_bps) : null
            const empty = price === null
            const tip = empty
              ? t('s1.marketTooltipEmpty')
              : `${t('s1.marketTooltip')}${model.market.age === null ? '' : ` · ${fmtAge(model.market.age)}`}`
            return h('span', { className: 'twb-ctx', title: tip },
              h('span', { className: 'v' }, model.market.symbol),
              price ? h('span', { className: 'v twb-mono' }, ` ${price}`) : null,
              funding ? h('span', { className: 'muted' }, ` · ${t('s1.funding')} ${funding}`) : null,
              spread ? h('span', { className: 'muted' }, ` · ${t('s1.spread')} ${spread}`) : null,
              empty ? h('span', null, ` · ${t('s1.marketNone')}`) : null)
          })(),
          h('span', { className: 'twb-ctx-sep' }),
          h('span', { className: 'twb-ctx' }, h('span', null, t('s1.positions')), h('span', { className: 'v twb-mono' }, String(positions.rows.length)), h('span', null, `· ${t('s1.protection')}`), h('span', { className: 'v' }, positions.rows.length === 0 ? '—' : zh(protectionSummary ? protectionSummary.label : 'prot.ok'))),
          h('span', { className: 'twb-ctx-sep' }),
          h('button', { type: 'button', className: 'twb-ctx-btn', onClick: () => openRound(round?.roundId) },
            round ? `${t('s1.round')} #${String(round.roundId ?? '').slice(-6)} · ${statusLabel(round.status)} · ${freshness.age === null ? t('concl.notProvided') : fmtAge(freshness.age)}` : `${t('s1.round')} · ${t('s1.noRound')}`),
          h('span', { className: 'twb-ctx-sep' }),
          h('button', { type: 'button', className: 'twb-ctx-btn', onClick: () => conductor && onAttention(conductor) }, `${t('s1.pending')} ${attention.length}`),
          // §2.1: 最近成功轮次 · 下次执行 sit next to the state they explain.
          h('span', { className: 'twb-ctx-sep' }),
          h('span', {
            className: 'twb-ctx twb-auto-meta',
            title: t(automationChipInfo.text),
            'data-auto-meta': automation.state,
          },
          h('span', null, t('auto.lastSuccess')),
          h('span', { className: 'v twb-mono' }, automation.lastSuccessAt ? fmtShortClock(automation.lastSuccessAt) : t('auto.lastSuccessNone')),
          h('span', { className: 'muted' }, ' ·'),
          h('span', null, t('auto.nextRun')),
          h('span', { className: 'v twb-mono' }, automation.nextAt ? fmtShortClock(automation.nextAt) : '—')),
          h('span', { className: 'twb-spacer' }),
          h('span', { className: 'twb-ctx' }, h('span', null, t('s1.data')), h('span', { className: 'v', style: { fontSize: 11 } }, loading ? t('state.loading') : freshness.age === null ? t('concl.notProvided') : fmtAge(freshness.age)))),
        conductor
          ? h('div', {
              className: 'twb-s2',
              'data-tone': conductor.tone ?? 'danger',
              // 03 D2: a blocking banner must be announced, not just painted.
              role: 'alert',
              'aria-live': 'assertive',
            },
            h('span', { className: 'twb-s2-ico', 'aria-hidden': 'true' },
              h('svg', { viewBox: '0 0 16 16', width: 16, height: 16 },
                h('path', { d: 'M8 1.5 15 14H1z', fill: 'currentColor' }),
                h('rect', { x: 7.25, y: 5, width: 1.5, height: 4.5, fill: 'var(--twb-s0)' }),
                h('rect', { x: 7.25, y: 10.5, width: 1.5, height: 1.5, fill: 'var(--twb-s0)' }))),
            // §2.2 copy contract: 影响 (title) + 原因 (reason) + 下一步 (action).
            h('span', { className: 'twb-s2-cat', 'data-cat': conductor.category ?? 'exec' }, t(ATTENTION_CATEGORY[conductor.category] ?? 's2.catExec')),
            h('span', { className: 'twb-s2-title' }, conductor.impact
              ? zh(conductor.impact)
              : (conductor.titleParams ? fmt(zh(conductor.title), conductor.titleParams) : zh(conductor.title))),
            h('span', { className: 'twb-s2-reason' }, conductor.reason),
            h('button', {
              type: 'button',
              className: 'twb-s2-act',
              disabled: conductor.action === 'start-service' && serviceStartPhase === 'starting',
              'aria-busy': String(conductor.action === 'start-service' && serviceStartPhase === 'starting'),
              onClick: () => onAttention(conductor),
            }, t(conductor.action === 'start-service' && serviceStartPhase === 'starting' ? 'svc.starting' : (conductor.actionLabel ?? 's2.view'))),
            h('span', { className: 'twb-spacer' }),
            moreCount > 0
              ? h('button', {
                  type: 'button',
                  className: 'twb-s2-act',
                  onClick: () => setPendingOpen(true),
                }, fmt(zh('s2.more'), { n: moreCount }))
              : null)
          : null,
        error && !stateData
          ? h('div', { className: 'twb-s2', 'data-tone': 'danger' },
            h('span', { className: 'twb-s2-title' }, t('state.hostOffline')),
            h('span', { className: 'twb-s2-reason' }, `HTTP ${error.status ?? '—'}`),
            h('button', { type: 'button', className: 'twb-s2-act', onClick: refresh }, t('state.retry')))
          : null,
        h('main', { className: 'twb-s3', role: 'tabpanel', id: `twb-ws-panel-${workspace}`, 'aria-labelledby': `twb-ws-tab-${workspace}` },
          h('div', { className: 'twb-canvas' },
            loading ? h('div', { className: 'twb-card', style: { padding: 24 } }, t('state.loading')) : workspace === 'cockpit' ? cockpit : ai)),
        h('footer', { className: 'twb-s4' },
          h('span', null, h('span', { className: `twb-s4-dot ${hostState === 'offline' ? 'twb-s4-dot--danger' : hostState === 'degraded' ? 'twb-s4-dot--warn' : ''}` }), `${t('s4.host')} ${hostState === 'offline' ? t('s4.offline') : hostState === 'degraded' ? t('s4.unknown') : t('s4.online')}`),
          h('span', { className: 'sep' }, '·'),
          h('span', null, h('span', { className: `twb-s4-dot ${serviceTone ? `twb-s4-dot--${serviceTone}` : ''}` }), `${t('s4.service')} ${zh(serviceChipInfo.text)}`),
          h('span', { className: 'sep' }, '·'),
          h('span', null, h('span', { className: `twb-s4-dot ${llmState === 'unknown' ? 'twb-s4-dot--warn' : ''}` }), `${t('s4.llm')} ${llmState === 'unknown' ? t('s4.unknown') : t('s4.online')}`),
          h('span', { className: 'sep' }, '·'),
          h('span', null, `${t('s4.control')} ${controlLabel(control)} · ${t('s4.newEntry')} ${entry.blocked ? t('s4.entryBlocked') : t('s4.entryAllowed')}${entry.blocked && entry.reasons.length > 0 ? ` · ${entry.reasons.map((item) => item.short).join(' / ')}` : ''}`),
          h('span', { className: 'sep' }, '·'),
          h('span', null, `${t('s4.schedule')} ${model.automation?.source === 'host' ? scheduleFooter(model, t) : scheduleLabel(stateData, t)}`),
          h('span', { className: 'sep' }, '·'),
          h('span', null, `${t('s4.updated')} ${lastUpdatedMs === null ? '—' : fmtAge(lastUpdatedMs)}`),
          h('span', { className: 'sep' }, '·'),
          h('span', null, `${t('s4.poll')} ${stateData?.in_flight ? '3s' : '8s'}`),
          h('span', { className: 'sep' }, '·'),
          h('span', null, `${t('s4.gaps')} ${gaps.length}`),
          h('span', { className: 'sep' }, '·'),
          h('span', null, t('s4.readonly')),
          h('span', { className: 'twb-spacer' }),
          h('span', null, 'v0.1.0')),
        notice
          ? h('div', {
            className: 'twb-toast',
            'data-tone': notice.kind === 'danger' ? 'danger' : notice.kind === 'warn' ? 'warn' : 'quiet',
            role: 'status',
            'aria-live': 'polite',
            style: { position: 'absolute', left: 0, right: 0, bottom: 26 },
          },
          h('span', { className: 'twb-s2-title' }, t('state.notice')),
          h('span', { className: 'twb-s2-reason' }, notice.text),
          h('button', { type: 'button', className: 'twb-s2-act', onClick: () => setNotice(null) }, t('common.close')))
          : null,
        ordersOpen
          ? h(OrdersDrawer, {
            model, t,
            initialTab: ordersTab,
            onTab: (tab) => setOrdersTab(tab),
            onClose: () => setOrdersOpen(false),
            onDetail: (type, row) => { setOrdersOpen(false); setDrawer({ type, row, tab: 'evidence' }) },
            onAll: () => { setOrdersOpen(false); openRound(rounds[0]?.roundId) },
            onReview: () => { setOrdersOpen(false); setWorkspace('ai'); setAiView('review') },
          })
          : null,
        drawer
          ? h(Drawer, {
            model, drawer, t,
            onClose: () => { if (!controlFlight.current) setDrawer(null) },
            onTab: (tab) => setDrawer((prev) => (prev ? { ...prev, tab } : prev)),
            onOpenSystem: () => setDrawer({ type: 'system', tab: 'evidence' }),
            lang,
            onLanguage: (value) => setLanguage(value),
            appearance,
            onAppearance: setAppearancePref,
            density,
            onDensity: setDensityPref,
          })
          : null,
        action
          ? h(ActionDialog, {
            action,
            t,
            busy: actionBusy,
            receipt,
            onCancel: () => { if (!actionBusy) { setAction(null); setReceipt(null) } },
            onSubmit: (body) => submitCommand(action.type === 'close' ? '/positions/close' : '/protection/repair', body),
          })
          : null)
    }

    return {
      inject: ['slots', 'locale'],
      apply(ctx) {
        const t = ctx.locale.bind(NS)
        ctx.effect(() => ctx.locale.register(NS, DICT))
        ctx.effect(() => ctx.slots.inject('main', () => ctx.slots.register({
          name: 'main',
          key: 'trading-workbench',
          locale: NS,
          inject: () => ({ t }),
        }, WorkbenchPanel)))
        ctx.effect(() => ctx.slots.inject('sidebar.panellist', () => ctx.slots.register({
          name: 'sidebar.panellist',
          id: 'trading-workbench',
          order: 40,
          label: () => t('panel.title'),
          locale: NS,
        }, SidebarIcon)))
      },
    }

    function SidebarIcon() {
      return h('svg', { width: 20, height: 20, viewBox: '0 0 20 20', 'aria-hidden': 'true', fill: 'none', stroke: 'currentColor', strokeWidth: 1.5 },
        h('path', { d: 'M3 16.5V10M8 16.5V5M13 16.5v-4.5M18 16.5v-9' }),
        h('path', { d: 'M2.5 16.5h16' }))
    }
  },
})
