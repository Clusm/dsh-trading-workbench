"""U12 验收：RC 交付物与 F01–F25 故障矩阵的一致性（全程离线）。

独立可运行（无 pytest）：

    PYTHONUTF8=1 <venv>/python.exe tests/acceptance/test_unattended_release.py

本套件验收的是 **RC 交付物**（不是产品行为）：

1. 发布门禁阈值**逐字未降低**（200 / 30 / 7 / 0.999 / 120），且不存在把 `required`
   调低的调用路径（`U09-P3-CLUSTER` 记录的入参风险只登记、不在此被"合法化"）。
2. `observed_status` 把 **7 天 / 200 方向候选 / 30 完整闭合** 三项显式标为
   `NOT_STARTED`，**任何门禁都不得被写成通过**。
3. `F11` 逐字记录 **`F11:v2_labels=DEFERRED`**，且集成树代码常量一致
   （`V2_LABELS_STATE == "DEFERRED"`、`EXECUTION_WEIGHT == 0.0`）。
4. `F01–F25` 在矩阵中**逐条**出现且各一次；缺能力的条目**写明缺什么**，**不得写 N/A**。
5. `K06` 全绿**不等于**发布通过（不得宣称 `RELEASE_READY` / 已冻结 / 已通过观察窗）。
6. 发布报告在**缺表/缺身份**时 **fail-closed**（抛错或 `NO_DATA`），绝不伪造通过。
7. CI 入口确实调用官方离线回归 `scripts/run_offline_regression.py`。

**两态可跑**（本项目血的教训）：不假定共享适配补丁（`core.health_watch` 等）是否已应用
——两态各自断言各自应有的语义，缺符号**逐条判失败并给出原因码**，绝不 `ImportError` 崩掉。
只读本仓文档且只写系统临时目录：不读 `.env`/真实 `data`/密钥、不联网、不下单、
不启动服务、不动风险档。
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

RC_DIR = ROOT / "docs" / "releases" / "unattended_rc"
README = RC_DIR / "README.md"
MATRIX = RC_DIR / "FAULT_MATRIX_F01_F25.md"
OBSERVED_MD = RC_DIR / "observed_status.md"
OBSERVED_JSON = RC_DIR / "observed_status.json"
CI = ROOT / ".github" / "workflows" / "ci.yml"

PASS = 0
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    global PASS
    if ok:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")
    return ok


def section(title: str) -> None:
    print(f"\n=== {title} ===")


def read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"<UNREADABLE {type(exc).__name__}: {exc}>"


#: 否定/限定标记：出现这些词的同一行不构成"宣称"。
_NEGATIONS = ("不", "未", "非", "禁止", "不得", "DRAFT", "由编排者", "待裁决")


def unnegated_claims(text: str, phrases: tuple[str, ...]) -> list[str]:
    """返回**被当作事实宣称**（同行没有任何否定/限定标记）的短语。

    文档合法地会*提及* "RC 已冻结"（例如"本文档不宣布 RC 已冻结"）。
    本函数只把**没有否定语境**的出现判为宣称，避免把提及误判成宣称，
    也避免测试因为措辞天然含否定词而恒真/恒假。
    """
    hits: list[str] = []
    for line in text.splitlines():
        if any(neg in line for neg in _NEGATIONS):
            continue
        for phrase in phrases:
            if phrase in line:
                hits.append(f"{phrase} :: {line.strip()[:120]}")
    return hits


# --------------------------------------------------------------------------- #
# [1] 发布门禁阈值逐字未降低
# --------------------------------------------------------------------------- #
def test_thresholds_frozen() -> None:
    section("[1] 发布门禁阈值逐字未降低（7 天 / 200 候选 / 30 闭合）")
    try:
        from core.release_evidence import DEFAULT_THRESHOLDS  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - 两态都不得崩
        check("core.release_evidence 可导入", False, f"{type(exc).__name__}: {exc}")
        return
    expected = {
        "min_directional_candidates": 200,
        "min_eligible_closed_trades": 30,
        "min_observation_days": 7.0,
        "min_observation_coverage": 0.999,
        "max_observation_gap_seconds": 120.0,
    }
    for key, want in expected.items():
        got = DEFAULT_THRESHOLDS.get(key)
        check(f"阈值 {key} == {want!r}（实测 {got!r}）", got == want)

    # 阈值必须来自模块常量，不能被"每调用一份"改写（两态都成立的静态事实）。
    source = read(ROOT / "src" / "core" / "release_evidence.py")
    check("release_evidence.py 可读", "DEFAULT_THRESHOLDS" in source)
    # 唯一允许的 thresholds 覆盖是显式入参；这里只登记其存在，不把它当合法降级路径。
    check("thresholds 入参风险已登记（U09-P3/A5），本套件不合法化降级",
          "thresholds" in source and "待裁决" in read(MATRIX))


# --------------------------------------------------------------------------- #
# [2] observed_status 不得写成通过
# --------------------------------------------------------------------------- #
def test_observed_status_not_started() -> None:
    section("[2] observed_status：三项观察门禁必须为 NOT_STARTED")
    raw = read(OBSERVED_JSON)
    check("observed_status.json 存在且可读", "NOT_STARTED" in raw)
    try:
        data = json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        check("observed_status.json 可解析", False, f"{type(exc).__name__}: {exc}")
        return
    gates = data.get("gates") or {}
    for key in ("observation_days", "directional_candidates", "eligible_closed_trades"):
        gate = gates.get(key) or {}
        check(f"{key}: state=NOT_STARTED 且 actual 为空",
              gate.get("state") == "NOT_STARTED" and gate.get("actual") is None,
              f"state={gate.get('state')!r} actual={gate.get('actual')!r}")
    states = {str((g or {}).get("state")) for g in gates.values()}
    check("没有任何门禁被写成 PASS", "PASS" not in states, f"states={sorted(states)}")
    check("thresholds_lowered 为 False", data.get("thresholds_lowered") is False)
    check("live_enabled 为 False", data.get("live_enabled") is False)
    check("K06 全绿 != 发布通过 已声明",
          data.get("k06_all_green_is_not_release_pass") is True)
    check("release_id 未被编造（绑定 release_manifest.json）",
          data.get("release_id") is None and "release_manifest.json" in str(
              data.get("release_id_note") or ""))

    md = read(OBSERVED_MD)
    check("observed_status.md 含三项 NOT_STARTED 表述",
          md.count("NOT_STARTED") >= 3 and "未开始" in md)
    check("observed_status.md 标记 DRAFT 且声明不得写成通过",
          "DRAFT" in md and "不得写成通过" in md)
    claims = unnegated_claims(md, ("已冻结", "已通过", "RELEASE_READY"))
    check("observed_status.md 未把冻结/通过写成事实宣称", not claims,
          str(claims))


# --------------------------------------------------------------------------- #
# [3] F11 v2_labels=DEFERRED（逐字）
# --------------------------------------------------------------------------- #
def test_f11_deferred() -> None:
    section("[3] F11：v2_labels=DEFERRED（逐字）")
    matrix = read(MATRIX)
    check("矩阵逐字含 `F11:v2_labels=DEFERRED`",
          "F11:v2_labels=DEFERRED" in matrix)
    # 代码常量必须与文档一致（不照抄作者自述，直接读集成树常量）。
    try:
        from intel import shadow_status  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        check("intel.shadow_status 可导入", False, f"{type(exc).__name__}: {exc}")
        return
    state = getattr(shadow_status, "V2_LABELS_STATE", None)
    weight = getattr(shadow_status, "EXECUTION_WEIGHT", None)
    scope = getattr(shadow_status, "FIRST_RELEASE_FAULT_SCOPE", None)
    check(f"V2_LABELS_STATE == 'DEFERRED'（实测 {state!r}）", state == "DEFERRED")
    check(f"EXECUTION_WEIGHT == 0.0（实测 {weight!r}）", weight == 0.0)
    check("FIRST_RELEASE_FAULT_SCOPE 已声明首批范围",
          isinstance(scope, str) and "ISOLATION_ONLY" in scope)
    check("矩阵把 F11 标为 DEFERRED（不得写通过）",
          "**`DEFERRED`**" in matrix and "不得算作已验收功能" in matrix)


# --------------------------------------------------------------------------- #
# [4] F01–F25 逐条出现，且缺能力不写 N/A
# --------------------------------------------------------------------------- #
def test_matrix_covers_all_faults() -> None:
    section("[4] F01–F25 逐条覆盖与'不得写 N/A'")
    matrix = read(MATRIX)
    missing = [f"F{n:02d}" for n in range(1, 26)
               if f"**F{n:02d}**" not in matrix]
    check(f"F01–F25 逐条出现（缺：{missing}）", not missing)
    check("矩阵含四类覆盖状态词表",
          all(token in matrix for token in (
              "COVERED_OFFLINE", "PARTIAL_OFFLINE", "DEFERRED", "NOT_VERIFIABLE_NOW")))
    check("矩阵含'缺什么能力'清单（§D）",
          "§D" in matrix and "缺失能力清单" in matrix)
    check("矩阵明确禁止把缺失能力写成 N/A",
          "写成 N/A" in matrix and ("部分依赖" in matrix or "缺" in matrix))
    # 反例：不得把某条缺陷直接写成 N/A 覆盖状态。
    na_rows = re.findall(r"\|\s*N/A\s*\|", matrix)
    check("矩阵表格中不存在 N/A 覆盖状态", not na_rows, f"found={len(na_rows)}")
    check("F14/F18 引用两份报告（原始 + 复验）",
          "drill_F14_F18/REPORT.md" in matrix and "U13-FU1/af76443/Q_r1.md" in matrix)
    check("F14/F18 保留'不得记作完全通过'",
          "不得记作" in matrix and "待裁决" in matrix)


# --------------------------------------------------------------------------- #
# [5] 不得宣称已冻结/已通过；live 未启用
# --------------------------------------------------------------------------- #
def test_no_release_claims() -> None:
    section("[5] 不得宣称 RC 已冻结 / 已通过观察窗")
    # 负向自检：证明"宣称检测器"非空转——正例必须被检出、否定例必须被放过。
    must_flag = unnegated_claims("状态：RC 已冻结，观察窗已通过。", ("已冻结", "已通过"))
    must_skip = unnegated_claims("本文档不得宣布 RC 已冻结。", ("已冻结",))
    check("宣称检测器非空转（正例检出 / 否定例放过）",
          len(must_flag) >= 2 and not must_skip,
          f"flag={must_flag} skip={must_skip}")
    readme = read(README)
    check("README 标记 DRAFT", "DRAFT" in readme)
    check("README 明示未冻结", "未冻结" in readme)
    check("README 明示观察门禁未开始", "未开始" in readme)
    claims = unnegated_claims(readme, ("已冻结", "已通过", "RELEASE_READY", "BUILD_VERIFIED"))
    check("README 未把冻结/通过/发布状态写成事实宣称", not claims, str(claims))
    check("README 保留回滚段", "回滚" in readme)
    check("README 登记待裁决项且不自行决定", "待裁决" in readme and "不自行决定" in readme)


# --------------------------------------------------------------------------- #
# [6] 发布报告 fail-closed（缺表/缺身份不得伪造通过）
# --------------------------------------------------------------------------- #
def test_report_fail_closed() -> None:
    section("[6] build_release_readiness_report 在缺证据时 fail-closed")
    try:
        from core.release_evidence import build_release_readiness_report  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        check("build_release_readiness_report 可导入", False,
              f"{type(exc).__name__}: {exc}")
        return
    with tempfile.TemporaryDirectory(prefix="u12_rc_acc_") as tmp:
        empty_db = Path(tmp) / "empty.sqlite"
        sqlite3.connect(empty_db).close()
        try:
            build_release_readiness_report(
                empty_db, {"release_id": "rel-test"}, environment="demo")
        except Exception as exc:  # noqa: BLE001 - 期望抛错（缺表 fail-closed）
            check("缺表的库 → 抛错而不是伪造通过",
                  True, f"{type(exc).__name__}")
        else:
            check("缺表的库 → 抛错而不是伪造通过", False, "未抛错")

    # 缺 release_id 必须拒绝（身份不可缺省）。
    with tempfile.TemporaryDirectory(prefix="u12_rc_acc_") as tmp:
        empty_db = Path(tmp) / "empty2.sqlite"
        sqlite3.connect(empty_db).close()
        try:
            build_release_readiness_report(empty_db, {}, environment="demo")
        except Exception:  # noqa: BLE001 - 期望 ValueError
            check("缺 release_id → 拒绝（ValueError）", True)
        else:
            check("缺 release_id → 拒绝（ValueError）", False, "未抛错")


# --------------------------------------------------------------------------- #
# [7] CI 入口确实调用官方离线回归
# --------------------------------------------------------------------------- #
def test_ci_wires_official_entry() -> None:
    section("[7] CI 接入官方离线回归")
    ci = read(CI)
    check("ci.yml 存在且可读", "run_offline_regression.py" in ci)
    check("CI 运行 check_quality.py", "check_quality.py" in ci)
    check("CI 运行本 RC 验收脚本",
          "test_unattended_release.py" in ci)
    check("CI 强制 UTF-8（避免仅因控制台编码误报）",
          "PYTHONUTF8" in ci)


def main() -> int:
    print("U12 RC 交付物验收（离线；两态可跑）")
    test_thresholds_frozen()
    test_observed_status_not_started()
    test_f11_deferred()
    test_matrix_covers_all_faults()
    test_no_release_claims()
    test_report_fail_closed()
    test_ci_wires_official_entry()
    print(f"\nU12 UNATTENDED RELEASE ACCEPTANCE PASS={PASS} FAIL={len(FAIL)}")
    if FAIL:
        for name in FAIL:
            print(f"  FAILED  {name}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
