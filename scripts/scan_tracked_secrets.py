"""扫描 Git 已跟踪文件中的疑似凭据。

扫描只输出文件、行号和规则名，绝不把匹配值写入终端、日志或报告。运行时
``.env`` 本来就不应进入 Git；发布前应在暂存/提交后再次运行本脚本，并在发现
泄露时立即撤销旧凭据，而不是只删除工作区文本。
"""
from __future__ import annotations

import ast
import hashlib
import io
import re
import subprocess
import tokenize
from collections.abc import Mapping, MutableMapping
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_ -]?key|api[_ -]?secret|secret[_ -]?key|"
    r"access[_ -]?token|auth[_ -]?token|client[_ -]?secret|password)\b"
    r"\s*[:=]\s*(?:[\"'](?P<quoted>[^\"']*)[\"']|(?P<bare>[^\s,#;]+))"
)
_PROVIDER_TOKEN_RE = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9][A-Za-z0-9_-]{19,}\b")
#: Python 源码里的裸右值：标识符 / 属性链（``fallback_key``、``cfg.key``）。
#:
#: **不要**用"未加引号的右值过不了语法解析"来论证这类匹配不是凭据——这不成立：
#: ``api_key = <40 位字母数字>`` 是完全合法的 Python（运行期才 NameError），
#: 因此"长得像标识符"**不足以**把它判成代码引用。r2 曾据此放宽，结果"把 ``.env``
#: 内容粘进 ``.py``"这类**代码位置**的裸明文凭据被静默漏报（B-r1/r2 P1-5）。
#:
#: r3 口径（两条**同时**成立才放宽）：
#:   1. 该名字在本模块的**符号表里确实被绑定过**（:func:`_bound_names`：赋值目标 /
#:      import / 函数参数 / ``with as`` / ``for`` 目标 / def·class / ``except as``
#:      / ``global``·``nonlocal``）。孤立出现的裸明文即使形状像标识符也**照报**；
#:   2. 该出现位置**在真正的代码里**（:func:`_non_code_spans` 的补集）。
#:
#: 注释、docstring/三引号串、内嵌 YAML/JSON 片段里"未加引号"完全合法——那里出现
#: 的明文凭据（注释掉的赋值、配置模板里的裸标量）**必须继续拦**。
#: **分不出词法/语法就放弃放宽**（fail-closed，宁可多报）。
#: 放宽**只对 ``.py`` 生效**：YAML/JSON 文件里的裸标量可以是明文凭据。
#:
#: **已知残余（如实登记，见交付 README §五.3）**：若明文凭据的形状恰好与**同一
#: 文件里某个已绑定名字**逐字相同，则仍会被放行（例如把 ``.env`` 里的
#: ``api_key=<某个已绑定变量名>`` 粘进 ``.py``）——此时右值与真变量引用在文本上
#: 不可区分，属刻意保留的误报抑制代价。
_PY_CODE_REFERENCE_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
#: 裸 token 的尾部分隔符：调用实参里右值常与右括号/逗号紧贴（``build(<kw>=<name>)``），
#: r2 的 ``fullmatch`` 因此判不出"这是变量引用"而误报了该形态（B-r1 N5）。
_TRAILING_DELIMITERS = ")]},;:"
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~-]{24,}\b")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

#: 审计计数器键：放宽**必须留痕**，否则"跳过了多少候选"不可复核。
SKIPPED_BARE_KEY = "skipped_bare_identifiers"
UNPARSABLE_PYTHON_KEY = "unparsable_python_files"


@dataclass(frozen=True)
class _Allowance:
    """一条显式误报登记：理由 + **命中行内容指纹**。

    为什么要指纹（B-r1 P2-9）：只锚行号，U15 一旦增删行 → 全仓密钥门禁因
    ``STALE_ALLOWANCE`` 变红（跨任务的隐式耦合）；同时"该行被替换成真凭据"
    会被静默抑制。加了内容指纹后：行号漂移但内容逐字不变 → 登记仍有效；
    内容一变（含换成真凭据）→ 登记失效 → 门禁失败并要求重新复核。
    """

    reason: str
    fingerprint: str


