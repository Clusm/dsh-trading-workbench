# Q / U10a r2 独立复验报告（R3, r1）

- 复验对象：`10bc4d2`（分支 `task/U10a`），r2 delta = `ff98566 → 10bc4d2`（4 个提交）
- 上一轮基线：`23bfe88`（E_r1 判 **有条件 GO**，0×P0 / 0×P1 / 3×P2 / 7×P3）
- 复验者：Q 角色，独立会话（非作者会话）。工作区 `C:\Users\Tao\Desktop\币安\mat-wt\reviews\U10a\R3\r1`
  （detached，`git rev-parse HEAD` = `10bc4d2676da4c9e8aaa3e2d76188e66895975da`）
- 解释器 `C:\Users\Tao\Desktop\币安\multi-agent-trader\.venv\Scripts\python.exe`，`PYTHONUTF8=1`；无 pytest
- 探针与原始输出：`_scratch_Q/`（`pkg/`、`p21_out/`、`p22_out/`、`p22b_out/`、`p23_out/`、
  `p35_out/`、`delta/`、`ev_*.txt`；沙箱树 `trees/` 已在收尾时按证据卫生删除，见文末清洁说明）
- 未联网；未读 `.env`/`data/`/密钥/账户标识；**未启动服务**；未执行 `sc.exe`；未写任何其它 worktree
  （所有产物只落在本工作区内；`mat-wt/I` 与 `mat-wt/EF` 零写入）

> 沙箱保真度（所有结论的前提）：`git archive <rev>` → `git init` + `git add -A`（`core.autocrlf=false`）
> → 与 `git ls-tree -r <rev>` **逐路径 IDENTICAL**（`ev_fidelity.txt`：两侧 diff 均 **0 行差异**；
> pristine 558 / head 564 已跟踪文件）。`head_patched` = head + 补丁 01/02/03/04（`git apply -p1` 全部 OK）。

## 结论

**PASS-with-notes**

三条 P2 均**独立复现为真闭合**（各自带反向"误杀"检查，见下）；claim ② 的更正到位且
无残留矛盾；P3 抽验 6 项中 5 项闭合、1 项（P3-10）**未处理也未在交付物内登记**；
全量 delta = ∅；门禁全绿。新增 **5 条**（N1–N5，全部 P3 级，本报告逐条登记）；
其中 **N4（部署流时序）/ N5（P3-10）为 r1 遗留、非本轮引入**。
**未发现任何未登记的 P2 fail-open、崩溃、静默出包或误导性文档。**

| 复验项 | 判定 |
|---|---|
| 1. P2-1 出包路径硬校验 | **闭合**（9/9 变异硬失败并点名缺失项；正常仓库不误杀） |
| 2. P2-2 手册 source-only 标注 + 包内启动方式 | **闭合**（自建解析器 0 违规；注入负例必报；正例不误报；实际包成员与标注一一对应） |
| 3. P2-3 台账口径 + 豁免如实列出断言 | **闭合**（断言经变异测试非空转）；附带 1 条 **P3-new(a)** 理由陈述不准 |
| 4. claim ② 三处更正 | **到位**（2 处载体都已更正；README **从未**承载该表述 → 无矛盾可留） |
| 5. P3 抽验 ≥5 | P3-5/6/7/8/9 **闭合**；**P3-10 未处理、未登记** |
| 6. 补丁与全量 delta | 补丁 4×3 配置 + pristine 干净树 `--check` 全 OK；**NEW=[] GONE=[]** |
| 7. 门禁 | **全绿**：`files=287`、`SECRET SCAN files=521` |
| 8. 反向检查 | P2-1 不误杀正常仓库；`on_disk` 判定不越界；`[1c]` 不误报合法写法；3 条窄口径残留已登记 |

---

## 1. P2-1（出包路径静默）—— 独立探针，9 类变异 + 5 类反向

探针：`_scratch_Q/probe_p21.py`、`probe_p21b.py`（自建；走仓库真实 CLI
`create_release_package.py --out`，每例在 `head_patched` 的独立拷贝里做变异 —
`git mv`/`rename`/`unlink`/新写入后 `git add -A` 再出清单再构建）。
原始输出：`p21_console.txt`、`p21b_console.txt`、`p21_out/p21.json`、`p21b_out`。

### 1.1 变异矩阵（原始输出逐行）

