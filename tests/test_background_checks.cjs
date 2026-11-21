const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = name => fs.readFileSync(path.join(__dirname, '../local_admin/js', name), 'utf8');
class Node {
  constructor(tag, cls, text) { this.tag = tag; this.className = cls; this.textContent = text; this.children = []; this.events = {}; this.dataset = {}; this.isConnected = true; }
  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this.events[name] = callback; }
  setAttribute() {}
}
function jobHarness(request) {
  const timers = [];
  const ctx = vm.createContext({request, el:(...args)=>new Node(...args), Date, note(){},
    setTimeout:callback=>{timers.push(callback); return timers.length;}, clearTimeout(){}});
  const code = source('01-jobs.js');
  vm.runInContext(code.slice(code.indexOf('  function serverJobPanel(')) + '\nglobalThis.create = serverJobPanel;', ctx);
  const button = new Node('button'), box = new Node('div'), reports = [];
  const create = () => ctx.create({button, box, kind:'impact', start:()=>request('start'), render:r=>{reports.push(r);box.replaceChildren(new Node('p','','report'));}});
  return {button, box, reports, timers, create};
}
test('a background check returns immediately and its saved result is restored in a new page', async () => {
  let job = {id:'abc',kind:'impact',status:'running',progress:{done:3,total:10}};
  const h = jobHarness(async url => url === '/api/jobs' ? {items:[job]} : job);
  await h.create().run();
  assert.equal(h.button.disabled,true);
  assert.match(h.box.children[0].textContent,/30%/);
  assert.match(h.box.children[0].textContent,/Можна закрити сторінку/);
  job = {...job,status:'done',finished:1,result:{records:12}};
  await h.create().resume();
  assert.equal(h.button.disabled,false);
  assert.deepEqual(h.reports,[{records:12}]);
  assert.match(h.box.children[0].textContent,/Збережений результат/);
});
test('network polling recovers without cancelling the server task, and detached panels stop polling', async () => {
  let reads = 0;
  const h = jobHarness(async url => {
    if (url === 'start') return {id:'abc',status:'queued'};
    if (++reads === 1) throw new Error('offline');
    return {id:'abc',status:'done',finished:1,result:{records:1}};
  });
  await h.create().run();
  await h.timers.shift()();
  assert.match(h.box.children[0].textContent,/серверне завдання не скасовується/);
  await h.timers.shift()();
  assert.equal(h.reports.length,1);
  const detached = jobHarness(async()=>({id:'def',status:'running'}));
  await detached.create().run(); detached.box.isConnected=false;
  await detached.timers.shift()();
  assert.equal(detached.timers.length,0);
});
test('lab refresh preserves the actual window editor nodes and an unfinished time edit', () => {
  const ctx = vm.createContext({el:(...args)=>new Node(...args), Date, dayKey:()=> '2026-11-20',
    localStorage:{getItem(){return null;},setItem(){}}, labPresets:{}, note(){}, action:f=>f(), setTimeout:()=>1, clearTimeout(){}});
  const code = source('04-analysis.js');
  vm.runInContext(code.slice(code.indexOf('  const WINDOWS_KEY ='), code.indexOf('  let labJobFingerprint')) + '\nglobalThis.create = windowsEditor;',ctx);
  const q = {estimates:{base:300},now:Date.now()/1000};
  const first = ctx.create(q,[{preset:'base'}]);
  const row = first.children.find(n=>n.className==='lab-window-rows').children[0];
  const field = row.children.find(n=>n.type==='time');
  field.value='02:25'; field.events.input();
  const updated = ctx.create({...q,now:q.now+10},[{preset:'base'}]);
  assert.equal(first,updated);
  assert.equal(updated.children.find(n=>n.className==='lab-window-rows').children[0],row);
  assert.equal(field.value,'02:25');
  assert.ok(row.children.some(n=>n.type==='date'));
});

