const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const script = fs.readFileSync(path.join(__dirname, '../local_admin/admin.js'), 'utf8');
const block = script.slice(script.indexOf('  const TODO_VIEWS ='), script.indexOf('  const loaders ='));

function harness(page, extraSummary = {}) {
  class Element {
    constructor() { this.children = []; this.value = ''; this.dataset = {}; this.classList = {add(){}, toggle(){}}; }
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
  $('recording-view').options = ['radio','speech','filtered','shadow','reviewed','all'].map(value => ({value, textContent:value, dataset:{}}));
  $('recording-view').value = 'radio';
  $('recording-period').value = '';
  $('recording-edits').options = [{value:''}, {value:'miniapp'}];
  const summary = {statuses:{ready:1}, reviewed:0, bytes:32, quality:{raw_text:{wer:null},final_text:{wer:null}}, ...extraSummary};
  let response = page;
  const notes = [];
  const urls = [];
  const context = vm.createContext({$, el:(tag, cls, text)=>Object.assign(new Element(), {className:cls,textContent:text}), request: async (url, method, body) => {
    urls.push(url);
    if (method==='PUT') return {...body,revision:body.expected_revision+1};
    return url.includes('/summary') ? summary : response;
  },
    pct:String, fix:String, yes:String, note:text=>notes.push(text), STATUS:{ready:'Ready'}, action:cb=>cb(),
    localStorage:{getItem(){return null;}}, clearTimeout(){}, setTimeout(){return 1;}});
  vm.runInContext(block + '\nglobalThis.loadRecordings = recordings; globalThis.saveFirst = () => reviewCards[0].submit();', context);
  return {$, notes, urls, load:()=>context.loadRecordings(true), more:()=>context.loadRecordings(false),
    save:()=>context.saveFirst(), set:page=>{response=page;}};
}

const item = {id:'a'.repeat(32),created:1,status:'ready',data:{raw_text:'текст',final_text:'текст'},reference:null,revision:0};

test('archive renders current views and retains pagination', async () => {
  const ui = harness({items:[item], total:30, views:{radio:30, speech:2}});
  await ui.load();
  assert.equal(ui.$('recordings').children.length, 1);
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

test('shadow view is hidden only when no candidates are available', async () => {
  const ui = harness({items:[],total:0,views:{shadow:0}});
  await ui.load();
  assert.equal(ui.$('recording-view').options[3].hidden, true);
  ui.set({items:[],total:0,views:{shadow:4}});
  await ui.load();
  assert.equal(ui.$('recording-view').options[3].hidden, false);
  assert.equal(ui.$('recording-view').options[3].textContent, 'shadow · 4');
});

test('shadow review shows the reason and reviewed speech separately from noise', async () => {
  const candidate = {...item, data:{...item.data,text_filter_shadow:{additional:true,phrase:'дякую'}}};
  const ui = harness({items:[candidate],total:1},
    {text_filter_shadow:{evaluated:7,candidates:4,reviewed:2,speech:1,noise:1,notification:0,room:0,unclear:0}});
  await ui.load();
  assert.equal(ui.$('recording-shadow-summary').hidden, false);
  assert.match(ui.$('recording-shadow-summary').textContent, /мовлення 1, шум 1/);
  const reason = ui.$('recordings').children[0].children.find(child=>child.className==='recording-shadow');
  assert.match(reason.textContent, /«дякую»/);
});

test('loading more preserves the existing card and its unsaved input', async () => {
  const ui = harness({items:[item],total:3});
  await ui.load();
  const card = ui.$('recordings').children[0];
  const form = card.children.find(child=>child.children.some(c=>c.className==='recording-text'));
  const input = form.children.find(c=>c.className==='recording-text').children[1];
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