| 场景 | 动作 | exit | 出包 | 成员 | 错误信息（点名缺什么） |
|---|---|---|---|---|---|
| S0 | 无变异（真实仓库基线） | **0** | 是 | **216** | —（`run_web.py` 在包内） |
| S1 | `run_web.py` → `run_web_renamed.py` | **2** | 否 | — | `存在未归类文件（禁止漏包，也未排除）：scripts/run_web_renamed.py` |
| S2 | `run_web.py` → `smoke_run_web.py`（已登记前缀） | **2** | 否 | — | `运行必需资产在仓库中不存在（登记项与实际仓库脱节，禁止出包）: ['scripts/run_web.py']` |
| S3 | 既有冒烟改名成未知名 `renamed_smoke_zz.py` | **2** | 否 | — | `存在未归类文件…：scripts/renamed_smoke_zz.py` |
| S4 | 删除 `scripts/health_watch.py` | **2** | 否 | — | `运行必需资产在仓库中不存在…: ['scripts/health_watch.py']` |
| S5 | **新增**未归类文件 `scripts/newthing_zz.py` | **2** | 否 | — | `存在未归类文件…：scripts/newthing_zz.py` |
| S6 | 把 `health_watch.py` 挪进排除目录 `docs/dispatch/` | **2** | 否 | — | `运行必需资产在仓库中不存在…: ['scripts/health_watch.py']` |
| S7 | 把 `knowledge/strategy_cards.json` 挪到 `tests/`（source-only） | **2** | 否 | — | `运行必需资产在仓库中不存在…: ['knowledge/strategy_cards.json']` |
| S8 | 把 `src/data/cache.py` 挪进 `docs/dispatch/` | 0 | 是 | 216 | 文件仍在包内（`docs/**` 也是 runtime），**未丢** |
| S9 | 把 `requirements-lock.txt` 挪进 `docs/dispatch/` | **2** | 否 | — | `运行必需资产在仓库中不存在…: ['requirements-lock.txt']` |

→ 任务要求的 5 类（改名 runtime 入口 / 改名冒烟 / 删除 runtime 资产 / 新增未归类文件 /
把应有文件挪到包外）**全部硬失败且错误信息点名具体路径**。

### 1.2 反向：不误杀

| 场景 | 期望 | 实测 |
|---|---|---|
| S0 正常仓库 | exit 0、成员数正确 | **exit 0、216 成员（file_count 214 + 2 元数据）**，与 r1 基线一致 |
| S11 真实源码树但**去掉 `.git`** | 不应误杀 | **exit 0、216 成员**（`src/core/release_manifest.py` 在 → 仍属真实仓库语义，必需项齐备） |
| S12 已跟踪文件内容被改动 | 不应误杀 | exit 0、216 成员（构建不校验清单哈希，符合设计） |
| S8b 非必需 src 模块挪进**确被排除**的前缀 | 见 §新发现 N3 | exit 0、215 成员（静默少 1，已登记） |

### 1.3 残留：合成夹具的宽松分支（**P3-new(b)**，非 fail-open 于真实仓库）

`build_release_package` 的 `real_repo = inventory["source"]=="git-index" or (root/src/core/release_manifest.py).is_file()`。
两者皆否时沿用旧的"存在才要求"口径。实测：

```
S10b 无 git + 无 release_manifest.py + 改名 run_web.py → run_web_renamed.py
      exit=2（仍被"未归类"闸门抓住）
S10c 无 git + **有** release_manifest.py + 同样改名 → exit=2（真实仓库分支，严格）
S10d 无 git + 无 release_manifest.py + 改名成 **smoke_** 前缀形态 → exit=0（静默，214 成员）
S10e 无 git + 无 release_manifest.py + 直接删除 run_web.py → exit=0（静默）
```

**判定**：该宽松分支**是必要的**——既有冒烟 `scripts/smoke_release_package.py`
自建 partial tree（`scripts/smoke_release_package.py:32-51`：只有 `src/app.py`、
`scripts/run_web.py`、`config/` 两项、`README.md`，**无 git、无
`src/core/release_manifest.py`**），旧口径正是它依赖的行为；收紧会打断它。
真实仓库/真实发布包必然满足两个判据之一（有 `.git`，或有包内 `release_manifest.py`），
故**不可由正常流程触发**。⇐ 定 P3，非 P2。

---

## 2. P2-2（手册写了包内不可用的启动方式）—— 独立解析器 + 实际包成员

探针：`_scratch_Q/probe_p22.py`（**自建**行式 fence 扫描器，非作者正则）+
`p22_membership.txt`（与实际构建出的包逐成员比对）。原始输出 `p22_console.txt`。

### 2.1 `[1c]` 门禁非空转（自建，不采信作者对照）

```
manual=运行与故障恢复手册_2026-09-06.md runtime_basenames=17 fences_with_script_refs=20
VIOLATIONS=0
INJECTED unmarked source-only -> violations=1     （期望 >=1）
INJECTED marked   source-only -> violations=0     （期望 0）
```

- 8 个引用 source-only 脚本的命令块**全部已标** `[source 包]`（行 94 / 239 / 304 / 373 /
  410 / 429 / 548 / 603）；
- 12 个只引用 runtime 脚本的命令块**未标**（正确，不应要求标注）；
- 注入未标注的 source-only 命令 → **报出**；注入**已标注**的同类命令 → **不报**
  （合法写法无误报）。

### 2.2 逐条核对"标了的确实不该在包内、没标的确实能跑"

对**实际构建出的包**（`pkg/q_runtime.zip` 216 成员 / `q_source.zip` 378 成员）逐 basename 比对：

- 未标注块引用的 12 个脚本（`validate_windows_service_plan.py`、`windows_service_host.py`、
  `verify_profile_lock.py`、`report_release_readiness.py`、`create_release_package.py`、
  `create_recovery_bundle.py`、`restore_recovery_bundle.py`、`run_web.py`、`health_watch.py`）
  **全部在 runtime 包内**；
