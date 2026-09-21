# E / U10a 初审报告（r1）

- 审查对象：head `23bfe88`（分支 `task/U10a`），base `f5e4790`（集成 HEAD，含 U00–U15 除 U08）
- 审查者：E 角色（可恢复性 / 工件完整性 / 权限），独立会话（非作者会话）
- 工作区：`C:\Users\Tao\Desktop\币安\mat-wt\reviews\U10a\E\r1`（detached，回读确认 `git rev-parse HEAD` = `23bfe881d04fbc7198b64d12df96298c4089ba7e`）
- 解释器：`C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`，`PYTHONUTF8=1`（无 pytest）
- 探针与原始输出：`_scratch_E/`（`trees/`、`pkg/`、`verify/`、`delta/`）；本报告所有数字来自这些原始输出
- 未联网；未读 `.env`/`data/`/密钥/账户标识；未启动服务；未执行 `sc.exe`；未写其它 worktree

## 结论

**有条件 GO**（无 P0/P1；**3×P2 + 7×P3**，均为"文档一致性 / 纵深防御 / 表述准确性"类，不改变核心交付物的正确性）

核心判定：

- **"不漏包"成立**：两处修复**真实且完整**，差集逐项列出（§1.1）；反向"多包"检查见 §1.3。
- **解压自检可复核**：13 项两次运行均 `ok=true exit=0`；自造 11 类负例全部按预期失败（§3）。
- **补丁可应用**：三种 git 配置 + 集成 HEAD 干净内容树 `apply --check` 全 OK；三文件逐字节相同；02 默认关闭成立（§6）。
- **全量回归 delta = ∅**：82 个共有脚本逐条 exit 码完全一致，两侧失败 0；唯一差异是新增登记的 `smoke_u10a_release_package.py`（§7）。
- **门禁全绿**：`check_quality OK files=287`、`SECRET SCAN OK files=520`（§8）。
- **新门禁非空转**（双向验证通过，§5.2）。

---

## 0. 审查方法（可复现）

两棵沙箱树的构造与保真度校验（关键前提）：

| 树 | 内容 | 已跟踪文件 | 保真度 |
|---|---|---|---|
| `_scratch_E/trees/base` | `git archive f5e4790` → `git init` + `git add -A` | 558 | 与 `git ls-tree -r f5e4790` **逐路径 IDENTICAL** |
| `_scratch_E/trees/head` | `git archive 23bfe88` → `git init` + `git add -A` | 563 | 与 `git ls-files`（=563）**逐路径 IDENTICAL** |
| `_scratch_E/trees/head_patched` | head + 补丁 01/02/03 | 563 | 三文件改动已 grep 复核 |
| `_scratch_E/trees/autocrlf` | base + 提交 + `core.autocrlf=true` 重新落盘（测 known_limit ③） | 558 | CRLF 已实测（§9.3） |

两棵树各自用仓库真实 API（`core.release_manifest.build_release_manifest`）生成
`data/release_manifest.json`（不读 `.env`、不联网），再打包。`data/` 被 `.gitignore`
忽略，不影响 `git ls-files` 盘点（已复核头部清单两侧一致）。

---

## 1. 核心：不漏包 / 不多包

### 1.1 两处"真实漏包"的独立核验（差集清单）

**实验（隔离旧/新打包器，2×2 矩阵）**：旧实现 = `f5e4790:scripts/create_release_package.py`
（192 行，已存档 `_scratch_E/old_create_release_package.py`）；新实现 = head 版（999 行）。

| 组合 | 输出 | 文件数 |
|---|---|---|
| 旧 × base 树 | `pkg/old_base.zip` | 261 |
| 旧 × head 树 | `pkg/old_head.zip` | 262 |
| 新 × base 树 | `pkg/new_base.zip` | 214 |
| 新 × head 树 | `pkg/new_head.zip` | **214**（与作者自测一致） |

差集（`old_head.zip` vs `new_head.zip`，原始清单见 `pkg/diff_old_vs_new_head.txt`）：
**旧少了 69 个文件**。逐项归类如下（**全部为"修复"性质，无一项是凑数**）：

| 组 | 个数 | 文件 |
|---|---|---|
| `src/data/*.py`（数据接入层，**修复①的核心**） | **13** | `__init__ / binance_public / binance_request_policy / binance_transport / cache / kline_store / macro_public / market_query / market_stream_cache / market_universe / onchain_public / price_stream / rate_limit` |
| `src/dashboard/templates/*.html` | **8** | `agents / base / channels / decisions / events / index / orders / reviews` |
| `knowledge/` | **3** | `strategy_cards.json`、`backup_strategy_cards.json`、`策略报告_整理版.md` |
| `src/strategy_runtime/PROVENANCE.md` | 1 | 与 `docs/文档索引.md` 的"策略来源（发布包内保留）"一致 |
| `.env.example`、`.gitignore` | 2 | 部署方据此建 `.env`；`.gitignore` 自带运行时目录忽略规则 |
| `scripts/start_web.bat` | 1 | 手册 §2 登记的诊断入口 |
| `docs/contracts/**`（README + 6 schema + 6 golden） | 13 | 契约 schema/golden 现在进包 |
| `docs/**` 其余现行文档（含 `文档索引.md`、`技术报告.md`、`dispatch/任务卡.md` 等） | 26 | 旧实现只有 5 篇 `SAFE_DOCS` 白名单 |
| `docs/dispatch/任务卡.md`、`角色卡.md` | 2 | 见 §1.4（是否应进包属判断问题） |

**逐项校验作者的原始表述**：

- "`src/data/*.py`(13)" → **准确**（`git ls-files src/data` = 13）
- "`templates/*.html`(8)" → **准确**（`git ls-files 'src/dashboard/templates/*'` = 8）
- "`knowledge/`" → **准确**（3 个）
- "`PROVENANCE.md` / `.env.example` / `start_web.bat`" → **准确**（各 1）

**根因确认**：旧 `_safe_relative_files`（存档第 90 行）
`if any(part in {"data", ".venv", "__pycache__", ".git"} for part in Path(relative).parts): continue`
——`src/data/foo.py` 的 parts 含 `data` → 被静默丢弃；同时旧实现只把
`src/**.py` / `scripts/**.py` 加入候选，`templates/*.html`、`knowledge/`、`PROVENANCE.md`、
`.env.example`、`start_web.bat` 从未进入候选 → 一并丢失。**修复方式真实**：新实现
把 `data` 降级为 `EXCLUDED_TOPLEVEL_DIR_NAMES`（仅第一层，第 89-99 行），并对
`RUNTIME_ASSET_ROOTS = ("src/","config/","knowledge/")` 加了硬断言（第 626-633 行）。

### 1.2 关于 claim ②（`git ls-files` 缺 `-z`）——**表述不准确（P3）**

作者称 `-z` 缺失导致"中文名文档被静默漏掉"。**反事实实验**：

