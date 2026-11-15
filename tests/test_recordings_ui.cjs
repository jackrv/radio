const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../local_admin/admin.js'), 'utf8');
const queueBlock = script.slice(script.indexOf('  async function loadQueue()'), script.indexOf('  let recordingOffset ='));
const block = script.slice(script.indexOf('  let recordingOffset ='), script.indexOf('  const loaders ='));

function harness(page, extraSummary = {}) {
  class Element {
    constructor() { this.children = []; this.value = ''; }
    get firstChild() { return this.children[0]; }
    get options() { return this._options || this.children; }
    set options(items) { this._options = items; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute() {}
    addEventListener() {}
  }
  const elements = new Map();
  const $ = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  $('recording-group').options = ['normal','short','all','shadow'].map(value => ({value}));
  $('recording-group').value = 'normal';
  $('recording-filter').value = 'all';
  const summary = {statuses:{ready:1}, reviewed:0, bytes:32, quality:{raw_text:{wer:null},final_text:{wer:null}}, ...extraSummary};
  let response = page;
  let queueResponse = {available:true,live:true,groups:[]};
  const notes = [];
  const urls = [];
  const context = vm.createContext({$, el:(tag, cls, text)=>Object.assign(new Element(), {className:cls,textContent:text}), request: async (url, method, body) => {
    urls.push(url);
    if (method==='PUT') return {...body,revision:body.expected_revision+1};
    if (url.endsWith('/queue')) return typeof queueResponse==='function' ? queueResponse() : queueResponse;
    return url.includes('/summary') ? summary : response;
  },
    pct:String, fix:String, yes:String, note:text=>notes.push(text), STATUS:{ready:'Ready'}, action:cb=>cb()});
  vm.runInContext(queueBlock + block + '\nglobalThis.loadRecordings = recordings; globalThis.loadQueueCounters = loadQueue; globalThis.saveFirst = () => reviewCards[0].submit();', context);
  return {$, notes, urls, load:()=>context.loadRecordings(true), more:()=>context.loadRecordings(false),
    loadQueue:()=>context.loadQueueCounters(), save:()=>context.saveFirst(),
    set:page=>{response=page;}, setQueue:queue=>{queueResponse=queue;}};
}

const item = {id:'a'.repeat(32),created:1,status:'ready',data:{raw_text:'текст',final_text:'текст'},reference:null,revision:0};

test('old API without groups still renders recordings and retains pagination', async () => {
  const ui = harness({items:[item],total:30});
  await ui.load();
  assert.equal(ui.$('recording-group').disabled, true);
  assert.equal(ui.$('recording-group').value, 'all');
  assert.equal(ui.$('recordings').children.length, 1);
  assert.equal(ui.$('recording-more').hidden, false);
  assert.match(ui.notes[0], /Перезапустіть/);
});

test('empty old response does not crash when rendering empty state', async () => {
  const ui = harness({items:[],total:0});
  await ui.load();
  assert.equal(ui.$('recordings').children.length, 1);
  assert.equal(ui.$('recording-more').hidden, true);
});

test('groups become usable after backend update without reopening page', async () => {
  const ui = harness({items:[],total:0});
  await ui.load();
  ui.set({items:[item],total:1,groups:{all:3,normal:1,short:2}});
  await ui.load();
  assert.equal(ui.$('recording-group').disabled, false);
  assert.match(ui.$('recording-group').options[2].textContent, /3/);
  assert.equal(ui.$('recordings').children.length, 1);
});

test('queue filter adds the queue to the request and freezes ordinary groups', async () => {
  const ui = harness({items:[item],total:1,groups:{all:3,normal:1,short:2}});
  const urls = [];
  ui.$('recording-queue').value = 'A';
  const original = ui.$('recording-group');
  await ui.load();
  assert.equal(original.disabled, true, 'звичайні групи вимкнено під час черги');
  ui.$('recording-queue').value = '';
  await ui.load();
  assert.equal(original.disabled, false);
});

test('shadow option stays disabled when an older panel does not support it', async () => {
  const ui = harness({items:[item],total:1,groups:{all:1,normal:1,short:0}});
  await ui.load();
  assert.equal(ui.$('recording-group').options[3].disabled, true);
  assert.equal(ui.$('recording-shadow-summary').hidden, true);
});

test('shadow review shows the reason and reviewed speech separately from noise', async () => {
  const candidate = {...item, data:{...item.data,text_filter_shadow:{additional:true,phrase:'дякую'}}};
  const ui = harness({items:[candidate],total:1,groups:{all:1,normal:1,short:0,shadow:1}},
    {text_filter_shadow:{evaluated:7,candidates:4,reviewed:2,speech:1,noise:1,notification:0,room:0,unclear:0}});
  await ui.load();
  assert.equal(ui.$('recording-group').options[3].disabled, false);
  assert.equal(ui.$('recording-shadow-summary').hidden, false);
  assert.match(ui.$('recording-shadow-summary').textContent, /мовлення 1, шум 1/);
  const card = ui.$('recordings').children[0];
  const reason = card.children.find(child=>child.className==='recording-shadow');
  assert.match(reason.textContent, /«дякую»/);
});

test('new queue counts preserve the selected queue and the card being edited', async () => {
  const ui = harness({items:[item],total:1,groups:{all:1,normal:1,short:0,shadow:0}});
  ui.setQueue({available:true,live:true,groups:[{key:'A',title:'Whisper',total:1,present:1,reviewed:0}]});
  await ui.loadQueue();
  ui.$('recording-queue').value='A';
  await ui.load();
  const card=ui.$('recordings').children[0];
  ui.setQueue({available:true,live:true,groups:[{key:'A',title:'Whisper',total:2,present:2,reviewed:0}]});
  await ui.loadQueue();
  assert.equal(ui.$('recording-queue').value,'A');
  assert.equal(ui.$('recordings').children[0],card);
  assert.match(ui.$('recording-queue-progress').textContent,/Залишилось 2/);
  ui.setQueue(()=>{throw new Error('temporarily offline');});
  await ui.loadQueue();
  assert.equal(ui.$('recording-queue').value,'A');
  assert.equal(ui.$('recording-queue').options.length,3);
});

test('saving a pending card keeps the next page from skipping unreviewed audio', async () => {
  const ui=harness({items:[item],total:3,groups:{all:3,normal:3,short:0,shadow:0}});
  ui.$('recording-filter').value='pending';
  await ui.load();
  await ui.save();
  ui.set({items:[{...item,id:'b'.repeat(32)}],total:2,groups:{all:2,normal:2,short:0,shadow:0}});
  await ui.more();
  const lists=ui.urls.filter(url=>url.startsWith('/api/recordings?'));
  assert.match(lists[1],/offset=0&/);
});
