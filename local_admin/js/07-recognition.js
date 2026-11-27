  // ---------- Текстові списки ----------
  const TEXT_LISTS = {
    blacklist: ["Фрази-галюцинації Whisper", "Відкидається лише фраза, що повністю збігається з рядком (регістр і пунктуація не важливі). Наприклад: «дякую за перегляд»."],
    short_garbage: ["Короткий шум", "Короткі хибні фрагменти. Відкидаються, лише якщо у фразі немає позивних, команд і слів спостереження."],
    aliases: ["Аліаси тексту", "Формат «правильне: варіант, варіант». Варіант замінюється на правильне написання перед пошуком позивних і команд, наприклад «злата: зла та, злото»."],
    callsign_aliases: ["Аліаси позивних", "Формат «позивний: почутий варіант». Для позивного з номерами діє лише перед дозволеним номером; для позивного без номера — на саме слово. Позивний має бути у словнику."],
    phrase_patterns: ["Шаблони фраз", "Шаблон цілої передачі: «{П}, {П}, я {П}, перевірка звʼязку, прийом». {П} — позивний (слово й необов’язковий номер), {Ч} — число чи код, {К} — команда зі словника. Фраза (між . ! ? ; /), що повністю відповідає шаблону, пишеться як у шаблоні: схожі слова виправляються, слово на місці позивного стає позивним зі словника, лише якщо почутий номер є в його словнику (без номера — лише майже точний збіг). Нічого не додається й не прибирається, числа не змінюються, назви зі словників і правильно почуті «прийом / прийнято / прийняв» не переписуються. Керуйте шаблонами перемикачами, перевіряйте зміни на еталонах і зберігайте, якщо гірше — 0."],
    phrase_templates: ["Шаблони зворотів", "М’які шаблони: якщо почуте схоже на оборот із цього списку, воно пишеться як в обороті («посилку отримаф» → «посилку отримав»). Кожне слово має бути схожим, однослівні — лише в кінці фрази. Числа шаблони не змінюють, назви зі словників і форми звертання («Клену») захищені. Окрім цього списку діють вбудовані правила: позивний перед номером пишеться як у словнику, якщо номер є в словнику («Бериза 56» → «Береза 56»); «на 109» на місці позивного, що починається на «на…» (напр. «Набат»), → «Набат 109», лише з номером зі словника; зациклення «вишня, вишня, вишня, вишня» скорочується до двох, крім «прийом». Перевірка змін на ваших еталонах: .venv/bin/python -m tools.text_replay"],
    whisper_prompt: ["Підказка Whisper", "Фрази на початку підказки розпізнавання, по одній у рядку, так, як їх вимовляють: «дощ 42 42». Допомагають Whisper правильно записати важливі команди, але зрідка потрапляють у розпізнаний текст. З’являться хибні спрацьовування — вимкніть рядок, поставивши «#» на початку."],
  };
  let textRevision = null;
  let textListType = (() => { try { return localStorage.getItem("radio345:text-list") || "short_garbage"; } catch (_) { return "short_garbage"; } })();

  function toLines(entries) { return entries.map(e => (e.enabled ? "" : "# ") + e.raw_line).join("\n"); }
  function fromLines(text) {
    const entries = [];
    for (const raw of text.split("\n")) {
      const line = raw.trim();
      if (!line) continue;
      if (line.startsWith("#")) { const rest = line.replace(/^#+/, "").trim(); if (rest) entries.push({ raw_line: rest, enabled: false }); }
      else entries.push({ raw_line: line, enabled: true });
    }
    return entries;
  }

  async function addCallsign(entry) {
    const dictionaries = await request("/api/dictionaries");
    const dictionary = dictionaries.callsigns;
    const line = entry.trim();
    if (!line) return;
    if (!dictionary.entries.some(e => e.enabled && e.raw_line.trim().toLocaleLowerCase("uk") === line.toLocaleLowerCase("uk"))) {
      await request("/api/dictionaries/callsigns", "PUT", {
        entries: [...dictionary.entries, {raw_line: line, enabled: true}], expected_version: dictionary.version,
      });
    }
    recentlySavedDictionaryLines.add(line);
    // Збережену вкладку можна перечитати, але незбережену чернетку не перезаписуємо.
    const editor = document.querySelector('[data-panel="dictionaries"] textarea');
    if (!editor || editor.value === editor.dataset.savedValue) loaded.delete("dictionaries");
    note(`Позивний «${line}» додано. Приймач підхопить зміну за кілька секунд.`);
  }

  const recentlySavedDictionaryLines = new Set();
  // ---------- Словники позивних, команд і Telegram ----------
  const DICTIONARIES = {
    callsigns: ["Позивні", "«назва: номери»: «береза: 31 56 700-710». Без номерів — позивний, що звучить сам: «вишня». «*» — будь-який номер. Порожнім словник позивних лишити не можна."],
    commands: ["Команди", "«назва номер» так, як вимовляють: «злива 42 42» (номер 4242). Без номера — ключове слово: «грім». Кілька номерів — через двокрапку: «злива: 4242 2727»."],
    telegram: ["Telegram (/watch)", "Що надсилати підписникам /watch. «береза: 56» — лише з цим номером; «вишня» без номера — і сам позивний без номера (на шум Whisper іноді вигадує такі слова); «вишня: *» — з будь-яким номером."],
  };
  // ---------- Коди доби ----------
  let codesByDay = {}, changeHour = 20;
  const codeDrafts = new Map(); let permanentDraft = null;
  function showDayCodes() {
    $("codes-input").value = codeDrafts.get($("codes-day").value) ?? (codesByDay[$("codes-day").value] || []).join(", ");
    const day = $("codes-day").value;
    if (!day) { $("codes-period").textContent = ""; return; }
    const end = new Date(`${day}T00:00:00`); end.setHours(changeHour);
    const start = new Date(end.getTime() - 24 * 3600 * 1000);
    const fmt = d => `${d.toLocaleDateString("uk-UA", { day: "2-digit", month: "2-digit" })} ${String(changeHour).padStart(2, "0")}:00`;
    $("codes-period").textContent = `діють з ${fmt(start)} до ${fmt(end)}`;
    $('codes-period').title=`Межа доби: ${String(changeHour).padStart(2,'0')}:00 за місцевим часом Mac. Параметр DAILY_CODES_CHANGE_HOUR у config.py.`;
    renderCodeChips('codes-input');
    for (const card of $("codes-recent")?.querySelectorAll?.(".code-day") || []) card.classList.toggle("on", card.dataset?.day === $("codes-day").value);
  }
  async function dailyCodes() {
    const data = await request("/api/daily-codes");
    codesByDay = Object.fromEntries(data.items.map(i => [i.day, i.codes]));
    changeHour = data.change_hour ?? 20;
    $("codes-permanent").value = permanentDraft ?? (data.permanent || []).join(", ");
    renderCodeChips('codes-permanent');
    if (!$("codes-day").value) $("codes-day").value = data.today;
    showDayCodes();
    const box = $("codes-recent");
    box.replaceChildren();
    if (!data.items.length) { box.textContent = "Кодів ще не внесено."; return; }
    // Картка дня, як на папірці: число доби великим | коди стовпчиком; рік і місяць — лише в підказці.
    const list = el("div", "code-days");
    for (const item of data.items) {
      const link = el("button", `code-day${item.day === $("codes-day").value ? " on" : ""}`); link.type = "button";
      if (link.dataset) link.dataset.day = item.day;
      const [, month, date] = item.day.split("-");
      const codes = el("span", "code-day-codes");
      codes.append(...item.codes.map(code => el("span", "", code)));
      link.append(el("b", "code-day-num", String(Number(date))), codes);
      link.title = `${date}.${month}: ${item.codes.join(", ")}\nДіють з ${item.period?.[0] ?? ""} до ${item.period?.[1] ?? ""}`;
      link.setAttribute?.("aria-label", `${date}.${month}: ${item.codes.join(", ")}`);
      link.addEventListener("click", () => {
        $("codes-day").value = item.day; showDayCodes();
        for (const other of list.children) other.classList.toggle("on", other === link);
      });
      list.append(link);
    }
    box.append(el("span", "code-days-title", "Останні дні"), list);
  }
  function renderCodeChips(id) {
    const input=$(id); let chips=$(`${id}-chips`);
    if (!chips) { chips=el('div','code-chips'); chips.id=`${id}-chips`; input.after?.(chips); }
    const values=[...new Set(input.value.split(/[,;\n]/).map(v=>v.trim()).filter(Boolean))];
    chips.replaceChildren();
    for (const code of values) {
      const chip=el('button','ghost code-chip',`${code} ×`);chip.type='button';
      chip.title='Прибрати з чернетки; застосовується лише після збереження';
      chip.addEventListener('click',()=>{ input.value=values.filter(v=>v!==code).join(', '); input.dispatchEvent(new Event('input')); });
      chips.append(chip);
    }
  }
  for (const id of ['codes-input','codes-permanent']) $(id).addEventListener('input',()=>{ if(id==='codes-permanent')permanentDraft=$(id).value; else codeDrafts.set($('codes-day').value,$(id).value); renderCodeChips(id); });
  $("codes-day").addEventListener("change", showDayCodes);
  $("codes-permanent-save").addEventListener("click", () => action(async () => {
    const status = $("codes-status");
    try {
      const sent = $("codes-permanent").value;
      await request("/api/daily-codes/permanent", "PUT", { codes: sent });
      if ($("codes-permanent").value === sent) permanentDraft = null;
      status.hidden = true; status.textContent = "";
      await dailyCodes();
    } catch (error) { status.hidden = false; status.className = "alert small"; status.textContent = error.message; throw error; }
  }));
  $("codes-save").addEventListener("click", () => action(async () => {
    const status = $("codes-status");
    try {
      const day = $("codes-day").value, sent = $("codes-input").value;
      await request(`/api/daily-codes/${day}`, "PUT", { codes: sent });
      if (codeDrafts.get(day) === sent) codeDrafts.delete(day);
      status.hidden = true; status.textContent = "";
      await dailyCodes();
    } catch (error) { status.hidden = false; status.className = "alert small"; status.textContent = error.message; throw error; }
  }));

  function rowIssue(tag, className, message, revealRow, entries) {
    const item = el(tag, className);
    const match = /^Рядок (\d+):\s*(.*)$/s.exec(message);
    if (!match || !revealRow || !entries[Number(match[1]) - 1]) { item.textContent = message; return item; }
    const number = Number(match[1]), line = entries[number - 1].raw_line;
    const jump = el('button', 'row-issue-link', `Рядок ${number} ↗`); jump.type = 'button';
    jump.setAttribute('aria-label', `Перейти до рядка ${number}: ${line}`);
    jump.addEventListener('click', () => revealRow(number, line));
    item.append(jump, el('span', '', `: ${match[2]}`));
    return item;
  }

  // Перевірка рядків під час набору: ті самі правила, що й «Зберегти», без запису.
  function liveCheck(area, box, path, initial = [], getEntries = () => fromLines(area.value), events = ["input"], revealRow = null) {
    const render = (errors, warnings, checked = false) => {
      box.replaceChildren();
      const entries = getEntries();
      for (const e of errors) box.append(rowIssue("li", "error", e, revealRow, entries));
      for (const w of warnings) box.append(rowIssue("li", "", w, revealRow, entries));
      // Перевірка після правки пройшла без зауважень — кажемо це прямо, а не мовчимо.
      if (checked && !errors.length && !warnings.length) box.append(el("li", "ok", "✓ Помилок у рядках немає"));
    };
    render([], initial);
    let timer = null, seq = 0;
    const schedule = () => {
      clearTimeout(timer);
      const mine = ++seq;
      box.replaceChildren();  // Старі номери вже не відповідають відредагованій чернетці.
      timer = setTimeout(async () => {
        try {
          const r = await request(path, "PUT", { entries: getEntries() });
          if (mine === seq) render(r.errors || [], r.warnings || [], true);
        } catch (_) { /* підказка необов’язкова */ }
      }, 600);
    };
    for (const event of events) area.addEventListener(event, schedule);
    return schedule;
  }

  // Один словник за раз: перемикач угорі, ліворуч поле рядків, праворуч пояснення й підказки.
  let dictionaryKind = (() => { try { return localStorage.getItem("radio345:dictionary") || "callsigns"; } catch (_) { return "callsigns"; } })();
  async function dictionaries() {
    const data = await request("/api/dictionaries");
    const box = $("dictionary-lists");
    box.replaceChildren();
    $("dictionaries-status").textContent = "";
    if (!DICTIONARIES[dictionaryKind]) dictionaryKind = "callsigns";
    const picker = el("div", "segmented list-picker");
    let selectedTab;
    for (const [kind, [title]] of Object.entries(DICTIONARIES)) {
      const active = data[kind].entries.filter(e => e.enabled).length;
      const b = el("button", kind === dictionaryKind ? "ghost on" : "ghost", `${title} · ${active}`); b.type = "button";
      if(kind===dictionaryKind)selectedTab=b;
      b.addEventListener("click", () => {
        dictionaryKind = kind;
        try { localStorage.setItem("radio345:dictionary", kind); } catch (_) { /* сесія */ }
        action(dictionaries);
      });
      picker.append(b);
    }
    box.append(picker);
    const kind = dictionaryKind, [title, help] = DICTIONARIES[kind], dictionary = data[kind];
    const panel = el("article", "panel list-layout dictionary-layout has-evaluation");
    const left = el("div", "list-left"), right = el("details", "list-right");
    right.append(el("summary", "", "Пояснення та інструменти"));
    const area = el("textarea", "list-editor");
    area.value = toLines(dictionary.entries);
    let inserted = 0, beforeInsert = null, insertedHighlights = [], requestedInsert = null;
    if (pendingDictionaryInsert?.list === kind) {
      requestedInsert = pendingDictionaryInsert;
      beforeInsert = area.value;
      const result = insertDictionaryLines(area.value, pendingDictionaryInsert);
      area.value = result.text;
      inserted = result.added;
      insertedHighlights = result.highlights;
      pendingDictionaryInsert = null;
    }
    area.dataset.savedValue = toLines(dictionary.entries);
    area.spellcheck = false;
    area.setAttribute("aria-label", title);
    const fit = () => { area.rows = Math.min(32, Math.max(8, area.value.split("\n").length + 2)); };
    fit(); area.addEventListener("input", fit);
    const save = el("button", "", "Зберегти"), status = el("span", "muted small");
    const draftEditor = insertedHighlights.length ? highlightDraftEditor(area, insertedHighlights) : null;
    let undoInsert = null;
    if (inserted) {
      status.className = "warn small";
      status.textContent = 'Є незбережені зміни.';
      save.classList.add("needs-save");
      undoInsert = el("button", "ghost", "↶ Прибрати вставлене"); undoInsert.type = "button";
      undoInsert.addEventListener("click", () => {
        area.value = beforeInsert; fit(); draftEditor.clear(); rowView.clearHighlight(); area.dispatchEvent(new Event("input"));
        save.classList.remove("needs-save"); undoInsert.remove();
        status.textContent = "Вставку прибрано — список як був"; status.className = "muted small";
      });
    } else if (beforeInsert !== null) {
      status.textContent = "Запропонований рядок уже є у словнику — нічого не додано";
    }
    save.addEventListener("click", () => action(async () => {
      const sent = fromLines(area.value);
      try {
        await request(`/api/dictionaries/${kind}`, "PUT", { entries: sent, expected_version: dictionary.version });
        note(`Словник «${title}» збережено. Приймач застосує зміни за кілька секунд.`);
        await dictionaries();
      } catch (error) {
        status.replaceChildren(...error.message.split(/;\s*(?=Рядок \d+:)/).map(message =>
          rowIssue('div', '', message, revealRow, sent)));
        status.className = "alert small"; throw error;
      }
    }));
    const actions = el("div", "list-actions"); actions.append(...(undoInsert ? [undoInsert] : []), status);
    const hints = el("ul", "warn-list");
    const revealRow = (number, line) => rowView.revealRow(number, line);
    liveCheck(area, hints, `/api/dictionaries/${kind}/check`, [], () => fromLines(area.value), ["input"], revealRow);
    if (inserted) area.dispatchEvent(new Event("input"));
    left.append(draftEditor?.element || area, actions, hints);
    if (draftEditor) draftEditor.element.hidden = true;
    const rowDraft = rowDraftOptions(toLines(dictionary.entries), area.value, insertedHighlights, false, requestedInsert);
    const recent = kind === 'callsigns' ? [...recentlySavedDictionaryLines] : [];
    rowDraft.spotlightLines.push(...recent);
    if (recent.length && !requestedInsert) rowDraft.savedSpotlight = true;
    const rowView = rowListView(area, title, save,count=>{selectedTab.textContent=`${title} · ${count}`;}, rowDraft);
    left.prepend(rowView);
    right.append(el("h2", "", title), el("p", "muted small", help));
    panel.append(left, listValidation(`/api/dictionaries/${kind}/check`,()=>fromLines(area.value), false, revealRow), right);
    box.append(panel);
    fitRecognition();
    if (requestedInsert || recent.length) rowView.reveal();
    if (kind === 'callsigns') for (const line of recent) {
      if (dictionary.entries.some(entry => entry.raw_line.trim().toLocaleLowerCase('uk') === line.toLocaleLowerCase('uk')))
        recentlySavedDictionaryLines.delete(line);
    }
  }

  // Рядки, які «Потребує уваги» просить вставити в список (не зберігаються самі).
  let pendingListInsert = null;
  let pendingRuleDisable = null;
  let pendingDictionaryInsert = null;
  let pendingPatternEnable = [];
  function highlightDraftEditor(area, initialRanges) {
    let ranges = initialRanges.map(range => ({ ...range }));
    let previous = area.value;
    const wrapper = el("div", "list-editor-highlight-wrap");
    const overlay = el("pre", "list-editor-highlight-overlay");
    overlay.setAttribute("aria-hidden", "true");
    wrapper.append(overlay, area);
    const render = () => {
      const text = area.value;
      let cursor = 0;
      overlay.replaceChildren();
      for (const range of [...ranges].sort((a, b) => a.start - b.start)) {
        if (range.start < cursor || range.end <= range.start || range.end > text.length) continue;
        if (range.start > cursor) overlay.append(el("span", "", text.slice(cursor, range.start)));
        overlay.append(el("mark", "list-editor-highlighted", text.slice(range.start, range.end)));
        cursor = range.end;
      }
      overlay.append(el("span", "", text.slice(cursor)));
      overlay.scrollTop = area.scrollTop;
      overlay.scrollLeft = area.scrollLeft;
    };
    area.addEventListener("input", () => {
      const text = area.value;
      let prefix = 0, suffix = 0;
      while (prefix < previous.length && prefix < text.length && previous[prefix] === text[prefix]) prefix++;
      while (suffix < previous.length - prefix && suffix < text.length - prefix &&
        previous[previous.length - suffix - 1] === text[text.length - suffix - 1]) suffix++;
      const oldEnd = previous.length - suffix, delta = text.length - previous.length;
      ranges = ranges.flatMap(range => oldEnd <= range.start
        ? [{ start: range.start + delta, end: range.end + delta }]
        : prefix >= range.end ? [range] : []);
      previous = text;
      render();
    });
    area.addEventListener("scroll", render);
    render();
    return {
      element: wrapper,
      clear() { ranges = []; previous = area.value; render(); },
      reveal() {
        area.scrollTop = area.scrollHeight;
        render();
        area.scrollIntoView({ block: "end" });
      },
    };
  }
  // Позивний аліас додається до свого рядка; шум — окремим рядком. Повтори пропускаються.
  function highlightExistingListLines(text, insert) {
    const rows = text ? text.split("\n") : [];
    const fold = value => value.trim().toLocaleLowerCase("uk");
    const remaining = new Map();
    for (const item of insert.lines) {
      const key = fold(item.line);
      remaining.set(key, (remaining.get(key) || 0) + 1);
    }
    const highlights = [];
    let offset = 0;
    rows.forEach(row => {
      const key = fold(row), count = remaining.get(key) || 0;
      if (count) {
        highlights.push({ start: offset, end: offset + row.length });
        if (count === 1) remaining.delete(key);
        else remaining.set(key, count - 1);
      }
      offset += row.length + 1;
    });
    return highlights;
  }
  function insertListLines(text, insert) {
    const rows = text ? text.split("\n") : [];
    const fold = s => s.trim().toLocaleLowerCase("uk");
    const marked = new Map();
    const mark = (row, start, end) => marked.set(row, [...(marked.get(row) || []), { start, end }]);
    let added = 0;
    for (const item of insert.lines) {
      if (insert.list === "callsign_aliases") {
        const at = rows.findIndex(r => r.includes(":") && fold(r.split(":")[0].replace(/^#\s*/, "")) === fold(item.callsign) && !r.trim().startsWith("#"));
        if (at < 0) {
          const row = rows.length;
          rows.push(`${item.callsign}: ${item.line}`); added++;
          mark(row, 0, rows[row].length);
          continue;
        }
        const variants = rows[at].split(":").slice(1).join(":").split(",").map(fold);
        if (variants.includes(fold(item.line))) continue;
        const previous = rows[at].replace(/[\s,]+$/, "");
        rows[at] = `${previous}, ${item.line}`; added++;
        mark(at, previous.length + 2, rows[at].length);
      } else if (!rows.some(r => fold(r) === fold(item.line))) {
        const row = rows.length;
        rows.push(item.line); added++;
        mark(row, 0, item.line.length);
      }
    }
    let offset = 0;
    const highlights = [];
    rows.forEach((row, index) => {
      for (const range of marked.get(index) || []) highlights.push({ start: offset + range.start, end: offset + range.end });
      offset += row.length + 1;
    });
    return { text: rows.join("\n"), added, highlights };
  }
  function insertDictionaryLines(text, insert) {
    const rows = text ? text.split("\n") : [];
    const fold = value => value.trim().toLocaleLowerCase("uk");
    const marked = new Map();
    const mark = (row, start, end) => marked.set(row, [...(marked.get(row) || []), { start, end }]);
    let added = 0;
    for (const item of insert.lines) {
      const at = rows.findIndex(row => {
        const active = row.trim().replace(/^#\s*/, "");
        const name = active.split(":", 1)[0];
        return fold(name) === fold(item.callsign);
      });
      if (at < 0) {
        const row = rows.length;
        rows.push(`${item.callsign}: ${item.number}`);
        added++;
        mark(row, 0, rows[row].length);
        continue;
      }
      const active = rows[at].trim().replace(/^#\s*/, "");
      const [name, ...tail] = active.split(":");
      const numbers = tail.join(":").trim().split(/[,\s]+/).filter(Boolean);
      const alreadyAllowed = numbers.some(value => {
        if (fold(value) === fold(item.number)) return true;
        const range = value.match(/^(\d+)-(\d+)$/);
        return range && Number(item.number) >= Number(range[1]) && Number(item.number) <= Number(range[2]);
      });
      const wasDisabled = rows[at].trim().startsWith("#");
      if (alreadyAllowed && !wasDisabled) continue;
      if (!alreadyAllowed) numbers.push(item.number);
      rows[at] = `${name.trim()}: ${numbers.join(" ")}`;
      added++;
      if (alreadyAllowed) mark(at, 0, rows[at].length);
      else mark(at, rows[at].length - item.number.length, rows[at].length);
    }
    let offset = 0;
    const highlights = [];
    rows.forEach((row, index) => {
      for (const range of marked.get(index) || []) highlights.push({ start: offset + range.start, end: offset + range.end });
      offset += row.length + 1;
    });
    return { text: rows.join("\n"), added, highlights };
  }
  function phrasePatternEditor(entries, enableLines = [], generic = false, save = null, onCount = null, draft = {}) {
    const requested = new Set(enableLines.map(line => line.trim()));
    const spotlight = new Set(draft.spotlightLines || []);
    const baseline = draft.baselineEntries || entries;
    const unused = new Set(baseline.map((_, index) => index));
    const exact = new Map(), names = new Map();
    const rowName = line => line.includes(':') ? line.split(':')[0].trim().toLocaleLowerCase('uk') : null;
    baseline.forEach((entry, index) => {
      const line = entry.raw_line.trim(), name = rowName(line);
      if (!exact.has(line)) exact.set(line, []); exact.get(line).push(index);
      if (name !== null) { if (!names.has(name)) names.set(name, []); names.get(name).push(index); }
    });
    const take = (map, key) => {
      const indices = map.get(key);
      while (indices?.length && !unused.has(indices[0])) indices.shift();
      if (!indices?.length) return null;
      const index = indices.shift(); unused.delete(index); return baseline[index];
    };
    const savedRows = entries.map(entry => take(exact, entry.raw_line.trim()));
    const patterns = entries.map((entry, index) => {
      let saved = savedRows[index];
      if (!saved) {
        const name = rowName(entry.raw_line);
        if (name !== null) saved = take(names, name);
        else if (unused.has(index)) { saved = baseline[index]; unused.delete(index); }
      }
      const savedEnabled = Boolean(saved?.enabled);
      return {
        raw_line: entry.raw_line,
        enabled: Boolean(entry.enabled) || requested.has(entry.raw_line.trim()),
        savedRawLine: saved?.raw_line.trim() || "",
        savedEnabled,
        isNew: !saved,
        spotlight: spotlight.has(entry.raw_line.trim()),
      };
    });
    const root = el("div", "phrase-pattern-editor");
    const toolbar = el("div", "pattern-toolbar");
    const search = el("input", "pattern-search");
    search.type = "search"; search.placeholder = generic ? "Знайти рядок" : "Знайти шаблон"; search.setAttribute("aria-label", search.placeholder);
    const add = el("button", "ghost", generic ? "＋ Додати рядок" : "＋ Додати шаблон"); add.type = "button";
    toolbar.append(search, add);
    if(save) { save.type='button';save.setAttribute('data-action','save');toolbar.append(save); }
    const activeRows = el("div", "phrase-pattern-rows");
    const disabledGroup = el("details", "phrase-pattern-disabled");
    const disabledSummary = el("summary", "", "Вимкнені");
    const disabledRows = el("div", "phrase-pattern-rows");
    disabledGroup.append(disabledSummary, disabledRows);
    activeRows.setAttribute('aria-label','Увімкнені рядки');
    activeRows.setAttribute('role','group');
    root.append(toolbar, activeRows, disabledGroup);
    let onChange = () => {};
    const updateCounts = () => {
      const active = patterns.filter(pattern => pattern.enabled).length;
      disabledSummary.textContent = `Вимкнені (${patterns.length - active})`;
      disabledGroup.hidden = patterns.length===active;
      onCount?.(active);
    };
    const getEntries = () => patterns.map(pattern => ({
      raw_line: pattern.raw_line.trim(), enabled: pattern.enabled,
    })).filter(pattern => pattern.raw_line);
    const isDraft = pattern => pattern.isNew || pattern.raw_line.trim() !== pattern.savedRawLine || pattern.enabled !== pattern.savedEnabled;
    const draftLabel = pattern => {
      const changed = isDraft(pattern);
      if (!changed) return pattern.spotlight ? (draft.savedSpotlight ? "Щойно збережено" : "Вже збережено") : "";
      if (pattern.isNew) return "Чернетка";
      return pattern.enabled && !pattern.savedEnabled ? "Тимчасово увімкнено" : "Чернетка";
    };
    let revealTarget = null, targetPattern = null;
    const rowFields = new Map();
    const updateRowNumbers = () => {
      let number = 0;
      for (const pattern of patterns) {
        const current = pattern.raw_line.trim() ? ++number : null;
        const fields = rowFields.get(pattern);
        if (!fields) continue;
        fields.number.textContent = current === null ? '—' : String(current);
        fields.number.title = current === null ? 'Новий порожній рядок' : `Рядок ${current}`;
        fields.line.setAttribute('aria-label', current === null ? 'Новий порожній рядок' : `${generic ? 'Текст рядка' : 'Текст шаблону'} ${current}`);
      }
    };
    // Строку цілком (з позначкою «Чернетка») — у середину списку; вікно не рухається, якщо рядок
    // і так видно з запасом (раніше scrollIntoView центрував лише поле й завжди прокручував сторінку).
    const revealRow = field => {
      const row = field.closest?.('.phrase-pattern-row') || field;
      const list = row.closest?.('.phrase-pattern-rows');
      if (!list || !row.getBoundingClientRect || !list.getBoundingClientRect) { field.scrollIntoView?.({block: 'nearest', behavior: 'instant'}); return; }
      const rowBox = row.getBoundingClientRect(), listBox = list.getBoundingClientRect();
      list.scrollTop += (rowBox.top + rowBox.height / 2) - (listBox.top + list.clientHeight / 2);
      const view = typeof window !== 'undefined' ? window.innerHeight : 0;
      const box = row.getBoundingClientRect();
      if (view && (box.top < 60 || box.bottom > view - 60)) row.scrollIntoView?.({block: 'center', behavior: 'instant'});
    };
    const focusRow = (field, selection = null) => {
      const reveal = () => {
        if (root.isConnected === false || root.closest?.('[data-panel]')?.hidden || field.isConnected === false) return;
        field.focus({preventScroll: true});
        const selected = selection || {start: 0, end: field.value.length};
        field.setSelectionRange?.(selected.start, selected.end);
        revealRow(field);
      };
      if (typeof requestAnimationFrame === 'function') requestAnimationFrame(() => requestAnimationFrame(reveal));
      else reveal();
    };
    const render = () => {
      revealTarget = null;
      rowFields.clear();
      const query = search.value.trim().toLocaleLowerCase("uk");
      if (query) disabledGroup.open = true;
      const drawRows = (container, enabledState) => {
        container.replaceChildren();
        const visible = patterns.map((pattern, index) => ({ pattern, index })).filter(({ pattern }) =>
        pattern.enabled === enabledState && (!query || pattern.raw_line.toLocaleLowerCase("uk").includes(query)));
        if (!visible.length) {
        const label = !patterns.length && !query ? 'Список порожній. Додайте рядок.' : generic ? (query ? 'За запитом рядків не знайдено.' : enabledState ? 'Увімкнених рядків немає.' : 'Вимкнених рядків немає.')
          : query ? "За запитом шаблонів не знайдено."
          : enabledState ? "Увімкнених шаблонів немає." : "Вимкнених шаблонів немає.";
        container.append(el("p", "muted small", label));
        }
        for (const { pattern, index } of visible) {
        const row = el("div", "phrase-pattern-row");
        const number = el('span', 'editor-row-number');
        const toggle = el("label", "switch");
        const enabled = el("input"); enabled.type = "checkbox"; enabled.checked = pattern.enabled;
        enabled.setAttribute('aria-label',`Стан рядка: ${pattern.raw_line || 'новий рядок'}`);
        toggle.append(enabled, pattern.enabled ? "Увімк." : "Вимк.");
        const badge = el("span", "pattern-draft");
        const updateBadge = () => {
          const label = draftLabel(pattern);
          badge.textContent = label;
          badge.hidden = !label;
          const changed = isDraft(pattern);
          badge.className = `pattern-draft${label === "Тимчасово увімкнено" ? " temporary" : ""}${!changed && label ? " saved" : ""}`;
          row.className = `phrase-pattern-row${changed ? " is-draft" : label ? " is-recent" : ""}${pattern === targetPattern ? ' is-targeted' : ''}`;
        };
        updateBadge();
        const line = el("input", "pattern-line");
        line.type = "text"; line.value = pattern.raw_line; line.spellcheck = false; line.title = pattern.raw_line;
        if (draftLabel(pattern) && (!revealTarget || (!revealTarget.draft && isDraft(pattern))))
          revealTarget = {field: line, disabled: !enabledState, draft: isDraft(pattern), selection: draft.selections?.get(pattern.raw_line.trim())};
        line.setAttribute("aria-label", `${generic ? 'Текст рядка' : 'Текст шаблону'} ${index + 1}`);
        line.addEventListener("input", () => {
          pattern.raw_line = line.value;
          line.title = line.value;
          updateBadge();
          updateRowNumbers();
          onChange();
        });
        enabled.addEventListener("change", () => {
          pattern.enabled = enabled.checked;
          if (!pattern.enabled) disabledGroup.open = true;
          render();
          onChange();
        });
        const remove = el("button", "ghost", "×"); remove.type = "button";
        remove.setAttribute("aria-label", `Видалити ${generic ? 'рядок' : 'шаблон'} ${index + 1}`);
        remove.addEventListener("click", () => {
          patterns.splice(index, 1);
          render();
          onChange();
        });
        rowFields.set(pattern, {line, number});
        row.append(number, toggle, badge, line, remove);
        container.append(row);
        }
      };
      drawRows(activeRows, true);
      drawRows(disabledRows, false);
      updateRowNumbers();
      updateCounts();
    };
    search.addEventListener("input", render);
    add.addEventListener("click", () => {
      patterns.push({ raw_line: search.value.trim(), enabled: Boolean(generic), savedRawLine: "", savedEnabled: Boolean(generic), isNew: true });
      search.value = ""; disabledGroup.open = true;
      render();
      onChange();
      const fields = (generic ? activeRows : disabledRows).querySelectorAll("input");
      const field = fields[fields.length - 1];
      field?.focus({preventScroll: true});
      if (field) revealRow(field);
    });
    render();
    return {
      element: root,
      rows: root,
      entries: getEntries,
      hasBlankRows: () => patterns.some(pattern => !pattern.raw_line.trim()),
      hasChanges: () => patterns.length !== baseline.length || patterns.some(pattern =>
        pattern.isNew || pattern.raw_line.trim() !== pattern.savedRawLine || pattern.enabled !== pattern.savedEnabled),
      reveal() {
        if (!revealTarget) return;
        if (revealTarget.disabled) disabledGroup.open = true;
        focusRow(revealTarget.field, revealTarget.selection);
      },
      revealRow(number, expectedLine) {
        const pattern = patterns.filter(row => row.raw_line.trim())[number - 1];
        if (!pattern || pattern.raw_line.trim() !== expectedLine.trim()) {
          note('Рядок уже змінено. Перевірте поточну чернетку ще раз.'); return false;
        }
        targetPattern = pattern;
        search.value = '';
        if (!pattern.enabled) disabledGroup.open = true;
        render();
        focusRow(rowFields.get(pattern).line);
        return true;
      },
      onChange(callback) { onChange = callback; },
    };
  }

  // Рядковий вигляд для всіх списків. Приховане поле зберігає один чинний
  // контракт перевірки/вставки/скасування; стан між двома редакторами не розходиться.
  function rowListView(area, title, save, onCount = null, draft = {}) {
    const host = el('div', 'row-list-view'); host.setAttribute('aria-label', title);
    let syncing = false;
    const baseline = draft.baselineEntries || fromLines(area.value);
    let spotlight = draft.spotlightLines || [], editor;
    function render() {
      const model = phrasePatternEditor(fromLines(area.value), [], true, save, onCount,
        {...draft, baselineEntries: baseline, spotlightLines: spotlight});
      editor = model;
      host.replaceChildren(model.element);
      model.onChange(() => {
        syncing = true;
        area.value = toLines(model.entries());
        if (save) { save.disabled = model.hasBlankRows();save.classList.add('needs-save'); }
        area.dispatchEvent(new Event('input'));
        syncing = false;
      });
    }
    area.addEventListener('input', () => { if (!syncing) render(); });
    area.hidden = true; render();
    host.reveal = () => editor.reveal();
    host.revealRow = (number, line) => editor.revealRow(number, line);
    host.clearHighlight = () => {spotlight = [];};
    return host;
  }
  function rowDraftOptions(before, text, highlights, savedSpotlight = false, insert = null) {
    const selections = new Map();
    const spotlightLines = highlights.map(range => {
      const start = text.lastIndexOf('\n', range.start - 1) + 1;
      const next = text.indexOf('\n', range.start);
      const full = text.slice(start, next < 0 ? text.length : next);
      const line = fromLines(full)[0]?.raw_line;
      if (line && !selections.has(line)) {
        const offset = start + full.indexOf(line);
        selections.set(line, {start: Math.max(0, range.start - offset), end: Math.min(line.length, range.end - offset)});
      }
      return line;
    }).filter(Boolean);
    if (insert) {
      const fold = value => value.trim().toLocaleLowerCase('uk');
      const lines = new Set(insert.lines.map(item => fold(item.line || '')));
      for (const entry of fromLines(text)) {
        const [name, ...tail] = entry.raw_line.split(':');
        const named = insert.lines.some(item => fold(name) === fold(item.callsign || '') && (
          insert.list === 'callsign_aliases' ? tail.join(':').split(',').some(value => fold(value) === fold(item.line || ''))
            : insert.list === 'callsigns' && tail.join(':').split(/[,\s]+/).some(value => {
              if (value === item.number) return true;
              const range = value.match(/^(\d+)-(\d+)$/);
              return range && Number(item.number) >= Number(range[1]) && Number(item.number) <= Number(range[2]);
            })));
        if (named || (!['callsigns', 'callsign_aliases'].includes(insert.list) && lines.has(fold(entry.raw_line))))
          spotlightLines.push(entry.raw_line);
        if (named && !selections.has(entry.raw_line)) {
          const item = insert.lines.find(item => fold(name) === fold(item.callsign || ''));
          const needle = insert.list === 'callsign_aliases' ? item?.line : item?.number;
          const at = needle ? fold(entry.raw_line).indexOf(fold(needle), name.length + 1) : -1;
          if (at >= 0) selections.set(entry.raw_line, {start: at, end: at + needle.length});
        }
      }
    }
    return {baselineEntries: fromLines(before), spotlightLines, savedSpotlight, selections};
  }
  function withoutImpactRule(entries,type,label) {
    const fold=value=>String(value||'').trim().toLocaleLowerCase('uk').replace(/[’'`]/g,'ʼ').replace(/\s+/g,' ');
    const [heard,target]=label.split(' → '), next=[]; let matched=false;
    for (const entry of entries) {
      if (!entry.enabled) {next.push(entry);continue;}
      if (['aliases','callsign_aliases'].includes(type)) {
        const colon=entry.raw_line.indexOf(':'),name=entry.raw_line.slice(0,colon).trim();
        const variants=entry.raw_line.slice(colon+1).split(',').map(v=>v.trim());
        if (colon>=0 && fold(name)===fold(target) && variants.some(v=>fold(v)===fold(heard))) {
          matched=true;const remaining=variants.filter(v=>fold(v)!==fold(heard));
          if (remaining.length)next.push({...entry,raw_line:`${name}: ${remaining.join(', ')}`});
          next.push({raw_line:`${name}: ${heard}`,enabled:false});continue;
        }
      } else if (fold(entry.raw_line).replace(/[.,;!?]+$/,'')===fold(label).replace(/[.,;!?]+$/,'')) {
        matched=true;next.push({...entry,enabled:false});continue;
      }
      next.push(entry);
    }
    if (!matched)throw new Error('Правило вже змінено або відсутнє в списку. Оновіть звіт впливу перед вимкненням.');
    return next;
  }

  async function texts() {
    const data = await request("/api/text-lists");
    textRevision = data.revision;
    const a = data.applied || {};
    const live = a.receiver === "listening" || a.receiver === "paused";
    $("texts-status").textContent = `Збережена ревізія: ${data.revision}. ` + (!live
      ? "Приймач не працює — списки застосуються під час запуску."
      : a.revision === data.revision ? "Приймач уже використовує цю ревізію."
      : `Приймач: ревізія ${a.revision ?? "—"} (знімок метрик оновлюється ~30 с; самі списки застосовуються за кілька секунд).`);
    // Один список за раз: перемикач угорі, ліворуч вузьке поле рядків, праворуч пояснення, підказки й кандидати.
    const box = $("text-lists");
    box.replaceChildren();
    if (!TEXT_LISTS[textListType]) textListType = "short_garbage";
    const picker = el("div", "segmented list-picker");
    let selectedTab;
    for (const [type, [title]] of Object.entries(TEXT_LISTS)) {
      const active = data.lists[type].entries.filter(e => e.enabled).length;
      const b = el("button", type === textListType ? "ghost on" : "ghost", `${title} · ${active}`); b.type = "button";
      if(type===textListType)selectedTab=b;
      b.addEventListener("click", () => {
        textListType = type;
        try { localStorage.setItem("radio345:text-list", type); } catch (_) { /* сесія */ }
        action(texts);
      });
      picker.append(b);
    }
    box.append(picker);
    const type = textListType, [title, help] = TEXT_LISTS[type], list = data.lists[type];
    const savedEntries = list.entries.map(entry => ({...entry}));
    const previewDisabled = pendingRuleDisable?.type === type;
    const disabledQuery = previewDisabled ? pendingRuleDisable.label.split(' → ')[0] : '';
    if (previewDisabled) {
      const label=pendingRuleDisable.label;pendingRuleDisable=null;
      list.entries=withoutImpactRule(list.entries,type,label);
    }
    const panel = el("article", "panel list-layout");
    const left = el("div", "list-left"), right = el("details", "list-right");
    right.append(el("summary", "", "Пояснення та інструменти"));
    if (previewDisabled) right.open=true;
    const save = el("button", "", "Зберегти"), status = el("span", "muted small");
    if (previewDisabled) {
      save.textContent='Зберегти вимкнення'; save.classList.add('needs-save');
      status.textContent='Чернетка без вибраного правила. Приймач ще не змінено: спочатку перевірка, потім ваше збереження.';
      status.className='warn small';
    }
    const updateCount=count=>{selectedTab.textContent=`${title} · ${count}`;};
    const requestedPatterns = type === "phrase_patterns" ? pendingPatternEnable : [];
    const patterns = type === "phrase_patterns"
      ? phrasePatternEditor(list.entries, requestedPatterns, false, save, updateCount, {spotlightLines: requestedPatterns, baselineEntries: savedEntries})
      : null;
    if (type === "phrase_patterns") pendingPatternEnable = [];
    const area = patterns ? null : el("textarea", "list-editor");
    if (area) {
      area.value = toLines(list.entries);
      area.spellcheck = false;
      area.setAttribute("aria-label", title);
    }
    const getEntries = patterns ? patterns.entries : () => fromLines(area.value);
    if (patterns) left.append(patterns.element);
    let inserted = 0, beforeInsert = null, insertedHighlights = [], alreadySaved = false, requestedInsert = null;
    if (area && pendingListInsert?.list === type) {
      requestedInsert = pendingListInsert;
      beforeInsert = area.value;
      alreadySaved = Boolean(pendingListInsert.alreadySaved);
      const result = pendingListInsert.alreadySaved
        ? { text: area.value, added: 0, highlights: highlightExistingListLines(area.value, pendingListInsert) }
        : insertListLines(area.value, pendingListInsert);
      area.value = result.text; inserted = result.added;
      insertedHighlights = result.highlights;
      pendingListInsert = null;
    }
    const fit = () => { if (area) area.rows = Math.min(30, Math.max(8, area.value.split("\n").length + 2)); };
    fit(); area?.addEventListener("input", fit);
    const warnings = el("ul", "warn-list");
    let rowView = null;
    const revealRow = (number, line) => (patterns || rowView).revealRow(number, line);
    const check = liveCheck(patterns?.rows || area, warnings, `/api/text-lists/${type}/check`, list.warnings || [],
      getEntries, patterns ? [] : ["input"], revealRow);
    const draftEditor = insertedHighlights.length ? highlightDraftEditor(area, insertedHighlights) : null;
    if (patterns) patterns.onChange(() => {
      save.classList.add("needs-save");
      save.disabled = patterns.hasBlankRows();
      status.textContent = patterns.hasBlankRows()
        ? 'Заповніть або видаліть порожній шаблон.'
        : 'Є незбережені зміни.';
      status.className = "warn small";
      check();
    });
    if (patterns?.hasChanges()) {
      save.classList.add("needs-save");
      save.disabled = patterns.hasBlankRows();
      if(!previewDisabled) { status.textContent='Є незбережені зміни.';status.className='warn small'; }
      check();
    }
    let undoInsert = null;
    if (inserted) {
      status.className = "warn small";
      status.textContent = 'Є незбережені зміни.';
      save.classList.add("needs-save");
      area.dispatchEvent?.(new Event("input"));   // одразу та сама перевірка, що й під час набору
      // Не сподобалось — повернути список, як був до вставки (поки не збережено, ніде не діє).
      undoInsert = el("button", "ghost", "↶ Прибрати вставлене"); undoInsert.type = "button";
      undoInsert.addEventListener("click", () => {
        area.value = beforeInsert; fit(); draftEditor.clear(); rowView.clearHighlight(); area.dispatchEvent?.(new Event("input"));
        save.classList.remove("needs-save"); undoInsert.remove();
        status.textContent = "Вставку прибрано — список як був"; status.className = "muted small";
      });
    }
    save.addEventListener("click", () => action(async () => {
      const sent = getEntries();
      try {
        const r = await request(`/api/text-lists/${type}`, "PUT", { entries: sent, expected_revision: textRevision });
        textRevision = r.revision;
        note(`«${title}» збережено (ревізія ${r.revision}). Приймач застосує зміни за кілька секунд.`);
        await texts();
      } catch (error) {
        status.replaceChildren(...error.message.split(/;\s*(?=Рядок \d+:)/).map(message =>
          rowIssue('div', '', message, revealRow, sent)));
        status.className = "alert small"; throw error;
      }
    }));
    const actions = el("div", "list-actions"); actions.append(...(undoInsert ? [undoInsert] : []), status);
    if (area) {
      rowView = rowListView(area, title, save, updateCount,
        rowDraftOptions(toLines(savedEntries), area.value, insertedHighlights, alreadySaved, requestedInsert));
      left.append(rowView, draftEditor?.element || area);
      if (draftEditor) draftEditor.element.hidden = true;
    }
    left.append(actions, warnings);
    right.append(el("h2", "", title), el("p", "muted small", help));
    panel.classList.add("has-evaluation");
    let evaluation;
    if (EVALUATED_LISTS.includes(type)) {
      panel.classList.add("has-evaluation");
      evaluation = listEvaluation(type, getEntries);
      // Після вставки — одразу видно, чи текст стане кращим на ваших еталонах. Запуск — коли завершиться
      // відкриття списку: action() не пускає другу дію, поки триває перша.
      if (inserted || previewDisabled) { const start = () => (busy ? setTimeout(start, 100) : evaluation.run()); setTimeout(start, 0); }
    } else evaluation=listValidation(`/api/text-lists/${type}/check`,getEntries,true,revealRow);
    if (type === "callsign_aliases") right.append(aliasCandidates(list.entries));
    if (type === 'aliases' && !list.entries.length) right.append(el('p','muted small',
      'Це ручні глобальні заміни, а не навчені правила. Порожній список є нормальним: звичайні слова керуються у «Навчанні», а позивні — в окремих аліасах. Неперевірені кандидати автоматично не додаються.'));
    if (type === "blacklist" || type === "short_garbage") right.append(noiseCandidates(type, list.entries));
    panel.append(left, ...(evaluation ? [evaluation] : []), right);
    box.append(panel);
    fitRecognition();
    if (previewDisabled) {
      const search=panel.querySelector('.pattern-search');
      if (search) { search.value=disabledQuery; search.dispatchEvent(new Event('input')); }
    }
    if (requestedInsert || previewDisabled) rowView?.reveal();
    if (requestedPatterns.length || previewDisabled) patterns?.reveal();
  }

  // Перевірка списку на розмічених записах: повтор текстової обробки, без збереження й без зупинки приймача.
  const EVALUATED_LISTS = ["phrase_templates", "phrase_patterns", "callsign_aliases", "aliases", "blacklist", "short_garbage"];
  function listValidation(path,getEntries,whisper=false,revealRow=null) {
    const wrap=el('aside','list-evaluation'), run=el('button','ghost','Перевірити рядки'), out=el('div');run.type='button';
    wrap.setAttribute('aria-label','Перевірка рядків');
    run.addEventListener('click',()=>action(async()=>{
      run.disabled=true;out.textContent='Перевіряю…';
      try {
        const entries=getEntries();
        const r=await request(path,'PUT',{entries});
        out.replaceChildren(el('p',`summary ${r.errors?.length?'bad':'ok'}`,r.errors?.length?'Є помилки у рядках':'Формат рядків правильний'));
        for(const error of r.errors || [])out.append(rowIssue('p','alert small',error,revealRow,entries));
        for(const warning of r.warnings || [])out.append(rowIssue('p','warn small',warning,revealRow,entries));
      } catch(error) { out.replaceChildren(el('p','alert small',error.message)); }
      finally { run.disabled=false; }
    }));
    wrap.append(run,out,el('p','muted small',whisper
      ? 'Тут перевіряється формат підказки. Її вплив на розпізнавання потребує нового аудіопрогону в лабораторії.'
      : 'Перевірка формату без збереження. Зміна словника також впливає на підказку Whisper; для оцінки розпізнавання потрібен аудіопрогін.'));
    const lab=el('button','ghost small-button','До експериментів');lab.type='button';
    lab.addEventListener('click',()=>show('experiments'));wrap.append(lab);
    return wrap;
  }

  function fitRecognition() {
    const panel=document.querySelector('[data-panel]:not([hidden]) .list-layout');
    if(!panel)return;
    if(!window.matchMedia('(min-width:1100px)').matches) { panel.style.removeProperty('--editor-height');return; }
    const main=document.querySelector('main'), style=getComputedStyle(main);
    const top=panel.getBoundingClientRect().top-main.getBoundingClientRect().top+main.scrollTop;
    const height=main.clientHeight-top-parseFloat(style.paddingBottom)-parseFloat(getComputedStyle(panel).marginBottom)-2;
    panel.style.setProperty('--editor-height',`${Math.max(240,Math.floor(height))}px`);
  }
  window.addEventListener('resize',()=>{if(['texts','dictionaries'].includes(view))fitRecognition();});
  function listEvaluation(type, getEntries) {
    const wrap = el("aside", "list-evaluation");
    wrap.setAttribute("aria-label", "Перевірка на еталонах");
    const run = el("button", "ghost", "Перевірити на моїх еталонах"); run.type = "button";
    run.title = "Як змінився б текст розмічених записів із цим списком (зміни не зберігаються)";
    const out = el("div");
    const task = serverJobPanel({button: run, box: out, kind: "evaluate", listType: type,
      start: () => request(`/api/text-lists/${type}/evaluate`, "PUT", {entries: getEntries()}),
      render: r => {
        const b = r.before, a = r.after;
        const tone = r.worse ? "bad" : r.better ? "ok" : "";
        const summary = el("div", `summary ${tone}`);
        summary.append(el("b", "", r.changed ? `Змінилося фраз: ${r.changed} — краще ${r.better}, гірше ${r.worse}` : "Жодна фраза не змінилася"),
          el("div", "small muted", `Мовлення: ${r.records} · шум/сигнали/кімната: ${b.non_speech_records || 0} · без правок ${b.no_edits} → ${a.no_edits} · пропущено ${b.missed} → ${a.missed} · хибний текст ${b.false_text} → ${a.false_text} · помилка в словах ${pct(b.wer)} → ${pct(a.wer)} · позивні ${b.callsigns_hit} → ${a.callsigns_hit} з ${a.callsigns_ref} (хибних ${b.callsigns_false} → ${a.callsigns_false}) · команди ${b.commands_hit} → ${a.commands_hit} з ${a.commands_ref}`));
        out.replaceChildren(summary);
        for (const c of r.examples) {
          const item = el("div", `eval-item ${c.delta > 0 ? "bad" : c.delta < 0 ? "ok" : ""}`);
          const link = el("a", "small", c.id.slice(0, 8)); link.href = `?view=recordings&q=${c.id.slice(0, 8)}`;
          item.append(link, el("span", "small", c.delta > 0 ? ` гірше на ${c.delta} сл.` : c.delta < 0 ? ` краще на ${-c.delta} сл.` : " так само"),
            el("div", "small", `було: ${c.before}`), el("div", "small", `стало: ${c.after}`), el("div", "small muted", `еталон: ${c.reference}`));
          if (c.matched_patterns?.length) {
            item.append(el("div", "small muted", `Збіг шаблону: ${c.matched_patterns.join(" · ")}`));
          }
          out.append(item);
        }
      },
    });
    run.addEventListener("click", () => task.run());
    // Дочекаємося монтування картки, тоді відновимо останній збережений прогін.
    setTimeout(() => task.resume(), 0);
    wrap.append(run, out);
    wrap.run = () => run.click();   // запуск без кліку (після «＋ Вставити в список»)
    return wrap;
  }

  // Підказка: як Whisper чув позивні в розмічених записах. Додає лише оператор.
  const CANDIDATE_FLAGS = {
    already_added: "вже додано",
    already_matched: "нечіткий пошук уже ловить",
    dictionary_word: "словникове слово — не можна",
    function_word: "службове або коротке слово — небезпечно",
    ambiguous: "веде до різних позивних",
    number_unknown: "номера немає у словнику",
    other_dictionary: "є в іншому словнику — діятиме лише перед номерами позивного",
    no_number: "позивний без номера — діятиме на кожне це слово",
  };
  function aliasCandidates(entries) {
    const wrap = el("div", "alias-candidates");
    const load = el("button", "ghost", "Кандидати з архіву");
    const out = el("div");
    load.addEventListener("click", () => action(async () => {
      if (out.childNodes.length) { out.replaceChildren(); load.textContent = "Кандидати з архіву"; return; }
      load.textContent = "Сховати кандидатів";
      out.replaceChildren(el("p", "muted small", "Перевіряю розмічені записи через обробку тексту…"));
      const data = await request("/api/text-lists/callsign-alias-candidates");
      out.replaceChildren();
      if (!data.items.length) { out.append(el("p", "muted small", `Розмічених записів мовлення: ${data.records}. Кандидатів немає.`)); return; }
      out.append(el("p", "muted small", `З ${data.records} розмічених записів: слово Whisper перед тим самим номером, що й позивний у правильному тексті. `
        + "Записи, де обробка тексту вже знайшла цей позивний, не враховано. Додавайте лише варіанти, які не можуть бути звичайним словом."));
      const table = el("table"), head = el("tr");
      for (const h of ["Почуто", "Позивний", "Разів", "Схожість", "Примітки", ""]) head.append(el("th", "", h));
      table.append(head);
      for (const c of data.items) {
        const row = el("tr");
        const flags = el("td", "small");
        for (const f of c.flags) flags.append(el("span", "badge", CANDIDATE_FLAGS[f] || f), " ");
        const cell = el("td");
        if (c.addable) {
          const add = el("button", "ghost", "Додати");
          add.addEventListener("click", () => action(async () => {
            const next = entries.concat([{ raw_line: `${c.callsign}: ${c.heard}`, enabled: true }]);
            const r = await request("/api/text-lists/callsign_aliases", "PUT", { entries: next, expected_revision: textRevision });
            textRevision = r.revision;
            note(`Додано «${c.callsign}: ${c.heard}» (ревізія ${r.revision}).`);
            pendingListInsert = {list: 'callsign_aliases', lines: [{callsign: c.callsign, line: c.heard}], alreadySaved: true};
            await texts();
          }));
          cell.append(add);
        }
        row.append(el("td", "", c.heard), el("td", "", c.callsign), el("td", "", String(c.count)),
                   el("td", "", `${c.similarity}%`), flags, cell);
        table.append(row);
      }
      const scroll = el("div", "table-wrap"); scroll.append(table);
      out.append(scroll);
    }));
    wrap.append(load, out);
    return wrap;
  }

  // Підказка: тексти Whisper, що повторюються на записах без мовлення. Додає лише оператор.
  const NOISE_FLAGS = {
    already_added: "вже у списках",
    has_number: "є число — може бути повідомленням",
    dictionary_word: "словникове ім’я — не можна",
    near_name: "схоже на назву зі словника — ризиковано",
    in_speech: "трапляється в справжньому мовленні",
  };
  const LABEL_NAMES = { noise: "шум ефіру", room: "кімната", notification: "сигнал" };
  function noiseCandidates(type, entries) {
    const wrap = el("div", "alias-candidates");
    const load = el("button", "ghost", "Кандидати з архіву");
    const out = el("div");
    load.addEventListener("click", () => action(async () => {
      if (out.childNodes.length) { out.replaceChildren(); load.textContent = "Кандидати з архіву"; return; }
      load.textContent = "Сховати кандидатів";
      const data = await request("/api/text-lists/noise-candidates");
      const items = data.items.filter(i => i.list === type);
      if (!items.length) { out.append(el("p", "muted small", `Розмічених записів: ${data.records}. Кандидатів немає.`)); return; }
      out.append(el("p", "muted small", "Тексти Whisper, що повторюються на записах, які ви позначили як шум, кімнату чи сигнал. "
        + "«Дійшло до тексту» — скільки разів такий запис пройшов усі фільтри. До трьох слів — сюди, у «Короткий шум»; довші — у «Фрази-галюцинації»."));
      const table = el("table"), head = el("tr");
      for (const h of ["Текст", "Разів", "Дійшло до тексту", "Де", "Примітки", ""]) head.append(el("th", "", h));
      table.append(head);
      for (const c of items) {
        const row = el("tr"), flags = el("td", "small"), cell = el("td");
        for (const f of c.flags) flags.append(el("span", "badge", NOISE_FLAGS[f] || f), " ");
        if (c.addable) {
          const add = el("button", "ghost", "Додати");
          add.addEventListener("click", () => action(async () => {
            const r = await request(`/api/text-lists/${type}`, "PUT",
              { entries: entries.concat([{ raw_line: c.text, enabled: true }]), expected_revision: textRevision });
            textRevision = r.revision;
            note(`Додано «${c.text}» (ревізія ${r.revision}).`);
            pendingListInsert = { list: type, lines: [{ line: c.text }], alreadySaved: true };
            await texts();
          }));
          cell.append(add);
        }
        const where = Object.entries(c.labels).map(([k, v]) => `${LABEL_NAMES[k] || k}: ${v}`).join(", ");
        const text = el("td", "", c.text); text.title = c.example;
        row.append(text, el("td", "", String(c.count)), el("td", "", String(c.reached_text)), el("td", "small", where), flags, cell);
        table.append(row);
      }
      const scroll = el("div", "table-wrap"); scroll.append(table);
      out.append(scroll);
    }));
    wrap.append(load, out);
    return wrap;
  }

  // ---------- Налаштування: одна сторінка, розділи в порядку обробки фрази, одна кнопка «Зберегти» ----------
  // "restart" — /api/settings (діє після перезапуску приймача), "runtime" — /api/runtime-settings (за кілька секунд).
  const RUNTIME_LABELS = {
    COOLDOWN_S: "Пауза між однаковими повідомленнями, с",
    LOG_CALLSIGNS_ONLY: "Записувати події з позивними навіть без команд",
    PHRASE_FRESH_S: "Фраза старша за N с — лише в архів, без події й Telegram",
    GARBAGE_MIN_WORDS: "Довге сміття: перевіряти фрази від N слів",
    GARBAGE_MIN_UNIQUE_RATIO: "…мінімальна частка різних слів (0–1)",
    GARBAGE_MAX_WORDS_PER_S: "…максимум слів за секунду аудіо",
    GARBAGE_EXTRA_WORDS: "…запас слів понад цю норму",
    LOW_CONFIDENCE_MAX_WORDS: "Коротка невпевнена фраза: до N слів (0 — вимкнено)",
    LOW_CONFIDENCE_LOGPROB: "…і впевненість Whisper нижча за (avg_logprob)",
    DICTIONARY_ECHO_MIN_RUN: "Перелік назв із підказки: від N назв підряд (0 — вимкнено)",
    REJECT_BARE_CALLSIGN: "Відкидати одиночний позивний без номера й команди",
    LIVE_MIN_RMS: "«Наживо» на iPhone: мовчати, доки рівень нижчий за (RMS, 0 — вимкнено)",
    MIN_RMS: "Тихіше за цей рівень — не розпізнавати (RMS, 0 — вимкнено)",
    WHISPER_RETRY_WITHOUT_PROMPT: "Другий шанс: зациклився з підказкою — ще раз без неї",
    WHISPER_PROMPT_DAILY_CODE: "Останній код доби в підказці Whisper",
    WHISPER_COMPRESSION_LIMIT: "Відкидати шматок зі стисненням вище (зациклення; 0 — вимкнено)",
    WHISPER_NO_SPEECH_PROB: "Шматок-тиша: no_speech вище (0 — вимкнено)",
    WHISPER_NO_SPEECH_LOGPROB: "…і впевненість нижча за (avg_logprob)",
    SPECTRAL_NOTIFICATION_MODE: "Режим",
    SPECTRAL_NOTIFICATION_THRESHOLD: "Поріг схожості на зразок",
    SPECTRAL_NOTIFICATION_MAX_OUTSIDE_S: "Звуку поза збігом не більше, с",
    SPECTRAL_NOTIFICATION_MAX_EXTRA: "Сторонньої енергії не більше (0–1)",
    TELEGRAM_AUTO_DELETE_HOURS: "Автовидалення копій Telegram, год (0 — вимкнено)",
  };
  const SETTINGS_SECTIONS = [
    { title: "Звук і нарізка на фрази", about: "Що вважати фразою — ще до Whisper.", fields: [
      ["restart", "VAD_LEVEL"], ["restart", "SILENCE_END_MS"], ["restart", "MIN_SPEECH_MS"], ["restart", "MAX_SEGMENT_S"],
      ["runtime", "MIN_RMS"], ["runtime", "LIVE_MIN_RMS"]] },
    { title: "Фільтр сигналів сповіщень", about: "Звук телефона відкидається до Whisper.", link: ["filter", "Зразки й перевірка →"], fields: [
      ["runtime", "SPECTRAL_NOTIFICATION_MODE"], ["runtime", "SPECTRAL_NOTIFICATION_THRESHOLD"],
      ["runtime", "SPECTRAL_NOTIFICATION_MAX_OUTSIDE_S"], ["runtime", "SPECTRAL_NOTIFICATION_MAX_EXTRA"]] },
    { title: "Whisper", about: "Підказка, другий шанс і шматки відповіді, що відкидаються мовчки.", fields: [
      ["runtime", "WHISPER_RETRY_WITHOUT_PROMPT"], ["runtime", "WHISPER_PROMPT_DAILY_CODE"],
      ["runtime", "WHISPER_COMPRESSION_LIMIT"], ["runtime", "WHISPER_NO_SPEECH_PROB"], ["runtime", "WHISPER_NO_SPEECH_LOGPROB"],
      ["restart", "USE_LLM"]] },
    { title: "Відсів тексту", about: "Що відкидається після Whisper.", link: ["texts", "Списки фраз →"], fields: [
      ["runtime", "GARBAGE_MIN_WORDS"], ["runtime", "GARBAGE_MIN_UNIQUE_RATIO"], ["runtime", "GARBAGE_MAX_WORDS_PER_S"],
      ["runtime", "GARBAGE_EXTRA_WORDS"], ["runtime", "LOW_CONFIDENCE_MAX_WORDS"], ["runtime", "LOW_CONFIDENCE_LOGPROB"],
      ["runtime", "DICTIONARY_ECHO_MIN_RUN"], ["runtime", "REJECT_BARE_CALLSIGN"]] },
    { title: "Події й сповіщення", about: "Коли фраза стає подією й повідомленням.", fields: [
      ["runtime", "COOLDOWN_S"], ["runtime", "LOG_CALLSIGNS_ONLY"], ["runtime", "PHRASE_FRESH_S"]] },
  ];
  // Має своє місце: «Система → Дані й копії» поруч з іншими строками зберігання.
  const SETTINGS_ELSEWHERE = new Set(["TELEGRAM_AUTO_DELETE_HOURS"]);
  const RUNTIME_CHOICES = { off: "вимкнено", shadow: "лише оцінювати", active: "відкидати сигнали" };
  let runtimeCurrent = null, restartCurrent = null;
  const showValue = (value, limits) => limits.type === "str" ? RUNTIME_CHOICES[value] || value
    : limits.type === "bool" ? (value ? "так" : "ні") : String(value).replace(".", ",");
  const settingInput = (source, key) => $(`${source === "runtime" ? "runtime" : "setting"}-${key}`);
  const readInput = (input, limits) => limits.type === "bool" ? input.checked : limits.type === "str" ? input.value
    : limits.type === "int" ? parseInt(input.value, 10) : Number(input.value);
  function settingSpec(source, key) {
    if (source === "runtime") {
      if (!runtimeCurrent || !(key in runtimeCurrent.values)) return null;
      return { value: runtimeCurrent.values[key], fallback: runtimeCurrent.defaults[key], limits: runtimeCurrent.limits[key],
               label: RUNTIME_LABELS[key] || key };
    }
    if (!restartCurrent || !(key in restartCurrent.values)) return null;
    const value = restartCurrent.values[key];
    return { value, fallback: restartCurrent.defaults?.[key], label: LABELS[key] || key,
             limits: { type: typeof value === "boolean" ? "bool" : "int", min: 0 } };
  }
  // Зручний діапазон повзунка: [мін, макс, крок]. Поле числа поруч приймає й значення поза ним (у межах limits).
  const SETTING_SLIDERS = {
    VAD_LEVEL: [0, 3, 1], SILENCE_END_MS: [200, 2000, 50], MIN_SPEECH_MS: [100, 2000, 50], MAX_SEGMENT_S: [5, 30, 1],
    MIN_RMS: [0, 0.06, 0.001], LIVE_MIN_RMS: [0, 0.06, 0.001],
    SPECTRAL_NOTIFICATION_THRESHOLD: [0.5, 0.99, 0.01], SPECTRAL_NOTIFICATION_MAX_OUTSIDE_S: [0, 3, 0.1],
    SPECTRAL_NOTIFICATION_MAX_EXTRA: [0, 0.5, 0.01],
    WHISPER_COMPRESSION_LIMIT: [0, 6, 0.1], WHISPER_NO_SPEECH_PROB: [0, 1, 0.05], WHISPER_NO_SPEECH_LOGPROB: [-3, 0, 0.05],
    GARBAGE_MIN_WORDS: [4, 60, 1], GARBAGE_MIN_UNIQUE_RATIO: [0, 1, 0.05], GARBAGE_MAX_WORDS_PER_S: [1, 15, 0.5],
    GARBAGE_EXTRA_WORDS: [0, 20, 1], LOW_CONFIDENCE_MAX_WORDS: [0, 10, 1], LOW_CONFIDENCE_LOGPROB: [-3, 0, 0.05],
    DICTIONARY_ECHO_MIN_RUN: [0, 10, 1], COOLDOWN_S: [0, 120, 1], PHRASE_FRESH_S: [30, 3600, 30],
  };
  // Який фільтр (статус у lab.analysis filters) керується цим полем — поруч показуємо його цифри й вердикт.
  const FILTER_OF_SETTING = { MIN_RMS: "low_rms", SPECTRAL_NOTIFICATION_MODE: "notification", GARBAGE_MIN_WORDS: "garbage",
    LOW_CONFIDENCE_MAX_WORDS: "low_confidence", DICTIONARY_ECHO_MIN_RUN: "dictionary_echo", REJECT_BARE_CALLSIGN: "bare_callsign",
    WHISPER_COMPRESSION_LIMIT: "segment_loop", WHISPER_NO_SPEECH_PROB: "segment_silence", WHISPER_RETRY_WITHOUT_PROMPT: "empty_text" };
  const FILTER_OF_SECTION = { "Відсів тексту": ["blacklist", "short_garbage"] };
  const VERDICT_CHIP = { off: ["bad", "вимкнути"], relax: ["warn", "послабити"], keep: ["ok", "працює"],
    label: ["", "мало розмітки"], info: ["warn", "втрачене мовлення"] };
  let filterStats = null;
  const decimals = step => (String(step).split(".")[1] || "").length;
  function filterChip(status) {
    const f = filterStats?.filters?.find(x => x.status === status);
    if (!f) return null;
    const [tone, word] = VERDICT_CHIP[f.verdict] || ["", f.verdict];
    const chip = el("span", `filter-chip ${tone}`, `${f.title}: ${f.dropped} за ${filterStats.days} діб · ${word}`);
    chip.title = `Шкода ${f.lost}, користь ${f.caught}, не розмічено ${f.unlabelled}` +
      (f.verdict === "label" ? `. Для вердикту розмітьте ще ${f.need} з відкинутого.` : "") + (f.note ? `. ${f.note}` : "");
    return chip;
  }
  // Поле: змінене відносно config.py — смуга ліворуч і «↺»; ⟳ — діє лише після перезапуску приймача.
  function settingField(source, key) {
    const spec = settingSpec(source, key);
    if (!spec) return null;
    const { value, fallback, limits } = spec;
    const row = el("label", "setting-row");
    row.dataset.source = source; row.dataset.key = key;
    row.dataset.search = `${spec.label} ${key}`.toLowerCase();
    const text = el("span", "setting-text"), control = el("span", "setting-control");
    text.append(el("span", "setting-name", spec.label));
    const id = `${source === "runtime" ? "runtime" : "setting"}-${key}`;
    let input;
    if (limits.type === "bool") {
      input = el("input"); input.type = "checkbox"; input.checked = value; input.className = "switch-input";
      const sw = el("span", "toggle-switch"); sw.append(input, el("span", "switch-track"));
      control.append(sw);
    } else if (limits.type === "str") {
      // Варіанти — чипи; значення тримає приховане поле, щоб збереження читало його як раніше.
      input = el("input"); input.type = "hidden"; input.value = value;
      const chips = el("span", "choice-chips");
      for (const choice of limits.choices) {
        const b = el("button", "choice-chip" + (choice === value ? " active" : ""), RUNTIME_CHOICES[choice] || choice);
        b.type = "button"; b.dataset.value = choice;
        b.addEventListener("click", () => {
          input.value = choice;
          for (const other of chips.children) other.classList.toggle("active", other === b);
          settingsDirty();
        });
        chips.append(b);
      }
      control.append(chips, input);
    } else {
      input = el("input"); input.type = "number"; input.value = value; input.required = true;
      if (limits.min !== undefined) input.min = limits.min;
      if (limits.max !== undefined) input.max = limits.max;
      input.step = limits.type === "int" ? "1" : limits.max <= 1 ? "0.001" : "0.01";
      const slide = SETTING_SLIDERS[key];
      if (slide) {
        const range = el("input"); range.type = "range"; range.className = "setting-range";
        [range.min, range.max, range.step] = slide; range.value = value;
        range.setAttribute("aria-label", spec.label);
        if (fallback !== undefined) {   // позначка «типово» на шкалі
          const list = el("datalist"); list.id = `${id}-default`;
          const o = el("option"); o.value = fallback; list.append(o);
          range.setAttribute("list", list.id); control.append(list);
        }
        range.addEventListener("input", () => { input.value = Number(range.value).toFixed(decimals(slide[2])).replace(/\.?0+$/, "") || "0"; settingsDirty(); });
        input.addEventListener("input", () => { range.value = input.value; });
        control.append(range);
      }
      control.append(input);
    }
    input.id = id;
    input.dataset.saved = JSON.stringify(value);
    const changed = fallback !== undefined && value !== fallback;
    row.classList.toggle("changed", changed);
    const info = el("span", "setting-hint", (source === "restart" ? "⟳ після перезапуску · " : "") +
      (fallback === undefined ? "" : `${changed ? "змінено · " : ""}типово ${showValue(fallback, limits)}`));
    if (changed) {
      const reset = el("button", "ghost small-button", "↺"); reset.type = "button"; reset.title = "Повернути значення з config.py";
      reset.addEventListener("click", e => {
        e.preventDefault();
        if (limits.type === "bool") input.checked = fallback;
        else if (limits.type === "str") control.querySelector(`[data-value="${fallback}"]`)?.click();
        else { input.value = fallback; const r = control.querySelector(".setting-range"); if (r) r.value = fallback; }
        settingsDirty();
      });
      info.append(" ", reset);
    }
    text.append(info);
    const chip = FILTER_OF_SETTING[key] && filterChip(FILTER_OF_SETTING[key]);
    if (chip) text.append(chip);
    row.append(text, control);
    return row;
  }
  function settingsChanges() {
    const out = { runtime: {}, restart: {} };
    for (const label of $("settings-sections").querySelectorAll(".setting-row")) {
      const { source, key } = label.dataset, spec = settingSpec(source, key), input = settingInput(source, key);
      if (!spec || !input) continue;
      const now = readInput(input, spec.limits);
      const dirty = JSON.stringify(now) !== input.dataset.saved;
      label.classList.toggle("dirty", dirty);
      if (dirty) out[source][key] = now;
    }
    return out;
  }
  function settingsDirty() {
    const c = settingsChanges(), n = Object.keys(c.runtime).length + Object.keys(c.restart).length;
    $("settings-dirty").textContent = n ? `Не збережено: ${n}` : "";
    $("settings-save").disabled = !n;
    for (const chip of $("settings-nav").children) {
      const card = $(chip.dataset.target);
      chip.classList.toggle("dirty", !!card?.querySelector(".setting-row.dirty"));
      chip.classList.toggle("changed", !!card?.querySelector(".setting-row.changed"));
    }
  }
  function settingsStatus() {
    const a = runtimeCurrent?.applied || {}, live = a.receiver === "listening" || a.receiver === "paused";
    const applied = !live ? "Приймач не працює — значення застосуються під час запуску."
      : a.revision === runtimeCurrent.revision ? "Приймач уже використовує збережені значення."
      : "Приймач підхопить збережене за кілька секунд.";
    const profile = filterStats?.profile ? ` Цифри фільтрів — канал ${filterStats.profile}, ${filterStats.days} діб.` : "";
    $("settings-status").textContent = `${applied} Позначка ⟳ — діє лише після перезапуску приймача.${profile}`;
  }
  async function settings() {
    const [runtime, summary, stats] = await Promise.all([request("/api/runtime-settings"), request("/api/summary"),
      request("/api/filter-stats").catch(() => null)]);
    runtimeCurrent = runtime; restartCurrent = summary.settings; filterStats = stats;
    const nav = $("settings-nav"); nav.replaceChildren();
    const box = $("settings-sections"), placed = new Set();
    box.replaceChildren();
    const known = new Set(SETTINGS_SECTIONS.flatMap(s => s.fields.map(([src, key]) => `${src}:${key}`)));
    const rest = [...Object.keys(runtime.values).filter(k => !known.has(`runtime:${k}`) && !SETTINGS_ELSEWHERE.has(k)).map(k => ["runtime", k]),
                  ...Object.keys(restartCurrent.values).filter(k => !known.has(`restart:${k}`)).map(k => ["restart", k])];
    for (const section of [...SETTINGS_SECTIONS, { title: "Інше", about: "", fields: rest }]) {
      const fields = section.fields.map(([src, key]) => placed.has(`${src}:${key}`) ? null : (placed.add(`${src}:${key}`), settingField(src, key))).filter(Boolean);
      if (!fields.length) continue;
      const card = el("article", "panel settings-section"), head = el("div", "panel-head");
      card.id = `settings-${box.children.length}`;
      head.append(el("h2", "", section.title));
      const jump = el("button", "nav-chip", section.title); jump.type = "button"; jump.dataset.target = card.id;
      jump.addEventListener("click", () => card.scrollIntoView({ behavior: "smooth", block: "start" }));
      nav.append(jump);
      if (section.link) {
        const go = el("button", "link-button small", section.link[1]); go.type = "button";
        go.addEventListener("click", () => show(section.link[0]));
        head.append(go);
      }
      card.append(head);
      if (section.about) card.append(el("p", "muted small", section.about));
      const sectionChips = (FILTER_OF_SECTION[section.title] || []).map(filterChip).filter(Boolean);
      if (sectionChips.length) { const line = el("p", "filter-chips"); line.append(...sectionChips); card.append(line); }
      const list = el("div", "settings-fields"); list.append(...fields);
      card.append(list);
      box.append(card);
    }
    if (filterStats?.profile) $("settings-status").dataset.profile = filterStats.profile;
    settingsStatus(); settingsDirty(); settingsFilter();
  }
  // Пошук параметра за назвою: решта рядків і порожні розділи ховаються.
  function settingsFilter() {
    const q = ($("settings-search").value || "").trim().toLowerCase();
    for (const card of $("settings-sections").children) {
      let shown = 0;
      for (const row of card.querySelectorAll(".setting-row")) {
        const hit = !q || row.dataset.search.includes(q);
        row.hidden = !hit; shown += hit;
      }
      card.hidden = !shown;
      const chip = $("settings-nav").querySelector(`[data-target="${card.id}"]`);
      if (chip) chip.hidden = !shown;
    }
  }
  $("settings-search").addEventListener("input", settingsFilter);
  async function runtimeSettings() { /* сумісність: усе завантажує settings() */ }
  $("settings-sections").addEventListener("input", settingsDirty);
  $("settings-sections").addEventListener("change", settingsDirty);
  $("settings-form").addEventListener("submit", e => { e.preventDefault(); action(async () => {
    const c = settingsChanges(), parts = [];
    if (Object.keys(c.runtime).length) {
      const values = {};
      for (const [key, limits] of Object.entries(runtimeCurrent.limits)) {
        const input = $(`runtime-${key}`);
        values[key] = input ? readInput(input, limits) : runtimeCurrent.values[key];
      }
      const r = await request("/api/runtime-settings", "PUT", { values, expected_revision: runtimeCurrent.revision });
      parts.push(`діє за кілька секунд (ревізія ${r.revision})`);
    }
    if (Object.keys(c.restart).length) {
      const values = {};
      for (const key of Object.keys(restartCurrent.values)) {
        const input = $(`setting-${key}`);
        values[key] = input.type === "checkbox" ? input.checked : Number(input.value);
      }
      await request("/api/settings", "PUT", { values, expected_revision: restartCurrent.revision });
      parts.push("поля ⟳ — після перезапуску приймача");
    }
    await settings();
    note(parts.length ? `Збережено: ${parts.join("; ")}.` : "Змін немає.");
  }); });

