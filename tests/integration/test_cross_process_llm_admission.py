"""U05/S04 跨进程 LLM 准入（脱敏别名共享、互斥、崩溃回收）离线回归。

独立可运行（本项目无 pytest）：全部通过退出码 0，任一失败退出码 1。
全程离线：不联网、不读真实 ``.env``/``data/``、不构造 Broker、不启动服务。
跨进程用例用 ``sys.executable`` 起**本仓自己的**子进程，并把
``MTA_LLM_COORDINATION_DIR`` 指向 ``tempfile`` 沙箱，因此不会碰真实
``data/shared/``，也不会与其它进程互相干扰。

**双态可运行**：
- 租约/别名/回收的**行为断言**落在 U05 独占模块 ``src/core/decision_trace.py``；
- 对共享适配 ``src/llm/request_coordinator.py`` 只断言 HEAD 已有、且改后必须保持的
  公开语义（准入互斥、超时类型化、异常退出后锁可被重新获取、作用域脱敏）。
  不读文件内容判定补丁状态。

预期值来源：任务卡 U05/S04「按 provider/脱敏凭据别名共享准入、崩溃回收」、
K01 §3（脱敏/不做值扫描的边界）、request_coordinator 的 Windows 字节区间锁注释。
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from core.decision_trace import (  # noqa: E402
    LeaseRegistry,
    coordination_dir,
    credential_alias,
    lock_path_for,
    owner_alive,
    shared_admission_scope,
    shared_quota_lease,
)
from llm.request_coordinator import (  # noqa: E402
    LLMAdmissionTimeout,
    LLMRequestCoordinator,
    get_default_coordinator,
    provider_scope,
)

PASS = 0
FAIL = 0
FAILURES: list[str] = []

FAKE_KEY = "cross" + "-process-" + "fixture"
FAKE_KEY_2 = "cross" + "-process-" + "fixture-2"


def check(name: str, condition: bool, detail: object = "") -> bool:
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        FAILURES.append(name)
        print(f"  FAIL  {name} :: {detail}")
    return bool(condition)


def _raises(exc_type: type[BaseException], func):
    try:
        func()
    except exc_type as exc:
        return exc
    except BaseException as exc:  # noqa: BLE001
        print(f"    (unexpected {type(exc).__name__}: {exc})")
        return None
    return None


@contextlib.contextmanager
def _point_at(sandbox: Path):
    """把**本进程**与子进程的协调目录都指向沙箱。

    缺了这一步，``request_coordinator`` 会回落到真实的
    ``data/shared/llm_coordination``——测试就会写到运行目录里，
    并且跨进程互斥断言会因"父进程用了另一个锁文件"而假通过。
    """
    saved = os.environ.get("MTA_LLM_COORDINATION_DIR")
    os.environ["MTA_LLM_COORDINATION_DIR"] = str(sandbox)
    try:
        yield sandbox
    finally:
        if saved is None:
            os.environ.pop("MTA_LLM_COORDINATION_DIR", None)
        else:
            os.environ["MTA_LLM_COORDINATION_DIR"] = saved


def _child_env(sandbox: Path) -> dict[str, str]:
    env = os.environ.copy()
    env["MTA_LLM_COORDINATION_DIR"] = str(sandbox)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["MTA_NO_PROXY"] = "1"
    for name in tuple(env):
        upper = name.upper()
        if ("SENSENOVA" in upper or "OPENCODE" in upper or "WORKBUDDY" in upper) \
                and ("KEY" in upper or "TOKEN" in upper or "SECRET" in upper):
            env.pop(name, None)
    return env


HOLDER_SCRIPT = """
import os, sys, time
sys.path.insert(0, {src!r})
from llm.request_coordinator import LLMRequestCoordinator

scope = {scope!r}
mode = {mode!r}
coordinator = LLMRequestCoordinator(max_concurrency=1, queue_poll_seconds=0.02)
with coordinator.admit(scope, purpose="decision", deadline=time.monotonic() + 30):
    print("HOLDING", flush=True)
    if mode == "abrupt":
        os._exit(7)          # 模拟崩溃：不执行任何 finally/清理
    time.sleep(float(sys.argv[1]))
