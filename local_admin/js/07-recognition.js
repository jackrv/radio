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
    // Збережену вкладку можна перечитати, але незбережену чернетку не перезаписуємо.
    const editor = document.querySelector('[data-panel="dictionaries"] textarea');
    if (!editor || editor.value === editor.dataset.savedValue) loaded.delete("dictionaries");
    note(`Позивний «${line}» додано. Приймач підхопить зміну за кілька секунд.`);
  }

  // ---------- Словники позивних, команд і Telegram ----------
  const DICTIONARIES = {
    callsigns: ["Позивні", "«назва: номери»: «береза: 31 56 700-710». Без номерів — позивний, що звучить сам: «вишня». «*» — будь-який номер. Порожнім словник позивних лишити не можна."],
    commands: ["Команди", "«назва номер» так, як вимовляють: «злива 42 42» (номер 4242). Без номера — ключове слово: «грім». Кілька номерів — через двокрапку: «злива: 4242 2727»."],
    telegram: ["Telegram (/watch)", "Що надсилати підписникам /watch. «береза: 56» — лише з цим номером; «вишня» без номера — і сам позивний без номера (на шум Whisper іноді вигадує такі слова); «вишня: *» — з будь-яким номером."],
  };
  // ---------- Коди доби ----------
  let codesByDay = {}, changeHour = 20;
  function showDayCodes() {
    $("codes-input").value = (codesByDay[$("codes-day").value] || []).join(", ");
    const day = $("codes-day").value;
    if (!day) { $("codes-period").textContent = ""; return; }
    const end = new Date(`${day}T00:00:00`); end.setHours(changeHour);
    const start = new Date(end.getTime() - 24 * 3600 * 1000);
    const fmt = d => `${d.toLocaleDateString("uk-UA", { day: "2-digit", month: "2-digit" })} ${String(changeHour).padStart(2, "0")}:00`;
    $("codes-period").textContent = `діють з ${fmt(start)} до ${fmt(end)}`;
  }
  async function dailyCodes() {
    const data = await request("/api/daily-codes");
    codesByDay = Object.fromEntries(data.items.map(i => [i.day, i.codes]));
    changeHour = data.change_hour ?? 20;
    $("codes-permanent").value = (data.permanent || []).join(", ");
    if (!$("codes-day").value) $("codes-day").value = data.today;
    showDayCodes();
    const box = $("codes-recent");
    box.replaceChildren();
    if (!data.items.length) { box.textContent = "Кодів ще не внесено."; return; }
    box.append(el("span", "", "Останні дні: "));
    for (const item of data.items) {
      const link = el("button", "ghost", `${item.day}: ${item.codes.join(", ")}`); link.type = "button";
      link.title = `Діють з ${item.period?.[0] ?? ""} до ${item.period?.[1] ?? ""}`;
      link.addEventListener("click", () => { $("codes-day").value = item.day; showDayCodes(); });
      box.append(link);
    }
  }
  $("codes-day").addEventListener("change", showDayCodes);
  $("codes-permanent-save").addEventListener("click", () => action(async () => {
    const status = $("codes-status");
    try {
      const result = await request("/api/daily-codes/permanent", "PUT", { codes: $("codes-permanent").value });
      status.className = "ok small";
      status.textContent = result.codes.length ? `✓ Постійні коди: ${result.codes.join(", ")}` : "✓ Постійні коди прибрано";
      await dailyCodes();
    } catch (error) { status.className = "alert small"; status.textContent = error.message; throw error; }
  }));
  $("codes-save").addEventListener("click", () => action(async () => {
    const status = $("codes-status");
    try {
      const result = await request(`/api/daily-codes/${$("codes-day").value}`, "PUT", { codes: $("codes-input").value });
      status.className = "ok small";
      status.textContent = result.codes.length ? `✓ ${result.day}: ${result.codes.join(", ")}` : `✓ ${result.day}: коди прибрано`;
      await dailyCodes();
    } catch (error) { status.className = "alert small"; status.textContent = error.message; throw error; }
  }));

  // Перевірка рядків під час набору: ті самі правила, що й «Зберегти», без запису.
  function liveCheck(area, box, path, initial = [], getEntries = () => fromLines(area.value), events = ["input"]) {
    const render = (errors, warnings, checked = false) => {
      box.replaceChildren();
      for (const e of errors) box.append(el("li", "error", e));
      for (const w of warnings) box.append(el("li", "", w));
      // Перевірка після правки пройшла без зауважень — кажемо це прямо, а не мовчимо.
      if (checked && !errors.length && !warnings.length) box.append(el("li", "ok", "✓ Помилок у рядках немає"));
    };
    render([], initial);
    let timer = null, seq = 0;
    const schedule = () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const mine = ++seq;
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
    for (const [kind, [title]] of Object.entries(DICTIONARIES)) {
      const active = data[kind].entries.filter(e => e.enabled).length;
      const b = el("button", kind === dictionaryKind ? "ghost on" : "ghost", `${title} · ${active}`); b.type = "button";
      b.addEventListener("click", () => {
        dictionaryKind = kind;
        try { localStorage.setItem("radio345:dictionary", kind); } catch (_) { /* сесія */ }
        action(dictionaries);
      });
      picker.append(b);
    }
    box.append(picker);
    const kind = dictionaryKind, [title, help] = DICTIONARIES[kind], dictionary = data[kind];
    const panel = el("article", "panel list-layout dictionary-layout");
    const left = el("div", "list-left"), right = el("div", "list-right");
    const area = el("textarea", "list-editor");
    area.value = toLines(dictionary.entries);
    let inserted = 0, beforeInsert = null, insertedHighlights = [];
    if (pendingDictionaryInsert?.list === kind) {
      beforeInsert = area.value;
      const result = insertDictionaryLines(area.value, pendingDictionaryInsert);
      area.value = result.text;
      inserted = result.added;
      insertedHighlights = result.highlights;
      pendingDictionaryInsert = null;
    }
    area.dataset.savedValue = area.value;
    area.spellcheck = false;
    area.setAttribute("aria-label", title);
    const fit = () => { area.rows = Math.min(32, Math.max(8, area.value.split("\n").length + 2)); };
    fit(); area.addEventListener("input", fit);
    const save = el("button", "", "Зберегти"), status = el("span", "muted small", `${dictionary.entries.filter(e => e.enabled).length} діючих рядків`);
    const draftEditor = inserted ? highlightDraftEditor(area, insertedHighlights) : null;
    let undoInsert = null;
    if (inserted) {
      status.className = "warn small";
      save.classList.add("needs-save");
      undoInsert = el("button", "ghost", "↶ Прибрати вставлене"); undoInsert.type = "button";
      undoInsert.addEventListener("click", () => {
        area.value = beforeInsert; fit(); draftEditor.clear(); area.dispatchEvent(new Event("input"));
        save.classList.remove("needs-save"); undoInsert.remove();
        status.textContent = "Вставку прибрано — список як був"; status.className = "muted small";
      });
    } else if (beforeInsert !== null) {
      status.textContent = "Запропонований рядок уже є у словнику — нічого не додано";
    }
    save.addEventListener("click", () => action(async () => {
      try {
        await request(`/api/dictionaries/${kind}`, "PUT", { entries: fromLines(area.value), expected_version: dictionary.version });
        note(`Словник «${title}» збережено. Приймач застосує зміни за кілька секунд.`);
        await dictionaries();
      } catch (error) { status.textContent = error.message; status.className = "alert small"; throw error; }
    }));
    const actions = el("div", "list-actions"); actions.append(save, ...(undoInsert ? [undoInsert] : []), status);
    const hints = el("ul", "warn-list");
    liveCheck(area, hints, `/api/dictionaries/${kind}/check`);
    if (inserted) area.dispatchEvent(new Event("input"));
    left.append(draftEditor?.element || area, actions);
    right.append(el("h2", "", title), el("p", "muted small", help), hints);
    panel.append(left, right);
    box.append(panel);
  }

  // Рядки, які «Потребує уваги» просить вставити в список (не зберігаються самі).
  let pendingListInsert = null;
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
    };
  }
  // Позивний аліас додається до свого рядка; шум — окремим рядком. Повтори пропускаються.
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
  function phrasePatternEditor(entries, enableLines = []) {
    const requested = new Set(enableLines.map(line => line.trim()));
    const patterns = entries.map(entry => {
      const savedEnabled = Boolean(entry.enabled);
      return {
        raw_line: entry.raw_line,
        enabled: savedEnabled || requested.has(entry.raw_line.trim()),
        savedRawLine: entry.raw_line.trim(),
        savedEnabled,
      };
    });
    const root = el("div", "phrase-pattern-editor");
    const toolbar = el("div", "pattern-toolbar");
    const search = el("input", "pattern-search");
    search.type = "search"; search.placeholder = "Знайти шаблон"; search.setAttribute("aria-label", "Знайти шаблон");
    const add = el("button", "ghost", "＋ Додати шаблон"); add.type = "button";
    const summary = el("span", "muted small");
    toolbar.append(search, add, summary);
    const activeRows = el("div", "phrase-pattern-rows");
    const disabledGroup = el("details", "phrase-pattern-disabled");
    const disabledSummary = el("summary", "", "Вимкнені");
    const disabledRows = el("div", "phrase-pattern-rows");
    disabledGroup.append(disabledSummary, disabledRows);
    root.append(toolbar, el("p", "pattern-section-label", "Увімкнені"), activeRows, disabledGroup);
    let onChange = () => {};
    const updateSummary = () => {
      const active = patterns.filter(pattern => pattern.enabled).length;
      summary.textContent = `Увімкнено ${active} · вимкнено ${patterns.length - active}`;
      disabledSummary.textContent = `Вимкнені (${patterns.length - active})`;
    };
    const getEntries = () => patterns.map(pattern => ({
      raw_line: pattern.raw_line.trim(), enabled: pattern.enabled,
    })).filter(pattern => pattern.raw_line);
    const draftLabel = pattern => {
      const changed = pattern.raw_line.trim() !== pattern.savedRawLine || pattern.enabled !== pattern.savedEnabled;
      if (!changed) return "";
      return pattern.enabled && !pattern.savedEnabled ? "Тимчасово увімкнено" : "Чернетка";
    };
    const render = () => {
      const query = search.value.trim().toLocaleLowerCase("uk");
      if (query) disabledGroup.open = true;
      const drawRows = (container, enabledState) => {
        container.replaceChildren();
        const visible = patterns.map((pattern, index) => ({ pattern, index })).filter(({ pattern }) =>
        pattern.enabled === enabledState && (!query || pattern.raw_line.toLocaleLowerCase("uk").includes(query)));
        if (!visible.length) {
        const label = query ? "За запитом шаблонів не знайдено."
          : enabledState ? "Увімкнених шаблонів немає." : "Вимкнених шаблонів немає.";
        container.append(el("p", "muted small", label));
        }
        for (const { pattern, index } of visible) {
        const row = el("div", "phrase-pattern-row");
        const toggle = el("label", "switch");
        const enabled = el("input"); enabled.type = "checkbox"; enabled.checked = pattern.enabled;
        toggle.append(enabled, pattern.enabled ? "Увімк." : "Вимк.");
        const badge = el("span", "pattern-draft");
        const updateBadge = () => {
          const label = draftLabel(pattern);
          badge.textContent = label;
          badge.hidden = !label;
          badge.className = `pattern-draft${label === "Тимчасово увімкнено" ? " temporary" : ""}`;
        };
        updateBadge();
        const line = el("input", "pattern-line");
        line.type = "text"; line.value = pattern.raw_line; line.spellcheck = false; line.title = pattern.raw_line;
        line.setAttribute("aria-label", `Текст шаблону ${index + 1}`);
        line.addEventListener("input", () => {
          pattern.raw_line = line.value;
          line.title = line.value;
          updateBadge();
          onChange();
        });
        enabled.addEventListener("change", () => {
          pattern.enabled = enabled.checked;
          if (!pattern.enabled) disabledGroup.open = true;
          render();
          onChange();
        });
        const remove = el("button", "ghost", "×"); remove.type = "button";
        remove.setAttribute("aria-label", `Видалити шаблон ${index + 1}`);
        remove.addEventListener("click", () => {
          patterns.splice(index, 1);
          render();
          onChange();
        });
        row.append(toggle, badge, line, remove);
        container.append(row);
        }
      };
      drawRows(activeRows, true);
      drawRows(disabledRows, false);
      updateSummary();
    };
    search.addEventListener("input", render);
    add.addEventListener("click", () => {
      patterns.push({ raw_line: "", enabled: false, savedRawLine: "", savedEnabled: false });
      search.value = ""; disabledGroup.open = true;
      render();
      onChange();
      const fields = disabledRows.querySelectorAll("input");
      fields[fields.length - 1]?.focus();
    });
    render();
    return {
      element: root,
      rows: root,
      entries: getEntries,
      hasBlankRows: () => patterns.some(pattern => !pattern.raw_line.trim()),
      hasChanges: () => patterns.some(pattern =>
        pattern.raw_line.trim() !== pattern.savedRawLine || pattern.enabled !== pattern.savedEnabled),
      summary: () => {
        const active = patterns.filter(pattern => pattern.enabled).length;
        return `Увімкнено ${active} · вимкнено ${patterns.length - active}`;
      },
      onChange(callback) { onChange = callback; },
    };
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
    for (const [type, [title]] of Object.entries(TEXT_LISTS)) {
      const active = data.lists[type].entries.filter(e => e.enabled).length;
      const b = el("button", type === textListType ? "ghost on" : "ghost", `${title} · ${active}`); b.type = "button";
      b.addEventListener("click", () => {
        textListType = type;
        try { localStorage.setItem("radio345:text-list", type); } catch (_) { /* сесія */ }
        action(texts);
      });
      picker.append(b);
    }
    box.append(picker);
    const type = textListType, [title, help] = TEXT_LISTS[type], list = data.lists[type];
    const panel = el("article", "panel list-layout");
    const left = el("div", "list-left"), right = el("div", "list-right");
    const save = el("button", "", "Зберегти"), status = el("span", "muted small", `${list.entries.filter(e => e.enabled).length} діючих рядків`);
    const patterns = type === "phrase_patterns"
      ? phrasePatternEditor(list.entries, pendingPatternEnable)
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
    let inserted = 0, beforeInsert = null, insertedHighlights = [];
    if (area && pendingListInsert?.list === type) {
      beforeInsert = area.value;
      const result = insertListLines(area.value, pendingListInsert);
      area.value = result.text; inserted = result.added;
      insertedHighlights = result.highlights;
      pendingListInsert = null;
    }
    const fit = () => { if (area) area.rows = Math.min(30, Math.max(8, area.value.split("\n").length + 2)); };
    fit(); area?.addEventListener("input", fit);
    const warnings = el("ul", "warn-list");
    const check = liveCheck(patterns?.rows || area, warnings, `/api/text-lists/${type}/check`, list.warnings || [],
      getEntries, patterns ? [] : ["input"]);
    const draftEditor = inserted ? highlightDraftEditor(area, insertedHighlights) : null;
    if (patterns) patterns.onChange(() => {
      save.classList.add("needs-save");
      save.disabled = patterns.hasBlankRows();
      status.textContent = patterns.hasBlankRows()
        ? `${patterns.summary()} · заповніть або видаліть порожній шаблон`
        : `${patterns.summary()} · є незбережені зміни`;
      status.className = "warn small";
      check();
    });
    if (patterns?.hasChanges()) {
      save.classList.add("needs-save");
      save.disabled = patterns.hasBlankRows();
      check();
    }
    let undoInsert = null;
    if (inserted) {
      status.className = "warn small";
      save.classList.add("needs-save");
      area.dispatchEvent?.(new Event("input"));   // одразу та сама перевірка, що й під час набору
      // Не сподобалось — повернути список, як був до вставки (поки не збережено, ніде не діє).
      undoInsert = el("button", "ghost", "↶ Прибрати вставлене"); undoInsert.type = "button";
      undoInsert.addEventListener("click", () => {
        area.value = beforeInsert; fit(); draftEditor.clear(); area.dispatchEvent?.(new Event("input"));
        save.classList.remove("needs-save"); undoInsert.remove();
        status.textContent = "Вставку прибрано — список як був"; status.className = "muted small";
      });
    }
    save.addEventListener("click", () => action(async () => {
      try {
        const r = await request(`/api/text-lists/${type}`, "PUT", { entries: getEntries(), expected_revision: textRevision });
        textRevision = r.revision;
        note(`«${title}» збережено (ревізія ${r.revision}). Приймач застосує зміни за кілька секунд.`);
        await texts();
      } catch (error) { status.textContent = error.message; status.className = "alert small"; throw error; }
    }));
    const actions = el("div", "list-actions"); actions.append(save, ...(undoInsert ? [undoInsert] : []), status);
    if (area) left.append(draftEditor?.element || area);
    left.append(actions);
    right.append(el("h2", "", title), el("p", "muted small", help), warnings);
    if (EVALUATED_LISTS.includes(type)) {
      const evaluation = listEvaluation(type, getEntries);
      right.append(evaluation);
      // Після вставки — одразу видно, чи текст стане кращим на ваших еталонах. Запуск — коли завершиться
      // відкриття списку: action() не пускає другу дію, поки триває перша.
      if (inserted) { const start = () => (busy ? setTimeout(start, 100) : evaluation.run()); setTimeout(start, 0); }
    }
    if (type === "callsign_aliases") right.append(aliasCandidates(list.entries));
    if (type === "blacklist" || type === "short_garbage") right.append(noiseCandidates(type, list.entries));
    panel.append(left, right);
    box.append(panel);
  }

  // Перевірка списку на розмічених записах: повтор текстової обробки, без збереження й без зупинки приймача.
  const EVALUATED_LISTS = ["phrase_templates", "phrase_patterns", "callsign_aliases", "aliases"];
  function listEvaluation(type, getEntries) {
    const wrap = el("div", "list-evaluation");
    const run = el("button", "ghost", "Перевірити на моїх еталонах"); run.type = "button";
    run.title = "Як змінився б текст розмічених записів із цим списком (зміни не зберігаються)";
    const out = el("div");
    run.addEventListener("click", () => action(async () => {
      run.disabled = true; out.replaceChildren(el("p", "muted small", "Рахую…"));
      try {
        const r = await request(`/api/text-lists/${type}/evaluate`, "PUT", { entries: getEntries() });
        const b = r.before, a = r.after;
        const tone = r.worse ? "bad" : r.better ? "ok" : "";
        const summary = el("div", `summary ${tone}`);
        summary.append(el("b", "", r.changed ? `Змінилося фраз: ${r.changed} — краще ${r.better}, гірше ${r.worse}` : "Жодна фраза не змінилася"),
          el("div", "small muted", `Записів: ${r.records} · помилка в словах ${pct(b.wer)} → ${pct(a.wer)} · позивні ${b.callsigns_hit} → ${a.callsigns_hit} з ${a.callsigns_ref} (хибних ${b.callsigns_false} → ${a.callsigns_false}) · команди ${b.commands_hit} → ${a.commands_hit} з ${a.commands_ref}`));
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
      } catch (error) { out.replaceChildren(el("p", "alert small", error.message)); throw error; }
      finally { run.disabled = false; }
    }));
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
      const data = await request("/api/text-lists/callsign-alias-candidates");
      out.replaceChildren();
      if (!data.items.length) { out.append(el("p", "muted small", `Розмічених записів мовлення: ${data.records}. Кандидатів немає.`)); return; }
      out.append(el("p", "muted small", `З ${data.records} розмічених записів: слово Whisper перед тим самим номером, що й позивний у правильному тексті. `
        + "Додавайте лише варіанти, які не можуть бути звичайним словом."));
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

  async function settings() {
    current = await request("/api/summary");
    $("models").textContent = `Whisper: ${current.settings.whisper_model} · Qwen: ${current.settings.correction_model}`;
    $("fields").replaceChildren();
    for (const [key, value] of Object.entries(current.settings.values)) {
      const label = el("label", typeof value === "boolean" ? "inline" : "", LABELS[key] || key), input = el("input");
      input.id = `setting-${key}`; input.type = typeof value === "boolean" ? "checkbox" : "number";
      if (input.type === "checkbox") { input.checked = value; label.prepend(input); }
      else { input.value = value; input.step = "1"; input.required = true; label.append(input); }
      const fallback = current.settings.defaults?.[key];
      if (fallback !== undefined) {
        const changed = value !== fallback;
        label.classList.toggle("changed", changed);
        label.append(el("span", "muted small", `${changed ? "змінено · " : ""}типово ${typeof fallback === "boolean" ? (fallback ? "так" : "ні") : fallback}`));
      }
      $("fields").append(label);
    }
  }
  // ---------- Обробка без перезапуску ----------
  const RUNTIME_LABELS = {
    COOLDOWN_S: "Пауза між однаковими повідомленнями, с",
    LOG_CALLSIGNS_ONLY: "Записувати події з позивними навіть без команд",
    GARBAGE_MIN_WORDS: "Перевіряти фрази від N слів",
    GARBAGE_MIN_UNIQUE_RATIO: "Мінімальна частка різних слів (0–1)",
    GARBAGE_MAX_WORDS_PER_S: "Максимум слів за секунду аудіо",
    GARBAGE_EXTRA_WORDS: "Запас слів понад цю норму",
    LOW_CONFIDENCE_MAX_WORDS: "Коротка невпевнена фраза: до N слів (0 — вимкнено)",
    LOW_CONFIDENCE_LOGPROB: "…і впевненість Whisper нижча за (avg_logprob)",
    MIN_RMS: "Мінімальна гучність сегмента (RMS)",
    WHISPER_RETRY_WITHOUT_PROMPT: "Другий шанс: зациклився з підказкою — ще раз без неї",
    WHISPER_PROMPT_DAILY_CODE: "Останній код доби в підказці Whisper",
    SPECTRAL_NOTIFICATION_MODE: "Режим",
    SPECTRAL_NOTIFICATION_THRESHOLD: "Поріг схожості на зразок",
    SPECTRAL_NOTIFICATION_MAX_OUTSIDE_S: "Звуку поза збігом не більше, с",
    SPECTRAL_NOTIFICATION_MAX_EXTRA: "Сторонньої енергії не більше (0–1)",
  };
  const RUNTIME_GROUPS = [
    ["Сповіщення", ["COOLDOWN_S", "LOG_CALLSIGNS_ONLY"]],
    ["Whisper", ["WHISPER_RETRY_WITHOUT_PROMPT", "WHISPER_PROMPT_DAILY_CODE", "MIN_RMS"]],
    ["Відсів сміття й невпевнених фраз", ["GARBAGE_MIN_WORDS", "GARBAGE_MIN_UNIQUE_RATIO", "GARBAGE_MAX_WORDS_PER_S", "GARBAGE_EXTRA_WORDS",
                                          "LOW_CONFIDENCE_MAX_WORDS", "LOW_CONFIDENCE_LOGPROB"]],
    ["Фільтр сигналів сповіщень", ["SPECTRAL_NOTIFICATION_MODE", "SPECTRAL_NOTIFICATION_THRESHOLD", "SPECTRAL_NOTIFICATION_MAX_OUTSIDE_S",
                                   "SPECTRAL_NOTIFICATION_MAX_EXTRA"]],
  ];
  const RUNTIME_CHOICES = { off: "вимкнено", shadow: "лише оцінювати", active: "відкидати сигнали" };
  let runtimeCurrent = null;
  async function runtimeSettings() {
    runtimeCurrent = await request("/api/runtime-settings");
    const a = runtimeCurrent.applied || {}, live = a.receiver === "listening" || a.receiver === "paused";
    $("runtime-status").textContent = `Збережена ревізія: ${runtimeCurrent.revision}. ` + (!live
      ? "Приймач не працює — значення застосуються під час запуску."
      : a.revision === runtimeCurrent.revision ? "Приймач уже використовує ці значення."
      : `Приймач: ревізія ${a.revision ?? "—"} (знімок метрик оновлюється ~30 с; самі значення діють за кілька секунд).`);
    $("runtime-fields").replaceChildren();
    const shown = new Set();
    const groups = [...RUNTIME_GROUPS, ["Інше", Object.keys(runtimeCurrent.values).filter(k => !RUNTIME_GROUPS.some(([, keys]) => keys.includes(k)))]];
    for (const [title, keys] of groups) {
      const present = keys.filter(k => k in runtimeCurrent.values && !shown.has(k));
      if (!present.length) continue;
      const box = el("fieldset", "runtime-group"); box.append(el("legend", "", title));
      for (const key of present) { shown.add(key); box.append(runtimeField(key)); }
      $("runtime-fields").append(box);
    }
  }
  const showValue = (value, limits) => limits.type === "str" ? RUNTIME_CHOICES[value] || value
    : limits.type === "bool" ? (value ? "так" : "ні") : String(value).replace(".", ",");
  // Поле налаштування: змінене відносно config.py — підсвічене, «↺» повертає значення за замовчуванням.
  function runtimeField(key) {
    const value = runtimeCurrent.values[key], limits = runtimeCurrent.limits[key], fallback = runtimeCurrent.defaults[key];
    const label = el("label", limits.type === "bool" ? "inline" : "", RUNTIME_LABELS[key] || key);
    let input;
    if (limits.type === "str") {
      input = el("select");
      for (const choice of limits.choices) { const o = el("option", "", RUNTIME_CHOICES[choice] || choice); o.value = choice; input.append(o); }
      input.value = value; label.append(input);
    } else {
      input = el("input");
      if (limits.type === "bool") { input.type = "checkbox"; input.checked = value; label.prepend(input); }
      else {
        input.type = "number"; input.value = value; input.required = true;
        input.min = limits.min; input.max = limits.max; input.step = limits.type === "int" ? "1" : "0.01";
        label.append(input);
      }
    }
    input.id = `runtime-${key}`;
    const changed = value !== fallback;
    label.classList.toggle("changed", changed);
    const info = el("span", "muted small", `${changed ? "змінено · " : ""}типово ${showValue(fallback, limits)}` +
      (limits.type === "int" || limits.type === "float" ? ` · від ${limits.min} до ${limits.max}` : ""));
    if (changed) {
      const reset = el("button", "ghost small-button", "↺"); reset.type = "button"; reset.title = "Повернути значення з config.py";
      reset.addEventListener("click", () => {
        if (limits.type === "bool") input.checked = fallback; else input.value = fallback;
        note("Повернуто значення за замовчуванням — натисніть «Зберегти».");
      });
      info.append(" ", reset);
    }
    label.append(info);
    return label;
  }
  $("runtime-settings").addEventListener("submit", e => { e.preventDefault(); action(async () => {
    const values = {};
    for (const [key, limits] of Object.entries(runtimeCurrent.limits)) {
      const input = $(`runtime-${key}`);
      values[key] = limits.type === "bool" ? input.checked : limits.type === "str" ? input.value
        : limits.type === "int" ? parseInt(input.value, 10) : Number(input.value);
    }
    const r = await request("/api/runtime-settings", "PUT", { values, expected_revision: runtimeCurrent.revision });
    await runtimeSettings();
    note(`Збережено (ревізія ${r.revision}). Приймач застосує без перезапуску за кілька секунд.`);
  }); });

  $("settings").addEventListener("submit", e => { e.preventDefault(); action(async () => {
    if (!confirm("Зберегти? Налаштування застосуються після перезапуску приймача.")) return;
    const values = {};
    for (const key of Object.keys(current.settings.values)) { const i = $(`setting-${key}`); values[key] = i.type === "checkbox" ? i.checked : Number(i.value); }
    await request("/api/settings", "PUT", { values, expected_revision: current.settings.revision });
    await settings(); note("Збережено. Приймач не перезапускався.");
  }); });

