import test from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'

const source=readFileSync(new URL('../client.js',import.meta.url),'utf8')
const start=source.indexOf('    function useMarkQuotes(')
const end=source.indexOf('    // ---- B+ editorial cockpit',start)
function harness(fetch) {
  let result, effect, cleanup, id=0
  const timers=new Map(), listeners=new Map()
  const document={visibilityState:'visible',addEventListener(k,f){listeners.set(k,f)},removeEventListener(k){listeners.delete(k)}}
  const React={useState(initial){result ??= initial;return [result,next=>{result=typeof next==='function'?next(result):next}]},useEffect(fn){effect=fn}}
  const ctx=vm.createContext({React,fetch,document,AbortController,Date,Object,Number,Math,Error,MARK_QUOTE_POLL_MS:2000,
    setTimeout(fn,ms){timers.set(++id,{fn,ms});return id},clearTimeout(key){timers.delete(key)}})
  vm.runInContext(source.slice(start,end)+';this.hook=useMarkQuotes',ctx)
  ctx.hook('/quotes?scope=a');cleanup=effect()
  return {get result(){return result},timers,document,listeners,close(){cleanup()},async tick(ms){const item=[...timers.entries()].find(([,v])=>v.ms===ms);assert.ok(item,`timer ${ms}`);timers.delete(item[0]);await item[1].fn()},visibility(value){document.visibilityState=value;listeners.get('visibilitychange')?.()}}
}
const flush=()=>new Promise(resolve=>setImmediate(resolve))
const quote=(time=100)=>({ok:true,json:async()=>({prices:{BTCUSDT:{mark:1,as_of_ms:time,stale:false}}})})
test('account rerenders cannot restart quote timer, single flight and visibility recovery',async()=>{
  let resolve,calls=0
  const h=harness(()=>{calls++;return new Promise(r=>{resolve=r})})
  try {
    h.visibility('hidden');h.visibility('visible');assert.equal(calls,1)
    resolve(quote());await flush()
    assert.equal(h.result.data.prices.BTCUSDT.mark,1)
    h.visibility('hidden');assert.equal(h.timers.size,0);assert.equal(calls,1)
    h.visibility('visible');assert.equal(calls,2)
    resolve(quote(200));await flush()
    assert.equal(h.result.data.prices.BTCUSDT.as_of_ms,200)
  } finally{h.close()}
  assert.equal(h.timers.size,0);assert.equal(h.listeners.size,0)
})
test('failure backs off without destroying evidence, rejects older quotes',async()=>{
  let n=0
  const h=harness(async()=>{n++;if(n===2) throw new Error('offline');return quote(n===3?50:100)})
  try {
    await flush();await h.tick(2000)
    assert.equal(h.result.error,'offline');assert.equal(h.result.data.prices.BTCUSDT.as_of_ms,100)
    await h.tick(4000)
    assert.equal(h.result.data.prices.BTCUSDT.as_of_ms,100)
    assert.equal(h.result.data.prices.BTCUSDT.stale,true)
  }finally{h.close()}
})
test('unmount aborts in-flight request and suppresses late state updates',async()=>{
  let resolve,signal
  const h=harness((url,opts)=>{signal=opts.signal;return new Promise(r=>{resolve=r})})
  h.close();assert.equal(signal.aborted,true)
  resolve(quote());await flush()
  assert.equal(h.result.data,null);assert.equal(h.timers.size,0)
})

