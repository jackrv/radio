const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=require('./panel_source.cjs');
function harness({missing=false,error=false}={}) {
 const nodes=[],calls=[],box={textContent:'',replaceChildren(){nodes.length=0;},append(...items){nodes.push(...items);}};
 const data={audio:{available:!missing,bytes:90,max_bytes:100,remaining_bytes:10,protected_bytes:60,protected_count:1,unreviewed_count:2,unreviewed_bytes:30,retention_days:7,low_rms_retention_seconds:3600,due_count:0,due_bytes:0,metadata_bytes:1},logs:[],
 diagnostics:{count:1,max_sessions:100,bytes:10,max_bytes:1000,retention_days:14},backups:{automatic_count:2,keep:7,automatic_bytes:20,manual_count:1,manual_bytes:10},disk:{free_bytes:10000}};
 const context=vm.createContext({$:()=>box,request:async(...args)=>{calls.push(args);if(error)throw new Error('offline');return data;},
 num:String,kv:rows=>({rows}),el:(tag,cls,text)=>({tag,className:cls,textContent:text,setAttribute(){}})});
 const block=source.slice(source.indexOf('  const storageSize ='),source.indexOf('  async function system()'));
 vm.runInContext(block+'\nglobalThis.loadStorage=storageView;',context);
 return {context,nodes,calls,box,data};
}
test('storage explains protected audio, the budget and the next cleanup without write requests',async()=>{
 const h=harness();await h.context.loadStorage();assert.deepEqual(h.calls,[['/api/storage']]);
 assert.equal(h.nodes.find(n=>n.tag==='progress').value,90);
 assert.ok(h.nodes.find(n=>n.rows)?.rows.some(r=>r[0].startsWith('Захищені:')));
 assert.ok(h.nodes.some(n=>n.textContent?.includes('після збереження нового аудіо')));
});
test('missing archive and older server have useful empty states',async()=>{
 const h=harness({missing:true});await h.context.loadStorage();assert.ok(h.nodes.some(n=>n.textContent?.includes('ще не створено')));
 const old=harness({error:true});await old.context.loadStorage();assert.match(old.box.textContent,/недоступні/);
});

 test('storage states the short quiet retention and periodic cleanup',async()=>{
 const h=harness();await h.context.loadStorage();
 assert.ok(h.nodes.some(n=>n.textContent?.includes('Тихі записи без розмітки: до 60 хв')));
 assert.ok(h.nodes.some(n=>n.textContent?.includes('раз на хвилину')));
 const old=harness();delete old.data.audio.low_rms_retention_seconds;await old.context.loadStorage();
 assert.ok(!old.nodes.some(n=>n.textContent?.includes('Тихі записи без розмітки:')));
 });