- 已标注块引用的 scripts 全部 **runtime=False / source=True**（`benchmark_demo_endpoints.py`、
  `smoke_order_outbox/lifecycle/plan.py`、`smoke_db_backup.py`、`smoke_recovery_bundle.py`、
  `smoke_strategy_evaluation.py`、`smoke_usdm_replay.py`、`start_web_detached.py`、
  `smoke_profile_isolation.py`、`smoke_rate_limit_multiprocess.py`、`smoke_fault_matrix.py`、
  `smoke_health_watch.py`、`smoke_release_observation_freshness.py`）。

### 2.3 新增的"包内可用的启动方式"在 runtime 包内**真的可执行**

```
$ python scripts/run_web.py --help            （cwd = 沙箱仓库根）
usage: run_web.py [-h] [--host HOST] [--port PORT] [--profile {demo,live}]
                  [--interval-minutes N] [--no-autostart] [--state-root DIR]
                  [--verify-package [DIR]]
RC=0
```

- `--profile {demo,live}` 是**真实 CLI 形参**（`run_web.py:114`），映射经
  `load_settings(PROJECT_ROOT, profile=...)`（`:76`）→ 与手册 §9.1 的
  demo→8000 / live→8001 表一致；手册新增的 `run_web.py --profile live|demo`
  **不是空头命令**。
- `run_web.py` 在 runtime 包内（`RUNTIME_SCRIPTS` 第 1 项 + 实际成员核对）；
  包内**没有** `start_web_detached.py` ✓（手册 §9.1 的 ⚠️ 提示属实）。
- 手册新增的代理提示（"包内启动器不会自动注入代理，需部署方显式配置"）与实现一致：
  `run_web.py:32 _applied_proxy = apply_proxy_env()`（**应用**已配置的代理，**不**自动注入），
  而 `start_web_detached.py:67-82 build_env()` 才做 `MTA_NO_PROXY=0` + 注入 `HTTP(S)_PROXY`。
- 冒烟 `[1c]` 门禁在**已打补丁**沙箱中与其余断言一并通过（见 §6 的 harness 输出）。

> **未覆盖（明说）**：不启动真实服务，故"`run_web.py --profile live` 是否真能绑到 8001
> 并进入 live 账本"未离线验证（归 U12）。

---

## 3. P2-3（台账进包 + 三处口径冲突）—— 变异测试

探针：`_scratch_Q/probe_p23.py`。原始输出 `p23_console.txt`、`p23_out/`。

### 3.1 现状成立

```
ledger_in_runtime_package = True
verify 报告 credentials_absent.detail.repo_exempt_files = ['docs/无人值守升级任务清单_2026-09-19.json']
verify 报告 ok=True（13 项全绿）
```

### 3.2 断言**非空转**（自建变异，非作者对照）

| 变异 | 动作 | 期望 | 实测 |
|---|---|---|---|
| A | 把自检报告里的 `repo_exempt_files` 改成 `[]`（模拟"免扫被静默吞掉"的回归） | 冒烟变红 | **smoke exit=1**，`FAIL 包内 KI04 范围豁免成员在自检报告中被如实列出（不是静默跳过） {'smoke_exempted': ['…json'], 'report': []}`（`p23_out/mutA_smoke.out`） |
| B | 把台账写进 `EXCLUDED_PATH_PREFIXES`（**移出包**） | 应报错 | `--out` exit=0（构建不查链接），**`--verify` exit=2**，`doc_links_resolve` 报 **5 条 BROKEN_LINK**（`决策链路…报告`、`多Agent执行分派手册`、`文档索引`、`无人值守交付与多Agent执行规格`、`模拟盘复审…总方案`） → 手册"排除它会让 `doc_links_resolve` 报断链"的**主理由属实** |
| C | 往解压目录加一个**未列出的豁免成员**（`docs/dispatch/reviews/fake_q.md`） | 应报错 | **exit=2**，`no_unlisted_files` + `forbidden_paths_absent` 双报（`credentials_absent` 仍如实列出台账）→ 无 fail-open |

### 3.3 三处口径是否真统一

| 处 | 现文本要点 | 判定 |
|---|---|---|
| `docs/文档索引.md:16` 版本约定表 | "流程记录…**不作为能力声明**；是否随包**只以** `EXCLUDED_PATH_PREFIXES` 为准" | ✓ 不再自相矛盾 |
| `docs/文档索引.md` 执行编排节 | 标题由"（流程记录，**仅源码仓库**）"改为"（流程记录，非交付物）"；分"不进任何发布包 / 随 `docs/` 进代码包（仅因链接可达）"两组，台账列在后者并注明"命中 KI04 范围豁免，自检报告会**显式列出**" | ✓ |
| `docs/运行与故障恢复手册_2026-09-06.md` §3.1 | 新增"**范围豁免随包时必须如实列出（不得静默跳过）**"引用块，理由与出处同索引 | ✓ |
| `scripts/create_release_package.py:126-135` | `EXCLUDED_PATH_PREFIXES` 前的注释写明台账**不在此列**、进包原因、免扫如实列出、冒烟有断言 | ✓ |

三处对"是否随包"与"免扫必须可审计"的口径**一致，无残留冲突**；且"随包只以排除清单为准"
在本节涉及的文件上准确（`reviews/`、`patches/`、`RESUME.md`、`w0_*`、`交接说明_*`
确实全在 `EXCLUDED_PATH_PREFIXES` 内）。

