const {test}=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../local_admin/js/14-mentions.js'),'utf8');
function setup(){
 class E{constructor(){this.dataset={};this.style={};this.children=[];this.listeners={};this.value='';this.textContent='';}append(...v){this.children.push(...v);}replaceChildren(...v){this.children=v;}setAttribute(){}addEventListener(n,f){this.listeners[n]=f;}setCustomValidity(message){this.validationMessage=message;}}
 const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,new E());return nodes.get(id);},pending=[],storage=new Map([['radio345:mention-terms','Клен']]);
 const c=vm.createContext({el:(tag,cls,text)=>Object.assign(new E(),{textContent:text||''}),$,URLSearchParams,
   document:{querySelector:()=>new E()},localStorage:{getItem:k=>storage.get(k),setItem:(k,v)=>storage.set(k,v)},action:f=>f(),loaders:{},loaded:new Set(),show(){},archiveSelection:'',
   request:(...args)=>new Promise((resolve,reject)=>pending.push({resolve,reject,args}))});
 vm.runInContext(source.slice(0,source.indexOf('  const attentionResult'))+'\nglobalThis.ui={search:runMentions,refresh:loaders.mentions,feedback:mentionFeedback,results:mentionResults,archive:mentionArchive,order:mentionOrder,terms:mentionTerms,pills:mentionPills,since:mentionSince,until:mentionUntil,timeValue:mentionTimeValue,dialValue:mentionDialValue,selection:()=>mentionSelection};',c);
 return {ui:c.ui,pending,$,storage};
}
const result=(selection,text)=>({selection,total:1,items:[{id:selection,created:1,text,reviewed:false,mentions:['Клен']}]});

test('dial distinguishes midnight, noon and inner 24-hour ring and selects exact minutes',()=>{
 const {ui}=setup();
 assert.equal(ui.dialValue('hour',112,16,224),12);
 assert.equal(ui.dialValue('hour',112,46,224),0);
 assert.equal(ui.dialValue('hour',112,208,224),6);
 assert.equal(ui.dialValue('hour',112,178,224),18);
 assert.equal(ui.dialValue('hour',112,112,224),null);
 for(let minute=0;minute<60;minute++){
  const angle=minute*2*Math.PI/60;
  assert.equal(ui.dialValue('minute',112+96*Math.sin(angle),112-96*Math.cos(angle),224),minute);
 }
});

test('manual time accepts exact minutes and compact numeric entry but rejects out-of-day values',()=>{
 const x=setup();
 for(const [input,expected] of [['9:26','09:26'],['0926','09:26'],['926','09:26'],['00:00','00:00'],['23:59','23:59']])assert.equal(x.ui.timeValue(input),expected);
 for(const input of ['24:00','12:60','2560','-1:20','12:3','','9'])assert.equal(x.ui.timeValue(input),'');
});

test('manual validation recovers when a valid preset replaces invalid time',()=>{
 const x=setup();x.ui.since.time.value='25:10';x.ui.since.time.listeners.input();
 assert.match(x.ui.since.time.validationMessage,/23:59/);assert.equal(x.ui.since.value,'');
 x.ui.since.value='2026-11-21T09:26';
 assert.equal(x.ui.since.time.validationMessage,'');assert.equal(x.ui.since.value,'2026-11-21T09:26');
 x.ui.since.time.value='0927';x.ui.since.time.listeners.blur();
 assert.equal(x.ui.since.time.value,'09:27');
});
test('late response from an older search cannot overwrite the current selection',async()=>{
 const x=setup(),first=x.ui.search(true),second=x.ui.search(true);
 x.pending[1].resolve(result('new','Нове'));await second;
 x.pending[0].resolve(result('old','Старе'));await first;
 assert.equal(x.ui.selection(),'new');assert.equal(x.ui.results.children.length,1);
 assert.equal(x.ui.results.children[0].children[1].children[0].textContent,'Нове');
});
test('an older failed search cannot replace current success and archive keeps chosen order',async()=>{
 const x=setup();x.ui.order.value='new';const first=x.ui.search(true),second=x.ui.search(true);
 x.pending[1].resolve(result('latest','Знайдено'));await second;
 const text=x.ui.feedback.textContent;x.pending[0].reject(Error('старий збій'));await first;
 assert.equal(x.ui.feedback.textContent,text);x.ui.archive.listeners.click();
 assert.equal(x.$('recording-order').value,'new');
});


