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
  // Плашка стану вгорі — перемикач без зайвих питань: пауза ↔ приймання, зупинений приймач — запустити.
  $("receiver-pill").addEventListener("click", () => action(async () => {
    const launcher = await request("/api/launcher").catch(() => ({}));
    if (launcher.receiver_stopped) {
      await request("/api/launcher/receiver-start", "POST");
      note("Приймач запускається (близько 10 с)…");
      setTimeout(() => action(overview), 10000);
      return;
    }
    await pauseRefresh();
    if (pauseState) await setPause(pauseState.pause_requested ? 0 : pauseMinutes);
    await overview();
  }));

  // ---------- Пошук у шапці ----------
  $("global-search").addEventListener("keydown", event => {
    if (event.key !== "Enter" || !event.target.value.trim()) return;
    event.preventDefault();
    openRecordings({search: event.target.value.trim().slice(0, 200)});
    event.target.value = "";
  });
  // Перехід в архів з чітко заданими фільтрами: попередній пошук чи вид не ховають потрібне.
  function openRecordings({view: kind = "all", period = "", search = ""} = {}) {
    $("recording-view").value = kind; $("recording-period").value = period;
    $("recording-edits").value = ""; $("recording-search").value = search;
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
      if (loaded.has("texts")) { show("texts"); action(texts); } else show("texts");
      return;
    }
    show(view);
  }
  let attentionAt = 0;
  $("suggestions-refresh").addEventListener("click", () => action(async () => {
    $("suggestions-refresh").disabled = true;
    try {
      const r = await request("/api/suggestions/refresh", "POST");
      note(r.new ? `Знайдено нових пропозицій: ${r.new} (усього ${r.total}).` : `Нових пропозицій немає (усього ${r.total}).`);
      await attention(true);
    } finally { $("suggestions-refresh").disabled = false; }
  }));
  let attentionRetry = null;
  async function attention(force = false) {
    if (!force && Date.now() - attentionAt < 15000) return;
    attentionAt = Date.now();
    let a;
    try { a = await request("/api/attention"); } catch (_) { return; }
    // Після правки перевірка йде у фоні кілька секунд — тоді оновлюємо список ще раз.
    clearTimeout(attentionRetry);
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
    if (a.codes_missing) item("warn", `Не внесено коди доби ${a.codes_day.split("-").reverse().slice(0, 2).join(".")} (діють з 20:00 попереднього дня).`, "Внести", () => show("codes"));
    if (a.approvals) item("warn", `Замін, що чекають вашого дозволу: ${a.approvals}.`, "Переглянути", () => show("learning"));
    if (a.conflicts) item("bad", `Конфліктів у навчанні: ${a.conflicts} — одне слово виправлено по-різному.`, "Переглянути", () => show("learning"));
    if (a.telegram_failed) item("bad", `Правок, що не дійшли в Telegram після всіх спроб: ${a.telegram_failed}.`, "Система", () => show("system"));
    // Пропозиції з розмітки, що накопичилися в усіх списках (фонова перевірка панелі).
    for (const g of a.suggestions || []) {
      const fresh = g.new ? ` · нових ${g.new}` : "";
      const extra = g.kind === "ref_event" ? [["Оновити всі", () => action(async () => {
        if (!confirm(`Замінити текст ${g.count} подій вашими еталонами? Зміняться журнал подій і підписи цих повідомлень у Telegram в усіх отримувачів.`)) return;
        const r = await request("/api/suggestions/sync-events", "POST");
        note(`Оновлено подій: ${r.updated}${r.failed ? `, не вдалося: ${r.failed}` : ""}${r.hidden ? `, приховані: ${r.hidden}` : ""}.`);
        await attention(true);
      })]] : [];
      // «Готово» — бачив, лишаю як є: зникає без підтвердження, нові пропозиції цього виду покажуться.
      extra.push(["✓ Готово", () => action(async () => {
        const r = await request("/api/suggestions/dismiss", "POST", {kind: g.kind});
        note(`Приховано: ${r.dismissed}. Повернути — «Показати приховані» внизу списку.`);
        await attention(true);
      })]);
      item(g.new ? "warn" : "info", `${g.title}: ${g.count}${fresh} — ${g.examples.join("; ")}${g.count > g.examples.length ? "…" : ""}`,
        "Переглянути", () => openTarget(g.target), extra, g.hint);
    }
    if (a.unlabeled_radio_24h) item("info", `Нерозмічених записів «Ефіру» за добу: ${a.unlabeled_radio_24h}.`, "Розмітити",
      () => openRecordings({view: "radio", period: "24"}));
    if (a.suggestions_hidden) {
      const row = el("div", "attention-footer");
      const b = el("button", "link-button", `Показати приховані (${a.suggestions_hidden})`); b.type = "button";
      b.addEventListener("click", () => action(async () => { await request("/api/suggestions/restore", "POST"); await attention(true); }));
      row.append(b);
      items.push(row);
    }
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
  const openRadioRecords = () => openRecordings({view: "radio"});
  function receiverTone(state) { return state === "listening" ? "ok" : state === "paused" ? "warn" : "bad"; }

  async function overview() {
    data = await request("/api/overview");
    attention();
    const d = data.diagnostics || {}, c = d.counters || {}, g = d.gauges || {}, o = data.overview;
    const state = data.receiver;
    const pill = $("receiver-pill");
    pill.className = `pill pill-button ${receiverTone(state)}`;
    pill.textContent = { listening: "Слухає ефір", paused: "Призупинено", stopped: "Зупинено", offline: "Не працює" }[state] || state;
    const age = d.updated_at ? now() - d.updated_at : null;
    $("updated").textContent = `оновлено ${new Date().toLocaleTimeString("uk-UA")}`;

    // Стан потоків беремо зі знімка лише тоді, коли він свіжий і приймач працює.
    const fresh = age !== null && age <= 120 && (state === "listening" || state === "paused");
    const health = !fresh ? "unknown" : g["asr.health"] || (g["workers.transcriber_alive"] === false ? "failing" : "ok");
    const models = [g["models.whisper"], g["models.qwen"]].filter(Boolean);
    const lastBackup = data.backups[0];
    const backupAge = lastBackup ? now() - lastBackup.created : null;
    const lastEvent = utcTs(o.last_event_at);
    const sync = data.telegram_sync || {};
    $("cards").replaceChildren(
      linked(card("Приймач", pill.textContent, d.started_at ? `сесія ${ago(now() - d.started_at)}` : "немає даних сесії", receiverTone(state)), () => show("system"), "Система: моделі, потоки, діагностика"),
      linked(card("Розпізнавання", { ok: "Працює", stuck: "Зависло", failing: "Помилки", unknown: "Немає даних" }[health] || health,
        fresh ? (models.length ? `моделі: ${models.join(" / ")}` : "") : "приймач не надсилає свіжих метрик",
        health === "ok" ? "ok" : health === "unknown" ? "" : "bad"), () => show("settings"), "Налаштування розпізнавання"),
      linked(card("Знімок метрик", age === null ? "—" : `${ago(age)} тому`, "оновлюється кожні ~30 с",
        age === null ? "" : age > 120 ? "warn" : "ok"), () => show("system"), "Система: історія діагностики"),
      linked(card("Telegram", !fresh ? "Немає даних" : g["workers.telegram_alive"] === false ? "Потік зупинено" : "Працює",
        `правок у черзі: ${num(sync.pending)}${sync.exhausted ? ` · не вдалося: ${sync.exhausted}` : ""}`,
        !fresh ? (sync.exhausted ? "bad" : "") : g["workers.telegram_alive"] === false || sync.exhausted ? "bad" : sync.pending ? "warn" : "ok"),
        () => show("system"), "Система: Telegram"),
      linked(card("Події", num(o.events), lastEvent ? `остання ${ago(now() - lastEvent)} тому` : "ще немає", "info"), openRadioRecords, "Аудіоархів: ефір"),
      linked(card("Виправлення", num(o.corrections), `правил: ${num(o.active_rules)} активних / ${num(o.total_rules)}`, "info"), () => show("learning"), "Навчання"),
      linked(card("Копія бази", backupAge === null ? "Немає" : `${ago(backupAge)} тому`, `${data.backups.length} автоматичних`,
        backupAge === null ? "bad" : backupAge > 30 * 3600 ? "warn" : "ok"), () => show("system"), "Система: резервні копії"),
      linked(card("Фільтр сповіщень", !data.samples ? "Немає зразків" : SPECTRAL_MODES[data.spectral?.mode] || "Спектральний",
        `зразків: ${data.samples} · відкинуто: ${num(c["asr.outcome.notification"] || 0)} · кандидатів: ${num(c["asr.spectral.candidate"] || 0)}`,
        data.samples ? (data.spectral?.mode === "active" ? "ok" : "info") : "warn"), () => show("filter"), "Сигнали: фільтр"),
    );
    drawEvents(data.events_by_hour);
    drawFunnel(c);
    if (view === "overview") await live();
    drawLatency(d.asr_latency?.ready || {});
    drawHealth(c, g);

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
    box.append(el("p", "muted small", `Усього ${num(total)} · розпізнано ${Math.round(((counters["asr.outcome.ready"] || 0) / total) * 100)}%`));
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
      ["Розриви приймання (сон Mac)", num(c["receiver.suspensions"] || 0), c["receiver.suspensions"] ? "caution" : "good"],
      ["Перевідкриття мікрофона", num(c["audio.stream_stalls"] || 0), c["audio.stream_stalls"] ? "caution" : "good"],
      ["Перезапуски Whisper", num(c["workers.transcriber_restarted"] || 0), bad(c["workers.transcriber_restarted"])],
      ["Черга Telegram", num(g["telegram.queue_depth"] ?? 0)],
      ["Голосові без доставки", num(c["telegram.voice_exhausted"] || 0), bad(c["telegram.voice_exhausted"])],
      ["Резервні копії з помилкою", num(c["backup.failed"] || 0), bad(c["backup.failed"])],
    ]));
  }