**附带（P3-new(a)）**：该理由块里的一个**子陈述不实**——见 §新发现 N1。

---

## 4. claim ② 的更正（"两处真实漏包"→ 一处）

作者自认：旧实现补 `-z` 前后成员集合 0 差异；`-z` 是**新设计**下的必需项（缺了 → 未归类 →
构建硬失败），不是旧实现漏中文名文档的根因。

| 载体 | 是否承载过该表述 | 现文本 | 判定 |
|---|---|---|---|
| `scripts/create_release_package.py` `_tracked_files` docstring（`:270-280`） | 是 | "…按行取值会得到转义后的伪路径，命中不了任何分类规则，于是构建以'存在未归类文件'**硬失败**"+"**更正（U10a r2，独立复核反证）**：旧打包器丢中文名文档的根因是 `SAFE_DOCS` 白名单只有 5 篇，与 `-z` 无关——给旧实现补 `-z` 前后成员集合为 0 差异" | **已更正** |
| `docs/代码维护规范.md §11.2`（`:147-151`） | 是 | "**表述更正（U10a r2，独立复核反证）**：同一次核对曾把…也列为成因，**这是错的**。反事实实验：…**0 差异**…真正根因是 `SAFE_DOCS` 白名单只有 5 篇" | **已更正** |
| `docs/dispatch/patches/U10a/1eb1b1e/README.md` | **否** | 全文无 `-z` / "漏包" / "中文名" / "两处" 字样 | **无该表述可改** |

**独立复核"无残留矛盾"**：全仓 grep（排除 `docs/dispatch/reviews/**`）"两处 / 静默漏 /
漏掉中文 / 中文名文档"→ 仅命中的是 §11.2 与 docstring 的**更正后**文本（外加若干无关
命中）。r1 的 `89541df/README.md` 同样从未承载该表述（`git show 23bfe88:… | grep` 为空），
且其 01/02/03 三个 diff 与 r2 目录**逐字节相同**（`git diff --stat 23bfe88 10bc4d2` 对
该三文件为 0 行）。

→ "不得只改一处留矛盾" **成立**：承载该表述的 2 处都已改，第 3 处（README）本来就没有。

---

## 5. P3 抽验（6 项）

### P3-5 `forbidden_paths_absent` 改判 `on_disk` 全集 —— **闭合**（含负例）

探针 `probe_p35.py`，原始输出 `p35_console.txt`：

| 场景 | forbidden_paths_absent | no_unlisted_files |
|---|---|---|
| 干净解压目录 | `ok=True count=0`（**无误报**） | `count=0` |
| 手工放入清单外的 `server.key` | **`ok=False hits=['server.key(私钥)']`**（r1 的 P3-5 缺口已补） | `unlisted=['server.key']` |
| 手工放入清单外的普通文件 `notes.txt` | `ok=True count=0`（**不越界**，路径规则不外溢） | `unlisted=['notes.txt']` |

**附带观察（N4）**：在包目录内按手册 §3.2 建好 `.venv` 与 `.env` 后再自检 →
`forbidden_paths_absent` 与 `no_unlisted_files` **同时**报出 `.env / .venv/**`。
`no_unlisted_files` 这一侧是 **r1 既有行为**（非本轮引入）；本轮把 `on_disk` 全集引入
`forbidden_paths_absent` 后，该项也会一并变红。见 N4。

### P3-6 索引补两篇 + 标题冲突 —— **闭合**

- `docs/文档索引.md:57` 新增 `[开发规格书 v2](开发规格书_v2.md)`（标 **设计稿**）；
  `:58` 新增 `[策略报告（整理版）](../knowledge/策略报告_整理版.md)`；
- `:87` 标题由"执行编排（流程记录，**仅源码仓库**）"改为"**执行编排（流程记录，非交付物）**"；
- 反向：`旧版` 表仍为 12 条，与"现行"表无交集，**未把现行文档标成旧版**（逐条比对
  `docs/文档索引.md:75-86` 与现行表）。

### P3-7 附录 A 的 14 条清单**可逐条核对** —— **闭合**

`docs/运行与故障恢复手册_2026-09-06.md` 末尾新增「附录 A：发布包能力指认清单（U10a）」，
C1–C14 + C15/C16（补丁依赖，单列并标边界）。我**逐个符号**在树内核对（`p37_symbols.txt`）：

