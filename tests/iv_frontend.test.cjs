const test = require('node:test');
const assert = require('node:assert/strict');
const {stats, symlog, windowSamples} = require('../src/fluke8845a/web/iv_math.js');
test('window statistics use valid raw samples and population variance', () => {
  assert.deepEqual(stats([1, 2, 3]), {mean: 2, variance: 2/3, std: Math.sqrt(2/3), median: 2});
  assert.equal(stats([]), null);
});
test('window excludes old and invalid samples', () => {
  assert.deepEqual(windowSamples([{t:0,value:1},{t:3,value:null},{t:4,value:2},{t:6,value:3}], 6, 3), [{t:4,value:2},{t:6,value:3}]);
});
test('signed log preserves zero and direction', () => {
  assert.equal(symlog(0, 1), 0);
  assert.equal(symlog(-10, 1), -symlog(10, 1));
});

test('current display switches units at 0.5 mA without changing raw amperes', () => {
  const {currentUnit} = require('../src/fluke8845a/web/iv_math.js');
  assert.deepEqual(currentUnit([]), {label:'µA', factor:1e6});
  assert.deepEqual(currentUnit([-0.000499]), {label:'µA', factor:1e6});
  assert.deepEqual(currentUnit([-0.0005]), {label:'mA', factor:1e3});
});

