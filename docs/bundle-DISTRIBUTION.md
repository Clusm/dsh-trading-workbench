# 分发与一键安装（作者向）

目标：别人在自己的 DeepSeek Harness 上装一次就能用 —— 不是"能加载一个面板"，而是"面板打开、数据出来、能跑一轮"。

---

## 1. 分发形态

DSH 的分发单位是 **bundle 包**：一个 npm 包，`package.json` 里声明 `dsh.bundle.patch`（挂载到 Host 组合）和
`dsh.client`（Web 端注入）。安装动作只有一个，三条入口等价：

| 入口 | 命令 / 操作 |
| --- | --- |
| DSH 界面（推荐给使用者） | 设置 → 插件 → 搜索包名 → 安装 |
| CLI | `dsh plugin --profile <profile> add dsh-trading-workbench` |
| Agent 工具 / 脚本 | `plugin_manager({action:'install_bundle', target:'dsh-trading-workbench'})` |

发布渠道按"门槛从低到高"：

1. **本地 tgz / 目录**（内测）：`npm pack` 后把 tgz 发给对方，`add ./xxx.tgz`。零账号、零审核。
2. **Git 仓库**：`add github:<user>/<repo>`（或 `git+https://…#<commit>`）。需要对方允许 build 脚本。
3. **npm 公开包**（正式）：`npm publish`，之后所有人都能在插件页搜索到。
4. **市场收录**（被发现）：向 `awesome-dsh-plugin` / `dsh-plugin-catalog` 提交条目，进入 `https://cdn.jsdelivr.net/npm/dsh-plugin-catalog/index.json` 这份 format-1 索引。条目形状见 `docs/market-entry.example.json`。

## 2. 打包

```sh
node trading-workbench/scripts/build-bundle.mjs          # -> trading-workbench/dist/dsh-trading-workbench/
node trading-workbench/scripts/build-bundle.mjs --tarball  # 顺带 npm pack
```

脚本做四件事：

1. 只拷运行时文件（`index.js`、`client.js`、`host/**`、`locale/**`、`icon.svg`、`cordis.patch.yml`）；
2. 把 `service/`（Python 交易服务源码 + `requirements.txt`）一起打进去，**排除** `__pycache__`、日志、`tests_*`、`.venv`；
3. 把 `cordis.patch.yml` 里的包名改写成发布名（不能留着 `@local/...`）；
4. 生成发布用 `package.json`（`dsh.bundle` + `dsh.client` + `files` + `peerDependencies`）。

当前产物：**~108 个文件 / 615 KB tgz / 2.3 MB 解包**（Python 源码占大头）。

## 3. 现在就能做到 vs 还差的

| 能力 | 状态 |
| --- | --- |
| 一键**安装**（插件页 / CLI / tgz） | ✅ 包结构已符合 bundle 规范 |
| 安装后出现工作台、AI 决策页 | ✅ |
| 没有服务时的诚实降级（服务不可达 + 原因） | ✅ |
| 一键**启动**本机服务（venv 已存在时） | ✅ `/api/service/start`，`/health` 就绪才算成功 |
| 一键**初始化**服务（没 venv / 没装依赖的机器） | ✅ 已实现：`/api/setup/readiness` + `/api/service/setup`（作业）+ 首启向导 |
| 数据目录不落在 `node_modules` 里 | ✅ 默认落用户目录；包内 `data/` 已有内容时继续沿用，**不孤立历史归档** |
| Demo 凭据录入（不用手改 `.env`） | ✅ 设置 → 连接与账户 → **Demo 接入**：写 `<dataDir>/credentials/demo.env`（0600），只回指纹 |
| 启动路径不依赖参考项目 | ✅ python / dotenv 都不再硬编码 `multi-agent-trader` |
| 市场收录 | ❌ 待提交 |

## 4. 要实现"一键运行"，还差的部分（§4.1–4.3 已完成）

### 4.1 服务引导（✅ 2026-11-15 已完成）

对方的机器上不会有 `multi-agent-trader/.venv`。Host 现在提供一次性的环境准备（已实现）：

```
GET  /api/setup/readiness  -> { ready, needsPython,
                                python:{found,path,version,source},
                                venv:{dir,python,exists,in_use},
                                deps:{installed,venv_installed,interpreter,detail},
                                service:{ok,...}, dotenv:{path,exists},
                                requirements:{path,exists},
                                blockers:[{code,message}], checked_at }
     blockers: PYTHON_MISSING | VENV_MISSING | DEPS_MISSING | SERVICE_DOWN

POST /api/service/setup     -> { ok, job_id, reused, job }      # 单飞：重复点击返回同一个作业
GET  /api/service/setup/:id  -> { ok, job:{ id, phase,
                                 steps:[{id,label:{zh,en},status,started_at,finished_at,exit_code,log_tail}] } }
     phase: running | ready | failed
     steps: create-venv（已存在则 skipped）→ install-deps → start-service → verify
```

