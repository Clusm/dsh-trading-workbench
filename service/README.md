# workbench-service（M1.3c 行情 / 证据 / cycle / AI 审计）

独立 Python 服务（FastAPI + uvicorn），只监听 `127.0.0.1:8791`，只访问
`demo-fapi.binance.com` 的**公共只读行情**（GET，无 key、无签名、无下单）。

本服务对外提供五类能力：

1. `/health`、`/api/ready`：存活、DB、demo 守卫、鉴权、单实例锁；
2. `/api/market/probe`：M1.0 只读探针（time / exchangeInfo / klines / ticker24h /
   premiumIndex / openInterest）；
3. **M1.1 行情层** `/api/market/health|clock|universe|klines|prices`：闭合 K 线、
   指标、20+8+index 币种池、数据质量；
4. **M1.1 证据层**：`md-evidence/1` 快照生成、schema 校验、sha256、落盘与 DB 引用；
5. **M1.3c cycle/审计层**：`POST /api/cycles` 生成一轮证据 + 账户/风控快照，
   AI run/call/artifact 审计落库，`finalize` 收口；供 Host/UI 读取。
   **不实现真实下单**；P1/P2 仍走 M1.2 的既有 demo-only 路径。

> 约束来源：`docs/04-行情与指标.md` §6–§10、`docs/01-总体架构.md` §8/§10、
> `docs/03-Demo执行与风控.md` 的 demo-only 约束。

**鉴权**：`/api/*` 默认要求 `Authorization: Bearer <token>`。token 优先取
`WORKBENCH_API_TOKEN`，否则读 `data/.api-token`，首次启动自动生成 32 字节随机 token
（0600）；env 或文件中的 token 少于 16 字符时启动失败（退出码 2）。仅显式
`WORKBENCH_AUTH_MODE=dev-open` 才关闭 Bearer（离线测试用）。`/health` 与 `/` 始终免鉴权；
启动时会实际调用 `/health` handler 校验 `auth`/`lock` 字段齐全，否则拒绝启动。
**token 值不写日志、不进健康响应。**

---

## 1. 目录

```
service/
├─ run_service.py                 # 启动入口（预检 + 单实例锁 + uvicorn）
├─ requirements.txt               # Python 3.13 依赖
├─ .env.example                   # 只列键名，不含值；不自动加载
├─ workbench_service/
│  ├─ __init__.py
│  ├─ app.py                      # FastAPI 装配、lifespan、错误信封、trace_id
│  ├─ config.py                   # env + 默认值；token 文件/生成/ACL
│  ├─ guard.py                    # demo-only 守卫
│  ├─ db.py                       # SQLite 初始化/迁移（workbench-v1.sqlite）
│  ├─ market_probe.py             # M1.0 只读探针与错误映射
│  ├─ errors.py / security.py     # 统一错误、Bearer 依赖、auth_status
│  ├─ api/
│  │  ├─ health.py                # /health, /api/ready
│  │  ├─ market.py                # /api/market/* 路由（全部 GET）
│  │  ├─ cycles.py                # /api/cycles 创建/列表/详情/证据/finalize
│  │  └─ ai_audit.py              # AI run/call/event/artifact 审计路由
│  ├─ cycles/
│  │  ├─ schema.py                # cycle/evidence/ai_* 表幂等建表/升级
│  │  ├─ store.py                 # SQLite 持久化（cycle/evidence/audit）
│  │  └─ service.py               # cycle 创建、Host cycle_id、审计/finalize 门面
│  └─ market/                     # M1.1 行情/指标/证据包
│     ├─ config.py                # 20 币白名单、周期/warmup、预算、阈值
│     ├─ transport.py             # 只读 httpx、600/min、/futures/data 800/5m、429/418
│     ├─ klines.py                # 闭合 bar、增量缓存、15m→1h/4h/1d 聚合
│     ├─ indicators.py            # EMA/RSI/ATR/MACD/BB/Donchian/ADX/量/taker（纯 Python）
│     ├─ universe.py              # 20 白名单 + deep 8（held/prev/top_volume/anomaly/fill）+ index
│     ├─ quality.py               # GOOD/DEGRADED/STALE/BLOCKED + issue codes + 系统级去重
│     ├─ schema.py                # md-evidence/1 最小 draft-07 校验器
│     ├─ evidence_schema.json     # docs/04 §10 的冻结 schema 副本
│     └─ evidence.py              # 证据组装、预算裁剪、sha256、落盘、DB 引用
├─ tests/test_m1_smoke.py         # M1.0 冒烟 + 锁/鉴权/只读面
├─ tests/test_m1_1_market.py      # M1.1 离线回归（假上游 200/429/418/timeout）
├─ tests_security/test_auth.py    # M1.1 Bearer/token/dev-open 回归
└─ tests_service_cycles/          # M1.3c cycle/evidence/audit 离线回归
```