#: 已登记的**误报**（不是"放行真实凭据"）：``(仓库相对路径, 行号, 规则名) -> 登记``。
#: 命中只有在 ``(路径, 规则, 行内容指纹)`` 三者都吻合时才被剔除；不含糊放行整类
#: 规则。登记项若不再命中任何发现（含内容被改动），扫描以 ``STALE_ALLOWANCE``
#: **失败**——两个方向都 fail-closed，不存在"静默放行"。
#:
#: 行号按**集成树**（本包的补丁基线）填写；指纹分支让同一登记在行号漂移的树上
#: （如 U13 删掉一行 import 后的旧树）仍然生效，不需要改行号。
_REGISTERED_FALSE_POSITIVES: dict[tuple[str, int, str], _Allowance] = {
    ("docs/dispatch/reviews/U00/0b85bef/C_w0r1_r2.md", 31, "secret-assignment"):
        _Allowance(
            reason="U00 r2 评审报告引用被测字符串的原文片段（值为省略号+中文说明，非凭据）",
            fingerprint="deab45caecdfeaaf",
        ),
    ("tests/acceptance/test_runtime_identity.py", 523, "provider-token"):
        _Allowance(
            reason="U15 刻意构造的 sk- 形状哨兵（should-never-appear），用于断言凭据不外泄",
            fingerprint="98e78280165ece7f",
        ),
    ("tests/acceptance/test_runtime_identity.py", 707, "provider-token"):
        _Allowance(
            reason="U15 刻意构造的 sk- 形状哨兵（NEVER-LEAK），用于断言递归敏感过滤",
            fingerprint="fd4ba52305b076b5",
        ),
}

_PLACEHOLDERS = {
    "",
    "abc",
    "k",
    "s",
    "test",
    "test-key",
    "must-not-leak",
    "secret-value",
    "runtime-secret",
    "changeme",
    "change-me",
    "replace-me",
    "replace_me",
    "your-api-key",
    "your_api_key",
    "none",
    "null",
}


@dataclass(frozen=True)
class SecretFinding:
    """不携带匹配文本的扫描结果。"""

    line: int
    rule: str
    #: 命中行的**归一化内容指纹**（sha256 前 16 位十六进制）。哈希不可逆、永不
    #: 打印，只用于让显式误报登记跨行号稳定（B-r1 P2-9），不泄露匹配值。
    fingerprint: str = ""


def line_fingerprint(line: str) -> str:
    """命中行的归一化指纹：压掉空白差异，只对内容形状敏感。"""
    return hashlib.sha256(" ".join(str(line).split()).encode("utf-8")).hexdigest()[:16]


def _is_placeholder(value: str) -> bool:
    """判断示例值、环境变量名和代码表达式，避免把测试夹具误报为凭据。"""
    normalized = value.strip().strip("\"'").strip()
    lowered = normalized.lower()
    if lowered in _PLACEHOLDERS or lowered.startswith(("${", "<", "your-", "your_")):
        return True
    # 配置通常保存的是环境变量名，而非密钥本身。
    if re.fullmatch(r"[A-Z][A-Z0-9_]*", normalized) and any(
            marker in normalized for marker in ("KEY", "SECRET", "TOKEN")):
        return True
    # Python 代码中的变量/函数引用不是内联凭据。
    if normalized.startswith(("os.", "self.", "str(", "getattr(", "environ", "None")):
        return True
    return False


def _reference_root(value: str) -> str | None:
    """裸值若是"代码引用"**形状**（标识符/属性链，允许尾随分隔符）就返回其根名。

    ``"fallback_key,"`` / ``"cfg.llm.key)"`` 都算引用形状（行内实参常带分隔符，
    见 B-r1 N5）；形状只是**必要条件**——是否真的是变量引用还要看根名有没有被
    绑定（:func:`_bound_names`），单靠形状会把明文凭据放走。
    """
    text = value.strip()
    if not text:
        return None
    core = text.rstrip(_TRAILING_DELIMITERS)
    if not _PY_CODE_REFERENCE_RE.fullmatch(core):
        return None
    return core.split(".", 1)[0]


