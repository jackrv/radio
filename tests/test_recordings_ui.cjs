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
    setSelectionRange(start, end) { this.selectionStart = start; this.selectionEnd = end; }
    setAttribute() {}
    addEventListener(name, callback) { this.listeners[name] = callback; }
  }
  const elements = new Map();
  const $ = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  $('recording-view').options = ['radio','speech','filtered','reviewed','telegram','all'].map(value => ({value, textContent:value, dataset:{}}));
  $('recording-view').value = 'radio';
  $('recording-period').value = '';
  const summary = {statuses:{ready:1}, reviewed:0, bytes:32, quality:{raw_text:{wer:null},final_text:{wer:null}}, ...extraSummary};
  let response = page;
  const notes = [];
  const urls = [];
  const added = [];
  const windowEvents = {};
  const context = vm.createContext({$, el:(tag, cls, text)=>Object.assign(new Element(), {className:cls,textContent:text}), request: async (url, method, body) => {
    urls.push(url);
    if (method==='PUT') return options.review ? options.review(body) : {...body,revision:body.expected_revision+1};
    if (url.includes('suggest-roles') && options.callsigns) return {callsigns:options.callsigns, roles:options.roles || null};
    if (url.includes('/text-check') && options.textCheck) return options.textCheck(body);
    if (url.includes('/restore')) { (options.restored||[]).push(body); return {...body, revision:body.expected_revision+1}; }
    return url.includes('/summary') ? summary : response;
  },
    addCallsign:async line=>{added.push(line); if (options.addError) throw new Error(options.addError);},
    window:{addEventListener:(name,cb)=>{windowEvents[name]=cb;}}, confirm:()=>options.discard !== false,
    pct:String, num:String, fix:String, yes:String, note:text=>notes.push(text), STATUS:{ready:'Ready'}, action:cb=>cb(),
    localStorage:{getItem(){return null;}}, clearTimeout(){}, setTimeout:options.timers ? cb=>{cb(); return 1;} : ()=>1});
  vm.runInContext(block + '\nglobalThis.loadRecordings = recordings; globalThis.saveFirst = () => reviewCards[0].submit(); globalThis.cards = reviewCards; globalThis.undoLast = undoLast;', context);
  return {$, notes, urls, added, windowEvents, cards:()=>context.cards, load:()=>context.loadRecordings(true), more:()=>context.loadRecordings(false),
    save:()=>context.saveFirst(), undo:()=>context.undoLast(), set:page=>{response=page;}};
}

const item = {id:'a'.repeat(32),created:1,status:'ready',data:{raw_text:'текст',final_text:'текст'},reference:null,revision:0};

test('editor backdrop renders server marks over the same text', async () => {
  const text = 'Клен / <img>';
  const ui = harness({items:[{...item,data:{raw_text:text}}],total:1}, {}, {
    timers:true, textCheck: () => ({issues:[],formatted:null,highlights:[
      {start:0,end:4,kind:'callsign'},{start:5,end:6,kind:'boundary'},
      {start:7,end:12,kind:'problem'}]})});
  await ui.load();
  await new Promise(resolve => setImmediate(resolve));
  const {input,nodes} = editor(ui);
  const marked = nodes.find(n => n.className === 'transcript-callsign');
  assert.equal(marked.textContent, 'Клен');
  const layer = nodes.find(n => n.className === 'transcript-backdrop');
  assert.equal(layer.children.map(n => n.textContent).join(''), input.value);
  assert.equal(nodes.filter(n => n.className === 'transcript-problem').map(n => n.textContent).join(''),'<img>');
});

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
  ui.$('recording-view').value = 'person:42';   // «Правки: Ім'я» — пункт виду
  ui.$('recording-period').value = '24';
  await ui.load();
  const url = new URL(ui.urls[0], 'http://localhost');
  assert.equal(url.searchParams.get('view'), 'all');
  assert.equal(url.searchParams.get('edits'), '42');
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
  const card = ui.$('recordings').children.find(c => c.className.includes('recording-card'));
  const input = descendants(card).find(n=>n.className==='text-input-box').children[0];
  input.value = 'незбережена правка';
  ui.set({items:[{...item,id:'b'.repeat(32)}],total:3});
  await ui.more();
  assert.equal(ui.$('recordings').children.find(c => c.className.includes('recording-card')), card);
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
  return {nodes, form:nodes.find(n=>n.listeners.submit), input:nodes.find(n=>n.className==='text-input-layer').children[1]};
}

