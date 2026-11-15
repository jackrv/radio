"use strict";
(() => {
  const $ = id => document.getElementById(id);
  let current, busy = false;
  const labels = {USE_LLM:"Корекція Qwen",VAD_LEVEL:"Рівень VAD",SILENCE_END_MS:"Тиша до завершення, мс",MIN_SPEECH_MS:"Мінімальне мовлення, мс",MAX_SEGMENT_S:"Максимальний сегмент, с"};
  const metricNames = {total_latency_s:"До готового тексту, с",queue_wait_s:"Очікування черги, с",whisper_s:"Whisper, с",qwen_s:"Qwen, с",speech_tail_s:"Завершення VAD, с",rtf:"RTF до тексту"};
  const note = text => { $("feedback").textContent = text; };
  async function request(path, method="GET", body, headers={}) {
    const options = {method,headers,credentials:"same-origin",cache:"no-store"};
    if (body !== undefined) {
      if (body instanceof Blob) options.body = body;
      else { options.body=JSON.stringify(body); options.headers={...headers,"Content-Type":"application/json"}; }
    }
    const response=await fetch(path,options);
    if (!response.ok) {
      const error=await response.json().catch(()=>({}));
      throw new Error(typeof error.detail==="string" ? error.detail : `HTTP ${response.status}. Перевір доступ або онови сторінку.`);
    }
    return response.status===204 ? null : response.json();
  }
  function text(tag,value){const item=document.createElement(tag);item.textContent=value;return item;}
  function preview() {
    const role=$("role").value;
    $("permissions").textContent={viewer:"Лише стан ефіру. Журнал, словники, аудіо, налаштування й користувачі недоступні.",editor:"Журнал, аудіо, виправлення, словники й особисті сповіщення. Користувачі та керування приймачем недоступні.",admin:"Усі розділи. У цьому перегляді будь-який запис однаково заборонено."}[role];
    $("preview").src=`/preview/${role}/`;
  }
  async function refresh() {
    current=await request("/api/summary");
    const o=current.overview;
    $("summary").textContent=`Приймач: ${current.receiver} · Подій: ${o.events} · Виправлених фраз: ${o.corrections} · Правил: ${o.active_rules} активних / ${o.total_rules} загалом · Рядків словників: ${o.dictionary_entries} · Копій Telegram: ${current.telegram_sync.copies}, оновлень у черзі: ${current.telegram_sync.pending}, вичерпано спроб: ${current.telegram_sync.exhausted}`;
    $("models").textContent=`Whisper: ${current.settings.whisper_model} · Qwen: ${current.settings.correction_model} · Фільтр сповіщень: ${current.notification_filter ? "увімкнено" : "вимкнено"}`;
    $("fields").replaceChildren();
    for (const [key,value] of Object.entries(current.settings.values)) {
      const label=text("label",labels[key]||key),input=document.createElement("input");
      input.id=`setting-${key}`;input.type=typeof value==="boolean" ? "checkbox" : "number";
      if(input.type==="checkbox") input.checked=value; else {input.value=value;input.step="1";input.required=true;}
      label.append(input);$("fields").append(label);
    }
    const d=await request("/api/diagnostics");
    $("diagnostics").textContent=JSON.stringify(d,null,2);
    $("metrics").replaceChildren();
    if(d.available){
      const table=document.createElement("table"), head=document.createElement("tr");
      for(const h of ["Затримка","Середня","p95 (128)","Максимум","Викликів"])head.append(text("th",h)); table.append(head);
      for(const [key,label]of Object.entries(metricNames)){
        const item=d.asr_latency?.ready?.[key];if(!item)continue;
        const row=document.createElement("tr");
        for(const value of [label,item.mean?.toFixed(3),item.recent_p95?.toFixed(3),item.max?.toFixed(3),item.count])row.append(text("td",value??"—"));table.append(row);
      }$("metrics").append(table);
      if(d.updated_at)$("metrics").append(text("p",`Знімок: ${new Date(d.updated_at*1000).toLocaleString("uk-UA")}`));
    }
    await samples();
  }
  async function samples(){
    const data=await request("/api/samples");$("samples").replaceChildren();
    for(const item of data.items){const row=text("li",`${item.id} · ${item.duration_s?.toFixed(2)??"непридатний"} с `);
      if(item.managed){const remove=text("button","Видалити");remove.addEventListener("click",()=>action(async()=>{if(!confirm("Видалити цей зразок?"))return;await request(`/api/samples/${item.id}`,"DELETE");await samples();note("Зразок видалено. Потрібен перезапуск приймача.");}));row.append(remove);}$("samples").append(row);}
  }
  async function action(callback){if(busy)return;busy=true;try{await callback();}catch(error){note(error.message);}finally{busy=false;}}
  function selected(){const file=$("sample-file").files[0];if(!file)throw new Error("Вибери WAV-файл.");if(file.size>200000)throw new Error("Файл завеликий.");return file;}
  $("refresh").addEventListener("click",()=>action(async()=>{await refresh();note("Дані оновлено.");}));
  $("role").addEventListener("change",preview);
  $("settings").addEventListener("submit",event=>{event.preventDefault();action(async()=>{
    if(!confirm("Зберегти налаштування? Вони застосуються після наступного перезапуску."))return;
    const values={};for(const key of Object.keys(current.settings.values)){const input=$(`setting-${key}`);values[key]=input.type==="checkbox" ? input.checked : Number(input.value);}
    await request("/api/settings","PUT",{values,expected_revision:current.settings.revision});await refresh();note("Збережено. Приймач не перезапускався.");
  });});
  $("sample-add").addEventListener("click",()=>action(async()=>{
    if(!$("sample-confirm").checked)throw new Error("Спершу підтвердь відсутність мовлення у зразку.");
    await request("/api/samples","PUT",selected(),{"X-Confirm-Isolated":"yes","Content-Type":"audio/wav"});await samples();note("Зразок додано. Потрібен перезапуск приймача.");
  }));
  $("sample-test").addEventListener("click",()=>action(async()=>{const result=await request("/api/samples/test","POST",selected(),{"Content-Type":"audio/wav"});note(result.isolated_match ? "Знайдено ізольований збіг: фільтр відкинув би сегмент." : "Збіг не підтверджено: сегмент піде в Whisper.");}));
  action(async()=>{
    const token=location.hash.slice(1);history.replaceState(null,"",location.pathname);
    if(token)await request("/session","POST",{token});
    await refresh();$("content").hidden=false;preview();note("Локальний доступ підтверджено. Перегляд ролей працює без права запису.");
  });
})();
