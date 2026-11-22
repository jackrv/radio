  // Вибірки — локальний знімок списку IDs, без зміни записів або словників.
  let mentionSelection = null;
  const mentionPanel = el('section'); mentionPanel.dataset.panel = 'mentions'; mentionPanel.hidden = true;
  mentionPanel.append(el('h2', '', 'Вибірки за згадками'),
    el('p', 'muted', 'У розмічених записах шукаємо лише в еталоні, у нерозмічених — у поточному тексті розпізнавання. Шум, сигнали й кімната, а також нерозмічене, яке відсіяла програма, виключені. Хибне розпізнавання може пропустити згадку.'));
  const mentionForm = el('form', 'mention-form');
  const mentionTerms = el('input', 'mention-term-input'); mentionTerms.type = 'text'; mentionTerms.placeholder = '＋ Слово або фраза';
  mentionTerms.autocomplete = 'off';
  mentionTerms.setAttribute('aria-label', 'Згадки');
  const localInput = date => { const d = new Date(date); d.setMinutes(d.getMinutes()-d.getTimezoneOffset()); return d.toISOString().slice(0,16); };
  function mentionTimeValue(value) {
    const text=value.trim();
    const parts=/^(\d{1,2}):(\d{2})$/.exec(text) || /^(\d{1,2})(\d{2})$/.exec(text);
    if(!parts || Number(parts[1])>23 || Number(parts[2])>59)return '';
    return `${parts[1].padStart(2,'0')}:${parts[2]}`;
  }
  function mentionDialValue(part,x,y,size) {
    if(!Number.isFinite(size)||size<=0)return null;
    const dx=x-size/2,dy=y-size/2,radius=Math.hypot(dx,dy);
    if(radius<size*.13||radius>size*.55)return null;
    const turn=(Math.atan2(dx,-dy)+Math.PI*2)%(Math.PI*2);
    if(part==='minute')return Math.round(turn*60/(Math.PI*2))%60;
    if(part!=='hour')return null;
    const hour=Math.round(turn*12/(Math.PI*2))%12;
    return radius<size*.365 ? (hour===0?0:hour+12) : (hour===0?12:hour);
  }
  function mentionTimeControl(name) {
    const element=el('div','mention-time-control'),input=el('input','mention-time-input');
    input.type='text';input.inputMode='numeric';input.placeholder='ГГ:ХХ';input.autocomplete='off';input.required=true;
    input.pattern='(?:(?:[01]?[0-9]|2[0-3]):[0-5][0-9]|(?:[01]?[0-9]|2[0-3])[0-5][0-9])';
    input.setAttribute('aria-label',`${name}: час`);
    input.title='Час у форматі 09:26 або 0926';
    const validate=()=>input.setCustomValidity?.(input.value && !mentionTimeValue(input.value)?'Укажіть час від 00:00 до 23:59, наприклад 09:26.':'');
    input.addEventListener('input',validate);
    input.addEventListener('blur',()=>{input.value=mentionTimeValue(input.value)||input.value;validate();});
    const toggle=el('button','mention-time-toggle','◷');toggle.type='button';toggle.title='Вибрати час';
    toggle.setAttribute('aria-label',`${name}: вибрати час`);toggle.setAttribute('aria-haspopup','dialog');toggle.setAttribute('aria-expanded','false');
    const popup=el('div','mention-time-popover');popup.setAttribute('popover','auto');popup.setAttribute('role','dialog');popup.setAttribute('aria-label',`${name}: вибір часу`);
    const native=typeof popup.showPopover==='function';popup.hidden=!native;
    const heading=el('div','mention-time-heading'),current=el('div','mention-dial-time'),done=el('button','ghost small-button','Готово');done.type='button';
    const hourButton=el('button','mention-dial-part'),minuteButton=el('button','mention-dial-part');
    hourButton.type=minuteButton.type='button';hourButton.setAttribute('aria-label','Обрати годину');minuteButton.setAttribute('aria-label','Обрати хвилини');
    current.append(hourButton,el('span','',':'),minuteButton);heading.append(current,done);popup.append(heading);
    const caption=el('p','muted small'),hint=el('p','muted small'),face=el('div','mention-dial'),hand=el('span','mention-dial-hand'),center=el('span','mention-dial-center');
    face.tabIndex=0;face.setAttribute('role','slider');
    const hours=el('div','mention-dial-marks'),minutes=el('div','mention-dial-marks'),ticks=el('div','mention-dial-ticks'),choices=[];
    let part='hour',dragging=false,beforeDrag='';
    for(let i=0;i<60;i++) {
      const tick=el('i');tick.style.transform=`rotate(${i*6}deg)`;ticks.append(tick);
    }
    for(const [kind,values,marks] of [['hour',Array.from({length:24},(_,i)=>i),hours],['minute',Array.from({length:12},(_,i)=>i*5),minutes]]) {
      for(const value of values) {
        const mark=el('span','mention-dial-number',String(value).padStart(2,'0'));
        const angle=(kind==='hour'?value%12*30:value*6)*Math.PI/180;
        const radius=kind==='hour'&&(value===0||value>12)?29.5:43;
        mark.style.left=`${50+Math.sin(angle)*radius}%`;mark.style.top=`${50-Math.cos(angle)*radius}%`;
        if(radius<40)mark.className+=' inner';
        mark.setAttribute('aria-hidden','true');choices.push({mark,part:kind,value});marks.append(mark);
      }
    }
    face.append(ticks,hand,hours,minutes,center);
    const isOpen=()=>native?popup.matches(':popover-open'):!popup.hidden;
    const close=focus=>{
      dragging=false;
      if(native && isOpen())popup.hidePopover();
      if(!native)popup.hidden=true;
      toggle.setAttribute('aria-expanded','false');
      if(focus)toggle.focus({preventScroll:true});
    };
    const render=()=>{
      const value=mentionTimeValue(input.value)||'00:00',hour=Number(value.slice(0,2)),minute=Number(value.slice(3));
      hourButton.textContent=value.slice(0,2);minuteButton.textContent=value.slice(3);
      hourButton.classList.toggle('on',part==='hour');minuteButton.classList.toggle('on',part==='minute');
      hourButton.setAttribute('aria-pressed',String(part==='hour'));minuteButton.setAttribute('aria-pressed',String(part==='minute'));
      caption.textContent=part==='hour'?'Оберіть годину':'Оберіть хвилини';
      hint.textContent=part==='hour'?'Зовні 1–12, усередині 13–00.':'Клацніть або перетягніть стрілку.';
      hours.hidden=part!=='hour';minutes.hidden=ticks.hidden=part!=='minute';
      const selected=part==='hour'?hour:minute;
      for(const choice of choices) {
        choice.mark.classList.toggle('on',choice.part===part&&choice.value===selected);
      }
      hand.style.transform=`rotate(${part==='hour'?hour%12*30:minute*6}deg)`;
      hand.style.height=`${part==='hour'&&(hour===0||hour>12)?29.5:43}%`;
      face.setAttribute('aria-label',`${name}: ${part==='hour'?'години':'хвилини'}`);
      face.setAttribute('aria-valuemin','0');face.setAttribute('aria-valuemax',part==='hour'?'23':'59');face.setAttribute('aria-valuenow',String(selected));
    };
    const setPart=next=>{part=next;render();face.focus({preventScroll:true});};
    const setValue=value=>{
      const previous=mentionTimeValue(input.value)||'00:00',text=String(value).padStart(2,'0');
      input.value=part==='hour'?`${text}:${previous.slice(3)}`:`${previous.slice(0,2)}:${text}`;
      validate();render();
    };
    const finish=()=>{if(part==='hour')setPart('minute');else close(true);};
    hourButton.addEventListener('click',()=>setPart('hour'));minuteButton.addEventListener('click',()=>setPart('minute'));
    const pointerValue=event=>{const r=face.getBoundingClientRect();return mentionDialValue(part,event.clientX-r.left,event.clientY-r.top,r.width);};
    face.addEventListener('pointerdown',event=>{
      if(event.button!==0)return;const value=pointerValue(event);if(value===null)return;
      event.preventDefault();beforeDrag=input.value;dragging=true;face.setPointerCapture(event.pointerId);setValue(value);
    });
    face.addEventListener('pointermove',event=>{if(dragging){const value=pointerValue(event);if(value!==null)setValue(value);}});
    face.addEventListener('pointerup',event=>{
      if(!dragging)return;dragging=false;
      if(face.hasPointerCapture(event.pointerId))face.releasePointerCapture(event.pointerId);
      finish();
    });
    face.addEventListener('pointercancel',()=>{if(dragging){dragging=false;input.value=beforeDrag;validate();render();}});
    face.addEventListener('keydown',event=>{
      const max=part==='hour'?23:59,current=Number((mentionTimeValue(input.value)||'00:00').slice(part==='hour'?0:3,part==='hour'?2:5));
      if(['ArrowUp','ArrowRight','ArrowDown','ArrowLeft','Home','End'].includes(event.key)) {
        event.preventDefault();setValue(event.key==='Home'?0:event.key==='End'?max:(current+(['ArrowUp','ArrowRight'].includes(event.key)?1:max))%(max+1));
      } else if(event.key==='Enter'||event.key===' '){event.preventDefault();finish();}
    });
    popup.append(caption,face,hint);
    const place=()=>{
      const anchor=toggle.getBoundingClientRect(),rect=popup.getBoundingClientRect();
      popup.style.left=`${Math.max(12,Math.min(anchor.right-rect.width,window.innerWidth-rect.width-12))}px`;
      popup.style.top=`${Math.max(12,anchor.bottom+8+rect.height<=window.innerHeight-12?anchor.bottom+8:Math.min(anchor.top-rect.height-8,window.innerHeight-rect.height-12))}px`;
    };
    toggle.addEventListener('click',()=>{
      if(isOpen()){close(false);return;}
      part='hour';render();if(native)popup.showPopover();else popup.hidden=false;
      toggle.setAttribute('aria-expanded','true');
      place();
    });
    done.addEventListener('click',()=>close(true));
    popup.addEventListener('toggle',()=>toggle.setAttribute('aria-expanded',String(isOpen())));
    popup.addEventListener('keydown',event=>{if(event.key==='Escape'){event.preventDefault();close(true);}});
    // Старі браузери без Popover API також отримують закриття поза панеллю.
    if(!native)document.addEventListener?.('pointerdown',event=>{if(!element.contains(event.target))close(false);});
    document.addEventListener?.('scroll',()=>{if(isOpen())close(false);},true);
    globalThis.addEventListener?.('resize',()=>{if(isOpen())place();});
    element.append(input,toggle,popup);
    return {element,input};
  }
  function mentionDateControl(title, name, {now=false}={}) {
    const element=el('fieldset','mention-date-control');
    const legend=el('legend','muted small',title);element.append(legend);
    const fields=el('div','mention-date-fields'), date=el('input'), clock=mentionTimeControl(name), time=clock.input;
    date.type='date';
    for(const field of [date,time]) { field.required=true; field.lang='uk-UA'; }
    date.setAttribute('aria-label',`${name}: дата`); time.setAttribute('aria-label',`${name}: час`);
    // День назад/вперед — одним кліком, без календаря.
    const step=delta=>{
      const d=new Date(`${date.value||localInput(Date.now()).slice(0,10)}T00:00`);d.setDate(d.getDate()+delta);
      date.value=`${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,'0')}-${String(d.getDate()).padStart(2,'0')}`;
      element.dispatchEvent?.(new Event('change',{bubbles:true}));
    };
    const prev=el('button','mention-day-step','‹'),next=el('button','mention-day-step','›');
    prev.type=next.type='button';prev.title='На день раніше';next.title='На день пізніше';
    prev.setAttribute('aria-label',`${name}: на день раніше`);next.setAttribute('aria-label',`${name}: на день пізніше`);
    prev.addEventListener('click',()=>step(-1));next.addEventListener('click',()=>step(1));
    fields.append(prev,date,next,clock.element);element.append(fields);
    if(now) {
      const nowButton=el('button','link-button mention-now','зараз');nowButton.type='button';nowButton.title='Поставити поточний час';
      nowButton.addEventListener('click',()=>{control.value=localInput(Date.now()+60000);element.dispatchEvent?.(new Event('change',{bubbles:true}));});
      legend.append(' · ',nowButton);
    }
    const control={element,date,time,
      get value() { const value=mentionTimeValue(time.value);return date.value && value ? `${date.value}T${value}` : ''; },
      set value(value) { date.value=value.slice(0,10);time.value=value.slice(11,16);time.setCustomValidity?.(''); },
    };
    return control;
  }
  const mentionSince=mentionDateControl('Від','Початок періоду'), mentionUntil=mentionDateControl('До','Кінець періоду',{now:true});
  mentionTerms.required=true;
  mentionSince.value = localInput(Date.now()-86400000); mentionUntil.value = localInput(Date.now()+60000);
  const mentionOrder = el('select'); mentionOrder.setAttribute('aria-label', 'Порядок часу');
  for (const [value,title] of [['old','Від давніх до нових'],['new','Від нових до давніх'],['unreviewed','Спершу нерозмічені']]) {
    const option = el('option', '', title); option.value = value; mentionOrder.append(option);
  }
  let mentionValues = [];
  const mentionPills=el('div','mention-term-chips'); mentionPills.setAttribute('aria-label','Пошукові згадки');
  const splitMentionTerms=value=>value.split(/[,;\n]/).map(s=>s.trim().replace(/\s+/g,' ')).filter(Boolean);
  function allMentionTerms() { return [...mentionValues,...splitMentionTerms(mentionTerms.value)].join(', '); }
  function saveMentionTerms() { try { localStorage.setItem('radio345:mention-terms',allMentionTerms()); } catch (_) {} }
  function renderMentionPills() {
    mentionPills.replaceChildren(); mentionTerms.required=!mentionValues.length;
    for(const term of mentionValues) {
      const pill=el('button','mention-term-pill',term+' ×');pill.type='button';
      pill.title='Прибрати згадку';pill.setAttribute('aria-label',`Прибрати згадку: ${term}`);
      pill.addEventListener('click',()=>{mentionValues=mentionValues.filter(v=>v!==term);renderMentionPills();saveMentionTerms();mentionTerms.focus?.();});
      mentionPills.append(pill);
    }
  }
  function commitMentionTerms() {
    const seen=new Set();
    mentionValues=[...mentionValues,...splitMentionTerms(mentionTerms.value)].filter(term=>{
      const key=term.toLocaleLowerCase('uk');if(seen.has(key))return false;seen.add(key);return true;
    });
    mentionTerms.value='';renderMentionPills();saveMentionTerms();
  }
  try { mentionTerms.value=localStorage.getItem('radio345:mention-terms') || ''; } catch (_) {}
  commitMentionTerms();
  mentionTerms.addEventListener('keydown',event=>{
    if(!event.isComposing && ['Enter',',',';'].includes(event.key)){event.preventDefault();commitMentionTerms();}
  });
  mentionTerms.addEventListener('blur',commitMentionTerms);
  mentionTerms.addEventListener('input',()=>{
    if(/[,;\n]/.test(mentionTerms.value))commitMentionTerms();else saveMentionTerms();
  });
  // Однорядковий input прибирає переноси; розбираємо багаторядкову вставку до цього.
  mentionTerms.addEventListener('paste',event=>{
    const text=event.clipboardData?.getData('text');
    if(text && /[,;\n]/.test(text)) {
      event.preventDefault();
      const start=mentionTerms.selectionStart ?? mentionTerms.value.length, end=mentionTerms.selectionEnd ?? start;
      mentionTerms.value=mentionTerms.value.slice(0,start)+text+mentionTerms.value.slice(end);
      commitMentionTerms();
    }
  });
  const mentionSearch = el('button', '', 'Знайти'); mentionSearch.type = 'submit';
  const mentionArchive = el('button', 'ghost', 'Відкрити вибірку в архіві'); mentionArchive.id='mention-open-archive'; mentionArchive.type = 'button'; mentionArchive.disabled = true;
  const mentionFeedback = el('p', 'muted small'); mentionFeedback.setAttribute('role', 'status'); mentionFeedback.textContent='Укажіть згадки й період. Знайдені записи можна відкрити разом в архіві для розмітки.';
  const mentionResults = el('div', 'mention-results');
  const mentionMore = el('button', 'ghost', 'Показати ще'); mentionMore.type = 'button'; mentionMore.hidden = true;
  const mentionTermLabel = el('div', 'mention-terms'), mentionTermLine=el('div','mention-term-line');
  mentionTermLine.append(mentionPills,mentionTerms);
  mentionTermLabel.append(el('span', 'muted small', 'Що шукаємо'), mentionTermLine, el('small','muted','Enter або кома — додати. × — прибрати. Список зберігається.'));
  // Пресети періоду: один клік підставляє період і, якщо згадки задано, одразу шукає.
  const MENTION_LAST_KEY='radio345:mention-last-until';
  const lastMentionUntil=()=>{try{const v=Number(localStorage.getItem(MENTION_LAST_KEY));return v>0?v:null;}catch(_){return null;}};
  const mentionPresets=el('div','mention-period-presets'), presetButtons=[];
  mentionPresets.append(el('span','muted small','Період'));
  const HOUR=3600000;
  const presetRange=kind=>{
    const now=Date.now(), midnight=new Date(now);midnight.setHours(0,0,0,0);
    const ago={'1h':HOUR,'3h':3*HOUR,'12h':12*HOUR,'24h':24*HOUR,'7d':7*24*HOUR}[kind];
    if(ago)return [now-ago,now+60000];
    if(kind==='today')return [midnight,now+60000];
    if(kind==='yesterday'){const start=new Date(midnight);start.setDate(start.getDate()-1);return [start,midnight];}
    if(kind==='last'){const last=lastMentionUntil();return last?[last,now+60000]:null;}
    return null;
  };
  const markPreset=kind=>{for(const b of presetButtons)b.classList.toggle('on',b.dataset.preset===kind);};
  for(const [kind,title] of [['1h','1 год'],['3h','3 год'],['12h','12 год'],['24h','24 год'],['today','Сьогодні'],['yesterday','Учора'],['7d','7 днів'],['last','Від минулої вибірки']]) {
    const button=el('button','ghost small-button',title);button.type='button';button.dataset.preset=kind;
    button.title='Підставити період і знайти';
    button.addEventListener('click',()=>{
      const range=presetRange(kind);if(!range)return;
      mentionSince.value=localInput(range[0]);mentionUntil.value=localInput(range[1]);markPreset(kind);
      if(allMentionTerms())mentionForm.requestSubmit?.();
    });
    presetButtons.push(button);mentionPresets.append(button);
  }
  const lastPreset=presetButtons.at(-1);
  function renderLastPreset() {
    const last=lastMentionUntil();lastPreset.disabled=!last;
    lastPreset.title=last?`Від ${new Date(last).toLocaleString('uk-UA',{day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'})} (кінець минулої вибірки) до зараз`:'Ще не було вибірки в цьому браузері';
  }
  renderLastPreset();
  for(const control of [mentionSince,mentionUntil]) {
    control.element.addEventListener?.('input',()=>markPreset(null));
    control.element.addEventListener?.('change',()=>markPreset(null));
  }
  const mentionDates = el('div', 'mention-controls');
  // Порядок — компактно в рядку пресетів праворуч, не займає окремого місця.
  const mentionOrderLabel=el('label','mention-order');mentionOrderLabel.append(el('span','muted small','Порядок'),mentionOrder);
  mentionPresets.append(mentionOrderLabel);
  mentionDates.append(mentionSince.element,mentionUntil.element);
  const mentionButtons = el('div', 'toolbar'); mentionButtons.append(mentionSearch, mentionArchive);
  mentionForm.append(mentionTermLabel, mentionPresets, mentionDates, mentionButtons);
  mentionPanel.append(mentionForm, mentionFeedback, mentionResults, mentionMore);
  document.querySelector('main')?.append(mentionPanel);
  let mentionOffset = 0, mentionLoaded = 0, mentionQuery = null, mentionRevision=0;
  async function runMentions(reset, keepQuery = false) {
    const revision=++mentionRevision;
    if (reset) {
      if(!keepQuery)commitMentionTerms();
      mentionOffset = 0; mentionLoaded = 0; mentionSelection = null; mentionArchive.disabled = true;
      mentionResults.replaceChildren();
      if(!keepQuery)mentionQuery = new URLSearchParams({terms:allMentionTerms(), since:new Date(mentionSince.value).getTime()/1000,
        until:new Date(mentionUntil.value).getTime()/1000, order:mentionOrder.value, limit:'100'});
      if(!keepQuery){
        // «Від минулої вибірки»: кінець цього періоду (не пізніше зараз) — початок наступного.
        try{localStorage.setItem(MENTION_LAST_KEY,String(Math.min(new Date(mentionUntil.value).getTime(),Date.now())));}catch(_){}
        renderLastPreset();
      }
    }
    const query = new URLSearchParams(mentionQuery); query.set('offset', mentionOffset);
    mentionSearch.disabled = true; mentionMore.disabled = true;
    try {
      const result = reset
        ? await request('/api/mentions','POST',{terms:query.get('terms'),since:Number(query.get('since')),until:Number(query.get('until')),order:query.get('order')})
        : await request(`/api/mentions?selection=${mentionSelection}&offset=${mentionOffset}&limit=100&order=${mentionQuery.get('order')}`);
      if(revision!==mentionRevision)return;
      if (result.selection) mentionSelection = result.selection;
      mentionArchive.disabled = !mentionSelection || !result.total;
      for (const item of result.items) {
        const card = el('article', item.reviewed ? 'mention-row reviewed' : 'mention-row unreviewed');
        const open = el('button', 'ghost small-button', 'Розмітити'); open.type = 'button';
        open.addEventListener('click', () => {
          archiveSelection = ''; $('recording-search').value = item.id;
          $('recording-view').value = 'all'; $('recording-period').value = 'all';
          loaded.delete('recordings'); show('recordings');
        });
        const when=new Date(item.created*1000);
        const stamp=el('time','mention-time'); stamp.dateTime=when.toISOString();
        stamp.append(el('b','mention-clock',when.toLocaleTimeString('uk-UA',{hour:'2-digit',minute:'2-digit',second:'2-digit'})),el('small','mention-date',when.toLocaleDateString('uk-UA')));
        const body = el('div', 'mention-body');
        body.append(el('p', '', item.text));
        const tags=el('div','mention-tags'); tags.append(el('span',item.reviewed?'badge':'badge cand',item.reviewed?'Розмічено':'Не розмічено'));
        for(const term of item.mentions)tags.append(el('span','badge',term));
        body.append(tags);
        card.append(stamp, body, open);
        mentionResults.append(card);
      }
      mentionLoaded += result.items.length; mentionOffset = mentionLoaded;
      const sorting=mentionQuery.get('order')==='unreviewed'?'Спершу нерозмічені; у кожній групі — за часом.':'За вибраним порядком часу.';
      mentionFeedback.textContent = `Знайдено ${result.total}; показано ${mentionLoaded}. ${sorting} Нерозмічені виділено кольором. Шум, сигнали й кімнату виключено. Період зафіксовано на час пошуку.`;
      mentionMore.hidden = mentionLoaded >= result.total;
    } catch (error) { if(revision===mentionRevision)mentionFeedback.textContent = error.message; }
    finally { if(revision===mentionRevision){mentionSearch.disabled = false; mentionMore.disabled = false;} }
  }
  mentionForm.addEventListener('submit', event => { event.preventDefault(); action(() => runMentions(true)); });
  mentionOrder.addEventListener('change',()=>{
    if(!mentionQuery)return;
    mentionQuery.set('order',mentionOrder.value);
    if(mentionSelection) {
      mentionOffset=0;mentionLoaded=0;mentionResults.replaceChildren();
      return runMentions(false);
    } else {
      // Зміна порядку під час першого пошуку також має перемогти його стару відповідь.
      return runMentions(true, true);
    }
  });
  mentionMore.addEventListener('click', () => action(() => runMentions(false)));
  mentionArchive.addEventListener('click', () => {
    archiveSelection = mentionSelection;
    $('recording-search').value = ''; $('recording-view').value = 'all'; $('recording-period').value = 'all';
    if ($('recording-order')) $('recording-order').value = mentionQuery?.get('order') || 'old';
    loaded.delete('recordings'); show('recordings');
  });
  loaders.mentions = async () => {
    // Після розмітки в архіві колишня згадка могла стати шумом/сигналом/кімнатою.
    // Перечитуємо той самий знімок, не створюючи новий пошук за зміненою чернеткою.
    if (mentionSelection) {
      mentionOffset=0;mentionLoaded=0;mentionResults.replaceChildren();
      await runMentions(false);
    }
  };
  // ---------- Кінець вибірок ----------

  const clearSelection = el('button', 'ghost small-button', 'Вийти з вибірки'); clearSelection.id='clear-recording-selection'; clearSelection.hidden=!archiveSelection; clearSelection.type = 'button';
  clearSelection.title = 'Повернути весь архів замість знімка знайдених записів';
  clearSelection.addEventListener('click', () => { archiveSelection = ''; action(() => recordings(true)); });
  $('recording-search')?.parentElement?.append(clearSelection);

  // Згортання меню не приховує назви від допоміжних технологій.
  const sidebarToggle = $('brand');
  function collapseSidebar(collapsed) {
    document.body.classList.toggle('sidebar-collapsed', collapsed);
    sidebarToggle?.setAttribute('aria-expanded', String(!collapsed));
    const toggleText = collapsed ? 'Розгорнути меню' : 'Згорнути меню';
    sidebarToggle?.setAttribute('aria-label', toggleText); if (sidebarToggle) sidebarToggle.title = toggleText;
    try { localStorage.setItem('radio345:sidebar', collapsed ? 'collapsed' : 'open'); } catch (_) {}
    if(['texts','dictionaries'].includes(view))requestAnimationFrame(fitRecognition);
  }
  const navPaths={overview:'M3 10 12 3l9 7v11h-6v-7H9v7H3Z',recordings:'M4 4h16v16H4ZM9 8l7 4-7 4Z',
    mentions:'M10 3a7 7 0 1 0 0 14 7 7 0 0 0 0-14M15 15l6 6',
    recognition:'M3 12h3l2-7 4 14 4-14 2 7h3',signals:'M5 17h14l-2-3V9a5 5 0 0 0-10 0v5ZM10 21h4',
    people:'M8 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8M2 21v-3a5 5 0 0 1 10 0v3M15 4a4 4 0 0 1 0 8M16 15a5 5 0 0 1 6 6',
    analysis:'M3 3v18h18M7 15l4-5 4 3 6-8',system:'M4 5h16v6H4ZM4 15h16v6H4ZM7 8h2M7 18h2'};
  for (const b of document.querySelectorAll('#nav button')) {
    b.title=GROUPS[b.dataset.group]?.[0] || b.textContent.trim(); b.setAttribute('aria-label', b.title);
    const icon=svg('svg',{viewBox:'0 0 24 24','aria-hidden':'true',width:'24',height:'24',fill:'none',stroke:'currentColor','stroke-width':'1.7','stroke-linecap':'round','stroke-linejoin':'round'});
    icon.append(svg('path',{d:navPaths[b.dataset.group] || navPaths.overview}));
    b.querySelector('span')?.replaceChildren(icon);
    // Підпис в окремому елементі: при згортанні він лише зникає, іконка не зсувається.
    for (const node of [...b.childNodes]) if (node.nodeType === 3 && node.textContent.trim()) {
      const label = el('em', 'nav-label', node.textContent.trim()); node.replaceWith(label);
    }
  }
  // Після анімації ширини — перерахувати висоту редакторів.
  document.querySelector('.app')?.addEventListener('transitionend', event => {
    if (event.target === event.currentTarget && ['texts','dictionaries'].includes(view)) fitRecognition();
  });
  try { collapseSidebar(localStorage.getItem('radio345:sidebar') === 'collapsed'); } catch (_) {}
  const toggleSidebar = () => { if (matchMedia('(min-width: 901px)').matches) collapseSidebar(!document.body.classList.contains('sidebar-collapsed')); };
  sidebarToggle?.addEventListener('click', toggleSidebar);
  sidebarToggle?.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') toggleSidebar(); });

  // Роль у прев'ю — кнопками; прихований select зберігає чинний контракт.
  const roleSelect = $('role');
  if (roleSelect) {
    const buttons = el('div', 'segmented'); buttons.setAttribute('aria-label', 'Роль перегляду Mini App');
    for (const option of roleSelect.options) {
      const b = el('button', 'ghost', option.textContent); b.type = 'button';
      b.addEventListener('click', () => { roleSelect.value=option.value; roleSelect.dispatchEvent(new Event('change')); });
      const update = () => { b.classList.toggle('on', roleSelect.value===option.value); b.setAttribute('aria-pressed',String(roleSelect.value===option.value)); };
      roleSelect.addEventListener('change', update); update(); buttons.append(b);
    }
    roleSelect.hidden = true; roleSelect.after?.(buttons);
  }