test('dirty badge and count follow edits and reverting them, including unsaved first-time references', async () => {
  const ui=harness({items:[item],total:1}); await ui.load();
  const {input,form}=editor(ui);const initial=input.value;
  input.value='нова чернетка';form.listeners.input();
  assert.equal(ui.cards()[0].dirty,true);assert.equal(ui.$('archive-drafts-nav').hidden,false);
  assert.equal(ui.$('archive-drafts-nav').textContent,'1');
  const event={preventDefault(){this.prevented=true;}};ui.windowEvents.beforeunload(event);assert.equal(event.prevented,true);
  input.value=initial;form.listeners.input();
  assert.equal(ui.cards()[0].dirty,false);assert.equal(ui.$('archive-drafts-nav').hidden,true);assert.equal(ui.cards()[0].draftsPill.disabled,true);
  const clean={preventDefault(){this.prevented=true;}};ui.windowEvents.beforeunload(clean);assert.equal(clean.prevented,undefined);
});

test('archive row follows button edits and shows the saved reference', async () => {
  const ui=harness({items:[{...item,data:{raw_text:'клен прийом'}}],total:1},{},{timers:true,
    textCheck:()=>({issues:[{kind:'format',message:'кома',fixed_text:'Клен, прийом'}],formatted:'Клен, прийом',highlights:[]}),
    review:body=>({...body,revision:1})});
  await ui.load(); await new Promise(resolve => setImmediate(resolve));
  const nodes=()=>descendants(ui.$('recordings')).concat(descendants(ui.$('recording-rows')));
  const rowText=()=>nodes().find(n=>String(n.className).startsWith('row-text'));
  const {input,form}=editor(ui);
  input.value='Клен прийом';form.listeners.input();
  assert.equal(rowText().textContent,'Клен прийом');
  nodes().find(n=>n.className==='fix-all').listeners.click();
  assert.equal(input.value,'Клен, прийом'); assert.equal(rowText().textContent,'Клен, прийом');
  await ui.save();
  assert.equal(rowText().textContent,'Клен, прийом'); assert.equal(rowText().className,'row-text');
  assert.equal(nodes().find(n=>n.className==='row-kind').textContent,'Мовлення');
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

test('leaving the text field applies a spelling suggestion',async()=>{
  const ui=harness({items:[item],total:1},{},{
    textCheck: ()=>({issues:[{
      kind:'spelling',
      message:'Можливо, «вишня», а не «вішня»?',
      fixed_text:'вишня',
    }],formatted:null})
  });
  await ui.load();
  const {input}=editor(ui);
  input.value='вішня';
  await ui.cards()[0].applySpellingOnLeave();
  assert.equal(input.value,'вишня');
});

test('saving right after leaving the field sends the spelling-fixed text',async()=>{
  let sent;
  const ui=harness({items:[{...item, reference:null, label:null, revision:0}],total:1},{},{
    review: body=>{sent=body; return {...body, revision:1};},
    textCheck: body=>body.text==='вішня'
      ? {issues:[{kind:'spelling', message:'Можливо, «вишня», а не «вішня»?', fixed_text:'вишня'}], formatted:null}
      : {issues:[], formatted:null}
  });
  await ui.load();
  const {input}=editor(ui);
  input.listeners.focus(); input.value='вішня';   // зайшли в поле й набрали
  input.listeners.blur({relatedTarget:{}});   // фокус пішов з поля не на «Зберегти»
  await ui.save();
  assert.equal(sent.reference,'вишня');
});

test('daily-code spelling hints are not applied automatically on blur',async()=>{
  let sent;
  const ui=harness({items:[{...item, reference:null, label:null, revision:0}],total:1},{},{
    review: body=>{sent=body; return {...body, revision:1};},
    textCheck: ()=>({issues:[{
      kind:'spelling', manual_only:true, message:'Можливо, код доби', fixed_text:'Береза 68, 48 62'
    }],formatted:null})
  });
  await ui.load();
  const {input}=editor(ui);
  input.listeners.focus(); input.value='Береза 68, 48, 42';
  input.listeners.blur({relatedTarget:{}});
  await ui.save();
  assert.equal(sent.reference,'Береза 68, 48, 42');
});

test('duplicate save while blur correction is pending sends only one review', async () => {
  let finish, reviews = 0;
  const correction = new Promise(resolve => { finish = resolve; });
  const ui = harness({items:[{...item}],total:1},{},{
    textCheck: () => correction,
    review: body => { reviews++; return {...body, revision:1}; },
  });
  await ui.load();
  const {input} = editor(ui);
  input.listeners.focus(); input.value = 'вішня';
  input.listeners.blur({relatedTarget:{}});
  const first = ui.save(), second = ui.save();
  const save = editor(ui).nodes.find(node => node.dataset.status === 'Зберігаю…');
  assert.equal(save.disabled, true, 'feedback appears before the text check completes');
  finish({issues:[], formatted:null});
  await Promise.all([first, second]);
  assert.equal(reviews, 1);
  assert.equal(save.disabled, false);
});

test('concurrent editor checks share a request but later checks get fresh dictionaries', async () => {
  let finish, calls = 0;
  const correction = new Promise(resolve => { finish = resolve; });
  const ui = harness({items:[{...item}],total:1},{},{
    textCheck: () => { calls++; return correction; },
  });
  await ui.load();
  const {input} = editor(ui); input.value = 'Клен 15';
  const card = ui.cards()[0];
  const first = card.applySpellingOnLeave(), second = card.applyFormatting();
  assert.equal(calls, 1);
  finish({issues:[], formatted:null});
  await Promise.all([first, second]);
  await card.applyFormatting();
  assert.equal(calls, 2);
});

test('an old spelling response never replaces text typed during the check', async () => {
  let finish;
  const ui = harness({items:[{...item}],total:1},{},{
    textCheck: () => new Promise(resolve => { finish = resolve; }),
  });
  await ui.load();
  const {input} = editor(ui); input.value = 'вішня';
  const pending = ui.cards()[0].applySpellingOnLeave();
  input.value = 'Клен 15, прийом';
  finish({issues:[{kind:'spelling', fixed_text:'Вишня'}], formatted:null});
  assert.equal(await pending, false);
  assert.equal(input.value, 'Клен 15, прийом');
});

test('leaving the field for Save does not start spelling auto-correction',async()=>{
  let sent;
  const ui=harness({items:[{...item, reference:null, label:null, revision:0}],total:1},{},{
    review: body=>{sent=body; return {...body, revision:1};},
    textCheck: ()=>({issues:[{
      kind:'spelling', message:'Можливо, «вишня»', fixed_text:'вишня'
    }],formatted:null})
  });
  await ui.load();
  const {input,nodes}=editor(ui);
  const save=nodes.find(node=>node.className==='save-primary');
  input.listeners.focus(); input.value='вішня';
  input.listeners.blur({relatedTarget:save});
  await ui.save();
  assert.equal(sent.reference,'вішня');
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

test('a callsign is either "to" or "from": choosing one role clears the other, a second click clears both', async () => {
  let sent;
  const reviewed = {...item, reference:'Клен 48, я Ясень 12', label:'speech', reference_recipient:'клен 48', reference_sender:''};
  const ui = harness({items:[reviewed], total:1}, {}, {timers:true, callsigns:['клен 48'], review:body=>{sent=body; return {...body, revision:1};}});
  await ui.load();
  ui.cards()[0].refreshHints();   // як під час відкриття картки: чипи й підказки
  await new Promise(resolve => setImmediate(resolve));
  const switches = () => descendants(ui.$('recordings')).filter(n=>n.className==='role-switch');
  const buttons = () => switches()[0].children;
  assert.deepEqual(buttons().map(b=>[b.textContent, b.className]), [['Кому','ghost on'], ['Від','ghost']]);
  buttons()[1].click();
  assert.deepEqual(buttons().map(b=>b.className), ['ghost', 'ghost on']);
  buttons()[0].click();
  assert.deepEqual(buttons().map(b=>b.className), ['ghost on', 'ghost']);
  buttons()[0].click();
  assert.deepEqual(buttons().map(b=>b.className), ['ghost', 'ghost']);
  buttons()[1].click();
  await ui.save();
  assert.equal(sent.reference_recipient, '');
  assert.equal(sent.reference_sender, 'клен 48');
});

test('role callsign field is flagged when that callsign is not in the text', async () => {
  const reviewed = {...item, reference:'Клен, 48, я Ясень 12', label:'speech', reference_recipient:'клен 48', reference_sender:''};
  const ui = harness({items:[reviewed], total:1}, {}, {timers:true, callsigns:['клен 48']});
  await ui.load();
  ui.cards()[0].refreshHints();
  await new Promise(resolve => setImmediate(resolve));
  const field = descendants(ui.$('recordings')).find(n=>n.className==='role-name');
  assert.doesNotMatch(field.title, /немає в тексті/);
  field.value = 'дуб 7'; field.listeners.input();
  assert.match(field.title, /«дуб 7» немає в тексті/);
});

test('a quick label (key or mouse) can be undone: the record returns to its previous state and the card keeps its text', async () => {
  const restored = [];
  const ui = harness({items:[{...item, reference:null, label:null, revision:0, reviewed_at:null, data:{raw_text:'клен сорок', final_text:'клен сорок'}}], total:1}, {}, {restored});
  await ui.load();
  const {input} = editor(ui);
  input.value = 'клен сорок вісім';
  ui.cards()[0].setCategory('noise', true);   // the same path as key 2 or a click on «Шум ефіру»
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.cards()[0].done, true);
  assert.equal(input.value, '');
  await ui.undo();
  assert.equal(restored.length, 1);
  assert.equal(restored[0].reference, null); assert.equal(restored[0].label, null); assert.equal(restored[0].expected_revision, 1);
  assert.equal(ui.cards()[0].done, false);
  assert.equal(input.value, 'клен сорок вісім');
  await ui.undo();   // nothing left to undo
  assert.equal(restored.length, 1);
});

test('every section loader names a function defined in the panel script', () => {
  const loaders = script.slice(script.indexOf('  const loaders ='), script.indexOf('\n', script.indexOf('  const loaders =')));
  for (const [, key, value] of loaders.matchAll(/(\w+): (\w+)(?=[,}])/g)) {
    assert.match(script, new RegExp(`(async )?function ${value}\\(`), `loader «${key}» → ${value} is not a function`);
  }
});

test('leaving the text with Esc applies the formatting fixes (not the spelling guesses)', async () => {
  const ui = harness({items:[{...item, reference:null, label:null, revision:0}], total:1}, {}, {textCheck: body => body.text === 'клен 48 прийом'
    ? {issues:[{kind:'format', message:'Коми'}, {kind:'spelling', message:'Можливо', fixed_text:'інше'}], formatted:'Клен 48, прийом'}
    : {issues:[], formatted:body.text}});
  await ui.load();
  const {input} = editor(ui);
  input.value = 'клен 48 прийом';
  assert.equal(await ui.cards()[0].applyFormatting(), true);
  assert.equal(input.value, 'Клен 48, прийом');
  assert.equal(ui.cards()[0].dirty, true);
  assert.equal(await ui.cards()[0].applyFormatting(), false, 'already formatted — nothing to do');
});

test('analysis "show" links fit the archive search limit and find every record', () => {
  const analysis = fs.readFileSync(path.join(__dirname, '..', 'local_admin', 'js', '04-analysis.js'), 'utf8');
  const code = analysis.slice(analysis.indexOf('  const idsQuery ='), analysis.indexOf('\n', analysis.indexOf('  const idsQuery =')));
  const idsQuery = vm.runInNewContext(code.replace('const idsQuery =', '(').replace(/;\s*$/, '') + ')');
  const ids = Array.from({length: 20}, (_, i) => i.toString(16).padStart(2, '0').repeat(16));
  const query = idsQuery(ids);
  assert.ok(query.length <= 200, `query is ${query.length} chars`);
  assert.deepEqual(query.split(','), ids.map(id => id.slice(0, 8)));
});

test('doubtful order is sent and low-confidence unlabeled records are marked', async () => {
  const shaky = {...item, id:'d'.repeat(32), reference:null, data:{raw_text:'щось', final_text:'щось', whisper_segments:[{avg_logprob:-1.1}]}};
  const sure = {...item, id:'e'.repeat(32), reference:null, data:{raw_text:'клен', final_text:'клен', whisper_segments:[{avg_logprob:-0.2}]}};
  const ui = harness({items:[shaky, sure], total:2, confidence_low:-0.77});
  ui.$('recording-order').value = 'doubtful';
  await ui.load();
  assert.match(ui.urls[0], /order=doubtful/);
  assert.equal(descendants(ui.$('recordings')).filter(n => n.className === 'rec-doubt').length, 1);
});

test('roles the program fills into a fresh card are its starting state, not an unsaved draft', async () => {
  const fresh = {...item, id:'f'.repeat(32), reference:null, label:null, revision:0, reference_recipient:null, reference_sender:null,
                 data:{raw_text:'клен 48, я дуб 7, прийом', final_text:'клен 48, я дуб 7, прийом'}};
  const ui = harness({items:[fresh], total:1}, {}, {timers:true, callsigns:['клен 48', 'дуб 7'], roles:{recipient:'клен 48', sender:'дуб 7'}});
  await ui.load();
  ui.cards()[0].refreshHints();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(ui.cards()[0].dirty, false);
  assert.equal(ui.$('archive-drafts-nav').hidden, true);
});

test('"insert into list" adds an alias to its callsign line or a new line, without duplicates and without saving', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'local_admin', 'js', '07-recognition.js'), 'utf8');
  const start = source.indexOf('  function insertListLines(');
  const code = source.slice(start, source.indexOf('\n  function insertDictionaryLines(', start));
  const insertListLines = vm.runInNewContext(`(${code.trim().replace(/^function insertListLines/, 'function')})`);
  const aliases = {list:'callsign_aliases', lines:[{callsign:'клен', line:'клей'}, {callsign:'дуб', line:'дум'}, {callsign:'клен', line:'клін'}]};
  const result = insertListLines('клен: клін\n# вишня: вишна', aliases);
  assert.equal(result.text, 'клен: клін, клей\n# вишня: вишна\nдуб: дум');
  assert.equal(result.added, 2, 'клін уже є');
  assert.deepEqual(Array.from(result.highlights, range => result.text.slice(range.start, range.end)), ['клей', 'дуб: дум']);
  const noise = insertListLines('пау пау', {list:'short_garbage', lines:[{line:'Пау пау'}, {line:'кхм'}]});
  assert.equal(noise.text, 'пау пау\nкхм'); assert.equal(noise.added, 1);
  assert.deepEqual(Array.from(noise.highlights, range => noise.text.slice(range.start, range.end)), ['кхм']);
});