| 实验 | 结果 |
|---|---|
| 旧打包器 + 仅补 `-z`（`_scratch_E/old_zfixed.py`）× head 树 | `files=262`，与未修版**成员集合 0 差异**（`pkg/diff_old_vs_oldz.txt`：`only_in_left=0 only_in_right=0`） |
| 新打包器 **去掉** `-z` × head 树（`_scratch_E/run_noz.py`） | `real_tracked=563 buggy_tracked=563`，`lost_paths=33`（33 条被转义成 `"docs//344/270/..."` 形态），随后 **`ReleasePackageError: 存在未归类文件`** → 构建**硬失败**（fail-closed），不是静默漏包 |

结论：`-z` 对**新**实现是**必需**（否则构建直接失败，而非少文件）；但对**旧**实现的输出
**没有任何影响**——旧实现丢中文文档的根因是 `SAFE_DOCS` 只有 5 篇的白名单设计，
不是 `-z`。`-z` 是真缺陷（值得修），但不是"漏中文名文档"的成因。**建议**：
`docs/代码维护规范.md:11.2` 与补丁 README 的因果表述改为"`-z` 缺失在**新设计**下会让
非 ASCII 路径无法归类而**构建失败**"。

### 1.3 反向检查：有没有"多包"（不该进包的进了）？

**结论：无凭据/运行状态混入；但有 3 项"工件卫生"问题（见 P2-3 / P3-4/5）。**

1. **路径规则无法多包**：`build_release_package` 对每个入包文件重跑 `_excluded_reason`
   （第 654-657 行），命中即 `ReleasePackageError`；`RUNTIME_ASSET_ROOTS` 被误伤也失败。
   实测 11 类负例 + 全部 `--verify` 检查均按预期报红（§3）。
2. **凭据/状态**：独立正则（13 条规则，不与仓库扫描器共享来源）扫 216 个 zip 成员 →
   **0 条凭据形状命中**（详见 §2）。
3. **唯一实质"多余"项**：`docs/无人值守升级任务清单_2026-09-19.json`（195 KB 台账）
   进了 runtime 包，且它命中仓库扫描器的 KI04 范围豁免（`scripts/scan_tracked_secrets.py:326`
   `_EXEMPT_FILES`），因此在包内**不被内容扫描**。`--verify` 的报告**如实列出**了这一点
   （`credentials_absent.detail.repo_exempt_files`），不是隐瞒——但豁免理由是"流程记录"，
   该理由在"部署工件"语境下不成立。→ **P2-3**
4. `docs/文档索引.md:87` 标题写"**执行编排（流程记录，仅源码仓库）**"，
   第 91 行却列"**进发布包**：…[机器可读任务清单]…"——同一节自相矛盾。→ **P3-6**

### 1.4 `unclassified:0` 是否真的覆盖全部已跟踪文件（自行复算）

```
tracked_total                      = 563   （独立 `git ls-files` 计数 = 563 ✓）
included(runtime_assets)           = 214
source_only                        = 162
excluded                           = 187
214 + 162 + 187                    = 563   ✓
```

- 两种包的成员并集：`runtime 214` + `source 376`（214+162）→ `563 - 187 = 376` ✓
- **187 条排除项全部位于 `docs/dispatch/`**（142 补丁文本 + 42 评审证据 + RESUME + w0 盘点 + 交接说明），
  **没有一条** 落在 `src/`、`config/`、`knowledge/`、`scripts/`。即"排除面 = 纯流程记录"。
- `unclassified` 在构建期是硬闸门（第 622-625 行 raise），`package_manifest.json` 里的
  `"unclassified": 0` 是常量（第 567 行）——不是"假绿"，因为未归类必然抛错。

### 1.5 "空转闸门"测试：**新实现会不会静默丢包？** → **发现一个真实缺口（P2-1）**

`RUNTIME_SCRIPTS`（第 179-197 行，17 项）**没有"登记项必须存在"的断言**，且
`scripts/` 下的兜底分类是**一律 source-only**（第 328 行 `_source_only_script_reason`
的 fallback 字符串）。两者叠加的后果，实测：

```
在 head_patched 沙箱里 git mv scripts/run_web.py scripts/run_web_renamed.py
$ python scripts/create_release_package.py --out ... --kind runtime
BUILD EXIT=0          ← 静默通过
files=213             ← 比正常少 1（启动入口消失）
inventory: runtime_assets=213 source_only=163 unclassified=0
```

原因：`scripts/run_web_renamed.py` 不匹配任何 `SOURCE_ONLY_SCRIPT_PREFIXES` →
走 fallback → source-only；而构建侧的必需项过滤是
`missing = [r for r in required if r not in files and (root / r).exists()]`
（第 640-641 行）——**不存在的必需路径被 `.exists()` 直接跳过**，于是不报错。

**缓解事实（已实测，故定 P2 而非 P1）**：更严格的两个入口**都会**抓到：

- `--self-check --kind runtime` → exit 2（`required_assets_present: missing=['scripts/run_web.py']`）
- `smoke_u10a_release_package.py` → exit 1，**5 项 FAIL**（含"脱敏配置模板与部署入口入包"、"解压目录自检 CLI 退出码 0"）

**建议**：把构建侧第 640-641 行的 `.exists()` 过滤去掉（或在 `--out` 路径上也跑一次
"必需项存在性"断言），使"必需清单里的路径不存在"与"存在但没进包"同样报错。
verifier 侧第 877 行**没有**这个 `.exists()` 过滤，所以自检是严的——两侧口径不对称。

### 1.6 消费者齐备性（按手册声称的启动路径独立核对）

`_scratch_E/consumer_check.py` 结果：

```
startup-path members missing from package: 0     （31 项：run_web.py / windows_service_host.py /
    validate_windows_service_plan.py / start_web.bat / health_watch.py / preflight_live_account.py /
    report_release_readiness.py / scan_tracked_secrets.py / create_release_package.py /
    create_recovery_bundle.py / restore_recovery_bundle.py / ops/verify_recovery_plan.py /
    ops/verify_runtime_paths.py / verify_profile_lock.py / verify_dependency_lock.py /
    smoke_db_migrations.py / config/windows_service.json / settings.yaml / decision_policy.yaml /
    agents.yaml / risk_learning.yaml / risk_profile.lock.json / strategy_cards.json /
    backup_strategy_cards.json / requirements-lock.txt / requirements.txt / .env.example /
    .gitignore / README.md / CONTRIBUTING.md / pyproject.toml）
COVERAGE src/       tracked=133 packaged=133   missing=[]
COVERAGE config/    tracked=6   packaged=6     missing=[]
COVERAGE knowledge/ tracked=3   packaged=3     missing=[]
```

→ **runtime 包内 `src/` 是完整的 133 个已跟踪文件**（含模板、`src/data/` 数据层、
`PROVENANCE.md`），`config/` 与 `knowledge/` 完整。手册 §3.2 的启动链（包内建 venv →
按 `.env.example` 建 `.env` → SCM 计划校验 → 启动）所需文件**齐备**。

