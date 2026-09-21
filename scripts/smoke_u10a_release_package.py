"""U10a 发布包冒烟：不漏包、不含凭据/状态、解压后自检与相对文档链接全部通过。

独立可运行（本项目无 pytest）：全部通过退出码 0，任一失败退出码 1。
全程离线：只读**已跟踪**文件清单与仓库内文本，只写系统临时目录；不读真实
``.env``/``data/``、不联网、不启动服务、不改风险档、不调用 ``sc.exe``。

沙箱：把仓库已跟踪文件复制到临时目录并 ``git init`` + ``git add -A``，得到与真实
仓库同构（同一份 .gitignore 生效、``data/`` 仍是未跟踪运行目录）的最小可打包树；
所有构建/篡改/自检都在沙箱里做，真实工作区零写入。

**双态可运行**：共享适配（``scripts/run_web.py`` 的 ``--verify-package``）未应用时，
第 [5] 节退化为"探测并如实标注未接线"，其余各节照常断言；已应用时追加端到端
断言。状态用**运行时行为**探测（真实调用 CLI 看退出码与 stderr），不读共享文件内容、
不假定补丁是否已应用。

运行：
    PYTHONUTF8=1 <repo>/.venv/Scripts/python.exe scripts/smoke_u10a_release_package.py

SCRIPTS 登记行（由 I 加入 ``scripts/run_offline_regression.py`` 的 ``SCRIPTS`` 元组）：
    "scripts/smoke_u10a_release_package.py",
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import create_release_package as crp  # noqa: E402
from scan_tracked_secrets import find_secret_hits  # noqa: E402

PASS = 0
FAIL = 0
FAILURES: list[str] = []
NOTES: list[str] = []


def check(name: str, condition: bool, extra: object = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL  {name}  {extra}")


def note(text: str) -> None:
    NOTES.append(text)
    print(f"  ----  {text}")


def tracked_files(root: Path) -> list[str]:
    """仓库（或沙箱）的已跟踪文件；``-z`` 避免非 ASCII 路径被转义。"""
    result = subprocess.run(
        ["git", "ls-files", "-z"], cwd=root, check=True, capture_output=True,
    )
    return [item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def make_sandbox(source: Path, target: Path) -> list[str]:
    """把已跟踪文件复制成同构沙箱（含 .gitignore 语义）。"""
    files = tracked_files(source)
    for relative in files:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source / relative, destination)
    subprocess.run(["git", "init", "-q"], cwd=target, check=True, capture_output=True)
    subprocess.run(["git", "add", "-A"], cwd=target, check=True, capture_output=True)
    return files


def write_manifest(target: Path) -> Path:
    """在沙箱里生成一份离线发布清单（走仓库真实 API，不读 .env/网络）。"""
    program = (
        "import json,sys;"
        "from pathlib import Path;"
        "sys.path.insert(0, str(Path(sys.argv[1]) / 'src'));"
        "from core.release_manifest import build_release_manifest, write_manifest;"
        "m = build_release_manifest(Path(sys.argv[1]), environment='demo', egress_id='smoke-u10a');"
        "write_manifest(Path(sys.argv[1]) / 'data' / 'release_manifest.json', m);"
        "print(m.release_id)"
    )
    result = subprocess.run(
        [sys.executable, "-c", program, str(target)],
        cwd=target, check=True, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return Path(result.stdout.strip().splitlines()[-1])


def zip_members(archive: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(archive) as handle:
        return {name: handle.read(name) for name in handle.namelist()}


def independent_broken_links(members: dict[str, bytes]) -> list[str]:
    """**独立复算** markdown 相对链接，不使用被测模块的解析函数。"""
    broken: list[str] = []
    names = set(members)
    for name in sorted(names):
        if not name.lower().endswith(".md"):
            continue
        try:
            text = members[name].decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        cursor = 0
        while True:
            start = text.find("](", cursor)
            if start < 0:
                break
            end = text.find(")", start + 2)
            if end < 0:
                break
            target = text[start + 2:end].strip()
            cursor = end + 1
            if not target or target.startswith(
                    ("http://", "https://", "mailto:", "#", "//", "/")):
                continue
            target = target.split("#", 1)[0]
            # 去掉 ``path:行号`` 形式的代码引用后缀
            while ":" in target and target.rsplit(":", 1)[1].isdigit():
                target = target.rsplit(":", 1)[0]
            if not target:
                continue
            stack = list(Path(name).parent.parts)
            for part in Path(target).parts:
                if part == "..":
                    if stack:
                        stack.pop()
                elif part not in (".", ""):
                    stack.append(part)
            resolved = "/".join(stack)
            if resolved not in names:
                broken.append(f"{name} -> {target}")
    return broken


def verify_cli(target: Path) -> tuple[int, dict]:
    """通过真实 CLI 路径运行解压自检，返回 ``(退出码, 报告)``。"""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "create_release_package.py"),
         "--verify", str(target)],
        cwd=target, check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError:
        report = {"ok": False, "problems": [f"非 JSON 输出: {result.stdout[:200]}"]}
    return result.returncode, report


def build_cli(target: Path, output: Path, kind: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "create_release_package.py"),
         "--project-root", str(target),
         "--manifest", str(target / "data" / "release_manifest.json"),
         "--kind", kind, "--out", str(output)],
        cwd=target, check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )


tmp = Path(tempfile.mkdtemp(prefix="mta-u10a-smoke-"))
try:
    sandbox = tmp / "repo"
    sandbox.mkdir(parents=True)
    tracked = make_sandbox(ROOT, sandbox)
    release_id = write_manifest(sandbox)
    note(f"沙箱已跟踪文件 {len(tracked)} 个，release_id={release_id}")

    archive = tmp / "dist" / "mta-runtime.zip"
    built = build_cli(sandbox, archive, "runtime")
    check("runtime 包可构建（CLI 退出码 0）", built.returncode == 0,
          built.stderr.strip()[-200:])
    members = zip_members(archive) if archive.is_file() else {}
    content = {name for name in members if name not in (
        "package_manifest.json", "release_manifest.json")}
    meta = json.loads(members.get("package_manifest.json", b"{}").decode("utf-8"))

    # --- [1] 来源文件和必要配置不漏包 ------------------------------
    print("\n[1] 来源文件和必要配置不漏包（对照仓库应有文件）")
    expected = set(tracked)
    must_pack = {
        relative for relative in expected
        if relative.startswith(("src/", "config/", "knowledge/"))
        or relative.startswith("requirements")
        or relative in ("pyproject.toml", "README.md", "CONTRIBUTING.md", ".env.example")
    }
    missing = sorted(must_pack - content)
    check(f"运行必需文件全部入包（应有 {len(must_pack)} 个）", not missing, missing[:8])
    check("src/data 数据接入层未被目录名规则误伤", {
        "src/data/rate_limit.py", "src/data/binance_transport.py",
        "src/data/market_query.py", "src/data/kline_store.py",
    }.issubset(content))
    check("dashboard 模板与来源记录入包", {
        "src/dashboard/templates/base.html", "src/dashboard/templates/index.html",
        "src/strategy_runtime/PROVENANCE.md", "scripts/start_web.bat",
    }.issubset(content))
    check("策略卡（K01 身份来源）入包",
          "knowledge/strategy_cards.json" in content
          and "knowledge/backup_strategy_cards.json" in content)
    check("脱敏配置模板与部署入口入包",
          ".env.example" in content and "scripts/run_web.py" in content
          and "scripts/windows_service_host.py" in content)
    inventory = meta.get("inventory", {})
    check("归类清单自证完整（runtime+source-only+排除==已跟踪，未归类=0）",
          inventory.get("unclassified") == 0
          and inventory.get("tracked_total") == len(tracked)
          and inventory.get("included", 0) == inventory.get("runtime_assets", 0)
          and (inventory.get("included", 0) + inventory.get("source_only", 0)
               + inventory.get("excluded", 0)) == len(tracked),
          inventory)
    check("报告含逐文件排除理由（可审计而非'已忽略'）",
          bool(meta.get("excluded_files")) and bool(meta.get("inventory", {}).get(
              "excluded_by_reason")))

    # --- [2] 凭据及运行状态不入代码包（对包内容实际扫描） -------------
    print("\n[2] 凭据及运行状态不入代码包（实际扫描包内每一个成员）")
    # ``data``/``logs``/``dist`` 只在**顶层**出现才算运行状态目录；``src/data/``
    # 是合法的数据接入层源码（旧实现正是把它一起丢掉的）。
    toplevel_state_dirs = {"data", "logs", "cache", "dist", "build"}
    anydepth_artifacts = {".venv", "__pycache__", ".git", ".ruff_cache", ".pytest_cache"}
    offenders = sorted(
        name for name in members
        if Path(name).parts[0] in toplevel_state_dirs
        or any(part in anydepth_artifacts for part in Path(name).parts)
        or Path(name).name in (".env", ".env.local", ".env.production")
        or Path(name).name.lower().endswith(
            (".log", ".sqlite", ".sqlite-wal", ".sqlite-shm", ".pem", ".key", ".p12", ".pyc"))
    )
    check("包内无顶层 data/、.env、日志、数据库、密钥、缓存", not offenders, offenders[:8])
    # 凭据扫描与仓库门禁同口径：剔除已登记误报，并遵守 KI04 的**范围豁免**
    # （豁免清单来自扫描器本身，此处只如实记录，不自行放宽）。
    from scan_tracked_secrets import _EXEMPT_FILES, _EXEMPT_PREFIXES  # noqa: PLC0415

    secret_hits: list[str] = []
    exempted: list[str] = []
    for name, raw in members.items():
        if name in _EXEMPT_FILES or name.startswith(_EXEMPT_PREFIXES):
            exempted.append(name)
            continue
        if b"\0" in raw:
            continue
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            continue
        for finding in find_secret_hits(text, suffix=Path(name).suffix):
            secret_hits.append(f"{name}:{finding.line} rule={finding.rule}")
    if exempted:
        note(f"按 KI04 范围豁免未扫描（仓库扫描器登记一致）：{exempted}")
    check("包内文本成员无未登记凭据命中", not secret_hits, secret_hits[:6])
    check("排除说明字段保留（data/ 与 .env 明示）",
          "data/" in meta.get("excluded", []) and ".env" in meta.get("excluded", []))

    # --- [3] 解压后自检 + 相对文档链接 -------------------------------
    print("\n[3] 解压后自检与相对文档链接（独立复算）")
    extracted = crp.extract_archive(archive, tmp / "extracted")
    code, report = verify_cli(extracted)
    check("解压目录自检 CLI 退出码 0", code == 0, report.get("problems"))
    check("自检报告 ok=true 且逐项通过",
          report.get("ok") is True
          and all(item["ok"] for item in report.get("checks", [])),
          [item["name"] for item in report.get("checks", []) if not item["ok"]])
    broken = independent_broken_links(members)
    check(f"所有相对文档链接可达（独立解析，{sum(1 for n in members if n.endswith('.md'))} 篇 md）",
          not broken, broken[:8])
    check("自检覆盖必需资产/凭据/语法/SCM 语义四类",
          {"required_assets_present", "credentials_absent", "python_syntax_ok",
           "windows_service_config_valid"} <= {item["name"] for item in report.get("checks", [])})
    extra = sorted(
        path.relative_to(extracted).as_posix()
        for path in extracted.rglob("*") if path.is_file()
    )
    check("解压目录文件数与包清单一致（无隐式落盘文件）",
          len(extra) == len(members), (len(extra), len(members)))
    code2, report2 = verify_cli(extracted)
    check("自检可重复运行（不在被测目录留下 __pycache__ 自我污染）",
          code2 == 0 and report2.get("ok") is True, report2.get("problems"))

    # --- [4] fail-closed 负例 ---------------------------------------
    print("\n[4] fail-closed 负例（真实篡改，不是断言注入）")
    tampered = tmp / "tampered"
    crp.extract_archive(archive, tampered)
    victim = tampered / "config" / "settings.yaml"
    victim.write_text(victim.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
    code_t, report_t = verify_cli(tampered)
    check("篡改已登记文件后自检失败（哈希不符）",
          code_t != 0 and report_t.get("ok") is False
          and any("file_hashes_match" in item for item in report_t.get("problems", [])),
          report_t.get("problems"))

    extra_file = tmp / "extra"
    crp.extract_archive(archive, extra_file)
    (extra_file / "data").mkdir(parents=True, exist_ok=True)
    (extra_file / "data" / "agent_trader.sqlite").write_bytes(b"state")
    code_e, report_e = verify_cli(extra_file)
    check("包内出现未登记运行状态文件后自检失败",
          code_e != 0 and report_e.get("ok") is False
          and any("no_unlisted_files" in item for item in report_e.get("problems", [])),
          report_e.get("problems"))

    removed = tmp / "removed"
    crp.extract_archive(archive, removed)
    (removed / "knowledge" / "strategy_cards.json").unlink()
    code_r, report_r = verify_cli(removed)
    check("缺失必需策略卡后自检失败",
          code_r != 0 and any("required_assets_present" in item
                              for item in report_r.get("problems", [])),
          report_r.get("problems"))

    linky = tmp / "linky"
    crp.extract_archive(archive, linky)
    doc = linky / "docs" / "运行与故障恢复手册_2026-09-06.md"
    doc.write_text(doc.read_text(encoding="utf-8") + "\n[断链](不存在的文档.md)\n",
                   encoding="utf-8")
    code_l, report_l = verify_cli(linky)
    check("新增相对断链后自检失败",
          code_l != 0 and any("doc_links_resolve" in item
                              for item in report_l.get("problems", [])),
          report_l.get("problems"))

    secret_dir = tmp / "secret"
    crp.extract_archive(archive, secret_dir)
    leak = secret_dir / "config" / "settings.yaml"
    leak.write_text(
        leak.read_text(encoding="utf-8") + '\napi_secret: "AbCdEf0123456789GhIjKl"\n',
        encoding="utf-8",
    )
    code_s, report_s = verify_cli(secret_dir)
    check("已登记文件被写入明文凭据后自检报出凭据命中",
          code_s != 0 and any("credentials_absent" in item
                              for item in report_s.get("problems", [])),
          report_s.get("problems"))

    unclassified = tmp / "unclassified"
    unclassified.mkdir(parents=True)
    make_sandbox(sandbox, unclassified)
    (unclassified / "NOTES.txt").write_text("adhoc\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=unclassified, check=True, capture_output=True)
    write_manifest(unclassified)
    code_u = build_cli(unclassified, tmp / "dist" / "u.zip", "runtime")
    check("存在未归类文件时构建失败（防漏包闸门）",
          code_u.returncode != 0 and "未归类" in (code_u.stderr + code_u.stdout),
          (code_u.returncode, code_u.stderr.strip()[-160:]))

    source_archive = tmp / "dist" / "mta-source.zip"
    built_src = build_cli(sandbox, source_archive, "source")
    src_content = set(zip_members(source_archive)) if source_archive.is_file() else set()
    check("source 包在 runtime 之上补齐 tests/CI（且仍排除 data/.env）",
          built_src.returncode == 0
          and "tests/acceptance/independent/fixtures/f01_unknown_submit_after_response_loss.json"
          in src_content
          and ".github/workflows/ci.yml" in src_content
          and not any(name.startswith("data/") or name == ".env" for name in src_content))

    # --- [5] 共享适配（两态，运行时行为探测） ------------------------
    print("\n[5] 共享适配 scripts/run_web.py --verify-package（运行时行为探测）")
    probe = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "run_web.py"),
         "--verify-package", str(extracted)],
        cwd=ROOT, check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    unsupported = (
        "unrecognized arguments" in (probe.stderr + probe.stdout)
        or "--verify-package" in probe.stderr and probe.returncode == 2
    )
    if probe.returncode == 0:
        check("已接线：run_web.py 能对解压包做启动前自检并退出 0", True)
        payload = json.loads(probe.stdout) if probe.stdout.strip().startswith("{") else {}
        check("自检结论为 ok=true（未启动服务、未绑定端口）", payload.get("ok") is True,
              probe.stdout[:200])
    elif unsupported:
        note("共享适配未应用：scripts/run_web.py 尚无 --verify-package；"
             "本脚本按两态约定不以该补丁为前提（见 docs/dispatch/patches/U10a）")
    else:
        check("run_web.py --verify-package 非预期失败", False,
              f"rc={probe.returncode} {probe.stderr.strip()[-200:]}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n结果: {PASS} 通过 / {FAIL} 失败")
if NOTES:
    print("备注: " + "；".join(NOTES))
if FAILURES:
    for name in FAILURES:
        print(f"  FAILED  {name}")
    raise SystemExit(1)
raise SystemExit(0)
