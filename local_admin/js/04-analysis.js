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
  const showIds = ids => openRecordings({ view: "all", search: ids.join(",") });
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
  const EXP_KEYS = [["wer", "Помилка в словах", false], ["digits_exact", "Номери точно", true], ["recipient_accuracy_from_hypothesis", "Адресат", true],
    ["missed_speech", "Пропущено мовлення", false, true], ["false_text_on_non_speech", "Текст на шумі/сигналі", false, true],
    ["false_text_on_room_noise", "Текст на кімнаті", false, true], ["latency_p90_s", "Затримка p90, с", false, true]];
  const expValue = (key, value, count) => value == null ? "—" : count ? (key.includes("latency") ? fix(value, 1) : num(value)) : pct(value);
  const picked = new Set();
  async function experiments() {
    const r = await request("/api/experiments");
    const table = $("experiments-list"), head = el("tr");
    table.replaceChildren(); picked.clear(); $("experiments-compare").disabled = true; $("experiments-result").replaceChildren();
    for (const h of ["", "Коли", "Назва", "Що перевірялось", "Записів", ...EXP_KEYS.map(k => k[1])]) head.append(el("th", "", h));
    table.append(head);
    if (!r.items.length) { const row = el("tr"), cell = el("td", "muted", "Прогонів ще немає."); cell.colSpan = 12; row.append(cell); table.append(row); return; }
    for (const run of r.items) {
      const row = el("tr"), pick = el("input"), p = run.params || {};
      pick.type = "checkbox";
      pick.addEventListener("change", () => {
        if (pick.checked) picked.add(run.name); else picked.delete(run.name);
        $("experiments-compare").disabled = picked.size !== 2;
      });
      const started = p.started_utc ? new Date(p.started_utc.replace(/^(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)Z$/, "$1-$2-$3T$4:$5:$6Z")) : null;
      const what = [p.variant, p.prompt && `підказка: ${p.prompt}`, p.model && String(p.model).replace(/^mlx-community\//, ""), p.llm && `Qwen: ${p.llm}`].filter(Boolean).join(" · ");
      const cells = [el("td"), el("td", "nowrap", started ? started.toLocaleString("uk-UA", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—"),
        el("td", "", run.label), el("td", "small muted", what), el("td", "num", run.complete ? num(run.done) : `${num(run.done)} / ${num(p.records || 0)} · не завершено`)];
      cells[0].append(pick);
      for (const [key, , , count] of EXP_KEYS) cells.push(el("td", "num", expValue(key, run.summary?.[key], count)));
      row.append(...cells); table.append(row);
    }
  }
  $("experiments-compare").addEventListener("click", () => action(async () => {
    const [a, b] = [...picked];
    const r = await request(`/api/experiments/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
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

