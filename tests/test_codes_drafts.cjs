const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.join(__dirname,'../local_admin/js/07-recognition.js'),'utf8');
function setup(){
 class E{constructor(){this.value='';this.listeners={};this.children=[];} addEventListener(n,f){this.listeners[n]=f;} replaceChildren(...v){this.children=v;} append(...v){this.children.push(...v);} }
 const nodes=new Map(),$=id=>{if(!nodes.has(id))nodes.set(id,new E());return nodes.get(id);};
 let data={today:'2026-11-20',items:[{day:'2026-11-20',codes:['42 42']}],permanent:['0 22']}, finish;
 const c=vm.createContext({Event,$,el:()=>new E(),action:f=>f(),request:async(url,method,body)=>{
   if(!method)return data;
   if(url.endsWith('/permanent'))return new Promise(resolve=>{finish=()=>{data={...data,permanent:[body.codes]};resolve({codes:[body.codes]});};});
   return {day:'2026-11-20',codes:[]};
 }});
 vm.runInContext(source.slice(source.indexOf('  let codesByDay'),source.indexOf('  // Перевірка рядків'))+'\nglobalThis.reload=dailyCodes;globalThis.day=showDayCodes;',c);
 return {$,reload:()=>c.reload(),day:()=>c.day(),finish:()=>finish()};
}
test('unsaved daily code survives refresh and switching dates',async()=>{
 const x=setup();await x.reload();
 x.$('codes-input').value='73 73';x.$('codes-input').listeners.input();
 await x.reload();assert.equal(x.$('codes-input').value,'73 73');
 x.$('codes-day').value='2026-11-19';x.day();assert.equal(x.$('codes-input').value,'');
 x.$('codes-day').value='2026-11-20';x.day();assert.equal(x.$('codes-input').value,'73 73');
});
test('editing permanent code during save keeps the newer draft',async()=>{
 const x=setup();await x.reload();
 x.$('codes-permanent').value='42 42';x.$('codes-permanent').listeners.input();
 const pending=x.$('codes-permanent-save').listeners.click();
 x.$('codes-permanent').value='73 73';x.$('codes-permanent').listeners.input();
 x.finish();await pending;assert.equal(x.$('codes-permanent').value,'73 73');
 await x.reload();assert.equal(x.$('codes-permanent').value,'73 73');
});
test('removing a code chip persists as a draft across refresh',async()=>{
 const x=setup();await x.reload();
 x.$('codes-permanent').dispatchEvent=()=>x.$('codes-permanent').listeners.input();
 x.$('codes-permanent-chips').children[0].listeners.click();
 await x.reload();assert.equal(x.$('codes-permanent').value,'');
});
