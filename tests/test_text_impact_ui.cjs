const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');

test('text-impact report displays non-speech analysis when there are no speech records',async()=>{
  const source=fs.readFileSync(path.join(__dirname,'../local_admin/js/04-analysis.js'),'utf8');
  const start=source.indexOf('  function renderTextImpact(');
  const end=source.indexOf('\n  $("text-impact-run")',start);
  const button={disabled:false};
  const box={children:[],replaceChildren(...children){this.children=children;}};
  const report={
    records:0,
    non_speech_records:1,
    checks:[],
    current:{false_text:1,false_text_by_label:{noise:1,notification:0,room:0}},
    scope:'synthetic test scope',
  };
  const context=vm.createContext({
    $:id=>id==='text-impact-run'?button:box,
    el:(tag,className,text)=>({tagName:tag,className,textContent:text,children:[],append(...c){this.children.push(...c);}}),
    num:String,
    request:async()=>report,
  });
  vm.runInContext(source.slice(start,end)+'\nglobalThis.renderTextImpact=renderTextImpact;',context);
  context.renderTextImpact(report);
  const rendered=box.children.map(child=>child.textContent).join(' ');
  assert.match(rendered,/0 розмічених фразах мовлення і 1 записах шуму/);
  assert.match(rendered,/Активних текстових правил для перевірки немає/);
  assert.doesNotMatch(rendered,/Розмічених текстів для перевірки немає/);
  assert.equal(button.disabled,false);
});
