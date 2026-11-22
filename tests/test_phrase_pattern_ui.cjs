const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');

class Element {
  constructor(tag, className='', text='') {
    this.tagName=tag;this.className=className;this.textContent=text;this.children=[];this.listeners={};this.attributes={};
    this.value='';this.checked=false;
  }
  append(...children){this.children.push(...children);}
  replaceChildren(...children){this.children=children;}
  addEventListener(name,callback){this.listeners[name]=callback;}
  setAttribute(name,value){this.attributes[name]=value;}
  querySelectorAll(tag){
    const found=[];
    const visit=node=>{for(const child of node.children){if(child?.tagName===tag)found.push(child);if(child?.children)visit(child);}};
    visit(this);return found;
  }
  focus(){this.focused=true;}
  scrollIntoView(options){this.scrolled=options;}
  setSelectionRange(start,end){this.selectionStart=start;this.selectionEnd=end;}
  dispatchEvent(event){this.listeners[event.type]?.(event);}
  click(){this.listeners.click?.();}
  dispatch(name){this.listeners[name]?.();}
}

function patternEditor({request, note=()=>{}, frames=null}={}) {
  const source=fs.readFileSync(path.join(__dirname,'../local_admin/js/07-recognition.js'),'utf8');
  const start=source.indexOf('  function phrasePatternEditor(');
  const end=source.indexOf('\n  async function texts()',start);
  const context=vm.createContext({el:(tag,className,text)=>new Element(tag,className,text),request,note,Event,
    requestAnimationFrame:frames?callback=>frames.push(callback):undefined});
  vm.runInContext(source.slice(source.indexOf('  function toLines('),source.indexOf('  async function addCallsign(')),context);
  vm.runInContext(source.slice(start,end)+'\nglobalThis.tools={editor:phrasePatternEditor,rowListView,rowDraftOptions};',context);
  return context.tools;
}

for(const generic of [true,false])test(`add uses search text without saving and preserves safe enabled state (generic=${generic})`,()=>{
 const tools=patternEditor(),save=new Element('button','','Зберегти');let saves=0;
 save.addEventListener('click',()=>saves++);
 const editor=tools.editor([],[],generic,save);
 const search=editor.element.querySelectorAll('input').find(input=>input.className==='pattern-search');
 search.value=generic?'  злива 42 42  ':'  {П}, прийом  ';search.dispatch('input');
 editor.element.querySelectorAll('button').find(button=>button.textContent.startsWith('＋ Додати')).click();
 const expected=generic?'злива 42 42':'{П}, прийом';
 assert.deepEqual(JSON.parse(JSON.stringify(editor.entries())),[{raw_line:expected,enabled:generic}]);
 assert.equal(search.value,'');assert.equal(editor.hasBlankRows(),false);assert.equal(editor.hasChanges(),true);
 const field=editor.element.querySelectorAll('input').find(input=>input.className==='pattern-line');
 assert.equal(field.focused,true);assert.equal(saves,0);
 assert.ok(editor.element.children[0].children.includes(save));
 save.click();assert.equal(saves,1);
});

test('reviewing a pattern suggestion prepares it as an unsaved enable draft',async()=>{
  const source=fs.readFileSync(path.join(__dirname,'../local_admin/js/05-overview.js'),'utf8');
  const start=source.indexOf('  function openTarget(');
  const end=source.indexOf('  let attentionAt',start);
  const calls=[], stored={};
  const context=vm.createContext({
    localStorage:{setItem:(key,value)=>{stored[key]=value;}},
    loaded:{delete:name=>calls.push(['delete',name])},
    show:name=>calls.push(['show',name]),
    pendingPatternEnable:[],
  });
  vm.runInContext(source.slice(start,end)+'\nglobalThis.reviewSuggestion=reviewSuggestion;',context);
  await context.reviewSuggestion({
    kind:'pattern', target:'recordings:abcd1234',
    lines:[{list:'phrase_patterns',line:'{П}, я {П}'},{list:'aliases',line:'не-використовувати'}],
  });
  assert.deepEqual(Array.from(context.pendingPatternEnable),['{П}, я {П}']);
  assert.equal(stored['radio345:text-list'],'phrase_patterns');
  assert.deepEqual(calls,[['delete','texts'],['show','texts']]);
});

