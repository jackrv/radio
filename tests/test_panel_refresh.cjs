const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=require('./panel_source.cjs');
const tick=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
function refreshHarness(view='dictionaries') {
 const nodes=new Map(),events={},calls=[];
 const $=id=>{if(!nodes.has(id))nodes.set(id,{value:'',textContent:''});return nodes.get(id);};
 $('recording-view').value='radio';
 const context=vm.createContext({$,view,busy:false,timer:null,REFRESH_MS:10000,
  document:{visibilityState:'visible',addEventListener:(n,f)=>{events[n]=f;}},window:{addEventListener:(n,f)=>{events[n]=f;}},
  overview:async()=>{calls.push('overview');},system:async()=>{calls.push('system');},
  filterView:async()=>{calls.push('filter');},activity:async()=>{calls.push('activity');},
  periodSince:()=>null,periodUntil:()=>null,searchQuery:()=>'',recordingFilterIds:['recording-view','recording-period','recording-edits','recording-search'],
  archiveBaseline:null,URLSearchParams,request:async()=>({total:2,items:[{id:'new'}]}),setInterval(){},clearInterval(){}});
 const block=source.slice(source.indexOf('  let refreshPending ='),source.indexOf('  // ---------- Вхід ----------'));
 vm.runInContext(block+'\nglobalThis.refresh=refreshPanel;',context);
 return {context,$,events,calls};
}
test('header refresh works on editor sections without calling their loaders',async()=>{
 const h=refreshHarness();await h.context.refresh();assert.deepEqual(h.calls,['overview']);
});
test('hidden and busy panels skip requests, and returning to a visible page refreshes immediately',async()=>{
 const h=refreshHarness('system');h.context.document.visibilityState='hidden';await h.context.refresh();assert.equal(h.calls.length,0);
 h.context.document.visibilityState='visible';h.context.busy=true;await h.context.refresh();assert.equal(h.calls.length,0);
 h.context.busy=false;h.events.visibilitychange();await tick();assert.deepEqual(h.calls,['overview','system']);
});
test('failed background refresh releases its lock and retries successfully',async()=>{
 const h=refreshHarness();h.context.overview=async()=>{throw new Error('offline');};await h.context.refresh();
 assert.match(h.$('updated').textContent,/не оновлено/);
 h.context.overview=async()=>h.calls.push('recovered');await h.context.refresh();assert.deepEqual(h.calls,['recovered']);
});
test('slow background requests cannot overlap',async()=>{
 const h=refreshHarness();let finish;h.context.overview=()=>new Promise(resolve=>{h.calls.push('slow');finish=resolve;});
 const first=h.context.refresh();await h.context.refresh();assert.deepEqual(h.calls,['slow']);finish();await first;
});
test('archive refresh announces changed records without replacing editable cards',async()=>{
 const h=refreshHarness('recordings');h.context.archiveBaseline={query:JSON.stringify(['radio','','','']),total:1,first:'old'};
 await h.context.refresh();assert.match(h.$('archive-update-note').textContent,/Є нові/);assert.deepEqual(h.calls,['overview']);
});

function requestHarness(fetch) {
 let abort,cleared=0;
 const context=vm.createContext({fetch,AbortController,Blob,Error,
  setTimeout:callback=>{abort=callback;return 1;},clearTimeout:()=>{cleared++;}});
 const block=source.slice(source.indexOf('  async function request('),source.indexOf('  async function action('));
 vm.runInContext(block+'\nglobalThis.send=request;',context);
 return {context,timeout:()=>abort(),cleared:()=>cleared};
}
test('request timeout aborts stalled connections and frees its deadline',async()=>{
 const h=requestHarness((url,options)=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{
  const error=new Error();error.name='AbortError';reject(error);
 })));
 const pending=h.context.send('/api/overview');h.timeout();await assert.rejects(pending,/30 с/);assert.equal(h.cleared(),1);
});
test('deadline also covers reading a stalled response body',async()=>{
 const h=requestHarness(async(url,options)=>({ok:true,status:200,headers:{get:()=> 'application/json'},json:()=>new Promise((resolve,reject)=>options.signal.addEventListener('abort',()=>{
  const error=new Error();error.name='AbortError';reject(error);
 }))}));
 const pending=h.context.send('/api/overview');await tick();h.timeout();await assert.rejects(pending,/30 с/);assert.equal(h.cleared(),1);
});