test('saved terms render as removable pills and multiword phrases stay together',()=>{
 const x=setup();assert.equal(x.ui.pills.children[0].textContent,'Клен ×');
 x.ui.terms.value='Дуб 34';x.ui.terms.listeners.keydown({key:'Enter',preventDefault(){}});
 assert.equal(x.ui.terms.value,'');assert.equal(x.ui.pills.children[1].textContent,'Дуб 34 ×');
 assert.equal(x.storage.get('radio345:mention-terms'),'Клен, Дуб 34');
 x.ui.pills.children[0].listeners.click();assert.equal(x.storage.get('radio345:mention-terms'),'Дуб 34');
});
test('paste splits terms on separators and deduplicates without splitting spaces',()=>{
 const x=setup();x.ui.terms.value='клен, Дуб 34; Вишня';x.ui.terms.listeners.input();
 assert.equal(x.ui.pills.children.length,3);assert.equal(x.storage.get('radio345:mention-terms'),'Клен, Дуб 34, Вишня');
});

test('single-line field preserves separate terms and multiword phrases on multiline paste',()=>{
 const x=setup();x.ui.terms.value='Дуб';x.ui.terms.selectionStart=3;x.ui.terms.selectionEnd=3;
 let prevented=false;
 x.ui.terms.listeners.paste({clipboardData:{getData:()=> ' 34\nВишня; клен'},preventDefault(){prevented=true;}});
 assert.equal(prevented,true);assert.equal(x.ui.terms.value,'');
 assert.equal(x.storage.get('radio345:mention-terms'),'Клен, Дуб 34, Вишня');
});

test('separate date and time inputs send the selected local period including a midnight crossing',async()=>{
 const x=setup();
 x.ui.since.date.value='2026-11-20';x.ui.since.time.value='23:45';
 x.ui.until.date.value='2026-11-21';x.ui.until.time.value='00:15';
 const task=x.ui.search(true),payload=x.pending[0].args[2];
 assert.equal(payload.since,new Date('2026-11-20T23:45').getTime()/1000);
 assert.equal(payload.until,new Date('2026-11-21T00:15').getTime()/1000);
 assert.equal(payload.until-payload.since,1800);
 x.pending[0].resolve({selection:'dates',total:0,items:[]});await task;
});


test('returning from archive refreshes snapshot and removes newly classified non-speech',async()=>{
 const x=setup(),first=x.ui.search(true);x.pending[0].resolve(result('snapshot','Клен'));await first;
 const refresh=x.ui.refresh();x.pending[1].resolve({selection:'snapshot',total:0,items:[]});await refresh;
 assert.equal(x.ui.selection(),'snapshot');assert.equal(x.ui.results.children.length,0);
 assert.equal(x.ui.archive.disabled,true);
});

test('changing order immediately reloads the whole saved selection without taking draft filters',async()=>{
 const x=setup(),first=x.ui.search(true);x.pending[0].resolve(result('snapshot','Старий порядок'));await first;
 x.ui.terms.value='Інша чернетка';x.ui.order.value='unreviewed';
 const sorted=x.ui.order.listeners.change();
 assert.match(x.pending[1].args[0],/selection=snapshot/);
 assert.match(x.pending[1].args[0],/order=unreviewed/);
 assert.equal(x.pending[1].args.length,1);
 assert.equal(x.ui.terms.value,'Інша чернетка');
 x.pending[1].resolve(result('snapshot','Нерозмічене'));await sorted;
 assert.equal(x.ui.selection(),'snapshot');
 assert.match(x.ui.feedback.textContent,/Спершу нерозмічені/);
});

test('order changes during first search preserve its filters and discard the late old result',async()=>{
 const x=setup(),first=x.ui.search(true);x.ui.terms.value='Не шукати цю чернетку';x.ui.order.value='new';
 const second=x.ui.order.listeners.change();
 assert.equal(x.pending[1].args[2].terms,x.pending[0].args[2].terms);
 assert.equal(x.pending[1].args[2].order,'new');
 assert.equal(x.ui.terms.value,'Не шукати цю чернетку');
 x.pending[1].resolve(result('new','Новий порядок'));await second;
 x.pending[0].resolve(result('old','Стара відповідь'));await first;
 assert.equal(x.ui.selection(),'new');
});
