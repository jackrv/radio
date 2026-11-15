const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../local_admin/admin.js'), 'utf8');
const block = script.slice(script.indexOf('  let recordingOffset ='), script.indexOf('  const loaders ='));

function harness(page) {
  class Element {
    constructor() { this.children = []; this.value = ''; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    setAttribute() {}
    addEventListener() {}
  }
  const elements = new Map();
  const $ = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  $('recording-group').options = ['normal','short','all'].map(value => ({value}));
  $('recording-group').value = 'normal';
  $('recording-filter').value = 'all';
  const summary = {statuses:{ready:1}, reviewed:0, bytes:32, quality:{raw_text:{wer:null},final_text:{wer:null}}};
  let response = page;
  const notes = [];
  const context = vm.createContext({$, el:()=>new Element(), request: async url => url.includes('/summary') ? summary : response,
    pct:String, fix:String, yes:String, note:text=>notes.push(text), STATUS:{ready:'Ready'}, action:cb=>cb()});
  vm.runInContext(block + '\nglobalThis.loadRecordings = recordings;', context);
  return {$, notes, load:()=>context.loadRecordings(true), set:page=>{response=page;}};
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