print("RELEASED", flush=True)
"""


def _run_holder(sandbox: Path, scope: str, mode: str, hold_seconds: float = 1.0):
    return subprocess.Popen(
        [sys.executable, "-c",
         HOLDER_SCRIPT.format(src=str(ROOT / "src"), scope=scope, mode=mode),
         str(hold_seconds)],
        cwd=str(ROOT), env=_child_env(sandbox),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        encoding="utf-8", errors="replace",
    )


# ---------------------------------------------------------------- [1] 别名


def test_desensitised_alias() -> None:
    print("\n[1] 准入按 provider + 脱敏凭据别名共享（不落原文）")
    scope = shared_admission_scope("https://provider.invalid/v1", FAKE_KEY)
    check("作用域不含凭据原文", FAKE_KEY not in scope and FAKE_KEY_2 not in scope, scope)
    check("别名是不可逆前缀哈希", credential_alias(FAKE_KEY) in scope
          and len(credential_alias(FAKE_KEY)) == 16)
    check("同 provider 同凭据 → 同作用域",
          shared_admission_scope("https://provider.invalid/v1", FAKE_KEY) == scope)
    check("同 provider 不同凭据 → 不同作用域（不误共享额度）",
          shared_admission_scope("https://provider.invalid/v1", FAKE_KEY_2) != scope)
    check("不同 provider → 不同作用域",
          shared_admission_scope("https://other.invalid/v1", FAKE_KEY) != scope)
    check("base_url 尾斜杠不改变作用域",
          shared_admission_scope("https://provider.invalid/v1/", FAKE_KEY) == scope)
    check("与 request_coordinator 的 provider_scope 完全同口径",
          provider_scope("https://provider.invalid/v1", FAKE_KEY) == scope)
    check("空凭据也是稳定的别名（不抛错、不落空串）",
          credential_alias(None) == credential_alias("") != "")

    # 脱敏作用域的锁文件名同样不落原文
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["MTA_LLM_COORDINATION_DIR"] = tmp
        try:
            check("协调目录可用环境变量重定向（测试沙箱）",
                  str(coordination_dir()) == tmp, str(coordination_dir()))
            path = lock_path_for(scope)
            check("锁文件名只含作用域摘要，不含凭据",
                  FAKE_KEY not in path.name and path.parent == Path(tmp), path.name)
            check("同一作用域 → 同一锁文件",
                  lock_path_for(scope) == path)
        finally:
            os.environ.pop("MTA_LLM_COORDINATION_DIR", None)


# ---------------------------------------------------------------- [2] 跨进程互斥


def test_cross_process_mutual_exclusion() -> None:
    print("\n[2] 跨进程互斥：另一个进程持锁时本进程必须排队/超时，不得并行进入")
    with tempfile.TemporaryDirectory() as tmp, _point_at(Path(tmp)):
        sandbox = Path(tmp)
        scope = shared_admission_scope("https://provider.invalid/v1", FAKE_KEY)
        child = _run_holder(sandbox, scope, mode="graceful", hold_seconds=1.2)
        try:
            line = child.stdout.readline().strip()
            check("子进程已持锁（HOLDING）", line == "HOLDING", line)
            coordinator = LLMRequestCoordinator(max_concurrency=1, queue_poll_seconds=0.02)
            started = time.monotonic()
            timeout = _raises(
                LLMAdmissionTimeout,
                lambda: coordinator.admit(scope, purpose="intel",
                                          deadline=time.monotonic() + 0.4).__enter__(),
            )
            waited = time.monotonic() - started
            check("持锁期间本进程拿不到准入（LLMAdmissionTimeout）",
                  isinstance(timeout, LLMAdmissionTimeout), type(timeout).__name__)
            check("排队确实发生过（等待接近给定 deadline 而非立即失败）",
                  waited >= 0.3, waited)
            child.wait(timeout=20)
            check("子进程正常释放（RELEASED）",
                  "RELEASED" in (child.stdout.read() or ""), "no RELEASED")
            with coordinator.admit(scope, purpose="intel",
                                   deadline=time.monotonic() + 5):
                acquired = True
            check("释放后立即可再次准入（锁未泄漏）", acquired)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


# ---------------------------------------------------------------- [3] 崩溃回收


def test_crash_recovery_after_abrupt_exit() -> None:
    print("\n[3] 崩溃回收：持有者异常退出后，锁由 OS 释放（无需等到 deadline）")
    with tempfile.TemporaryDirectory() as tmp, _point_at(Path(tmp)):
        sandbox = Path(tmp)
        scope = shared_admission_scope("https://provider.invalid/v1", FAKE_KEY)
        child = _run_holder(sandbox, scope, mode="abrupt")
        try:
            check("子进程持锁后以 os._exit 崩溃（跳过所有清理）",
                  child.stdout.readline().strip() == "HOLDING")
            # 崩溃仍在进行中或刚发生；确保进程确实已退出（exit=7）
            child.wait(timeout=20)
            check("崩溃退出码符合预期（finally 未执行）", child.returncode == 7,
                  child.returncode)
            coordinator = LLMRequestCoordinator(max_concurrency=1, queue_poll_seconds=0.02)
            started = time.monotonic()
            with coordinator.admit(scope, purpose="decision",
                                   deadline=time.monotonic() + 3):
                acquired = True
            elapsed = time.monotonic() - started
            check("崩溃后新进程立即获得准入（不需要等 deadline/超时）",
                  acquired and elapsed < 1.0, elapsed)
            check("崩溃不残留死锁文件占用（锁文件本身可复用）",
                  lock_path_for(scope).exists(), str(lock_path_for(scope)))
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)


# ---------------------------------------------------------------- [4] 租约回收


def test_lease_registry_reaps_dead_owners() -> None:
    print("\n[4] 租约登记与回收：死属主/过期立即回收，活属主不误回收")
    with tempfile.TemporaryDirectory() as tmp, _point_at(Path(tmp)):
        registry = LeaseRegistry(directory=Path(tmp), ttl_ms=200)
        scope = shared_admission_scope("https://provider.invalid/v1", FAKE_KEY)

        # 活着的属主（本进程）不得被回收
        live = registry.acquire(scope)
        check("登记租约附带 pid 与令牌",
              live.pid == os.getpid() and bool(live.owner_token), live.as_dict())
        check("status 是只读视图（不因读而回收）",
              registry.read(scope) is not None
              and registry.status(scope)["owner_alive"] is True,
              registry.status(scope))
        check("活属主的租约不被回收", registry.reap(scope=scope) == (),
              registry.status(scope))
        check("令牌不匹配时不得删除他人租约",
              registry.release(scope, owner_token="someone-else") is False
              and registry.read(scope) is not None)
        check("令牌匹配可释放", registry.release(scope, owner_token=live.owner_token) is True
              and registry.read(scope) is None)

        # 死属主（真实子进程，持有租约后被杀）→ 立即回收
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; print('up', flush=True); time.sleep(60)"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", env=_child_env(Path(tmp)),
        )
        try:
            child.stdout.readline()
            dead = registry.acquire(scope, pid=child.pid, owner_token="child-token")
            check("租约可登记在另一个进程名下", dead.pid == child.pid)
            check("属主存活时 owner_alive=True", owner_alive(child.pid) is True)
            child.kill()
            child.wait(timeout=10)
            for _ in range(50):  # 等 OS 真正回收进程句柄
                if not owner_alive(child.pid):
                    break
                time.sleep(0.05)
            check("属主被杀后 owner_alive=False", owner_alive(child.pid) is False)
            reaped = registry.reap(scope=scope)
            check("死属主的租约被回收（崩溃即回收，不等 TTL）",
                  len(reaped) == 1 and reaped[0].pid == child.pid,
                  [item.as_dict() for item in reaped])
            check("回收后登记表为空", registry.read(scope) is None)
        finally:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=10)

        # 过期租约（活属主但已超 TTL）同样被回收：有界，不会永久占用
        registry.acquire(scope, owner_token="stale", now_ms=1_000)
        reaped = registry.reap(scope=scope, now_ms=1_000 + 201)
        check("过期租约（属主仍活）也被回收（卡死有界）",
              len(reaped) == 1 and reaped[0].owner_token == "stale",
              [item.as_dict() for item in reaped])

        # 共享租约上下文管理器：异常也释放
        try:
            with shared_quota_lease(scope, registry=registry):
                check("上下文内租约可读", registry.read(scope) is not None)
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        check("异常退出后租约被释放（不泄漏）", registry.read(scope) is None)

        check("坏 JSON 的 sidecar 不导致崩溃",
              (Path(tmp) / "lease-broken.json").write_text("{not json", encoding="utf-8")
              is not None and isinstance(registry.reap(), tuple))
        check("status 全量视图可序列化",
              isinstance(json.loads(json.dumps(registry.status())), dict))


# ---------------------------------------------------------------- [5] 真并发


CONCURRENT_SCRIPT = """
import json, os, sys, time
sys.path.insert(0, {src!r})
from llm.request_coordinator import LLMRequestCoordinator