**但发现一处消费者不一致（P2-2）**：手册 §9.1（第 390-403 行）给出的
"**当前临时运行（SCM 按用户决定暂缓）**"启动命令、以及第 423 行"live 现在**只能**走
`start_web_detached.py`"，用的都是 `scripts/start_web_detached.py` —— 该脚本被
`SOURCE_ONLY_SCRIPT_PREFIXES` 判为 **source-only**，**不在 runtime 包内**。
而手册自己在第 192 行定义："标 `[source 包]` 的命令**只存在于 `source` 包或源码检出**"，
§9.1 却**没有**这个标记（全文只有 §5/§8/§9.5/L08 带标记）。
→ 只拿到 runtime 包的运维**按手册执行 live 启动会直接"文件不存在"**，且手册的标记
约定没能提示这一点。（`scripts/run_web_service.py` 同理，但 §2 第 43 行是"禁止并行"
的说明而非启动指令，影响较小。）

---

## 2. 凭据 / 运行状态不入包

**独立解包扫描**（`_scratch_E/indep_scan.py`，13 条自研规则：
`sk-`/AWS AKID/PEM 头/`api_key|secret|passphrase|mnemonic` 赋值/Bearer/Telegram bot/Discord
webhook/ETH 地址/邮箱/Windows 与 POSIX 用户目录/`.env` 形态/`chat_id|bot_token`）。

结果（`pkg/indep_scan3.txt`，共 20 行）：

| 规则 | 命中 | 判定 |
|---|---|---|
| `api-key-assign` | 15 | **全部是代码标识符/形参引用，不是凭据字面量**（原文见 `pkg/hit_lines_masked.txt`，已按 KI04 纪律掩码）。形态分布：`api_key = <os.getenv 表达式>`（`src/llm/factory.py`、`src/exchange/factory.py`、`src/intel/service.py:659`）、`api_key = str(getattr(...))`（`src/dashboard/app.py`）、`self.api_key = str(<形参>)`（`src/exchange/user_stream.py`）、`self.api_secret = <构造形参>`（`src/exchange/binance_futures.py`）、**关键字实参** `api_key=<形参>,`（`src/llm/factory.py`、`src/dashboard/app.py`、`src/exchange/factory.py`）、以及 `scripts/scan_tracked_secrets.py:48` 的 docstring 引用形态。**无一处右值是字面量密钥** |
| `posix-user-path` | 3 | `docs/多Agent执行分派手册_2026-09-20.md:51`、`docs/无人值守升级任务清单_2026-09-19.json:3482/3491` → 见下（路径/用户名，非凭据） |
| `win-user-path` | 2 | `docs/开发规格书_v2.md:21`、`docs/项目说明书.md:139` → 见下 |
| `sk-key` / `aws-akid` / `pem-header` / `bearer` / `telegram-bot` / `discord-webhook` / `eth-address` / `email` / `.env` 形态 | **0** | 无 |

**"3 处类型注解"的核对**：我的规则集比作者宽，命中 15 处（不是 3 处），但**结论一致**：
无任何一处是真凭据；全部是"变量/形参引用"或一句 docstring。作者的"3 处"应是其规则集
的口径（更窄），不影响结论。

**压缩包元数据**（`_scratch_E/zipmeta.py`，`pkg/zipmeta.txt`）：

```
comment=b''                       ← 无归档注释
members=216
absolute_or_traversal_members=[]  ← 成员名全部为相对路径，无 .. / 盘符 / ':' 
members_with_extra_fields=0       ← 无 extra field、无逐成员注释
package_manifest 顶层键：created_at, excluded, excluded_files, files, inventory, kind,
                       package_schema, release_id, release_manifest_sha256
has output key: False             ← 构建机输出绝对路径**未**写进归档内清单（写归档在
has package_sha256 key: False        设置 output/package_sha256 之前，已复核）
```

**运行状态**：`runtime_state_absent`（`.log/.sqlite/.sqlite-wal/.sqlite-shm/.pem/.key` 扫
`on_disk` 全集）= `hits: []`；`dotenv_absent` = True；`forbidden_paths_absent count=0`。

**结论**：凭据与运行状态**没有**进包。**但**有 2 项与"工件卫生"相关的问题：