function activityHarness() {
 class Element {
  constructor(){this.children=[];this.dataset={};this.events={};this.classList={toggle:(key,value)=>{this[key]=value;}};}
  append(...children){this.children.push(...children);}replaceChildren(...children){this.children=children;}
  addEventListener(name,callback){this.events[name]=callback;}setAttribute(name,value){this[name]=value;}
 }
 const nodes=new Map(),saved=new Map(),calls=[];
 const $=id=>{if(!nodes.has(id))nodes.set(id,new Element());return nodes.get(id);};
 const users=[{user_id:1001,name:'Тест А',role:'admin',enabled:true},{user_id:2002,name:'Тест Б',role:'editor',enabled:true}];
 const data=id=>({users,selected:{...users.find(u=>String(u.user_id)===String(id)),actions:{},actions_week:{},delivered:{total:0},items:[]}});
 const context=vm.createContext({$,localStorage:{getItem:key=>saved.get(key)||null,setItem:(key,value)=>saved.set(key,value),removeItem:key=>saved.delete(key)},
  el:(tag,cls,text)=>Object.assign(new Element(),{className:cls,textContent:text}),
  ROLES:{admin:'Адміністратор',editor:'Редактор'},MODES:{},when:()=>'',card:()=>new Element(),linked:node=>node,note(){},
  request:async url=>{calls.push(url);return data(new URL(url,'http://localhost').searchParams.get('user_id')||1001);}});
 const block=source.slice(source.indexOf('  const ACTIVITY_KEY ='),source.indexOf('  async function users()'));
 vm.runInContext(block+'\nglobalThis.loadActivity=activity;',context);
 return {context,$,calls,saved,data};
}
test('activity uses named user buttons, remembers selection and refreshes the selected user',async()=>{
 const h=activityHarness();await h.context.loadActivity();
 const first=h.$('activity-user').children[0],second=h.$('activity-user').children[1];
 assert.match(second.textContent,/Тест Б/);assert.equal(first['aria-pressed'],'true');
 second.events.click();await tick();assert.equal(second['aria-pressed'],'true');
 assert.equal(h.saved.get('radio345.activityUser'),'2002');
 await h.context.loadActivity();assert.equal(h.calls.at(-1),'/api/activity?user_id=2002');
 assert.equal(h.$('activity-user').children[1],second);
});
test('late activity responses cannot switch back to the previously selected user',async()=>{
 const h=activityHarness();let release;
 h.context.request=url=>url.includes('2002')?new Promise(resolve=>{release=()=>resolve(h.data(2002));}):Promise.resolve(h.data(1001));
 const slow=h.context.loadActivity('2002');await h.context.loadActivity('1001');release();await slow;
 assert.equal(h.saved.get('radio345.activityUser'),'1001');
 assert.equal(h.$('activity-user').children[0]['aria-pressed'],'true');
});

test('Mini App preview is a separate People tab with one iframe and role picker',()=>{
 const html=fs.readFileSync(path.join(__dirname,'../local_admin/index.html'),'utf8');
 const preview=html.match(/<section data-panel="preview"[\s\S]*?<\/section>/)?.[0];
 assert.ok(preview);assert.match(preview,/id="preview"/);assert.match(preview,/id="role"/);
 assert.equal((html.match(/id="preview"/g)||[]).length,1);
 assert.match(source,/people: \["Люди", \["users", "activity", "preview"\]\]/);
});

test('live feed refresh preserves a playing voice and resumes replacing rows after pause',async()=>{
 const audio={paused:false,ended:false};let renders=0;
 const note={textContent:''},box={querySelectorAll:()=>[audio],replaceChildren(){renders++;}};
 const context=vm.createContext({$:id=>id==='live'?box:note,request:async()=>({items:[]}),el:()=>({})});
 const block=source.slice(source.indexOf('  async function live()'),source.indexOf('  async function quality()'));
 vm.runInContext(block+'\nglobalThis.refreshLive=live;',context);
 await context.refreshLive();assert.equal(renders,0);assert.match(note.textContent,/прослуховування/);
 audio.paused=true;await context.refreshLive();assert.equal(renders,1);
});

test('temporary activity failures preserve the selected user rather than falling back to someone else',async()=>{
 const h=activityHarness();await h.context.loadActivity('2002');let calls=0;
 h.context.request=async()=>{calls++;throw new Error('offline');};
 await assert.rejects(h.context.loadActivity(),/offline/);assert.equal(calls,1);
 assert.equal(h.saved.get('radio345.activityUser'),'2002');
});
test('a removed activity user falls back only after HTTP 404',async()=>{
 const h=activityHarness();await h.context.loadActivity('2002');
 h.context.request=async url=>{
  if(url.includes('user_id')){const error=new Error('not found');error.status=404;throw error;}
  return h.data(1001);
 };
 await h.context.loadActivity();assert.equal(h.saved.get('radio345.activityUser'),'1001');
});
