const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = require('./panel_source.cjs');
const block = script.slice(script.indexOf('  const TODO_VIEWS ='), script.indexOf('  const loaders ='));

function harness(page, extraSummary = {}, options = {}) {
  class Element {
    constructor() { this.children = []; this.value = ''; this.dataset = {}; this.listeners = {}; this.classList = {add(){}, toggle(){}}; }
    get firstChild() { return this.children[0]; }
    get options() { return this._options || this.children; }
    set options(items) { this._options = items; }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children = items; }
    click() { return this.listeners.click?.(); }
    focus() { this.focused = true; }
    setAttribute() {}
    addEventListener(name, callback) { this.listeners[name] = callback; }
  }
  const elements = new Map();
  const $ = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  $('recording-view').options = ['radio','speech','filtered','reviewed','telegram','all'].map(value => ({value, textContent:value, dataset:{}}));
  $('recording-view').value = 'radio';
  $('recording-period').value = '';
  $('recording-edits').options = [{value:''}, {value:'miniapp'}];
  const summary = {statuses:{ready:1}, reviewed:0, bytes:32, quality:{raw_text:{wer:null},final_text:{wer:null}}, ...extraSummary};
  let response = page;
  const notes = [];
  const urls = [];
  const added = [];
  const windowEvents = {};
  const context = vm.createContext({$, el:(tag, cls, text)=>Object.assign(new Element(), {className:cls,textContent:text}), request: async (url, method, body) => {
    urls.push(url);
    if (method==='PUT') return options.review ? options.review(body) : {...body,revision:body.expected_revision+1};
    return url.includes('/summary') ? summary : response;
  },
    addCallsign:async line=>{added.push(line); if (options.addError) throw new Error(options.addError);},
    window:{addEventListener:(name,cb)=>{windowEvents[name]=cb;}}, confirm:()=>options.discard !== false,
    pct:String, fix:String, yes:String, note:text=>notes.push(text), STATUS:{ready:'Ready'}, action:cb=>cb(),
    localStorage:{getItem(){return null;}}, clearTimeout(){}, setTimeout(){return 1;}});
  vm.runInContext(block + '\nglobalThis.loadRecordings = recordings; globalThis.saveFirst = () => reviewCards[0].submit(); globalThis.cards = reviewCards;', context);
  return {$, notes, urls, added, windowEvents, cards:()=>context.cards, load:()=>context.loadRecordings(true), more:()=>context.loadRecordings(false),
    save:()=>context.saveFirst(), set:page=>{response=page;}};
}

const item = {id:'a'.repeat(32),created:1,status:'ready',data:{raw_text:'текст',final_text:'текст'},reference:null,revision:0};

test('archive renders current views and retains pagination', async () => {
  const ui = harness({items:[item], total:30, views:{radio:30, speech:2}});
  await ui.load();
  assert.equal(ui.$('recordings').children.filter(c => c.className.includes('recording-card')).length, 1);
  assert.equal(ui.$('recording-more').hidden, false);
  assert.equal(ui.$('recording-view').options[0].textContent, 'radio · 30');
  assert.match(ui.urls[0], /view=radio/);
});

test('empty view renders a useful message without failing', async () => {
  const ui = harness({items:[],total:0,views:{radio:0}});
  await ui.load();
  assert.match(ui.$('recordings').children[0].textContent, /розмічено/);
  assert.equal(ui.$('recording-more').hidden, true);
});

test('period and editor filters are sent with the selected view', async () => {
  const ui = harness({items:[],total:0});
  ui.$('recording-view').value = 'reviewed';
  ui.$('recording-period').value = '24';
  ui.$('recording-edits').value = 'miniapp';
  await ui.load();
  const url = new URL(ui.urls[0], 'http://localhost');
  assert.equal(url.searchParams.get('view'), 'reviewed');
  assert.equal(url.searchParams.get('edits'), 'miniapp');
  const since = Number(url.searchParams.get('since'));
  assert.ok(Math.abs(since - (Math.floor(Date.now()/1000)-86400)) <= 2);
});

test('search text is encoded and the next page uses its offset', async () => {
  const ui = harness({items:[item],total:3});
  ui.$('recording-search').value = 'синтетика & №42';
  await ui.load();
  await ui.more();
  const lists = ui.urls.filter(url=>url.startsWith('/api/recordings?'));
  assert.equal(new URL(lists[0], 'http://localhost').searchParams.get('q'), 'синтетика & №42');
  assert.match(lists[1], /offset=1&/);
});

test('loading more preserves the existing card and its unsaved input', async () => {
  const ui = harness({items:[item],total:3});
  await ui.load();
  const card = ui.$('recordings').children[0];
  const input = descendants(card).find(n=>n.className==='text-input-box').children[0];
  input.value = 'незбережена правка';
  ui.set({items:[{...item,id:'b'.repeat(32)}],total:3});
  await ui.more();
  assert.equal(ui.$('recordings').children[0], card);
  assert.equal(input.value, 'незбережена правка');
});