def _bound_names(text: str) -> frozenset[str] | None:
    """模块里**确实被绑定过**的顶层/局部名字集合；无法解析时返回 ``None``。

    绑定位置：赋值（含增强赋值、海象、解包）目标、``for``/``with ... as`` 目标、
    函数/类定义名、函数参数、``import``/``from ... import`` 的绑定名、
    ``except ... as``、``global``/``nonlocal``。**只被读取、从未被绑定**的名字
    不在集合里——这正是"形状像标识符的裸明文凭据"与"真变量引用"的分界。
    """
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, ast.arg):
            names.add(node.arg)
        elif isinstance(node, ast.alias):
            names.add((node.asname or node.name).split(".", 1)[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            names.update(node.names)
    return frozenset(names)


def _non_code_spans(text: str) -> dict[int, tuple[tuple[int, int], ...]] | None:
    """``{行号: ((起始列, 结束列), ...)}``：该行属于**注释/字符串**的列区间。

    返回 ``None`` 表示这份文本**无法按 Python 词法分析**（语法/缩进错误、或是
    被当成 ``.py`` 扫描的非 Python 片段）——调用方必须**放弃放宽**（fail-closed：
    宁可多报，不可漏报）。

    多行字符串按"整段都算文本"标记：**过标记只会多报，漏标记才是盲区**。
    """
    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError, ValueError):
        return None
    spans: dict[int, list[tuple[int, int]]] = {}
    last_column = 1 << 30

    def _mark(start: tuple[int, int], end: tuple[int, int]) -> None:
        (row_start, col_start), (row_end, col_end) = start, end
        if row_end < row_start:
            return
        if row_start == row_end:
            spans.setdefault(row_start, []).append((col_start, col_end))
            return
        spans.setdefault(row_start, []).append((col_start, last_column))
        for row in range(row_start + 1, row_end):
            spans.setdefault(row, []).append((0, last_column))
        spans.setdefault(row_end, []).append((0, col_end))

    fstring_middle = getattr(tokenize, "FSTRING_MIDDLE", None)
    for token in tokens:
        if token.type in (tokenize.COMMENT, tokenize.STRING) or (
                fstring_middle is not None and token.type == fstring_middle):
            _mark(token.start, token.end)
    return {row: tuple(items) for row, items in spans.items()}


def _in_non_code_text(spans: dict[int, tuple[tuple[int, int], ...]],
                      line_number: int, column: int) -> bool:
    """该列位置是否落在注释/字符串（即"文本"）内部。"""
    for start, end in spans.get(line_number, ()):
        if start <= column < end:
            return True
    return False


def _bump(audit: MutableMapping[str, int] | None, key: str) -> None:
    if audit is not None:
        audit[key] = int(audit.get(key, 0)) + 1


def find_secret_hits(text: str, *, suffix: str = "",
                     audit: MutableMapping[str, int] | None = None) -> list[SecretFinding]:
    """在文本中返回不含秘密值的可定位命中。

    ``suffix`` 是文件后缀（如 ``".py"``）：只有 Python 源码会启用"裸右值是变量
    引用"的判定；缺省空串表示"不按 Python 处理"，行为与旧版完全一致（向后兼容）。
    该放宽**只作用于真正的代码位置**、且**只对真被绑定过的名字**生效——注释/字符串/
    内嵌 YAML 里的裸标量、以及孤立出现的"像标识符的"裸明文照旧拦。

    ``audit`` 非空时把放宽的**留痕**写进去（:data:`SKIPPED_BARE_KEY` 跳过了多少
    裸标识符候选、:data:`UNPARSABLE_PYTHON_KEY` 有多少 ``.py`` 因无法词法/语法分析
    而整体放弃放宽），使放宽可审计。
    """
    findings: set[tuple[int, str, str]] = set()
    python_source = str(suffix).lower() == ".py"
    lines = text.splitlines()
    spans: dict[int, tuple[tuple[int, int], ...]] | None = None
    bound: frozenset[str] | None = None
    if python_source:
        spans = _non_code_spans(text)
        bound = _bound_names(text)
        if spans is None or bound is None:
            # 分不出"代码/文本"或读不出"符号表"就不放宽；fail-closed 取舍。
            spans = bound = None
            _bump(audit, UNPARSABLE_PYTHON_KEY)
    for line_number, line in enumerate(lines, start=1):
        fingerprint = line_fingerprint(line)
        if _PRIVATE_KEY_RE.search(line):
            findings.add((line_number, "private-key", fingerprint))
        if _PROVIDER_TOKEN_RE.search(line):
            findings.add((line_number, "provider-token", fingerprint))
        if _BEARER_TOKEN_RE.search(line):
            findings.add((line_number, "bearer-token", fingerprint))
        for match in _SECRET_ASSIGNMENT_RE.finditer(line):
            quoted = match.group("quoted")
            if quoted is not None:
                value = quoted
            else:
                value = match.group("bare") or ""
                if spans is not None and bound is not None:
                    root = _reference_root(value)
                    if (root is not None and root in bound
                            and not _in_non_code_text(spans, line_number,
                                                      match.start("bare"))):
                        # 代码位置上、且根名**确实被绑定过** = 变量引用，不是内联凭据。
                        # 孤立出现的同形状裸明文（未被绑定）走不到这里 -> 照报。
                        _bump(audit, SKIPPED_BARE_KEY)
                        continue
            if not _is_placeholder(value) and len(value.strip()) >= 12:
                findings.add((line_number, "secret-assignment", fingerprint))
    return [SecretFinding(line=line, rule=rule, fingerprint=fingerprint)
            for line, rule, fingerprint in sorted(findings)]


def _git_files(root: Path, *extra_args: str) -> list[Path]:
    """按给定视图列出文件；``--exclude-standard`` 保证被忽略的运行时目录不会进入。"""
    result = subprocess.run(
        ["git", "ls-files", "-z", *extra_args], cwd=root, check=True,
        capture_output=True,
    )
    return [root / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def _tracked_files(root: Path) -> list[Path]:
    """获取 Git 索引中的文件，避免扫描未纳入发布范围的本地运行时目录。"""
    return _git_files(root)


def scannable_files(root: Path) -> list[Path]:
    """返回待扫描文件 = 已跟踪文件 + 未跟踪但未被忽略的文件。

    2026-09-15 修正：旧实现只扫 Git 索引。这样"新增但尚未提交"的脚本在校验时
    完全隐形，而它们随后就会被提交——密钥门禁的作用是在**提交之前**拦下来，
    不是等进了索引才看。未跟踪文件同样排除 .gitignore 命中的运行时目录
    （``data/``、``.env``、``.venv/`` 等），因此不会扫到本机真实凭据。
    """
    tracked = _git_files(root)
    untracked = _git_files(root, "--others", "--exclude-standard")
    merged: dict[str, Path] = {}
    for path in [*tracked, *untracked]:
        merged[str(path)] = path
    return sorted(merged.values())


def raw_hits(root: Path = PROJECT_ROOT, *,
             audit: MutableMapping[str, int] | None = None,
             ) -> list[tuple[Path, SecretFinding]]:
    """**未过滤**的扫描结果：登记表只在这里之外过滤，便于做过期登记检查。"""
    hits: list[tuple[Path, SecretFinding]] = []
    for path in scannable_files(root):
        try:
            raw = path.read_bytes()
            if b"\0" in raw:
                continue
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        hits.extend((path, finding) for finding in find_secret_hits(
            text, suffix=path.suffix, audit=audit))
    return hits


def _relative(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


def _allowance_matches(allowances: Mapping[tuple[str, int, str], _Allowance],
                       relative: str, finding: SecretFinding,
                       *, by_fingerprint: set[tuple[str, str, str]]) -> bool:
    """登记是否覆盖这条命中：同一行同一规则**且内容指纹一致**，或指纹一致仅行号漂移。"""
    exact = allowances.get((relative, finding.line, finding.rule))
    if exact is not None and exact.fingerprint == finding.fingerprint:
        return True
    return (relative, finding.rule, finding.fingerprint) in by_fingerprint


def stale_allowances(root: Path = PROJECT_ROOT) -> tuple[str, ...]:
    """登记了却不再命中的误报项：文件改动（含行内容变化）后登记必须失效。"""
    hits = raw_hits(root)
    matched_lines = {(_relative(path, root), finding.line, finding.rule):
                     finding.fingerprint for path, finding in hits}
    matched_prints = {(_relative(path, root), finding.rule, finding.fingerprint)
                      for path, finding in hits}
    stale: list[str] = []
    for (path, line, rule), allowance in sorted(_REGISTERED_FALSE_POSITIVES.items()):
        if matched_lines.get((path, line, rule)) == allowance.fingerprint:
            continue
        if (path, rule, allowance.fingerprint) in matched_prints:
            continue
        stale.append("%s:%d rule=%s" % (path, line, rule))
    return tuple(stale)


def scan_tracked_files(root: Path = PROJECT_ROOT, *,
                       audit: MutableMapping[str, int] | None = None,
                       ) -> list[tuple[Path, SecretFinding]]:
    """扫描所有可解码的待扫描文本文件，不读取或打印二进制内容。

    已登记误报按 ``(相对路径, 行号, 规则) + 行内容指纹`` 精确剔除；不含糊放行
    整类规则，也不因行号漂移而牵连他人（B-r1 P2-9）。
    """
    hits = raw_hits(root, audit=audit)
    by_fingerprint = {(path, rule, allowance.fingerprint)
                      for (path, _, rule), allowance
                      in _REGISTERED_FALSE_POSITIVES.items()}
    return [
        (path, finding) for path, finding in hits
        if not _allowance_matches(_REGISTERED_FALSE_POSITIVES,
                                  _relative(path, root), finding,
                                  by_fingerprint=by_fingerprint)
    ]


def main() -> int:
    """执行发布前密钥门禁，只输出安全的定位信息。"""
    audit: dict[str, int] = {}
    stale = stale_allowances()
    if stale:
        for item in stale:
            print(f"STALE_ALLOWANCE {item}（登记与现状不符：请复核后更新登记）")
        return 1
    hits = scan_tracked_files(audit=audit)
    if hits:
        for path, finding in hits:
            print(f"SECRET SCAN FAIL {path.relative_to(PROJECT_ROOT)}:{finding.line} "
                  f"rule={finding.rule}")
        return 1
    allowed = len(_REGISTERED_FALSE_POSITIVES)
    skipped = int(audit.get(SKIPPED_BARE_KEY, 0))
    unparsable = int(audit.get(UNPARSABLE_PYTHON_KEY, 0))
    print(f"SECRET SCAN OK files={len(scannable_files(PROJECT_ROOT))} "
          f"registered_false_positives={allowed} "
          f"skipped_bare_identifiers={skipped} unparsable_python_files={unparsable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
