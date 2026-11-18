  // ---------- Аналіз: якість ----------
  // Плитки зверху (за весь час і сьогодні), таблиця по днях, розбір помилок «що заважає без правок».
  const dayKey = (shift = 0) => { const d = new Date(); d.setDate(d.getDate() + shift);
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; };
  function qualityTile(label, value, extra, tone = "", title = "", go = null) {
    const box = el(go ? "button" : "div", `stat-tile ${tone}`.trim());
    if (go) { box.type = "button"; box.addEventListener("click", go); }
    box.append(el("span", "stat-label", label), el("span", "stat-value", value));
    if (extra) box.append(el("span", "stat-extra", extra));
    if (title) box.title = title;
    return box;
  }
  async function quality() {
    const r = await request("/api/quality?days=60");
    const table = $("quality-days"), head = el("tr");
    table.replaceChildren();
    for (const h of ["День", "Фраз", "Без правок", "Пропущено", "WER Whisper", "після обробки", "Номери", "Адресат"]) head.append(el("th", "", h));
    table.append(head);
    // Підсумок за весь період — зважений за кількістю записів і слів.
    const sum = key => r.days.reduce((a, d) => a + (d[key] || 0), 0);
    const share = (a, b) => (b ? a / b : null);
    const total = { day: "Усього", records: sum("records"), no_edits: sum("no_edits"), no_edits_share: share(sum("no_edits"), sum("records")),
      missed_share: share(sum("missed"), sum("records")), wer_raw: share(sum("raw_errors"), sum("words")),
      wer_final: share(sum("final_errors"), sum("words")), numbers: share(sum("numbers_ok"), sum("with_numbers")),
      recipient: share(sum("roles_ok"), sum("roles")) };
    const byDay = Object.fromEntries(r.days.map(d => [d.day, d])), today = byDay[dayKey()], yesterday = byDay[dayKey(-1)];
    const share0 = v => v == null ? "—" : `${Math.round(v * 100)} %`;
    const trend = (now, before, higherBetter) => {
      if (now == null || before == null) return "";
      const delta = (now - before) * 100, good = higherBetter ? delta >= 0 : delta <= 0;
      return `${delta >= 0 ? "▲" : "▼"} ${Math.abs(delta).toFixed(0)} п.п. до вчора${good ? "" : " ⚠"}`;
    };
    const goal = qualityTile("Без правок", share0(today?.no_edits_share ?? total.no_edits_share),
      today ? `сьогодні · ${trend(today.no_edits_share, yesterday?.no_edits_share, true) || `за весь час ${share0(total.no_edits_share)}`}` : `за весь час · ${num(total.no_edits)} з ${num(total.records)}`,
      "goal", "Фрази, де підсумковий текст збігся з еталоном слово в слово — головна мета");
    if (today || total.no_edits_share != null) {
      const bar = el("span", "stat-bar"), fill = el("i");
      fill.style.setProperty("--share", `${((today?.no_edits_share ?? total.no_edits_share) * 100).toFixed(1)}%`); bar.append(fill); goal.append(bar);
    }
    $("quality-tiles").replaceChildren(goal,
      qualityTile("Пропущено", share0(today?.missed_share ?? total.missed_share), today ? `сьогодні · за весь час ${share0(total.missed_share)}` : "за весь час", "",
                  "Мовлення, з якого тексту не вийшло зовсім"),
      qualityTile("WER до → після", `${share0(total.wer_raw)} → ${share0(total.wer_final)}`, "Whisper → після обробки, за весь час", "wide",
                  "Частка слів, записаних не так: сирий текст Whisper і після словників, шаблонів і правил"),
      qualityTile("Номери точно", share0(today?.numbers ?? total.numbers), today ? `сьогодні · ${trend(today.numbers, yesterday?.numbers, true)}` : "за весь час", "",
                  "Фрази з числами, де всі числа збіглися"),
      qualityTile("Адресат вірно", share0(today?.recipient ?? total.recipient), today ? `сьогодні · за весь час ${share0(total.recipient)}` : "за весь час", "",
                  "Фрази, де програма правильно визначила, кому адресовано"),
      qualityTile("Розмічено", num(total.records), today ? `сьогодні ${num(today.records)}` : "", "compact", "Розмічене мовлення за 60 днів"));
    if (!r.days.length) {
      const row = el("tr"), cell = el("td", "muted", "Розмічених записів мовлення за цей період немає. Розмітьте кілька в «Аудіоархіві».");
      cell.colSpan = 8; row.append(cell); table.append(row);
    }
    const bar = (value, good) => {
      const cell = el("td", "num");
      if (value == null) { cell.textContent = "—"; return cell; }
      const wrap = el("div", "bar-cell"), fill = el("i", good ? "good" : "bad");
      fill.style.width = `${Math.round(Math.min(1, value) * 100)}%`;
      wrap.append(fill, el("span", "", share0(value)));
      cell.append(wrap);
      return cell;
    };
    for (const d of r.days.length ? [total, ...[...r.days].reverse()] : []) {
      const row = el("tr", d === total ? "total" : d.day === dayKey() ? "today" : "");
      row.append(el("td", "nowrap", d === total ? "Усього" : d.day === dayKey() ? "сьогодні" : d.day.slice(8, 10) + "." + d.day.slice(5, 7)), el("td", "num", String(d.records)),
        bar(d.no_edits_share, true), bar(d.missed_share, false), bar(d.wer_raw, false), bar(d.wer_final, false),
        bar(d.numbers, true), bar(d.recipient, true));
      table.append(row);
    }
    await qualityErrors();
    if (typeof fitAnalysis === "function") fitAnalysis();
  }

  // Розбір помилок: види розбіжностей (клік — ці записи в архіві), вплив обробки, найчастіші заміни.
  const KIND_HELP = { numbers: "числа почуто чи записано не так", regroup: "ті самі цифри, але інакше згруповані («4862» замість «48 62»)",
    callsigns: "позивний зі словника почуто як інше слово", words: "звичайні слова (не позивні й не числа)",
    inserted: "у тексті є слова, яких не казали", deleted: "сказане не потрапило в текст", missed: "з мовлення не вийшло жодного слова" };
  const SUB_KIND = { callsign: "позивний", number: "число", word: "слово" };
  // Пошук архіву приймає до 200 знаків: перші 8 знаків ID (20 × 9 = 180) однозначні й уміщаються.
  const idsQuery = ids => ids.map(id => id.slice(0, 8)).join(",");
  const showIds = ids => openRecordings({ view: "all", search: idsQuery(ids) });
  async function qualityErrors() {
    const e = await request(`/api/quality/errors?days=${$("errors-days").value}`);
    const box = $("errors-kinds");
    if (!e.records) { box.replaceChildren(el("p", "muted", "Розміченого мовлення за цей період немає.")); $("errors-subs").replaceChildren(); $("errors-words").replaceChildren(); return; }
    const head = el("p", "error-head");
    head.append(el("b", "", num(e.with_edits)), ` з ${num(e.records)} фраз потребували правок. Одна фраза може мати кілька причин.`);
    const rows = Object.entries(e.categories).filter(([, c]) => c.count).sort((a, b) => b[1].count - a[1].count).map(([key, c]) => {
      const row = el("div", "error-row"), track = el("span", "quality-track"), fill = el("i");
      fill.style.width = `${Math.max(2, (c.count / Math.max(1, e.with_edits)) * 100)}%`; track.append(fill);
      const open = el("button", "live-open", "показати →"); open.type = "button";
      open.title = `Відкрити в архіві ${c.ids.length < c.count ? `перші ${c.ids.length}` : "ці записи"}`;
      open.addEventListener("click", () => showIds(c.ids));
      const label = el("span", "error-label", c.title); label.title = KIND_HELP[key] || "";
      row.append(label, track, el("b", "", num(c.count)), open);
      return row;
    });
    const p = e.processing, proc = el("div", "error-processing");
    proc.append(el("span", "muted", "Обробка після Whisper:"));
    for (const [count, ids, text, tone] of [[p.better, p.better_ids, "допомогла", "good"], [p.worse, p.worse_ids, "зашкодила", p.worse ? "bad" : ""]]) {
      const b = el("button", `error-chip ${tone}`, `${text} ${num(count)}`); b.type = "button"; b.disabled = !count;
      b.title = count ? `Показати в архіві (до ${ids.length})` : "";
      b.addEventListener("click", () => showIds(ids));
      proc.append(b);
    }
    box.replaceChildren(head, ...rows, proc);
    const subs = e.substitutions.map(s => {
      const row = el("button", "sub-row"); row.type = "button"; row.title = "Показати ці записи в архіві";
      row.append(el("span", "sub-from", s.from), el("span", "sub-arrow", "→"), el("span", "sub-to", s.to),
                 el("span", `sub-kind ${s.kind}`, SUB_KIND[s.kind] || s.kind), el("b", "", `×${s.count}`));
      row.addEventListener("click", () => showIds(s.ids));
      return row;
    });
    $("errors-subs").replaceChildren(...(subs.length ? subs : [el("p", "muted", "Повторюваних замін (від 2 разів) немає.")]));
    const chips = (title, items) => {
      if (!items.length) return null;
      const line = el("div", "word-chips"); line.append(el("span", "muted small", title));
      for (const w of items) line.append(el("span", "word-chip", `${w.word} ×${w.count}`));
      return line;
    };
    $("errors-words").replaceChildren(...[chips("Вигадані:", e.inserted), chips("Пропущені:", e.deleted)].filter(Boolean));
  }
  $("errors-days").addEventListener("change", () => action(async () => { await qualityErrors(); if (typeof fitAnalysis === "function") fitAnalysis(); }));

  // ---------- Аналіз: набір для дообучення ----------
  async function training() {
    const t = await request("/api/training");
    const box = $("training"); box.replaceChildren();
    const minutes = s => `${Math.floor(s / 3600)} год ${Math.round((s % 3600) / 60)} хв`;
    for (const target of t.targets) {
      const row = el("div", "bar-cell"), fill = el("i", "good");
      fill.style.width = `${Math.round(target.share * 100)}%`;
      row.append(fill, el("span", "", `${minutes(t.seconds)} з ${target.seconds / 3600} год (${Math.round(target.share * 100)}%)`));
      box.append(row);
    }
    box.append(el("p", "muted small", `Придатних записів: ${t.records}${t.too_long ? ` · довших за 30 с (не входять): ${t.too_long}` : ""}`));
  }
  $("training-export").addEventListener("click", () => action(async () => {
    $("training-export").disabled = true;
    try {
      const r = await request("/api/training/export", "POST");
      note(`Вивантажено ${r.records} записів (${Math.round(r.seconds / 60)} хв; навчання ${r.train}, перевірка ${r.test}) у ${r.path}`);
    } finally { $("training-export").disabled = false; }
  }));

  // ---------- Аналіз: офлайн-прогони ----------
  // Колонки: [ключ, назва, більше — краще, кількість (не частка)]. «Без правок» — частка фраз слово в слово.
  const EXP_KEYS = [["no_edits", "Без правок", true], ["wer", "Помилка в словах", false], ["digits_exact", "Номери", true],
    ["recipient_accuracy_from_hypothesis", "Адресат", true], ["missed_speech", "Пропущено", false, true],
    ["false_text_on_non_speech", "Текст на шумі", false, true], ["false_text_on_room_noise", "Текст на кімнаті", false, true],
    ["latency_p90_s", "Затримка p90, с", false, true]];
  const expValue = (key, value, count) => value == null ? "—" : count ? (key.includes("latency") ? fix(value, 1) : num(value)) : pct(value);
  const withNoEdits = s => s ? { ...s, no_edits: s.speech_records ? (s.exact_phrases ?? null) / s.speech_records : null } : s;
  // Опис прогону простими словами — з параметрів (тест із черги — назва рецепта).
  let labPresets = {};
  function describeRun(run) {
    const p = run.params || {}, preset = run.label.startsWith("q-") ? labPresets[run.label.slice(2)] : null;
    const parts = [];
    parts.push({ whisper: "лише Whisper, без обробки тексту", pipeline: "увесь шлях, як в ефірі", rules: "Whisper + правила",
                 qwen: "Whisper + Qwen" }[p.variant] || p.variant || "");
    parts.push({ receiver: "підказка як у приймачі", none: "без підказки", dictionary: "підказка: лише назви словника",
                 phrases: "підказка: лише звороти", combined: "підказка: звороти + словник" }[p.prompt] || "");
    if (p.daily_codes === "latest") parts.push("+ останній код доби"); else if (p.daily_codes === "append") parts.push("+ усі коди доби");
    if (p.names_file) parts.push("свій список назв");
    if (p.retry === "on") parts.push("другий шанс");
    if (p.model && String(p.model).includes("turbo")) parts.push("модель turbo");
    if (p.bandpass && p.bandpass !== "off" && p.bandpass !== false) parts.push("фільтр частот");
    if (p.resegment && p.resegment !== "none") parts.push("повторна нарізка");
    if (Array.isArray(p.temperature) && p.temperature.length > 3) parts.push("широкі температури");
    if (p.llm === "on" || p.llm === true) parts.push("Qwen");
    if (p.min_rms_gate) parts.push(`поріг гучності ${p.min_rms_gate}`);
    return { title: preset?.title || run.label, details: parts.filter(Boolean).join(" · "), about: preset?.about || "" };
  }
  const picked = new Set();
  async function experiments() {
    await labQueue();
    const r = await request("/api/experiments");
    const table = $("experiments-list"), head = el("tr");
    table.replaceChildren(); picked.clear(); $("experiments-compare").disabled = true; $("experiments-result").replaceChildren();
    for (const h of ["", "Коли", "Тест", "Записів", ...EXP_KEYS.map(k => k[1])]) head.append(el("th", "", h));
    table.append(head);
    const showAll = $("experiments-incomplete").checked;
    const runs = r.items.map(run => ({ ...run, summary: withNoEdits(run.summary) })).filter(run => showAll || run.complete);
    if (!runs.length) { const row = el("tr"), cell = el("td", "muted", "Завершених прогонів ще немає — додайте тест у чергу."); cell.colSpan = 12; row.append(cell); table.append(row); return; }
    // Найкраще значення кожної колонки серед завершених — зеленим.
    const best = {};
    for (const [key, , higher] of EXP_KEYS) {
      const values = runs.filter(x => x.complete).map(x => x.summary?.[key]).filter(v => v != null);
      if (values.length > 1) best[key] = higher ? Math.max(...values) : Math.min(...values);
    }
    for (const run of runs) {
      const row = el("tr", run.complete ? "" : "incomplete"), pick = el("input"), p = run.params || {};
      pick.type = "checkbox"; pick.title = "Позначити для порівняння";
      pick.addEventListener("change", () => {
        if (pick.checked) picked.add(run.name); else picked.delete(run.name);
        $("experiments-compare").disabled = picked.size !== 2;
      });
      const started = p.started_utc ? new Date(p.started_utc.replace(/^(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)Z$/, "$1-$2-$3T$4:$5:$6Z")) : null;
      const info = describeRun(run), what = el("td", "lab-what");
      what.append(el("b", "", info.title), el("span", "muted small", info.details));
      if (info.about) what.title = info.about;
      const cells = [el("td"), el("td", "nowrap", started ? started.toLocaleString("uk-UA", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—"),
        what, el("td", "num", run.complete ? num(run.done) : `${num(run.done)} / ${num(p.records || 0)}`)];
      cells[0].append(pick);
      for (const [key, , , count] of EXP_KEYS) {
        const value = run.summary?.[key];
        cells.push(el("td", `num${run.complete && value != null && value === best[key] ? " best" : ""}`, expValue(key, value, count)));
      }
      row.append(...cells); table.append(row);
    }
    if (typeof fitLab === "function") fitLab();
  }
  $("experiments-incomplete").addEventListener("change", () => action(experiments));
  $("experiments-compare").addEventListener("click", () => action(async () => {
    const [a, b] = [...picked];
    const r = await request(`/api/experiments/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
    r.a = withNoEdits({ ...r.a, speech_records: r.a.speech_records }); r.b = withNoEdits({ ...r.b, speech_records: r.b.speech_records });
    const table = el("table", "compact"), head = el("tr");
    for (const h of ["Показник", a.split("_").slice(1).join("_"), b.split("_").slice(1).join("_")]) head.append(el("th", "", h));
    table.append(head);
    for (const [key, title, higherBetter, count] of EXP_KEYS) {
      const va = r.a[key], vb = r.b[key], row = el("tr");
      const better = va == null || vb == null || va === vb ? null : (higherBetter ? vb > va : vb < va) ? "b" : "a";
      row.append(el("td", "", title), el("td", `num ${better === "a" ? "good" : ""}`, expValue(key, va, count)), el("td", `num ${better === "b" ? "good" : ""}`, expValue(key, vb, count)));
      table.append(row);
    }
    const box = $("experiments-result");
    box.replaceChildren(el("p", "small", `Порівняння на ${num(r.common)} спільних записах. Зеленим — кращий результат.`), table);
  }));

  // ---------- Черга тестів ----------
  const LAB_STATE = { starting: "запускається…", stopping_receiver: "зупиняю приймач…", running: "іде тест", cooldown: "пауза від перегріву",
                      waiting: "чекає часу старту", idle: "не запущено" };
  const clockTime = t => new Date(t * 1000).toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" });
  async function labQueue() {
    let q;
    try { q = await request("/api/lab/queue"); } catch (_) { return; }
    labPresets = Object.fromEntries(q.presets.map(p => [p.key, p]));
    const runner = q.runner || {}, active = runner.active, jobs = q.jobs;
    const queued = jobs.filter(j => j.status === "queued"), running = jobs.find(j => j.status === "running");
    const minutes = queued.reduce((a, j) => a + (labPresets[j.preset]?.minutes || 30), 0) + Math.max(0, queued.length - 1) * Math.round(q.cooldown_s / 60);
    $("lab-state").textContent = active ? LAB_STATE[runner.state] || runner.state : queued.length ? `у черзі ${queued.length} · ≈${minutes} хв` : "черга порожня";
    // Банер: приймач на паузі через тести — ефір не слухається.
    const banner = $("lab-banner");
    banner.hidden = !active;
    if (active) {
      const until = runner.until ? ` до ${clockTime(runner.until)}` : "";
      banner.textContent = runner.state === "waiting" ? `Черга чекає старту о ${clockTime(runner.start_at || runner.until)}. Приймач поки працює.`
        : runner.state === "cooldown" ? `Пауза між тестами${until}, щоб Mac охолов. Приймач зупинено — ефір не слухається.`
        : `Іде тест — приймач зупинено, ефір не слухається. Після черги приймач запуститься сам.`;
    }
    const STATUS_ICON = { queued: "○", running: "◉", done: "✓", failed: "⚠", cancelled: "✕" };
    const rows = jobs.map((job, index) => {
      const preset = labPresets[job.preset] || { title: job.preset }, row = el("div", `lab-job ${job.status}`);
      const text = el("div", "lab-job-text");
      text.append(el("b", "", preset.title));
      const bits = [];
      if (job.note) bits.push(job.note);
      if (job.status === "running" && job.progress) bits.push(`${num(job.progress.done)}${job.progress.total ? ` з ${num(job.progress.total)}` : ""} записів`);
      if (job.status === "done") bits.push(`готово о ${clockTime(job.finished)}`);
      if (job.status === "failed") bits.push(job.error || `помилка (код ${job.returncode ?? "?"})`);
      if (job.status === "cancelled") bits.push("скасовано");
      if (job.status === "queued") bits.push(`≈${preset.minutes || 30} хв`);
      text.append(el("span", "muted small", bits.join(" · ")));
      if (job.status === "running" && job.progress?.total) {
        const bar = el("span", "quality-track lab-progress"), fill = el("i");
        fill.style.width = `${Math.min(100, (job.progress.done / job.progress.total) * 100)}%`; bar.append(fill); text.append(bar);
      }
      const tools = el("span", "lab-job-tools");
      const tool = (label, title, go) => { const b = el("button", "lab-tool", label); b.type = "button"; b.title = title; b.addEventListener("click", () => action(go)); tools.append(b); };
      if (job.status === "queued") {
        const position = queued.indexOf(job);
        if (position > 0) tool("↑", "Раніше", async () => { await request(`/api/lab/queue/${job.id}/move`, "POST", { delta: -1 }); await labQueue(); });
        if (position < queued.length - 1) tool("↓", "Пізніше", async () => { await request(`/api/lab/queue/${job.id}/move`, "POST", { delta: 1 }); await labQueue(); });
      }
      tool("✕", job.status === "running" ? "Зупинити цей тест (черга піде далі)" : job.status === "queued" ? "Прибрати з черги" : "Прибрати з переліку", async () => {
        if (job.status === "running" && !confirm("Зупинити тест, що йде? Його результат буде неповним.")) return;
        await request(`/api/lab/queue/${job.id}`, "DELETE"); await labQueue();
      });
      row.append(el("span", "lab-job-icon", STATUS_ICON[job.status] || "•"), text, tools);
      return row;
    });
    $("lab-jobs").replaceChildren(...(rows.length ? rows : [el("p", "muted", "Тестів у черзі немає. Оберіть праворуч, що перевірити.")]));
    // Кнопки керування.
    const controls = [];
    const button = (label, cls, title, go) => { const b = el("button", cls, label); b.type = "button"; b.title = title; b.addEventListener("click", () => action(go)); controls.push(b); return b; };
    if (!active && queued.length) {
      button("▶ Почати зараз", "", "Зупинити приймач і виконати чергу", async () => {
        if (!confirm(`Запустити ${queued.length} тест(и), ≈${minutes} хв? На цей час приймач зупиниться — ефір не слухатиметься.`)) return;
        await request("/api/lab/queue/start", "POST", {}); await labQueue();
      });
      const at = el("input", "lab-time"); at.type = "time"; at.value = "01:00"; at.title = "Час старту (сьогодні або завтра)";
      controls.push(at);
      button("Почати о цій порі", "ghost", "Черга запуститься в указаний час; до того приймач працює", async () => {
        const [h, m] = at.value.split(":").map(Number), when = new Date(); when.setHours(h, m, 0, 0);
        if (when.getTime() < Date.now()) when.setDate(when.getDate() + 1);
        await request("/api/lab/queue/start", "POST", { start_at: when.getTime() / 1000 }); await labQueue();
        note(`Черга запуститься о ${at.value} (${when.toLocaleDateString("uk-UA")}).`);
      });
    }
    if (active) {
      button("Зупинити після поточного", "ghost", "Дочекатися кінця тесту, що йде, і зупинити чергу", async () => { await request("/api/lab/queue/stop", "POST", { now: false }); await labQueue(); });
      button("Зупинити зараз", "ghost danger-text", "Перервати тест, що йде, і зупинити чергу", async () => {
        if (!confirm("Перервати тест, що йде? Приймач запуститься знову.")) return;
        await request("/api/lab/queue/stop", "POST", { now: true }); await labQueue();
      });
    }
    if (jobs.some(j => !["queued", "running"].includes(j.status)))
      button("Прибрати виконані", "link-button", "Прибрати з переліку виконані, перервані й скасовані", async () => { await request("/api/lab/queue/clear", "POST"); await labQueue(); });
    $("lab-controls").replaceChildren(...controls);
    // Рецепти тестів.
    $("lab-presets").replaceChildren(...q.presets.map(p => {
      const card = el("div", "lab-preset"), text = el("div", "lab-preset-text");
      text.append(el("b", "", p.title), el("span", "muted small", p.about));
      const add = el("button", "ghost small-button", "＋ У чергу"); add.type = "button";
      add.title = `≈${p.minutes} хв; на час тесту приймач зупиниться`;
      add.addEventListener("click", () => action(async () => { await request("/api/lab/queue", "POST", { preset: p.key }); await labQueue(); note(`«${p.title}» — у черзі.`); }));
      card.append(text, el("span", "lab-preset-time muted small", `≈${p.minutes} хв`), add);
      return card;
    }));
  }