## 2. 安装与启动

要求 Python 3.13。

```powershell
cd trading-workbench\service

# 方式 A：独立 venv（推荐）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 方式 B：用原项目已有 3.13 venv（本机验证使用）
#   C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe

# 启动（默认 127.0.0.1:8791，数据目录 trading-workbench/data）
python run_service.py --port 8791
# 自定义数据目录：
python run_service.py --port 8791 --data-dir ..\data
```

退出码：`0` 正常；`2` 配置/守卫/鉴权失败；`3` 已有实例运行；`4` 端口占用
（docs/01 §10.4：**不自动换端口**）。

## 3. 端点

| 方法 | 路径 | 鉴权 | 说明 |
|---|---|---|---|
| GET | `/health` | 免鉴权 | 版本/uptime/db/ready/guard/auth/lock |
| GET | `/` | 免鉴权 | 服务横幅 |
| GET | `/api/ready` | Bearer | `ready`（new-entry readiness）、`service_ready`、`new_entry_allowed`、`reasons[]`、guard/db 明细；无 Demo key 时 ready=false、reasons 非空 |
| GET | `/api/market/probe` | Bearer | M1.0 只读探针（6 步） |
| GET | `/api/market/health` | Bearer | 行情健康：verdict/score/issues/sources/last_probe/cooldown/data_budget |
| GET | `/api/market/clock` | Bearer | 服务器时钟 offset/rtt/trusted |
| GET | `/api/market/universe` | Bearer | 20 白名单 + deep 8 + index 分层 |
| GET | `/api/market/klines` | Bearer | 仅闭合 K 线（`closed_only=true`） |
| GET | `/api/market/prices` | Bearer | last/mark/index/bid/ask/spread_bps |
| POST | `/api/cycles` | Bearer | 原子创建 RUNNING cycle；生成证据 + 账户/风控快照；返回 `cycle_id` |
| GET | `/api/cycles?limit=&cursor=` | Bearer | cycle 列表（新→旧） |
| GET | `/api/cycles/{id}` | Bearer | cycle + evidence 引用 + agents/AI runs/outputs |
| GET | `/api/cycles/{id}/evidence?agent=` | Bearer | 读取并校验落盘证据 |
| POST | `/api/cycles/{id}/finalize` | Bearer | RUNNING→终态；重复同结果幂等 |
| POST | `/api/cycles/{id}/ai-runs` | Bearer | 创建/幂等打开 AI run |
| PUT | `/api/ai-runs/{run_id}/calls/{call_id}` | Bearer | 每次尝试一行 AI call；同内容幂等 |
| POST | `/api/ai-runs/{run_id}/events` | Bearer | 追加 AI 状态事件 |
| PUT | `/api/cycles/{id}/artifacts/{agent}` | Bearer | 按 attempt 幂等保存 agent 输出 |
| GET | `/docs` | 免鉴权 | OpenAPI（本机调试） |

```powershell
$token = (Get-Content trading-workbench\data\.api-token -Raw).Trim()
curl.exe -H "Authorization: Bearer $token" http://127.0.0.1:8791/api/market/health
curl.exe -H "Authorization: Bearer $token" http://127.0.0.1:8791/api/market/clock
curl.exe -H "Authorization: Bearer $token" http://127.0.0.1:8791/api/market/universe
curl.exe -H "Authorization: Bearer $token" "http://127.0.0.1:8791/api/market/klines?symbol=BTCUSDT&interval=15m&limit=100"
curl.exe -H "Authorization: Bearer $token" "http://127.0.0.1:8791/api/market/prices?symbols=BTCUSDT,ETHUSDT"
```

无 token/错误 token：HTTP `401`，错误码 `E_AUTH_REQUIRED`。

### 3.1 `/api/market/health`

- 默认 **`refresh=false`**：10s 进程内缓存（`HEALTH_TTL_S`），二次调用 `cached=true`；
  手动排查可显式 `refresh=true` 强制刷新（保留强制路径）。
- 冷启动/缓存过期才重新采样：`/fapi/v1/time` + 24h tickers + BTCUSDT
  15m/1h/4h/1d 闭合 K 线。
- **冷却/封禁时仍返回 HTTP 200**：`sources.rest.state=cooldown|banned`、
  `cooldown_s>0`、`issues` 含 `RATE_LIMITED_429`/`IP_BANNED_418`，并带
  `last_probe`（本次诊断）。`BLOCKED` 结果不写缓存，冷却恢复后可立即重探。
