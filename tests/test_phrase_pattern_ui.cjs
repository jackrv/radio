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
  click(){this.listeners.click?.();}
  dispatch(name){this.listeners[name]?.();}
}

function patternEditor({request, note=()=>{}}={}) {
  const source=fs.readFileSync(path.join(__dirname,'../local_admin/js/07-recognition.js'),'utf8');
  const start=source.indexOf('  function phrasePatternEditor(');
  const end=source.indexOf('\n  async function texts()',start);
  const context=vm.createContext({el:(tag,className,text)=>new Element(tag,className,text),request,note});
  vm.runInContext(source.slice(start,end)+'\nglobalThis.tools={editor:phrasePatternEditor};',context);
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