```
C1  build_release_package / verify_release_package / _excluded_reason /
    required_runtime_assets / _scan_secrets                     → 全部 crp ✓
C2  KINDS / packaged_files                                      → crp ✓
C3  _classify / discover_inventory / unclassified                → crp(+smoke) ✓
C4  RUNTIME_ASSET_ROOTS / misfiled                              → crp ✓
C5  required_runtime_assets / _CROSS_CHECK_PROGRAM /
    CONFIG_LIST_DRIFT / CARD_LIST_DRIFT / DEPENDENCY_LIST_DRIFT  → crp ✓
C6  REQUIRED_TOP_LEVEL / _CONFIGS / _KNOWLEDGE / _DEPENDENCY_FILES /
    _RUNTIME_SOURCES                                            → crp ✓（templates=8 ✓）
C7  RUNTIME_SCRIPTS（17 项 ✓）/ absent 分支                      → crp(+smoke) ✓
C8  _source_only_script_reason 返回 None → _classify unclassified → crp ✓
C9  _secret_allowances / _repo_scan_exemptions / _scan_secrets /
    _REGISTERED_FALSE_POSITIVES / _EXEMPT_FILES / _EXEMPT_PREFIXES → crp(+scanner) ✓
C10 EXCLUDED_PATH_PREFIXES / runtime_state_absent / dotenv_absent → crp ✓
C11 self_check / dont_write_bytecode / _subprocess_env /
    PYTHONDONTWRITEBYTECODE                                     → crp ✓
C12 record( 调用 → 实跑报告恰为 **13 项检查**（逐项名核对）        → crp ✓
C13 release_identity_matches                                    → crp ✓
C14 _check_links / _normalize_link + smoke [1b]/[1c]             → crp(+smoke) ✓
```

→ r1 的"无法 1:1 核对"已补救：14 条**全部可指认**，C15/C16 已明确标为补丁依赖
（并已在 §3.1 集成状态注记中单列边界），不构成"写未实现能力"。

### P3-8 门禁按 basename 的已知边界 —— 登记文字**准确**

手册 §3.1 末："已知边界：该比对按 **basename** 进行，且只识别反引号/裸 `scripts\...` 形式；
若将来把两个不同目录的同名脚本分别列为 runtime/source，它分辨不出（当前无此情形）"。
对照实现：`[1b]` 用 `re.findall(r"`([A-Za-z0-9_./]+\.(?:py|bat))`", paragraph)`（只认反引号、
字符类不含反斜杠）、`[1c]` 用 `scripts[\\/][A-Za-z0-9_./\\]+\.(?:py|bat)`（反引号或裸路径），
两者都按 `Path(name).name` 比对 → **描述与实现一致**。

### P3-9 known_limit ③（autocrlf）更正 —— **准确**

补丁 README「行尾说明（已复测，更正 r1 的保守表述）」：明确写 `run_web.py` 混合行尾
（41 CRLF / 75 LF）、补丁 01 只在 LF 段落加行、补丁 04 上下文含 CRLF 需按字节写出，
并声明 r1 的"`autocrlf=true` 下 02/03 会失败"在 git 2.53.0.windows.1 上**不可复现**且
"保留此更正而非删除"。与我在 §6 实测的三种配置全 OK 一致。

### P3-10 `runtime_identity.py` decl-11 note / U15 README —— **未处理，且交付物内无登记**

```
src/core/runtime_identity.py:2276  "decl-11": {"status":"registered","note":"发布包 SAFE_DOCS
                                     白名单缺最新规格、tracked 诊断脚本入包…"}
docs/dispatch/patches/U15/a136cb5/README.md:180  同一条，status 仍 registered
```

- `git diff --stat 23bfe88 10bc4d2` **不含** `src/core/runtime_identity.py` 与 U15 README →
  本轮未改；
- r2 的 4 个提交信息只提到 P3-5/6/7/8/9，**未提到 P3-10**；
- 全树 grep（`SAFE_DOCS`）只命中上述两处 + 我报告引用的更正文本。
- 该 note 现在**实质过期**：新打包器已无 `SAFE_DOCS`，最新规格/决策链路报告**已进包**
  （`docs/` 全量入 runtime），且 `diag_`/`probe_` 脚本**已不进 runtime 包**（prefix → source-only）。

→ **P3-10 既未修复也未在交付物内登记**（作者可能只在会话内的 r2 报告中登记，该报告
不在仓库内，我无法核对）。属 U15/I 独占路径的遗留，不阻塞 U10a，但**必须登记**（见 N5）。

---

## 6. 补丁与全量 delta

### 6.1 `git apply --check` 矩阵（仓库根执行）

在 `trees/head`（= `10bc4d2` 内容树，含未打补丁的原始文件）与 `trees/pristine`
（= `f5e4790` 干净内容树）上，各 `-p1`：

| 环境 | 01 | 02 | 03 | 04 |
|---|---|---|---|---|
| 仓库默认（`core.autocrlf=false`） | OK | OK | OK | OK |
| `-c core.autocrlf=false -c core.eol=lf` | OK | OK | OK | OK |
| `-c core.autocrlf=true` | OK | OK | OK | OK |
| pristine `f5e4790` 干净内容树 | OK | OK | OK | OK |

原始输出：`ev_applycheck.txt`（三段 head 树配置 + 一段 pristine 干净树，共 16 行全 `OK`）。

**base 等价性独立复核**：`git diff --stat f5e4790 8a96609 -- <补丁触及的 6 个共享文件>`
= **空**（`scripts/run_web.py`、`src/core/windows_service.py`、
`scripts/run_offline_regression.py`、`docs/开发规格书_v2.md`、`docs/项目说明书.md`、
`docs/多Agent执行分派手册_2026-09-20.md`）→ 补丁对 `f5e4790` 与 `8a96609` **同效**，
README 的该表述属实。

补丁 01/02/03 与 r1 目录**逐字节相同**（`git diff --stat 23bfe88 10bc4d2` 该三文件 0 行）。

