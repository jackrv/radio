const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('fs'),vm=require('vm'),path=require('path');
const text=fs.readFileSync(path.join(__dirname,'../local_admin/js/07-recognition.js'),'utf8');
const start=text.indexOf('  function withoutImpactRule('),end=text.indexOf('\n  async function texts()',start);
const context=vm.createContext({});vm.runInContext(text.slice(start,end)+'\nglobalThis.draft=withoutImpactRule;',context);
test('preview disables only the selected alias and preserves other spellings',()=>{
  const entries=[{raw_line:'Клен: Клин, клін',enabled:true}];
  const result=JSON.parse(JSON.stringify(context.draft(entries,'callsign_aliases','клин → клен')));
  assert.deepEqual(result,[{raw_line:'Клен: клін',enabled:true},{raw_line:'Клен: клин',enabled:false}]);
  assert.equal(entries[0].raw_line,'Клен: Клин, клін');assert.equal(entries[0].enabled,true);
});
test('stale impact reports never silently disable another rule',()=>{
  assert.throws(()=>context.draft([{raw_line:'інший зворот',enabled:true}],'phrase_templates','прийнято прийом'),/вже змінено/);
});
