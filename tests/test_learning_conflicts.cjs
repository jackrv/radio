const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../local_admin/js/08-learning.js'),'utf8');
class Element{
 constructor(tag,cls='',text=''){this.tag=tag;this.className=cls;this.textContent=text;this.children=[];this.listeners={};}
 get childNodes(){return this.children;}
 append(...children){this.children.push(...children);}
 addEventListener(name,callback){this.listeners[name]=callback;}
}
function harness(){
 const filter={value:'conflict'},search={value:''},calls=[];
 const ctx=vm.createContext({el:(...args)=>new Element(...args),document:{createTextNode:text=>new Element('text','',text)},
   $:()=>filter,learningSearch:search,renderLearning:()=>calls.push('render'),action:f=>f(),changeRule:(...args)=>calls.push(args),
   localStorage:{setItem:(...args)=>calls.push(args)}});
 vm.runInContext(source.slice(source.indexOf('  function learningSources('),source.indexOf('  async function changeRule('))+
   source.slice(source.indexOf('  function learningActions('),source.indexOf('  function renderLearning('))+
   '\nglobalThis.alternatives=learningAlternatives;globalThis.details=learningConflictDetails;globalThis.actions=learningActions;',ctx);
 return {ctx,filter,search,calls};
}
function rule(corrected,extras={}){return {original:'стежко',corrected,confirmations:2,status:'pending',state:'conflict',kind:'word',approved:false,sources:['db:7'],...extras};}

test('one-confirmation competitor remains visible even when its own state is not conflict',()=>{
 const h=harness(),current=rule('стежка'),other=rule('стежку',{confirmations:1,state:'needs_confirmation',sources:['archive:'+'a'.repeat(32)]});
 const all=[current,other,rule('стежці',{status:'disabled'}),rule('стежок',{confirmations:0}),{...other,original:'інше'}];
 const alternatives=h.ctx.alternatives(current,all);
 assert.equal(alternatives.length,1);assert.equal(alternatives[0],other);
 const details=h.ctx.details(current,alternatives);
 const variant=details.children.find(n=>n.className==='learning-conflict-variant');
 assert.equal(variant.children[0].textContent,'стежко → стежку');
 assert.equal(variant.children[1].textContent,'Правок: 1 · джерела:');
 assert.match(variant.children[2].children[0].href,/view=recordings/);
 assert.match(variant.children[2].children[0].href,/aaaaaaaa/);
 assert.match(details.children.find(n=>n.tag==='p'&&n.className==='muted small').textContent,/нічого не змінюйте/);
});

test('show all variants changes only local view and search',()=>{
 const h=harness(),current=rule('стежка');
 const box=h.ctx.details(current,[rule('стежку')]);
 box.children.find(n=>n.tag==='button').listeners.click();
 assert.equal(h.filter.value,'all');assert.equal(h.search.value,'стежко');
 assert.deepEqual(h.calls,[['radio345:learning-filter','all'],'render']);
});

test('approval cannot pretend to resolve ambiguity, but existing approval can be revoked',()=>{
 const h=harness(),current=rule('стежка'),other=rule('стежку');
 assert.deepEqual(h.ctx.actions(current,[other]).children.map(n=>n.textContent),['Вимкнути']);
 assert.deepEqual(h.ctx.actions({...current,approved:true},[other]).children.map(n=>n.textContent),['Зняти дозвіл','Вимкнути']);
 assert.deepEqual(h.ctx.actions({...current,state:'needs_approval',status:'active'},[]).children.map(n=>n.textContent),['Дозволити','Вимкнути']);
});