### 6.2 全量离线回归 delta（**非 fail-fast**，自建 harness）

探针：`_scratch_Q/offline_delta_q.py`。逐脚本记 exit code；env 逐条复刻
`run_offline_regression.main()`（`EXECUTION_MODE=paper`、清 `MTA_PROFILE/MTA_STATE_ROOT/
MTA_DATA_ROOT`、剥离 (BINANCE|SENSENOVA)×(KEY|SECRET|TOKEN)、`MTA_NO_PROXY=1`、
`PYTHONUTF8=1`、`PYTHONIOENCODING=utf-8`）；脚本清单由 `ast` 解析**该树自己的**
`run_offline_regression.py` 的 `SCRIPTS`（不硬编码）。

| 树 | 清单 | 结果 |
|---|---|---|
| `trees/pristine`（`f5e4790`，未含 U10a） | **82** | **FAILURES = 0 of 82** |
| `trees/head_patched`（`10bc4d2` + 补丁 01–04） | **83** | **FAILURES = 0 of 83** |

```
NEW failures (head-only): []
GONE (base-only): []
common scripts compared: 82
common scripts with differing exit code: {}
scripts only in head: [('scripts/smoke_u10a_release_package.py', 0)]
```

原始输出：`delta/summary.txt`、`delta/*.run.log`、`delta/*_logs/`。

**误报规避**：`smoke_u02_strategy_position_manager.py` 的日志含 **22 处 `FAIL`** 字样
（其自带负向对照），但 **exit=0**；我的对比只用 exit code，未把输出当失败。

**harness 本体复跑**（`trees/head_patched` 内直接跑 `scripts/run_offline_regression.py`）：

```
SMOKE MANIFEST OK included=79 excluded=1
OFFLINE REGRESSION OK scripts=83
EXIT=0
```

（原始输出 `delta/harness_head_patched.out`）→ 与作者自述逐字一致；`included=79 + excluded=1`
说明"新增冒烟必须登记"在补丁 03 落地后恰好平衡。

**双态**：`trees/head`（**未打补丁**）跑冒烟 → **32 通过 / 0 失败，exit 0**，
并如实打印 `未接线：scripts/run_web.py 尚无 --verify-package`、
`未接线：core.windows_service 尚无 require_in_root/require_package_interpreter`
（`pkg_q_smoke_unpatched.out`）；`trees/head_patched`（**已打补丁**）→
**37 通过 / 0 失败**，其中 `[1c]` 两项 `PASS`（门禁本体 + 非空转自检）、
`[4]` 新增的 `新增未登记的 scripts/ 入口 → 构建失败` 与
`runtime 启动入口改名后（落 source-only）→ 构建硬失败，不再静默出包` 均 `PASS`
（`delta/harness_head_patched.out`）。→ P2-1 的收紧**没有**破坏任何既有入口
（`smoke_release_package.py` 等合成夹具冒烟全部 exit 0）。

---

## 7. 门禁（打过补丁的沙箱 `trees/head_patched`）

```
$ python scripts/check_quality.py         → exit 0
All checks passed!
PROFILE LOCK OK sha256=333d6a6b… locked_fields=30 symbols=20 quote=USDT
DEPENDENCY LOCK OK packages=50 sha256=a28db842… python=3.13 platform=win32
SECRET SCAN OK files=521 registered_false_positives=2 skipped_bare_identifiers=10 unparsable_python_files=0
QUALITY CHECK OK files=287 untracked=0

$ python scripts/scan_tracked_secrets.py  → exit 0
SECRET SCAN OK files=521 registered_false_positives=2 skipped_bare_identifiers=10 unparsable_python_files=0
```

→ **全绿**，`files=287` / `SECRET SCAN files=521` 与作者自述**逐字一致**。
原始输出：`pkg_q_check_quality.out`、`pkg_q_secret_scan.out`。

---

## 8. 反向检查（任务重点）

1. **P2-1 的收紧会不会误杀正常仓库？** → **不会**。S0（真实仓库）exit 0 / 216 成员；
   S11（真实树去掉 `.git`）exit 0 / 216 成员；S12（改动已跟踪文件内容）exit 0；
   未打补丁的 `head` 树跑冒烟 32/0 exit 0；`smoke_release_package.py` 等既有入口
   在 `scripts=83` 的回归里全部 exit 0。唯一被"收紧"打断的是**无 git 且无
   `src/core/release_manifest.py`** 的合成夹具，而作者已为此保留宽松分支（N2）。
2. **`on_disk` 全集会不会把不该进包的东西算成必需？** → **不会**。`forbidden_paths_absent`
   只对 `_excluded_reason` 命中的路径报错；干净解压目录 `count=0`；未列出的**普通**
   文件（`notes.txt`）不误报（只有 `no_unlisted_files` 报，语义正确）。
3. **`[1c]` 会不会对合法写法误报？** → **不会**。12 个只引用 runtime 脚本的块（0 标记）
   全部通过；注入**已标注**的 source-only 命令 → 0 违规；`[1b]`/`[1c]` 的门禁在本轮
   patched 与 unpatched 两侧都通过。
4. **`[1c]` 会不会漏判？** → 注入未标注的 source-only 命令必报（我自建解析器与作者
   门禁双向一致）。

