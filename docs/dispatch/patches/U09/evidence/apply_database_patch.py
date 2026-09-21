"""按字节级拼接生成 U09 对 ``src/audit/database.py`` 的补丁内容。

为什么要字节级拼接：该文件的历史行尾是**混合**的（部分 CRLF、部分 LF）。用编辑器
直接保存会把整文件归一化，产生 4000+ 行的假 diff。此脚本只替换/插入必要片段，
其余字节（含行尾）原样保留，diff 只显示语义改动。

用法（在仓库根目录）：

    python docs/dispatch/patches/U09/evidence/apply_database_patch.py <target_file>
"""
from __future__ import annotations

import sys
from pathlib import Path


MARKER_BLOCK = '''#: U09 接线标记：下游（含 U09 验收套件）据此判断共享补丁是否已应用，从而在
#: "作者 worktree"与"集成树"两态下各自断言各自应有的行为（U14 同款做法）。
U09_INTEGRATION = "u09.database.v1"

#: 影子来源的**存储别名** → 规范来源名。
#:
#: 这只是存储归一化（与迁移 10 把 ``binance_top_trader_proxy`` 改成
#: ``B_contract_market`` 同类），不是第二份门禁来源清单：**哪些来源能进发布门禁
#: 分母**仍只由 ``intel.shadow_status.COHORTS`` 的 ``gate_role`` 决定，本层不得
#: 增删组或改变语义。``A_strategy`` 是 U06a 之前写入路径使用的旧名，若不归一，
#: 发布门禁会把它当作未知来源（fail-closed 排除），把真实执行候选漏出分母。
#: 每个目标值必须与 ``shadow_status.SOURCE_*`` 逐字相同，由
#: ``tests/acceptance/test_release_evidence_v2.py`` 断言绑定（改名即红）。
SHADOW_SOURCE_ALIASES: dict[str, str] = {
    "A_strategy": "A_current_baseline",
    "a_strategy": "A_current_baseline",
    "binance_top_trader_proxy": "B_contract_market",
}


def canonical_shadow_source(source: Any) -> str:
    """把历史别名归一成 U06a 的规范来源名；非别名原样返回（未知保持未知）。"""
    text = str(source or "")
    return SHADOW_SOURCE_ALIASES.get(text, SHADOW_SOURCE_ALIASES.get(text.strip(), text))


'''

OLD_INSERT_CALL = '''            (decision_id, utc_now(), symbol, signal, reference_price, confidence, source,
             execution_weight, feature_json, effective_environment, effective_release),'''

NEW_INSERT_CALL = '''            (decision_id, utc_now(), symbol, signal, reference_price, confidence,
             canonical_shadow_source(source),
             execution_weight, feature_json, effective_environment, effective_release),'''


def _split_keepends(text: str) -> list[str]:
    return text.splitlines(keepends=True)


def patch(text: str) -> str:
    lines = _split_keepends(text)
    # 1) 在 SCHEMA 定义之前插入标记常量与来源别名归一化函数。
    anchor = next(index for index, line in enumerate(lines)
                  if line.startswith('SCHEMA = """'))
    ending = "\r\n" if lines[anchor - 1].endswith("\r\n") else "\n"
    block = [f"{line}{ending}" for line in MARKER_BLOCK.split("\n")]
    del block[-1]  # 末行是空串占位，去掉
    lines[anchor:anchor] = block
    # 2) 写入路径归一化：只改 shadow_decisions 的 source 值，不动其它列。
    joined = "".join(lines)
    lf_old = OLD_INSERT_CALL
    crlf_old = OLD_INSERT_CALL.replace("\n", "\r\n")
    if lf_old in joined:
        joined = joined.replace(lf_old, NEW_INSERT_CALL)
    elif crlf_old in joined:
        joined = joined.replace(crlf_old, NEW_INSERT_CALL.replace("\n", "\r\n"))
    else:
        raise SystemExit("anchor for log_shadow_decision insert not found")
    return joined


def main() -> int:
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "src/audit/database.py")
    original = target.read_bytes().decode("utf-8")
    patched = patch(original)
    target.write_bytes(patched.encode("utf-8"))
    print(f"patched {target}: {len(original)} -> {len(patched)} chars")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