test('a pattern-list target is handled as a pattern suggestion even if kind is missing',async()=>{
  const source=fs.readFileSync(path.join(__dirname,'../local_admin/js/05-overview.js'),'utf8');
  const start=source.indexOf('  function openTarget(');
  const end=source.indexOf('  let attentionAt',start);
  const calls=[];
  const context=vm.createContext({
    localStorage:{setItem(){}},
    loaded:{delete:name=>calls.push(['delete',name])},
    show:name=>calls.push(['show',name]),
    pendingPatternEnable:[],
  });
  vm.runInContext(source.slice(start,end)+'\nglobalThis.reviewSuggestion=reviewSuggestion;',context);
  await context.reviewSuggestion({
    target:'texts:phrase_patterns',
    lines:[{list:'phrase_patterns',line:'{П}, я {П}'}],
  });
  assert.deepEqual(Array.from(context.pendingPatternEnable),['{П}, я {П}']);
  assert.deepEqual(calls,[['delete','texts'],['show','texts']]);
});

test('a missing pattern line produces an error without navigating to an empty list',async()=>{
  const calls=[],notices=[];
  const source=fs.readFileSync(path.join(__dirname,'../local_admin/js/05-overview.js'),'utf8');
  const start=source.indexOf('  function openTarget(');
  const end=source.indexOf('  let attentionAt',start);
  const context=vm.createContext({
    note:text=>notices.push(text),
    localStorage:{setItem(){}},
    loaded:{delete:name=>calls.push(['delete',name])},
    show:name=>calls.push(['show',name]),
    pendingPatternEnable:[],
  });
  vm.runInContext(source.slice(start,end)+'\nglobalThis.reviewSuggestion=reviewSuggestion;',context);
  await context.reviewSuggestion({kind:'pattern',target:'texts:phrase_patterns',lines:[]});
  assert.deepEqual(calls,[]);
  assert.match(notices[0],/немає рядка шаблону/);
});

test('reviewing alias and noise suggestions opens the matching editor with an unsaved draft',async()=>{
  const source=fs.readFileSync(path.join(__dirname,'../local_admin/js/05-overview.js'),'utf8');
  const start=source.indexOf('  function openTarget(');
  const end=source.indexOf('  let attentionAt',start);
  for (const [kind,list,line] of [
    ['alias','callsign_aliases',{callsign:'клен',line:'клей'}],
    ['noise','short_garbage',{line:'пау пау'}],
  ]) {
    const calls=[],stored={},requests=[];
    const context=vm.createContext({
      localStorage:{setItem:(key,value)=>{stored[key]=value;}},
      loaded:{delete:name=>calls.push(['delete',name])},
      show:name=>calls.push(['show',name]),
      request:(...args)=>requests.push(args),
      pendingListInsert:null,
    });
    vm.runInContext(source.slice(start,end)+'\nglobalThis.reviewSuggestion=reviewSuggestion;',context);
    await context.reviewSuggestion({kind,target:`texts:${list}`,lines:[{list,...line}]});
    assert.deepEqual(JSON.parse(JSON.stringify(context.pendingListInsert)),{list,lines:[{list,...line}]});
    assert.equal(stored['radio345:text-list'],list);
    assert.deepEqual(calls,[['delete','texts'],['show','texts']]);
    assert.deepEqual(requests,[]);
  }
});

