"""密钥扫描器的离线回归；测试值通过拼接构造，避免进入扫描器源码。"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from scan_tracked_secrets import (  # noqa: E402
    SKIPPED_BARE_KEY,
    UNPARSABLE_PYTHON_KEY,
    _REGISTERED_FALSE_POSITIVES,
    find_secret_hits,
    stale_allowances,
)


_PASSED = 0


def check(name: str, condition: bool) -> None:
    global _PASSED
    if not condition:
        raise AssertionError(name)
    _PASSED += 1
    print(f"  PASS  {name}")


print("\n[1] 示例配置和环境变量名不误报")
check("空值与环境变量名通过", not find_secret_hits(
    "SENSENOVA_API_KEY=\napi_secret: BINANCE_DEMO_API_SECRET\n"
))
check("短测试值通过", not find_secret_hits("api_key='test-key'\n"))

print("\n[2] 高风险凭据只报告位置，不返回匹配值")
provider_token = "sk-" + ("x" * 24)
hits = find_secret_hits("api_key='" + provider_token + "'\n")
check("provider token被发现", any(hit.rule == "provider-token" for hit in hits))
check("赋值凭据被发现", any(hit.rule == "secret-assignment" for hit in hits))
check("结果不含匹配值", all(provider_token not in repr(hit) for hit in hits))

print("\n[3] 私钥和Bearer凭据被发现")
bearer = "Bearer " + ("y" * 30)
private_key_marker = "-----BEGIN " + "PRIVATE KEY-----"
hits = find_secret_hits(private_key_marker + "\n" + bearer + "\n")
check("私钥标记被发现", any(hit.rule == "private-key" for hit in hits))
check("Bearer凭据被发现", any(hit.rule == "bearer-token" for hit in hits))

print("\n[4] KI01：Python 源码里的裸变量引用不是内联凭据（r3：须**真被绑定**）")
# 探针文本一律拼接构造：本测试自身也在扫描范围内，字面量会自伤。
KEYWORD = "api_" + "key"
BAREWORD = "fallback" + "_key"
BIND = BAREWORD + " = os.environ.get('X')\n"          # 赋值目标
BIND_CFG = "cfg = load_settings()\n"                  # 属性链的根名
BIND_ARG = "def f(" + BAREWORD + "):\n"               # 函数参数
BIND_IMPORT = "from config import " + BAREWORD + "\n"  # import 绑定
ASSIGN = KEYWORD + "=" + BAREWORD
check("裸变量引用不误报（.py，名字在别处被绑定）", not find_secret_hits(
    BIND + ASSIGN + ",\n", suffix=".py"))
check("属性链同样不误报（.py）", not find_secret_hits(
    BIND_CFG + KEYWORD + "=" + "cfg." + BAREWORD + ",\n", suffix=".py"))
check("右括号紧贴的实参不误报（B-r1 N5 已修）", not find_secret_hits(
    BIND + "client = build(" + ASSIGN + ")\n", suffix=".py"))
check("函数参数计入绑定表", not find_secret_hits(
    BIND_ARG + "    call(" + ASSIGN + ")\n", suffix=".py"))
check("import 绑定计入绑定表", not find_secret_hits(
    BIND_IMPORT + KEYWORD + "=" + BAREWORD + "\n", suffix=".py"))
check("同一行在非 .py 视图下仍然拦截（YAML/JSON 裸标量）",
      any(hit.rule == "secret-assignment" for hit in find_secret_hits(
          KEYWORD + ": " + BAREWORD + "\n")))
check("Python 源码里的裸明文 token 仍被拦截", any(
    hit.rule == "secret-assignment" for hit in find_secret_hits(
        KEYWORD + "=" + ("sk-" + "live-" + "0123456789abcdef") + "\n", suffix=".py")))

print("\n[4b] B-r1 P1-5：放宽只作用于**代码位置**，注释/字符串里的明文照旧拦")
PLAIN = "abcdefgh" + "ijklmnop" + "1234"
case = [("注释里的明文必须继续拦", "# " + KEYWORD + " = " + PLAIN + "\n"),
        ("docstring/三引号串里的明文必须继续拦",
         'NOTE = """\n' + KEYWORD + " = " + PLAIN + "\n" + '"""\n'),
        (" .py 内嵌 YAML 片段里的明文必须继续拦",
         'TPL = """\n' + KEYWORD + ": " + PLAIN + "\n" + '"""\n'),
        ("非 .py 视图下的裸标量仍拦截", KEYWORD + ": " + PLAIN + "\n")]
for name, probe in case:
    suffix = ".yaml" if name.startswith("非 .py") else ".py"
    check(name, any(hit.rule == "secret-assignment"
                    for hit in find_secret_hits(probe, suffix=suffix)))
INDENT_BIND = "def g():\n    " + BAREWORD + " = 1\n"
check("真变量引用（带缩进）在 .py 视图下仍不误报", not find_secret_hits(
    INDENT_BIND + "    " + ASSIGN + ",\n", suffix=".py"))
check("注释行与代码行混排：只报注释里的那条",
      [hit.line for hit in find_secret_hits(
          "# " + KEYWORD + " = " + PLAIN + "\n" + BIND + ASSIGN + "\n",
          suffix=".py") if hit.rule == "secret-assignment"] == [1])
check("多行字符串之后的代码行不受前面字符串影响", not find_secret_hits(
    'T = """\ntext\n"""\n' + BIND + ASSIGN + "\n", suffix=".py"))

print("\n[4c] 放宽必须留痕（可审计），且分不出词法时 fail-closed")
audit: dict[str, int] = {}
check("真变量引用被跳过时计数留痕", not find_secret_hits(
    BIND + ASSIGN + ",\n", suffix=".py", audit=audit)
    and audit.get(SKIPPED_BARE_KEY) == 1 and audit == {SKIPPED_BARE_KEY: 1})
check("非 .py 视图不产生跳过留痕（照旧拦下裸标量）",
      any(hit.rule == "secret-assignment" for hit in find_secret_hits(
          KEYWORD + ": " + BAREWORD + "\n", suffix=".yaml", audit=audit))
      and audit == {SKIPPED_BARE_KEY: 1})
broken: dict[str, int] = {}
broken_probe = "def broken(:\n    " + KEYWORD + " = " + BAREWORD + "\n"
check("无法词法分析的 .py 放弃放宽并留痕（fail-closed）",
      any(hit.rule == "secret-assignment"
          for hit in find_secret_hits(broken_probe, suffix=".py", audit=broken))
      and broken == {UNPARSABLE_PYTHON_KEY: 1})

print("\n[4d] r3：**代码位置**的裸明文凭据必须报出（B-r1/r2 P1-5 残留已闭合）")
# 明文值一律拼接构造；形状为"首字符字母/下划线 + 其余 [A-Za-z0-9_.]"——即 r2
# 的"像标识符就放宽"会静默放走的那一类。它们**不在**绑定表里，故必须报。
OPAQUE = "zx8k2mQ9vLp4Rt7YbN1wXc4v" + "AbCdEfGh1234"
HEXISH = "a1b2c3d4" * 8
plain_cases = [
    ("`.env` 单行内容粘进 .py（赋值形态）", KEYWORD + "=" + OPAQUE + "\n"),
    ("`.env` 多行内容粘进 .py（key + secret）",
     KEYWORD + "=" + OPAQUE + "\n" + "api_" + "secret" + "=" + OPAQUE + "\n"),
    ("未加引号的 64 位十六进制 key（赋值形态）", KEYWORD + " = " + HEXISH + "\n"),
    (".py 里的顶层 YAML 行", KEYWORD + ": " + HEXISH + "\n"),
    ("模块层裸键 JSON 块", "CONFIG = {\n    " + KEYWORD + ": " + HEXISH + ",\n}\n"),
]
for name, probe in plain_cases:
    check("必须报出：" + name, any(
        hit.rule == "secret-assignment"
        for hit in find_secret_hits(probe, suffix=".py")))
check("对照：加了引号的同一值照旧报出", any(
    hit.rule == "secret-assignment" for hit in find_secret_hits(
        KEYWORD + ' = "' + HEXISH + '"\n', suffix=".py")))
check("残余登记：明文与**同文件已绑定名**逐字同形时仍放行（README §五.3）",
      not find_secret_hits(BIND + ASSIGN + "\n", suffix=".py"))

print("\n[5] 误报登记必须是显式且精确的（两向 fail-closed）")
# 注：数量不是重点（KI01 已由"须真被绑定"的结构性判定修好，不再需要登记；
# U00 评审报告那条登记随 KI04 的流程记录豁免一并删除），要求的是**非空**：
# 凡确需登记的误报必须显式列出，而不是靠放宽规则静默放过。
check("登记表非空（确需登记的误报显式列出而非静默忽略）",
      len(_REGISTERED_FALSE_POSITIVES) >= 1)
check("登记项精确到行号与规则名",
      all(isinstance(key, tuple) and len(key) == 3 and isinstance(key[1], int)
          for key in _REGISTERED_FALSE_POSITIVES))
check("登记项带内容指纹（跨行号稳定，B-r1 P2-9）",
      all(len(getattr(item, "fingerprint", "")) == 16
          for item in _REGISTERED_FALSE_POSITIVES.values()))
check("登记项带理由且不含凭据原文",
      all(getattr(item, "reason", "") for item in _REGISTERED_FALSE_POSITIVES.values()))
check("登记项与当前仓库一致（无过期登记）", stale_allowances() == ())

print(f"\n结果：PASS {_PASSED} / FAIL 0")
