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
