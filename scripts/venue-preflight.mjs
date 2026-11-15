#!/usr/bin/env node
/**
 * 环境切换预检（只读，不执行切换）。
 *
 * 依据 07-方案优化评审 §3.3：切换前必须能展示目标环境、域名、账户身份、
 * 源账户持仓/挂单与切换后行为。本脚本只做判定，不改任何状态——
 * 真正的切换仍由人执行（改 WORKBENCH_VENUE + 重启），因为它会改变运行态。
 *
 * 用法：
 *   node scripts/venue-preflight.mjs --service http://127.0.0.1:8791 --target live
 */
import { readFileSync, existsSync, accessSync, constants } from 'node:fs'
import { resolve } from 'node:path'

const args = new Map()
for (let i = 2; i < process.argv.length; i += 2) {
  const key = process.argv[i].replace(/^--/, "")
  args.set(key, process.argv[i + 1])
}

const serviceBase = (args.get("service") || "http://127.0.0.1:8791").replace(/\/$/, "")
const dataDir = resolve(args.get("data-dir") || "trading-workbench/data")
const target = (args.get("target") || (process.env.WORKBENCH_VENUE === "live" ? "demo" : "live")).toLowerCase()

const VENUE_HOSTS = { demo: "demo-fapi.binance.com", live: "fapi.binance.com" }
const checks = []
const record = (name, ok, detail) => checks.push({ name, ok, detail })

async function getJson(url, token) {
  const headers = token ? { Authorization: `Bearer ${token}` } : {}
  const resp = await fetch(url, { headers })
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
  return resp.json()
}

function readToken() {
  const file = resolve(dataDir, ".api-token")
  if (!existsSync(file)) return null
  return readFileSync(file, "utf8").trim()
}

try {
  // 1. 源环境身份（必须来自运行进程，不靠推断）
  const health = await getJson(`${serviceBase}/health`)
  const sourceVenue = health.venue
  record("源环境身份可读", Boolean(sourceVenue), `source venue=${sourceVenue}`)
  record("源环境与目标不同", sourceVenue !== target, `source=${sourceVenue} target=${target}`)
  record("守卫通过", Boolean(health.guard?.ok), health.guard?.reason_code ?? "unknown")
  record("服务就绪", health.ready === true, `ready=${health.ready}`)

  // 2. 源账户持仓与挂单（有仓不得离开，§3.3）
  const token = readToken()
  let positions = null
  let orders = null
  try {
    positions = await getJson(`${serviceBase}/api/positions`, token)
    orders = await getJson(`${serviceBase}/api/orders?limit=200`, token)
  } catch (error) {
    record("源账户持仓/挂单可读", false, `读取失败：${error.message}（需鉴权 token）`)
  }
  if (positions) {
    const open = (positions.positions || []).filter((p) => Number(p.qty) !== 0)
    record("源账户无持仓", open.length === 0, `open_positions=${open.length}`)
  }
  if (orders) {
    const live = (orders.orders || []).filter((o) => ["NEW", "PARTIALLY_FILLED", "SUBMITTING", "UNKNOWN"].includes(String(o.status)))
    record("源账户无在途挂单", live.length === 0, `open_orders=${live.length}`)
  }

  // 3. 目标环境凭据（明确来源，不回退 Demo）
  const keyName = target === "live" ? "BINANCE_LIVE_API_KEY" : "BINANCE_DEMO_API_KEY"
  const secretName = target === "live" ? "BINANCE_LIVE_API_SECRET" : "BINANCE_DEMO_API_SECRET"
  const envPresent = Boolean(process.env[keyName] && process.env[secretName])
  const liveFile = process.env.WORKBENCH_LIVE_ENV_FILE || resolve("trading-workbench/service/config/live.env")
  const filePresent = target === "live" && existsSync(liveFile)
  record(`目标凭据存在（${keyName}）`, envPresent || filePresent,
    envPresent ? "source=env" : filePresent ? `source=file:${liveFile}` : "缺失：Live 不会借用 Demo 凭据")

  // 4. 目标数据目录（每环境独立库）
  const targetDir = target === "demo" ? dataDir : resolve(dataDir, target)
  let writable = true
  try {
    accessSync(target === "demo" ? dataDir : resolve(dataDir), constants.W_OK)
  } catch { writable = false }
  record("目标数据目录可写", writable, targetDir)
  record("目标域名受控", Boolean(VENUE_HOSTS[target]), `${target} → ${VENUE_HOSTS[target] ?? "未知"}`)
} catch (error) {
  record("服务可达", false, `${error.message}（先确认 service 在运行）`)
}

const failed = checks.filter((c) => !c.ok)
for (const check of checks) {
  console.log(`${check.ok ? "[PASS]" : "[FAIL]"} ${check.name} — ${check.detail}`)
}
console.log("")
if (failed.length === 0) {
  console.log(`GO：可以切换到 ${target}。切换动作需人工执行（改 WORKBENCH_VENUE 并重启），本脚本不改任何状态。`)
  // 用 exitCode 而不是 process.exit()：Windows 上 undici 的 keep-alive socket
  // 未关闭时强退会抛 UV_HANDLE_CLOSING 断言，把正常判定伪装成脚本崩溃。
  process.exitCode = 0
}
console.log(`NO-GO：${failed.length} 项未通过，禁止切换。`)
process.exitCode = 1