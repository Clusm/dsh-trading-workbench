import test from 'node:test'
import assert from 'node:assert/strict'
import { StrategyAssistant, eligibleStrategies } from '../host/strategies.js'

const rules={symbols:['BTCUSDT'],timeframes:['15m'],entry:{kind:'mark_threshold',side:'long',operator:'gt',value:100}}
const candidate=id=>({strategy_id:id,name:id,current_version:1,generation:2,enabled:true,confirmed_version:{rules,validation:{execution_ready:true}}})
const llm = value => ({async *stream() {yield {type:'text-delta',text:JSON.stringify(value)};yield {type:'finish',reason:{kind:'stop'}}}})
const route={provider:'fake',model:'test'}
test('candidate prefilter rejects stale/unknown prices and inactive/unsupported strategies',()=>{
  const now=Date.now(),q={BTCUSDT:{mark:101,as_of_ms:now,stale:false}}
  assert.equal(eligibleStrategies([candidate('a')],q,now).length,1)
  assert.equal(eligibleStrategies([{...candidate('a'),enabled:false}],q,now).length,0)
  assert.equal(eligibleStrategies([candidate('a')],{BTCUSDT:{...q.BTCUSDT,stale:true}},now).length,0)
  assert.equal(eligibleStrategies([candidate('a')],q,now+6000).length,0)
  assert.equal(eligibleStrategies([candidate('a')],{BTCUSDT:{mark:99,as_of_ms:now,stale:false}},now).length,0)
})
test('no candidate means no model call; selection cannot escape authorized set',async()=>{
  const assistant=new StrategyAssistant({llm:llm({strategy_id:'evil',reason:'x'}),route})
  assert.equal((await assistant.choose([],{})).strategy,null)
  assert.equal((await assistant.choose([candidate('a')],{})).strategy.strategy_id,'a')
  await assert.rejects(assistant.choose([candidate('a'),candidate('b')],{}),e=>e.code==='E_STRATEGY_UNAUTHORIZED_SELECTION')
})
test('clarification preserves original/answers and writes only against analyzed revision',async()=>{
  const draft={revision:3,source_text:'用户原文',answers:{q:'回答'},questions:[]}
  const calls=[]
  const client={async strategy(path,opts){calls.push({path,opts});if(!opts)return {strategy:{draft}};return {strategy:{draft:opts.body}}}}
  const assistant=new StrategyAssistant({llm:llm({summary:'理解',rules,questions:[],suggestions:[],sources:{entry:'原文'}}),route})
  const result=await assistant.analyze(client,'a',3)
  assert.equal(result.strategy.draft.source_text,'用户原文')
  assert.equal(result.strategy.draft.answers.q,'回答')
  assert.equal(calls[1].opts.body.revision,3)
  await assert.rejects(assistant.analyze(client,'a',2),e=>e.status===409)
})
test('malformed model output does not overwrite draft and concurrency gate clears on failure',async()=>{
  let writes=0
  const client={async strategy(path,opts){if(opts){writes++;return {}}return {strategy:{draft:{revision:1,source_text:'x',answers:{}}}}}}
  const assistant=new StrategyAssistant({llm:llm({summary:'x',questions:[]}),route})
  await assert.rejects(assistant.analyze(client,'a',1))
  assert.equal(writes,0);assert.equal(assistant.busy.size,0)
})