for (const view of ['radio', 'reviewed']) {
  test(`saving a ${view} card keeps the next page aligned`, async () => {
    const ui = harness({items:[item],total:3});
    ui.$('recording-view').value = view;
    await ui.load();
    await ui.save();
    ui.set({items:[{...item,id:'b'.repeat(32)}],total:2});
    await ui.more();
    const lists = ui.urls.filter(url=>url.startsWith('/api/recordings?'));
    assert.match(lists[1], new RegExp(`offset=${view === 'radio' ? 0 : 1}&`));
    assert.match(ui.notes[0], /збережено/);
  });
}


test('missing callsign number can be added from its hint without saving or replacing the draft', async () => {
  const ui = harness({items:[{...item, text_check:{issues:[{kind:'info', message:'Бракує номера', dictionary_entry:'береза: 57'}]}}],total:1});
  await ui.load();
  const all = [];
  const visit = node => {all.push(node);for(const child of node.children || [])visit(child);};
  visit(ui.$('recordings'));
  const add = all.find(node=>node.textContent==='＋ У словник: береза 57');
  const field = all.find(node=>node.className==='text-input-box').children[0];
  field.value = 'незбережений текст оператора';
  add.listeners.click();
  for(let i=0;i<8;i++)await Promise.resolve();
  assert.deepEqual(ui.added,['береза: 57']);
  assert.equal(field.value,'незбережений текст оператора');
  assert.equal(ui.urls.some(url=>url.endsWith('/review')),false);
  assert.equal(add.disabled,false);
});


function descendants(node) {
  return [node, ...(node.children || []).flatMap(descendants)];
}
function editor(ui) {
  const nodes = descendants(ui.$('recordings'));
  return {nodes, form:nodes.find(n=>n.listeners.submit), input:nodes.find(n=>n.className==='text-input-box').children[0]};
}

test('dirty badge and count follow edits and reverting them, including unsaved first-time references', async () => {
  const ui=harness({items:[item],total:1}); await ui.load();
  const {input,form}=editor(ui);const initial=input.value;
  input.value='нова чернетка';form.listeners.input();
  assert.equal(ui.cards()[0].dirty,true);assert.equal(ui.$('archive-drafts-nav').hidden,false);
  assert.equal(ui.$('archive-drafts-nav').textContent,'1');
  const event={preventDefault(){this.prevented=true;}};ui.windowEvents.beforeunload(event);assert.equal(event.prevented,true);
  input.value=initial;form.listeners.input();
  assert.equal(ui.cards()[0].dirty,false);assert.equal(ui.$('archive-drafts').hidden,true);
  const clean={preventDefault(){this.prevented=true;}};ui.windowEvents.beforeunload(clean);assert.equal(clean.prevented,undefined);
});

test('cancelled refresh preserves draft and restores the filters describing the visible cards',async()=>{
  const ui=harness({items:[item],total:1},{},{discard:false});await ui.load();
  const {input,form}=editor(ui);input.value='чернетка';form.listeners.input();
  ui.$('recording-view').value='all';const before=ui.urls.length;await ui.load();
  assert.equal(input.value,'чернетка');assert.equal(ui.$('recording-view').value,'radio');assert.equal(ui.urls.length,before);
});

test('typing during save remains dirty after the earlier snapshot is acknowledged',async()=>{
  let finish;
  const ui=harness({items:[item],total:1},{},{review:body=>new Promise(resolve=>{finish=()=>resolve({...body,revision:1});})});
  await ui.load();const {input,form}=editor(ui);
  input.value='перша правка';form.listeners.input();const saving=ui.save();
  input.value='друга правка';form.listeners.input();finish();await saving;
  assert.equal(input.value,'друга правка');assert.equal(ui.cards()[0].dirty,true);
  input.value='перша правка';form.listeners.input();assert.equal(ui.cards()[0].dirty,false);
});

test('identical hints are grouped without hiding distinct remarks',async()=>{
  const hint={kind:'info',message:'Перевірте номер',dictionary_entry:'береза: 57'};
  const ui=harness({items:[{...item,text_check:{issues:[hint,hint,{kind:'format',message:'Пробіли'}]}}],total:1});await ui.load();
  const nodes=editor(ui).nodes;
  assert.equal(nodes.filter(n=>n.className?.startsWith('text-check-item')).length,2);
  assert.equal(nodes.filter(n=>n.textContent==='＋ У словник: береза 57').length,1);
});



test('callsign hints carry the add button; no separate form, save is the last action', async () => {
  const ui = harness({items:[{...item, text_check:{issues:[{kind:'info', message:'Нового позивного немає', dictionary_entry:'клен: 48'}]}}],total:1});
  await ui.load();
  const nodes = editor(ui).nodes;
  assert.equal(nodes.some(n=>n.textContent==='＋ Позивний' || n.className==='callsign-inline'), false);
  assert.equal(nodes.filter(n=>n.textContent==='＋ У словник: клен 48').length, 1);
  const actions = nodes.find(n=>n.className==='save-actions');
  const last = actions.children.at(-1), before = actions.children.at(-2);
  assert.equal(last.className, 'save-primary');
  assert.equal(before.className, 'dirty-badge');
});