- `data_budget`：`/futures/data` 独立 300s 窗口已用/上限（默认 800）。

### 3.2 `/api/market/klines`

- 只返回**闭合 bar**：`close_time_ms <= now`；正在形成的 bar 只计入
  `meta.live_bar_present`，永不进入指标/证据。
- `limit` 1–1000；内部自动预热：15m 500、1h 300、4h 300、1d 200 根（`meta.needed`）。
- `meta.source`：`rest`（直连）或 `aggregate_15m`（目标周期失败时用 15m 聚合；
  只接受完整且全部 final 的桶，不插值）。
- `meta` 字段：`count/needed/last_closed_at_ms/age_s/fresh/gap/non_monotonic/
  live_bar_present/source`；`issues` 使用固定 code（`KLINE_*`）。

### 3.3 `/api/market/prices`

- 最多 10 个白名单 symbol；返回 `last/mark/index/bid/ask/spread_bps/age_ms/complete/errors`。
- 逐 symbol 失败不整体报错：缺字段为 `null`，`errors` 记录上游 code。

### 3.4 错误映射

| 上游/参数 | HTTP | `error.code` |
|---|---|---|
| 参数 symbol 不在 20 白名单 | 400 | `E_SYMBOL_NOT_ALLOWED` |
| interval 非 15m/1h/4h/1d | 400 | `E_INVALID_INTERVAL` |
| 本地权重预算让路（600/min 或 `/futures/data` 800/5m） | 503 | `E_MARKET_BUDGET_DEFERRED` |
| 429 冷却中 | 503 | `E_MARKET_COOLDOWN` + `Retry-After` |
| 418 封禁中 | 503 | `E_MARKET_BANNED` + `Retry-After` |
| 上游超时 | 504 | `E_MARKET_TIMEOUT` |
| 其他上游失败 | 502 | `E_MARKET_UPSTREAM` |

429/418 **不重试**；冷却状态落盘 `data/state/market_rate_limit.json`，进程重启后仍生效。

## 4. 限流预算（docs/04 §8.2）

| 项 | 值 | 行为 |
|---|---|---|
| 本地预算 | **600 weight/min** | 后台请求 ≥75% 让路；普通 ≥90% 让路；critical 绕过软预算但**不绕过 429/418** |
| `/futures/data` | **800 次/300s** | **独立 deque 计数**，不随 60s 全局记录裁剪；超限前置拒绝，不发请求 |
| 429 冷却 | 默认 60s（夹 5–3600） | 写状态文件；`Retry-After` |
| 418 封禁 | 默认 480s（夹 480–604800） | REST 停止；`Retry-After` |
| 官方权重上限 | 2400/min | 仅记录在 `sources.rest.weight_limit_1m`，本地按 600 让路 |

## 5. 证据快照（md-evidence/1）

- 生成入口：`market.evidence.build_cycle_evidence(service, ...)`（内部）；外部只通过
  `POST /api/cycles` 触发。证据至少包含 **BTCUSDT deep + 15m/1h/4h/1d**，默认 8 deep +
  其余白名单 index 行。
- 落盘：`trading-workbench/data/evidence/<UTC 日期>/<cycle_id>/evidence.json`
  （原子写：先 `.tmp`，DB 引用成功后 `os.replace`；DB 失败清理临时文件并报
  `PERSIST_FAILED`，不留下可被误读的文件）。
- DB：`evidence_snapshots(cycle_id PK, schema_version, produced_at_ms, verdict, score,
  path, sha256, bytes, prompt_tokens, created_at)`，**只存引用**，不存证据全文。
- 校验：写入前用 `market/evidence_schema.json`（docs/04 §10 冻结副本）做
  fail-closed 校验；`sha256` 覆盖除 `snapshot_id` 与 `provenance.snapshot_hash`
  之外的规范 JSON，`snapshot_id` 后缀为 hash 前 6 位。
- 质量：`data_health.verdict` 为 `GOOD/DEGRADED/STALE/BLOCKED`，`score=1-0.25*errors-
  0.05*warns`；issue code 见 docs/04 §9（`KLINE_*`、`TICKER_*`、`CLOCK_SKEW`、
  `RATE_LIMITED_429`、`IP_BANNED_418`、`DERIVATIVES_PARTIAL` 等）。
- **系统级 issue 语义**：`DERIVATIVES_PARTIAL`/`UNIVERSE_DEGRADED`/`RATE_LIMITED_429`
  等源级 code 在同一轮只计一次；**对外 `data_health.issues` = `_effective(issues)`**，
  score/verdict 也基于该去重集合。每个币的细节仍保留在
  `symbols.<symbol>.quality.issues`。
