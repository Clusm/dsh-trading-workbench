"""扫描 Git 已跟踪文件中的疑似凭据。

扫描只输出文件、行号和规则名，绝不把匹配值写入终端、日志或报告。运行时
``.env`` 本来就不应进入 Git；发布前应在暂存/提交后再次运行本脚本，并在发现
泄露时立即撤销旧凭据，而不是只删除工作区文本。
"""
from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]

_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(?:api[_ -]?key|api[_ -]?secret|secret[_ -]?key|"
    r"access[_ -]?token|auth[_ -]?token|client[_ -]?secret|password)\b"
    r"\s*[:=]\s*(?:[\"'](?P<quoted>[^\"']*)[\"']|(?P<bare>[^\s,#;]+))"
)
_PROVIDER_TOKEN_RE = re.compile(r"\b(?:sk|rk|pk)-[A-Za-z0-9][A-Za-z0-9_-]{19,}\b")
_BEARER_TOKEN_RE = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~-]{24,}\b")
_PRIVATE_KEY_RE = re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----")

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


def find_secret_hits(text: str) -> list[SecretFinding]:
    """在文本中返回不含秘密值的可定位命中。"""
    findings: set[tuple[int, str]] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        if _PRIVATE_KEY_RE.search(line):
            findings.add((line_number, "private-key"))
        if _PROVIDER_TOKEN_RE.search(line):
            findings.add((line_number, "provider-token"))
        if _BEARER_TOKEN_RE.search(line):
            findings.add((line_number, "bearer-token"))
        for match in _SECRET_ASSIGNMENT_RE.finditer(line):
            value = match.group("quoted")
            if value is None:
                value = match.group("bare") or ""
            if not _is_placeholder(value) and len(value.strip()) >= 12:
                findings.add((line_number, "secret-assignment"))
    return [SecretFinding(line=line, rule=rule) for line, rule in sorted(findings)]


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


def scan_tracked_files(root: Path = PROJECT_ROOT) -> list[tuple[Path, SecretFinding]]:
    """扫描所有可解码的待扫描文本文件，不读取或打印二进制内容。"""
    hits: list[tuple[Path, SecretFinding]] = []
    for path in scannable_files(root):
        try:
            raw = path.read_bytes()
            if b"\0" in raw:
                continue
            text = raw.decode("utf-8-sig")
        except (OSError, UnicodeDecodeError):
            continue
        hits.extend((path, finding) for finding in find_secret_hits(text))
    return hits


def main() -> int:
    """执行发布前密钥门禁，只输出安全的定位信息。"""
    hits = scan_tracked_files()
    if hits:
        for path, finding in hits:
            print(f"SECRET SCAN FAIL {path.relative_to(PROJECT_ROOT)}:{finding.line} "
                  f"rule={finding.rule}")
        return 1
    print(f"SECRET SCAN OK files={len(scannable_files(PROJECT_ROOT))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
