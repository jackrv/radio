"use strict";
// Radio 345 · локальна панель. Лише 127.0.0.1; дані з того самого Mac.
(() => {
  const $ = id => document.getElementById(id);
  const REFRESH_MS = 10000;
  let busy = false, data = null, view = "overview", phraseOffset = 0, timer = null;
  const loaded = new Set();

  const STATUS = {
    ready: "Розпізнано", low_rms: "Тихо (нижче MIN_RMS)", short: "Закоротко (VAD)",
    notification: "Звук сповіщення", blacklist: "Чорний список", garbage: "Сміття Whisper",
    empty_text: "Порожній текст", whisper_error: "Помилка Whisper", stale: "Після паузи", discarded: "Відкинуто",
  };
  const SPECTRAL_MODES = { shadow: "Спостереження", active: "Активний", off: "Спектральний вимкнено" };
  const pct = v => (typeof v === "number" ? `${(v * 100).toFixed(1)}%` : "—");
  const yes = v => (v ? "так" : "ні");
  const ROLES = { admin: "Адміністратор", editor: "Редактор", viewer: "Спостерігач" };
  const MODES = { all: "усі фрази", watch: "спостереження", commands: "команди й позивні", off: "вимкнено" };
  const LABELS = { USE_LLM: "Корекція Qwen", VAD_LEVEL: "Рівень VAD (0–3)", SILENCE_END_MS: "Тиша до завершення, мс",
    MIN_SPEECH_MS: "Мінімальне мовлення, мс", MAX_SEGMENT_S: "Максимальний сегмент, с" };
  const LATENCY = { total_latency_s: "До готового тексту", queue_wait_s: "Очікування в черзі", whisper_s: "Whisper",
    qwen_s: "Qwen", speech_tail_s: "Завершення фрази (VAD)", rtf: "RTF (час / тривалість)" };

  // ---------- Допоміжне ----------
  const note = text => { $("feedback").textContent = text; };
  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
  function svg(tag, attrs) { const e = document.createElementNS("http://www.w3.org/2000/svg", tag); for (const [k, v] of Object.entries(attrs || {})) e.setAttribute(k, v); return e; }
  const num = v => (typeof v === "number" ? v.toLocaleString("uk-UA") : "—");
  const fix = (v, d = 2) => (typeof v === "number" ? v.toFixed(d) : "—");
  function ago(seconds) {
    if (typeof seconds !== "number" || !isFinite(seconds)) return "—";
    if (seconds < 60) return `${Math.max(0, Math.round(seconds))} с`;
    if (seconds < 3600) return `${Math.round(seconds / 60)} хв`;
    if (seconds < 86400) return `${Math.floor(seconds / 3600)} год ${Math.round((seconds % 3600) / 60)} хв`;
    return `${Math.floor(seconds / 86400)} д ${Math.floor((seconds % 86400) / 3600)} год`;
  }
  const now = () => Date.now() / 1000;
  const stamp = unix => new Date(unix * 1000).toLocaleString("uk-UA", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
  function utcTs(text) { return text ? Date.parse(text.replace(" ", "T") + "Z") / 1000 : null; }

  async function request(path, method = "GET", body, headers = {}) {
    const options = { method, headers, credentials: "same-origin", cache: "no-store" };
    if (body !== undefined) {
      if (body instanceof Blob) options.body = body;
      else { options.body = JSON.stringify(body); options.headers = { ...headers, "Content-Type": "application/json" }; }
    }
    const response = await fetch(path, options);
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(typeof error.detail === "string" ? error.detail : `HTTP ${response.status}. Онови сторінку або відкрий панель командою --open.`);
    }
    if (response.status === 204) return null;
    return (response.headers.get("content-type") || "").includes("json") ? response.json() : response.blob();
  }
  async function action(callback) { if (busy) return; busy = true; try { await callback(); } catch (error) { note(error.message); } finally { busy = false; } }

  // ---------- Навігація ----------
  const titles = { recordings: "Аудіоархів", overview: "Огляд", phrases: "Фрази", sounds: "Звуки сповіщень", filter: "Фільтр сповіщень", texts: "Фільтри тексту", settings: "Налаштування", users: "Користувачі", system: "Система", roles: "Ролі" };
  function show(name) {
    view = name;
    for (const b of document.querySelectorAll("#nav button")) b.classList.toggle("active", b.dataset.view === name);
    for (const s of document.querySelectorAll("[data-panel]")) s.hidden = s.dataset.panel !== name;
    $("view-title").textContent = titles[name];
    if (!loaded.has(name)) { loaded.add(name); action(() => loaders[name]()); }
  }
  for (const b of document.querySelectorAll("#nav button")) b.addEventListener("click", () => show(b.dataset.view));

  // ---------- Огляд ----------
  function card(label, value, detail, tone) {
    const c = el("div", `card ${tone || ""}`);
    c.append(el("div", "label", label), el("div", "value", value), el("div", "detail", detail || ""));
    return c;
  }
  function receiverTone(state) { return state === "listening" ? "ok" : state === "paused" ? "warn" : "bad"; }

  async function overview() {
    data = await request("/api/overview");
    const d = data.diagnostics || {}, c = d.counters || {}, g = d.gauges || {}, o = data.overview;
    const state = data.receiver;
    const pill = $("receiver-pill");
    pill.className = `pill ${receiverTone(state)}`;
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
      card("Приймач", pill.textContent, d.started_at ? `сесія ${ago(now() - d.started_at)}` : "немає даних сесії", receiverTone(state)),
      card("Розпізнавання", { ok: "Працює", stuck: "Зависло", failing: "Помилки", unknown: "Немає даних" }[health] || health,
        fresh ? (models.length ? `моделі: ${models.join(" / ")}` : "") : "приймач не надсилає свіжих метрик",
        health === "ok" ? "ok" : health === "unknown" ? "" : "bad"),
      card("Знімок метрик", age === null ? "—" : `${ago(age)} тому`, "оновлюється кожні ~30 с",
        age === null ? "" : age > 120 ? "warn" : "ok"),
      card("Telegram", !fresh ? "Немає даних" : g["workers.telegram_alive"] === false ? "Потік зупинено" : "Працює",
        `правок у черзі: ${num(sync.pending)}${sync.exhausted ? ` · не вдалося: ${sync.exhausted}` : ""}`,
        !fresh ? (sync.exhausted ? "bad" : "") : g["workers.telegram_alive"] === false || sync.exhausted ? "bad" : sync.pending ? "warn" : "ok"),
      card("Події", num(o.events), lastEvent ? `остання ${ago(now() - lastEvent)} тому` : "ще немає", "info"),
      card("Виправлення", num(o.corrections), `правил: ${num(o.active_rules)} активних / ${num(o.total_rules)}`, "info"),
      card("Копія бази", backupAge === null ? "Немає" : `${ago(backupAge)} тому`, `${data.backups.length} автоматичних`,
        backupAge === null ? "bad" : backupAge > 30 * 3600 ? "warn" : "ok"),
      card("Фільтр сповіщень", !data.samples ? "Немає зразків" : SPECTRAL_MODES[data.spectral?.mode] || "Хвильовий",
        `зразків: ${data.samples} · відкинуто: ${num(c["asr.outcome.notification"] || 0)} · кандидатів: ${num(c["asr.spectral.candidate"] || 0)}`,
        data.samples ? (data.spectral?.mode === "active" ? "ok" : "info") : "warn"),
    );
    drawEvents(data.events_by_hour);
    drawFunnel(c);
    drawTimeline(d.asr_latency?.recent_segments || []);
    drawLatency(d.asr_latency?.ready || {});
    drawHealth(c, g);
    if (view === "users") users();
    if (view === "system") await system();
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

  function drawTimeline(segments) {
    const W = 720, H = 110, n = Math.max(segments.length, 1), bw = W / Math.max(n, 40);
    const maxDur = Math.max(1, ...segments.map(s => s.audio_duration_s || 0));
    const chart = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Останні сегменти" });
    segments.forEach((s, i) => {
      const h = Math.max(4, ((s.audio_duration_s || 0) / maxDur) * (H - 6));
      const r = svg("rect", { class: `st-${s.status}`, x: i * bw + 1, y: H - h, width: Math.max(2, bw - 2), height: h, rx: 2 });
      r.append(svg("title"));
      r.firstChild.textContent = `#${s.segment_id} · ${STATUS[s.status] || s.status} · ${fix(s.audio_duration_s, 1)} с` +
        (s.total_latency_s ? ` · готово за ${fix(s.total_latency_s)} с` : "");
      chart.append(r);
    });
    $("timeline").replaceChildren(segments.length ? chart : el("p", "muted", "Сегментів ще немає."));
    const used = [...new Set(segments.map(s => s.status))];
    $("timeline-legend").replaceChildren(...used.map(s => { const span = el("span", "", STATUS[s] || s); const i = el("i", `st-${s}`); span.prepend(i); return span; }));
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

  // ---------- Фрази ----------
  async function phrases(reset = true) {
    if (reset) { phraseOffset = 0; $("phrases").replaceChildren(); }
    const q = encodeURIComponent($("phrase-query").value.trim());
    const page = await request(`/api/phrases?q=${q}&offset=${phraseOffset}&limit=30`);
    for (const item of page.items) $("phrases").append(phraseItem(item));
    phraseOffset += page.items.length;
    $("phrase-more").hidden = !page.has_more;
    if (!phraseOffset) $("phrases").append(el("p", "muted", "Фраз не знайдено."));
  }
  function phraseItem(item) {
    const box = el("div", "item"), meta = el("div", "meta");
    const created = utcTs(item.created_at);
    meta.append(el("span", "", created ? stamp(created) : item.created_at || ""), el("span", "", item.fix_id.startsWith("db:") ? `подія ${item.fix_id.slice(3)}` : "тимчасова фраза"));
    if (item.spectral) {
      const sp = item.spectral;
      const badge = el("span", `badge ${sp.candidate ? "cand" : ""}`, `спектр ${fix(sp.score)}${sp.candidate ? " · кандидат" : ""}`);
      badge.title = `Поза збігом: ${fix(sp.outside_s)} с · стороння енергія: ${pct(sp.extra_ratio)}`;
      meta.append(badge);
    }
    const labelBadge = el("span", "badge", "");
    meta.append(labelBadge);
    box.append(meta, el("div", "text", item.current_text));
    if (item.original_text && item.original_text !== item.current_text) box.append(el("div", "orig", `Whisper: ${item.original_text}`));
    const actions = el("div", "actions");
    if (item.has_audio) {
      const play = el("button", "ghost", "▶ Аудіо");
      const notif = el("button", "ghost", "🔔 Сповіщення"), speech = el("button", "ghost", "🗣 Мовлення");
      const mark = el("button", "ghost", "＋ До зразків");
      const paint = label => {
        item.label = label;
        notif.classList.toggle("on", label === "notification");
        speech.classList.toggle("on", label === "speech");
        labelBadge.hidden = !label;
        labelBadge.className = `badge ${label === "notification" ? "notif" : "speech"}`;
        labelBadge.textContent = label === "notification" ? "позначено: сповіщення" : "позначено: мовлення";
      };
      paint(item.label || null);
      play.addEventListener("click", () => action(async () => {
        const blob = await request(`/api/phrases/audio?fix_id=${encodeURIComponent(item.fix_id)}`);
        const audio = el("audio"); audio.controls = true; audio.src = URL.createObjectURL(blob);
        play.replaceWith(audio); audio.play().catch(() => {});
      }));
      for (const [button, label] of [[notif, "notification"], [speech, "speech"]]) {
        button.addEventListener("click", () => action(async () => {
          const next = item.label === label ? null : label;
          await request("/api/phrases/label", "PUT", { fix_id: item.fix_id, label: next });
          paint(next);
          loaded.delete("filter");
          note(next ? "Позначку збережено. Перевірка — у розділі «Фільтр»." : "Позначку знято.");
        }));
      }
      mark.addEventListener("click", () => action(async () => {
        if (!confirm("Додати цей фрагмент до зразків звуків сповіщень?\n\nСпершу прослухайте його: у записі не має бути мовлення. Зразок — еталон фільтра; діє після перезапуску приймача.")) return;
        await request("/api/phrases/notification-sample", "PUT", { fix_id: item.fix_id, confirmed_isolated: true });
        mark.disabled = true; mark.textContent = "✓ Додано до зразків";
        loaded.delete("sounds"); loaded.delete("filter");
        note("Зразок додано. Набуде чинності після перезапуску приймача.");
      }));
      actions.append(play, notif, speech, mark);
    } else { labelBadge.hidden = true; actions.append(el("span", "muted small", "аудіо не збережено")); }
    box.append(actions);
    return box;
  }
  $("phrase-search").addEventListener("click", () => action(() => phrases(true)));
  $("phrase-query").addEventListener("keydown", e => { if (e.key === "Enter") action(() => phrases(true)); });
  $("phrase-more").addEventListener("click", () => action(() => phrases(false)));

  // ---------- Звуки ----------
  async function samples() {
    const list = await request("/api/samples");
    $("samples").replaceChildren();
    if (!list.items.length) $("samples").append(el("p", "muted", "Зразків ще немає. Додайте WAV або позначте фрагмент у розділі «Фрази»."));
    for (const item of list.items) {
      const row = el("div", "item"), actions = el("div", "actions");
      row.append(el("div", "text", `${item.id}`), el("div", "orig", item.duration_s ? `${fix(item.duration_s)} с` : "непридатний файл"));
      if (item.managed) {
        const remove = el("button", "danger", "Видалити");
        remove.addEventListener("click", () => action(async () => {
          if (!confirm("Видалити цей зразок?")) return;
          await request(`/api/samples/${item.id}`, "DELETE"); await samples(); note("Зразок видалено. Діє після перезапуску приймача.");
        }));
        actions.append(remove);
      }
      row.append(actions); $("samples").append(row);
    }
  }
  function selected() { const f = $("sample-file").files[0]; if (!f) throw new Error("Вибери WAV-файл."); if (f.size > 200000) throw new Error("Файл завеликий."); return f; }
  $("sample-add").addEventListener("click", () => action(async () => {
    if (!$("sample-confirm").checked) throw new Error("Спершу підтвердь відсутність мовлення у зразку.");
    await request("/api/samples", "PUT", selected(), { "X-Confirm-Isolated": "yes", "Content-Type": "audio/wav" });
    await samples(); note("Зразок додано. Діє після перезапуску приймача.");
  }));
  $("sample-test").addEventListener("click", () => action(async () => {
    const r = await request("/api/samples/test", "POST", selected(), { "Content-Type": "audio/wav" });
    note(r.isolated_match ? "Збіг знайдено: фільтр відкинув би цей сегмент." : "Збігу немає: сегмент пішов би у Whisper.");
  }));

  // ---------- Налаштування ----------
  let current;
  // ---------- Фільтр сповіщень ----------
  async function filterView() {
    const f = await request("/api/notification-filter");
    const c = f.counters || {}, sp = f.spectral, obs = f.observed || {};
    const recognised = (c["asr.spectral.candidate.ready"] || 0) + (c["asr.spectral.candidate.ready_fallback"] || 0);
    const age = f.updated_at ? now() - f.updated_at : null;
    $("filter-cards").replaceChildren(
      card("Режим", SPECTRAL_MODES[sp.mode] || sp.mode, sp.mode === "shadow" ? "лише оцінює, нічого не відкидає" : sp.mode === "active" ? "відкидає кандидатів" : "оцінки не збираються",
        sp.mode === "active" ? "ok" : sp.mode === "shadow" ? "info" : "warn"),
      card("Зразків", num(f.samples), f.waveform ? "хвильовий фільтр теж працює" : "хвильовий фільтр вимкнено", f.samples ? "ok" : "bad"),
      card("Оцінено сегментів", num(c["asr.spectral.scored"] || 0), age === null ? "немає знімка метрик" : `знімок ${ago(age)} тому`, "info"),
      card("Кандидатів", num(c["asr.spectral.candidate"] || 0), `із них розпізнано Whisper: ${num(recognised)}`, recognised ? "warn" : ""),
      card("Розмічено", `${num(f.labels.notification)} / ${num(f.labels.speech)}`, "сповіщень / мовлення", f.labels.notification && f.labels.speech ? "ok" : "warn"),
      card("Поріг", fix(sp.threshold), `поза збігом ≤ ${fix(sp.max_outside_s, 1)} с · стороннє ≤ ${pct(sp.max_extra)}`, ""),
    );
    drawHistogram(obs.buckets || [], sp.threshold);
    drawRecent((obs.recent || []).slice(-50).reverse());
    $("filter-labels").textContent = f.samples
      ? `Розмічено: сповіщення — ${f.labels.notification}, мовлення — ${f.labels.speech}. Для рекомендації порогу потрібно щонайменше 3 і 10.`
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
    $("filter-hist").replaceChildren(total ? chart : el("p", "muted", "Оцінок ще немає: потрібні зразки й перезапущений приймач у режимі спостереження."));
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
      box.classList.add("ok");
      box.textContent = `Рекомендований поріг ${fix(s.recommended_threshold)}: знайде ${s.recommended.notifications_found} з ${s.notifications} сповіщень, мовлення не зачепить (0 з ${s.speech}). ` +
        `Поточний ${fix(s.current_threshold)}: знайде ${s.current.notifications_found}, хибних на мовленні — ${s.current.speech_dropped}.`;
    } else if (s.status === "need_more") {
      box.classList.add("warn");
      box.textContent = `Замало розмітки для рекомендації: потрібно щонайменше 3 сповіщення й 10 фраз мовлення (зараз ${s.notifications} / ${s.speech}). Таблиця нижче — попередня.`;
    } else {
      box.classList.add("bad");
      box.textContent = "Жоден поріг не відділяє сповіщення від мовлення без втрат. Додайте точніші зразки або залиште режим спостереження.";
    }
    $("filter-summary").replaceChildren(box);

    const grid = el("table"), gh = el("tr");
    for (const h of ["Поріг", `Сповіщень знайдено (з ${s.notifications})`, `Мовлення відкинуто (з ${s.speech})`]) gh.append(el("th", "", h));
    grid.append(gh);
    for (const t of s.table) {
      const row = el("tr");
      if (t.threshold === s.recommended_threshold) row.classList.add("pick");
      if (Math.abs(t.threshold - s.current_threshold) < 1e-9) row.classList.add("now");
      row.append(el("td", "", fix(t.threshold)), el("td", "", num(t.notifications_found)), el("td", t.speech_dropped ? "alert" : "good", num(t.speech_dropped)));
      grid.append(row);
    }
    $("filter-table").replaceChildren(grid);

    const items = el("table"), ih = el("tr");
    for (const h of ["Фраза", "Позначка", "Оцінка", "Поза", "Стороннє", "Спектр", "Хвиля"]) ih.append(el("th", "", h));
    items.append(ih);
    for (const i of r.items) {
      const row = el("tr"), name = el("td", "", i.fix_id.startsWith("db:") ? `подія ${i.fix_id.slice(3)}` : "тимчасова");
      if (i.self_template) name.append(el("span", "muted small", " · зразок, себе не враховано"));
      const wrong = i.score !== undefined && i.score !== null && (i.label === "speech" ? i.candidate : !i.candidate);
      row.append(name, el("td", "", i.label === "notification" ? "🔔 сповіщення" : "🗣 мовлення"));
      if (i.error || i.score === null || i.score === undefined) row.append(el("td", "muted", i.error || "не оцінено"), el("td"), el("td"), el("td"), el("td"));
      else row.append(el("td", "", fix(i.score)), el("td", "", `${fix(i.outside_s)} с`), el("td", "", pct(i.extra_ratio)),
        el("td", wrong ? "alert" : "good", yes(i.candidate)), el("td", "", yes(i.waveform_match)));
      items.append(row);
    }
    $("filter-items").replaceChildren(r.items.length ? items : el("p", "muted", "Розмічених записів ще немає. Позначайте фрази в розділі «Фрази»."));
    note(`Оцінено записів: ${r.items.length}.`);
  }
  $("filter-eval").addEventListener("click", () => action(evaluation));

  // ---------- Текстові списки ----------
  const TEXT_LISTS = {
    blacklist: ["Фрази-галюцинації Whisper", "Відкидається лише фраза, що повністю збігається з рядком (регістр і пунктуація не важливі). Наприклад: «дякую за перегляд»."],
    short_garbage: ["Короткий шум", "Короткі хибні фрагменти. Відкидаються, лише якщо у фразі немає позивних, команд і слів спостереження."],
    aliases: ["Алиаси тексту", "Формат «правильне: варіант, варіант». Варіант замінюється на правильне написання перед пошуком позивних і команд, наприклад «наган: на ган, ган»."],
    callsign_aliases: ["Алиаси позивних", "Формат «позивний: почутий варіант». Діє лише для пошуку позивного з номером; позивний має бути у словнику."],
  };
  let textRevision = null;

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

  async function texts() {
    const data = await request("/api/text-lists");
    textRevision = data.revision;
    const a = data.applied || {};
    const live = a.receiver === "listening" || a.receiver === "paused";
    $("texts-status").textContent = `Збережена ревізія: ${data.revision}. ` + (!live
      ? "Приймач не працює — списки застосуються під час запуску."
      : a.revision === data.revision ? "Приймач уже використовує цю ревізію."
      : `Приймач: ревізія ${a.revision ?? "—"} (знімок метрик оновлюється ~30 с; самі списки застосовуються за кілька секунд).`);
    const box = $("text-lists");
    box.replaceChildren();
    for (const [type, [title, help]] of Object.entries(TEXT_LISTS)) {
      const list = data.lists[type];
      const panel = el("article", "panel");
      const area = el("textarea", "list-editor");
      area.value = toLines(list.entries);
      area.spellcheck = false;
      area.setAttribute("aria-label", title);
      const save = el("button", "", "Зберегти"), status = el("span", "muted small", `${list.entries.filter(e => e.enabled).length} діючих рядків`);
      const warnings = el("ul", "warn-list");
      for (const w of list.warnings || []) warnings.append(el("li", "", w));
      save.addEventListener("click", () => action(async () => {
        try {
          const r = await request(`/api/text-lists/${type}`, "PUT", { entries: fromLines(area.value), expected_revision: textRevision });
          textRevision = r.revision;
          note(`«${title}» збережено (ревізія ${r.revision}). Приймач застосує зміни за кілька секунд.`);
          await texts();
        } catch (error) { status.textContent = error.message; status.className = "alert small"; throw error; }
      }));
      const actions = el("div", "list-actions"); actions.append(save, status);
      panel.append(el("h2", "", title), el("p", "muted small", help), area, actions, warnings);
      box.append(panel);
    }
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
      $("fields").append(label);
    }
  }
  // ---------- Обробка без перезапуску ----------
  const RUNTIME_LABELS = {
    COOLDOWN_S: "Пауза між однаковими повідомленнями, с",
    LOG_CALLSIGNS_ONLY: "Записувати події з позивними навіть без команд",
    GARBAGE_MIN_WORDS: "Сміття Whisper: перевіряти фрази від N слів",
    GARBAGE_MIN_UNIQUE_RATIO: "Сміття: мінімальна частка різних слів (0–1)",
    GARBAGE_MAX_WORDS_PER_S: "Сміття: максимум слів за секунду аудіо",
    GARBAGE_EXTRA_WORDS: "Сміття: запас слів понад цю норму",
  };
  let runtimeCurrent = null;
  async function runtimeSettings() {
    runtimeCurrent = await request("/api/runtime-settings");
    const a = runtimeCurrent.applied || {}, live = a.receiver === "listening" || a.receiver === "paused";
    $("runtime-status").textContent = `Збережена ревізія: ${runtimeCurrent.revision}. ` + (!live
      ? "Приймач не працює — значення застосуються під час запуску."
      : a.revision === runtimeCurrent.revision ? "Приймач уже використовує ці значення."
      : `Приймач: ревізія ${a.revision ?? "—"} (знімок метрик оновлюється ~30 с; самі значення діють за кілька секунд).`);
    $("runtime-fields").replaceChildren();
    for (const [key, value] of Object.entries(runtimeCurrent.values)) {
      const limits = runtimeCurrent.limits[key], fallback = runtimeCurrent.defaults[key];
      const label = el("label", limits.type === "bool" ? "inline" : "", RUNTIME_LABELS[key] || key), input = el("input");
      input.id = `runtime-${key}`;
      if (limits.type === "bool") { input.type = "checkbox"; input.checked = value; label.prepend(input); }
      else {
        input.type = "number"; input.value = value; input.required = true;
        input.min = limits.min; input.max = limits.max; input.step = limits.type === "int" ? "1" : "0.01";
        label.append(input, el("span", "muted small", `типово ${fallback} · від ${limits.min} до ${limits.max}`));
      }
      $("runtime-fields").append(label);
    }
  }
  $("runtime-settings").addEventListener("submit", e => { e.preventDefault(); action(async () => {
    const values = {};
    for (const [key, limits] of Object.entries(runtimeCurrent.limits)) {
      const input = $(`runtime-${key}`);
      values[key] = limits.type === "bool" ? input.checked : limits.type === "int" ? parseInt(input.value, 10) : Number(input.value);
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

  // ---------- Користувачі ----------
  async function users() {
    if (!data) await overview();
    const table = $("users"), head = el("tr");
    table.replaceChildren();
    for (const h of ["Ім’я", "Telegram", "Роль", "Сповіщення", "Стан"]) head.append(el("th", "", h));
    table.append(head);
    for (const u of data.users) {
      const row = el("tr"), role = el("span", `role ${u.role}`, ROLES[u.role] || u.role);
      const roleCell = el("td"); roleCell.append(role);
      row.append(el("td", "", u.name || "—"), el("td", "", `${u.username ? "@" + u.username + " · " : ""}${u.user_id}`),
        roleCell, el("td", "", MODES[u.mode] || u.mode), el("td", u.enabled ? "good" : "alert", u.enabled ? "активний" : "вимкнений"));
      table.append(row);
    }
  }

  // ---------- Система ----------
  async function system() {
    if (!data) await overview();
    const d = data.diagnostics || {}, c = d.counters || {}, g = d.gauges || {};
    $("backups").replaceChildren(...(data.backups.length ? data.backups.map(b => {
      const row = el("div", "item"); row.append(el("div", "text", stamp(b.created)), el("div", "orig", `${b.name} · ${Math.round(b.size / 1024)} КБ · ${ago(now() - b.created)} тому`)); return row;
    }) : [el("p", "muted", "Автоматичних копій ще немає: перша з’являється через 5 хв після старту приймача.")]));
    $("workers").replaceChildren(kv([
      ["Whisper", data.models.whisper], ["Стан Whisper", g["models.whisper"] || "—"],
      ["Qwen", data.models.qwen], ["Стан Qwen", g["models.qwen"] || "—"],
      ["Потік розпізнавання", g["workers.transcriber_alive"] === false ? "зупинено" : "працює", g["workers.transcriber_alive"] === false ? "alert" : "good"],
      ["Потік Telegram", g["workers.telegram_alive"] === false ? "зупинено" : "працює", g["workers.telegram_alive"] === false ? "alert" : "good"],
      ["Початок сесії", d.started_at ? stamp(d.started_at) : "—"],
    ]));
    const t = data.telegram_sync || {};
    $("telegram").replaceChildren(kv([
      ["Відомих копій повідомлень", num(t.copies)], ["Правок у черзі", num(t.pending)], ["Правок, що не вдалися", num(t.exhausted), t.exhausted ? "alert" : "good"],
      ["Надіслано голосових", num(c["telegram.sendVoice.api_confirmed"] || 0)], ["Заблокували бота (403)", num(c["telegram.recipient_blocked"] || 0)],
      ["Збоїв опитування", num(Object.entries(c).filter(([k]) => k.startsWith("telegram.getUpdates.") && !k.endsWith("attempts") && !k.endsWith("http_200")).reduce((s, [, v]) => s + v, 0))],
      ["Службових повідомлень адміністраторам", num(c["telegram.alert_sent"] || 0)],
    ]));
    const table = $("counters"); table.replaceChildren();
    for (const [k, v] of Object.entries(c).sort()) { const r = el("tr"); r.append(el("td", "", k), el("td", "", num(v))); table.append(r); }
    $("diagnostics").textContent = JSON.stringify(d, null, 2);
    await diagnosticHistory();
  }
  // ---------- Історія сесій ----------
  let selectedHistory = null;
  async function showHistory() {
    selectedHistory = null;
    $("history-download").disabled = true;
    $("history-metrics").replaceChildren();
    $("history-json").textContent = "";
    const session = $("history-session").value;
    if (!session) { $("history-summary").textContent = "Збережених сесій поки немає."; return; }
    $("history-summary").textContent = "Завантаження сесії…";
    const snapshot = await request(`/api/diagnostics/history/${encodeURIComponent(session)}`);
    if ($("history-session").value !== session) return;
    selectedHistory = snapshot;
    const status = snapshot.session_status === "finished" ? "завершена (є фінальний знімок)" : "завершення не підтверджено";
    $("history-summary").textContent = `${stamp(snapshot.started_at)} · ${status} · останній знімок ${stamp(snapshot.updated_at)} · сегментів: ${num(snapshot.counters?.["asr.measured_segments"] || 0)}`;
    const table = $("history-metrics"), head = el("tr");
    for (const title of ["Метрика", "Середня", "p95 (128)", "Максимум", "Вимірювань"]) head.append(el("th", "", title));
    table.append(head);
    for (const [key, title] of Object.entries(LATENCY)) {
      const metric = snapshot.asr_latency?.ready?.[key]; if (!metric) continue;
      const row = el("tr");
      for (const value of [title, fix(metric.mean, 3), fix(metric.recent_p95, 3), fix(metric.max, 3), num(metric.count)]) row.append(el("td", "", value));
      table.append(row);
    }
    $("history-json").textContent = JSON.stringify(snapshot, null, 2);
    $("history-download").disabled = false;
  }
  async function diagnosticHistory() {
    const select = $("history-session");
    select.disabled = true;
    try {
      const response = await request("/api/diagnostics/history");
      const previous = select.value;
      select.replaceChildren();
      for (const item of response.items) {
        const label = `${stamp(item.started_at)} · ${item.session.slice(0, 8)} · ${item.session_status === "finished" ? "завершена" : "останній знімок"} · ${num(item.measured_segments)} сегм.`;
        const option = el("option", "", label); option.value = item.session; select.append(option);
      }
      if (response.items.some(item => item.session === previous)) select.value = previous;
      $("history-policy").textContent = `До ${response.retention_days} днів, ${response.max_sessions} сесій та ${Math.round(response.max_bytes / 1048576)} МіБ. Оновлення приблизно кожні 30 с.`;
      await showHistory();
    } finally { select.disabled = select.options.length === 0; }
  }
  $("history-refresh").addEventListener("click", () => action(diagnosticHistory));
  $("history-session").addEventListener("change", () => action(showHistory));
  $("history-download").addEventListener("click", () => {
    if (!selectedHistory) return;
    const url = URL.createObjectURL(new Blob([JSON.stringify(selectedHistory, null, 2)], {type:"application/json"}));
    const anchor = document.createElement("a"); anchor.href = url;
    anchor.download = `radio345-session-${selectedHistory.session}.json`;
    document.body.append(anchor); anchor.click(); anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  });

  $("backup-now").addEventListener("click", () => action(async () => {
    const r = await request("/api/backups", "POST"); data.backups = r.items; await system(); note(`Копію створено: ${r.name}`);
  }));

  // ---------- Ролі ----------
  function roles() {
    const role = $("role").value;
    $("permissions").textContent = {
      viewer: "Спостерігач: лише стан ефіру. Журнал, словники, аудіо, налаштування й користувачі недоступні.",
      editor: "Редактор: журнал, аудіо, виправлення, словники й особисті сповіщення. Без користувачів і керування приймачем.",
      admin: "Адміністратор: усі розділи.",
    }[role];
    $("preview").src = `/preview/${role}/`;
  }
  $("role").addEventListener("change", roles);

  let recordingOffset = 0;
  async function recordings(reset = true) {
    if (reset) { recordingOffset = 0; $("recordings").replaceChildren(); }
    const [page, summary] = await Promise.all([
      request(`/api/recordings?offset=${recordingOffset}&review=${$("recording-filter").value}&group=${$("recording-group").value}`),
      request("/api/recordings/summary")
    ]);
    const groups = page.groups;
    const grouped = groups && ["all", "normal", "short"].every(key => typeof groups[key] === "number");
    const groupSelect = $("recording-group");
    groupSelect.disabled = !grouped;
    groupSelect.title = grouped ? "" : "Перезапустіть локальну панель, щоб увімкнути групи";
    if (!grouped) groupSelect.value = "all";
    for (const option of groupSelect.options) {
      const count = grouped ? groups[option.value] : option.value === "all" ? page.total : null;
      option.textContent = `${{normal: "Основні", short: "Короткі (до 2 с / VAD)", all: "Усі разом"}[option.value]}${count == null ? "" : ` · ${count}`}`;
    }
    if (!grouped) note("Записи відкрито без груп. Перезапустіть локальну адмінпанель і оновіть список, щоб відокремити короткі фрази. Приймач зупиняти не потрібно.");
    const quality = summary.quality;
    $("recording-summary-details").textContent = `Записів: ${Object.values(summary.statuses).reduce((a,b) => a+b, 0)} · перевірено: ${summary.reviewed} · аудіо: ${(summary.bytes / 1048576).toFixed(1)} МБ · WER Whisper: ${pct(quality.raw_text.wer)} · WER виходу: ${pct(quality.final_text.wer)} · мовлення без тексту: ${summary.missed_speech} · хибний текст на шумі/сигналі: ${summary.false_text} · точність «кому»: ${pct(summary.roles?.recipient?.accuracy)} (${summary.roles?.recipient?.evaluated || 0}) · «від кого»: ${pct(summary.roles?.sender?.accuracy)} (${summary.roles?.sender?.evaluated || 0}). Неперевірені: ${summary.retention_days} днів / ${(summary.max_bytes / 1073741824).toFixed(1)} ГБ; перевірені зберігаються.`;
    $("recording-summary").textContent = `Записів: ${page.total} · перевірено: ${summary.reviewed} · WER до / після: ${pct(quality.raw_text.wer)} / ${pct(quality.final_text.wer)}`;
    if (!page.total) $("recordings").append(el("p", "muted", (grouped ? groups.all : page.total) ? "У цій групі записів немає. Оберіть іншу групу або стан перевірки." : "Записів за цим фільтром ще немає. Нові з’являться після запуску оновленого приймача."));
    for (const item of page.items) {
      const d = item.data, signal = d.signal || {};
      const card = el("article", "panel recording-card");
      const header = el("div", "recording-header");
      header.append(el("h3", "", `${new Date(item.created * 1000).toLocaleString("uk-UA")} · ${STATUS[item.status] || (item.status === "vad_short" ? "Закоротко (VAD)" : item.status)}`));
      const audio = el("audio"); audio.controls = true; audio.preload = "none";
      audio.src = `/api/recordings/${item.id}/audio`; header.append(audio); card.append(header);
      const details = el("details", "recording-details");
      details.append(el("summary", "", "Розбір і метрики"));
      details.append(el("p", "muted small", `Тривалість: ${fix(signal.duration_s)} с · рівень: ${fix(signal.rms_dbfs, 1)} dBFS · кліпінг: ${pct(signal.clipping_ratio)} · Whisper: ${fix(d.whisper_s)} с · Qwen: ${fix(d.qwen_s)} с · черга: ${fix(d.queue_wait_s)} с`));
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
      const form = el("form"), label = el("select"), input = el("textarea");
      for (const [value, title] of Object.entries({speech: "Мовлення", noise: "Шум / тиша", notification: "Звук сповіщення", unclear: "Неможливо розібрати"})) {
        const option = el("option", "", title); option.value = value; label.append(option);
      }
      label.value = item.label || "speech";
      input.value = item.reference ?? (d.final_text || d.raw_text || "");
      input.rows = 2; input.maxLength = 2000; input.placeholder = "Введіть те, що справді чутно…";
      input.setAttribute("aria-label", "Еталонна розшифровка");
      function roleField(title, reference) {
        const group = el("div", "recording-role"), mode = el("select"), value = el("input");
        group.append(el("span", "", title));
        mode.setAttribute("aria-label", `${title}: стан перевірки`);
        for (const [key, text] of Object.entries({unknown: "Невідомо / не перевірено", absent: "Не вказано у фразі", value: "Вказати позивний"})) {
          const option = el("option", "", text); option.value = key; mode.append(option);
        }
        mode.value = reference == null ? "unknown" : reference === "" ? "absent" : "value";
        value.type = "text"; value.maxLength = 300; value.value = reference || "";
        value.placeholder = "Позивний і номер; декілька — через кому";
        value.setAttribute("aria-label", `${title}: позивний`);
        const update = () => { value.hidden = mode.value !== "value"; value.required = mode.value === "value"; };
        mode.addEventListener("change", update); update(); group.append(mode, value);
        return {node: group, read: () => mode.value === "unknown" ? null : mode.value === "absent" ? "" : value.value,
                absent: () => { mode.value = "absent"; update(); }};
      }
      const recipient = roleField("Кому", item.reference_recipient), sender = roleField("Від кого", item.reference_sender);
      label.addEventListener("change", () => {
        if (["noise", "notification"].includes(label.value)) { input.value = ""; recipient.absent(); sender.absent(); }
      });
      const save = el("button", "", item.reference === null ? "Зберегти еталон" : "Оновити еталон");
      const textField = el("label", "recording-text");
      textField.append(el("span", "muted small", "Почутий текст"), input);
      const roles = el("div", "recording-roles"); roles.append(recipient.node, sender.node);
      const actions = el("div", "recording-actions");
      label.setAttribute("aria-label", "Тип запису"); actions.append(label, save);
      form.append(textField, roles, actions);
      form.addEventListener("submit", event => { event.preventDefault(); action(async () => {
        const result = await request(`/api/recordings/${item.id}/review`, "PUT", {reference: input.value, label: label.value, expected_revision: item.revision, reference_recipient: recipient.read(), reference_sender: sender.read()});
        item.revision = result.revision; item.reference = result.reference;
        showQuality(result);
        save.textContent = "Оновити еталон"; note("Еталон збережено. Початкові тексти й правила не змінено. Оновіть список для перерахунку загальної оцінки.");
      }); });
      card.append(form);
      details.append(el("pre", "", JSON.stringify(d, null, 2)));
      card.append(details); $("recordings").append(card);
    }
    recordingOffset += page.items.length;
    $("recording-more").hidden = recordingOffset >= page.total;
  }
  $("recording-refresh").addEventListener("click", () => action(() => recordings(true)));
  $("recording-filter").addEventListener("change", () => action(() => recordings(true)));
  $("recording-group").addEventListener("change", () => action(() => recordings(true)));
  $("recording-more").addEventListener("click", () => action(() => recordings(false)));

  const loaders = { recordings: () => recordings(true), overview, phrases: () => phrases(true), sounds: samples, filter: filterView, texts, settings: async () => { await settings(); await runtimeSettings(); }, users, system, roles: async () => roles() };

  // ---------- Автооновлення ----------
  function schedule() {
    clearInterval(timer);
    timer = setInterval(() => {
      if (!$("auto").checked || document.visibilityState !== "visible" || busy) return;
      if (view === "overview" || view === "system" || view === "users") action(overview);
      else if (view === "filter") action(filterView);
    }, REFRESH_MS);
  }
  $("auto").addEventListener("change", schedule);
  $("refresh").addEventListener("click", () => action(async () => { await loaders[view](); if (view !== "overview") await overview(); note("Дані оновлено."); }));

  // ---------- Вхід ----------
  action(async () => {
    const token = location.hash.slice(1);
    history.replaceState(null, "", location.pathname + location.search);
    if (token) await request("/session", "POST", { token });
    await overview();
    loaded.add("overview");
    $("content").hidden = false;
    note("Локальний доступ підтверджено.");
    schedule();
    // ?view=phrases тощо — одразу потрібний розділ (зручно для закладок).
    const wanted = new URLSearchParams(location.search).get("view");
    if (wanted && titles[wanted] && wanted !== "overview") { busy = false; show(wanted); }
  }).then(() => {
    // Справжню помилку вже показано; підказку про вхід — лише якщо ключа не було.
    if ($("content").hidden && $("feedback").textContent === "Перевіряємо доступ…")
      note("Немає доступу. Відкрийте панель командою: .venv/bin/python -m api.local_dashboard --open");
  });
})();
