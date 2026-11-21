const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = require('./panel_source.cjs');

function harness({inner=false}={}) {
  const listeners = {}, panels = {}, fields = {}, frames = [], scrolls = [], loads = [];
  let height = 4000;
  const window = {scrollY:0, scrollTo({top}) { this.scrollY = Math.min(top, height); scrolls.push(this.scrollY); }};
  const main = {scrollTop:0,scrollTo({top}){this.scrollTop=Math.min(top,height);scrolls.push(this.scrollTop);}};
  const body = {};
  const document = {body, activeElement:body, addEventListener(name, callback) {listeners[name] = callback;},
    querySelector(selector){return inner && selector==='main'?main:null;},
    querySelectorAll(selector) {return selector === '[data-panel]' ? Object.values(panels) : [];}};
  for (const name of ['overview','recordings','dictionaries','codes']) {
    panels[name] = {dataset:{panel:name}, hidden:name!=='recordings'};
    fields[name] = {value:'незбережений еталон', isConnected:true, selectionStart:4, selectionEnd:9,
      selectionDirection:'backward', scrollTop:70, top:1300,
      matches(){return true;}, closest(){return panels[name];},
      getBoundingClientRect(){return {top:this.top-(inner?main.scrollTop:window.scrollY)};},
      focus(options){assert.equal(options.preventScroll,true);document.activeElement=this;listeners.focusin({target:this});},
      setSelectionRange(start,end,direction){Object.assign(this,{selectionStart:start,selectionEnd:end,selectionDirection:direction});}};
  }
  const nodes = {'view-title':{}, preview:{src:'about:blank'}, tabs:{replaceChildren(){}, append(){}}};
  const reviewCards = [{card:{contains(field){return field===fields.recordings;}}, refreshHints(){loads.push('hints');}}];
  const context = vm.createContext({window,document,requestAnimationFrame:cb=>frames.push(cb),
    $:id=>nodes[id], el:()=>({setAttribute(){},addEventListener(){}}),
    view:'recordings',loaded:new Set(['recordings','dictionaries']),reviewCards,
    location:{pathname:'/',search:''},history:{pushState(){},replaceState(){}},
    roles(){nodes.preview.src='/preview/viewer/';loads.push('preview');},
    note(){}, loaders:{preview:async()=>{nodes.preview.src='/preview/viewer/';loads.push('preview');}, codes:async()=>{}, dictionaries:async()=>{loads.push('dictionaries');}},
  });
  const block = source.slice(source.indexOf('  const titles ='), source.indexOf('  for (const b of document.querySelectorAll("#nav button")) b.addEventListener'));
  vm.runInContext(block+'\nglobalThis.navigate=show;globalThis.states=stateByView;',context);
  return {context, panels, fields, frames, scrolls, loads, document, window, main,
    flush(){while(frames.length)frames.shift()();},
    focus(name){document.activeElement=fields[name];listeners.focusin({target:fields[name]});},
    blur(name){listeners.focusout({target:fields[name]});document.activeElement=body;},
    setHeight(value){height=value;}};
}

const tick = async () => {for(let i=0;i<12;i++)await Promise.resolve();};

test('inner page scrolling restores editor position without scrolling the document',()=>{
 const h=harness({inner:true});h.main.scrollTop=1000;h.focus('recordings');h.blur('recordings');
 h.context.navigate('dictionaries');h.flush();h.fields.recordings.top=1400;
 h.context.navigate('recordings');h.flush();
 assert.equal(h.main.scrollTop,1100);assert.equal(h.window.scrollY,0);
 assert.equal(h.document.activeElement,h.fields.recordings);assert.equal(h.fields.recordings.selectionStart,4);
});

test('switching tabs restores draft, caret, selection and textarea scroll after navigation steals focus', () => {
  const h=harness();h.window.scrollY=1000;h.focus('recordings');h.blur('recordings');
  h.context.navigate('dictionaries');h.flush();
  h.fields.recordings.selectionStart=0;h.fields.recordings.selectionEnd=0;h.fields.recordings.scrollTop=0;
  // Layout above the card changed while the dictionary was edited.
  h.fields.recordings.top=1400;
  h.context.navigate('recordings');h.flush();
  assert.equal(h.window.scrollY,1100);
  assert.equal(h.document.activeElement,h.fields.recordings);
  assert.equal(h.fields.recordings.value,'незбережений еталон');
  assert.equal(h.fields.recordings.selectionStart,4);assert.equal(h.fields.recordings.selectionEnd,9);
  assert.equal(h.fields.recordings.selectionDirection,'backward');assert.equal(h.fields.recordings.scrollTop,70);
  assert.deepEqual(h.loads,['hints']);
});

