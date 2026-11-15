#!/usr/bin/env node
/**
 * 环境切换执行器（§3.3）。默认 dry-run，只有显式 --confirm 才会真的停/起服务。
 *
 * 刻意不做的事：
 * - 失败时不自动切回源环境、不反复重启碰运气（§3.3 明令取消“失败自动回 Demo”）；
 * - 不自动平仓：停止只关调度 + 撤授权，保护单保持（§3.2）；
 * - 有持仓/在途挂单时直接拒绝切换（§3.3）。
 *
 * 用法：
 *   node scripts/switch-venue.mjs --target live --data-dir <abs> --service http://127.0.0.1:8791
 *   node scripts/switch-venue.mjs --target live ... --confirm
 */
import { spawn, execFileSync } from 'node:child_process'
import { readFileSync, existsSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

// 所有路径都从脚本自身位置推导，不依赖调用者的 cwd——否则在 repo 根与
// trading-workbench/ 下运行会得到两个不同的结果（这台机器上就踩过一次）。
const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url))
const WORKBENCH_DIR = resolve(SCRIPT_DIR, '..')

const args = new Map()
for (let i = 2; i < process.argv.length; i += 1) {
  const raw = process.argv[i]
  if (!raw.startsWith("--")) continue
  const key = raw.replace(/^--/, "")
  const next = process.argv[i + 1]
  if (next && !next.startsWith("--")) { args.set(key, next); i += 1 } else { args.set(key, "true") }
}

const serviceBase = (args.get("service") || "http://127.0.0.1:8791").replace(/\/$/, "")
const dataDir = resolve(args.get("data-dir") || "trading-workbench/data")
const target = (args.get("target") || "live").toLowerCase()
const port = Number(args.get("port") || 8791)
const confirmed = args.get("confirm") === "true"
const dryRun = !confirmed

const fail = (message) => { console.error(`NO-GO: ${message}`); process.exitCode = 1 }

function readToken() {
  const file = resolve(dataDir, ".api-token")
  return existsSync(file) ? readFileSync(file, "utf8").trim() : null
}

async function getJson(url, token, init = {}) {
  const headers = { ...(init.headers || {}) }
  if (token) headers.Authorization = `Bearer ${token}`
  const resp = await fetch(url, { ...init, headers })
  const text = await resp.text()
  let body = null
  try { body = text ? JSON.parse(text) : null } catch { body = { raw: text } }
  return { ok: resp.ok, status: resp.status, body }
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

async function main() {
  console.log(dryRun ? "== DRY RUN（未加 --confirm，不会停/起任何进程）==" : "== 实际执行 ==");

  // 1) 预检（复用同一个脚本，保持单一判定口径）
  const preflight = spawn(process.execPath, [
    resolve(SCRIPT_DIR, "venue-preflight.mjs"),
    "--service", serviceBase, "--data-dir", dataDir, "--target", target,
  ], { stdio: "inherit" })
  const preflightCode = await new Promise((r) => preflight.on("exit", r))
  if (preflightCode !== 0) return fail("预检未通过，拒绝切换（有仓/有挂单/缺凭据都会走到这里）")

  const token = readToken()
  const health = (await getJson(`${serviceBase}/health`)).body || {}
  const sourceVenue = health.venue
  console.log(`[1/6] 源环境 ${sourceVenue}，目标 ${target}`)
  if (sourceVenue === target) return fail(`当前已经是 ${target}，无需切换`)

  if (dryRun) {
    console.log("[2/6] 将调用 POST /api/control {state:pause} 关闭调度（dry-run 跳过）")
    console.log("[3/6] 将轮询 outbox 直到 pending=0（dry-run 跳过）")
    console.log("[4/6] 将停止服务进程（dry-run 跳过）")
    console.log(`[5/6] 将以 WORKBENCH_VENUE=${target} --data-dir <target> 重新拉起（dry-run 跳过）`)
    console.log("[6/6] 将核对 /health.venue==target 与身份一致（dry-run 跳过）")
    console.log("");
    console.log("DRY RUN 完成：预检通过。真正执行请追加 --confirm。")
    return
  }

  // 2) 停止自动交易（只关调度 + 撤授权，不平仓、不撤保护单）
  const pause = await getJson(`${serviceBase}/api/control`, token, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ state: "pause", confirm: "PAUSE", reason: `switch to ${target}` }),
  });
  if (!pause.ok) return fail(`停止自动交易失败：HTTP ${pause.status} ${JSON.stringify(pause.body).slice(0, 200)}`);
  console.log("[2/6] 已请求 pause（不平仓、不撤保护单）")

  // 3) 等在途订单收敛：未决结果不清楚时不报告切换完成
  let pending = null;
  for (let i = 0; i < 30; i += 1) {
    const state = await getJson(`${serviceBase}/api/status`, token);
    const outbox = (state.body && state.body.outbox) || {};
    pending = Number(outbox.pending_count ?? 0) + Number(outbox.unknown ?? 0) + Number(outbox.submitting ?? 0);
    if (pending === 0) break;
    await sleep(2000);
  }
  if (pending !== 0) return fail(`仍有 ${pending} 个未决订单，拒绝报告切换完成`)
  console.log("[3/6] 在途订单已收敛")

  console.log("[4/6] 停止服务进程");
  try {
    if (process.platform === "win32") execFileSync("taskkill", ["/F", "/PID", String(health.pid)]);
    else process.kill(health.pid, "SIGTERM");
  } catch (error) {
    console.log(`  停止进程时出错（可能已退出）：${error.message}`)
  }
  await sleep(3000);

  console.log(`[5/6] 以 ${target} 拉起服务`);
  const targetDataDir = target === "demo" ? dataDir : resolve(dataDir, target);
  const env = { ...process.env, WORKBENCH_VENUE: target };
  const child = spawn(process.env.WORKBENCH_PYTHON || "python", [
    resolve(WORKBENCH_DIR, "service/run_service.py"),
    "--port", String(port), "--data-dir", targetDataDir,
  ], { env, detached: true, stdio: "ignore" });
  child.unref();

  // 6) 核对真实身份；失败**不**自动切回
  let after = null;
  for (let i = 0; i < 40; i += 1) {
    await sleep(1500);
    const probe = await getJson(`${serviceBase}/health`).catch(() => null);
    if (probe && probe.ok) { after = probe.body; break }
  }
  if (!after) {
    return fail("切换后服务无响应：保持停止状态，实际环境未确认。请人工检查，不要自动回滚")
  }
  const afterVenue = after.venue;
  const identity = after.identity || {};
  if (afterVenue !== target) {
    return fail(`切换后实际环境=${afterVenue}，与目标 ${target} 不一致；保持停止，不自动回源`)
  }
  console.log(`[6/6] 已连接：venue=${afterVenue} host=${identity.venue_host} ledger=${identity.ledger_id}`);
  console.log("");
  console.log("切换完成：已连接，但**自动交易未启动**。需要时请显式启动。")
}

main().catch((error) => fail(`未预期错误：${error.message}`));