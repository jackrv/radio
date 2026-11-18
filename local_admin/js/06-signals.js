  // ---------- Звуки ----------
  async function samples() {
    const list = await request("/api/samples");
    const box = $("samples");
    box.replaceChildren();
    $("samples-count").textContent = `${list.items.length} з 16`;
    if (!list.items.length) box.append(el("p", "muted", "Зразків ще немає. Без них фільтр не працює: додайте чистий сигнал кнопкою «＋ До зразків» у картці запису архіву."));
    for (const item of list.items) {
      const row = el("div", "sample-row");
      const when = item.created ? new Date(item.created * 1000).toLocaleDateString("uk-UA", { day: "2-digit", month: "2-digit" }) : "";
      row.append(el("span", "sample-name", item.id.replace(/\.wav$/, "").slice(0, 8)),
                 el("span", "muted small", [item.duration_s ? `${fix(item.duration_s, 1)} с` : "непридатний файл", when].filter(Boolean).join(" · ")));
      const play = el("button", "ghost small-button", "▶"); play.type = "button"; play.title = "Прослухати зразок";
      play.addEventListener("click", () => { const audio = el("audio"); audio.controls = true; audio.src = `/api/samples/${encodeURIComponent(item.id)}/audio`; play.replaceWith(audio); audio.play().catch(() => {}); });
      row.append(play);
      if (item.managed) {
        const remove = el("button", "danger small-button", "✕"); remove.type = "button"; remove.title = "Видалити зразок";
        remove.addEventListener("click", () => action(async () => {
          if (!confirm("Видалити цей зразок?")) return;
          await request(`/api/samples/${item.id}`, "DELETE"); await samples(); note("Зразок видалено. Діє після перезапуску приймача.");
        }));
        row.append(remove);
      }
      box.append(row);
    }
  }
  // Що фільтр відкинув як сигнал: прослухати, підтвердити або відкрити в архіві.
  const LABEL_UA = { speech: "мовлення", noise: "шум ефіру", notification: "сигнал", unclear: "нерозбірливо", room: "кімната" };
  // Типово — лише нерозмічені: позначене зникає зі списку. «Показати й розмічені» — для перегляду.
  let droppedReviewed = false;
  async function dropped() {
    const r = await request(`/api/notification-filter/dropped?limit=12${droppedReviewed ? "&reviewed=true" : ""}`);
    const box = $("dropped");
    const toggle = el("button", "link-button", droppedReviewed ? "Лише нерозмічені" : `Показати й розмічені (${r.total - r.unreviewed})`);
    toggle.type = "button";
    toggle.addEventListener("click", () => { droppedReviewed = !droppedReviewed; action(dropped); });
    const footer = el("div", "attention-footer");
    if (r.total > r.unreviewed || droppedReviewed) footer.append(toggle);
    $("dropped-count").textContent = r.unreviewed ? `нерозмічених: ${r.unreviewed}` : "усе розмічено";
    if (!r.items.length) {
      box.replaceChildren(el("p", "muted", r.total ? "Усе відкинуте вже розмічено." : "Фільтр ще нічого не відкидав."), footer);
      return;
    }
    box.replaceChildren(...r.items.map(item => {
      const row = el("div", `live-row ${item.reviewed ? (item.label === "notification" ? "ok" : "bad") : "info"}`);
      const time = new Date(item.created * 1000).toLocaleString("uk-UA", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
      const main = el("div", "live-text");
      main.append(el("span", "live-time", time), el("span", "", `${fix(item.duration_s, 1)} с · схожість ${fix(item.score)}`));
      const meta = el("div", "live-meta");
      if (item.reviewed) meta.append(el("span", `badge ${item.label === "notification" ? "ok" : "bad"}`, `розмічено: ${LABEL_UA[item.label] || item.label}`));
      const play = el("button", "ghost small-button", "▶"); play.type = "button";
      play.addEventListener("click", () => { const audio = el("audio"); audio.controls = true; audio.src = `/api/recordings/${item.id}/audio`; play.replaceWith(audio); audio.play().catch(() => {}); });
      meta.append(play);
      if (!item.reviewed) {
        const yes = el("button", "ghost small-button", "✓ Сигнал"); yes.type = "button"; yes.title = "Так, це звук сповіщення без мовлення";
        yes.addEventListener("click", () => action(async () => {
          await request(`/api/recordings/${item.id}/review`, "PUT", { reference: "", label: "notification", expected_revision: item.revision });
          note("Позначено як сигнал."); await dropped();   // розмічене зникає зі списку
        }));
        meta.append(yes);
      }
      const open = el("button", "ghost small-button", "Розмітити"); open.type = "button"; open.title = "Відкрити в «Аудіоархіві» (якщо там мовлення)";
      open.addEventListener("click", () => openRecordings({search: item.id.slice(0, 8)}));
      meta.append(open);
      row.append(main, meta);
      return row;
    }), footer);
  }
  function selected() { const f = $("sample-file").files[0]; if (!f) throw new Error("Вибери WAV-файл."); if (f.size > 200000) throw new Error("Файл завеликий."); return f; }
  $("sample-add").addEventListener("click", () => action(async () => {
    if (!$("sample-confirm").checked) throw new Error("Спершу підтвердь відсутність мовлення у зразку.");
    await request("/api/samples", "PUT", selected(), { "X-Confirm-Isolated": "yes", "Content-Type": "audio/wav" });
    await samples(); note("Зразок додано. Діє після перезапуску приймача.");
  }));
  $("sample-test").addEventListener("click", () => action(async () => {
    const r = await request("/api/samples/test", "POST", selected(), { "Content-Type": "audio/wav" });
    note(r.isolated_match ? "Спектральний збіг знайдено. Відкидання залежить від режиму фільтра." : "Збігу немає: сегмент пішов би у Whisper.");
  }));

  // ---------- Налаштування ----------
  let current;
  // ---------- Фільтр сповіщень ----------
  async function filterView() {
    const f = await request("/api/notification-filter");
    const c = f.counters || {}, sp = f.spectral, obs = f.observed || {};
    const recognised = (c["asr.spectral.candidate.ready"] || 0) + (c["asr.spectral.candidate.ready_fallback"] || 0);
    const age = f.updated_at ? now() - f.updated_at : null;
    const reveal = (id, openDetails) => () => {
      const target = $(id);
      if (openDetails) target.closest?.("details")?.setAttribute("open", "");
      target.scrollIntoView?.({ behavior: "smooth", block: "start" });
    };
    const dropped_n = c["asr.outcome.notification"] || 0;
    $("filter-cards").replaceChildren(
      linked(card("Режим", SPECTRAL_MODES[sp.mode] || sp.mode, sp.mode === "shadow" ? "лише оцінює, нічого не відкидає" : sp.mode === "active" ? "відкидає кандидатів" : "оцінки не збираються",
        sp.mode === "active" ? "ok" : sp.mode === "shadow" ? "info" : "warn"), reveal("filter-how", true), "Як працює фільтр"),
      linked(card("Зразків", num(f.samples), "WAV-зразки спектрального фільтра", f.samples ? "ok" : "bad"), reveal("samples-panel"), "Зразки звуків"),
      linked(card("Відкинуто як сигнал", num(dropped_n), `цей запуск · оцінено ${num(c["asr.spectral.scored"] || 0)}${age === null ? "" : ` · знімок ${ago(age)} тому`}`, dropped_n ? "ok" : "info"), reveal("dropped-panel"), "Останні відкинуті"),
      linked(card("Кандидатів", num(c["asr.spectral.candidate"] || 0), `із них розпізнано Whisper: ${num(recognised)}`, recognised ? "warn" : ""), reveal("filter-recent", true), "Високі збіги"),
      linked(card("Розмічено в архіві", `${num(f.labels.notification)} / ${num(f.labels.speech)}`, `сигналів / мовлення · шуму: ${num(f.labels.noise)}`, f.labels.notification && f.labels.speech ? "ok" : "warn"),
        () => { reveal("filter-eval")(); action(evaluation); }, "Оцінити фільтр на розмітці"),
      linked(card("Поріг", fix(sp.threshold), `поза збігом ≤ ${fix(sp.max_outside_s, 1)} с · стороннє ≤ ${pct(sp.max_extra)}`, ""),
        () => { reveal("filter-eval")(); action(evaluation); }, "Перевірити поріг на розмітці"),
    );
    await Promise.all([samples(), dropped()]);
    drawHistogram(obs.buckets || [], sp.threshold);
    drawRecent((obs.recent || []).slice(-50).reverse());
    $("filter-labels").textContent = f.samples
      ? `Мітки беруться з «Аудіоархіву»: звук сповіщення — ${f.labels.notification}, мовлення й нерозбірливе — ${f.labels.speech}, шум — ${f.labels.noise}. Для рекомендації порогу потрібно щонайменше 3 сигнали й 10 записів мовлення.`
      : "Спершу додайте хоча б один зразок звуку сповіщення.";
  }

  function drawHistogram(buckets, threshold) {
    const W = 720, H = 160, n = buckets.length || 20, bw = W / n, max = Math.max(1, ...buckets);
    const chart = svg("svg", { viewBox: `0 0 ${W} ${H + 20}`, role: "img", "aria-label": "Розподіл спектральних оцінок" });
    buckets.forEach((count, i) => {
      const h = count ? Math.max(3, (count / max) * (H - 8)) : 0;
      const r = svg("rect", { class: `bar ${(i + 1) / n > threshold ? "hi" : ""}`, x: i * bw + 2, y: H - h, width: bw - 4, height: h, rx: 3 });
      r.append(svg("title")); r.firstChild.textContent = `${(i / n).toFixed(2)}–${((i + 1) / n).toFixed(2)}: ${count}`;
      chart.append(r);
    });
    for (const v of [0, 0.25, 0.5, 0.75, 1]) {
      const t = svg("text", { class: "axis", x: Math.min(W - 14, Math.max(0, v * W - 8)), y: H + 15 }); t.textContent = v.toFixed(2); chart.append(t);
    }
    chart.append(svg("line", { class: "thr", x1: threshold * W, x2: threshold * W, y1: 0, y2: H }));
    const total = buckets.reduce((a, b) => a + b, 0);
    $("filter-hist").replaceChildren(total ? chart : el("p", "muted", "Оцінок ще немає: потрібні зразки й запущений приймач."));
  }

  function drawRecent(rows) {
    const table = el("table"), head = el("tr");
    for (const h of ["Час", "Тривалість", "Оцінка", "Поза збігом", "Стороннє", "Кандидат", "Що сталося"]) head.append(el("th", "", h));
    table.append(head);
    for (const r of rows) {
      const row = el("tr");
      if (r.notification_candidate && (r.status === "ready" || r.status === "ready_fallback")) row.className = "now";
      const cells = [r.processing_started_unix_s ? stamp(r.processing_started_unix_s) : "—", `${fix(r.audio_duration_s, 1)} с`,
        fix(r.notification_score), `${fix(r.notification_outside_s)} с`, pct(r.notification_extra), yes(r.notification_candidate), STATUS[r.status] || r.status];
      for (const v of cells) row.append(el("td", "", v));
      table.append(row);
    }
    $("filter-recent").replaceChildren(rows.length ? table : el("p", "muted", "Високих збігів у цьому запуску ще не було."));
    if (rows.length) $("filter-recent").append(el("p", "muted small", "Підсвічено кандидатів, які Whisper розпізнав як фразу: саме їх змінить активний режим. Прослухайте ці фрази й позначте."));
  }

  async function evaluation() {
    note("Оцінюємо розмічені записи…");
    const r = await request("/api/notification-filter/evaluation");
    const s = r.summary, box = el("div", "summary");
    if (!r.templates) { box.classList.add("bad"); box.textContent = "Немає зразків — нема з чим порівнювати."; }
    else if (s.status === "ok") {
      // Висновок простими словами: чи варто щось міняти. Зміна — у config.py (попросіть Claude).
      const now = s.current.notifications_found, best = s.best;
      const better = best && best.notifications_found - now >= Math.max(3, now * 0.1);
      box.classList.add(s.current.speech_dropped ? "bad" : better ? "warn" : "ok");
      const head = `Зараз фільтр ловить ${now} з ${s.notifications} розмічених сигналів` +
        (s.current.speech_dropped ? ` і помилково відкидає мовлення: ${s.current.speech_dropped} з ${s.speech}.` : `, мовлення не зачіпає (0 з ${s.speech}).`);
      box.textContent = better
        ? `${head} Можна ловити ${best.notifications_found}: поріг ${fix(best.threshold)} і стороння енергія до ${pct(best.max_extra)} — мовлення теж не зачепить. Щоб застосувати — «Розпізнавання → Налаштування → Фільтр сигналів сповіщень».`
        : `${head} Налаштування близькі до найкращих для цієї розмітки — нічого міняти не треба.`;
    } else if (s.status === "need_more") {
      box.classList.add("warn");
      box.textContent = `Замало розмітки для рекомендації: потрібно щонайменше 3 сповіщення й 10 фраз мовлення (зараз ${s.notifications} / ${s.speech}). Таблиця нижче — попередня.`;
    } else {
      box.classList.add("bad");
      box.textContent = "Жоден поріг не відділяє сповіщення від мовлення без втрат. Додайте точніші зразки або залиште режим спостереження.";
    }
    $("filter-summary").replaceChildren(box);

    const grid = el("table"), gh = el("tr");
    for (const h of ["Поріг", `Сигналів знайдено (з ${s.notifications})`, `Мовлення відкинуто (з ${s.speech})`, `Шуму відкинуто (з ${s.noise})`]) gh.append(el("th", "", h));
    grid.append(gh);
    for (const t of s.table) {
      const row = el("tr");
      if (t.threshold === s.recommended_threshold) row.classList.add("pick");
      if (Math.abs(t.threshold - s.current_threshold) < 1e-9) row.classList.add("now");
      row.append(el("td", "", fix(t.threshold)), el("td", "", num(t.notifications_found)), el("td", t.speech_dropped ? "alert" : "good", num(t.speech_dropped)), el("td", "", num(t.noise_dropped)));
      grid.append(row);
    }
    $("filter-table").replaceChildren(grid);
    if (s.extras?.length) {
      // Ліміт сторонньої енергії: шум ефіру під сигналом. Для кожного — найкращий безпечний поріг.
      const ex = el("table"), eh = el("tr");
      for (const h of ["Стороння енергія до", "Поріг", `Сигналів знайдено (з ${s.notifications})`]) eh.append(el("th", "", h));
      ex.append(eh);
      for (const e of s.extras) {
        const row = el("tr");
        if (s.best && e.max_extra === s.best.max_extra) row.classList.add("pick");
        if (Math.abs(e.max_extra - s.current_max_extra) < 1e-9) row.classList.add("now");
        row.append(el("td", "", pct(e.max_extra)), el("td", "", fix(e.threshold)), el("td", "", num(e.notifications_found)));
        ex.append(row);
      }
      $("filter-table").append(el("p", "muted small", "Мовлення на всіх варіантах нижче — 0. Під сигналом в ефірі буває шум: суворий ліміт сторонньої енергії відкидає такі сигнали."), ex);
    }

    const items = el("table"), ih = el("tr");
    for (const h of ["Запис", "Позначка", "Оцінка", "Поза", "Стороннє", "Спектр"]) ih.append(el("th", "", h));
    items.append(ih);
    for (const i of r.items) {
      const row = el("tr"), name = el("td", "", `${stamp(i.created)}${i.duration_s ? ` · ${fix(i.duration_s, 1)} с` : ""}`);
      if (i.self_template) name.append(el("span", "muted small", " · зразок, себе не враховано"));
      // Помилка: сигнал не знайдено або мовлення відкинуто; шум відкидати можна.
      const scored = i.score !== undefined && i.score !== null;
      const wrong = scored && (i.label === "notification" ? !i.candidate : i.label !== "noise" && i.candidate);
      row.append(name, el("td", "", { notification: "🔔 сигнал", speech: "🗣 мовлення", unclear: "🗣 нерозбірливе", noise: "· шум" }[i.label] || i.label));
      if (i.error || i.score === null || i.score === undefined) row.append(el("td", "muted", i.error || "не оцінено"), el("td"), el("td"), el("td"));
      else row.append(el("td", "", fix(i.score)), el("td", "", `${fix(i.outside_s)} с`), el("td", "", pct(i.extra_ratio)),
        el("td", wrong ? "alert" : "good", yes(i.candidate)));
      items.append(row);
    }
    $("filter-items").replaceChildren(r.items.length ? items : el("p", "muted", "Розмічених записів ще немає. Позначайте їх у розділі «Аудіоархів»."));
    note(`Оцінено записів: ${r.items.length}.`);
  }
  $("filter-eval").addEventListener("click", () => action(evaluation));

