"""按真实顺序执行页面脚本，捕获「调用时函数尚未定义」这类错误。

为什么要这个工具
----------------
浏览器里多个 <script> 的顶层 const/let/function 共享同一个全局词法环境，
所以后一个脚本能用到前一个脚本定义的 FMT、apiFetch、smartInterval。
但如果页面脚本被插到了公共脚本**之前**（Jinja2 把页面内容塞进 content 块，
而公共脚本在 content 块之后），页面脚本执行时这些函数还不存在 =>
ReferenceError => 整个页面脚本中断 => 轮询永远不启动。
这类错误还常被空的 catch(e){} 吞掉，唯一外在表现就是"只能手动刷新才更新"。

必须用 vm.runInContext 而不是 new Function：后者每次创建独立作用域，
顶层 const 不会泄漏到下一次调用，与浏览器行为不符，会误报。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request

NODE = r"C:\Users\Tao\.workbuddy-ai\binaries\node\versions\22.22.2-1\node.exe"

RUNNER = r"""
const vm = require('vm');
const realSetTimeout = global.setTimeout;

function makeStub(name){
  const f = function(){ return makeStub(name + '()'); };
  return new Proxy(f, {
    get(t, k){
      if (k === Symbol.toPrimitive) return () => 0;
      if (k === Symbol.iterator) return [][Symbol.iterator].bind([]);
      if (k === 'toString') return () => '';
      if (k === 'length') return 0;
      return makeStub(name + '.' + String(k));
    },
    set(){ return true; },
    apply(){ return makeStub(name + '()'); },
    has(){ return true; }
  });
}

const logs = [];
const sandboxConsole = {
  log:   (...a) => logs.push('console.log   ' + a.join(' ')),
  warn:  (...a) => logs.push('console.warn  ' + a.join(' ')),
  error: (...a) => logs.push('console.error ' + a.join(' ')),
};

const documentStub = new Proxy(
  { hidden: false, documentElement: makeStub('html'), body: makeStub('body') },
  {
    get(t, k){ return (k in t) ? t[k] : makeStub('document.' + String(k)); },
    set(t, k, v){ t[k] = v; return true; },
    has(){ return true; }
  }
);

/* 页面脚本会用到 window.addEventListener / localStorage 等，这里预设真实实现，
   其余属性访问回落到万能 stub（Proxy），避免 stub 不完整产生假报错。 */
const windowStub = new Proxy(
  {
    matchMedia: () => ({ matches: false }),
    scrollTo: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    document: documentStub,
    location: { reload: () => {}, href: '' },
  },
  {
    get(t, k){ return (k in t) ? t[k] : makeStub('window.' + String(k)); },
    set(t, k, v){ t[k] = v; return true; },
    has(){ return true; }
  }
);

/* fetch 返回一份字段较全的假数据：否则页面脚本读 undefined.xxx 会刷屏，
   真正的「未定义函数」错误反而被噪音淹没。 */
const MOCK = {
  ok: true, status: 200,
  session_equity: 0, positions: [], unrealized_pnl: 0,
  control_state: 'normal', broker_mode: 'paper',
  stream: { active: false, text: 'mock' },
  latest: {}, capital: {}, quotas: {}, channels: [],
  macro: null, onchain: null, strategy_decision: null, ai_decision: null,
  events: [], rows: [], snapshots: [], agents: [],
};

let timers = 0;
const sandbox = {
  console: sandboxConsole,
  document: documentStub,
  window: windowStub,
  localStorage: { getItem: () => null, setItem: () => {} },
  performance: { now: () => Date.now() },
  requestAnimationFrame: () => 0,
  cancelAnimationFrame: () => {},
  fetch: async () => ({ ok: true, status: 200, json: async () => MOCK }),
  setInterval: () => ++timers,
  setTimeout: () => ++timers,
  clearInterval: () => {},
  clearTimeout: () => {},
  location: { reload: () => {}, href: '' },
  navigator: { userAgent: 'node' },
  Blob: function(){},
  URL: { createObjectURL: () => '', revokeObjectURL: () => {} },
  matchMedia: () => ({ matches: false }),
};
sandbox.globalThis = sandbox;
sandbox.window.document = documentStub;
const ctx = vm.createContext(sandbox);

const errs = [];
const chunks = __CHUNKS__;
const names  = __NAMES__;
chunks.forEach((code, i) => {
  try {
    vm.runInContext(code, ctx, { filename: names[i] });
  } catch (e) {
    errs.push('[' + names[i] + '] ' + e.name + ': ' + e.message);
  }
});

realSetTimeout(() => {
  if (errs.length) {
    console.log('脚本执行中断:');
    errs.forEach(e => console.log('  x ' + e));
  }
  if (logs.length) {
    console.log('页面脚本主动上报:');
    logs.slice(0, 10).forEach(l => console.log('  ! ' + l));
  }
  if (!errs.length && !logs.length) {
    console.log('  ok 全部脚本按序执行完毕，无未定义引用');
  }
  process.exit(errs.length ? 1 : 0);
}, 400);
"""


def check(path: str, reversed_order: bool = False) -> int:
    url = "http://127.0.0.1:8000" + path
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    html = opener.open(url, timeout=25).read().decode("utf-8", "ignore")
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    if reversed_order and len(scripts) >= 3:
        # 自证工具有效：还原成"页面脚本排在公共脚本之前"的错误顺序，应当报错
        scripts = [scripts[0], *scripts[2:], scripts[1]]
    print(f"{url}  ({len(scripts)} 段脚本){'  [已还原为错误顺序]' if reversed_order else ''}")

    names = ["head" if i == 0 else ("base" if i == 1 else f"page#{i - 1}") for i in range(len(scripts))]
    payload = RUNNER.replace("__CHUNKS__", json.dumps(scripts)).replace("__NAMES__", json.dumps(names))

    tmp = "scripts/_js_check_run.js"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)

    try:
        r = subprocess.run([NODE, tmp], capture_output=True, text=True, timeout=90)
        print((r.stdout or r.stderr).strip())
        return r.returncode
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--reversed"]
    bad = "--reversed" in sys.argv
    # 顺序与 base.html 导航一致；/trades 已改名 /orders，不再单独检查
    paths = args or ["/", "/decisions", "/orders", "/events", "/reviews", "/agents", "/channels"]
    code = 0
    for p in paths:
        hit = check(p, reversed_order=bad)
        print()
        # 反向验证时期望报错：没报错说明这个检查工具本身失效了
        code |= (0 if bad else hit)
    sys.exit(code)
