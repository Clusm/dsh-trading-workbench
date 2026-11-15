# 运行交付：启动、停止与核对（F1 / §5.1 / §8·F）

这是一份操作说明，不引入部署平台。只有三个脚本 + 一个 release 生成步骤。

## 1. 构建源码发布标识（每次改代码后）

```powershell
cd trading-workbench
node scripts/build-release.mjs            # 写 scripts/.release/plugin-release.json（部署产物）
node scripts/build-release.mjs --check    # CI/自检：清单是否与源码一致
```

- `scripts/.release/plugin-release.json` 是**部署产物**：`release_id` + `source_digest`（31 个源码文件的内容摘要）。
- Host 的 `build.js` 在模块加载时读一次，之后只从内存回传；**不会**每个请求重读磁盘。
- 因此 `release_id`（打包了什么）与 `loaded_at`（本进程何时加载）是两个独立事实。

## 2. 启动服务（显式目录 + 端口）

```powershell
cd trading-workbench
./scripts/start-service.ps1 -Port 8791 -DataDir "C:\Users\Tao\Desktop\币安\trading-workbench\data" -Background -HostBaseUrl http://127.0.0.1:58707
```

- `-DataDir` 会解析成**绝对路径**并打印；`-Port` 同时决定 `--port` 与核对用的 URL。
- `-Background`：后台启动（日志落在 `<data-dir>/logs/`），等 `/health` 就绪后调用核对脚本。
- 前台启动（不加 `-Background`）时，脚本会打印一条独立的核对命令，另开一个终端执行。
- `-HostBaseUrl` 是 DSH Web 的地址（运行工作台插件的那个进程）；不传则读 `WORKBENCH_HOST_BASE_URL`，都没有时脚本明确警告 Host 侧无法核对，不会假报通过。

## 3. 核对 Host 与 Service 身份（只读）

```powershell
node scripts/verify-runtime.mjs --host http://127.0.0.1:58707 --service http://127.0.0.1:8791 --data-dir "C:\Users\Tao\Desktop\币安\trading-workbench\data"
```

检查项：

| 检查 | 含义 |
|---|---|
| `host.data_dir` | Host 报告的绝对数据目录 |
| `identity.data_dir` | Host 与 Service（`/health.db.path` 的父目录）指向同一目录 |
| `identity.ledger` | Host 经 `/api/risk/envelope` 看到的 ledger 与 Service `db.ledger_id` 一致 |
| `identity.port` | Host 配置的服务端口 == 实际监听端口 |
| `identity.control` | Host 与 Service 的控制态一致 |
| `service.venue` | 环境固定 demo |
| `release.manifest / release.manifest_current` | 打包清单存在且与当前源码一致 |
| `release.loaded` | 运行进程回传的发布标识 |
| `release.loaded_matches_build` | 运行进程加载的正是这份打包产物（不一致 = 进程跑的是旧代码） |
| `release.loaded_at_separate` | 加载时间与发布标识分开 |
| `automation.state` | 自动运行状态 + 原因码 + 调度开关 + 服务端控制态 + 最近成功时间 |

退出码：0 全通过（可含 warning）；1 有失败；2 端点不可达/参数不足，无法核对。

## 4. 停止

- **停止自动交易**（正常停止）：在工作台顶栏点「停止自动交易」。
  Host 关闭本机调度并 `POST /api/control {state:'pause', confirm:'PAUSE', reason}`，
  **不平仓、不撤交易所保护单**；状态以 Service 控制态为准，刷新/重启都不会丢。
  服务端不可达时显示「本机调度已停，服务端状态待确认」，不会谎称已停止。
- **停止服务进程**（维护用）：

```powershell
./scripts/stop-workbench.ps1 -DataDir "C:\Users\Tao\Desktop\币安\trading-workbench\data"
```

  只结束进程，**不改变**控制态；重新启动后用第 3 步核对身份与发布版本。
  需要继续自动交易时，显式点「启动自动交易」（Host 会先核验身份/数据/未决状态）。

## 5. 已知边界

- 这份说明不覆盖 Windows 服务化、多实例、双环境并存：本工作台是单实例单账户。
- `release.loaded_matches_build` 为 warning 表示「进程加载的不是当前打包产物」，通常是改了代码没重启 Host。
