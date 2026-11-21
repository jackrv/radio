// Синтетичний smoke-тест панелі: node tools/check_panel_ui.cjs (Chrome + .venv).
const fs=require('fs'),os=require('os'),path=require('path'),{spawn}=require('child_process');
process.chdir(path.resolve(__dirname,'..'));
const chromeBin=process.env.CHROME_BIN || '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';
if(!fs.existsSync(chromeBin)||!fs.existsSync('.venv/bin/python')){console.error('Потрібні .venv і Chrome (або CHROME_BIN).');process.exit(1);}
const root=fs.mkdtempSync(path.join(os.tmpdir(),'radio-panel-smoke-'));fs.chmodSync(root,0o700);
const sleep=ms=>new Promise(r=>setTimeout(r,ms));
const server=spawn('.venv/bin/python',[path.join(process.cwd(),'tests/panel_browser_fixture.py'),root],{cwd:process.cwd(),stdio:['ignore','pipe','pipe']});
let chrome,ws;const errors=[];let serverOutput='';server.stdout.on('data',b=>serverOutput+=b);server.stderr.on('data',b=>errors.push('server: '+b));
async function until(fn,timeout=15000){const at=Date.now();while(Date.now()-at<timeout){const v=await fn();if(v)return v;await sleep(100);}throw Error('timeout');}
(async()=>{try{
 await until(()=>serverOutput.includes('\n'));const port=JSON.parse(serverOutput.split('\n')[0]).port;
 await until(async()=>{try{return (await fetch(`http://127.0.0.1:${port}/`)).ok;}catch{return false;}});
 const profile=path.join(root,'chrome');
 chrome=spawn(chromeBin,['--headless=new','--no-first-run','--no-default-browser-check','--disable-background-networking','--disable-sync','--remote-debugging-port=0',`--user-data-dir=${profile}`,'about:blank'],{stdio:'ignore'});
 const active=path.join(profile,'DevToolsActivePort');await until(()=>fs.existsSync(active));
 const dp=fs.readFileSync(active,'utf8').split('\n')[0];const pages=await (await fetch(`http://127.0.0.1:${dp}/json`)).json();
 ws=new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);await new Promise((r,j)=>{ws.onopen=r;ws.onerror=j;});
 let id=0;const pending=new Map();ws.onmessage=e=>{const v=JSON.parse(e.data);if(v.id){const p=pending.get(v.id);pending.delete(v.id);if(v.error)p.reject(Error(v.error.message));else p.resolve(v.result);}if(v.method==='Runtime.exceptionThrown')errors.push(v.params.exceptionDetails.exception?.description||v.params.exceptionDetails.text);};
 const send=(method,params={})=>new Promise((resolve,reject)=>{const n=++id;pending.set(n,{resolve,reject});ws.send(JSON.stringify({id:n,method,params}));});
 const evaluate=async expression=>{const r=await send('Runtime.evaluate',{expression,returnByValue:true,awaitPromise:true});if(r.exceptionDetails)throw Error(r.exceptionDetails.exception?.description||r.exceptionDetails.text);return r.result.value;};
 await send('Runtime.enable');await send('Page.enable');await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
 await send('Page.navigate',{url:`http://127.0.0.1:${port}/#${'x'.repeat(43)}`});
 await until(()=>evaluate(`document.querySelector('#content')?.hidden===false`));
 await evaluate(`document.querySelector('[data-group="mentions"]').click()`);
 await until(()=>evaluate(`!document.querySelector('[data-panel="mentions"]').hidden`));
 const dialPoint=async(degrees,radius=.43)=>evaluate(`{const r=document.querySelector('.mention-time-popover:popover-open .mention-dial').getBoundingClientRect(),a=${degrees}*Math.PI/180;({x:r.left+r.width/2+Math.sin(a)*r.width*${radius},y:r.top+r.height/2-Math.cos(a)*r.width*${radius}});}`);
 const dialClick=async(degrees,radius=.43)=>{const point=await dialPoint(degrees,radius);await send('Input.dispatchMouseEvent',{type:'mouseMoved',...point});await send('Input.dispatchMouseEvent',{type:'mousePressed',...point,button:'left',clickCount:1});await send('Input.dispatchMouseEvent',{type:'mouseReleased',...point,button:'left',clickCount:1});};
 const timeDate=await evaluate(`{const c=document.querySelector('.mention-date-control'),i=c.querySelector('.mention-time-input');i.value='09:26';i.dispatchEvent(new Event('input'));c.querySelector('.mention-time-toggle').click();c.querySelector('input[type=date]').value;}`);
 await dialClick(180,.295);
 if(await evaluate(`document.querySelector('.mention-time-input').value`)!=='18:26')throw Error('hour dial rounded exact minutes');
 if(await evaluate(`document.querySelector('.mention-dial[aria-label="Початок періоду: хвилини"]')===null`))throw Error('hour dial did not switch to minutes');
 fs.writeFileSync(path.join(root,'time-dial-minutes.png'),Buffer.from((await send('Page.captureScreenshot',{format:'png'})).data,'base64'));
 await dialClick(210);
 if(await evaluate(`document.querySelector('.mention-time-input').value`)!=='18:35')throw Error('minute dial mismatch');
 if(await evaluate(`document.querySelector('.mention-date-control input[type=date]').value`)!==timeDate)throw Error('dial changed date');
 await evaluate(`document.querySelector('.mention-time-toggle').click();document.querySelector('.mention-time-popover:popover-open [aria-label="Обрати хвилини"]').click()`);
 const from=await dialPoint(150),to=await dialPoint(162);
 await send('Input.dispatchMouseEvent',{type:'mousePressed',...from,button:'left',clickCount:1});
 await send('Input.dispatchMouseEvent',{type:'mouseMoved',...to,buttons:1});
 await send('Input.dispatchMouseEvent',{type:'mouseReleased',...to,button:'left',clickCount:1});
 if(await evaluate(`document.querySelector('.mention-time-input').value`)!=='18:27')throw Error('minute drag did not select exact minute');
 await evaluate(`{const i=document.querySelector('.mention-time-input');i.value='2460';i.dispatchEvent(new Event('input'));if(i.checkValidity())throw Error('invalid manual time accepted');i.value='0926';i.dispatchEvent(new Event('input'));i.dispatchEvent(new Event('blur'));if(i.value!=='09:26'||!i.checkValidity())throw Error('exact manual time mismatch');}`);
 for(const width of [1440,390]){
  await send('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
  await sleep(120);
  await evaluate(`document.querySelector('.mention-time-toggle').click()`);
  const picker=await evaluate(`{const p=document.querySelector('.mention-time-popover:popover-open'),r=p.getBoundingClientRect();({visible:!!p,bounded:r.left>=0&&r.right<=innerWidth&&r.top>=0&&r.bottom<=innerHeight,scroll:p.scrollHeight>p.clientHeight+1||p.scrollWidth>p.clientWidth+1,hours:p.querySelectorAll('.mention-dial-number').length});}`);
  if(!picker.visible||!picker.bounded||picker.scroll||picker.hours!==36)throw Error('time picker layout '+JSON.stringify({width,...picker}));
  fs.writeFileSync(path.join(root,'time-picker-'+width+'.png'),Buffer.from((await send('Page.captureScreenshot',{format:'png'})).data,'base64'));
  await send('Input.dispatchKeyEvent',{type:'keyDown',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
  await send('Input.dispatchKeyEvent',{type:'keyUp',key:'Escape',code:'Escape',windowsVirtualKeyCode:27});
  if(await evaluate(`document.querySelector('.mention-time-popover:popover-open')!==null`))throw Error('Escape did not close time picker');
  await evaluate(`document.querySelector('.mention-time-toggle').click()`);
  await send('Input.dispatchMouseEvent',{type:'mousePressed',x:2,y:2,button:'left',clickCount:1});
  await send('Input.dispatchMouseEvent',{type:'mouseReleased',x:2,y:2,button:'left',clickCount:1});
  if(await evaluate(`document.querySelector('.mention-time-popover:popover-open')!==null`))throw Error('outside click did not close time picker');
  await evaluate(`document.querySelector('.mention-time-toggle').click();document.querySelector('main').dispatchEvent(new Event('scroll'))`);
  if(await evaluate(`document.querySelector('.mention-time-popover:popover-open')!==null`))throw Error('scroll did not close time picker');
 }
 await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
 await evaluate(`{const controls=document.querySelectorAll('.mention-date-control');if(controls.length!==2)throw Error('missing date groups');const form=document.querySelector('.mention-form'),date=controls[0].querySelector('input[type=date]'),saved=date.value;date.value='';if(form.checkValidity())throw Error('empty date accepted');date.value=saved;[...document.querySelectorAll('.mention-period-presets button')].find(b=>b.textContent==='Учора').click();const midnight=new Date();midnight.setHours(0,0,0,0);const start=controls[0].querySelector('input[type=date]').value+'T'+controls[0].querySelector('.mention-time-input').value,end=controls[1].querySelector('input[type=date]').value+'T'+controls[1].querySelector('.mention-time-input').value;if(new Date(end).getTime()!==midnight.getTime()||new Date(start).getTime()>=midnight.getTime())throw Error('yesterday period mismatch');[...document.querySelectorAll('.mention-period-presets button')].find(b=>b.textContent==='24 год').click();}`);
 await evaluate(`document.querySelector('.mention-term-input').value='Клен 12'; document.querySelector('.mention-term-input').dispatchEvent(new Event('input')); document.querySelector('[data-panel="mentions"] form').requestSubmit()`);
 await until(()=>evaluate(`document.querySelector('.mention-results')?.children.length===2`)).catch(async e=>{console.log('selection-debug',await evaluate(`document.querySelector('[data-panel="mentions"]').innerText`));console.log(errors);throw e;});
 const mentionLayout=await evaluate(`({pills:document.querySelectorAll('.mention-term-pill').length,time:parseFloat(getComputedStyle(document.querySelector('.mention-clock')).fontSize),date:parseFloat(getComputedStyle(document.querySelector('.mention-date')).fontSize)})`);
 if(!mentionLayout.pills || mentionLayout.time<=mentionLayout.date)throw Error('mention pills or time hierarchy missing');
 for(const order of ['new','unreviewed','old']){
  await evaluate(`{const select=document.querySelector('select[aria-label="Порядок часу"]');select.value='${order}';select.dispatchEvent(new Event('change'));}`);
  await until(()=>evaluate(`document.querySelector('.mention-results')?.children.length===2&&!document.querySelector('.mention-form button[type=submit]').disabled`));
 }
 console.log('flows: selection order changes immediately without another search click');
 const termLayout=await evaluate(`{const input=document.querySelector('.mention-term-input').getBoundingClientRect(),pill=document.querySelector('.mention-term-pill').getBoundingClientRect();({width:input.width,inline:Math.abs(input.top-pill.top)<10})}`);
 if(termLayout.width>200||!termLayout.inline)throw Error('term input not compact beside pills '+JSON.stringify(termLayout));
 fs.writeFileSync(path.join(root,'mentions.png'),Buffer.from((await send('Page.captureScreenshot',{format:'png'})).data,'base64'));
 for(const width of [760,390]){
  await send('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
  if(await evaluate(`document.documentElement.scrollWidth>innerWidth`))throw Error('mentions overflow at '+width);
 }
 await evaluate(`document.querySelector('.mention-form').scrollIntoView({block:'start'})`);
 fs.writeFileSync(path.join(root,'mentions-mobile.png'),Buffer.from((await send('Page.captureScreenshot',{format:'png'})).data,'base64'));
 await evaluate(`window.scrollTo({top:0})`);
 await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
 await evaluate(`document.querySelector('#mention-open-archive').click()`);
 await until(()=>evaluate(`!document.querySelector('[data-panel="recordings"]').hidden && document.querySelector('#recordings').children.length>0`));
 await until(()=>evaluate(`document.querySelector('.transcript-callsign')!==null`));
 await evaluate(`document.querySelector('[data-panel=recordings] audio').load()`);
 await until(()=>evaluate(`document.querySelector('[data-panel=recordings] audio').readyState>=1`));
 const marker=async time=>{await evaluate(`{const a=document.querySelector('[data-panel=recordings] audio');a.currentTime=${time};a.dispatchEvent(new Event('seeked'));}`);await sleep(80);return evaluate(`[...document.querySelectorAll('.transcript-playing')].map(n=>n.textContent).join('')`);};
 const first=await marker(.5), later=await marker(2.6), gap=await marker(1.7);
 if(!first||!later||first===later||gap)throw Error('approximate marker mismatch '+JSON.stringify({first,later,gap}));
 await marker(.5);
 fs.writeFileSync(path.join(root,'word-marker.png'),Buffer.from((await send('Page.captureScreenshot',{format:'png'})).data,'base64'));
 await evaluate(`{const input=document.querySelector('.recording-text textarea');input.value='Я довжелезне слово, прийом';input.dispatchEvent(new Event('input',{bubbles:true}));}`);
 const edited=await marker(.5);
 if(!edited||!['Я','довжелезне','слово','прийом'].includes(edited))throw Error('marker did not rebuild for edited text '+edited);
 await evaluate(`document.querySelector('[data-panel=recordings] audio').playbackRate=1.5`);
 if(await marker(.5)!==edited)throw Error('speed changed timeline mapping');
 console.log('word-marker',JSON.stringify({first,later,gap,edited}));

 const marked=await evaluate(`({text:document.querySelector('.transcript-preview').textContent,stored:localStorage.getItem('radio345:mention-terms')})`);
 if(marked.stored!=='Клен 12')throw Error('mention terms not persisted');
 fs.writeFileSync(path.join(root,'archive-highlights.png'),Buffer.from((await send('Page.captureScreenshot',{format:'png'})).data,'base64'));
 await evaluate(`{const original=window.fetch;window.__reviewedRecalls=[];window.fetch=async(url,options)=>{const response=await original(url,options);if(String(url).endsWith('/review')&&options?.method==='PUT'&&response.ok)window.__reviewedRecalls.push(await response.clone().json());return response;};}`);
 await evaluate(`[...document.querySelectorAll('#recordings .segmented button')].find(b=>b.textContent.includes('Шум ефіру')).click()`);
 await until(()=>evaluate(`!document.querySelector('#recordings .save-primary')?.disabled`));
 await until(()=>evaluate(`window.__reviewedRecalls.some(r=>r.label==='noise'&&r.telegram_recall?.status==='scheduled'&&r.telegram_recall.after_seconds>15)`));
 if(!await evaluate(`[...document.querySelectorAll('#recordings .save-status')].some(n=>n.textContent.includes('відкликання копій Telegram'))`))throw Error('scheduled recall is invisible after noise label');
 console.log('flows: noise label durably schedules known Telegram copy with visible undo grace');
 await evaluate(`document.querySelector('[data-group="mentions"]').click()`);
 await until(()=>evaluate(`document.querySelector('.mention-results')?.children.length===1`));
 console.log('flows: newly labelled noise removed when returning to selection');
 await evaluate(`document.querySelector('.mention-row > button').click()`);
 await until(()=>evaluate(`document.querySelector('#recordings .recording-text textarea')!==null`));
 await evaluate(`{const i=document.querySelector('#recordings .recording-text textarea');i.value='Дуб 34, прийом';i.dispatchEvent(new Event('input',{bubbles:true}));document.querySelector('#recordings .save-primary').click();}`);
 await until(()=>evaluate(`!document.querySelector('#recordings .save-primary')?.disabled`));
 await evaluate(`document.querySelector('[data-group="mentions"]').click()`);
 await until(()=>evaluate(`document.querySelector('.mention-results')?.children.length===0`));
 console.log('flows: old Whisper mention removed after reference correction');

 await evaluate(`document.querySelector('[data-group="recognition"]').click()`);
 await until(()=>evaluate(`document.querySelector('.row-list-view')!==null`));
 const fieldWidth=await evaluate(`document.querySelector('.row-list-view .pattern-line').getBoundingClientRect().width`);
 if(fieldWidth<300)throw Error('row text field too narrow: '+fieldWidth);
 fs.writeFileSync(path.join(root,'recognition.png'),Buffer.from((await send('Page.captureScreenshot',{format:'png'})).data,'base64'));
 await evaluate(`[...document.querySelectorAll('#tabs button')].find(b=>b.textContent==='Навчання').click()`);
 await until(()=>evaluate(`document.querySelector('#learning-explanation')!==null`));
 await evaluate(`document.querySelector('#learning-filter').value='all';document.querySelector('#learning-filter').dispatchEvent(new Event('change'))`);
 await until(()=>evaluate(`document.querySelector('#learning-items').textContent.includes('дорга')`));
 await evaluate(`{const i=document.querySelector('input[aria-label="Пошук навчених замін"]');i.value='дорга';i.dispatchEvent(new Event('input'));}`);
 await evaluate(`[...document.querySelectorAll('#learning-items button')].find(b=>b.textContent==='Дозволити').click()`);
 await until(()=>evaluate(`fetch('/api/learning').then(r=>r.json()).then(d=>d.counts.applies===1)`));
 console.log('flows: learned replacement found and approved through UI');

 const learningBeforeConflict=await evaluate(`fetch('/api/learning').then(r=>r.json())`);
 await evaluate(`{const search=document.querySelector('[aria-label="Пошук навчених замін"]');search.value='стежко';document.querySelector('#learning-filter').value='conflict';search.dispatchEvent(new Event('input'));}`);
 await until(()=>evaluate(`document.querySelector('#learning-items .learning-conflict')?.textContent.includes('стежко → стежку')===true`));
 const conflict=await evaluate(`{const details=document.querySelector('#learning-items .learning-conflict'),row=details.closest('tr');({rows:document.querySelectorAll('#learning-items tr').length,source:details.querySelector('a')?.getAttribute('href'),approval:[...row.querySelectorAll('button')].some(b=>b.textContent==='Дозволити'),explanation:details.textContent.includes('нічого не змінюйте')});}`);
 if(conflict.rows!==2||!conflict.source?.includes(encodeURIComponent('№77'))||conflict.approval||!conflict.explanation)throw Error('hidden learning competitor not explained '+JSON.stringify(conflict));
 await evaluate(`[...document.querySelectorAll('#learning-items button')].find(b=>b.textContent==='Показати всі варіанти').click()`);
 await until(()=>evaluate(`document.querySelector('#learning-filter').value==='all'&&document.querySelectorAll('#learning-items tr').length===3`));
 const learningAfterConflict=await evaluate(`fetch('/api/learning').then(r=>r.json())`);
 if(JSON.stringify(learningAfterConflict)!==JSON.stringify(learningBeforeConflict))throw Error('viewing learning conflict changed rules or votes');
 console.log('flows: hidden one-vote competitor, source and instructions visible; show all changes only view');

 await evaluate(`[...document.querySelectorAll('#tabs button')].find(b=>b.textContent==='Коди доби').click()`);
 await until(()=>evaluate(`document.querySelector('#codes-period')?.textContent.includes('діють')`));
 await evaluate(`document.querySelector('#codes-input').value='48 62';document.querySelector('#codes-save').click()`);
 await until(()=>evaluate(`document.querySelector('#codes-input-chips')?.children.length===1`));
 await evaluate(`{const i=document.querySelector('#codes-input');i.value='73 73';i.dispatchEvent(new Event('input'));[...document.querySelectorAll('#tabs button')].find(b=>b.textContent==='Словники').click();}`);
 await sleep(200);
 await evaluate(`[...document.querySelectorAll('#tabs button')].find(b=>b.textContent==='Коди доби').click()`);await sleep(250);
 if(await evaluate(`document.querySelector('#codes-input').value`)!=='73 73')throw Error('code draft lost across tabs');
 await evaluate(`[...document.querySelectorAll('#tabs button')].find(b=>b.textContent==='Словники').click()`);await sleep(200);
 await evaluate(`[...document.querySelectorAll('[data-panel=dictionaries] button')].find(b=>b.textContent.startsWith('Команди')).click()`);await sleep(200);
 await evaluate(`{const search=document.querySelector('[data-panel=dictionaries] .pattern-search');search.value='злива 42 42';search.dispatchEvent(new Event('input'));[...document.querySelectorAll('[data-panel=dictionaries] button')].find(b=>b.textContent.includes('Додати рядок')).click();const i=document.activeElement;if(i.value!=='злива 42 42'||search.value)throw Error('dictionary add did not use search text');const check=i.closest('.phrase-pattern-row').querySelector('input[type=checkbox]');if(!check.checked)throw Error('new dictionary row is disabled by default');if(!document.querySelector('[data-panel=dictionaries] .pattern-toolbar [data-action=save]'))throw Error('dictionary save missing from toolbar');}`);
 await until(()=>evaluate(`document.querySelector('[data-panel=dictionaries] .warn-list li')?.checkVisibility()===true && !document.querySelector('[data-panel=dictionaries] .warn-list').closest('details')`));
 const dictionaryCounts=await evaluate(`{const p=document.querySelector('#dictionary-lists'),tab=p.querySelector('.list-picker button.on');({top:Number(tab.textContent.split(' · ')[1]),enabled:[...p.querySelectorAll('.phrase-pattern-row input[type=checkbox]')].filter(i=>i.checked).length,duplicate:/Увімкнено [0-9]+|діючих рядків/.test(p.querySelector('.list-left').textContent)});}`);
 if(dictionaryCounts.top!==dictionaryCounts.enabled||dictionaryCounts.duplicate)throw Error('dictionary duplicate or stale counters '+JSON.stringify(dictionaryCounts));
 await evaluate(`[...document.querySelectorAll('[data-panel=dictionaries] aside button')].find(b=>b.textContent==='Перевірити рядки').click()`);
 await until(()=>evaluate(`document.querySelector('[data-panel=dictionaries] aside .summary')?.textContent==='Формат рядків правильний'`));
 await evaluate(`[...document.querySelectorAll('[data-panel=dictionaries] button')].find(b=>b.textContent==='Зберегти').click()`);
 await until(()=>evaluate(`fetch('/api/dictionaries').then(r=>r.json()).then(d=>d.commands.entries.some(e=>e.raw_line==='злива 42 42'&&e.enabled))`));
 await until(()=>evaluate(`document.querySelectorAll('[data-panel=dictionaries] .phrase-pattern-row.is-draft').length===0`));
 console.log('flows: code draft retained; spaced command added, enabled, saved through UI');

 await evaluate(`[...document.querySelectorAll('#tabs button')].find(b=>b.textContent==='Фільтри тексту').click()`);
 await until(()=>evaluate(`document.querySelector('#text-lists .list-picker')!==null`));
 const originalLists=await evaluate(`fetch('/api/text-lists').then(r=>r.json())`);
 await evaluate(`window.__evaluations=[];{const original=window.fetch;window.fetch=(url,options)=>{if(String(url).endsWith('/evaluate'))window.__evaluations.push({url,body:JSON.parse(options.body)});return original(url,options);};}`);
 for(const [type,title,line] of [
  ['phrase_templates','Шаблони зворотів','перевірка зв’язку'],
  ['phrase_patterns','Шаблони фраз','{П}, прийом'],
  ['aliases','Аліаси тексту','стежка: стешка'],
  ['callsign_aliases','Аліаси позивних','Клен: Клин'],
  ['blacklist','Фрази-галюцинації Whisper','синтетичний шум'],
  ['short_garbage','Короткий шум','сторонній шелест'],
 ]){
  await evaluate(`[...document.querySelectorAll('#text-lists .list-picker button')].find(b=>b.textContent.startsWith(${JSON.stringify(title)}+' ·')).click()`);
  await until(()=>evaluate(`document.querySelector('#text-lists .list-picker button.on')?.textContent.startsWith(${JSON.stringify(title)}+' ·')===true && document.querySelector('#text-lists .pattern-toolbar')!==null`));
  await evaluate(`{const search=document.querySelector('#text-lists .pattern-search');search.value=${JSON.stringify(line)};search.dispatchEvent(new Event('input'));[...document.querySelectorAll('#text-lists .pattern-toolbar button')].find(b=>b.textContent.startsWith('＋ Додати')).click();const i=document.activeElement;if(i.value!==${JSON.stringify(line)}||search.value)throw Error('list add did not use search text');if(!document.querySelector('#text-lists .pattern-toolbar [data-action=save]'))throw Error('list save missing from toolbar');const toggle=i.closest('.phrase-pattern-row').querySelector('input[type=checkbox]');if(${JSON.stringify(type)}==='phrase_patterns'&&toggle.checked)throw Error('new pattern enabled without explicit action');if(!toggle.checked){toggle.checked=true;toggle.dispatchEvent(new Event('change'));}}`);
  await until(()=>evaluate(`document.querySelector('#text-lists .warn-list li')?.checkVisibility()===true && !document.querySelector('#text-lists .warn-list').closest('details')`));
  const counts=await evaluate(`{const p=document.querySelector('#text-lists'),tab=p.querySelector('.list-picker button.on');({top:Number(tab.textContent.split(' · ')[1]),enabled:[...p.querySelectorAll('.phrase-pattern-row input[type=checkbox]')].filter(i=>i.checked).length,duplicate:/Увімкнено [0-9]+|діючих рядків/.test(p.querySelector('.list-left').textContent)});}`);
  if(counts.top!==counts.enabled||counts.duplicate)throw Error('list duplicate or stale counters '+JSON.stringify({type,...counts}));
  const visible=await evaluate(`{const b=[...document.querySelectorAll('#text-lists button')].find(b=>b.textContent==='Перевірити на моїх еталонах');b.checkVisibility()&&!b.closest('details')&&b.closest('aside.list-evaluation')!==null;}`);
  if(!visible)throw Error('draft check button hidden for '+type);
  await evaluate(`[...document.querySelectorAll('#text-lists button')].find(b=>b.textContent==='Перевірити на моїх еталонах').click()`);
  await until(()=>evaluate(`window.__evaluations.some(r=>r.url==='/api/text-lists/${type}/evaluate'&&r.body.entries.some(e=>e.raw_line===${JSON.stringify(line)}&&e.enabled))`));
  await until(()=>evaluate(`document.querySelector('#text-lists .list-evaluation .summary')?.checkVisibility()===true`));
  const layout=await evaluate(`{const left=document.querySelector('#text-lists .list-left').getBoundingClientRect(),check=document.querySelector('#text-lists .list-evaluation').getBoundingClientRect();({editorWidth:left.width,checkWidth:check.width,onRight:check.left>=left.right,overflow:document.documentElement.scrollWidth>innerWidth});}`);
  if(!layout.onRight||layout.checkWidth<256||layout.checkWidth>264||layout.editorWidth<500||layout.overflow)throw Error('check sidebar layout '+JSON.stringify(layout));
  if(type==='phrase_templates'){
   const scroll=await evaluate(`{const panel=document.querySelector('#text-lists .list-layout'),rows=panel.querySelector('.phrase-pattern-rows'),save=panel.querySelector('.pattern-toolbar [data-action=save]');({internal:rows.scrollHeight>rows.clientHeight,saveVisible:save.getBoundingClientRect().bottom<=panel.getBoundingClientRect().bottom&&save.getBoundingClientRect().top>=panel.getBoundingClientRect().top,outer:document.querySelector('main').scrollHeight>document.querySelector('main').clientHeight+2});}`);
   if(!scroll.internal||!scroll.saveVisible||scroll.outer)throw Error('long editor scrolling '+JSON.stringify(scroll));
  }
 }
 await evaluate(`[...document.querySelectorAll('#text-lists .list-picker button')].find(b=>b.textContent.startsWith('Підказка Whisper ·')).click()`);
 await until(()=>evaluate(`[...document.querySelectorAll('#text-lists aside button')].some(b=>b.textContent==='Перевірити рядки')`));
 if(await evaluate(`document.querySelector('#text-lists .phrase-pattern-disabled').checkVisibility()`))throw Error('empty disabled group remains visible');
 if(await evaluate(`document.querySelector('#text-lists .list-actions').checkVisibility()`))throw Error('empty status footer remains visible');
 await evaluate(`[...document.querySelectorAll('#text-lists aside button')].find(b=>b.textContent==='Перевірити рядки').click()`);
 await until(()=>evaluate(`document.querySelector('#text-lists aside .summary')?.textContent==='Формат рядків правильний'`));
 if(await evaluate(`document.querySelector('#text-lists aside').textContent.includes('Жодна фраза не змінилася')`))throw Error('prompt syntax check pretends to evaluate audio');
 const viewport=await evaluate(`({documentHeight:document.documentElement.scrollHeight,viewport:innerHeight,mainHeight:document.querySelector('main').clientHeight,mainScrollHeight:document.querySelector('main').scrollHeight})`);
 if(viewport.documentHeight>viewport.viewport+1||viewport.mainScrollHeight>viewport.mainHeight+2)throw Error('recognition whole page scroll '+JSON.stringify(viewport));
 await evaluate(`{const out=document.querySelector('#text-lists aside > div');for(let i=0;i<40;i++){const p=document.createElement('p');p.textContent='Синтетичний довгий результат перевірки';p.dataset.stress='1';out.append(p);}}`);
 const overflow=await evaluate(`{const panel=document.querySelector('#text-lists .list-layout'),aside=panel.querySelector('aside');({internal:aside.scrollHeight>aside.clientHeight,bounded:aside.getBoundingClientRect().bottom<=panel.getBoundingClientRect().bottom,outer:document.querySelector('main').scrollHeight>document.querySelector('main').clientHeight+2});}`);
 if(!overflow.internal||!overflow.bounded||overflow.outer)throw Error('check results not independently scrollable '+JSON.stringify(overflow));
 await evaluate(`document.querySelectorAll('[data-stress]').forEach(n=>n.remove())`);
 fs.writeFileSync(path.join(root,'recognition-check-sidebar.png'),Buffer.from((await send('Page.captureScreenshot',{format:'png'})).data,'base64'));
 for(const width of [1100,760]){
  await send('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
  const layout=await evaluate(`{const left=document.querySelector('#text-lists .list-left').getBoundingClientRect(),check=document.querySelector('#text-lists .list-evaluation').getBoundingClientRect();({onRight:check.left>=left.right,below:check.top>=left.bottom,checkWidth:check.width,overflow:document.documentElement.scrollWidth>innerWidth});}`);
  if(layout.overflow||width===1100&&(!layout.onRight||layout.checkWidth>264)||width===760&&!layout.below)throw Error('responsive check sidebar '+JSON.stringify({width,...layout}));
 }
 await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
 const afterChecks=await evaluate(`fetch('/api/text-lists').then(r=>r.json())`);
 if(JSON.stringify(afterChecks.lists)!==JSON.stringify(originalLists.lists)||afterChecks.revision!==originalLists.revision)throw Error('draft evaluation saved a text list');
 console.log('flows: six draft types evaluated without saving; prompt syntax checked separately');

 await evaluate(`document.querySelector('#sidebar-toggle').click()`);
 await evaluate(`document.querySelector('[data-group="system"]').click()`);
 await until(()=>evaluate(`document.querySelector('#telegram-retention input')!==null`));
 await until(()=>evaluate(`[...document.querySelectorAll('#telegram button')].some(b=>b.textContent==='Пропустити ці оновлення')`));
 await evaluate(`window.confirm=()=>true;[...document.querySelectorAll('#telegram button')].find(b=>b.textContent==='Пропустити ці оновлення').click()`);
 await until(()=>evaluate(`fetch('/api/summary').then(r=>r.json()).then(d=>d.telegram_sync.exhausted===0&&d.telegram_sync.skipped===1)`));
 await until(()=>evaluate(`!document.querySelector('#telegram button') && document.querySelector('#telegram').textContent.includes('Пропущено за вашим рішенням')`));
 console.log('flows: exhausted Telegram edit explicitly skipped through UI');

 // Усі типи програмної вставки перевіряються на видимих рядках, не hidden textarea.
 await evaluate(`(async()=>{for(const [type,entries] of [
  ['callsign_aliases',[{raw_line:'Клен: клін',enabled:true}]],
  ['phrase_patterns',[{raw_line:'{П}, я {П}, прийом',enabled:false}]],
 ]){const state=await fetch('/api/text-lists').then(r=>r.json());const response=await fetch('/api/text-lists/'+type,{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({entries,expected_revision:state.revision})});if(!response.ok)throw Error('cannot seed insertion test '+type+': '+await response.text());}})()`);
 await evaluate(`{const original=window.fetch;window.fetch=async(url,options)=>{const response=await original(url,options);if(String(url).endsWith('/api/attention')&&response.ok){const data=await response.json();data.suggestions_pending=false;data.suggestions=[
  {kind:'noise',title:'Синтетична перевірка шуму',target:'texts:short_garbage',count:2,new:2,examples:['два різні списки'],lines:[{list:'short_garbage',line:'синтетичний шерех'},{list:'blacklist',line:'синтетична довга фраза без мовлення'}]},
  {kind:'alias',title:'Синтетична перевірка аліаса',target:'texts:callsign_aliases',count:1,new:1,examples:['новий варіант'],lines:[{list:'callsign_aliases',callsign:'Клен',line:'клинок'}]},
  {kind:'dictionary',title:'Синтетична перевірка словника',target:'dictionaries',count:2,new:2,examples:['новий номер'],lines:[{list:'callsigns',callsign:'Клен',number:'13'},{list:'callsigns',callsign:'Явір',number:'19'}]},
  {kind:'pattern',title:'Синтетична перевірка шаблону',target:'texts:phrase_patterns',count:1,new:1,examples:['тимчасовий шаблон'],lines:[{list:'phrase_patterns',line:'{П}, я {П}, прийом'}]},
 ];return new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}});}return response;};}`);
 await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
 await evaluate(`document.querySelector('[data-group="overview"]').click();document.querySelector('#suggestions-refresh').click()`);
 await until(()=>evaluate(`[...document.querySelectorAll('.attention-item button')].some(b=>b.textContent==='Переглянути шум (1)')`));
 const beforeSuggestions=await evaluate(`fetch('/api/text-lists').then(r=>r.json())`);
 for(const width of [1440,390]){
  await send('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
  const layout=await evaluate(`{const row=[...document.querySelectorAll('.attention-item')].find(n=>n.textContent.includes('Синтетична перевірка шуму')),actions=row.querySelector('.attention-actions'),r=actions.getBoundingClientRect();({overflow:document.documentElement.scrollWidth>innerWidth,inside:r.right<=row.getBoundingClientRect().right+1&&r.left>=row.getBoundingClientRect().left});}`);
  if(layout.overflow||!layout.inside)throw Error('mixed suggestion actions overflow '+JSON.stringify({width,...layout}));
 }
 for(const [button,title,line,other] of [
  ['Переглянути шум (1)','Короткий шум','синтетичний шерех','синтетична довга фраза без мовлення'],
  ['Переглянути галюцинації (1)','Фрази-галюцинації Whisper','синтетична довга фраза без мовлення','синтетичний шерех'],
 ]){
  await evaluate(`document.querySelector('[data-group="overview"]').click();[...document.querySelectorAll('.attention-item button')].find(b=>b.textContent===${JSON.stringify(button)}).click()`);
  await until(()=>evaluate(`document.querySelector('[data-panel=texts]')?.hidden===false&&document.querySelector('#text-lists .list-picker button.on')?.textContent.startsWith(${JSON.stringify(title)}+' ·')===true&&[...document.querySelectorAll('#text-lists input.pattern-line')].some(i=>i.value===${JSON.stringify(line)})`));
  await until(()=>evaluate(`document.activeElement?.value===${JSON.stringify(line)}&&document.activeElement.closest('.phrase-pattern-row')?.classList.contains('is-draft')===true`));
  const visible=await evaluate(`{const field=document.activeElement,row=field.closest('.phrase-pattern-row'),r=row.getBoundingClientRect(),list=row.parentElement.getBoundingClientRect();({badge:row.querySelector('.pattern-draft')?.checkVisibility(),color:getComputedStyle(row).backgroundColor,inside:r.top>=list.top-1&&r.bottom<=list.bottom+1&&r.bottom<=innerHeight,changed:document.querySelector('#text-lists [data-action=save]').classList.contains('needs-save')});}`);
  if(!visible.badge||!visible.inside||!visible.changed||visible.color==='rgba(0, 0, 0, 0)')throw Error('inserted noise row is invisible '+JSON.stringify(visible));
  if(await evaluate(`[...document.querySelectorAll('#text-lists input.pattern-line')].some(i=>i.value===${JSON.stringify(other)})`))throw Error('proposal inserted into wrong list');
  const after=await evaluate(`fetch('/api/text-lists').then(r=>r.json())`);
  if(JSON.stringify(after.lists)!==JSON.stringify(beforeSuggestions.lists)||after.revision!==beforeSuggestions.revision)throw Error('review silently saved a noise proposal');
 }
 await send('Emulation.setDeviceMetricsOverride',{width:1440,height:1000,deviceScaleFactor:1,mobile:false});
 console.log('flows: mixed noise proposals open both editors independently, no automatic save, 1440/390 px');

 for(const [title,panel,line,selected,count] of [
  ['Синтетична перевірка аліаса','texts','Клен: клін, клинок','клинок',1],
  ['Синтетична перевірка словника','dictionaries','Клен: 12 13','13',2],
  ['Синтетична перевірка шаблону','texts','{П}, я {П}, прийом','{П}, я {П}, прийом',1],
 ]){
  await evaluate(`document.querySelector('[data-group="overview"]').click();[...document.querySelectorAll('.attention-item')].find(r=>r.textContent.includes(${JSON.stringify(title)})).querySelector('button').click()`);
  await until(()=>evaluate(`document.activeElement?.value===${JSON.stringify(line)}&&document.activeElement.closest('.phrase-pattern-row')?.classList.contains('is-draft')===true`));
  const marker=await evaluate(`{const p=document.querySelector('[data-panel=${panel}]'),i=document.activeElement;({count:p.querySelectorAll('.phrase-pattern-row.is-draft').length,selection:i.value.slice(i.selectionStart,i.selectionEnd),badge:i.closest('.phrase-pattern-row').querySelector('.pattern-draft')?.checkVisibility(),visible:i.getBoundingClientRect().top>=0&&i.getBoundingClientRect().bottom<=innerHeight});}`);
  if(marker.count!==count||marker.selection!==selected||!marker.badge||!marker.visible)throw Error('proposal draft is invisible '+JSON.stringify({title,...marker}));
  const after=await evaluate(`fetch('/api/text-lists').then(r=>r.json())`);
  if(JSON.stringify(after.lists)!==JSON.stringify(beforeSuggestions.lists)||after.revision!==beforeSuggestions.revision)throw Error('proposal preview saved changes');
  fs.writeFileSync(path.join(root,panel+'-draft-'+count+'.png'),Buffer.from((await send('Page.captureScreenshot',{format:'png'})).data,'base64'));
 }
 console.log('flows: merged alias, existing dictionary number/new callsign, and temporary pattern are colored and focused');

 await evaluate(`{const original=window.fetch;window.fetch=async(url,options)=>{const response=await original(url,options);let data=null;if(String(url).endsWith('/api/text-lists/noise-candidates'))data={records:2,items:[{text:'синтетичний стукіт',example:'синтетичний стукіт',count:2,reached_text:2,labels:{room:2},list:'short_garbage',flags:[],addable:true}]};if(String(url).endsWith('/api/text-lists/callsign-alias-candidates'))data={records:2,items:[{callsign:'Клен',heard:'клейн',count:2,similarity:90,flags:[],addable:true}]};return data?new Response(JSON.stringify(data),{status:200,headers:{'Content-Type':'application/json'}}):response;};}`);
 for(const [type,title,line] of [
  ['short_garbage','Короткий шум','синтетичний стукіт'],
  ['callsign_aliases','Аліаси позивних','Клен: клейн'],
 ]){
  await evaluate(`[...document.querySelectorAll('#text-lists .list-picker button')].find(b=>b.textContent.startsWith(${JSON.stringify(title)}+' ·')).click()`);
  await until(()=>evaluate(`document.querySelector('#text-lists .list-picker button.on')?.textContent.startsWith(${JSON.stringify(title)}+' ·')===true`));
  await evaluate(`document.querySelector('#text-lists details.list-right').open=true;document.querySelector('#text-lists .alias-candidates > button').click()`);
  await until(()=>evaluate(`[...document.querySelectorAll('#text-lists .alias-candidates button')].some(b=>b.textContent==='Додати')`));
  await evaluate(`[...document.querySelectorAll('#text-lists .alias-candidates button')].find(b=>b.textContent==='Додати').click()`);
  await until(()=>evaluate(`document.activeElement?.value===${JSON.stringify(line)}&&document.activeElement.closest('.phrase-pattern-row')?.classList.contains('is-recent')===true`));
  const saved=await evaluate(`{const row=document.activeElement.closest('.phrase-pattern-row');({label:row.querySelector('.pattern-draft').textContent,dirty:document.querySelector('#text-lists [data-action=save]').classList.contains('needs-save'),visible:row.querySelector('.pattern-draft').checkVisibility(),recent:document.querySelectorAll('#text-lists .is-recent').length});}`);
  if(saved.label!=='Щойно збережено'||saved.dirty||!saved.visible||saved.recent!==1)throw Error('saved candidate not visibly distinguished '+JSON.stringify({type,...saved}));
  if(!await evaluate(`fetch('/api/text-lists').then(r=>r.json()).then(d=>d.lists.${type}.entries.some(e=>e.raw_line===${JSON.stringify(line)}&&e.enabled))`))throw Error('candidate was not saved');
 }
 console.log('flows: saved noise and alias candidates are focused, blue, explicitly saved, and not dirty');

 // Номер помилки — той самий у перевірці/збереженні, навіть із вимкненим рядком.
 await evaluate(`(async()=>{const current=await fetch('/api/text-lists').then(r=>r.json());const entries=Array.from({length:24},(_,i)=>({raw_line:'синтетичний фільтр '+String.fromCharCode(1072+i),enabled:i!==0}));const response=await fetch('/api/text-lists/short_garbage',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({entries,expected_revision:current.revision})});if(!response.ok)throw Error(await response.text());})()`);
 await evaluate(`[...document.querySelectorAll('#text-lists .list-picker button')].find(b=>b.textContent.startsWith('Короткий шум ·')).click()`);
 await until(()=>evaluate(`document.querySelector('#text-lists .list-picker button.on')?.textContent.startsWith('Короткий шум ·')===true&&document.querySelectorAll('#text-lists .editor-row-number').length===24`));
 await evaluate(`{const search=document.querySelector('#text-lists .pattern-search');search.value='синтетичний текст 42';search.dispatchEvent(new Event('input'));[...document.querySelectorAll('#text-lists button')].find(b=>b.textContent==='＋ Додати рядок').click();}`);
 await until(()=>evaluate(`!![...document.querySelectorAll('#text-lists .row-issue-link')].find(b=>b.textContent==='Рядок 25 ↗')`));
 for (const width of [1440,390]) {
  await send('Emulation.setDeviceMetricsOverride',{width,height:1000,deviceScaleFactor:1,mobile:false});
  await evaluate(`{const search=document.querySelector('#text-lists .pattern-search');search.value='фільтр';search.dispatchEvent(new Event('input'));[...document.querySelectorAll('#text-lists .row-issue-link')].find(b=>b.textContent==='Рядок 25 ↗').click();}`);
  await until(()=>evaluate(`document.activeElement?.value==='синтетичний текст 42'`));
  const target=await evaluate(`{const field=document.activeElement,row=field.closest('.phrase-pattern-row'),rect=field.getBoundingClientRect();({number:row.querySelector('.editor-row-number').textContent,marked:row.classList.contains('is-targeted'),selected:field.value.slice(field.selectionStart,field.selectionEnd),search:document.querySelector('#text-lists .pattern-search').value,visible:rect.top>=0&&rect.bottom<=innerHeight});}`);
  if(target.number!=='25'||!target.marked||target.selected!=='синтетичний текст 42'||target.search||!target.visible)throw Error('warning did not reveal numbered row '+JSON.stringify({width,...target}));
 }
 fs.writeFileSync(path.join(root,'row-warning-jump.png'),Buffer.from((await send('Page.captureScreenshot',{format:'png'})).data,'base64'));
 console.log('flows: row 25 error jumps through search filter, highlights and selects actual numbered row at 1440/390 px');

 const screenshot=await send('Page.captureScreenshot',{format:'png'});fs.writeFileSync(path.join(root,'panel.png'),Buffer.from(screenshot.data,'base64'));
 await send('Emulation.setDeviceMetricsOverride',{width:1100,height:800,deviceScaleFactor:1,mobile:false});
 await evaluate(`document.querySelector('[data-group="people"]').click()`);
 await until(()=>evaluate(`document.querySelector('#tabs').children.length===3`));
 const details=await evaluate(`({selectionCards:document.querySelector('#recordings').children.length, sidebar:document.body.classList.contains('sidebar-collapsed'), navRole:document.querySelector('#tabs').getAttribute('role')})`);
 console.log(JSON.stringify({root,errors,details,fieldWidth}));if(errors.length)process.exitCode=1;
}finally{if(ws)ws.close();if(chrome)chrome.kill('SIGTERM');server.kill('SIGTERM');await sleep(500);}})().catch(e=>{console.error(e);process.exitCode=1;});