scope = {scope!r}
marker = {marker!r}
tag = sys.argv[1]
coordinator = LLMRequestCoordinator(max_concurrency=1, queue_poll_seconds=0.01)
for index in range(4):
    with coordinator.admit(scope, purpose="decision", deadline=time.monotonic() + 30):
        state = json.loads(open(marker, encoding="utf-8").read())
        if state["holder"] not in (None, tag):
            print("OVERLAP", state["holder"], tag, flush=True)
            sys.exit(3)
        state["holder"] = tag
        state["seq"] = state.get("seq", 0) + 1
        open(marker, "w", encoding="utf-8").write(json.dumps(state))
        time.sleep(0.03)
        again = json.loads(open(marker, encoding="utf-8").read())
        if again["holder"] != tag:
            print("OVERLAP-LATE", again["holder"], tag, flush=True)
            sys.exit(3)
        state["holder"] = None
        open(marker, "w", encoding="utf-8").write(json.dumps(state))
print("DONE", flush=True)
"""


def test_two_processes_never_overlap() -> None:
    print("\n[5] 真并发：两个进程各做 4 轮，临界区内从不重叠")
    with tempfile.TemporaryDirectory() as tmp, _point_at(Path(tmp)):
        sandbox = Path(tmp)
        marker = sandbox / "marker.json"
        marker.write_text(json.dumps({"holder": None, "seq": 0}), encoding="utf-8")
        scope = shared_admission_scope("https://provider.invalid/v1", FAKE_KEY)
        children = [
            subprocess.Popen(
                [sys.executable, "-c", CONCURRENT_SCRIPT.format(
                    src=str(ROOT / "src"), scope=scope, marker=str(marker)), tag],
                cwd=str(ROOT), env=_child_env(sandbox), stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
            )
            for tag in ("proc-a", "proc-b")
        ]
        outputs = []
        for child in children:
            out, err = child.communicate(timeout=60)
            outputs.append((child.returncode, out.strip(), err.strip()[-300:]))
        check("两个进程都成功完成 4 轮准入",
              all(code == 0 and "DONE" in out for code, out, _ in outputs), outputs)
        state = json.loads(marker.read_text(encoding="utf-8"))
        check("临界区共被进入 8 次（无丢失、无重叠）", state["seq"] == 8, state)
        check("结束后无残留持有者", state["holder"] is None, state)


# ---------------------------------------------------------------- [6] 类型与状态


def test_admission_surface() -> None:
    print("\n[6] 准入接口可观测性（状态、类型化超时、默认协调器）")
    with tempfile.TemporaryDirectory() as tmp, _point_at(Path(tmp)):
        scope = shared_admission_scope("https://provider.invalid/v1", FAKE_KEY)
        coordinator = LLMRequestCoordinator(max_concurrency=2, queue_poll_seconds=0.01,
                                            default_queue_deadline_seconds=2.0)
        with coordinator.admit(scope, purpose="decision", priority="critical"):
            pass
        with coordinator.admit(scope, purpose="intel", priority="background"):
            pass
        status = coordinator.status(scope)
        provider = status["providers"][0]
        check("状态按作用域可查且键为脱敏作用域",
              provider["scope"] == scope and FAKE_KEY not in json.dumps(status),
              sorted(provider))
        check("准入/完成计数与本层 purpose 分类可见",
              provider["admitted"] == 2 and provider["completed"] == 2
              and provider["purposes"] == {"decision": 1, "intel": 1}, provider)
        check("并发上限被如实上报", provider["max_concurrency"] == 2, provider)
        check("冷却字段存在且初始为 0",
              provider["cooldown_remaining_seconds"] == 0.0, provider)
        coordinator.mark_cooldown(scope, 0.2, reason="HTTP 429")
        check("429 冷却可登记（限流事实不靠猜）",
              coordinator.status(scope)["providers"][0][
                  "cooldown_remaining_seconds"] > 0)
        check("默认协调器是进程内单例（同凭据跨 Agent 共享）",
              get_default_coordinator() is get_default_coordinator())
        check("未配置的作用域不凭空出现在状态里",
              coordinator.status("never-used#key:0000")["providers"] == [])


def main() -> int:
    print("=" * 72)
    print("U05/S04 跨进程 LLM 准入离线回归（独立脚本，无 pytest）")
    print("=" * 72)
    test_desensitised_alias()
    test_cross_process_mutual_exclusion()
    test_crash_recovery_after_abrupt_exit()
    test_lease_registry_reaps_dead_owners()
    test_two_processes_never_overlap()
    test_admission_surface()
    print("\n" + "=" * 72)
    print(f"结果：PASS {PASS} / FAIL {FAIL}")
    if FAILURES:
        print("失败项：")
        for name in FAILURES:
            print(f"  - {name}")
    print("=" * 72)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