test('dictionary suggestion drafts merge into an existing callsign row and skip allowed codes', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'local_admin', 'js', '07-recognition.js'), 'utf8');
  const start = source.indexOf('  function insertDictionaryLines(');
  const code = source.slice(start, source.indexOf('\n  function phrasePatternEditor(', start));
  const insertDictionaryLines = vm.runInNewContext(`(${code.trim().replace(/^function insertDictionaryLines/, 'function')})`);
  const result = insertDictionaryLines('# клен: 15\nДуб: 6 8', {
    list: 'callsigns',
    lines: [
      { callsign: 'клен', number: '15' },
      { callsign: 'Дуб', number: '7' },
      { callsign: 'вишня', number: '9' },
    ],
  });
  assert.equal(result.text, 'клен: 15\nДуб: 6 8 7\nвишня: 9');
  assert.equal(result.added, 3, 'увімкнення вимкненого рядка теж залишається чернеткою');
  assert.deepEqual(Array.from(result.highlights, range => result.text.slice(range.start, range.end)), ['клен: 15', '7', 'вишня: 9']);
  const duplicate = insertDictionaryLines(result.text, {
    list: 'callsigns', lines: [{ callsign: 'Дуб', number: '7' }, { callsign: 'Дуб', number: '8' }],
  });
  assert.equal(duplicate.text, result.text);
  assert.equal(duplicate.added, 0);
  assert.deepEqual(Array.from(duplicate.highlights), []);
  const range = insertDictionaryLines('Дуб: 6-8', {
    list: 'callsigns', lines: [{ callsign: 'Дуб', number: '7' }],
  });
  assert.equal(range.text, 'Дуб: 6-8');
  assert.equal(range.added, 0);
});

