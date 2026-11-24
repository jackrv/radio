  // ---------- Пауза приймача на час: «живий» повзунок ----------
  // Потягнули й відпустили — пауза на стільки хвилин; на паузі повзунок сам повзе до нуля.
  // Стягнути в 0 — відновити приймання; пересунути — нова тривалість.
  let pauseState = null, pauseDeadline = 0, dragging = false;
  let pauseMinutes = (() => { try { return Number(localStorage.getItem("radio345:pause-minutes")) || 15; } catch (_) { return 15; } })();
  const range = $("pause-range");
  const snap = value => Math.round(value);            // хвилина — найменший крок
  function fillPause(minutes, active) {
    range.style.setProperty("--fill", `${Math.min(100, (minutes / 60) * 100)}%`);
    range.classList.toggle("active", active);
    $("pause-label").classList.toggle("active", active);
  }
  function paintPause() {
    if (dragging) return;
    const left = Math.max(0, (pauseDeadline - Date.now()) / 1000);
    const on = pauseState?.pause_requested && left > 0;
    range.value = on ? String(left / 60) : "0";
    fillPause(on ? left / 60 : 0, !!on);
    $("pause-label").textContent = on ? `ще ${Math.floor(left / 60)}:${String(Math.floor(left % 60)).padStart(2, "0")}` : "потягніть →";
  }
  async function pauseRefresh() {
    try { pauseState = await request("/api/receiver-pause"); } catch (_) { return; }
    pauseDeadline = Date.now() + (pauseState.remaining_seconds || 0) * 1000;
    paintPause();
  }
  async function setPause(minutes) {
    if (!pauseState) await pauseRefresh();
    await request("/api/receiver-pause", "PUT", { minutes, expected_revision: pauseState.revision });
    if (minutes) {
      pauseMinutes = minutes;
      try { localStorage.setItem("radio345:pause-minutes", String(minutes)); } catch (_) { /* сесія */ }
    }
    await pauseRefresh();
    note(minutes ? `Мікрофон закрито на ${minutes} хв.` : "Приймання відновлено.");
  }
  range.addEventListener("pointerdown", () => { dragging = true; });
  range.addEventListener("input", () => {
    dragging = true;
    const minutes = snap(Number(range.value));
    fillPause(minutes, minutes > 0);
    $("pause-label").textContent = minutes ? `${minutes} хв` : (pauseState?.pause_requested ? "відновити" : "потягніть →");
  });
  range.addEventListener("change", () => {
    const minutes = snap(Number(range.value));
    dragging = false;
    const on = pauseState?.pause_requested;
    if (!minutes && !on) { paintPause(); return; }
    action(() => setPause(minutes)).finally?.(paintPause);
  });
  pauseRefresh();
  setInterval(paintPause, 1000);
  setInterval(pauseRefresh, 15000);
  // Плашка стану вгорі — перемикач без зайвих питань: пауза ↔ приймання.
  // Поки команда виконується, плашка показує дію («Відновлюю…») і не реагує на повторні натискання;
  // знімається, щойно приймач прийшов у потрібний стан, або через ліміт часу.
  function pendPill(text, target, seconds) {
    pillPending = { text, target, until: Date.now() + seconds * 1000 };
    paintPill(data?.receiver);
    clearInterval(pillPoll);
    pillPoll = setInterval(() => { if (!pillPending) clearInterval(pillPoll); else overview().catch(() => {}); }, 1500);
  }
  $("receiver-pill").addEventListener("click", () => {
    if (pillPending) return;
    const state = data?.receiver;
    // Реакція одразу, ще до відповіді сервера.
    pendPill(state === "listening" ? "Ставлю на паузу…" : state === "paused" ? "Відновлюю…" : "Перевіряю…", null, 15);
    action(async () => { try {
      if (state === "stopped" || state === "offline") {
        pillPending = null; await overview();
        note("Приймач не працює. Запустіть служби: ./r345 up (на Pi — systemctl)."); return;
      }
      await pauseRefresh();
      const resume = pauseState?.pause_requested;
      pendPill(resume ? "Відновлюю…" : "Ставлю на паузу…", resume ? "listening" : "paused", 20);
      if (pauseState) await setPause(resume ? 0 : pauseMinutes);
      await overview();
    } catch (error) { pillPending = null; clearInterval(pillPoll); paintPill(data?.receiver); throw error; } }).finally?.(() => { if (pillPending && !pillPending.target) { pillPending = null; paintPill(data?.receiver); } });
  });

  // ---------- Пошук у шапці ----------
  $("global-search").addEventListener("keydown", event => {
    if (event.key !== "Enter" || !event.target.value.trim()) return;
    event.preventDefault();
    openRecordings({search: event.target.value.trim().slice(0, 200)});
    event.target.value = "";
  });
  // Перехід в архів з чітко заданими фільтрами: попередній пошук чи вид не ховають потрібне.
  function openRecordings({view: kind = "all", period = "", search = ""} = {}) {
    if (typeof archiveSelection !== 'undefined') archiveSelection = '';
    $("recording-view").value = kind; $("recording-period").value = period;
    $("recording-search").value = search;
    if (loaded.has("recordings")) { show("recordings"); action(() => recordings(true)); } else show("recordings");
  }

  // ---------- Огляд: що потребує уваги ----------
  // «texts:callsign_aliases» — вкладка «Фільтри тексту» з потрібним списком; інакше — назва вкладки.
  function openTarget(target) {
    const [view, list] = target.split(":");
    if (view === "recordings" && list) {
      // Записи для перевірки — пошуком за номерами в усьому архіві, без інших фільтрів.
      openRecordings({search: list.split(",").join(", ")});
      return;
    }
    if (view === "texts" && list) {
      textListType = list;
      try { localStorage.setItem("radio345:text-list", list); } catch (_) { /* сесія */ }
      loaded.delete("texts");
      show("texts");
      return;
    }
    if (view === "dictionaries" && list) {
      dictionaryKind = list;
      try { localStorage.setItem("radio345:dictionary", list); } catch (_) { /* сесія */ }
      loaded.delete("dictionaries");
      show("dictionaries");
      return;
    }
    show(view);
  }
  function suggestionReviewOptions(g) {
    // Одна група «noise» містить два законні призначення. Не змішувати їх
    // в одному редакторі й не вимагати refresh для незмінного формату сервера.
    const names = {short_garbage: "Переглянути шум", blacklist: "Переглянути галюцинації"};
    const lines = g.lines || [];
    const lists = Object.keys(names).filter(type => lines.some(line => line.list === type));
    if (g.kind !== "noise" || lists.length < 2 || lines.some(line => !names[line.list])) {
      return [{title: "Переглянути", suggestion: g}];
    }
    return lists.map(type => {
      const selected = lines.filter(line => line.list === type);
      return {title: `${names[type]} (${selected.length})`,
              suggestion: {...g, target: `texts:${type}`, lines: selected}};
    });
  }
  let suggestionReviewPending = false;
  async function reviewSuggestion(g) {
    if (suggestionReviewPending) return;
    suggestionReviewPending = true;
    try {
      if (g.kind === "pattern" || g.target === "texts:phrase_patterns") {
        const lines = (g.lines || []).filter(line => line.list === "phrase_patterns").map(line => line.line);
        if (!lines.length) throw new Error("У пропозиції немає рядка шаблону. Оновіть «Потребує уваги» й спробуйте ще раз.");
        pendingPatternEnable = lines;
        openTarget("texts:phrase_patterns");
        return;
      }
      if (g.lines?.length) {
        const list = g.lines[0].list;
        if (!list || g.lines.some(line => line.list !== list)) {
          throw new Error("У пропозиції є рядки для різних списків. Оновіть «Потребує уваги» й спробуйте ще раз.");
        }
        if (g.target === "dictionaries") {
          if (list !== "callsigns" || g.lines.some(line => !line.callsign || !line.number)) {
            throw new Error("У пропозиції немає рядка словника позивних. Оновіть «Потребує уваги» й спробуйте ще раз.");
          }
          pendingDictionaryInsert = { list, lines: g.lines };
          openTarget(`dictionaries:${list}`);
          return;
        }
        pendingListInsert = { list, lines: g.lines };
        openTarget(`texts:${list}`);
        return;
      }
      openTarget(g.target);
    } catch (error) {
      note(error.message || String(error));
    } finally {
      suggestionReviewPending = false;
    }
  }
  let attentionAt = 0;
  let suggestionTask = null;
  function suggestionPanel() {
    if (!suggestionTask) suggestionTask = serverJobPanel({button: $("suggestions-refresh"), box: $("suggestions-check-result"), kind: "suggestions",
      start: () => request("/api/suggestions/refresh", "POST"), render: r => {
        $("suggestions-check-result").replaceChildren(el("p", "small", `Перевірено: пропозицій ${r.total}, нових ${r.new}.`));
        attention(true);
        if (typeof attentionSettings === "function") attentionSettings(true);
      }});
    return suggestionTask;
  }
  $("suggestions-refresh").addEventListener("click", () => suggestionPanel().run());
  let syncTask = null;
  function syncEventsJob() {
    if (!syncTask) syncTask = serverJobPanel({button: el("button"), box: $("sync-events-result"), kind:"sync_events",
      start: () => request("/api/suggestions/sync-events", "POST"), render: r => {
        $("sync-events-result").replaceChildren(el("p", "small", `Події: оновлено ${r.updated}, без змін ${r.same}, видалено ${r.hidden}, помилок ${r.failed}.`));
        attention(true);
      }});
    return syncTask;
  }
  let attentionRetry = null;
  async function attention(force = false) {
    const interval = view === "overview" ? 15000 : 60000;
    if (!force && Date.now() - attentionAt < interval) return;
    attentionAt = Date.now();
    let a;
    try { a = await request("/api/attention"); } catch (_) { return; }
    // Після правки перевірка йде у фоні кілька секунд — тоді оновлюємо список ще раз.
    clearTimeout(attentionRetry);
    // На вкладці «Аудіоархів»: ефір за 24 год, ще не розмічений (шум, кімнату й інше мовлення не рахуємо).
    {
      const todo = $("archive-todo-nav"), count = a.unlabeled_radio_24h || 0;
      todo.hidden = !count; todo.textContent = count > 99 ? "99+" : String(count);
      todo.title = `Ефір за останні 24 год, ще не розмічено: ${count}`;
    }
    if (view !== "overview") return;
    if (a.suggestions_pending && view === "overview") attentionRetry = setTimeout(() => attention(true), 6000);
    const items = [];
    const item = (tone, text, label, go, extra, hint) => {
      const row = el("div", `attention-item ${tone}`);
      const body = el("span", "attention-text", text);
      if (hint) body.append(el("small", "attention-hint", hint));
      row.append(body);
      const buttons = el("span", "attention-actions");
      for (const [title, handler] of [[label, go], ...(extra || [])]) {
        if (!title) continue;
        const b = el("button", "ghost", title); b.type = "button"; b.addEventListener("click", handler); buttons.append(b);
      }
      row.append(buttons);
      items.push(row);
    };
    if (a.lab_running) item("warn", "Іде черга тестів лабораторії — приймач на цей час зупинено, ефір не слухається.", "Переглянути", () => show("experiments"));
    if (a.codes_missing) item("warn", `Не внесено коди доби ${a.codes_day.split("-").reverse().slice(0, 2).join(".")} (діють з 20:00 попереднього дня).`, "Внести", () => show("codes"));
    // «Переглянути» відкриває «Навчання» одразу з потрібним фільтром.
    const openLearning = filter => () => {
      try { localStorage.setItem("radio345:learning-filter", filter); } catch (_) { /* сесія */ }
      $("learning-filter").value = filter;
      show("learning");   // «Навчання» щоразу перезавантажується (ALWAYS_RELOAD)
    };
    // Дозволи й конфлікти навчання — звичайні групи пропозицій нижче: з «Готово» й «Не перевіряти».
    if (a.telegram_failed) item("bad", `Правок, що не дійшли в Telegram після всіх спроб: ${a.telegram_failed}.`, "Система", () => show("system"));
    // Пропозиції з розмітки, що накопичилися в усіх списках (фонова перевірка панелі).
    for (const g of a.suggestions || []) {
      const fresh = g.new ? ` · нових ${g.new}` : "";
      const reviews = suggestionReviewOptions(g);
      const extra = reviews.slice(1).map(option => [option.title, () => { void reviewSuggestion(option.suggestion); }]);
      if (g.kind === "ref_event") extra.push(["Оновити всі", () => syncEventsJob().run()]);
      extra.push(["✓ Готово", () => action(async () => {
        const r = await request("/api/suggestions/dismiss", "POST", {kind: g.kind});
        note(`Позначено як переглянуті: ${r.dismissed}. Повернути можна в «Система» → «Перевірки».`);
        await attention(true);
      })]);
      extra.push(["Не перевіряти", () => action(async () => {
        await request("/api/suggestions/mute", "POST", {kind: g.kind, muted: true});
        note(`«${g.title}» більше не перевіряється. Увімкнути — у «Система» → «Перевірки».`);
        await attention(true);
      })]);
      const learningFilter = {approval: "needs_approval", conflict: "conflict"}[g.kind];
      const open = learningFilter ? openLearning(learningFilter) : () => { void reviewSuggestion(reviews[0].suggestion); };
      item(g.new || learningFilter ? "warn" : "info", `${g.title}: ${g.count}${fresh} — ${g.examples.join("; ")}${g.count > g.examples.length ? "…" : ""}`,
        reviews[0].title, open, extra, g.hint);
    }
    // Нерозмічений ефір за добу — плиткою «Ефір без розмітки» на робочому столі, тут не дублюємо.
    // Порожній розділ не показуємо: історія рішень і вимкнені перевірки — у «Система».
    $("attention").replaceChildren(...items);
    $("attention-panel").hidden = !items.length;
  }

  // ---------- Огляд ----------
  function card(label, value, detail, tone) {
    const c = el("div", `card ${tone || ""}`);
    c.append(el("div", "label", label), el("div", "value", value), el("div", "detail", detail || ""));
    return c;
  }
  // Картка Огляду веде на сторінку з подробицями.
  function linked(c, go, title) {
    c.classList.add("clickable");
    c.tabIndex = 0; c.setAttribute("role", "button"); c.title = title;
    c.addEventListener("click", go);
    c.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); go(); } });
    return c;
  }
  function receiverTone(state) { return state === "listening" ? "ok" : state === "paused" ? "warn" : "bad"; }

  // Стан служб — один рядок; проблеми не дублюємо окремими плитками.
  function renderServices(services) {
    const problems = services.filter(s => s[3] === "warn" || s[3] === "bad");
    const line = [el("span", problems.length ? "health-sum warn" : "health-sum ok",
                     problems.length ? `⚠ Потребує уваги: ${problems.map(s => s[0]).join(", ")}` : "✓ Усі служби працюють")];
    for (const [label, value, detail, tone, go, title] of services) {
      const item = el("button", `health-item ${tone || ""}`); item.type = "button";
      item.append(el("span", "health-label", label), el("b", "", value));
      if ((tone === 'warn' || tone === 'bad') && detail) item.append(el('small','health-detail',detail));
      item.title = `${detail ? `${detail}. ` : ""}${title}`;
      item.addEventListener("click", go);
      line.push(item);
    }
    $("health-line").replaceChildren(...line);
    $("cards").replaceChildren();
  }

  // ---------- Робочий стіл: якість сьогодні, черги розмітки, коди доби ----------
  let workdeskAt = 0;
  const localDay = (shift = 0) => { const d = new Date(); d.setDate(d.getDate() + shift);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
  async function workdesk(force = false) {
    if (!force && Date.now() - workdeskAt < 60000) return;
    workdeskAt = Date.now();
    let w;
    try { w = await request("/api/workdesk"); } catch (_) { return; }
    const share = v => v == null ? "—" : `${Math.round(v * 100)} %`;
    const tile = (label, value, extra, tone, go, title) => {
      const box = el("button", `stat-tile ${tone || ""}`.trim()); box.type = "button";
      box.append(el("span", "stat-label", label), el("span", "stat-value", value));
      if (extra) box.append(el("span", "stat-extra", extra));
      box.title = title; box.addEventListener("click", go);
      return box;
    };
    const byDay = Object.fromEntries((w.quality || []).map(d => [d.day, d]));
    const today = byDay[localDay()], yesterday = byDay[localDay(-1)];
    const goal = tile("Без правок сьогодні", share(today?.no_edits_share),
                      [today ? `${num(today.no_edits)} з ${num(today.records)}` : "ще не розмічено", yesterday ? `вчора ${share(yesterday.no_edits_share)}` : ""].filter(Boolean).join(" · "),
                      "goal", () => show("quality"), "Фрази сьогоднішнього ефіру, де текст збігся з еталоном слово в слово. Клік — Аналіз");
    if (today) { const bar = el("span", "stat-bar"), fill = el("i"); fill.style.setProperty("--share", `${(today.no_edits_share * 100).toFixed(1)}%`); bar.append(fill); goal.append(bar); }
    const q = w.queue || {};
    const queue = (label, count, extra, view, period, title) =>
      tile(label, num(count), extra, count ? "queue" : "compact", () => openRecordings({view, period}), title);
    const day = w.codes?.day || "", dayLabel = day ? `${day.slice(8, 10)}.${day.slice(5, 7)}` : "";
    const codes = w.codes?.codes?.length
      ? tile(`Коди доби ${dayLabel}`, w.codes.codes.join(" · "), `до ${w.codes.period?.[1] || ""}`, "codes", () => show("codes"), "Коди поточної доби (змінюються о 20:00). Клік — сторінка кодів")
      : tile(`Коди доби ${dayLabel}`, "не внесено", "внести →", "codes missing", () => show("codes"), "Коди поточної доби ще не внесено");
    $("workdesk").replaceChildren(goal,
      queue("Ефір без розмітки", q.radio_24h, "за 24 год", "radio", "24", "Записи з позивним чи подією за добу, ще не розмічені"),
      queue("Без ролей", q.no_roles, "мовлення", "no_roles", "", "Розмічене мовлення без «Кому / Від»"),
      queue("Хибний текст", q.false_text, "на шумі / сигналі", "false_text", "", "Програма дала текст там, де мовлення немає"),
      queue("Пропущене", q.missed, "мовлення", "missed", "", "Мовлення, з якого тексту не вийшло"),
      codes);
    drawQualityMini(w.quality || []);
    fitOverview();   // робочий стіл змінив висоту над стрічкою
  }
  // Якість за 7 днів: «Без правок» смугою, поруч пропущене й помилка в словах.
  function drawQualityMini(days) {
    const box = $("quality-mini");
    if (!days.length) { box.replaceChildren(el("p", "muted", "Розмічених записів мовлення за тиждень немає.")); return; }
    const pct0 = v => v == null ? "—" : `${Math.round(v * 100)} %`;
    const rows = [...days].reverse().map(d => {
      const row = el("div", d.day === localDay() ? "quality-row today" : "quality-row");
      const track = el("span", "quality-track"), fill = el("i");
      fill.style.width = `${Math.max(2, d.no_edits_share * 100)}%`; track.append(fill);
      row.append(el("span", "quality-day", d.day === localDay() ? "сьогодні" : `${d.day.slice(8, 10)}.${d.day.slice(5, 7)}`), track,
                 el("b", "", pct0(d.no_edits_share)), el("span", "quality-meta", `${num(d.records)} фраз · пропущено ${pct0(d.missed_share)} · WER ${pct0(d.wer_final)}`));
      row.title = `Без правок: ${num(d.no_edits)} з ${num(d.records)}`;
      return row;
    });
    box.replaceChildren(el("div", "quality-head", "Без правок"), ...rows);
  }
  $("quality-open")?.addEventListener("click", () => show("quality"));

  function headerAlert(text, title = "") {
    const box = $("updated");
    box.textContent = text;
    box.title = text ? title : "";
    box.hidden = !text;
  }
  let pillPending = null, pillPoll = 0;
  function paintPill(state) {
    const pill = $("receiver-pill");
    if (pillPending && (state === pillPending.target || Date.now() > pillPending.until)) {
      if (state !== pillPending.target) note("Приймач поки не відповів — стан оновиться сам.");
      pillPending = null; clearInterval(pillPoll);
    }
    pill.disabled = Boolean(pillPending);
    pill.setAttribute?.("aria-busy", String(Boolean(pillPending)));
    if (pillPending) { pill.className = "pill pill-button busy"; pill.textContent = pillPending.text; return; }
    pill.className = `pill pill-button ${receiverTone(state)}`;
    pill.textContent = { listening: "Слухає ефір", paused: "Призупинено", stopped: "Зупинено", offline: "Не працює" }[state] || state || "—";
  }
  async function overview() {
    data = await request("/api/overview");
    const d = data.diagnostics || {}, c = d.counters || {}, g = d.gauges || {}, o = data.overview;
    const state = data.receiver;
    const pill = $("receiver-pill");
    paintPill(state);
    const age = d.updated_at ? now() - d.updated_at : null;
    // Час оновлення — у підказці кнопки; плашка в шапці лише коли є проблема.
    pill.title = `Натисніть, щоб поставити приймач на паузу чи відновити\nОновлено ${new Date().toLocaleTimeString("uk-UA")}`;
    const stale = (state === "listening" || state === "paused") && age !== null && age > 120;
    headerAlert(stale ? `Діагностика застаріла · ${ago(age)}` : "",
      "Приймач працює, але його діагностика не оновлювалась понад 2 хв. Подробиці — у «Система».");
    attention();
    if (view !== "overview") return;
    if (typeof suggestionPanel === "function" && !suggestionTask) suggestionPanel().resume();
    if (typeof syncEventsJob === "function" && !syncTask) syncEventsJob().resume();

    // Стан потоків беремо зі знімка лише тоді, коли він свіжий і приймач працює.
    const fresh = age !== null && age <= 120 && (state === "listening" || state === "paused");
    const health = !fresh ? "unknown" : g["asr.health"] || (g["workers.transcriber_alive"] === false ? "failing" : "ok");
    const models = [g["models.whisper"], g["models.qwen"]].filter(Boolean);
    const lastBackup = data.backups[0];
    const backupAge = lastBackup ? now() - lastBackup.created : null;
    const sync = data.telegram_sync || {};
    // Стан служб: [назва, значення, подробиці, тон, куди веде, підказка].
    const services = [
      ["Приймач", pill.textContent, d.started_at ? `сесія ${ago(now() - d.started_at)}` : "немає даних сесії", receiverTone(state), () => show("system"), "Система: моделі, потоки, діагностика"],
      ["Розпізнавання", { ok: "Працює", stuck: "Зависло", failing: "Помилки", unknown: "Немає даних" }[health] || health,
        fresh ? (models.length ? `моделі: ${models.join(" / ")}` : "") : "приймач не надсилає свіжих метрик",
        health === "ok" ? "ok" : health === "unknown" ? "" : "bad", () => show("settings"), "Налаштування розпізнавання"],
      ["Метрики", age === null ? "—" : `${ago(age)} тому`, "оновлюється кожні ~30 с",
        age === null ? "" : age > 120 ? "warn" : "ok", () => show("sessions"), "Система: історія діагностики"],
      ["Telegram", !fresh ? "Немає даних" : g["workers.telegram_alive"] === false ? "Потік зупинено" : "Працює",
        `правок у черзі: ${num(sync.pending)}${sync.exhausted ? ` · не вдалося: ${sync.exhausted}` : ""}`,
        !fresh ? (sync.exhausted ? "bad" : "") : g["workers.telegram_alive"] === false || sync.exhausted ? "bad" : sync.pending ? "warn" : "ok", () => show("system"), "Система: Telegram"],
      ["Копія бази", backupAge === null ? "Немає" : `${ago(backupAge)} тому`, `${data.backups.length} автоматичних`,
        backupAge === null ? "bad" : backupAge > 30 * 3600 ? "warn" : "ok", () => show("storage"), "Система: резервні копії"],
      ["Сигнали", !data.samples ? "Немає зразків" : SPECTRAL_MODES[data.spectral?.mode] || "Спектральний",
        `зразків: ${data.samples} · відкинуто: ${num(c["asr.outcome.notification"] || 0)} · кандидатів: ${num(c["asr.spectral.candidate"] || 0)}`,
        data.samples ? (data.spectral?.mode === "active" ? "ok" : "info") : "warn", () => show("filter"), "Сигнали: фільтр"],
    ];
    renderServices(services);
    workdesk();
    drawEvents(data.events_by_hour);
    drawFunnel(c);
    if (view === "overview") await live();
    drawLatency(d.asr_latency?.ready || {});
    drawHealth(c, g);
    fitOverview();

  }

  // Широкий екран: Огляд — точно у висоту вікна, як архів; прокручуються лише стрічка й ліва колонка.
  function fitOverview() {
    if (typeof document === "undefined" || !document.querySelector) return;
    const split = document.querySelector(".overview-split");
    if (!split) return;
    if (!window.matchMedia?.("(min-width: 1100px)").matches || split.offsetParent === null) { split.style.removeProperty("--overview-h"); return; }
    const top = split.getBoundingClientRect().top + pageScrollY();
    let height = Math.max(420, Math.floor(window.innerHeight - top - 10));
    split.style.setProperty("--overview-h", `${height}px`);
    const extra = document.querySelector('main').scrollHeight - document.querySelector('main').clientHeight;
    if (extra > 0) { height = Math.max(420, height - extra); split.style.setProperty("--overview-h", `${height}px`); }
  }
  window.addEventListener?.("resize", () => { if (view === "overview") fitOverview(); else if (view === "quality") fitAnalysis(); else if (view === "experiments") fitLab(); });
  // «Аналіз → Експерименти»: таблиця результатів — до низу вікна, прокручується сама.
  function fitLab() {
    if (typeof document === "undefined" || !document.querySelector) return;
    const panel = document.querySelector(".lab-results");
    if (!panel) return;
    if (!window.matchMedia?.("(min-width: 1100px)").matches || panel.offsetParent === null) { panel.style.removeProperty("--lab-h"); return; }
    const top = panel.getBoundingClientRect().top + pageScrollY();
    let height = Math.max(320, Math.floor(window.innerHeight - top - 10));
    panel.style.setProperty("--lab-h", `${height}px`);
    const extra = document.querySelector('main').scrollHeight - document.querySelector('main').clientHeight;
    if (extra > 0) { height = Math.max(320, height - extra); panel.style.setProperty("--lab-h", `${height}px`); }
  }
  // Те саме для «Аналіз → Якість»: колонки прокручуються всередині, сторінка — ні.
  function fitAnalysis() {
    if (typeof document === "undefined" || !document.querySelector) return;
    const split = document.querySelector(".analysis-split");
    if (!split) return;
    if (!window.matchMedia?.("(min-width: 1100px)").matches || split.offsetParent === null) { split.style.removeProperty("--analysis-h"); return; }
    split.style.removeProperty("--analysis-h");
  }
  function drawEvents(series) {
    const counts = series.counts, max = Math.max(1, ...counts), W = 720, H = 180, pad = 22, bw = (W - pad) / counts.length;
    const chart = svg("svg", { viewBox: `0 0 ${W} ${H + 20}`, role: "img", "aria-label": "Події за годинами" });
    counts.forEach((value, i) => {
      const h = value ? Math.max(3, (value / max) * (H - 10)) : 0;
      const bar = svg("rect", { class: "bar", x: pad + i * bw + 2, y: H - h, width: Math.max(2, bw - 4), height: h, rx: 3 });
      const hour = new Date((series.start + i * 3600) * 1000).getHours();
      bar.append(svg("title")); bar.firstChild.textContent = `${String(hour).padStart(2, "0")}:00 — ${value}`;
      chart.append(bar);
      if (i % 3 === 0) { const t = svg("text", { class: "axis", x: pad + i * bw + bw / 2, y: H + 15, "text-anchor": "middle" }); t.textContent = String(hour).padStart(2, "0"); chart.append(t); }
    });
    const top = svg("text", { class: "axis", x: 0, y: 10 }); top.textContent = max; chart.append(top);
    $("events-chart").replaceChildren(chart);
    $("events-total").textContent = `усього ${counts.reduce((a, b) => a + b, 0)}`;
  }

  function drawFunnel(counters) {
    const rows = Object.entries(counters).filter(([k]) => k.startsWith("asr.outcome.")).map(([k, v]) => [k.slice(12), v]);
    if (counters["vad.short_discarded"]) rows.push(["short", counters["vad.short_discarded"]]);
    rows.sort((a, b) => (a[0] === "ready" ? -1 : b[0] === "ready" ? 1 : b[1] - a[1]));
    const total = rows.reduce((s, [, v]) => s + v, 0) || 1;
    const box = $("funnel"); box.replaceChildren();
    if (!rows.length) { box.append(el("p", "muted", "Сегментів у цьому запуску ще немає.")); return; }
    for (const [status, value] of rows) {
      const row = el("div", "funnel-row"), track = el("div", "funnel-track"), fill = el("div", `funnel-fill st-${status}`);
      fill.style.width = `${(value / total) * 100}%`;
      track.append(fill);
      row.append(el("span", "", STATUS[status] || status), track, el("b", "", `${num(value)}`));
      box.append(row);
    }
    const retries = counters["asr.whisper_retry"] || 0, recovered = counters["asr.whisper_retry_recovered"] || 0;
    box.append(el("p", "muted small", `Усього ${num(total)} · розпізнано ${Math.round(((counters["asr.outcome.ready"] || 0) / total) * 100)}%` +
      (retries ? ` · другий шанс Whisper (зациклився з підказкою): ${num(retries)}, текст відновлено ${num(recovered)}` : "")));
  }

  function drawLatency(ready) {
    const table = el("table"), head = el("tr");
    for (const h of ["", "Середня", "p95", "Макс.", "N"]) head.append(el("th", "", h));
    table.append(head);
    for (const [key, label] of Object.entries(LATENCY)) {
      const m = ready[key]; if (!m) continue;
      const row = el("tr");
      for (const v of [label, fix(m.mean), fix(m.recent_p95), fix(m.max), num(m.count)]) row.append(el("td", "", v));
      table.append(row);
    }
    $("latency").replaceChildren(table.rows.length > 1 ? table : el("p", "muted", "Ще немає розпізнаних фраз у цьому запуску."));
  }

  function kv(pairs) {
    const box = el("div", "kv");
    for (const [k, v, tone] of pairs) box.append(el("span", "", k), el("span", tone || "", v));
    return box;
  }
  function drawHealth(c, g) {
    const bad = v => (v ? "alert" : "good");
    $("health").replaceChildren(kv([
      ["Втрачено аудіокадрів", num(g["audio.frames_dropped"] ?? 0), bad(g["audio.frames_dropped"])],
      ["Втрачено сегментів", num(g["audio.segments_dropped"] ?? 0), bad(g["audio.segments_dropped"])],
      ["Пік черги сегментів", num(g["asr.segment_queue_peak"] ?? 0)],
      ["Розриви циклу під час приймання", num(c["receiver.suspensions"] || 0), c["receiver.suspensions"] ? "caution" : "good"],
      ["Перевідкриття мікрофона", num(c["audio.stream_stalls"] || 0), c["audio.stream_stalls"] ? "caution" : "good"],
      ["Перезапуски Whisper", num(c["workers.transcriber_restarted"] || 0), bad(c["workers.transcriber_restarted"])],
      ["Черга Telegram", num(g["telegram.queue_depth"] ?? 0)],
      ["Голосові без доставки", num(c["telegram.voice_exhausted"] || 0), bad(c["telegram.voice_exhausted"])],
      ["Резервні копії з помилкою", num(c["backup.failed"] || 0), bad(c["backup.failed"])],
    ]));
  }

