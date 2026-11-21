const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
const source=fs.readFileSync(path.join(__dirname,'../local_admin/js/05-overview.js'),'utf8');

function harness(){
 const navigations=[],errors=[];
 const ctx=vm.createContext({openTarget:target=>navigations.push(target),note:message=>errors.push(message),
   pendingListInsert:null,pendingDictionaryInsert:null,pendingPatternEnable:[]});
 vm.runInContext(source.slice(source.indexOf('  function suggestionReviewOptions('),source.indexOf('  let attentionAt ='))+
   '\nglobalThis.options=suggestionReviewOptions;globalThis.review=reviewSuggestion;',ctx);
 return {ctx,navigations,errors,options:g=>ctx.options(g),review:g=>ctx.review(g)};
}
const plain=value=>JSON.parse(JSON.stringify(value));
function mixed(){return {kind:'noise',target:'texts:short_garbage',lines:[
 {list:'blacklist',line:'синтетична довга фраза шуму'},
 {list:'short_garbage',line:'сторонній шелест'},
 {list:'short_garbage',line:'хибний звук'},
]};}

test('mixed noise proposal offers both destinations without losing or mutating lines',()=>{
 const h=harness(),g=mixed(),before=plain(g),options=plain(h.options(g));
 assert.deepEqual(options.map(o=>o.title),['Переглянути шум (2)','Переглянути галюцинації (1)']);
 assert.deepEqual(options.map(o=>o.suggestion.target),['texts:short_garbage','texts:blacklist']);
 assert.deepEqual(options.flatMap(o=>o.suggestion.lines).sort((a,b)=>a.line.localeCompare(b.line)),
   g.lines.toSorted((a,b)=>a.line.localeCompare(b.line)));
 assert.deepEqual(g,before);
});

test('each review opens only its selected list as an unsaved draft',async()=>{
 const h=harness();
 for(const option of h.options(mixed())){
   await h.review(option.suggestion);
   assert.equal(h.navigations.at(-1),option.suggestion.target);
   assert.deepEqual(plain(h.ctx.pendingListInsert.lines),plain(option.suggestion.lines));
   assert.ok(h.ctx.pendingListInsert.lines.every(line=>line.list===h.ctx.pendingListInsert.list));
 }
 assert.deepEqual(h.errors,[]);
});

test('one-list proposal retains the familiar review action and all its lines',async()=>{
 const h=harness(),g={...mixed(),lines:mixed().lines.filter(line=>line.list==='blacklist')};
 const options=h.options(g);
 assert.equal(options.length,1);assert.equal(options[0].title,'Переглянути');
 await h.review(options[0].suggestion);
 assert.deepEqual(h.navigations,['texts:blacklist']);assert.deepEqual(h.errors,[]);
});

test('dictionary and phrase-pattern proposals keep their specialised review paths',async()=>{
 const h=harness();
 const dictionary={kind:'dictionary',target:'dictionaries',lines:[{list:'callsigns',callsign:'клен',number:'12'}]};
 await h.review(h.options(dictionary)[0].suggestion);
 assert.deepEqual(plain(h.ctx.pendingDictionaryInsert),{list:'callsigns',lines:dictionary.lines});
 await h.review({kind:'pattern',target:'texts:phrase_patterns',lines:[{list:'phrase_patterns',line:'{П}, прийом'}]});
 assert.deepEqual(plain(h.ctx.pendingPatternEnable),['{П}, прийом']);
 assert.deepEqual(h.navigations,['dictionaries:callsigns','texts:phrase_patterns']);
 assert.deepEqual(h.errors,[]);
});

test('unknown mixed destination is not silently dropped or added to the wrong editor',async()=>{
 const h=harness(),g=mixed();g.lines.push({list:'unknown',line:'невідома пропозиція'});
 const options=h.options(g);assert.equal(options.length,1);
 await h.review(options[0].suggestion);
 assert.equal(h.errors.length,1);assert.deepEqual(h.navigations,[]);
 assert.equal(h.ctx.pendingListInsert,null);
});
