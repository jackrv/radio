const {test}=require('node:test');
const assert=require('node:assert/strict');
const vm=require('node:vm');
const source=require('./panel_source.cjs');
const c=vm.createContext({ago:s=>`${s} с`});
vm.runInContext(source.slice(source.indexOf('  function flowModel('),source.indexOf('  function flowRender('))+'\nglobalThis.m=flowModel;globalThis.ch=flowChanges;',c);
const devices={marker:'pi',mac:{processes:{worker:{alive:true}},model:'ready'},pi:{processes:{node:{alive:true},asr:{alive:true}}}};
const snap=extra=>({home:'pi',age_s:1,data:{node:'listening',queue:0,segment:null,event:1,sent:1,air:false,transmitting:false,listeners:0,...extra}});
test('idle system shows services without moving data',()=>{
 const r=c.m(snap(),devices,{},0);assert.equal(r.nodes.asr[0],'ok');assert.ok(Object.values(r.edges).every(v=>v===false));
});
test('speech, queue and listeners light their own links',()=>{
 const r=c.m(snap({air:true,queue:2,listeners:1,whisper:{state:'sending',age_s:1,sent:1}}),devices,{},0);
 assert.ok(r.edges.air&&r.edges.whisper&&r.edges.listen&&!r.edges.tx);assert.equal(r.nodes.whisper[1],'розпізнає');
});
test('paused model and a bare queue do not animate the link',()=>{
 const r=c.m(snap({queue:2}),{...devices,mac:{...devices.mac,model:'paused'}},{},0);
 assert.equal(r.edges.whisper,false);assert.equal(r.nodes.whisper[0],'warn');
});
test('Pi panel: Whisper state comes from the recognizer itself',()=>{
 const pi={...devices,mac:null}, w=(state,age_s=5)=>c.m(snap({queue:1,whisper:{state,age_s,sent:3}}),pi,{},0);
 assert.deepEqual([...w('sending').nodes.whisper],['busy','розпізнає']);assert.ok(w('sending').edges.whisper);
 assert.equal(w('sending',100).edges.whisper,false,'застаріла відмітка');
 for(const [state,tone] of [['ready','ok'],['refused','warn'],['unavailable','bad']]){
  assert.equal(w(state).nodes.whisper[0],tone);assert.equal(w(state).edges.whisper,false);}
 assert.equal(w('ready',4000).nodes.whisper[0],'','давня відповідь не доводить, що Mac увімкнено');
 assert.equal(c.m(snap({queue:2}),pi,{},0).edges.whisper,false);
});
test('stale snapshot never animates',()=>{
 const r=c.m({...snap({air:true,queue:1}),age_s:30},devices,{},0);assert.equal(r.live,false);assert.equal(r.edges.air||r.edges.whisper,false);
});
test('changes need a baseline',()=>{
 const a=snap().data;assert.deepEqual([...c.ch(null,a)],[]);
 assert.deepEqual([...c.ch(a,{...a,segment:'1.seg',queue:1})],['segment']);
 assert.deepEqual([...c.ch({...a,queue:1,segment:'1.seg'},{...a,event:2,sent:2})],['db','telegram']);
});
test('exchange between two snapshots still pulses',()=>{
 const a={...snap().data,whisper:{state:'ready',sent:1}};
 assert.deepEqual([...c.ch(a,{...a,whisper:{state:'ready',sent:2}})],['whisper']);
});
test('level bar follows RMS, threshold and clipping',()=>{
 vm.runInContext(source.slice(source.indexOf('  function flowLevel('),source.indexOf('  // Що змінилося між'))+'\nglobalThis.lv=flowLevel;',c);
 const l=(rms,peak=.1,phrase)=>c.lv({min_rms:.02,level:{rms,peak,phrase_rms:phrase}});
 assert.equal(l(.01).tone,'');assert.equal(l(.03).tone,'above');assert.equal(l(.03,.99).tone,'clip');
 assert.ok(l(.01).part<l(.01).mark&&l(.03).part>l(.03).mark);
 assert.match(l(.0107).now,/0\.0107 · -39 дБ/);assert.match(l(.01,.1,.0231).phrase,/0\.0231 ✓/);assert.match(l(.01,.1,.011).phrase,/тихо/);
 const p=c.lv({min_rms:.02,level:{rms:.01,phrase_rms:.011,louder:[[.05,90],[.03,40],[.011,2]]}});
 assert.match(p.passed,/0\.0300 · 40/);assert.match(p.phrase,/тихо/);assert.match(l(.01).passed,/ще не/);
 assert.equal(c.lv(null).tone,'off');assert.equal(c.lv({level:{}}).tone,'off');
});
test('service command is finished only by the observed state',()=>{
 vm.runInContext(source.slice(source.indexOf('  function serviceDone('),source.indexOf('  async function runService('))+'\nglobalThis.sd=serviceDone;',c);
 assert.equal(c.sd({alive:true,pid:5},'restart',5),null);assert.equal(c.sd({alive:false},'restart',5),null);assert.equal(c.sd({alive:true,pid:6},'restart',5),true);
 assert.equal(c.sd({alive:true},'stop'),null);assert.equal(c.sd({alive:false},'stop'),true);
 assert.equal(c.sd({alive:false,blocked:true},'start'),false);assert.equal(c.sd({alive:true,pid:1},'start'),true);assert.equal(c.sd(undefined,'start'),null);
});
