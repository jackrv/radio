  // ---------- Аналіз: якість ----------
  // Плитки зверху (за весь час і сьогодні), таблиця по днях, розбір помилок «що заважає без правок».
  function showAnalysisTab(name) {
    const valid = new Set(["overview", "errors", "conditions", "callsigns", "numbers", "impact", "training"]);
    if (!valid.has(name)) name = "overview";
    if (typeof document !== "undefined") {
      for (const b of document.querySelectorAll("[data-analysis-tab]")) {
        const active = b.dataset.analysisTab === name;
        b.classList.toggle("active", active);
        b.setAttribute("aria-current", active ? "page" : "false");
        b.removeAttribute?.('aria-selected');
      }
      for (const panel of document.querySelectorAll("[data-analysis-panel]")) {
        panel.hidden = panel.dataset.analysisPanel !== name;
      }
    }
    try { localStorage.setItem("radio345:analysis-tab", name); } catch (_) {}
    if (typeof fitAnalysis === "function") requestAnimationFrame(() => fitAnalysis());
  }
  if (typeof document !== "undefined") {
    for (const b of document.querySelectorAll("[data-analysis-tab]")) {
      b.addEventListener("click", () => showAnalysisTab(b.dataset.analysisTab));
    }
  }
  let savedAnalysisTab = "overview";
  try { savedAnalysisTab = localStorage.getItem("radio345:analysis-tab") || "overview"; } catch (_) {}
  showAnalysisTab(savedAnalysisTab);

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
    impactPanel().resume();
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
    drawEffort(e); drawConditions(e.conditions); drawCallsigns(e); drawNumberConfusions(e);
  }
  // Плитка «≤ 1 правка»: фрази без правок або з одним хибним словом — прогрес видно раніше, ніж фрази стануть ідеальними.
  function drawEffort(e) {
    const f = e.effort; if (!f || !e.records) return;
    $("quality-tiles").querySelector?.(".effort")?.remove();
    const near = (f.no_edits + f.one_word) / e.records;
    const tile = qualityTile("≤ 1 правка", `${Math.round(near * 100)} %`, `ще ${num(f.one_word)} з одним словом · сер. ${fix(f.errors_mean, 1)} сл. на фразу`,
                             "effort", "Фрази без правок або з одним хибним словом (за періодом розбору). Клік — фрази з одним словом",
                             () => showIds(f.one_word_ids));
    $("quality-tiles").children?.[0]?.after?.(tile);
  }
  // «Без правок» за тривалістю, гучністю й упевненістю Whisper.
  function drawConditions(c) {
    const box = $("errors-conditions");
    if (!c) { box.replaceChildren(); return; }
    const pct0 = v => v == null ? "—" : `${Math.round(v * 100)} %`;
    const group = (title, rows) => {
      if (!rows?.length) return null;
      const wrap = el("div", "condition-group"); wrap.append(el("div", "quality-head", title));
      for (const g of rows) {
        const row = el("div", "error-row"), track = el("span", "quality-track"), fill = el("i");
        fill.style.width = `${Math.max(2, (g.share || 0) * 100)}%`; track.append(fill);
        const open = el("button", "live-open", "показати →"); open.type = "button"; open.title = "Фрази з правками в цій групі";
        open.disabled = !g.ids.length; open.addEventListener("click", () => showIds(g.ids));
        const label = el("span", "error-label", g.title);
        label.title = `${num(g.records)} фраз · у середньому ${fix(g.errors_mean, 1)} хибних слів${g.range ? ` · упевненість ${fix(g.range[0], 2)}…${fix(g.range[1], 2)}` : ""}`;
        row.append(label, track, el("b", "", pct0(g.share)), open);
        wrap.append(row);
      }
      return wrap;
    };
    const verdict = el("p", "condition-verdict");
    if (c.separation != null) {
      const low = c.confidence[0], high = c.confidence[c.confidence.length - 1];
      verdict.textContent = c.separation >= 0.15
        ? `Упевненість Whisper добре передбачає правки: серед найменш упевнених без правок ${pct0(low.share)}, серед найбільш — ${pct0(high.share)}. За нею можна сортувати чергу розмітки й позначати сумнівні фрази.`
        : `Упевненість Whisper слабо передбачає правки (${pct0(low.share)} проти ${pct0(high.share)}) — сортувати за нею чергу немає сенсу.`;
      verdict.className = c.separation >= 0.15 ? "condition-verdict good" : "condition-verdict";
    }
    box.replaceChildren(...[group("Тривалість запису", c.duration), group("Гучність", c.loudness), group("Упевненість Whisper", c.confidence), verdict].filter(Boolean));
  }
  // Позивні: скільки разів почуто вірно, з чим плутається, скільки разів вигадано (узято з підказки там, де його не казали).
  function drawCallsigns(e) {
    const box = $("errors-callsigns"), list = e.callsigns || [];
    if (!list.length) { box.replaceChildren(el("p", "muted", "Позивних зі словника в еталонах за цей період немає.")); return; }
    const cap = s => s.charAt(0).toUpperCase() + s.slice(1);
    const rows = list.map(c => {
      const row = el("div", "callsign-row"), track = el("span", "quality-track"), fill = el("i");
      fill.style.width = `${Math.max(2, c.share * 100)}%`; track.append(fill);
      const heard = el("span", "callsign-heard");
      // Довгий вигаданий фрагмент замість позивного — коротко (повністю — у підказці).
      for (const h of c.heard.slice(0, 2)) {
        const words = h.text.split(" "), short = words.length > 3 || h.text.length > 24 ? `${words.slice(0, 3).join(" ").slice(0, 24)}…` : h.text;
        const chip = el("span", "word-chip", `${short} ×${h.count}`); chip.title = h.text; heard.append(chip);
      }
      if (c.missing) heard.append(el("span", "word-chip muted", `пропущено ×${c.missing}`));
      const invented = el("span", c.invented > c.correct ? "callsign-invented bad" : c.invented ? "callsign-invented" : "callsign-invented none",
                          c.invented ? `вигадано ×${c.invented}` : "");
      if (c.invented) invented.title = "Скільки разів Whisper вставив цей позивний там, де його не казали (найчастіше — з підказки)";
      const open = el("button", "live-open", "→"); open.type = "button"; open.title = "Фрази, де цей позивний почуто не так";
      open.disabled = !c.ids.length; open.addEventListener("click", () => showIds(c.ids));
      row.append(el("b", "callsign-name", cap(c.name)), track, el("span", "callsign-count", `${num(c.correct)} з ${num(c.total)}`), heard, invented, open);
      return row;
    });
    const extra = (e.invented_only || []).length
      ? [el("p", "muted small", `Вигадані, хоча в еталонах їх немає: ${e.invented_only.map(i => `${cap(i.name)} ×${i.count}`).join(", ")}.`)] : [];
    box.replaceChildren(...rows, ...extra);
  }
  // Числа: скільки разів конкретне почуте число відрізнялось від еталону.
  function drawNumberConfusions(e) {
    const box = $("errors-numbers"), list = e.number_confusions || [];
    if (!list.length) {
      box.replaceChildren(el("p", "muted", "Повторюваних плутанин конкретних чисел немає."));
      return;
    }
    const rows = list.map(pair => {
      const row = el("div", "number-confusion-row"), open = el("button", "live-open", "показати →");
      open.type = "button"; open.title = "Відкрити записи з цими числами";
      open.addEventListener("click", () => showIds(pair.ids));
      row.append(el("b", "sub-from", pair.from), el("span", "sub-arrow", "→"),
                 el("b", "sub-to", pair.to), el("b", "", `×${pair.count}`), open);
      return row;
    });
    box.replaceChildren(...rows);
  }
  $("errors-days").addEventListener("change", () => action(async () => { await qualityErrors(); if (typeof fitAnalysis === "function") fitAnalysis(); }));

  const IMPACT_NAMES = {
    phrase_template: "Шаблон звороту",
    phrase_pattern: "Шаблон фрази",
    text_alias: "Аліас тексту",
    callsign_alias: "Аліас позивного",
    short_garbage: "Фільтр короткого шуму",
    blacklist: "Чорний список",
  };
  const IMPACT_LABELS = { noise: "шум", notification: "сигнал", room: "кімната" };
  let impactTask = null;
  function impactPanel() {
    if (!impactTask) impactTask = serverJobPanel({button: $("text-impact-run"), box: $("text-impact-results"), kind: "impact",
      start: () => request("/api/quality/text-impact", "POST"), render: renderTextImpact});
    return impactTask;
  }
  async function textImpact() { return impactPanel().run(); }
  function renderTextImpact(report) {
    const box = $("text-impact-results");
      if (!report.records && !report.non_speech_records) {
        box.replaceChildren(el("p", "muted", "Розмічених текстів для перевірки немає."));
        return;
      }
      const signed = value => `${value > 0 ? "+" : ""}${num(value)}`;
      const signedPp = value => `${value > 0 ? "+" : ""}${(value * 100).toFixed(1)} п.п.`;
      const renderRows = items => items.map(item => {
        const tone = item.effect === "harmful" ? "bad" : item.effect === "helpful" ? "ok" : "";
        const row = el("div", `eval-item ${tone}`);
        const title = el("button", "link-button", `${IMPACT_NAMES[item.kind] || item.kind}: ${item.label}`);
        title.type='button'; title.title='Відкрити чернетку без цього правила й перевірити вплив; збереження окреме';
        title.addEventListener('click', () => {
          const types = {phrase_template:'phrase_templates',phrase_pattern:'phrase_patterns',text_alias:'aliases',callsign_alias:'callsign_aliases',short_garbage:'short_garbage',blacklist:'blacklist'};
          textListType = types[item.kind];
          if (!textListType) return;
          pendingRuleDisable = {type:textListType,label:item.label};
          loaded.delete('texts'); show('texts');
        });
        const effect = item.effect === "harmful" ? "може шкодити"
          : item.effect === "helpful" ? "допомагає"
          : item.effect === "mixed" ? "змішаний вплив" : "не змінило розмічені фрази";
        row.append(title, el("div", "small", `${effect} · без правок ${signed(item.no_edits_delta)} · пропущено ${signed(item.missed_delta)} · WER ${signedPp(item.wer_delta)} · хибний текст на шумі/сигналах/у кімнаті ${signed(item.false_text_delta)} · гірше ${num(item.worse)}, краще ${num(item.better)}`));
        for (const example of item.examples) {
          const open = el("button", "link-button small", `Запис ${example.id.slice(0, 8)}`);
          open.type = "button"; open.addEventListener("click", () => showIds([example.id]));
          row.append(open, el("div", "small", `з правилом: ${example.current}`),
            el("div", "small", `без нього: ${example.without}`),
            el("div", "small muted", `еталон: ${example.reference}`));
        }
        for (const example of item.false_text_examples) {
          const open = el("button", "link-button small", `Запис ${example.id.slice(0, 8)}`);
          open.type = "button"; open.addEventListener("click", () => showIds([example.id]));
          row.append(open, el("div", "small muted", `${IMPACT_LABELS[example.label] || example.label}: з правилом «${example.current || "порожньо"}» · без нього «${example.without || "порожньо"}»`));
        }
        return row;
      });
      const relevant = report.checks.filter(item => item.effect !== 'neutral');
      const neutral = report.checks.filter(item => item.effect === 'neutral');
      let shown = Math.min(40, relevant.length);
      const rows = renderRows(relevant.slice(0, shown));
      const currentNoiseText = report.current.false_text_by_label;
      box.replaceChildren(
        el("p", "muted small", `${num(report.records)} фраз мовлення і ${num(report.non_speech_records)} прикладів шуму, сигналів та кімнати. Поточний хибний текст: ${num(report.current.false_text)} (шум ${num(currentNoiseText.noise)}, сигнали ${num(currentNoiseText.notification)}, кімната ${num(currentNoiseText.room)}). Різниця — поточний результат мінус результат без конкретного правила.`),
        ...(rows.length ? rows : [el("p", "muted", report.checks.length ? 'Помітного впливу активних правил на цей набір немає.' : "Активних текстових правил для перевірки немає.")]),
        el("p", "muted small", report.scope),
      );
      if (neutral.length) {
        const details = el('details','impact-neutral'), heading = el('summary','',`Без змін на цьому наборі (${neutral.length})`);
        details.append(heading);
        let revealed = false;
        details.addEventListener('toggle', () => { if (details.open && !revealed) {
          revealed=true; details.append(...renderRows(neutral.slice(0,40)));
          if (neutral.length>40) details.append(el('p','muted small','Показано перші 40; відсутність впливу на цьому наборі не доводить непотрібність правила.'));
        }});
        box.append(details);
      }
      if (shown < relevant.length) {
        const more = el("button", "ghost small-button", `Ще перевірки (${relevant.length - shown})`); more.type = "button";
        more.addEventListener("click", () => {
          box.append(...renderRows(relevant.slice(shown, shown + 40)));
          shown = Math.min(shown + 40, relevant.length);
          more.hidden = shown === relevant.length;
          more.textContent = `Ще перевірки (${relevant.length - shown})`;
        });
        box.append(more);
      }
  }

  $("text-impact-run").addEventListener("click", () => textImpact());

  // ---------- Аналіз: набір для дообучення ----------
  async function training() {
    if (!trainingTask) trainingJob().resume();
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
  let trainingTask = null;
  function trainingJob() {
    if (!trainingTask) trainingTask = serverJobPanel({button: $("training-export"), box: $("training-export-result"), kind:"training_export",
      start: () => request("/api/training/export", "POST"),
      render: r => $("training-export-result").replaceChildren(el("p", "small", `Вивантажено ${r.records} записів (${Math.round(r.seconds/60)} хв; навчання ${r.train}, перевірка ${r.test}) у ${r.path}`))});
    return trainingTask;
  }
  $("training-export").addEventListener("click", () => trainingJob().run());

  // ---------- Аналіз: офлайн-прогони ----------
  // Колонки: [ключ, назва, більше — краще, кількість (не частка)]. «Без правок» — частка фраз слово в слово.
  const EXP_KEYS = [["no_edits", "Без правок", true], ["wer", "Помилка в словах", false], ["digits_exact", "Номери", true],
    ["recipient_accuracy_from_hypothesis", "Адресат", true], ["missed_speech", "Пропущено", false, true],
    ["false_text_on_non_speech", "Текст на шумі", false, true], ["false_text_on_room_noise", "Текст на кімнаті", false, true],
    ["latency_p90_s", "Затримка p90, с", false, true]];
  const expValue = (key, value, count) => value == null ? "—" : count ? (key.includes("latency") ? fix(value, 1) : num(value)) : pct(value);
  const withNoEdits = s => s ? { ...s, no_edits: s.speech_records && s.exact_phrases != null ? s.exact_phrases / s.speech_records : null } : s;
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
  async function experiments(refresh = false) {
    if (!refresh) await labQueue(false);
    const r = await request("/api/experiments");
    const table = $("experiments-list"), head = el("tr");
    table.replaceChildren();
    if (!refresh) { picked.clear(); $("experiments-result").replaceChildren(); }
    $("experiments-compare").disabled = picked.size !== 2;
    for (const h of ["", "Коли", "Тест", "Записів", ...EXP_KEYS.map(k => k[1])]) head.append(el("th", "", h));
    table.append(head);
    const showAll = $("experiments-incomplete").checked;
    const runs = r.items.map(run => ({ ...run, summary: withNoEdits(run.summary) })).filter(run => showAll || run.complete);
    if (!runs.length) { const row = el("tr"), cell = el("td", "muted", "Завершених прогонів ще немає — додайте тест у чергу."); cell.colSpan = 12; row.append(cell); table.append(row); return; }
    // Різні прогони можуть мати різні записи: «кращий» лише в порівнянні на спільному наборі.
    for (const run of runs) {
      const row = el("tr", run.complete ? "" : "incomplete"), pick = el("input"), p = run.params || {};
      pick.type = "checkbox"; pick.title = "Позначити для порівняння";
      pick.disabled = !run.complete;
      pick.checked = picked.has(run.name);
      pick.addEventListener("change", () => {
        if (pick.checked) picked.add(run.name); else picked.delete(run.name);
        $("experiments-compare").disabled = picked.size !== 2;
      });
      const started = p.started_utc ? new Date(p.started_utc.replace(/^(\d{4})(\d\d)(\d\d)T(\d\d)(\d\d)(\d\d)Z$/, "$1-$2-$3T$4:$5:$6Z")) : null;
      const info = describeRun(run), what = el("td", "lab-what");
      what.append(el("b", "", info.title), el("span", "muted small", info.details));
      const report = el("button", "link-button small", "Звіт і порівняння з базою"); report.type = "button";
      report.addEventListener("click", () => action(() => experimentReport(run.name)));
      what.append(report);
      if (info.about) what.title = info.about;
      const cells = [el("td"), el("td", "nowrap", started ? started.toLocaleString("uk-UA", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—"),
        what, el("td", "num", run.complete ? num(run.done) : `${num(run.done)} / ${num(p.records || 0)}`)];
      cells[0].append(pick);
      for (const [key, , , count] of EXP_KEYS) {
        const value = run.summary?.[key];
        cells.push(el("td", "num", expValue(key, value, count)));
      }
      row.append(...cells); table.append(row);
    }
    if (typeof fitLab === "function") fitLab();
  }
  $("experiments-incomplete").addEventListener("change", () => action(experiments));
  function comparisonDetails(r) {
    const box = el("div", "lab-comparison");
    box.append(el("p", "small", `Спільних записів з однаковими еталонами: ${num(r.common)}. Виключено через зміну розмітки: ${num(r.excluded_reference || 0)}; лише в базі ${num(r.only_a || 0)}, лише в тесті ${num(r.only_b || 0)}.`));
    if (!r.common) { box.append(el("p", "alert small", "Порівняння неможливе: немає спільного набору. Запустіть базовий і дослідний рецепти на тій самій розмітці.")); return box; }
    const a = withNoEdits(r.a), b = withNoEdits(r.b), table = el("table", "compact");
    const head = el("tr"); for (const title of ["Показник", "База", "Тест", "Різниця тест − база"]) head.append(el("th", "", title)); table.append(head);
    for (const [key, title, higher, count] of EXP_KEYS) {
      const va = a[key], vb = b[key], delta = va == null || vb == null ? null : vb-va, row = el("tr");
      const change = delta == null ? "—" : count ? `${delta > 0 ? "+" : ""}${fix(delta, key.includes("latency") ? 1 : 0)}` : `${delta > 0 ? "+" : ""}${fix(delta*100,1)} п.п.`;
      row.append(el("td", "", title), el("td", "num", expValue(key,va,count)), el("td", "num", expValue(key,vb,count)),
        el("td", `num ${delta && (higher ? delta>0 : delta<0) ? "good" : delta ? "alert" : ""}`, change)); table.append(row);
    }
    box.append(table, el("p", "small", `За помилками слів: краще ${num(r.better || 0)}, гірше ${num(r.worse || 0)} фраз. Спершу оцінюйте «без правок», потім пропущене мовлення й хибний текст, далі WER.`));
    if ((a.errors || 0) || (b.errors || 0)) box.append(el("p", "alert small", `Помилки виконання: база ${a.errors || 0}, тест ${b.errors || 0}. Прогін з помилками потребує перевірки.`));
    for (const example of r.examples || []) {
      const open = el("button", "link-button small", `${example.delta > 0 ? "Гірше" : "Краще"}: ${example.id.slice(0,8)} (${example.delta > 0 ? "+" : ""}${example.delta} помилок)`);
      open.type = "button"; open.addEventListener("click", () => showIds([example.id])); box.append(open);
    }
    return box;
  }
  async function experimentReport(name) {
    const data = await request(`/api/experiments/${encodeURIComponent(name)}`), run = data.run, s = run.summary || {}, info = describeRun(run);
    const box = $("experiments-result");
    box.replaceChildren(el("h3", "", info.title), el("p", "muted small", `${run.name} · ${info.details}`),
      el("p", "small", `${run.complete ? "Завершено" : "Незавершено — це частковий результат"}: ${run.done} / ${run.params.records || "?"} записів · помилок виконання ${s.errors ?? "невідомо"}. Модель: ${run.params.model || "не вказана"}.`),
      el("p", "small", `Без правок: ${s.exact_phrases ?? "—"} / ${s.speech_records ?? "—"}; шум/сигнали: ${s.false_text_on_non_speech ?? "—"} / ${s.non_speech_records ?? "—"}; кімната: ${s.false_text_on_room_noise ?? "—"} / ${s.room_records ?? "—"}. «—» означає відсутність даних, а не нуль помилок.`));
    if (data.comparison) { box.append(el("p", "muted small", `База: ${data.baseline}`), comparisonDetails(data.comparison)); }
    else box.append(el("p", "muted small", "Для порівняння потрібні завершені базовий рецепт «Як зараз у приймачі» та цей тест. Результати автоматично не вмикають зміни розпізнавання."));
    if (s.literal_phrases !== undefined) box.append(el('p','muted small',
      `Буквально однакові рядки: ${s.literal_phrases} / ${s.speech_records}. «Без правок» вище порівнює слова без регістру й пунктуації.`));
    box.scrollIntoView?.({block:"nearest", behavior:"smooth"});
  }
  $("experiments-compare").addEventListener("click", () => action(async () => {
    const [a, b] = [...picked];
    const r = await request(`/api/experiments/compare?a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`);
    $("experiments-result").replaceChildren(el("p", "muted small", `База: ${a} · тест: ${b}`), comparisonDetails(r));
  }));

  // ---------- Черга тестів ----------
  const LAB_STATE = { starting: "запускається…", starting_capture: "вивантажую Whisper…", capturing_audio: "лише запис аудіофрагментів",
                      restoring_recognition: "відновлюю Whisper…", running: "іде тест", cooldown: "пауза від перегріву",
                      waiting: "чекає часу старту", idle: "не запущено" };
  const clockTime = t => new Date(t * 1000).toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" });
  // ---------- Вікна часу для тестів ----------
  // Рядки «з — до»; запам'ятовуються в браузері. Під ними — план: який тест у яке вікно потрапить
  // (той самий розрахунок, що й у виконавця: тест стартує, лише якщо встигає до кінця вікна з запасом).
  const WINDOWS_KEY = "radio345:lab-windows";
  const loadWindows = () => { try { const w = JSON.parse(localStorage.getItem(WINDOWS_KEY)); if (Array.isArray(w) && w.length) return w; } catch (_) { /* типово */ }
    return [["01:00", "02:55"], ["03:15", "06:55"]]; };
  const saveWindows = list => { try { localStorage.setItem(WINDOWS_KEY, JSON.stringify(list)); } catch (_) { /* сесія */ } };
  // «01:00–02:55» → unix-час найближчого такого вікна; наступні вікна — не раніше кінця попередніх.
  function windowTimes(list) {
    const out = [];
    let floor = Date.now() - 60000;
    for (const [from, to, day] of list) {
      if (!/^\d{2}:\d{2}$/.test(from) || !/^\d{2}:\d{2}$/.test(to) || from === to) return [];
      const at = hm => { const [h, m] = hm.split(":").map(Number); const d = new Date(floor); d.setHours(h, m, 0, 0); return d; };
      const start = day ? new Date(`${day}T${from}:00`) : at(from);
      if (!Number.isFinite(start.getTime())) return [];
      if (!day) while (start.getTime() < floor) start.setDate(start.getDate() + 1);
      const end = new Date(start); const [eh, em] = to.split(":").map(Number); end.setHours(eh, em, 0, 0);
      if (end <= start) end.setDate(end.getDate() + 1);
      out.push([start.getTime() / 1000, end.getTime() / 1000]);
      floor = end.getTime();
    }
    return out;
  }
  let labWindowEditor = null;
  function windowsEditor(q, queued) {
    if (labWindowEditor) { labWindowEditor.update(q, queued); return labWindowEditor; }
    const box = el("div", "lab-windows");
    let list = loadWindows();
    const dateValue = t => { const d = new Date(t * 1000); return `${d.getFullYear()}-${String(d.getMonth()+1).padStart(2,"0")}-${String(d.getDate()).padStart(2,"0")}`; };
    const initial = windowTimes(list);
    list = list.map((w, i) => [w[0], w[1], w[2] || (initial[i] ? dateValue(initial[i][0]) : dayKey())]);
    const rows = el("div", "lab-window-rows"), preview = el("div", "lab-plan");
    const hm = t => new Date(t * 1000).toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" });
    const render = () => {
      rows.replaceChildren(...list.map((w, i) => {
        const row = el("div", "lab-window-row");
        const day = el("input", "lab-date"); day.type = "date"; day.value = w[2];
        day.setAttribute?.("aria-label", "Дата початку вікна");
        const from = el("input", "lab-time"), to = el("input", "lab-time");
        from.type = to.type = "time"; from.value = w[0]; to.value = w[1];
        from.step = to.step = "300";
        from.setAttribute?.("aria-label", "Початок вікна"); to.setAttribute?.("aria-label", "Кінець вікна");
        const change = () => { list[i] = [from.value, to.value, day.value]; saveWindows(list); drawPlan(); };
        for (const field of [from, to, day]) field.addEventListener("input", change);
        const remove = el("button", "lab-tool", "✕"); remove.type = "button"; remove.title = "Прибрати вікно";
        remove.addEventListener("click", () => { list.splice(i, 1); saveWindows(list); render(); });
        row.append(day, el("span", "muted small", "з"), from, el("span", "muted small", "до"), to, remove);
        return row;
      }));
      drawPlan();
    };
    let planRevision = 0, planTimer = null;
    const drawPlan = () => {
      clearTimeout(planTimer);
      const revision = ++planRevision;
      go.disabled = true;
      preview.replaceChildren(el("p", "muted small", "Сервер розраховує план…"));
      planTimer = setTimeout(async () => {
        const windows = windowTimes(list);
        if (!windows.length) { preview.replaceChildren(el("p", "alert small", "Вкажіть дату й різний час початку та завершення.")); return; }
        try {
          const {plan, left} = await request("/api/lab/plan", "POST", {windows});
          if (revision !== planRevision) return;
          go.disabled = !plan.some(slot => slot.jobs.length);
          const lines = plan.map(slot => {
            const line = el("div", "lab-plan-window");
            line.append(el("b", "", `${dateValue(slot.window[0])} ${hm(slot.window[0])}–${hm(slot.window[1])}${dateValue(slot.window[1]) !== dateValue(slot.window[0]) ? " (+1 доба)" : ""} · ${Math.round((slot.window[1]-slot.window[0])/60)} хв`));
            line.append(el("span", slot.jobs.length ? "" : "muted", slot.jobs.length
              ? slot.jobs.map(p => `${labPresets[p.job.preset]?.title || p.job.preset} (≈${Math.round((p.end - p.start) / 60)} хв, до ${hm(p.end)})`).join(" → ")
              : "нічого не вміщається — приймач працює"));
            return line;
          });
          if (left.length) lines.push(el("div", "lab-plan-left", `Не вміститься: ${left.map(j => labPresets[j.preset]?.title || j.preset).join(", ")} — збільште вікна або приберіть тести.`));
          preview.replaceChildren(...lines);
        } catch (error) {
          if (revision === planRevision) preview.replaceChildren(el("p", "alert small", error.message));
        }
      }, 200);
    };
    const add = el("button", "ghost small-button", "＋ вікно"); add.type = "button";
    add.addEventListener("click", () => {
      if (list.length >= 8) { note("Можна додати до 8 вікон."); return; }
      const windows = windowTimes(list), start = windows.length ? windows[windows.length-1][1] : Date.now()/1000 + 300;
      list.push([hm(start), hm(start+7200), dateValue(start)]); saveWindows(list); render();
    });
    const go = el("button", "", "Запланувати на ці вікна"); go.type = "button";
    go.title = "Лише у вікнах приймач записуватиме фрагменти без розпізнавання; потім доробить накопичене";
    go.addEventListener("click", () => action(async () => {
      const windows = windowTimes(list);
      const checked = await request("/api/lab/plan", "POST", {windows});
      if (!checked.plan.some(s => s.jobs.length)) { note("У ці вікна жоден тест не вміщається."); return; }
      await request("/api/lab/queue/start", "POST", { windows });
      note(`Заплановано: ${windows.map(w => `${hm(w[0])}–${hm(w[1])}`).join(", ")}. Поза вікнами приймач працює.`);
      await labQueue();
    }));
    const shortcuts = el("div", "lab-window-shortcuts");
    for (const hours of [1, 2, 4]) {
      const quick = el("button", "ghost small-button", `Найближчі ${hours} год`); quick.type = "button";
      quick.addEventListener("click", () => { const start = Math.ceil((Date.now()/1000+60)/300)*300; list = [[hm(start), hm(start+hours*3600), dateValue(start)]]; saveWindows(list); render(); });
      shortcuts.append(quick);
    }
    box.append(el("div", "lab-windows-head", "Коли дозволено тести без розпізнавання ефіру"), shortcuts, rows, add, preview,
      el("p", "muted small", "Місцевий час Mac. Між тестами — пауза від перегріву; у плані є запас часу. «Лише запис» зберігає звук, але не продовжує вікно: тест має встигнути до його кінця, щоб повернути розпізнавання вчасно. Поза вікнами — звичайне розпізнавання. Сторінку можна закрити."), go);
    box.update = (next, jobs) => { q = next; queued = jobs; drawPlan(); };
    labWindowEditor = box;
    render();
    return box;
  }
  let labJobFingerprint = null;
  async function labQueue(refreshResults = true) {
    let q;
    try { q = await request("/api/lab/queue"); } catch (_) { return; }
    labPresets = Object.fromEntries(q.presets.map(p => [p.key, p]));
    const fingerprint = JSON.stringify(q.jobs.map(j => [j.id, j.status, j.run_dir]));
    const changed = labJobFingerprint !== null && labJobFingerprint !== fingerprint;
    labJobFingerprint = fingerprint;
    if (refreshResults && changed && typeof view !== "undefined" && view === "experiments") await experiments(true);

    const runner = q.runner || {}, active = runner.active, jobs = q.jobs;
    const queued = jobs.filter(j => j.status === "queued"), running = jobs.find(j => j.status === "running");
    // Тривалість — оцінка з реальних прогонів (сервер), а не стале число рецепта.
    const estimate = preset => Math.round((q.estimates?.[preset] ?? (labPresets[preset]?.minutes || 30) * 60) / 60);
    const duration = m => m >= 60 ? `${Math.floor(m / 60)} год ${m % 60} хв` : `${m} хв`;
    const minutes = queued.reduce((a, j) => a + estimate(j.preset), 0) + Math.max(0, queued.length - 1) * Math.round(q.cooldown_s / 60);
    $("lab-state").textContent = active ? LAB_STATE[runner.state] || runner.state : queued.length ? `у черзі ${queued.length} · ≈${duration(minutes)}` : "черга порожня";
    renderCaptureState(q.capture);
    // Банер показує режим приймача та помилки відновлення Whisper.
    const banner = $("lab-banner");
    banner.hidden = !active && !runner.capture_error;
    if (runner.capture_error) {
      banner.textContent = `⚠ ${runner.capture_error}`;
    } else if (active) {
      const until = runner.until ? ` до ${clockTime(runner.until)}` : "";
      const windowsText = (runner.windows || []).map(w => `${clockTime(w[0])}–${clockTime(w[1])}`).join(", ");
      banner.textContent = runner.state === "waiting" ? `Черга чекає ${windowsText ? `вікон ${windowsText}` : `старту о ${clockTime(runner.start_at || runner.until)}`}. Приймач поки працює.`
        : runner.state === "cooldown" ? `Пауза між тестами${until}, щоб Mac охолов. Приймач слухає й записує фрагменти без розпізнавання.`
        : runner.state === "starting_capture" ? "Приймач слухає; чекаємо, поки Whisper вивантажиться перед тестом."
        : runner.state === "restoring_recognition" ? "Тести завершені; приймач завантажує Whisper і доробить накопичені фрагменти."
        : `Іде тест — приймач слухає й готує аудіофрагменти без розпізнавання. Очікують: ${num(q.capture?.pending_records || 0)}.`;
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
      if (job.status === "queued") bits.push(`≈${duration(estimate(job.preset))}`);
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
      if (["cancelled", "failed"].includes(job.status))
        tool("↻", "Повторити: знову в чергу", async () => { await request(`/api/lab/queue/${job.id}/retry`, "POST"); await labQueue(); });
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
      button("▶ Почати зараз", "", "Вивантажити Whisper, записувати фрагменти під час тестів, потім доробити їх", async () => {
        if (!confirm(`Запустити ${queued.length} тест(и), ≈${duration(minutes)}? Приймач продовжить слухати й записувати фрагменти; Whisper буде вивантажено.`)) return;
        await request("/api/lab/queue/start", "POST", {}); await labQueue();
      });
      controls.push(windowsEditor(q, queued));
    }
    if (active) {
      button("Зупинити після поточного", "ghost", "Дочекатися кінця тесту, що йде, і зупинити чергу", async () => { await request("/api/lab/queue/stop", "POST", { now: false }); await labQueue(); });
      button("Зупинити зараз", "ghost danger-text", "Перервати тест, що йде, і зупинити чергу", async () => {
        if (!confirm("Перервати тест, що йде? Приймач відновить розпізнавання й доробить накопичене.")) return;
        await request("/api/lab/queue/stop", "POST", { now: true }); await labQueue();
      });
    }
    if (jobs.some(j => j.status === "done"))
      button("Прибрати виконані", "link-button", "Прибрати з переліку успішно виконані (перервані лишаються — їх можна повторити)", async () => { await request("/api/lab/queue/clear", "POST"); await labQueue(); });
    $("lab-controls").replaceChildren(...controls);
    // Рецепти тестів.
    const priority = {base:0,resegment:1};
    $("lab-presets").replaceChildren(...[...q.presets].sort((a,b)=>(priority[a.key]??10)-(priority[b.key]??10)).map(p => {
      const card = el("div", "lab-preset"), text = el("div", "lab-preset-text");
      card.title = p.about;   // повний опис — у підказці; у рядку — назва й початок опису
      text.append(el("b", "", p.title), el("span", "muted small lab-preset-about", p.about));
      if (p.key==='base') text.append(el('span','badge','Базовий для порівняння'));
      if (p.key==='resegment') text.append(el('span','badge','Перевірити довгі записи'));
      const add = el("button", "ghost small-button", "＋ У чергу"); add.type = "button";
      add.title = `≈${duration(estimate(p.key))} на ${num(q.records)} розмічених записах; під час тесту приймач записуватиме фрагменти без розпізнавання`;
      add.addEventListener("click", () => action(async () => { await request("/api/lab/queue", "POST", { preset: p.key }); await labQueue(); note(`«${p.title}» — у черзі.`); }));
      card.append(text, el("span", "lab-preset-time muted small", `≈${duration(estimate(p.key))}`), add);
      return card;
    }));
  }

  function renderCaptureState(capture = {}) {
    const box = $("lab-capture-state"), a = capture.archive;
    if (!a) { box.replaceChildren(); return; }
    const latest = a.last_recorded_at ? new Date(a.last_recorded_at*1000).toLocaleString("uk-UA") : "ще немає";
    box.replaceChildren(el("p", "small", `Режим «лише запис» — зараз в архіві: ${num(a.saved)} фрагментів, ${fix(a.audio_seconds/60,1)} хв аудіо; чекають ${num(a.pending)}, оброблено ${num(a.processed)}. Останній запис: ${latest}.`),
      el("p", "small muted", "Лічильники — для збережених фрагментів усіх тестів, включно з тихими. Після обробки вони лишаються в окремому виді архіву. За тиші або без сегментів VAD кількість не зростає."));
    if (capture.write_errors) box.append(el("p", "alert small", `Помилки запису аудіо за сесію приймача: ${num(capture.write_errors)}. Самого статусу «Whisper вивантажено» недостатньо для підтвердження збереження звуку.`));
    const open = el("button", "link-button small", "Відкрити записи під час тестів"); open.type = "button";
    open.addEventListener("click", () => openRecordings({view:"captured",period:""})); box.append(open);
  }