test('large rule reports render in bounded chunks and keep the remaining results available', () => {
  const box = new Node('div');
  const code = source('04-analysis.js');
  const ctx = vm.createContext({$:()=>box,el:(...args)=>new Node(...args),num:String,
    IMPACT_NAMES:{},IMPACT_LABELS:{}});
  vm.runInContext(code.slice(code.indexOf('  function renderTextImpact('),code.indexOf('\n  $("text-impact-run")'))+'\nglobalThis.render = renderTextImpact;',ctx);
  const check = {kind:'text_alias',label:'синтетичне правило',effect:'helpful',no_edits_delta:0,missed_delta:0,wer_delta:0,
    false_text_delta:0,worse:0,better:0,examples:[],false_text_examples:[]};
  ctx.render({records:1,non_speech_records:0,checks:Array.from({length:85},()=>check),
    current:{false_text:0,false_text_by_label:{noise:0,notification:0,room:0}},scope:'test'});
  const count = () => box.children.filter(n=>n.className.startsWith('eval-item')).length;
  const more = box.children.find(n=>n.tag==='button');
  assert.equal(count(),40);
  more.events.click(); assert.equal(count(),80);
  more.events.click(); assert.equal(count(),85); assert.equal(more.hidden,true);
});

test('capture panel shows persisted audio separately from backlog and exposes processed records', () => {
  const box = new Node('div'), opened = [];
  const code = source('04-analysis.js');
  const ctx = vm.createContext({$:()=>box,el:(...args)=>new Node(...args),num:String,fix:v=>String(v),Date,
    openRecordings:options=>opened.push(options)});
  vm.runInContext(code.slice(code.indexOf('  function renderCaptureState('))+'\nglobalThis.render=renderCaptureState;',ctx);
  ctx.render({archive:{saved:5,pending:2,processed:3,last_recorded_at:1,audio_seconds:60},write_errors:2});
  const text = box.children.map(n=>n.textContent).join(' ');
  assert.match(text,/5 фрагментів/); assert.match(text,/чекають 2, оброблено 3/);
  assert.match(text,/Помилки запису аудіо за сесію приймача: 2/);
  box.children.find(n=>n.tag==='button').events.click();
  assert.equal(opened[0].view,'captured');
});

test('window preview delegates to the server and ignores a late plan for older input', async () => {
  const timers=[], requests=[];
  const ctx=vm.createContext({el:(...args)=>new Node(...args),Date,dayKey:()=> '2026-11-20',labPresets:{},
    localStorage:{getItem(){return null;},setItem(){}},note(){},action:f=>f(),clearTimeout(){},
    setTimeout:cb=>{timers.push(cb);return timers.length;},
    request:(url,method,body)=>new Promise(resolve=>requests.push({url,method,body,resolve}))});
  const code=source('04-analysis.js');
  vm.runInContext(code.slice(code.indexOf('  const WINDOWS_KEY ='),code.indexOf('  let labJobFingerprint'))+'\nglobalThis.create=windowsEditor;',ctx);
  const box=ctx.create({},[{preset:'base'}]);
  const old=timers.shift()();
  const row=box.children.find(n=>n.className==='lab-window-rows').children[0];
  const field=row.children.find(n=>n.type==='time');field.value='02:25';field.events.input();
  const fresh=timers.shift()();
  assert.equal(requests.length,2);assert.equal(requests[0].url,'/api/lab/plan');assert.equal(requests[0].method,'POST');
  requests[1].resolve({plan:[{window:[1,7201],jobs:[{job:{preset:'base'},start:1,end:3601}]}],left:[]});
  await fresh;
  const go=box.children.at(-1);assert.equal(go.disabled,false);
  requests[0].resolve({plan:[{window:[1,7201],jobs:[]}],left:[{preset:'base'}]});await old;
  assert.equal(go.disabled,false,'старий порожній план не перекриває новішу відповідь');
  assert.equal(field.value,'02:25');
});