test('reviewing a dictionary suggestion stages its code in the callsign dictionary editor',async()=>{
  const source=fs.readFileSync(path.join(__dirname,'../local_admin/js/05-overview.js'),'utf8');
  const start=source.indexOf('  function openTarget(');
  const end=source.indexOf('  let attentionAt',start);
  const calls=[],stored={},requests=[];
  const context=vm.createContext({
    localStorage:{setItem:(key,value)=>{stored[key]=value;}},
    loaded:{delete:name=>calls.push(['delete',name])},
    show:name=>calls.push(['show',name]),
    request:(...args)=>requests.push(args),
    pendingDictionaryInsert:null,
    dictionaryKind:'commands',
  });
  vm.runInContext(source.slice(start,end)+'\nglobalThis.reviewSuggestion=reviewSuggestion;',context);
  await context.reviewSuggestion({
    kind:'dictionary',target:'dictionaries',
    lines:[{list:'callsigns',callsign:'клен',number:'17'}],
  });
  assert.deepEqual(JSON.parse(JSON.stringify(context.pendingDictionaryInsert)),{
    list:'callsigns',lines:[{list:'callsigns',callsign:'клен',number:'17'}],
  });
  assert.equal(context.dictionaryKind,'callsigns');
  assert.equal(stored['radio345:dictionary'],'callsigns');
  assert.deepEqual(calls,[['delete','dictionaries'],['show','dictionaries']]);
  assert.deepEqual(requests,[]);
});

test('saved enabled patterns are not marked as draft changes',()=>{
  const editor=patternEditor().editor([{raw_line:'{П}, прийом',enabled:true}]);
  const badge=editor.rows.querySelectorAll('span').find(item=>item.className.includes('pattern-draft'));
  assert.equal(badge.hidden,true);
});

test('a recommended disabled pattern is enabled locally as an unsaved draft',()=>{
  const editor=patternEditor().editor([
    {raw_line:'{П}, я {П}',enabled:false},
    {raw_line:'{П}, я {П}, прийом',enabled:false},
    {raw_line:'{П}, я {П}, прийом',enabled:true},
  ],['{П}, я {П}']);
  const disabledGroup=editor.rows.querySelectorAll('details')[0];
  assert.equal(Boolean(disabledGroup.open),false);
  assert.equal(editor.rows.querySelectorAll('input').find(input=>input.type==='search').value,'');
  const rows=editor.rows.querySelectorAll('div').filter(item=>item.className.split(/\s+/).includes('phrase-pattern-row'));
  const line=rows.find(row=>row.querySelectorAll('input').find(input=>input.type==='text')?.value==='{П}, я {П}');
  assert.ok(line);
  assert.equal(line.querySelectorAll('input').find(input=>input.type==='checkbox').checked,true);
  assert.equal(line.querySelectorAll('span').find(item=>item.className.includes('pattern-draft')).textContent,'Тимчасово увімкнено');
  assert.deepEqual(JSON.parse(JSON.stringify(editor.entries())).map(entry=>entry.enabled),[true,false,true]);
  assert.equal(editor.rows.querySelectorAll('summary')[0].textContent,'Вимкнені (1)');
  assert.equal(editor.hasChanges(),true);
});

test('normal template search continues to filter by substring',()=>{
  const editor=patternEditor().editor([
    {raw_line:'{П}, я {П}',enabled:false},
    {raw_line:'{П}, я {П}, прийом',enabled:false},
  ]);
  const search=editor.rows.querySelectorAll('input').find(input=>input.type==='search');
  search.value='{П}, я {П}';
  search.dispatch('input');
  assert.equal(editor.rows.querySelectorAll('input').filter(input=>input.type==='text').length,2);
});