test('opening a measurement replaces the session restored after reload', async () => {
  const fs = require('node:fs');
  const vm = require('node:vm');
  const elements = new Map();
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      value:'', textContent:'', files:[], hidden:false, disabled:false,
      addEventListener(){}, replaceChildren(){}, append(){}, setAttribute(){},
      getBoundingClientRect(){return {width:0,height:0}},
      getContext(){return {scale(){}}},
    });
    return elements.get(id);
  }
  let saved = {points:[{voltage:9,current:9}], samples:[], voltage:9, boundary:null, dirty:false, ignoreLatest:true};
  const db = {
    transaction(){ const tx={objectStore(){return {
      put(value){saved=structuredClone(value);setImmediate(()=>tx.oncomplete());},
      get(){const req={};setImmediate(()=>{req.result=structuredClone(saved);req.onsuccess();});return req;},
    }}};return tx; }, close(){},
  };
  const indexedDB = {open(){const req={result:db};setImmediate(()=>req.onsuccess());return req;}};
  const context = vm.createContext({
    document:{getElementById:element,createElement:()=>({addEventListener(){},setAttribute(){},append(){}})},
    window:{addEventListener(){}}, indexedDB, localStorage:{getItem(){return null}},
    fetch:()=>new Promise(()=>{}), setTimeout(){}, console, devicePixelRatio:1, IVMath:require('../src/fluke8845a/web/iv_math.js'),
  });
  vm.runInContext(fs.readFileSync(require('node:path').join(__dirname,'../src/fluke8845a/web/iv.js'),'utf8'),context);
  await new Promise(resolve=>setImmediate(()=>setImmediate(resolve)));
  element('file').files=[{text:async()=>JSON.stringify({format:'fluke-iv',points:[{voltage:2,current:0.000001}],samples:[],voltage:2,boundary:null,interval_s:3,window_s:120,settings:{nplc:10,autorange:true}})}];
  await element('file').onchange();
  vm.runInContext('model.points=[];model.voltage=null',context);
  element('interval').value='1'; element('window').value='60'; element('nplc').value='1';
  await vm.runInContext('restore()',context);
  assert.equal(vm.runInContext('model.points[0].voltage',context),2);
  assert.equal(vm.runInContext('model.voltage',context),2);
  assert.equal(Number(element('interval').value),3);
  assert.equal(Number(element('window').value),120);
  assert.equal(Number(element('nplc').value),10);
});
const fs2 = require('node:fs');
const vm2 = require('node:vm');
function ivHarness() {
  const elements=new Map();
  function el(id){if(!elements.has(id))elements.set(id,{value:'',textContent:'',files:[],hidden:false,disabled:false,checked:true,addEventListener(){},replaceChildren(){},append(){},setAttribute(){},getBoundingClientRect(){return {width:0,height:0}},getContext(){return {scale(){}}}});return elements.get(id);}
  let saved=null, responder=()=>new Promise(()=>{});
  const db={transaction(){const tx={objectStore(){return {put(value){saved=structuredClone(value);setImmediate(()=>tx.oncomplete());},get(){const req={};setImmediate(()=>{req.result=structuredClone(saved);req.onsuccess();});return req;}}}};return tx;},close(){}};
  const context=vm2.createContext({document:{getElementById:el,createElement:()=>({addEventListener(){},setAttribute(){},append(){}})},window:{addEventListener(){}},indexedDB:{open(){const req={result:db};setImmediate(()=>req.onsuccess());return req;}},localStorage:{getItem(){return null}},fetch:(...args)=>responder(...args),setTimeout(){},console,devicePixelRatio:1,IVMath:require('../src/fluke8845a/web/iv_math.js'),URLSearchParams});
  vm2.runInContext(fs2.readFileSync(require('node:path').join(__dirname,'../src/fluke8845a/web/iv.js'),'utf8'),context);
  return {el,context,setFetch(fn){responder=fn;}};
}
test('poll after opening a file does not reintroduce the previous instrument run', async()=>{
  const h=ivHarness();await new Promise(r=>setImmediate(()=>setImmediate(r)));
  vm2.runInContext("model.ignoreLatest=true;model.voltage=2;model.boundary=null;model.samples=[];model.generation=4;model.last=0",h.context);
  h.setFetch(async()=>({json:async()=>({generation:4,connected:true,last_experiment_dir:'old',sample_buffer_start:1,samples:[{sample_index:1,elapsed_s:1,value_si:0.5,status:'ok'}],iv_voltage_boundary:{generation:4,sample_index:0,elapsed_s:0,voltage_v:9}})}));
  await vm2.runInContext('poll()',h.context);
  assert.equal(vm2.runInContext('model.samples.length',h.context),0);
  assert.equal(vm2.runInContext('model.voltage',h.context),2);
  assert.equal(vm2.runInContext('model.boundary',h.context),null);
});
test('restart recovers the prior stopped run before creating a new run', async()=>{
  const h=ivHarness();await new Promise(r=>setImmediate(()=>setImmediate(r)));
  h.el('interval').value='1';h.el('range').value='auto';h.el('nplc').value='1';
  vm2.runInContext("model.state={connected:true,simulation:true,last_experiment_dir:'old'};model.generation=4;model.voltage=2;model.ignoreLatest=false",h.context);
  const actions=[];
  h.context.actions=actions;
  vm2.runInContext("recoverRaw=async()=>{actions.push('recover');model.samples.push({source:'old',index:1})};command=async action=>{actions.push(action);if(action==='iv_voltage')return {generation:5,sample_index:0,elapsed_s:0};return {success:true}};pollOnce=async()=>{model.state.experiment_dir='new'}",h.context);
  await h.el('start').onclick();
  assert.deepEqual(actions,['recover','start','iv_voltage']);
  assert.equal(vm2.runInContext('model.samples[0].source',h.context),'old');
});
test('a poll started before opening a file cannot overwrite the new session', async()=>{
  const h=ivHarness();await new Promise(r=>setImmediate(()=>setImmediate(r)));
  let resolve;h.setFetch(()=>new Promise(r=>{resolve=r;}));
  const pending=vm2.runInContext('poll()',h.context);
  vm2.runInContext('model.epoch=(model.epoch||0)+1;model.ignoreLatest=true;model.voltage=3;model.samples=[]',h.context);
  resolve({json:async()=>({generation:1,connected:true,last_experiment_dir:'old',samples:[{sample_index:1,elapsed_s:1,value_si:1,status:'ok'}],iv_voltage_boundary:{generation:1,sample_index:0,elapsed_s:0,voltage_v:9}})});
  await pending;
  assert.equal(vm2.runInContext('model.voltage',h.context),3);
  assert.equal(vm2.runInContext('model.samples.length',h.context),0);
});
test('restart is blocked if the old raw file cannot be recovered',async()=>{
  const h=ivHarness();await new Promise(r=>setImmediate(()=>setImmediate(r)));
  h.el('interval').value='1';h.el('range').value='auto';h.el('nplc').value='1';
  vm2.runInContext("model.state={connected:true,simulation:true,last_experiment_dir:'old'};model.voltage=2;model.ignoreLatest=false",h.context);
  const actions=[];h.context.actions=actions;
  vm2.runInContext("recoverRaw=async()=>{throw Error('读取失败')};command=async action=>{actions.push(action);return {success:true}}",h.context);
  await h.el('start').onclick();
  assert.deepEqual(actions,[]);
  assert.match(h.el('status').textContent,/读取失败/);
});
test('user-specified firmware is marked as a default in connection and JSON',async()=>{
  const h=ivHarness();await new Promise(r=>setImmediate(()=>setImmediate(r)));
  h.el('range').value='auto';h.el('nplc').value='1';
  const calls=[];h.context.calls=calls;
  vm2.runInContext("command=async (action,payload)=>{calls.push({action,payload});return {success:true}};download=(name,text)=>{globalThis.exported=JSON.parse(text)};checkpoint=async()=>{}",h.context);
  await h.el('connect').onclick();
  assert.equal(calls[1].payload.source,'user_default');
  await vm2.runInContext('save()',h.context);
  assert.equal(vm2.runInContext('exported.firmware.outguard_sw',h.context),'2.10.882.24');
  assert.equal(vm2.runInContext('exported.firmware.source',h.context),'user_default');
});
test('cancelling the save dialog leaves the session unsaved',async()=>{
  const h=ivHarness();await new Promise(r=>setImmediate(()=>setImmediate(r)));
  vm2.runInContext("model.ignoreLatest=true;model.dirty=true;window.showSaveFilePicker=async()=>{const e=Error('cancelled');e.name='AbortError';throw e};checkpoint=async()=>{};download=()=>{throw Error('unexpected download')}",h.context);
  await vm2.runInContext('save()',h.context);
  assert.equal(vm2.runInContext('model.dirty',h.context),true);
});
test('save writes the selected file before marking the session saved',async()=>{
  const h=ivHarness();await new Promise(r=>setImmediate(()=>setImmediate(r)));
  vm2.runInContext("model.ignoreLatest=true;model.dirty=true;globalThis.events=[];window.showSaveFilePicker=async()=>({createWritable:async()=>({write:async data=>events.push(JSON.parse(data).format),close:async()=>events.push('closed')})});checkpoint=async()=>events.push('checkpoint');download=()=>{throw Error('unexpected download')}",h.context);
  await vm2.runInContext('save()',h.context);
  assert.deepEqual(Array.from(vm2.runInContext('events',h.context)),['fluke-iv','closed','checkpoint']);
  assert.equal(vm2.runInContext('model.dirty',h.context),false);
});