---

## 9. 新发现（本轮独有，逐条登记）

| ID | 严重度 | 发现 | 位置 | 证据 |
|---|---|---|---|---|
| **N1** | **P3** | 台账"必须随包"的理由块里的子陈述**不实**：三处都写"其中 `docs/contracts/README.md`（冻结目录，不得改）**以相对链接引用它**"。实测 `docs/contracts/` 全目录（README + 6 schema + 6 golden）**无一处**引用台账（`grep -rn "无人值守\|任务清单" docs/contracts/`：唯一命中 `docs/contracts/README.md:8` 指向**执行规格**，不指向台账）。主理由仍成立（5 篇非 contracts 文档直接以 md 相对链接引用它，见 P2-3 变异 B 的 5 条 BROKEN_LINK） | `scripts/create_release_package.py:129`、`docs/运行与故障恢复手册_2026-09-06.md:180`、`docs/文档索引.md:109` | `grep -rn "任务清单" docs/contracts/` = 空；`p23_out/mutB_verify.out` 的 5 条断链名单 |
| **N2** | **P3** | 合成夹具宽松分支（无 git **且** 无 `src/core/release_manifest.py`）下，"未归类闸门"仍是唯一防线：改名成**已登记前缀**形态或直接删除必需入口 → `--out` **exit 0 静默出包**（S10d/S10e）。该分支是 `scripts/smoke_release_package.py` 的 partial tree 所必需；真实仓库/发布包必满足两个判据之一 | `create_release_package.py:678-691` | `p21b_console.txt` S10b/c/d/e |
| **N3** | **P3** | `misfiled` 硬断言只检查**新路径**是否 `startswith(RUNTIME_ASSET_ROOTS)`：把**非必需**源码模块（如 `src/data/cache.py`）挪进**确被排除**的前缀（`docs/dispatch/reviews/`）→ `--out` **exit 0**、成员 215（少 1）、无任何告警。移除项仍出现在 `package_manifest.excluded_files`（理由为路径规则文本），故非"不可审计"，但理由文案会误导（一个源码模块被标为"评审证据归档"） | `create_release_package.py:656-663` | `p21b_console.txt` S8b（成员 215、`cache-ish members` 不含 `src/data/cache.py`）；`ev_classify.txt`（`docs/dispatch/reviews/cache.py → excluded` vs `src/data/cache.py → runtime`） |
| **N4** | **P3（r1 遗留，非本轮引入）** | 部署流交互：按手册 §3.2 在包目录内建好 `.venv`/`.env` 后再自检 → `no_unlisted_files`（r1 既有）+ `forbidden_paths_absent`（本轮改判后新增）**同时**变红。而补丁 01 的 `run_web.py --verify-package` 因 known_limit ① **必须先装依赖**才能运行 → 在"依赖已装"的自然顺序下自检必然报红。手册 §3.2 第 1 步把 `--verify` 排在建 venv **之前**，与 known_limit ① 形成未明写的时序约束。**fail-closed（偏严），非 fail-open**，但应在手册/known_limits 里明写"`--verify`/`--verify-package` 必须在建 `.venv`/`.env` 之前、对**未改动的解压目录**运行" | `create_release_package.py:857-890`；手册 §3.1/§3.2；补丁 README known_limit ① | `p35_console.txt` `[after_venv_and_env]` |
| **N5** | **P3** | P3-10 **未处理且交付物内无登记**（详见 §5 的 P3-10）：`runtime_identity.py:2276` 的 decl-11 note 与 U15 README:180 仍为过期表述（新打包器已无 `SAFE_DOCS`；最新规格已进包；`diag_`/`probe_` 已不进 runtime 包）。r2 提交信息未提 P3-10 | `src/core/runtime_identity.py:2276`、`docs/dispatch/patches/U15/a136cb5/README.md:180` | `git diff --stat 23bfe88 10bc4d2` 不含该两文件；`grep -rn SAFE_DOCS` |

> 另有 1 条**非缺陷**记录：`docs/无人值守升级任务清单_2026-09-19.json` 内的构建机绝对路径
> 未被补丁 04 覆盖 —— 作者已在 r2 README「未做与已知限制」第 5 条如实登记
> （活台账行级补丁必冲突），**登记准确**，不计为发现。
>
> **未发现**任何未登记的 P2 fail-open、崩溃、静默出包或误导性文档。

---

## 10. 命令清单（可复现，均在 `_scratch_Q/` 下）

```bash
# 沙箱（保真度：与 git ls-tree 逐路径 IDENTICAL）
git archive <rev> | tar -x -C trees --one-top-level=<name> && (cd trees/<name> && git init -q . && git add -A && git commit -qm init)
cp -r trees/head trees/head_patched && (cd trees/head_patched && for p in …/1eb1b1e/0*.diff; do git apply -p1 "$p"; done)

# 探针
python probe_p21.py            # P2-1 九类变异
python probe_p21b.py           # P2-1 反向 + 边界（S8b/S10b-e/S11/S12）
python probe_p22.py            # P2-2 自建 fence 解析 + 注入负例/正例
python probe_p23.py            # P2-3 变异 A/B
python probe_p35.py            # P3-5 on_disk 正负例 + 部署流
python build_pkgs.py           # 构建 runtime/source 包 + 解压自检（落 pkg/）
python offline_delta_q.py pristine head_patched   # 全量 delta（非 fail-fast）
# 门禁
(cd trees/head_patched && python scripts/check_quality.py)
(cd trees/head_patched && python scripts/scan_tracked_secrets.py)
(cd trees/head_patched && python scripts/run_offline_regression.py)
(cd trees/head && python scripts/smoke_u10a_release_package.py)   # 未打补丁态
```

