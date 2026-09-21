# U10b 共享适配补丁

- 任务：U10b 旧脚本与历史文档整理（F 线 / 后置）
- 作者分支：`task/U10b`，base `0706db1`
- 本目录名 `273892c` = 承载 U10b 归档内容的提交（`docs(U10b): add docs/archive/ with index; …`）。
  其后的提交只增加本补丁目录本身。
- 补丁数量：**1**。

## 补丁清单

| 补丁 | 目标共享文件 | 为什么必须应用 |
|---|---|---|
| `01_docs_index_archive_links.diff` | `docs/文档索引.md` | U10b 把 8 篇历史文档从 `docs/` 迁到 `docs/archive/`，本索引里 8 条指向它们的**相对链接**必须同步改写为 `archive/…`。不改写就会在发布包自检 `doc_links_resolve` 上报 8 条 `BROKEN_LINK`，让 `scripts/smoke_u10a_release_package.py` 与真实出包**一起变红** |

补丁内容除了 8 条链接改写，还包含：

1. 首行"最后更新"改为 U10b；
2. "历史复盘、计划与事故记录（旧版）"一节拆成两张表——留在 `docs/` 顶层的仍然逐条列，
   已归档的只列"归档后位置"，把"去向 + 过期点"单一登记在新增的
   `docs/archive/README.md`，避免两份清单漂移；
3. 明示原先标注的"U10b 统一处理"如何落地：`实盘账户并行接入评估与上线整改清单` 正文
   §3.1 的"源码需要 16" schema 断言已过期，**正文不改**（历史证据保持原样），改由归档
   索引给出当前值（**17**）与唯一口径（`current_schema_version()` 的实际返回）。

## 没有补丁的部分（重要）

`scripts/run_offline_regression.py` 的 `SCRIPTS` / `EXCLUDED_SMOKE_SCRIPTS` **一行都没改**，
因为 U10b **没有移动、改名或新增任何 `scripts/smoke_*.py`**。这是刻意的：

> `_validate_smoke_manifest()` 只 glob `scripts/` **顶层** 的 `smoke_*.py`。把冒烟脚本
> 移进 `scripts/diagnostics/` 之类子目录，等于让它静默退出统一离线回归——那是削减回归
> 覆盖而不是整理。因此归档规则里硬性排除所有冒烟脚本。

证据（`pristine` = `0706db1`，`patched` = 本分支 + 本补丁）：

| 指标 | pristine | patched |
|---|---|---|
| `SMOKE MANIFEST OK included=` | 81 | **81** |
| `excluded=` | 1 | 1 |
| `OFFLINE REGRESSION OK scripts=` | 85 | **85** |

N 未减少。

## 应用方式

```bash
# 在 I 的集成候选上（base 0706db1 之后的任意整合 HEAD）
git apply docs/dispatch/patches/U10b/273892c/01_docs_index_archive_links.diff
```

## 应用前核验（3 种 git 配置）

在 `git archive 0706db1 | tar -x` 后 `git init` 的干净解包目录里逐条执行，均只输出
`Checking patch "docs/文档索引.md"...`，**0 条 Skipped、0 条 error**：

| 配置 | 命令 | 结果 |
|---|---|---|
| 默认 | `git apply --check -v <diff>` | Checking patch（通过） |
| LF | `git -c core.autocrlf=false -c core.eol=lf apply --check -v <diff>` | Checking patch（通过） |
| CRLF | `git -c core.autocrlf=true apply --check -v <diff>` | Checking patch（通过） |

补丁刻意只触达 `docs/文档索引.md` 一个文件，且该文件在 base 上为纯 LF（仓库无
`.gitattributes`），因此三种配置下判定一致。

## 应用后的整链复验（patched 沙箱）

沙箱 = `git archive <task/U10b HEAD>` 解包 + `git init` + `git add -A` + 应用本补丁
（已跟踪文件 735 个，比 pristine 多出的 1 个即新增的 `docs/archive/README.md`）：

| 入口 | pristine | patched |
|---|---|---|
| `scripts/run_offline_regression.py` | `SMOKE MANIFEST OK included=81` + `OFFLINE REGRESSION OK scripts=85` | 同左 |
| `scripts/smoke_u10a_release_package.py` | 37 通过 / 0 失败 | **37 通过 / 0 失败** |
| `scripts/check_quality.py` | `QUALITY CHECK OK files=296 untracked=0` | `QUALITY CHECK OK files=296 untracked=0` |

`check_quality.py` 结果未新增任何 ruff exclude；`pyproject.toml` 未改动。

全量离线回归失败集合增量对比（按"失败签名"归一化后取 `patched - pristine`）：

- `NEW_FAIL = ∅`、`FIXED = ∅` —— 两侧失败签名集合**完全相同**（各 10 条）。
- 这 10 条全部来自 `scripts/smoke_u02_strategy_position_manager.py` 的**双态快照**输出：
  该冒烟分三段运行（原始 / 中间 / 应用后）并如实打印每段的 `FAIL` 行，正文以
  `U02 SMOKE OK` 结束、进程退出码 0。两侧逐行相同（`PASS=158 FAIL=0` /
  `PASS=156 FAIL=2` / `PASS=150 FAIL=8` 三段计数一字不差），属既有基线现象，
  与 U10b 无关。

回归入口本身退出码 0，输出 `SMOKE MANIFEST OK included=81 excluded=1` 与
`OFFLINE REGRESSION OK scripts=85`。