- **token 预算**：`limits.prompt_token_budget=6000`。超预算时按 docs/04 §10.5 顺序
  裁剪并写 `truncated=true` + `dropped_fields[]`：非持仓 candles → index>10 → 非持仓
  1d → 1h/4h 可选指标 → index 收敛到 4 → 移除非持仓 deep（BTC/持仓始终保留）→
  index 清空。生成后 `prompt_token_estimate <= budget`；AI prompt 视图由
  `02-AI决策链` 从同一 `evidence.json` 按角色再派生（只删字段，不改语义）。
- 无账户/密钥字段；账户、仓位、成交由 02 在拼 prompt 时合并。

### 5.1 Cycle / Host 顺序（M1.3c）

`POST /api/cycles` 是 Host 一轮的唯一入口，顺序固定：

1. Host 调 `POST /api/cycles`（可带 `slot_id`、`trigger`、`prev_pick`；body 里的
   `cycle_id` 只是建议值，服务按 DD-09 **自行生成权威 `cycle_id`**，Host 必须 adopt
   响应值）→ 服务生成 `cycle_id`，通过 market 层生成并落盘 `md-evidence/1`，写入
   `cycle_runs(RUNNING)` + `evidence_snapshots`，返回
   `{cycle_id, evidence{snapshot_id,path,sha256,...}, account, risk_envelope}`；
2. Host 用该 `cycle_id` 跑 AI 链，并写 audit：
   `POST /api/cycles/{id}/ai-runs` → `PUT .../calls/{call_id}`（每次尝试独立
   call_id）→ `PUT .../artifacts/{agent}`（`attempt` 递增，不覆盖历史）；
3. P1 `POST /api/risk/evaluate` 必须携带 **同一个 `cycle_id`**；服务端
   `_server_evidence(cycle_id)` 只从 `evidence_snapshots` + 落盘文件读取证据，
   找不到/跨 cycle/hash 不符 → 422 `E_EVIDENCE_UNAVAILABLE`；
4. P2/P1 既有 demo-only 闸门不变；无 Demo key 时 `account.credentials_configured=false`，
   cycle 仍可创建并按 `no_trade` 收口；
5. 最后 `POST /api/cycles/{id}/finalize`
   `{state:DONE|FAILED|SKIPPED|CANCELLED, outcome:trade|no_trade|error, reason, execution}`。
   只允许 RUNNING→终态；同结果重复调用返回 `idempotent=true`，不同结果 409。

约束：同一时刻最多一个 RUNNING cycle（否则 409 `E_CYCLE_RUNNING`）；`slot_id` 全局唯一
（重复 409 `E_CYCLE_DUPLICATE_SLOT`）。创建失败不会留下半条 cycle/evidence 引用。
**跨 cycle 不串证据**：P1 的 `cycle_id` 必须来自本轮 `POST /api/cycles` 响应，不能复用旧 cycle。

## 6. Demo 数据限制（如实标注，不伪造）

- `/futures/data/*`（OI 历史、long/short、taker ratio）在 demo 常不可用 →
  `DERIVATIVES_PARTIAL`（warn，不阻断），对应字段为 `null`。
- demo `openInterest` 量级不可信：当 `OI × mark` 超过 24h 成交额 20 倍时，
  `oi_usd=null` 并在 issue detail 记录 `implausible demo openInterest scale`。
- 无 WS：`sources.ws.state="disabled"`、`last_frame_age_s=null`；M1.1 全部 REST 轮询。
- `O(200)` 量级 warmup 使单次证据构建约 8–15 个上游请求（8 deep 约 20–32 weight）。

## 7. 环境变量（只读进程 env，不加载 `.env`）

| 变量 | 默认 | 说明 |
|---|---|---|
| `WORKBENCH_HOST` | `127.0.0.1` | 仅允许 loopback |
| `WORKBENCH_PORT` | `8791` | 1–65535 |
| `WORKBENCH_DATA_DIR` | `trading-workbench/data` | 运行时目录；token 文件 `data/.api-token` |
| `WORKBENCH_LOG_LEVEL` | `info` | debug/info/warning/error/critical |
| `WORKBENCH_DEMO_BASE_URL` | `https://demo-fapi.binance.com` | 非 demo 域名拒绝启动（退出码 2） |
| `WORKBENCH_PROBE_SYMBOL` | `BTCUSDT` | 正则 `[A-Z0-9]{2,15}USDT` |
| `WORKBENCH_PROBE_INTERVAL` | `15m` | 15m/1h/4h/1d |
| `WORKBENCH_PROBE_LIMIT` | `30` | 1–100 |
| `WORKBENCH_PROBE_TIMEOUT_S` | `10` | 1–30 |
| `WORKBENCH_PROBE_CACHE_TTL_S` | `5` | 0–60 |
| `WORKBENCH_AUTH_MODE` | `bearer`（服务入口） | `bearer`/`dev-open`；只有显式 `dev-open` 才关闭 Bearer |
| `WORKBENCH_API_TOKEN` | 空 | **优先**于文件；设置后使用该 token，不读/写 token 文件 |
| `BINANCE_DEMO_API_KEY/SECRET` | — | M1.2 执行层读取（仅 demo，只读启动对账；M1.3c 不需要） |