- 解释器发现顺序：`WORKBENCH_SERVICE_PYTHON` → `<dataDir>/service-venv` → 包内 `.venv` → 参考项目 venv（作者机兜底）→ 系统 `py -3` / `python3` / `python`；每个候选都**实际跑一次**打印版本，跑不通的不算数。
- venv 位置：`<dataDir>/service-venv`（不放进包目录，升级不丢）。
- 依赖：只用包内 `service/requirements.txt`；每步保留最后 40 行输出，失败时把尾部日志带进作业，不能只说"失败"。
- **成功判定只认 `/health`**：`verify` 步骤探测服务自己的健康检查，通了才 `phase=ready`。
- 首次向导 = 这个接口的 UI（AI 决策/操作台顶部的「初始化服务」）：检测 → 一键初始化 → 逐步进度 → 失败可重试；没有 Python 时直接给"装 Python"的说明，不渲染一个无效按钮。

### 4.2 数据目录（✅ 2026-11-15 已完成）

旧行为：默认 `<包目录>/data/`，装进 profile 后落在 `node_modules` 里，升级即丢。
现在的解析顺序（`resolveDefaultDataDir`）：

1. `config.dataDir` / `WORKBENCH_DATA_DIR`（显式优先）；
2. 包内 `data/` **只要已经有文件就继续沿用**——否则升级会把现有轮次归档孤立掉；
3. 都没有时落 `$DSH_HOME/data/trading-workbench`（没有 `DSH_HOME` 时用 `~/.dsh/data/trading-workbench`）。

`/health` 新增 `dataDir` 字段回报**实际**路径，用户不必再猜"我的归档去哪了"。
凭据（`credentials/demo.env`、`credentials/live.env`）跟 dataDir 走，因此同样不随升级丢失。

### 4.3 配置与平台（✅ 2026-11-15 已完成）

- `engines`：已在真实宿主 **`dsh 0.2.2-rc.1`** 上验证，写入
  `"dsh": ">=0.2.2-rc.1 <0.3.0-0"`、`"node": ">=22.19"`；构建脚本把它带进发布 manifest，装不上的机器会被明确拦下而不是运行到一半崩溃。
- 平台：venv 路径、解释器名、`Scripts` / `bin` 都已按 `process.platform` 分支
  （`serviceLauncherPaths`、`service-setup.discoverPython`、`resolveSystemPython` 三处一致），
  Linux/macOS 走 `bin/python`，同一份 `run_service.py`。
- Demo 凭据：**不需要手改 `.env`**。设置 → 连接与账户 → Demo 接入，明文只写本机 0600 文件
  （`<dataDir>/credentials/demo.env`），浏览器拿不到、日志不记、不进插件配置（基线 §5.3：Demo key 只放本机）。
  仍然是"没有凭据 → 只读、不阻断安装"，只是入口从文件变成了界面。
  - 解析顺序：`WORKBENCH_DOTENV_FILE` → `<dataDir>/credentials/demo.env` → 包内 `.env.local`（旧安装兜底）。
  - `service/start` 拉起服务时 Host 自动传 `WORKBENCH_DOTENV_FILE`；手动起（`scripts/start-service.ps1`）用同一顺序。
  - 文件已写但服务未重启时，界面会明确说"需重启才生效"，不会让两处状态互相矛盾。

## 5. 发布步骤

```sh
# 1) 改版本（语义化：插件 API 变更升 minor，修 bug 升 patch）
node -e "…"   # 或直接改 plugin/package.json 的 version

# 2) 构建 + 打包
node trading-workbench/scripts/build-bundle.mjs --tarball

# 3) 先在干净 profile 里真装一遍（别人的机器上等价）
dsh plugin --profile maketest add ./dist/dsh-trading-workbench-0.1.0.tgz
dsh --profile maketest --dump-config | grep trading-workbench   # 应出现在 bundle 列表
#    打开页面确认：侧边栏入口、AI 决策页、服务不可达 + 启动服务按钮

# 4) 发布
cd trading-workbench/dist && npm publish ./dsh-trading-workbench --access public --tag latest

# 5) 收录（可选，进入市场索引）
#    按 docs/market-entry.example.json 的形状向 catalog 提交条目（name/version/repository/category）
```

版本纪律：**每个已安装的用户都会跟着 `latest` 升级**，所以破坏性改动（配置键、路由、数据格式）必须升 minor 并在 README 写迁移说明。

## 6. 验收清单（发布前必过）

- [ ] 干净 profile 安装 → 重启 → 侧边栏有入口、AI 决策页可开
- [ ] 没有服务时：横幅说明"服务不可达"，且"启动服务"可用；没有 Python 时给出明确原因
- [ ] 一键初始化能建 venv、装依赖、拉起服务；失败带 pip 日志尾部
- [ ] 行情/账户/持仓/权益来自服务，不是本地编造
- [ ] 归档按日分文件（`rounds/YYYY-MM-DD.jsonl`），历史页能按日期查
- [ ] 原因码/归因中英文都随 中文/EN 切换
- [ ] 卸载插件不影响交易所仓位与保护单
- [ ] 包内**不含**任何凭据、token、日志、`.venv`、`__pycache__`

## 7. 已知限制（写进 README，别让用户踩）

- 交易服务是独立 Python 进程；关闭 DSH 应用时它可能随之退出（长期后台跑用 `scripts/start-service.ps1`）。
- 插件不代管交易所凭据；没有凭据时全链路只读。凭据由本机文件承载，在「连接与账户」里录入。
- 归档默认全量保留，最坏约 2 MB/日；磁盘紧张用 `WORKBENCH_ROUNDS_RETENTION_DAYS`。