test('phrase-pattern editor edits, toggles, adds disabled rows, and removes entries',()=>{
  const editor=patternEditor().editor([{raw_line:'{П}, прийом',enabled:false}]);
  let changes=0;editor.onChange(()=>changes++);
  const inputs=()=>editor.rows.querySelectorAll('input');
  const textFields=()=>inputs().filter(input=>input.type==='text');
  const draftBadges=()=>editor.rows.querySelectorAll('span').filter(item=>item.className.includes('pattern-draft'));
  assert.equal(Boolean(editor.rows.querySelectorAll('details')[0].open),false);
  assert.equal(draftBadges()[0].hidden,true);
  textFields()[0].value='{П}, я {П}, прийом';textFields()[0].dispatch('input');
  assert.equal(editor.entries()[0].raw_line,'{П}, я {П}, прийом');
  assert.equal(draftBadges()[0].textContent,'Чернетка');

  const search=inputs().find(input=>input.type==='search');
  search.value='неіснуючий шаблон';search.dispatch('input');
  assert.equal(textFields().length,0);
  search.value='';search.dispatch('input');
  const checkbox=inputs().find(input=>input.type==='checkbox');checkbox.checked=true;checkbox.dispatch('change');
  assert.equal(editor.entries()[0].enabled,true);
  assert.equal(draftBadges()[0].textContent,'Тимчасово увімкнено');

  editor.element.querySelectorAll('button').find(button=>button.textContent==='＋ Додати шаблон').click();
  assert.equal(editor.hasBlankRows(),true);
  assert.equal(editor.rows.querySelectorAll('details')[0].open,true);
  const added=textFields().at(-1);added.value='{П}, прийнято';added.dispatch('input');
  assert.equal(editor.hasBlankRows(),false);
  assert.equal(draftBadges().find(item=>item.textContent==='Чернетка')?.hidden,false);
  assert.deepEqual(JSON.parse(JSON.stringify(editor.entries())),[
    {raw_line:'{П}, я {П}, прийом',enabled:true},
    {raw_line:'{П}, прийнято',enabled:false},
  ]);
  editor.rows.querySelectorAll('button').find(button=>button.attributes['aria-label']==='Видалити шаблон 2').click();
  assert.equal(editor.entries().length,1);
  assert.ok(changes>=4);
});

const editorRows=editor=>editor.rows.querySelectorAll('div').filter(n=>n.className.split(/\s+/).includes('phrase-pattern-row'));
const fieldOf=row=>row.querySelectorAll('input').find(n=>n.type==='text');

test('inserted row is visibly dirty against saved baseline and reveal targets it',()=>{
 const tools=patternEditor(),before=[{raw_line:'збережене',enabled:true}];
 const editor=tools.editor([...before,{raw_line:'новий шелест',enabled:true}],[],true,null,null,{baselineEntries:before});
 const rows=editorRows(editor);
 assert.equal(rows[0].className,'phrase-pattern-row');
 assert.match(rows[1].className,/is-draft/);
 assert.equal(rows[1].querySelectorAll('span').find(n=>n.className.startsWith('pattern-draft')).textContent,'Чернетка');
 assert.equal(editor.hasChanges(),true);editor.reveal();
 assert.equal(fieldOf(rows[1]).focused,true);assert.equal(fieldOf(rows[1]).scrolled.block,'nearest');
 assert.equal(fieldOf(rows[0]).focused,undefined);
});

test('merged alias highlights only modified row and selects the actual inserted variant',()=>{
 const tools=patternEditor(),before='Клен: клін\nДуб: дубб',text='Клен: клін, клинок\nДуб: дубб';
 const options=tools.rowDraftOptions(before,text,[{start:12,end:18}]);
 const editor=tools.editor([{raw_line:'Клен: клін, клинок',enabled:true},{raw_line:'Дуб: дубб',enabled:true}],[],true,null,null,options);
 const rows=editorRows(editor);assert.match(rows[0].className,/is-draft/);assert.equal(rows[1].className,'phrase-pattern-row');
 editor.reveal();const field=fieldOf(rows[0]);
 assert.equal(field.value.slice(field.selectionStart,field.selectionEnd),'клинок');
 const search=editor.rows.querySelectorAll('input').find(n=>n.type==='search');search.value='Дуб';search.dispatch('input');
 search.value='';search.dispatch('input');assert.match(editorRows(editor)[0].className,/is-draft/);
});

test('splitting alias rows never marks unrelated saved rows as new',()=>{
 const before=[{raw_line:'Клен: клін, клинок',enabled:true},{raw_line:'Дуб: дубб, дубик',enabled:true},{raw_line:'Явір: явр',enabled:true}];
 const entries=[{raw_line:'Клен: клін',enabled:true},{raw_line:'Клен: клинок',enabled:false},
   {raw_line:'Дуб: дубб',enabled:true},{raw_line:'Дуб: дубик',enabled:false},before[2]];
 const editor=patternEditor().editor(entries,[],true,null,null,{baselineEntries:before});
 const rows=editorRows(editor);
 assert.equal(rows.filter(n=>n.className.includes('is-draft')).length,4);
 assert.equal(rows.find(n=>fieldOf(n).value==='Явір: явр').className,'phrase-pattern-row');
});

