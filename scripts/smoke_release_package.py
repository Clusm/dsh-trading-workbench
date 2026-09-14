"""发布包白名单、哈希清单和不可覆盖行为离线回归。"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from create_release_package import create_release_package  # noqa: E402


passed = 0


def check(name: str, condition: bool) -> None:
    global passed
    if not condition:
        raise AssertionError(name)
    passed += 1
    print(f"  PASS  {name}")


tmp = Path(tempfile.mkdtemp(prefix="mta-release-package-"))
try:
    root = tmp / "project"
    (root / "src").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / "config").mkdir()
    (root / "docs").mkdir()
    (root / "data").mkdir()
    (root / ".venv").mkdir()
    (root / "src" / "app.py").write_text("print('safe')", encoding="utf-8")
    (root / "scripts" / "run_web.py").write_text("print('safe entry')", encoding="utf-8")
    (root / "scripts" / "diag_local.py").write_text("print('temporary')", encoding="utf-8")
    (root / "config" / "settings.yaml").write_text("execution: {}", encoding="utf-8")
    (root / "config" / "windows_service.json").write_text("{}", encoding="utf-8")
    (root / "docs" / "项目说明书.md").write_text("safe docs", encoding="utf-8")
    (root / "README.md").write_text("safe readme", encoding="utf-8")
    (root / "data" / "secret.log").write_text("must not package", encoding="utf-8")
    (root / ".env").write_text("API_SECRET=must not package", encoding="utf-8")
    manifest = root / "data" / "release_manifest.json"
    manifest.write_text(json.dumps({
        "manifest_schema": 1, "release_id": "rel-test-1", "git_commit": "abc",
    }), encoding="utf-8")

    output = tmp / "dist" / "release.zip"
    result = create_release_package(root, output, manifest_path=manifest)
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        package_manifest = json.loads(archive.read("package_manifest.json"))
    check("发布包已原子生成并返回自身哈希", output.is_file() and len(result["package_sha256"]) == 64)
    check("源码/入口/安全配置进入发布包", {
        "src/app.py", "scripts/run_web.py", "config/settings.yaml", "README.md",
        "release_manifest.json", "package_manifest.json",
    }.issubset(names))
    check("data、env和临时诊断不进入发布包", not any(
        name.startswith(("data/", ".env")) or "diag_local.py" in name for name in names
    ))
    check("包清单包含文件哈希和排除说明", bool(package_manifest["files"])
          and "data/" in package_manifest["excluded"])

    try:
        create_release_package(root, output, manifest_path=manifest)
        overwrite_blocked = False
    except FileExistsError:
        overwrite_blocked = True
    check("已有发布包拒绝覆盖", overwrite_blocked)

    bad_manifest = root / "data" / "bad_manifest.json"
    bad_manifest.write_text(json.dumps({
        "release_id": "rel-test-2", "api_secret": "secret-value",
    }), encoding="utf-8")
    try:
        create_release_package(root, tmp / "dist" / "bad.zip", manifest_path=bad_manifest)
        secret_blocked = False
    except ValueError:
        secret_blocked = True
    check("疑似凭据manifest被拒绝", secret_blocked)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print(f"\n结果: {passed} 通过 / 0 失败")