- **P2-3**：`docs/无人值守升级任务清单_2026-09-19.json` 在包内且**豁免扫描**（见 §1.3-3）。
- **P3-4（路径/用户名泄露）**：4 篇包内文档含构建机绝对路径与 Windows 用户名：

  | 文件:行 | 形态 |
  |---|---|
  | `docs/开发规格书_v2.md:21` | `C:\Users\<用户名>\Desktop\币安\multi-agent-trader\` |
  | `docs/项目说明书.md:139` | `cd C:\Users\<用户名>\Desktop\币安\multi-agent-trader` |
  | `docs/多Agent执行分派手册_2026-09-20.md:51` | `C:/Users/<用户名>/Desktop/币安/mat-wt` |
  | `docs/无人值守升级任务清单_2026-09-19.json:3482,3491` | `"worktree_root": "C:/Users/<用户名>/..."` |

  其中 `项目说明书.md` 在**旧**包里也有（`SAFE_DOCS` 白名单），另 3 篇是**新实现扩面后**
  新增的。不是凭据，但属于"部署工件不应携带构建机身份"。**建议**：把这 4 处替换为
  占位路径，或在打包时把 `docs/dispatch/**` 与文档中的绝对路径纳入检查。
  （本报告按 KI04 纪律不写实值，用户名以占位表示。）

---

## 3. 解压自检与链接

**主实验**：`new_head.zip` 解压 → `scripts/create_release_package.py --verify <dir>`（用
**包内自己的**脚本运行，非我的进程导入），**连跑两次**：

| 运行 | exit | ok | problems | file_count |
|---|---|---|---|---|
| run1 | **0** | True | 0 | 214 |
| run2（同目录，查自我污染） | **0** | True | 0 | 214 |

13 项逐项（两次完全一致）：

```
package_manifest_present  True  {"schema": 2, "kind": "runtime"}
release_manifest_present  True
release_identity_matches  True  {"release_id_equal": true, "manifest_sha256_equal": true}
no_unlisted_files         True  {"count": 0}
file_hashes_match         True  {"count": 214}
forbidden_paths_absent    True  {"count": 0}
credentials_absent        True  {"scanned": 214, "repo_exempt_files": ["docs/无人值守升级任务清单_2026-09-19.json"]}
runtime_state_absent      True  {"hits": []}
dotenv_absent             True
required_assets_present   True  {"required": 39}
doc_links_resolve         True  {"checked_docs": 37}      ← 与作者"37 篇"一致
python_syntax_ok          True  {"compiled": 140}          ← 与作者"140 文件"一致
windows_service_config_valid True
```

**"二次 --verify 仍 0"**：**成立**。`sys.dont_write_bytecode = True`（模块导入前，第 33-35 行）
+ 子进程 `PYTHONDONTWRITEBYTECODE=1`（`_subprocess_env`）+ `no_unlisted_files count=0`
共同证明它不在被测目录落 `__pycache__`。

**自造负例（11 类，全部自己设计，不用作者的）** → `_scratch_E/verify/negatives.txt`

| 负例 | 篡改 | exit | 报出的检查 | 判定 |
|---|---|---|---|---|
| tamper_content | 追加一行注释到 `src/core/config_loader.py` | 2 | `file_hashes_match` | AS_EXPECTED |
| extra_unlisted | 新增 `extra_unlisted_artifact.txt` | 2 | `no_unlisted_files` | AS_EXPECTED |
| missing_strategy_card | 删 `knowledge/strategy_cards.json` | 2 | `required_assets_present` + `file_hashes_match` | AS_EXPECTED |
| missing_run_web | 删 `scripts/run_web.py` | 2 | `required_assets_present` + … | AS_EXPECTED |
| broken_link | `docs/文档索引.md` 加一条指向不存在文件的相对链接 | 2 | `doc_links_resolve` → `BROKEN_LINK: docs/文档索引.md -> docs/不存在的目标_zzz.md` | AS_EXPECTED |
| plaintext_credential | 往 `.py` 写入合成 `api_secret = "<32 位十六进制>"`（非真密钥，本报告不写字面量） | 2 | `credentials_absent` → `rule=secret-assignment` | AS_EXPECTED |
| dotenv_present | 写根 `.env` | 2 | `dotenv_absent` + `no_unlisted_files` | AS_EXPECTED |
| state_file | 写 `data/trading.sqlite` | 2 | `runtime_state_absent` + `no_unlisted_files` | AS_EXPECTED |
| forbidden_suffix | 写 `server.key` | 2 | `runtime_state_absent` + `no_unlisted_files`（**未**报 `forbidden_paths_absent`，见 P3-5） | PARTIAL（仍失败，检查项不同） |
| release_identity | 改 `release_manifest.json` 的 `release_id` | 2 | `release_identity_matches`（`release_id_equal:false, manifest_sha256_equal:false`） | AS_EXPECTED |
| syntax_error | `.py` 尾部加 `def broken(:` | 2 | `python_syntax_ok`（`SYNTAX_ERROR: …:259`） | AS_EXPECTED |
| 构建期：未归类 | （见 §1.5 / §5 的 `fake_runtime_entry` / 重命名实验） | 2 或 1 | `unclassified` / `required_assets_present` | AS_EXPECTED |

附加负例（跨清单门禁非空转，§5.3）：把包内 `src/core/release_manifest.py` 的
`SAFE_CONFIG_FILES` 删掉一项 → exit 2，`required_assets_present.cross_check` =
`CONFIG_LIST_DRIFT: 包内 SAFE_CONFIG_FILES=[...] 与打包清单 [...] 不一致` ✓

**P3-5（`forbidden_paths_absent` 覆盖范围窄于其名）**：该检查（第 840-864 行）只遍历
`listed & on_disk` —— **只有出现在清单里的文件**才做路径规则判定。因此"盘上有、清单无"
的禁止路径（我造的 `server.key`）**不会**被它报出，只由 `no_unlisted_files` /
`runtime_state_absent` 兜住。实际不可利用（构建侧对入包文件重跑排除判定并 raise，
已实测），但检查名与语义不符，建议改为遍历 `on_disk`。

---

## 4. 历史标记旧版（DL06）

`docs/文档索引.md` 的版本约定表（现行/旧版/设计稿/流程记录）+ 历史材料免责声明段落——
**存在且结构清晰**。逐项核对：

- 旧版表中以"**旧版：**"开头的**恰好 12 篇** ✓（与作者自述一致）：
  `上线前核查报告_2026-09-02`、`风险事件排查与改善计划_2026-09-03`、
  `项目问题盘点与优化计划_2026-09-07`、`USD-M合约系统稳定性…_2026-09-05`、
  `实盘上线评估与整改计划_2026-09-14`、`上线前最后一轮优化工作计划_2026-09-15`、
  `实盘账户并行接入评估与上线整改清单_2026-09-15`、`实盘接管阻断诊断_2026-09-16`、
  `模拟盘复审与上线优化总方案_2026-09-19`、`决策链路专项评估与优化报告_2026-09-19`、
  `架空机制排查_2026-09-02`、`CHANGES_2026-09-01`；另有 `UI重构方案_整合版` 标 **设计稿**。
- **没有把现行文档标成旧版**：现行表 9 篇（README/项目说明书/运行与故障恢复手册/
  代码维护规范/WorkBuddy 适配说明/上线前人工检查单/当前状态与剩余工作矩阵/测试与验收矩阵/
  `src/strategy_runtime/PROVENANCE.md`）与"旧版"表**无交集**；`技术报告.md` 以
  "主体形成较早，顶部已标明当前补充；历史章节按旧版阅读"的混合口径单列于"架构与专题"，
  可接受（未谎称现行）。
- **漏标（P3-6）**：`docs/开发规格书_v2.md` **完全未出现在索引任何表中**（自带头部
  "状态：历史设计稿"故危害小）；`knowledge/策略报告_整理版.md` 同样未收录。
  索引自称"项目文档入口"，这两篇属漏列。

---

## 5. 不写未实现能力（K06/F13）与新门禁

### 5.1 关于"14 条能做到 X"（**P3-7：无法做 1:1 核对**）

作者所称"14 条逐条指认（13 条落本仓代码）"的**枚举清单不在交付物内**：不在
`docs/运行与故障恢复手册`、`docs/文档索引.md`、`docs/代码维护规范.md`、
`docs/dispatch/patches/U10a/89541df/README.md`，也不在 5 个 U10a 提交信息里
（已逐一读过）。因此我**无法按其编号逐条核对**，`docs/dispatch/` 下也没有 U10a 作者报告
（`docs/dispatch/reviews/U10a/` 不存在）。

改为**按内容核对**：我把手册 §3.1/§3.2 + 索引 + 维护规范 §11.6 的能力声明逐条落到代码上
（21 条，摘要）：

| # | 声明 | 指认 | 判定 |
|---|---|---|---|
| 1 | 打包/自检"唯一实现、不存在第二套口径" | `create_release_package.py` 内 `build_release_package`/`verify_release_package` 共用同一组常量 | ✓ 本仓 |
| 2-5 | `--kind runtime/source`、`--self-check` 不写 dist、`--verify` 只读可重复 | 实测（§1.1 矩阵、§3 两次 verify） | ✓ 本仓 |
| 6-7 | 13 项自检、`doc_links_resolve` 逐篇解析 | §3 实测；`_check_links` 第 692-703 行 | ✓ 本仓 |
| 8-13 | 包内必含 `src/` 全部（含 8 模板、PROVENANCE、`src/data/`）、`config/` 6 个、策略卡+备份、依赖锁、README/CONTRIBUTING/.env.example | §1.6 覆盖率 133/133、6/6、3/3 | ✓ 本仓 |
| 14 | 必需清单与包内 `release_manifest.py` 交叉核对，不一致即失败 | §3 附加负例（`CONFIG_LIST_DRIFT`） | ✓ 本仓，**非空转** |
| 15-18 | 包内必不含 `.env`/`data/`/日志/密钥/缓存/未跟踪诊断脚本；凭据口径唯一（复用扫描器）；不打印匹配值；未归类即失败 | §2、§1.4、§3（`credentials_absent` 只输出 `路径:行号 rule fp`） | ✓ 本仓 |
| 19-21 | `inventory`/`excluded_files` 完整对照；runtime 清单与 `RUNTIME_SCRIPTS` 逐项比对；`[source 包]` 命令在纯 runtime 机器上"立即报文件不存在" | §1.4、§5.2 | ✓ 本仓 |

**"第 14 条是什么"**：若作者的编号与我核对的口径一致，唯一**不落本仓代码**的能力只有
**依赖共享适配补丁的两项**——`run_web.py --verify-package`（补丁 01）与"发布包解释器
fail-closed"`require_in_root/require_package_interpreter`（补丁 02），即手册 §3.2 **第 3 步**
与 §3.1 末尾的"集成状态"注记。手册**已如实标注**这一边界（"本仓已实现…"与
"依赖共享适配补丁…在集成前不在本分支内"分开写，并给出补丁路径与双态冒烟出处），
不构成"写未实现能力"。除此之外我在交付文档里**没有找到任何无法在本仓代码指认的能力声明**。

### 5.2 新门禁是否非空转（实测，双向）

`smoke_u10a_release_package.py` 第 [1b] 节从手册 `**本手册命令的包适用性**` 段落提取
反引号内 `*.py|*.bat`，与 `crp.RUNTIME_SCRIPTS` 的 basename 集合比对。

| 注入 | 结果 |
|---|---|
| 往手册该段塞 `scripts/fake_runtime_entry.py`（不存在的 runtime 脚本） | **exit 1，30 通过 / 1 失败**，`{'手册漏写': [], '手册多写': ['fake_runtime_entry.py']}` |
| 从手册该段删掉 `scan_tracked_secrets.py` | **exit 1，30 通过 / 1 失败**，`{'手册漏写': ['scan_tracked_secrets.py'], '手册多写': []}` |

→ **非空转，且双向**。注意其解析只认反引号形式（第 305 行的正则），且是 **basename** 比对：
若将来把两个不同目录的同名脚本分别列为 runtime / source，该门禁分辨不出（当前无此情形，
登记为 P3-8 观察项）。

### 5.3 其他非空转验证

- 清单交叉核对（`CONFIG_LIST_DRIFT`）：见 §3 附加负例 ✓
- 发布身份绑定（`release_identity_matches`）：见 §3 `release_identity` 负例 ✓

---

## 6. 补丁逐条意见

**前置事实核验**：

```
git diff --stat f5e4790 89541df -- scripts/run_web.py src/core/windows_service.py scripts/run_offline_regression.py
  → 空（逐字节相同）
git diff --stat f5e4790 23bfe88 -- <同三文件>  → 空
```
即作者分支**未改任何共享文件**，补丁同时适用于 `89541df` 与 `f5e4790` ✓

**`apply --check` 矩阵**（在仓库根执行；子目录会静默 no-op，故全部在根执行）：

| 环境 | 01 | 02 | 03 |
|---|---|---|---|
| 仓库默认（`core.autocrlf=false`，仓库级已设） | OK | OK | OK |
| `-c core.autocrlf=false -c core.eol=lf` | OK | OK | OK |
| `-c core.autocrlf=true` | OK | OK | OK |
| **集成 HEAD `f5e4790` 干净内容树**（`trees/base`） | OK | OK | OK |

**逐条**：

- **01 `scripts/run_web.py`**：`verify_release_package()`（第 45 行）复用打包器的
  `verify_release_package`（第 59 行**函数内延迟 import**，无第二套判定），
  `--verify-package` 为 `nargs="?"`（第 122 行），退出码沿用 `report["ok"]` 的 0/2 ✓
  **保留意见**：该分支位于**模块级重依赖导入之后**（`uvicorn`/`langchain`/`pandas`/
  `core.service`/`dashboard.app`，第 12-39 行），因此未装锁文件依赖时**无法**先自检
  → 与 known_limit ①一致，手册 §3.2 已按"先建 venv 再自检"排序 ✓
- **02 `src/core/windows_service.py`**：`is_release_package_root`（第 19-25 行，判据 =
  第一层 `package_manifest.json`）与 `create_release_package.PACKAGE_SCHEMA` 同源，不引入
  新口径；`resolve_python_executable(..., require_in_root: bool = False)` **默认关闭** ✓；
  `load_windows_service_config(..., require_package_interpreter: bool | None = None)`
  默认 `None` → `require_files and is_release_package_root(root)`（第 382-383 行）。
  **"默认关闭 / 不改变既有行为"核实**：
  - 开发检出 / worktree / CI 检出**都没有** `package_manifest.json` → 恒为 `False` →
    `require_in_root=False` → 逐字节等价旧路径。**含 `require_files=True` 的既有调用方**
    （`validate_windows_service_plan.py`）也只在"目录里恰好有 `package_manifest.json`"时
    才改变行为——而那正是"这是一份发布包"的定义，属预期收紧 ✓
  - `require_in_root` 的 fail-closed 分支位于**显式 override / `strict` 早返回之后**
    （`resolve_python_executable` 内：第 125 行声明路径、第 135-140 行 `strict` 早返回、
    第 155 行才是 `require_in_root` 门），即 `MTA_PYTHON`/CLI 注入参数**仍可**指向包外解释器。
    我最初把这当成"与手册'必须位于包内'的措辞张力"，**复核后撤回**：
    手册 §3.2 第 3 步**已明确写出**"确需外置解释器时用 `MTA_PYTHON` 显式声明，或在调用时
    显式传 `require_package_interpreter=False`"（`docs/运行与故障恢复手册_2026-09-06.md:210-217`），
    与实现一致；smoke 第 [5] 节也有对应断言（`显式 require_package_interpreter=False 可覆盖` PASS）。
    → **不是问题**，语义为"要么包内确有解释器，要么部署方显式声明"，与补丁注释一致。
  - `WindowsServiceConfig` 字段未增删改 ✓（diff 未触及 dataclass 定义）
- **03 `scripts/run_offline_regression.py`**：仅在 `SCRIPTS` 追加
  `"scripts/smoke_u10a_release_package.py"`（第 109 行）✓ 与 03 同时落地才自洽
  （只应用 03 会翻转为"清单引用不存在"）——已由 harness 的 `_validate_smoke_manifest`
  的 `stale` 分支兜住 ✓

---

## 7. 全量回归 delta（强制项）

**方法**：`_scratch_E/offline_delta.py`，**非 fail-fast**，逐脚本记 exit code；env 策略
逐条复刻 harness（`EXECUTION_MODE=paper`、清 `MTA_PROFILE/MTA_STATE_ROOT/MTA_DATA_ROOT`、
剥离含 BINANCE/SENSENOVA 且含 KEY/SECRET/TOKEN 的环境变量、`MTA_NO_PROXY=1`、
`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`）；脚本清单直接 `ast` 解析**该树自己的**
`run_offline_regression.py` 的 `SCRIPTS` 元组（不硬编码）。

| 树 | 清单 | 结果 |
|---|---|---|
| `trees/base`（pristine `f5e4790`） | 82 | **FAILURES=0 of 82** |
| `trees/head_patched`（head + 01/02/03） | 83 | **FAILURES=0 of 83** |

**失败集合差集**：

```
NEW failures (head-only): []         GONE (base-only): []
common scripts compared: 82
common scripts with differing exit code: []
scripts only in head: ['scripts/smoke_u10a_release_package.py']   exit=0
```

→ **新增失败 0 项；无任何脚本由绿转红；无脚本消失。唯一差异就是补丁 03 登记的新冒烟。**

**误报规避说明**：`smoke_u02_strategy_position_manager.py` 的输出里含 22 处 `FAIL` 字样
（其自带负向对照），但其 **exit=0**；我的对比只用 exit code，未把该输出当失败
（已按审查提示核对原始日志尾部 `U02 SMOKE OK`）。

**harness 本体复跑**（`trees/head_patched` 内直接跑 `scripts/run_offline_regression.py`）：

```
SMOKE MANIFEST OK included=79 excluded=1
OFFLINE REGRESSION OK scripts=83
EXIT=0
```

→ 与作者自述的 `OFFLINE REGRESSION OK scripts=83` **逐字一致**；`SMOKE MANIFEST OK
included=79 excluded=1` 说明"新增冒烟必须登记"的门禁在补丁 03 落地后恰好平衡
（83 = 79 冒烟 + 4 非冒烟脚本；excluded=1 即 `smoke_p0c.py`，理由=需联网）。
base 树（未打补丁）的清单长度已由 `ast` 独立解析确认为 **82**（未含 `smoke_u10a`）。

`smoke_u10a` 单项在 head_patched 沙箱实测 **31 通过 / 0 失败，exit 0**；在未打补丁的
head 沙箱 **26 通过 / 0 失败，exit 0**（双态探测如作者所述：未接线时打印
`未接线：scripts/run_web.py 尚无 --verify-package` 与
`未接线：core.windows_service 尚无 require_in_root/require_package_interpreter`
且其余断言照常通过）。

---

## 8. 门禁（打过补丁的沙箱）

```
$ python scripts/check_quality.py       → exit 0
All checks passed!
PROFILE LOCK OK sha256=333d6a6b… locked_fields=30 symbols=20 quote=USDT
DEPENDENCY LOCK OK packages=50 sha256=a28db842… python=3.13 platform=win32
SECRET SCAN OK files=520 registered_false_positives=2 skipped_bare_identifiers=10 unparsable_python_files=0
QUALITY CHECK OK files=287 untracked=0

$ python scripts/scan_tracked_secrets.py → exit 0
SECRET SCAN OK files=520 registered_false_positives=2 skipped_bare_identifiers=10 unparsable_python_files=0
```

→ **全绿，`files=287` 与作者自述一致** ✓（`skipped_bare_identifiers=10` 的放宽留痕也在，
`unparsable_python_files=0`，说明"分不出就不放宽"的 fail-closed 未触发。）

---

## 9. known_limits 逐条判定

| # | 作者自述 | 我的判定 | 证据 |
|---|---|---|---|
| ① | `--verify-package` 需已装锁文件依赖（位于模块级重依赖导入之后） | **属实** | `scripts/run_web.py:12-39` 模块级 import `langchain/pandas/uvicorn/core.service/dashboard.app`；分支在 `main()` 内（`:129`） |
| ② | 未加"启动即校验发布包"自动闸门（刻意不做） | **属实** | 全仓 grep `verify_release_package` 仅出现在 `create_release_package.py` 与（补丁后的）`run_web.py` 的显式 CLI 分支；无任何启动路径调用 |
| ③ | `run_web.py` 混合行尾；`autocrlf=true` 重新检出时 02/03 上下文会失败 | **前半属实，后半不可复现** | 实测 `run_web.py` **CRLF=41 / LF=75** 与作者数字**逐字一致**；`windows_service.py` 与 `run_offline_regression.py` 纯 LF（0 CRLF）。但在 `core.autocrlf=true` + `git checkout` 重新落盘（两文件变 398/175 CRLF）后，三补丁 `git apply --check` **仍全部 OK**，且**实际 apply 成功**（`require_in_root` ×6、登记行 ×1 均已落盘）；git 2.53.0.windows.1 会做 CRLF 归一。→ **P3-10：该限制在当代 git 下偏保守**，建议复测后改写（保守方向，非缺陷） |
| ④ | 包内台账 JSON 命中 KI04 范围豁免 | **属实** | `verify` 报告 `credentials_absent.detail.repo_exempt_files = ["docs/无人值守升级任务清单_2026-09-19.json"]`；来源 `scripts/scan_tracked_secrets.py:326 _EXEMPT_FILES`。**补充**：旧打包器不含该文件，是本轮扩面后才进包，故"豁免 + 进包"的组合是新引入的 → 见 P2-3 |
| ⑤ | `src/core/runtime_identity.py` 的 decl-11 note 已过期（`SAFE_DOCS` 已不存在） | **属实** | `src/core/runtime_identity.py:2276` note 写"发布包 `SAFE_DOCS` 白名单缺最新规格…"；该常量**只存在于**旧版打包器（`_scratch_E/old_create_release_package.py:34`），head 版已无。属 U15 文件、非 U10a 独占路径，作者已如实登记为遗留 → P3-11（建议连同 `docs/dispatch/patches/U15/a136cb5/README.md:180` 一并更新 `status`） |
| ⑥ | `U01-R3-FU1` 仅在索引标旧版、正文未改，留 U10b | **属实** | `docs/dispatch/RESUME.md:26` 记 `U01-R3-FU1`＝"4 处文档内嵌断言写死 `current_schema_version()==16`"；`docs/文档索引.md` 对 `实盘账户并行接入评估与上线整改清单_2026-09-15.md` 标"旧版：…内嵌的 schema 版本断言当时为 16，**已过期**（U10b 统一处理）"，正文未改 ✓ |

---

## 10. 问题清单

| ID | 严重度 | 问题 | 位置 | 可复现证据 |
|---|---|---|---|---|
| P2-1 | P2 | `RUNTIME_SCRIPTS` 登记项**未做存在性校验**，且 `scripts/` 兜底一律 source-only；构建侧必需项过滤带 `.exists()`，于是"启动入口被重命名/删除"时 `--out` **静默通过**（包内无任何启动器） | `scripts/create_release_package.py:179`、`:328`、`:640-641` | `trees/rename_test`：`git mv run_web.py run_web_renamed.py` → `BUILD EXIT=0 files=213`（正常 214）。缓解：`--self-check` exit 2、smoke exit 1（5 FAIL）。verifier 侧 `:877` **无** `.exists()` 过滤 → 两侧口径不对称 |
| P2-2 | P2 | 手册 §9.1 的"当前临时运行"**live 启动命令**用 `scripts/start_web_detached.py`，但该脚本是 source-only、**不在 runtime 包内**，且 §9.1 未加手册自定的 `[source 包]` 标记 → runtime 包使用者按手册执行会"文件不存在" | `docs/运行与故障恢复手册_2026-09-06.md:390-403`、`:423`（标记约定见 `:192`）；分类见 `create_release_package.py:223` | `consumer_check`: `scripts/start_web_detached.py` ∉ `new_head.zip`；`grep -n "source 包"` 手册中 §9.1 段无标记 |
| P2-3 | P2 | runtime 包收入 195 KB 台账 `docs/无人值守升级任务清单_2026-09-19.json`，且它命中 KI04 范围豁免 → **包内唯一不被内容扫描的文件**；豁免理由（"流程记录"）在部署工件语境下不成立。索引内部三处口径冲突：`:16` 版本约定写"流程记录…**不进发布包**"、`:87` 标题"仅源码仓库"、`:91` 却列它"**进发布包**" | `scripts/scan_tracked_secrets.py:326`；`docs/文档索引.md:16,87,91`；`create_release_package.py:351-352`（`docs/` 全量入包） | `pkg/zipmeta.txt`（216 成员）；`verify` 报告 `repo_exempt_files`；`grep -n "执行编排\|进发布包\|仅源码仓库\|不进发布包" docs/文档索引.md` |
| P3-4 | P3 | 4 篇包内文档含构建机绝对路径 + Windows 用户名（`开发规格书_v2.md:21`、`项目说明书.md:139`、`多Agent执行分派手册_2026-09-20.md:51`、`无人值守升级任务清单…json:3482/3491`）；后 3 篇是本轮扩面新增 | 见左 | `indep_scan3.txt`（`win-user-path`×2、`posix-user-path`×3） |
| P3-5 | P3 | `forbidden_paths_absent` 只遍历 `listed & on_disk`（清单内文件），"盘上有、清单无"的禁止路径不被它报出（由 `no_unlisted_files`/`runtime_state_absent` 兜住，不可利用） | `create_release_package.py:840-864` | `negatives.txt` 的 `forbidden_suffix`: exit 2 但报的是 `runtime_state_absent` + `no_unlisted_files`，未报 `forbidden_paths_absent` |
| P3-6 | P3 | 文档索引漏列 `docs/开发规格书_v2.md`（自标"历史设计稿"）与 `knowledge/策略报告_整理版.md`；"执行编排（流程记录，**仅源码仓库**）"标题与正文"进发布包"冲突 | `docs/文档索引.md`（无该两篇；`:87` vs `:91`） | 用 `git ls-files` × 索引文本比名，命中上述两篇为"未被提及" |
| P3-7 | P3 | 作者"14 条能做到 X 逐条指认"的**枚举清单不在交付物内**，无法 1:1 核对；交付文档无 U10a 作者报告（`docs/dispatch/reviews/U10a/` 不存在） | — | 5 个 U10a 提交信息 + 手册/索引/维护规范/补丁 README 全文检索；`docs/dispatch/reviews/` 目录列表 |
| P3-8 | P3 | 手册↔`RUNTIME_SCRIPTS` 门禁按 **basename** 比对且只认反引号，同名不同目录无法分辨；`scripts/` 下新脚本自动落 source-only（无"未归类"警示） | `scripts/smoke_u10a_release_package.py:298-309`；`create_release_package.py:323-328` | 门禁双向实测通过（§5.2）；分类逻辑见 `:349-355` |
| P3-9 | P3 | known_limit ③"`autocrlf=true` 重新检出时 02/03 会失败"**不可复现**（git 2.53.0.windows.1 下三补丁 `--check`/实际 apply 均成功）；"混合行尾 41 CRLF/75 LF"事实属实 | 补丁 README；`trees/autocrlf` | `apply --check`×3 OK + 实际 apply OK（`require_in_root`×6） |
| P3-10 | P3 | `runtime_identity.py:2276` 的 decl-11 note 指向已不存在的 `SAFE_DOCS`；`docs/dispatch/patches/U15/a136cb5/README.md:180` 同为过期表述（status 仍 `registered`） | 见左 | `grep -rn SAFE_DOCS` |
| （已撤回） | — | ~~手册 §3.2 第 3 步与补丁 02 的显式覆盖语义张力~~ | — | **复核后撤回**：手册 `:210-217` 已写明 `MTA_PYTHON` / `require_package_interpreter=False` 例外，与实现一致 |

> 自纠：初审稿曾登记一条 "P3-9 手册与补丁 02 语义张力"，复核手册原文后**撤回**
> （手册已写出显式覆盖例外）。最终问题计数：**0×P0 / 0×P1 / 3×P2 / 7×P3**（表格中 P3-4…P3-10 共 7 条；另有 1 条自纠撤回行）。

**无 P0/P1。** 最关键的 3 个：**P2-1（启动入口可静默出包）**、**P2-2（手册 live 启动路径不在 runtime 包内）**、**P2-3（唯一免扫台账进包 + 索引自相矛盾）**。

---

## 11. 未覆盖项（无法离线验证，明说）

1. **真实宿主部署**：ZIP 解压到真实目标机、建 venv、绑端口、`sc.exe create/start`、
   服务日志轮转——本任务权限内不能启动服务，归 U12 OPS。
2. **`--verify-package` 在"未装依赖"的目标机上的行为**：我只能证明它位于重依赖导入之后
   （静态阅读 + known_limit ①），无法在"干净解释器"上实跑（本机 venv 已装依赖）。
3. **`docs/dispatch/reviews/**` 与 `docs/无人值守升级任务清单…json` 的**内容是否真的无凭据**：
   前者被范围豁免（KI04），我**按纪律未逐篇读**；后者我做了形态扫描（无凭据形状命中）
   但未逐行人工阅读全文（195 KB）。
4. **G 线/其它 worktree 的实际分支状态**与补丁最终合入形态（作者 branch 未改共享文件，
   我已核；合入动作由 I 执行）。
5. **U08 未集成**：本 head 的 base 不含 U08，故"集成 U08 后 delta 是否仍为 ∅"未验（属 U12）。
6. 旧版（`f5e4790`）打包器**是否曾被真实发布**过：未查 `dist/`（工作区内无 `dist/`）。

---

## 11b. 原始证据文件索引（均在 `_scratch_E/` 内）

> **清洁说明（供后续会话**：初稿刻意保留了全部沙箱树/压缩包（326 MB），但它们是
> `docs/dispatch/reviews/**` 等**刻意含凭据形状**文件的副本，落在未跟踪且**未被
> `.gitignore` 忽略**的路径下，会让本工作区的 `scan_tracked_secrets.py` 门禁凭空报红。
> 因此收尾时**删除了 `trees/`、`*.tar`、`pkg/*.zip`、`verify/pkg|neg|drift`**
> （326 MB → 1.5 MB），仅保留**全部原始 stdout/报告/探针源码**；并用仓库门禁自己的
> `scan_tracked_secrets.scannable_files()` 复扫 `_scratch_E` 的 217 个可见文件 →
> **TOTAL HITS: 0**。需要重建沙箱时按 §0 的两条命令（`git archive` → `git init` +
> `git add -A`）+ §11b 的探针脚本即可，约 2 分钟。
> 同理，本报告与派生证据文件**不写会被密钥扫描器匹配的字面量**（`pkg/hit_lines_masked.txt`
> 为掩码版，命中右值一律替换为 `<REDACTED-BY-E-REVIEW>`，构建机用户名替换为 `<USER>`）。


| 文件 | 内容 |
|---|---|
| `trees/base`、`trees/head`、`trees/head_patched`、`trees/autocrlf`、`trees/gate_test`、`trees/gate_test2`、`trees/rename_test` | 各沙箱树（含已跟踪清单 `*_tracked.txt`、保真度比对用 `repo_head.txt`/`repo_base.txt`） |
| `pkg/old_base.zip`、`old_head.zip`、`new_base.zip`、`new_head.zip`、`new_head_source.zip`、`oldz_head.zip`、`rename_test.zip` | 各组合的发布包 |
| `pkg/old_base.log`、`old_head.log`、`new_base.log`、`new_head.log`、`new_head_source.log`、`oldz_head.log` | 构建 stdout（含 `inventory` 全文） |
| `pkg/diff_old_vs_new_head.txt` | **两处漏包修复的差集清单**（69 项逐条） |
| `pkg/diff_old_vs_newsource.txt` | 反向检查：source 包为旧包严格超集（`only_in_left=0`） |
| `pkg/diff_old_vs_oldz.txt` | claim ② 反事实：补 `-z` 对旧实现输出 **0 差异** |
| `pkg/new_noz.log` | claim ② 反事实：新实现去掉 `-z` → 33 路径丢失 → 构建**硬失败** |
| `pkg/indep_scan2.txt`、`indep_scan3.txt`、`hit_lines.txt` | 独立凭据/路径扫描与命中行原文（判定"类型注解"） |
| `pkg/zipmeta.txt` | zip 元数据（注释/extra field/成员名/内嵌清单键） |
| `pkg/consumer_check.txt` | 启动路径齐备性 + `src/config/knowledge` 覆盖率 |
| `pkg/check_quality.out`、`scan_secrets.out`、`smoke_patched.out`、`smoke_unpatched.out` | 门禁与冒烟原始输出 |
| `pkg/gate_fake_entry.out`、`gate_missing_entry.out` | 新门禁双向非空转证据 |
| `pkg/rename_selfcheck.out`、`rename_smoke.out` | P2-1 的缓解证据（`--self-check` exit 2 / smoke 5 FAIL） |
| `verify/run1.out`、`run2.out`、`negatives.txt`、`drift.out` | 两次 `--verify` 与 11 类自造负例、跨清单漂移负例 |
| `delta/base.json`、`delta/head_patched.json`、`delta/*_logs/`、`delta/*.run.log` | **全量回归 delta**（逐脚本 exit code + 原始日志） |
| `delta/harness_head_patched.out` | harness 本体复跑输出 |
| `old_create_release_package.py`、`old_zfixed.py`、`run_packager.py`、`run_noz.py`、`negatives.py`、`indep_scan.py`、`zipdiff.py`、`zipmeta.py`、`consumer_check.py`、`offline_delta.py`、`make_manifest.py`、`show_lines.py` | 全部探针源码（可重跑） |

## 12. 总体评价（E 视角）

- 本任务的**核心承诺"不漏包"经独立复现成立且修复完整**：差集 69 项逐条可解释，
  `src/` 133/133 全覆盖，source 包是旧包的**严格超集**（`only_in_old = 0`，即
  "旧包里的东西一样也没丢"，新包在此基础上补齐）。这一点是我最看重的可恢复性保证，
  它成立。
- **自检的可复核性很强**：13 项检查、两次运行稳定、11 类自造负例全部报红、
  且**报告本身可审计**（逐文件排除理由、`repo_exempt_files` 明示豁免、匹配值不打印）。
- **文档诚实度高**：手册把"本仓已实现"与"依赖共享补丁"分开写，未把未合入能力写成已实现。
- 遗留问题集中在**"包边界"与"文档↔工件一致性"**：一个可静默丢启动入口的构建路径（P2-1）、
  一处手册 live 启动路径不在 runtime 包内（P2-2）、一处唯一免扫台账进包 + 索引自相矛盾（P2-3）。
  三者都不影响"已打的这个包是正确的"，但都会在**下一次**改脚本/换运维按手册操作时失效——
  正是 U10a 想要防的那类问题。

**结论：有条件 GO。** 条件（建议在 U10b 或本轮 P2 修复轮内闭合，不必阻塞合入）：

1. 构建侧去掉 `.exists()` 过滤（或在 `--out` 路径补"必需项必须存在"断言），使 P2-1 的
   重命名/删除场景在**构建**阶段即报错（与 verifier 侧 `:877` 口径对齐）。
2. 手册 §9.1 的 `start_web_detached.py` 命令补 `[source 包]` 标记（或把该脚本并入
   `RUNTIME_SCRIPTS`），使"只有 runtime 包"的机器不会按手册执行到不存在的入口（P2-2）。
3. 决定是否让 `docs/无人值守升级任务清单_2026-09-19.json`（及 `docs/dispatch/` 编排材料）
   继续进 runtime 包：若继续，应把它从 KI04 范围豁免中移出（或在打包器里对该文件单独
   内容扫描）；同时修正 `docs/文档索引.md:87` 的"仅源码仓库"标题（P2-3）。
4. 把 4 处绝对路径/用户名占位化（P3-4）。
5. 修正 claim ②、known_limit ③ 的表述，补列 `开发规格书_v2.md`、更新 decl-11 note，
   并在交付物内附上"14 条"清单以便后续 1:1 复核（P3-6/7/10/11）。