test('saved candidate is distinguished from an unsaved proposal and picks only its alias row',()=>{
 const tools=patternEditor(),text='Клен: клін\nКлен: клинок';
 const options=tools.rowDraftOptions(text,text,[],true,{list:'callsign_aliases',lines:[{callsign:'Клен',line:'клинок'}]});
 const entries=text.split('\n').map(raw_line=>({raw_line,enabled:true}));
 const editor=tools.editor(entries,[],true,null,null,options),rows=editorRows(editor);
 assert.equal(rows[0].className,'phrase-pattern-row');assert.match(rows[1].className,/is-recent/);
 assert.equal(rows[1].querySelectorAll('span').find(n=>n.className.startsWith('pattern-draft')).textContent,'Щойно збережено');
 assert.equal(editor.hasChanges(),false);editor.reveal();assert.equal(fieldOf(rows[1]).focused,true);
});

test('existing duplicate is visible without creating an unsaved change',()=>{
 const tools=patternEditor(),text='збережене\nновий шелест';
 const options=tools.rowDraftOptions(text,text,[],false,{list:'short_garbage',lines:[{line:'новий шелест'}]});
 const editor=tools.editor(text.split('\n').map(raw_line=>({raw_line,enabled:true})),[],true,null,null,options);
 const row=editorRows(editor)[1];assert.match(row.className,/is-recent/);
 assert.equal(row.querySelectorAll('span').find(n=>n.className.startsWith('pattern-draft')).textContent,'Вже збережено');assert.equal(editor.hasChanges(),false);
});

test('visible row adapter keeps baseline through input and clears insertion on undo',()=>{
 const tools=patternEditor(),area=new Element('textarea');area.value='збережене\nновий шелест';
 const options=tools.rowDraftOptions('збережене',area.value,[{start:10,end:22}]);
 const host=tools.rowListView(area,'Короткий шум',null,null,options);
 assert.equal(area.hidden,true);assert.equal(host.querySelectorAll('div').filter(n=>n.className.includes('is-draft')).length,1);
 host.clearHighlight();area.value='збережене';area.dispatch('input');
 assert.equal(host.querySelectorAll('div').filter(n=>n.className.includes('is-draft')||n.className.includes('is-recent')).length,0);
});

test('deferred reveal respects navigation away and prefers draft over existing suggestion',()=>{
 const frames=[],tools=patternEditor({frames});
 const before=[{raw_line:'вже є',enabled:true}],entries=[...before,{raw_line:'нова чернетка',enabled:true}];
 const editor=tools.editor(entries,[],true,null,null,{baselineEntries:before,spotlightLines:['вже є']});
 editor.reveal();frames.shift()();assert.equal(fieldOf(editorRows(editor)[1]).focused,undefined);
 frames.shift()();assert.equal(fieldOf(editorRows(editor)[1]).focused,true);
 const other=tools.editor(entries,[],true,null,null,{baselineEntries:before});
 other.reveal();frames.shift()();other.element.isConnected=false;frames.shift()();
 assert.equal(fieldOf(editorRows(other)[1]).focused,undefined);
});

test('row numbers include disabled entries and stay stable when searching',()=>{
 const tools=patternEditor(),editor=tools.editor([
  {raw_line:'перший',enabled:false},{raw_line:'другий',enabled:true},{raw_line:'третій',enabled:true},
 ],[],true);
 const labels=()=>editor.element.querySelectorAll('span').filter(n=>n.className==='editor-row-number').map(n=>n.textContent);
 assert.deepEqual(labels(),['2','3','1']);
 const search=editor.element.querySelectorAll('input').find(n=>n.className==='pattern-search');
 search.value='третій';search.dispatch('input');assert.deepEqual(labels(),['3']);
 assert.equal(editor.revealRow(1,'перший'),true);assert.equal(search.value,'');
 const row=editorRows(editor).find(row=>fieldOf(row).value==='перший');
 assert.match(row.className,/is-targeted/);assert.equal(fieldOf(row).focused,true);
 assert.ok(fieldOf(row).scrolled);assert.equal(editor.element.querySelectorAll('details')[0].open,true);
 assert.equal(editor.hasChanges(),false);
});