`data/.api-token` 不存在时首次启动自动生成；不可读/不可写会启动失败（退出码 2），
不会静默降级为 dev-open。日志只打印 `source`/`path`/`token_ready`。

> 测试/脚本读取 token 时建议 `(Get-Content ... -Raw).Trim()`；文件可能带尾随换行，
> 原样拼进 HTTP header 会被 uvicorn 判为非法请求。

## 8. 存储

- 库：`trading-workbench/data/workbench-v1.sqlite`（WAL）；证据表 `evidence_snapshots`。
- M1.3c 表：`cycle_runs`、`cycle_agents`、`ai_runs`、`ai_calls`、`ai_outputs`、
  `ai_run_events`（与 M1.2 账本同库，幂等建表；旧 `cycle_runs` 自动补列）。
- 证据：`trading-workbench/data/evidence/<date>/<cycle_id>/evidence.json`。
- 冷却：`trading-workbench/data/state/market_rate_limit.json`。
- 日志：`trading-workbench/data/logs/service.log`（10MB × 5 轮转，UTC 单行，无 key）。
- 旧库 `multi-agent-trader/data/agent_trader.sqlite` **不 attach、不 import、不读写**。

## 9. 安全边界

- 无签名、无 key、无下单：市场层只发 GET；`transport` 构造后断言不存在
  `Authorization`/`X-MBX-APIKEY`/`X-MBX-SIGNATURE` 头。
- 基址被 `guard` 锁定为 `demo-fapi.binance.com`；守卫失败时市场端点直接 403，
  **不发任何上游请求**。
- 默认 Bearer；`/api/*` 无 token → 401 `E_AUTH_REQUIRED`；Bearer 模式无 token 配置 →
  503 `E_AUTH_NOT_CONFIGURED`（fail-closed）。
- 只绑定 loopback；错误信封不泄露堆栈；日志不打印请求头/凭据/token。

## 10. 测试

全部离线：本地假币安服务（200/429/418/timeout 分支）+ 临时目录 SQLite + FastAPI
TestClient，不访问真实网络。

```powershell
cd trading-workbench

# 全量（M1.0 smoke + M1.1 market/evidence + auth）
python -m unittest discover -s trading-workbench/service/tests -v

# M1.3c cycle/evidence/audit（离线 fake market/execution，不联网/不下单）
python -m unittest discover -s trading-workbench/service/tests_service_cycles -v

# M1.2 执行/风控（离线 fake demo）
python -m unittest discover -s trading-workbench/service/tests_execution -v

# 只跑 M1.1 行情/证据回归
python -m unittest tests.test_m1_1_market -v   # 在 service/ 目录下执行

# 编译检查
python -m compileall trading-workbench/service
```

覆盖点：闭合 bar/warmup/聚合/缺口；指标边界与前缀一致性（无未来函数）；
20+8+index 选池；四态 quality 与 issue code；`md-evidence/1` 正/反例；
429/418 不重试 + 冷却持久化；`/futures/data` 5m 独立预算（滚动/临界/优先级）；
health 10s 缓存命中 + 强制刷新 + 冷却期可达；token 预算硬收敛；DB 失败 fail-closed；
API 端点与错误映射。

## 11. 已知限制 / 下一步

- 未实现真实下单入口（`POST /api/execution/order` 仍为 M1.2 既有 P1/P2 demo-only 逻辑）；
  M1.3c 只做 cycle/证据/审计持久化。
- 无行情 WS；M1.1 全 REST（`sources.ws=disabled`）。
- Demo `/futures/data/*` 不稳定、OI 量级异常，见 §6；M1.2 接入真实账户后再评估。
- AI 编排在 Host 侧（M1.3a/b），本服务只提供 cycle/audit 数据源；真实 Demo key
  配置后仍须人工显式 P2，不会自动下单。
- 干净退出写 `data/service.lock.released_at`；强制终止后内容可能仍显示运行中，
  但 OS 锁已释放，新实例可接管。