test('editor highlight overlay marks every inserted fragment and tracks edits before them', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'local_admin', 'js', '07-recognition.js'), 'utf8');
  const start = source.indexOf('  function highlightDraftEditor(');
  const code = source.slice(start, source.indexOf('\n  // Позивний аліас додається', start));
  const node = (tagName, className = '', textContent = '') => ({
    tagName, className, textContent, children:[], listeners:{},
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    setAttribute() {},
    addEventListener(name, callback) { this.listeners[name] = callback; },
    dispatch(name) { this.listeners[name]?.(); },
    scrollTop:0, scrollLeft:0, scrollHeight:100,
    scrollIntoView(options) { this.scrollIntoViewOptions = options; },
  });
  const context = vm.createContext({el:node});
  vm.runInContext(`${code}\nglobalThis.highlightDraftEditor=highlightDraftEditor;`, context);
  const area = node('textarea'); area.value = 'old\nnew';
  const editor = context.highlightDraftEditor(area, [{start:4,end:7}]);
  const marks = () => editor.element.children[0].children.filter(child => child.tagName === 'mark');
  assert.equal(marks()[0].textContent, 'new');
  area.value = 'prefix\nold\nnew'; area.dispatch('input');
  assert.equal(marks()[0].textContent, 'new');
  assert.equal(marks().length, 1);
  editor.reveal();
  assert.equal(area.scrollTop, area.scrollHeight);
  assert.equal(editor.element.children[0].scrollTop, area.scrollTop);
  assert.equal(area.scrollIntoViewOptions.block, 'end');
  editor.clear();
  assert.equal(marks().length, 0);
});