test('blank row never shifts validation numbers and editing it updates visible numbering',()=>{
 const tools=patternEditor(),editor=tools.editor([{raw_line:'перший',enabled:true}],[],true);
 editor.element.querySelectorAll('button').find(b=>b.textContent.startsWith('＋ Додати')).click();
 const row=editorRows(editor).find(row=>fieldOf(row).value==='');
 const number=row.querySelectorAll('span').find(n=>n.className==='editor-row-number');
 assert.equal(number.textContent,'—');
 fieldOf(row).value='другий';fieldOf(row).dispatch('input');assert.equal(number.textContent,'2');
 assert.equal(editor.revealRow(2,'другий'),true);
});

test('stale row warning never jumps to a different record after deletion',()=>{
 const notices=[],tools=patternEditor({note:text=>notices.push(text)});
 const editor=tools.editor([{raw_line:'перший',enabled:true},{raw_line:'другий',enabled:true}],[],true);
 editorRows(editor)[0].querySelectorAll('button')[0].click();
 assert.equal(editor.revealRow(1,'перший'),false);assert.equal(notices.length,1);
 assert.equal(fieldOf(editorRows(editor)[0]).focused,undefined);
 assert.equal(editor.revealRow(1,'другий'),true);
});

test('numbered errors expose an accessible jump while general errors remain plain text',()=>{
 const source=fs.readFileSync(path.join(__dirname,'../local_admin/js/07-recognition.js'),'utf8');
 const context=vm.createContext({el:(tag,className,text)=>new Element(tag,className,text)});
 vm.runInContext(source.slice(source.indexOf('  function rowIssue('),source.indexOf('  // Перевірка рядків під час набору:'))+'\nglobalThis.issue=rowIssue;',context);
 const calls=[],jump=(...args)=>calls.push(args),entries=[{raw_line:'синтетичний текст 42',enabled:true}];
 const item=context.issue('li','error','Рядок 1: фраза з числом',jump,entries);
 const button=item.querySelectorAll('button')[0];assert.equal(button.attributes['aria-label'],'Перейти до рядка 1: синтетичний текст 42');
 button.click();assert.deepEqual(calls,[[1,'синтетичний текст 42']]);
 assert.equal(context.issue('li','error','Загальна помилка',jump,entries).textContent,'Загальна помилка');
 assert.equal(context.issue('li','error','Рядок 9: старе',jump,entries).querySelectorAll('button').length,0);
});

test('editing invalidates in-flight live validation before the next debounce fires',async()=>{
 const source=fs.readFileSync(path.join(__dirname,'../local_admin/js/07-recognition.js'),'utf8');
 const timers=new Map(),pending=[];let next=0;
 const context=vm.createContext({el:(tag,className,text)=>new Element(tag,className,text),
  setTimeout:callback=>{timers.set(++next,callback);return next;},clearTimeout:id=>timers.delete(id),
  request:()=>new Promise(resolve=>pending.push(resolve))});
 vm.runInContext(source.slice(source.indexOf('  function rowIssue('),source.indexOf('  // Один словник за раз:'))+'\nglobalThis.check=liveCheck;',context);
 const area=new Element('textarea'),box=new Element('ul');let entries=[{raw_line:'перша версія',enabled:true}];
 context.check(area,box,'/check',[],()=>entries,['input'],()=>{});
 area.dispatch('input');const oldCheck=timers.get(1)();
 entries=[{raw_line:'друга версія',enabled:true}];area.dispatch('input');
 pending.shift()({errors:['Рядок 1: стара помилка']});await oldCheck;
 assert.equal(box.children.length,0);
 const freshCheck=timers.get(2)();pending.shift()({errors:['Рядок 1: нова помилка']});await freshCheck;
 assert.equal(box.querySelectorAll('button')[0].attributes['aria-label'],'Перейти до рядка 1: друга версія');
});