test('scroll restoration waits for asynchronous panel loading', async () => {
  const h=harness();let finish;
  h.context.states.codes={y:800};h.setHeight(0);
  h.context.loaders.codes=()=>new Promise(resolve=>{finish=()=>{h.setHeight(4000);resolve();};});
  h.context.navigate('codes');await tick();h.flush();
  assert.equal(h.scrolls.length,0);
  finish();await tick();h.flush();assert.equal(h.window.scrollY,800);
});

test('late loads and queued frames cannot steal scroll after another navigation', async () => {
  const h=harness();let finish;
  h.context.loaders.codes=()=>new Promise(resolve=>{finish=resolve;});
  h.context.navigate('codes');await tick();
  h.context.navigate('recordings');h.flush();h.window.scrollY=520;
  finish();await tick();h.flush();assert.equal(h.window.scrollY,520);
});

test('disconnected controls are not focused after a panel redraw', () => {
  const h=harness();h.window.scrollY=900;h.focus('recordings');
  h.context.navigate('dictionaries');h.flush();h.fields.recordings.isConnected=false;
  h.context.navigate('recordings');h.flush();assert.equal(h.window.scrollY,900);
  assert.notEqual(h.document.activeElement,h.fields.dictionaries);
});

function dictionaryHarness({duplicate=false,dirty=false,conflict=false}={}) {
  const calls=[], loaded=new Set(['dictionaries']);
  const entries=[{raw_line:'береза: 56',enabled:true},{raw_line:'клен: 15',enabled:false}];
  if(duplicate)entries.push({raw_line:'Береза: 57',enabled:true});
  const editor={value:dirty?'чернетка':'збережене',dataset:{savedValue:'збережене'}};
  const context=vm.createContext({loaded,note(){},document:{querySelector(){return editor;}},
    request:async(url,method,body)=>{
      calls.push({url,method,body});
      if(method==='PUT'){if(conflict)throw new Error('409');return {};}
      return {callsigns:{entries,version:'current-version'}};
    }});
  const block=source.slice(source.indexOf('  async function addCallsign('),source.indexOf('  // ---------- Словники позивних, команд і Telegram ----------'));
  vm.runInContext(block+'\nglobalThis.add=addCallsign;',context);
  return {context,calls,entries,editor,loaded};
}

test('quick add preserves existing rows and uses the latest dictionary version',async()=>{
  const h=dictionaryHarness();await h.context.add('береза: 57');
  assert.equal(h.calls.length,2);assert.equal(h.calls[1].body.expected_version,'current-version');
  assert.deepEqual(Array.from(h.calls[1].body.entries.slice(0,2)),h.entries);
  assert.equal(h.calls[1].body.entries[2].raw_line,'береза: 57');
  assert.equal(h.loaded.has('dictionaries'),false);
});

test('quick add does not duplicate an enabled identical entry',async()=>{
  const h=dictionaryHarness({duplicate:true});await h.context.add('береза: 57');assert.equal(h.calls.length,1);
});

test('quick add keeps an unsaved dictionary editor attached',async()=>{
  const h=dictionaryHarness({dirty:true});await h.context.add('береза: 57');
  assert.equal(h.loaded.has('dictionaries'),true);assert.equal(h.editor.value,'чернетка');
});

test('dictionary conflict is surfaced without retry or draft replacement',async()=>{
  const h=dictionaryHarness({conflict:true,dirty:true});
  await assert.rejects(h.context.add('береза: 57'),/409/);
  assert.equal(h.calls.length,2);assert.equal(h.editor.value,'чернетка');assert.equal(h.loaded.has('dictionaries'),true);
});


test('preview loads on its tab and releases its iframe on leaving',async()=>{
  const h=harness();h.context.navigate('preview');await tick();h.flush();
  assert.equal(h.context.$('preview').src,'/preview/viewer/');
  h.context.navigate('dictionaries');h.flush();assert.equal(h.context.$('preview').src,'about:blank');
  h.context.navigate('preview');h.flush();assert.equal(h.context.$('preview').src,'/preview/viewer/');
});

test('assembled panel script is valid JavaScript and matches what the server sends', () => {
  new vm.Script(source);
  const python = path.join(__dirname, '../.venv/bin/python');
  if (!fs.existsSync(python)) return;   // CI-завдання JavaScript без Python-оточення: лише синтаксис
  const { execFileSync } = require('node:child_process');
  const served = execFileSync(python, ['-c', 'from api.local_dashboard import panel_script; import sys; sys.stdout.write(panel_script())'],
    { cwd: path.join(__dirname, '..'), encoding: 'utf8' });
  assert.equal(served, source);
});