test('saved short-noise entries are highlighted after the list refresh', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'local_admin', 'js', '07-recognition.js'), 'utf8');
  const start = source.indexOf('  function highlightExistingListLines(');
  const code = source.slice(start, source.indexOf('\n  function insertListLines(', start));
  const highlightExistingListLines = vm.runInNewContext(`(${code.trim().replace(/^function highlightExistingListLines/, 'function')})`);
  const text = 'пау пау\nКхм\nкхм-2';
  const ranges = highlightExistingListLines(text, {lines:[{line:'кхм'}]});
  assert.deepEqual(Array.from(ranges, range => text.slice(range.start, range.end)), ['Кхм']);
});

test('leaving the field without typing (tab switch, re-render) changes nothing', async () => {
  let checks = 0;
  const ui = harness({items:[{...item, reference:null, label:null, revision:0}], total:1}, {}, {
    textCheck: () => { checks++; return {issues:[{kind:'spelling', message:'Можливо', fixed_text:'вишня'}], formatted:null}; }});
  await ui.load();
  const {input} = editor(ui);
  input.value = 'вішня';
  input.listeners.focus(); input.listeners.blur();   // фокус прийшов і пішов, текст не міняли
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(input.value, 'вішня');
  assert.equal(ui.cards()[0].dirty, false);
});

test('time windows: explicit dates, next occurrence and crossing midnight', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'local_admin', 'js', '04-analysis.js'), 'utf8');
  const code = source.slice(source.indexOf('  function windowTimes('), source.indexOf('  function windowsEditor('));
  const ctx = vm.createContext({ Date });
  vm.runInContext(code + '\nglobalThis.windowTimes = windowTimes;', ctx);
  const [late, early] = ctx.windowTimes([["23:30", "00:30"], ["01:00", "02:00"]]);
  assert.equal(late[1] - late[0], 3600, 'через північ — година');
  assert.ok(early[0] >= late[1], 'наступне вікно — після попереднього');
  assert.equal(ctx.windowTimes([["01:00", "01:00"]]).length, 0);
});


test('refreshing archive disposes old audio before replacing cards',async()=>{
 const ui=harness({items:[item],total:1});await ui.load();
 const old=ui.cards()[0].audio;let stopped=0;old.pause=()=>stopped++;
 await ui.load();assert.equal(stopped,1);assert.notEqual(ui.cards()[0].audio,old);
});