---

## 11. 未覆盖项（无法离线验证，明说）

1. **真实宿主部署**：ZIP 解压到目标机、建 venv（已装依赖）、端口绑定、真实
   `run_web.py --profile live` 起服务并进 live 账本、`sc.exe create/start`、日志轮转
   —— 权限内**不启动服务**，归 U12。
2. **`--verify-package` 在"干净解释器"上的行为**：我只能静态确认它位于模块级重依赖
   导入之后（known_limit ①），本机 venv 已装依赖，无法在"未装依赖"解释器上实跑。
3. **`docs/dispatch/reviews/**` 的内容是否真的无凭据**：该前缀按 KI04 范围豁免，
   我按纪律未逐篇读；台账 JSON（195 KB）只做形态核对（自检报告已如实列为免扫成员）。
4. **作者会话内的 r2 报告**（不在仓库内）→ claim ② 之外的自述（如"受影响既有入口逐个
   复跑"的具体数字）只能以树内交付物 + 我的全量回归为准。
5. **U08 未集成**：本 base 不含 U08；"集成 U08 后 delta 是否仍 ∅"未验（归 U12）。
6. **`mat-wt/I` 集成树工作区脏件**：`I/ac.tar`（8.4 MB 散件）仍在集成树根（上一名复验者
   遗留，非本轮产物）。我**未**触碰（只读纪律）；建议编排者清理。

## 12. 原始证据索引（均在 `_scratch_Q/`）

| 文件 | 内容 |
|---|---|
| `p21_console.txt` / `p21_out/p21.json` | P2-1 九类变异逐条 exit/成员/错误行 |
| `p21b_console.txt` / `p21_out/p21b.json` | P2-1 反向与边界（S8b/S10b-e/S11/S12） |
| `p22_console.txt` / `p22_out/audit.json` | P2-2 自建解析器 20 块逐块标记 + 注入正/负例 |
| `p22_membership.txt` | 手册引用脚本 vs 实际 runtime/source 包成员 |
| `p22b_out/run_web_help.out` | `run_web.py --help`（`--profile` / `--verify-package` 真实存在） |
| `p23_console.txt` / `p23_out/` | P2-3 变异 A（smoke exit 1）/ 变异 B（5 条 BROKEN_LINK） |
| `p35_console.txt` / `p35_out/p35.json` | P3-5 正/负例 + 部署流 `.venv`/`.env` |
| `p37_symbols.txt` | 附录 A 的 C1–C14 符号逐条指认 + 13 项自检名 |
| `ev_classify.txt` | `_classify` 对 7 条关键路径的判定（验证 S8/S8b/N3 与台账归属） |
| `ev_fidelity.txt`、`ev_{pristine,head}_{repo,local}.txt` | 保真度比对（与 `git ls-tree -r` 逐路径 IDENTICAL，0 行差异） |
| `ev_applycheck.txt` | `git apply --check` 矩阵（head 树 ×2 配置 + autocrlf=true + pristine 干净树） |
| `ev_selfgate_scan.out` | 本复验工作区自身 `scan_tracked_secrets` → `SECRET SCAN OK files=737` |
| `pkg/q_runtime.zip`、`q_source.zip`、`verify_q_*.out`、`runtime_members.txt` | 实际包与解压自检报告 |
| `pkg_q_check_quality.out`、`pkg_q_secret_scan.out`、`pkg_q_smoke_unpatched.out` | 门禁与未打补丁态冒烟 |
| `delta/summary.txt`、`delta/*.run.log`、`delta/*_logs/`、`delta/harness_head_patched.out` | **全量 delta** 逐脚本 exit code + harness 复跑 |

> 证据卫生：本报告与全部证据文件**不写**会被密钥扫描器匹配的字面量（命中值一律用
> 路径/规则名描述，构建机用户名以 `<用户名>` 占位）。探针只写系统临时目录与本
> `_scratch_Q/`；`git archive` 产物**只落在本工作区**，未写入 `mat-wt/I` 或其它 worktree。
>
> **清洁说明（供后续会话）**：收尾时删除了沙箱树 `trees/`（含 `docs/dispatch/reviews/**`
> 的副本，未跟踪且未被 `.gitignore` 忽略，会让本工作区自己的密钥门禁凭空报红）与解压出的
> `verify_*`/`p35_out` 包目录，**全部原始 stdout / JSON / 探针源码 / 包成员清单均保留**
> （91 MB → 4.7 MB）。删除后本工作区自身 `scan_tracked_secrets.py` 复扫 → **EXIT=0、
> `files=737`**（`ev_selfgate_scan.out`）。需要重建沙箱时按 §10 前两条命令
> （`git archive` → `git init` + `git add -A`），约 1 分钟。
