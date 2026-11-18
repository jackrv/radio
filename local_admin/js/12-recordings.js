  // ---------- Черги розмітки з поточного архіву ----------
  // Види «розмітки» показують лише нерозмічене: розмічений запис зникає зі списку.
  const TODO_VIEWS = ["radio", "speech", "filtered"];
  const ROW_KIND = { speech: "Мовлення", unclear: "Нерозбірливо", noise: "Шум", notification: "Сигнал", room: "Кімната" };
  function recordingRow(item, controller) {
    const row = el("div", "recording-row");
    row.tabIndex = 0; row.setAttribute?.("role", "button");
    const at = new Date(item.created * 1000);
    const today = at.toDateString() === new Date().toDateString();
    const time = at.toLocaleString("uk-UA", today ? { hour: "2-digit", minute: "2-digit" } : { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
    const kind = item.reference != null ? ROW_KIND[item.label] || item.label
      : ((typeof STATUS !== "undefined" && STATUS[item.status]) || item.status);
    const d = item.data || {}, event = /^db:(\d+)$/.exec(d.fix_id || "");
    const duration = d.signal?.duration_s;
    const meta = [item.corrections?.length ? "✎" : "", typeof duration === "number" ? `${duration.toFixed(1).replace(".", ",")} с` : "",
                  event ? `№${event[1]}` : "", d.fix_id ? "✈" : ""].filter(Boolean).join(" · ");
    // Еталон оператора — звичайним; текст Whisper нерозміченого запису — приглушено.
    const heard = (d.raw_text || "").trim();
    const text = el("div", item.reference != null ? "row-text" : "row-text heard",
                    item.reference || heard || "Whisper: порожньо — варто прослухати");
    // Суперечність розмітки: «мовлення» без тексту або шум / кімната / сигнал із текстом.
    const reference = (item.reference || "").trim(), speechLike = ["speech", "unclear"].includes(item.label);
    const conflict = item.reference != null && (speechLike ? !reference : Boolean(reference));
    row.classList.toggle("conflict", conflict);
    row.classList.toggle("empty", item.reference == null && !heard);
    if (conflict) row.title = speechLike ? "Позначено мовлення, але тексту немає" : "Позначено без мовлення, але є текст";
    const head = el("div", "row-head");
    head.append(el("span", "row-time", time), el("span", "row-kind", kind), el("span", "row-meta", meta));
    row.append(head, text);
    row.addEventListener("click", () => activateReview(controller, false));
    row.addEventListener("keydown", event => { if (event.key === "Enter") activateReview(controller, false); });   // далі Enter веде до тексту
    $("recording-rows").append(row);
    // Набраний текст — у рядку переліку; клас картки — так само на рядку.
    controller.card.addEventListener("input", event => { if (event.target?.tagName === "TEXTAREA") text.textContent = event.target.value || "—"; });
    if (typeof MutationObserver !== "undefined") {
      const sync = () => {
        for (const name of ["active", "done", "dirty", "has-remarks"]) row.classList.toggle(name, controller.card.classList.contains(name));
        row.classList.toggle("todo", !controller.card.classList.contains("done"));
      };
      new MutationObserver(sync).observe(controller.card, { attributes: true, attributeFilter: ["class"] }); sync();
    }
    return row;
  }

  // Швидкість відтворення — біля кожного плеєра; спільна для всіх карток (setSpeed нижче).
  const SPEEDS = ["0.75", "0.85", "1", "1.25", "1.5", "1.75", "2"];
  function speedSelect() {
    const select = el("select", "speed-select");
    select.title = "Швидкість відтворення (клавіші ] і [). Тон голосу не змінюється.";
    select.setAttribute?.("aria-label", "Швидкість відтворення");
    for (const value of SPEEDS) { const o = el("option", "", `${value.replace(".", ",")}×`); o.value = value; select.append?.(o); }
    select.value = typeof reviewPrefs === "undefined" ? "1" : String(reviewPrefs.speed);
    select.addEventListener?.("change", () => setSpeed(select.value));
    return select;
  }
  let pendingEdits = "";   // збережений фільтр «правки користувача», доки варіанти ще не завантажено
  const editsValue = () => $("recording-edits").value || pendingEdits;
  // Календарні доби («сьогодні», «вчора», «позавчора») — від опівночі; числа — останні N годин.
  const DAYS_BACK = { today: 0, yesterday: 1, daybefore: 2 };
  function midnight(daysBack) { const d = new Date(); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() - daysBack); return Math.floor(d.getTime() / 1000); }
  function periodSince() {
    const value = $("recording-period").value;
    if (!value) return null;
    if (value in DAYS_BACK) return midnight(DAYS_BACK[value]);
    return Math.floor(Date.now() / 1000) - Number(value) * 3600;
  }
  function periodUntil() {
    const value = $("recording-period").value;
    return value in DAYS_BACK && DAYS_BACK[value] > 0 ? midnight(DAYS_BACK[value] - 1) : null;
  }
  let recordingOffset = 0;
  const reviewCards = [];
  let archiveBaseline = null;
  let loadedRecordingFilters = null;
  const recordingFilterIds = ["recording-view", "recording-period", "recording-edits", "recording-search"];
  function updateDraftCount() {
    const count = reviewCards.filter(card => card.dirty).length;
    $("archive-drafts").hidden = $("archive-drafts-nav").hidden = !count;
    $("archive-drafts").textContent = `Незбережені зміни: ${count}`;
    $("archive-drafts-nav").textContent = String(count);
    $("archive-drafts-nav").title = `Незбережених карток: ${count}`;
  }
  if (typeof window !== "undefined") window.addEventListener("beforeunload", event => {
    if (!reviewCards.some(card => card.dirty)) return;
    event.preventDefault(); event.returnValue = "";
  });
  // Пошук за ID («1a2b3c4d, 5e6f7a8b»), номером події («№82») або текстом — по всьому архіву.
  const searchQuery = () => $("recording-search").value.trim();
  // Видалити надіслану фразу в усіх отримувачів (Telegram дозволяє лише протягом 48 год).
  function telegramRecall(item) {
    const t = item.telegram;
    if (t.pending) return el("span", "badge", `видаляється в Telegram… (${t.pending})`);
    if (t.requested || (t.hidden && !t.copies)) return el("span", "badge", "видалено в Telegram");
    if (!t.copies) return el("span", "badge", "у чатах копій немає");
    if (!t.deletable) {
      const old = el("span", "badge", `у чатах: ${t.copies}`);
      old.title = "Минуло понад 48 год — Telegram не дозволяє ботові видалити повідомлення";
      return old;
    }
    const hours = Math.max(1, Math.floor((t.deletable_until * 1000 - Date.now()) / 3600000));
    const b = el("button", "ghost small-button danger-text", `🗑 Видалити в Telegram у всіх (${t.copies})`); b.type = "button";
    b.title = `Повідомлення зникне в усіх ${t.copies} отримувачів і в журналі. Можна ще ≈${hours} год.`;
    b.addEventListener("click", () => action(async () => {
      if (!confirm(`Видалити це повідомлення в Telegram в усіх отримувачів (${t.copies})? Фраза також зникне з журналу й Mini App. Скасувати не можна.`)) return;
      const r = await request(`/api/recordings/${item.id}/telegram-delete`, "POST");
      item.telegram = {...item.telegram, ...r};
      b.replaceWith(telegramRecall(item));
      note(r.queued ? `Видаляється в ${r.queued} чатах — бот зробить це за кілька секунд (якщо приймач запущено).` : "Фразу приховано; копій у чатах не лишилося.");
    }));
    return b;
  }

  async function recordings(reset = true) {
    if (reset && reviewCards.some(card => card.dirty) && !confirm("Є незбережені зміни. Оновити список і відкинути їх?")) {
      if (loadedRecordingFilters) for (const id of recordingFilterIds) $(id).value = loadedRecordingFilters[id];
      return;
    }
    if (reset) { recordingOffset = 0; reviewCards.length = 0; updateDraftCount(); $("recordings").replaceChildren(); $("recording-rows").replaceChildren(); }
    const since = periodSince(), until = periodUntil();
    const [page, summary] = await Promise.all([
      request(`/api/recordings?offset=${recordingOffset}&view=${$("recording-view").value}` +
        (since ? `&since=${since}` : "") + (until ? `&until=${until}` : "") +
        (searchQuery() ? `&q=${encodeURIComponent(searchQuery())}` : "") +
        (editsValue() ? `&edits=${encodeURIComponent(editsValue())}` : "")),
      request("/api/recordings/summary")
    ]);
    if (reset) archiveBaseline = {query: JSON.stringify(recordingFilterIds.map(id => $(id).value)), total: page.total, first: page.items[0]?.id};
    $("archive-update-note").textContent = "";
    loadedRecordingFilters = Object.fromEntries(recordingFilterIds.map(id => [id, $(id).value]));
    // Правки з Mini App: усі або конкретного користувача (журнал дій з 16.11.2026).
    const editsSelect = $("recording-edits"), chosenEdits = editsValue();
    pendingEdits = "";
    for (const option of [...editsSelect.options].slice(2)) option.remove();
    for (const person of page.editors || []) {
      const option = el("option", "", `Правки: ${person.name} (${person.corrections})`);
      option.value = String(person.user_id); editsSelect.append(option);
    }
    if ([...editsSelect.options].some(o => o.value === chosenEdits)) editsSelect.value = chosenEdits;
    // Кількість записів у кожному виді — за обраний період.
    for (const option of $("recording-view").options) {
      option.dataset.title ||= option.textContent;
      const count = page.views?.[option.value];
      option.textContent = `${option.dataset.title}${typeof count === "number" ? ` · ${count}` : ""}`;
    }
    const quality = summary.quality;
    $("recording-summary-details").textContent = `Записів: ${Object.values(summary.statuses).reduce((a,b) => a+b, 0)} · перевірено: ${summary.reviewed} · аудіо: ${(summary.bytes / 1048576).toFixed(1)} МБ · WER Whisper: ${pct(quality.raw_text.wer)} · WER виходу: ${pct(quality.final_text.wer)} · мовлення без тексту: ${summary.missed_speech} · хибний текст на шумі ефіру/сигналі: ${summary.false_text} · кімнатний шум: ${summary.room ?? 0} (з текстом: ${summary.room_false_text ?? 0}) · кілька передач у записі: ${summary.multiple_messages ?? 0} з ${summary.speech_reviewed ?? 0} · точність «кому»: ${pct(summary.roles?.recipient?.accuracy)} (${summary.roles?.recipient?.evaluated || 0}) · «від кого»: ${pct(summary.roles?.sender?.accuracy)} (${summary.roles?.sender?.evaluated || 0}). Неперевірені: ${summary.retention_days} днів / ${(summary.max_bytes / 1073741824).toFixed(1)} ГБ; перевірені зберігаються.`;
    $("recording-summary").textContent = `Записів: ${page.total} · перевірено: ${summary.reviewed} · WER до / після: ${pct(quality.raw_text.wer)} / ${pct(quality.final_text.wer)}`;
    if (!page.total && searchQuery()) $("recordings").append(el("p", "muted", "Нічого не знайдено. Шукайте за початком ID (від 4 знаків), №події або шматком тексту."));
    else if (!page.total) $("recordings").append(el("p", "muted", TODO_VIEWS.includes($("recording-view").value)
      ? "Тут усе розмічено. Оберіть інший вид або довший період."
      : "Записів за цим вибором немає. Оберіть інший вид або довший період."));
    for (const item of page.items) {
      const d = item.data, signal = d.signal || {};
      const card = el("article", "panel recording-card");
      const header = el("div", "recording-header");
      const eventNo = /^db:(\d+)$/.exec(d.fix_id || "");
      header.append(el("h3", "", `${new Date(item.created * 1000).toLocaleString("uk-UA")} · ${STATUS[item.status] || (item.status === "vad_short" ? "Закоротко (VAD)" : item.status)} · ${item.id.slice(0, 8)}${eventNo ? ` · №${eventNo[1]}` : ""}`));
      const audio = el("audio"); audio.controls = true; audio.preload = "none";
      audio.src = `/api/recordings/${item.id}/audio`;
      const player = el("div", "recording-player"); player.append(audio, speedSelect());
      header.append(player); card.append(header);
      if (typeof d.notification_score === "number" || d.fix_id) {
        // Надіслане в Telegram і оцінка спектрального фільтра; чистий сигнал можна зробити зразком.
        const tags = el("div", "recording-tags");
        if (d.fix_id) tags.append(el("span", "badge", eventNo ? `надіслано в Telegram · подія №${eventNo[1]}` : "надіслано в Telegram"));
        if (item.telegram) tags.append(telegramRecall(item));
        if (typeof d.notification_score === "number") {
          const badge = el("span", `badge ${d.notification_candidate ? "cand" : ""}`, `спектр ${fix(d.notification_score)}${d.notification_candidate ? " · кандидат" : ""}`);
          badge.title = `Схожість на зразок сигналу. Поза збігом: ${fix(d.notification_outside_s)} с · стороння енергія: ${pct(d.notification_extra)}`;
          tags.append(badge);
        }
        const sample = el("button", "ghost small-button", "＋ До зразків"); sample.type = "button";
        sample.title = "Зробити цей звук зразком фільтра сигналів (лише чистий сигнал без мовлення)";
        sample.addEventListener("click", () => action(async () => {
          if (!confirm("Додати цей запис як зразок звуку сповіщення? Лише якщо в ньому чистий сигнал без мовлення. Діє після перезапуску.")) return;
          await request(`/api/recordings/${item.id}/notification-sample`, "POST", { confirmed_isolated: true });
          sample.disabled = true; sample.textContent = "✓ у зразках";
          note("Зразок додано. Він почне діяти після перезапуску (кнопка внизу меню).");
        }));
        tags.append(sample);
        card.append(tags);
      }
      const details = el("details", "recording-details");
      details.append(el("summary", "", "Технічні деталі"));
      details.append(el("p", "muted small", `Тривалість: ${fix(signal.duration_s)} с · рівень: ${fix(signal.rms_dbfs, 1)} dBFS · кліпінг: ${pct(signal.clipping_ratio)} · Whisper: ${fix(d.whisper_s)} с · Qwen: ${fix(d.qwen_s)} с · черга: ${fix(d.queue_wait_s)} с`));
      for (const fix of item.corrections || []) {
        const when = new Date(fix.ts * 1000).toLocaleString("uk-UA");
        card.append(el("p", "recording-edit small", fix.changed
          ? `✎ Правка в Mini App: ${fix.name} · ${when}: «${fix.from}» → «${fix.to}»`
          : `✎ Відкрито й збережено без змін: ${fix.name} · ${when}`));
      }
      if (!item.corrections && item.reference_source === "miniapp")
        card.append(el("p", "recording-edit small muted", "✎ Еталон із правки в Mini App (автор невідомий: до журналу дій)"));
      card.append(el("p", "recording-raw", `Whisper: ${d.raw_text || "Текст відсутній / розпізнавання не виконувалось"}`));
      if (d.before_llm && d.before_llm !== d.raw_text) details.append(el("p", "", `До Qwen: ${d.before_llm}`));
      if (d.llm_text !== undefined) details.append(el("p", "", `Пропозиція Qwen (${d.llm_accepted ? "прийнято" : "відхилено"}): ${d.llm_text}`));
      details.append(el("p", "", `Вихід конвеєра: ${d.final_text || "Не передано далі"}`));
      const predictedRole = key => d[key] === undefined ? "немає збереженої оцінки" : d[key] || "не визначено";
      details.append(el("p", "muted small", `Розбір програми — кому: ${predictedRole("predicted_recipient")} · від кого: ${predictedRole("predicted_sender")}`));
      const qualityLine = el("p", "muted small");
      function showQuality(row) {
        if (!row.quality) { qualityLine.textContent = "Ще не перевірено"; return; }
        if (row.label !== "speech") { qualityLine.textContent = "Позначку збережено; до загального WER мовлення не входить."; return; }
        const raw = row.quality.raw_text, final = row.quality.final_text;
        qualityLine.textContent = `WER до / після: ${pct(raw.wer)} / ${pct(final.wer)} · CER до / після: ${pct(raw.cer)} / ${pct(final.cer)} · числа до / після: ${yes(raw.number_match)} / ${yes(final.number_match)} · збіг ролей кому / від кого: ${row.role_quality?.recipient?.match == null ? "—" : yes(row.role_quality.recipient.match)} / ${row.role_quality?.sender?.match == null ? "—" : yes(row.role_quality.sender.match)}`;
      }
      showQuality(item); details.append(qualityLine);
      const form = el("form"), input = el("textarea");
      // Тип запису — кнопками; «Шум ефіру», «Сигнал» і «Кімната» зберігаються одразу (без тексту й ролей).
      const CATEGORIES = [["speech", "1 Мовлення"], ["noise", "2 Шум ефіру"], ["notification", "3 Сигнал"], ["unclear", "4 Нерозбірливо"], ["room", "5 Кімната / клавіатура"]];
      const category = { value: item.label || "speech" };
      const segmented = el("div", "segmented");
      const categoryButtons = {};
      for (const [value, title] of CATEGORIES) {
        const button = el("button", "ghost", title); button.type = "button";
        button.addEventListener("click", () => setCategory(value, true));
        categoryButtons[value] = button; segmented.append(button);
      }
      input.value = item.reference ?? (d.final_text || d.raw_text || "");
      input.rows = 2; input.maxLength = 2000; input.placeholder = "Введіть те, що справді чутно…";
      input.setAttribute("aria-label", "Еталонна розшифровка");
      const splitRoles = text => (text || "").split(",").map(v => v.trim()).filter(Boolean);
      const sameRole = (a, b) => a.toLocaleLowerCase("uk") === b.toLocaleLowerCase("uk");
      // Ролі підставляються самі (два різні позивні: перший — «Кому», другий — «Від»),
      // доки оператор не змінив їх уручну. Уже розмічені ролі не перезаписуються.
      let rolesAuto = item.reference_recipient == null && item.reference_sender == null;
      let rolesAutoApplied = false;
      function applyAutoRoles(roles) {
        if (!rolesAuto) return;
        rolesAutoApplied = Boolean(roles);
        recipient.set(roles ? [roles.recipient] : []);
        sender.set(roles ? [roles.sender] : []);
        updateDirty();
      }
      function rolesTouched() { rolesAuto = false; rolesAutoApplied = false; }
      // Роль без випадних списків: порожнє поле — ще не перевірено; «∅» — у фразі не вказано.
      function roleField(title, reference) {
        const group = el("div", "recording-role"), value = el("input"), none = el("button", "ghost role-none", "∅");
        let absent = reference === "";
        value.type = "text"; value.maxLength = 300; value.value = reference || "";
        value.setAttribute("aria-label", `${title}: позивний`);
        none.type = "button"; none.title = `«${title}» у фразі не вказано (порожнє поле — ще не перевірено)`;
        const update = () => {
          none.className = absent ? "ghost role-none on" : "ghost role-none";
          none.setAttribute("aria-pressed", String(absent));
          value.disabled = absent;
          value.placeholder = absent ? "не вказано у фразі" : "невідомо";
        };
        none.addEventListener("click", () => { absent = !absent; if (absent) value.value = ""; rolesTouched(); update(); renderChips(); updateDirty(); });
        value.addEventListener("input", () => { rolesTouched(); renderChips(); });
        update(); group.append(el("span", "role-title", title), value, none);
        return {node: group, read: () => absent ? "" : (value.value.trim() ? value.value : null),
                absent: () => { absent = true; value.value = ""; update(); },
                values: () => absent ? [] : splitRoles(value.value),
                set: list => { absent = false; value.value = list.join(", "); update(); }};
      }
      const recipient = roleField("Кому", item.reference_recipient), sender = roleField("Від", item.reference_sender);

      // Позивні зі словника, знайдені в набраному тексті: оператор сам відмічає «Кому» / «Від».
      const chips = el("div", "recording-chips");
      let suggestions = [], suggestTimer = null;
      function renderChips() {
        chips.replaceChildren();
        if (!suggestions.length) return;
        chips.append(el("span", "muted small", rolesAutoApplied
          ? "Ролі підставлено: перший позивний — «Кому», другий — «Від». Змініть, якщо не так:"
          : "Позивні у фразі:"));
        for (const name of suggestions) {
          const chip = el("span", "chip");
          chip.append(el("b", "", name));
          for (const [field, title] of [[recipient, "Кому"], [sender, "Від"]]) {
            const chosen = field.values().some(v => sameRole(v, name));
            const toggle = el("button", chosen ? "ghost on" : "ghost", title); toggle.type = "button";
            toggle.setAttribute("aria-pressed", String(chosen));
            toggle.addEventListener("click", () => {
              rolesTouched();
              const list = field.values().filter(v => !sameRole(v, name));
              field.set(chosen ? list : [...list, name]);
              updateDirty(); renderChips();
            });
            chip.append(toggle);
          }
          chips.append(chip);
        }
      }
      function suggest() {
        clearTimeout(suggestTimer);
        suggestTimer = setTimeout(async () => {
          if (!["speech", "unclear"].includes(category.value) || !input.value.trim()) { suggestions = []; applyAutoRoles(null); renderChips(); return; }
          try {
            const result = await request("/api/recordings/suggest-roles", "POST", { text: input.value });
            suggestions = result.callsigns || [];
            applyAutoRoles(multiBox.checked ? null : result.roles);
          } catch (_) { suggestions = []; }
          renderChips();
        }, 350);
      }
      input.addEventListener("input", suggest);

      // Перевірка оформлення: пробіли, цифри групами, «з», апостроф, назви словника.
      // Лише підказки: зберегти можна будь-який текст.
      const lint = el("div", "text-check");
      let lintTimer = null;
      function renderLint(result) {
        lint.replaceChildren();
        const issues = result?.issues || [];
        card.classList.toggle("has-remarks", issues.length > 0);
        if (!issues.length) return;
        if (result.formatted) {
          const all = el("button", "ghost", "Виправити оформлення"); all.type = "button";
          all.addEventListener("click", () => { input.value = result.formatted; updateDirty(); checkText(0); input.focus(); });
          lint.append(all);
        }
        const seenIssues = new Set();
        for (const issue of issues) {
          const key = JSON.stringify([issue.kind, issue.message, issue.dictionary_entry, issue.fixed_text]);
          if (seenIssues.has(key)) continue;
          seenIssues.add(key);
          const row = el("div", `text-check-item ${issue.kind}`);
          const kind = issue.dictionary_entry ? "Словник" : {format:"Оформлення", spelling:"Написання", info:"Перевірити"}[issue.kind] || "Підказка";
          row.append(el("span", "hint-kind", kind), el("span", "hint-message", issue.message));
          if (issue.fixed_text) {
            const fix = el("button", "ghost", "Виправити"); fix.type = "button";
            fix.addEventListener("click", () => { input.value = issue.fixed_text; updateDirty(); checkText(0); input.focus(); });
            row.append(fix);
          }
          if (issue.dictionary_entry) {
            const add = el("button", "ghost small-button", `＋ У словник: ${issue.dictionary_entry.replace(":", "")}`); add.type = "button";
            add.title = `Додати рядок «${issue.dictionary_entry}» у словник позивних (перевірте на слух)`;
            add.addEventListener("click", () => {
              if (add.disabled) return;
              add.disabled = true;
              addCallsign(issue.dictionary_entry).then(() => { suggest(); checkText(0); })
                .catch(error => note(error.message)).finally(() => { add.disabled = false; });
            });
            row.append(add);
          }
          lint.append(row);
        }
      }
      let lintRevision = 0;
      function checkText(delay = 500) {
        clearTimeout(lintTimer);
        const revision = ++lintRevision;
        lintTimer = setTimeout(async () => {
          if (!["speech", "unclear"].includes(category.value) || !input.value.trim()) { renderLint(null); return; }
          const text = input.value;
          try {
            const result = await request("/api/text-check", "POST", { text, at: item.created });
            if (revision === lintRevision && input.value === text) renderLint(result);
          } catch (_) { if (revision === lintRevision) renderLint(null); }
        }, delay);
      }
      input.addEventListener("input", () => checkText());
      if (item.text_check) renderLint(item.text_check);

      // Кілька передач в одному записі: VAD злив сегменти. Окрема позначка для оцінки.
      const multiBox = el("input"); multiBox.type = "checkbox"; multiBox.checked = item.multiple_messages === 1;
      const multi = el("label", "check small"); multi.append(multiBox, el("span", "", " Кілька передач у записі"));
      // «/» (чи старий «|») у тексті — кілька передач: відмітка ставиться сама й знімається, лише якщо її
      // поставила підстановка. Ручне перемикання вимикає підстановку в цій картці.
      let multiAuto = false, multiTouched = false;
      multiBox.addEventListener("change", () => { multiTouched = true; multiAuto = false; suggest(); });
      input.addEventListener("input", () => {
        if (multiTouched) return;
        const split = /[|/]/.test(input.value);
        if (split && !multiBox.checked) { multiBox.checked = true; multiAuto = true; }
        else if (!split && multiAuto) { multiBox.checked = false; multiAuto = false; }
      });
      multi.title = "Кілька людей або передач у одному записі (злиття сегментів)";
      const save = el("button", "", item.reference === null ? "Зберегти еталон" : "Оновити еталон");
      const textField = el("label", "recording-text");
      textField.append(el("span", "muted small", "Почутий текст"), input);
      const roles = el("div", "recording-roles"); roles.append(recipient.node, sender.node);
      const actions = el("div", "recording-actions");
      const saveStatus = el("span", "muted small save-status"); saveStatus.setAttribute("role", "status");
      save.className = "save-primary";
      const dirtyBadge = el("span", "dirty-badge", "Незбережені зміни"); dirtyBadge.hidden = true;
      segmented.setAttribute("aria-label", "Тип запису");
      // «Зберегти» — крайня праворуч, позначка незбережених змін і результат — ліворуч від неї.
      const saveActions = el("div", "save-actions"); saveActions.append(saveStatus, dirtyBadge, save);
      actions.append(segmented, saveActions);
      function setCategory(value, quick) {
        category.value = value;
        for (const [key, button] of Object.entries(categoryButtons)) button.className = key === value ? "ghost on" : "ghost";
        const speechLike = ["speech", "unclear"].includes(value);
        textField.hidden = !speechLike; roles.hidden = !speechLike; chips.hidden = !speechLike; multi.hidden = !speechLike; lint.hidden = !speechLike;
        if (!speechLike) { input.value = ""; recipient.absent(); sender.absent(); if (quick) submit(); }
        else if (quick) suggest();
        if (quick) updateDirty();
      }
      // Результат збереження — поруч із кнопкою: повідомлення в шапці при прокручуванні не видно.
      // Власний прапорець замість спільного action(): фонове оновлення черги не «ковтає» натискання.
      let saving = false, savedSnapshot;
      const snapshot = () => JSON.stringify({reference:input.value, label:category.value,
        reference_recipient:recipient.read(), reference_sender:sender.read(), multiple_messages:multiBox.checked});
      function updateDirty() {
        if (savedSnapshot === undefined) return;
        controller.dirty = snapshot() !== savedSnapshot;
        dirtyBadge.hidden = !controller.dirty;
        card.classList.toggle("dirty", controller.dirty);
        updateDraftCount();
      }
      form.addEventListener("input", updateDirty); form.addEventListener("change", updateDirty);
      function showSave(text, tone = "muted") { saveStatus.textContent = text; saveStatus.className = `${tone} small save-status`; }
      async function submit() {
        if (saving) return;
        saving = true; save.disabled = true; showSave("Зберігаю…");
        try {
          const speechLike = ["speech", "unclear"].includes(category.value);
          const sentSnapshot = snapshot();
          const payload = {reference: input.value, label: category.value, expected_revision: item.revision, reference_recipient: recipient.read(), reference_sender: sender.read(), multiple_messages: speechLike ? multiBox.checked : null};
          const result = await request(`/api/recordings/${item.id}/review`, "PUT", payload);
          savedSnapshot = sentSnapshot;
          // Reviewed rows leave the pending query; keep the next page's offset aligned.
          if (item.reference === null && TODO_VIEWS.includes($("recording-view").value)) recordingOffset = Math.max(0, recordingOffset - 1);
          item.revision = result.revision; item.reference = result.reference; item.label = result.label;
          showQuality(result);
          controller.done = true;
          updateDirty();
          card.classList.add("done");
          save.textContent = "Оновити еталон";
          const synced = { updated: " · подію й повідомлення в Telegram оновлено", hidden: " · повідомлення видалене — Telegram не змінено",
                           failed: " · подію оновити не вдалося" }[result.event_sync] || "";
          showSave(`✓ Збережено о ${new Date().toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" })}${synced}`,
                   result.event_sync === "failed" ? "alert" : "ok");
          note(result.event_sync === "updated" ? "Еталон збережено; подію й повідомлення в Telegram оновлено." : "Еталон збережено.");
          if (!controller.dirty && typeof advanceReview === "function") advanceReview(controller);
        } catch (error) {
          showSave(`Не збережено: ${error.message}`, "alert"); note(error.message);
        } finally {
          saving = false; save.disabled = false;
        }
      }
      const meta = el("div", "recording-meta"); meta.append(roles, multi);
      form.append(textField, lint, chips, meta, actions);
      form.addEventListener("submit", event => { event.preventDefault(); submit(); });
      const controller = { card, audio, item, dirty:false, done: item.reference !== null, setCategory, submit,
                           focusText: () => { if (!textField.hidden) input.focus(); }, suggest,
                           refreshHints: () => { if (!textField.hidden) { suggest(); checkText(0); } } };
      reviewCards.push(controller);
      controller.row = recordingRow(item, controller);
      card.addEventListener("click", () => { if (typeof activateReview === "function") activateReview(controller, false); });
      if (controller.done) card.classList.add("done");
      setCategory(category.value, false);
      savedSnapshot = snapshot();
      card.append(form);
      details.append(el("pre", "", JSON.stringify(d, null, 2)));
      card.append(details); $("recordings").append(card);
    }
    recordingOffset += page.items.length;
    $("recording-more").hidden = recordingOffset >= page.total;
    if (typeof fitArchive === "function") fitArchive();
    // Одразу перший запис, щоб редактор не був порожнім.
    if (reset && reviewCards.length && typeof activateReview === "function") activateReview(reviewCards[0], false);
  }
  $("recording-refresh").addEventListener("click", () => action(() => recordings(true)));
  // Обраний вид і період запам’ятовуються в цьому браузері.
  const VIEW_KEY = "radio345:archive-view";
  const saveArchiveView = () => {
    try { localStorage.setItem(VIEW_KEY, JSON.stringify({ view: $("recording-view").value, period: $("recording-period").value, edits: $("recording-edits").value })); } catch (_) { /* лише поточна сесія */ }
  };
  try {
    const saved = JSON.parse(localStorage.getItem(VIEW_KEY) || "{}");
    // Варіанти «правки користувача» з’являються після першого завантаження архіву.
    if ([...$("recording-edits").options].some(o => o.value === saved.edits)) $("recording-edits").value = saved.edits;
    else if (saved.edits) pendingEdits = String(saved.edits);
    if ([...$("recording-view").options].some(o => o.value === saved.view)) $("recording-view").value = saved.view;
    if ([...$("recording-period").options].some(o => o.value === saved.period)) $("recording-period").value = saved.period;
  } catch (_) { /* типовий вибір */ }
  for (const id of ["recording-view", "recording-period"]) {
    $(id).addEventListener("change", () => { saveArchiveView(); action(() => recordings(true)); });
  }
  $("recording-edits").addEventListener("change", () => { saveArchiveView(); action(() => recordings(true)); });
  $("recording-more").addEventListener("click", () => action(() => recordings(false)));
  // Enter — шукати; очищене поле (зокрема хрестиком) — знову звичайний список.
  $("recording-search").addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); action(() => recordings(true)); } });
  $("recording-search").addEventListener("input", event => { if (!event.target.value) action(() => recordings(true)); });

  // Інші вибори, що запам’ятовуються в цьому браузері.
  function rememberValue(id, key, { checkbox = false } = {}) {
    const box = $(id);
    try {
      const saved = localStorage.getItem(key);
      if (saved !== null) {
        if (checkbox) box.checked = saved === "on";
        else if ([...box.options].some(o => o.value === saved)) box.value = saved;
      }
    } catch (_) { /* типово */ }
    box.addEventListener("change", () => {
      try { localStorage.setItem(key, checkbox ? (box.checked ? "on" : "off") : box.value); } catch (_) { /* сесія */ }
    });
  }
  rememberValue("learning-filter", "radio345:learning-filter");
  rememberValue("role", "radio345:preview-role");

  const loaders = { recordings: () => recordings(true), overview, filter: filterView, dictionaries, codes: dailyCodes, texts, learning, settings: async () => { await settings(); await runtimeSettings(); }, users, activity: () => activity(), system, quality: async () => { await quality(); await training(); }, experiments, preview: roles };

  // ---------- Швидка розмітка архіву: активна картка, клавіші, швидкість ----------
  const REVIEW_PREFS = "radio345:review-prefs";
  let reviewPrefs = { speed: "1", autoplay: true };
  try { reviewPrefs = { ...reviewPrefs, ...JSON.parse(localStorage.getItem(REVIEW_PREFS) || "{}") }; } catch (_) { /* типово */ }
  const savePrefs = () => { try { localStorage.setItem(REVIEW_PREFS, JSON.stringify(reviewPrefs)); } catch (_) { /* сесія */ } };
  $("recording-autoplay").checked = reviewPrefs.autoplay;
  // Швидкість — спільна для всіх карток, вибір біля кожного плеєра; ] / [ (ї / х) — на крок.
  if (!SPEEDS.includes(String(reviewPrefs.speed))) reviewPrefs.speed = "1";
  function setSpeed(value, announce = false) {
    reviewPrefs.speed = String(value); savePrefs();
    for (const select of document.querySelectorAll(".speed-select")) select.value = reviewPrefs.speed;
    for (const c of reviewCards) c.audio.playbackRate = Number(value);
    if (announce) note(`Швидкість відтворення: ${String(value).replace(".", ",")}×`);
  }
  $("recording-autoplay").addEventListener("change", () => { reviewPrefs.autoplay = $("recording-autoplay").checked; savePrefs(); });
  document.addEventListener("play", event => {
    if (event.target instanceof HTMLMediaElement && event.target.closest('[data-panel="recordings"]'))
      event.target.playbackRate = Number(reviewPrefs.speed);
  }, true);

  let activeReview = null;
  // Широкий екран — перелік і редактор поруч: прокручується перелік, редактор завжди на місці.
  const splitView = () => window.matchMedia?.("(min-width: 1100px)").matches;
  // Широкий екран: архів — точно у висоту вікна (сторінка не прокручується, лише перелік і запис);
  // «Показати ще» — внизу переліку. Вузький — звичайна стрічка, кнопка під картками.
  function fitArchive() {
    if (typeof document === "undefined" || !document.querySelector) return;
    const split = document.querySelector(".archive-split"), list = document.querySelector(".archive-list");
    const main = document.querySelector(".archive-main"), more = $("recording-more");
    if (!split || !list || !main || !more) return;
    if (splitView() && split.offsetParent !== null) {
      const top = split.getBoundingClientRect().top + window.scrollY;
      split.style.setProperty("--archive-h", `${Math.max(360, Math.floor(window.innerHeight - top - 10))}px`);
      if (more.parentElement !== list) list.append(more);
    } else {
      split.style.removeProperty("--archive-h");
      if (more.parentElement !== main) main.append(more);
    }
  }
  window.addEventListener?.("resize", () => fitArchive());
  function activateReview(controller, scroll = true) {
    activeReview = controller;
    for (const c of reviewCards) c.card.classList.toggle("active", c === controller);
    if (splitView()) controller.row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    else if (scroll) controller.card.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  function stepReview(delta) {
    if (!reviewCards.length) return;
    const index = activeReview ? reviewCards.indexOf(activeReview) : -1;
    const next = reviewCards[Math.max(0, Math.min(reviewCards.length - 1, index + delta))];
    activateReview(next);
  }
  function advanceReview(controller) {
    controller.audio.pause();
    const start = reviewCards.indexOf(controller);
    const next = reviewCards.slice(start + 1).find(c => !c.done);
    if (!next) { note("Еталон збережено. На цій сторінці неперевірених записів більше немає — оновіть список або натисніть «Показати ще»."); return; }
    activateReview(next);
    if (reviewPrefs.autoplay) next.audio.play().catch(() => {});
  }
  document.addEventListener("keydown", event => {
    if (view !== "recordings" || !reviewCards.length) return;
    const typing = event.target.closest?.("textarea, input, select");
    if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
      event.preventDefault(); (activeReview || reviewCards[0]).submit(); return;
    }
    if (event.key === "Escape" && typing) { event.target.blur(); return; }
    if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
    if (!activeReview) activateReview(reviewCards[0], false);
    const current = activeReview;
    if (event.key === " ") {
      event.preventDefault();
      if (current.audio.paused) current.audio.play().catch(() => {}); else current.audio.pause();
    } else if (["1", "2", "3", "4", "5"].includes(event.key)) {
      current.setCategory(["speech", "noise", "notification", "unclear", "room"][Number(event.key) - 1], true);
      if (["1", "4"].includes(event.key)) current.focusText();
    } else if (event.key === "j" || event.key === "о" || event.key === "ArrowDown") { event.preventDefault(); stepReview(1); }
    else if (event.key === "k" || event.key === "л" || event.key === "ArrowUp") { event.preventDefault(); stepReview(-1); }
    else if (event.key === "r" || event.key === "к") { current.audio.currentTime = 0; current.audio.play().catch(() => {}); }
    else if (["]", "ї", "[", "х"].includes(event.key)) {
      const step = ["]", "ї"].includes(event.key) ? 1 : -1;
      const index = Math.max(0, Math.min(SPEEDS.length - 1, SPEEDS.indexOf(String(reviewPrefs.speed)) + step));
      setSpeed(SPEEDS[index], true);
    }
    else if (event.key === "Enter") { event.preventDefault(); current.focusText(); }
  });

  // ---------- Відтворення без луни в мікрофон приймача ----------
  // Звук із колонок Mac потрапляє в мікрофон: приймач розпізнав би його знову
  // (дублікати в Telegram і в архіві). Поки грає будь-яке аудіо панелі, просимо
  // приймач закрити мікрофон. Утримання короткострокове й поновлюється, тож
  // закрита вкладка не залишить приймач вимкненим довше ніж на ~10 с.
  const HOLD_KEY = "radio345:hold-while-listening";
  const holdBox = $("hold-while-listening");
  try { holdBox.checked = localStorage.getItem(HOLD_KEY) !== "off"; } catch (_) { /* лишаємо типово */ }
  holdBox.addEventListener("change", () => {
    try { localStorage.setItem(HOLD_KEY, holdBox.checked ? "on" : "off"); } catch (_) { /* лише поточна сесія */ }
    if (!holdBox.checked && playing.size) sendHold(0);
  });
  const playing = new Set();
  let holdTimer = null;
  function sendHold(seconds) {
    return fetch("/api/listening-hold", { method: "PUT", credentials: "same-origin", cache: "no-store",
      headers: { "Content-Type": "application/json" }, body: JSON.stringify({ seconds: Math.max(0, Math.min(120, seconds)) }) })
      .catch(() => { /* приймач не зупиняємо через збій панелі */ });
  }
  // Одночасно грає лише один запис у всій панелі: новий зупиняє попередній.
  document.addEventListener("play", event => {
    const media = event.target;
    if (!(media instanceof HTMLMediaElement)) return;
    for (const other of document.querySelectorAll("audio, video")) if (other !== media && !other.paused) other.pause();
  }, true);
  document.addEventListener("play", event => {
    const media = event.target;
    if (!(media instanceof HTMLMediaElement) || !holdBox.checked) return;
    playing.add(media);
    const left = Number.isFinite(media.duration) ? media.duration - media.currentTime : 10;
    sendHold(Math.min(120, left + 1.5));
    if (!holdTimer) holdTimer = setInterval(() => { if (playing.size) sendHold(10); }, 4000);
  }, true);
  function stopped(event) {
    if (!playing.delete(event.target) || playing.size) return;
    clearInterval(holdTimer); holdTimer = null;
    sendHold(1);  // 1 с на відлуння кімнати, потім мікрофон відкривається
  }
  document.addEventListener("pause", stopped, true);
  document.addEventListener("ended", stopped, true);
  window.addEventListener("pagehide", () => { if (playing.size) sendHold(0); });

