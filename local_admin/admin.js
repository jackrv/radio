"use strict";
// Radio 345 · локальна панель. Лише 127.0.0.1; дані з того самого Mac.
(() => {
  const $ = id => document.getElementById(id);
  const REFRESH_MS = 10000;
  let busy = false, data = null, view = "overview", timer = null;
  const loaded = new Set();

  const STATUS = {
    ready: "Розпізнано", ready_fallback: "Розпізнано (резервний текст)",
    short_garbage: "Короткий хибний фрагмент", bare_callsign: "Лише позивний без номера", low_rms: "Тихо (нижче MIN_RMS)", short: "Закоротко (VAD)",
    notification: "Звук сповіщення", blacklist: "Чорний список", garbage: "Сміття Whisper", low_confidence: "Низька впевненість", dictionary_echo: "Перелік назв із підказки",
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
  // Повідомлення — спливаючі, на кілька секунд, а не постійний рядок під заголовком.
  let toastTimer = null;
  const note = text => {
    const box = $("feedback");
    box.textContent = text;
    box.classList?.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => box.classList?.remove("show"), Math.min(9000, 3500 + String(text).length * 40));
  };
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
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 30000);
    try {
      const response = await fetch(path, {...options, signal: controller.signal});
      if (!response.ok) {
        const error = await response.json().catch(() => ({}));
        const failure = new Error(typeof error.detail === "string" ? error.detail : `HTTP ${response.status}. Онови сторінку або відкрий панель командою --open.`);
        failure.status = response.status;
        throw failure;
      }
      if (response.status === 204) return null;
      return await ((response.headers.get("content-type") || "").includes("json") ? response.json() : response.blob());
    } catch (error) {
      if (error.name === "AbortError") throw new Error("Сервер не відповів за 30 с. Спробуйте ще раз.");
      throw error;
    } finally { clearTimeout(timeout); }
  }
  async function action(callback) { if (busy) return; busy = true; try { await callback(); } catch (error) { note(error.message); } finally { busy = false; } }

  // ---------- Навігація: розділи й вкладки ----------
  const titles = { recordings: "Аудіоархів", overview: "Огляд", filter: "Сигнали сповіщень", dictionaries: "Словники", codes: "Коди доби", texts: "Фільтри тексту", learning: "Навчання", settings: "Налаштування", users: "Користувачі", activity: "Активність", preview: "Mini App", quality: "Якість по днях", experiments: "Експерименти", system: "Система" };
  const GROUPS = {
    overview: ["Огляд", ["overview"]],
    recordings: ["Аудіоархів", ["recordings"]],
    recognition: ["Розпізнавання", ["dictionaries", "codes", "texts", "learning", "settings"]],
    signals: ["Сигнали сповіщень", ["filter"]],
    people: ["Люди", ["users", "activity", "preview"]],
    analysis: ["Аналіз", ["quality", "experiments"]],
    system: ["Система", ["system"]],
  };
  // Дані, які змінюються поза цією вкладкою, перечитуються при кожному відкритті.
  const ALWAYS_RELOAD = new Set(["codes", "learning", "users", "activity"]);
  const lastTab = {};
  const stateByView = {};
  let navigationRevision = 0;
  const loadingViews = new Map();
  function rememberField(field) {
    if (!field?.matches?.("textarea, input, select, [contenteditable=true]")) return;
    const panel = field.closest("[data-panel]");
    if (!panel) return;
    const state = stateByView[panel.dataset.panel] ||= {};
    state.field = field;
    state.selection = typeof field.selectionStart === "number"
      ? [field.selectionStart, field.selectionEnd, field.selectionDirection] : null;
    state.fieldScroll = field.scrollTop;
  }
  // focusout ще бачить поле редактора, коли click уже перевів фокус на навігацію.
  document.addEventListener("focusin", event => rememberField(event.target));
  document.addEventListener("focusout", event => rememberField(event.target));
  const groupOf = name => Object.keys(GROUPS).find(g => GROUPS[g][1].includes(name));
  // history: "push" — новий крок для кнопки «Назад», "replace" — замінити поточний, "none" — уже з історії.
  function show(name, { history: mode = "push" } = {}) {
    if (GROUPS[name]) name = lastTab[name] || GROUPS[name][1][0];
    const group = groupOf(name);
    if (!group) return;
    const changed = name !== view;
    if (changed) {
      rememberField(document.activeElement);
      const state = stateByView[view] ||= {};
      state.y = window.scrollY;
      state.fieldTop = state.field?.isConnected ? state.field.getBoundingClientRect().top : null;
    }
    const revision = ++navigationRevision;
    function restore() {
      if (!changed) return;
      requestAnimationFrame(() => {
        if (revision !== navigationRevision || view !== name) return;
        const state = stateByView[name] || {}, field = state.field;
        const connected = field?.isConnected && !field.closest("[data-panel]")?.hidden;
        const top = connected && state.fieldTop != null
          ? window.scrollY + field.getBoundingClientRect().top - state.fieldTop : state.y || 0;
        window.scrollTo({ top, behavior: "instant" });
        if (connected) {
          const selection = state.selection, fieldScroll = state.fieldScroll;
          field.focus({ preventScroll: true });
          if (selection && field.setSelectionRange) {
            try { field.setSelectionRange(...selection); } catch (_) { /* input без виділення */ }
          }
          field.scrollTop = fieldScroll || 0;
        }
        if (name === "recordings") {
          reviewCards.find(card => card.card.contains(field))?.refreshHints?.();
        }
      });
    }
    if (changed && view === "preview") $("preview").src = "about:blank";
    view = name; lastTab[group] = name;
    const url = name === "overview" ? location.pathname : `${location.pathname}?view=${encodeURIComponent(name)}`;
    if (mode === "push" && changed) history.pushState({ view: name }, "", url);
    else if (mode === "replace") history.replaceState({ view: name }, "", location.pathname + location.search);
    for (const b of document.querySelectorAll("#nav button")) b.classList.toggle("active", b.dataset.group === group);
    for (const s of document.querySelectorAll("[data-panel]")) s.hidden = s.dataset.panel !== name;
    $("view-title").textContent = GROUPS[group][0];
    const tabs = $("tabs"), list = GROUPS[group][1];
    tabs.replaceChildren();
    tabs.hidden = list.length < 2;
    for (const tab of list) {
      const b = el("button", tab === name ? "tab active" : "tab", titles[tab]);
      b.type = "button"; b.setAttribute("role", "tab"); b.setAttribute("aria-selected", String(tab === name));
      b.addEventListener("click", () => show(tab));
      tabs.append(b);
    }
    if (!loaded.has(name) || ALWAYS_RELOAD.has(name)) {
      let pending = loadingViews.get(name);
      if (!pending) {
        pending = Promise.resolve().then(() => loaders[name]()).then(() => loaded.add(name))
          .finally(() => loadingViews.delete(name));
        loadingViews.set(name, pending);
      }
      pending.then(restore, error => { note(error.message); restore(); });
    } else { if (name === "preview") roles(); restore(); }
  }
  for (const b of document.querySelectorAll("#nav button")) b.addEventListener("click", () => show(b.dataset.group));
  // «Назад» / «Вперед» у браузері перемикають розділи панелі, а не залишають її.
  window.addEventListener("popstate", event => {
    const wanted = event.state?.view || new URLSearchParams(location.search).get("view") || "overview";
    show(titles[wanted] ? wanted : "overview", { history: "none" });
  });

  // ---------- Огляд: «Ефір зараз» ----------
  const LIVE_TONE = { ready: "ok", ready_fallback: "ok", notification: "info", low_confidence: "warn", blacklist: "warn",
    garbage: "warn", short_garbage: "warn", bare_callsign: "warn", dictionary_echo: "warn", empty_text: "", whisper_error: "bad" };
  async function live() {
    let r;
    try { r = await request("/api/live?limit=15"); } catch (_) { return; }
    const box = $("live");
    if ([...box.querySelectorAll("audio")].some(audio => !audio.paused && !audio.ended)) {
      $("live-note").textContent = "Стрічка оновиться після прослуховування.";
      return;
    }
    $("live-note").textContent = r.skipped_quiet ? `ще ${r.skipped_quiet} тихих чи закоротких не показано` : "";
    if (!r.items.length) { box.replaceChildren(el("p", "muted", "Записів ще немає. Щойно приймач щось почує, фраза з’явиться тут.")); return; }
    box.replaceChildren(...r.items.map(item => {
      const row = el("div", `live-row ${LIVE_TONE[item.status] ?? ""}`);
      const time = new Date(item.created * 1000).toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit", second: "2-digit" });
      const text = item.final_text || item.rejected_text || item.raw_text;
      const main = el("div", "live-text");
      main.append(el("span", "live-time", time), el("span", "", text || "— тексту немає —"));
      const meta = el("div", "live-meta");
      meta.append(el("span", `badge ${LIVE_TONE[item.status] || ""}`, STATUS[item.status] || item.status));
      if (item.fix_id) meta.append(el("span", "badge", item.fix_id.startsWith("db:") ? `Telegram · №${item.fix_id.slice(3)}` : "Telegram"));
      if (item.reference !== null) meta.append(el("span", "badge", "✓ розмічено"));
      const play = el("button", "ghost small-button", "▶"); play.type = "button"; play.title = "Прослухати";
      play.addEventListener("click", () => {
        const audio = el("audio"); audio.controls = true; audio.src = `/api/recordings/${item.id}/audio`;
        play.replaceWith(audio); audio.play().catch(() => {});
      });
      const open = el("button", "ghost small-button", "Розмітити"); open.type = "button";
      open.addEventListener("click", () => { $("recording-search").value = item.id.slice(0, 8); show("recordings"); action(() => recordings(true)); });
      meta.append(play, open);
      row.append(main, meta);
      return row;
    }));
  }

  // ---------- Аналіз: якість по днях ----------
  async function quality() {
    const r = await request("/api/quality?days=60");
    const table = $("quality-days"), head = el("tr");
    table.replaceChildren();
    for (const h of ["День", "Розмічено", "Помилка в словах: Whisper", "після обробки", "Номери точно", "Адресат вірно"]) head.append(el("th", "", h));
    table.append(head);
    if (!r.days.length) {
      const row = el("tr"), cell = el("td", "muted", "Розмічених записів мовлення за цей період немає. Розмітьте кілька в «Аудіоархіві».");
      cell.colSpan = 6; row.append(cell); table.append(row); return;
    }
    const bar = (value, good) => {
      const cell = el("td", "num");
      if (value == null) { cell.textContent = "—"; return cell; }
      const wrap = el("div", "bar-cell"), fill = el("i", good ? "good" : "bad");
      fill.style.width = `${Math.round(Math.min(1, value) * 100)}%`;
      wrap.append(fill, el("span", "", pct(value)));
      cell.append(wrap);
      return cell;
    };
    for (const d of [...r.days].reverse()) {
      const row = el("tr");
      row.append(el("td", "nowrap", d.day.split("-").reverse().join(".")), el("td", "num", String(d.records)),
        bar(d.wer_raw, false), bar(d.wer_final, false),
        bar(d.numbers, true), bar(d.recipient, true));
      table.append(row);
    }
  }

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
    $("recording-search").value = event.target.value.trim().slice(0, 200);
    event.target.value = "";
    if (view === "recordings" || loaded.has("recordings")) { show("recordings"); action(() => recordings(true)); } else show("recordings");
  });

  // ---------- Огляд: що потребує уваги ----------
  let attentionAt = 0;
  async function attention(force = false) {
    if (!force && Date.now() - attentionAt < 60000) return;
    attentionAt = Date.now();
    let a;
    try { a = await request("/api/attention"); } catch (_) { return; }
    const items = [];
    const item = (tone, text, label, go) => {
      const row = el("div", `attention-item ${tone}`);
      row.append(el("span", "", text));
      if (label) { const b = el("button", "ghost", label); b.type = "button"; b.addEventListener("click", go); row.append(b); }
      items.push(row);
    };
    if (a.codes_missing) item("warn", `Не внесено коди доби ${a.codes_day.split("-").reverse().slice(0, 2).join(".")} (діють з 20:00 попереднього дня).`, "Внести", () => show("codes"));
    if (a.approvals) item("warn", `Замін, що чекають вашого дозволу: ${a.approvals}.`, "Переглянути", () => show("learning"));
    if (a.conflicts) item("bad", `Конфліктів у навчанні: ${a.conflicts} — одне слово виправлено по-різному.`, "Переглянути", () => show("learning"));
    if (a.telegram_failed) item("bad", `Правок, що не дійшли в Telegram після всіх спроб: ${a.telegram_failed}.`, "Система", () => show("system"));
    if (a.unlabeled_radio_24h) item("info", `Нерозмічених записів «Ефіру» за добу: ${a.unlabeled_radio_24h}.`, "Розмітити", () => {
      $("recording-view").value = "radio"; $("recording-period").value = "24";
      if (loaded.has("recordings")) { show("recordings"); action(() => recordings(true)); } else show("recordings");
    });
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
  const openRadioRecords = () => {
    $("recording-view").value = "radio";
    if (loaded.has("recordings")) { show("recordings"); action(() => recordings(true)); } else show("recordings");
  };
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
  async function dropped() {
    const r = await request("/api/notification-filter/dropped?limit=12");
    const box = $("dropped");
    if (!r.items.length) { box.replaceChildren(el("p", "muted", "Фільтр ще нічого не відкидав.")); return; }
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
          note("Позначено як сигнал."); await dropped();
        }));
        meta.append(yes);
      }
      const open = el("button", "ghost small-button", "Розмітити"); open.type = "button"; open.title = "Відкрити в «Аудіоархіві» (якщо там мовлення)";
      open.addEventListener("click", () => { $("recording-search").value = item.id.slice(0, 8); show("recordings"); action(() => recordings(true)); });
      meta.append(open);
      row.append(main, meta);
      return row;
    }));
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

  // ---------- Текстові списки ----------
  const TEXT_LISTS = {
    blacklist: ["Фрази-галюцинації Whisper", "Відкидається лише фраза, що повністю збігається з рядком (регістр і пунктуація не важливі). Наприклад: «дякую за перегляд»."],
    short_garbage: ["Короткий шум", "Короткі хибні фрагменти. Відкидаються, лише якщо у фразі немає позивних, команд і слів спостереження."],
    aliases: ["Алиаси тексту", "Формат «правильне: варіант, варіант». Варіант замінюється на правильне написання перед пошуком позивних і команд, наприклад «наган: на ган, ган»."],
    callsign_aliases: ["Алиаси позивних", "Формат «позивний: почутий варіант». Для позивного з номерами діє лише перед дозволеним номером; для позивного без номера — на саме слово. Позивний має бути у словнику."],
    whisper_prompt: ["Підказка Whisper", "Фрази на початку підказки розпізнавання, по одній у рядку, так, як їх вимовляють: «дощ 13 13». Допомагають Whisper правильно записати важливі команди, але зрідка потрапляють у розпізнаний текст. З’являться хибні спрацьовування — вимкніть рядок, поставивши «#» на початку."],
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
  function liveCheck(area, box, path, initial = []) {
    const render = (errors, warnings) => {
      box.replaceChildren();
      for (const e of errors) box.append(el("li", "error", e));
      for (const w of warnings) box.append(el("li", "", w));
    };
    render([], initial);
    let timer = null, seq = 0;
    area.addEventListener("input", () => {
      clearTimeout(timer);
      timer = setTimeout(async () => {
        const mine = ++seq;
        try {
          const r = await request(path, "PUT", { entries: fromLines(area.value) });
          if (mine === seq) render(r.errors || [], r.warnings || []);
        } catch (_) { /* підказка необов’язкова */ }
      }, 600);
    });
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
    area.dataset.savedValue = area.value;
    area.spellcheck = false;
    area.setAttribute("aria-label", title);
    const fit = () => { area.rows = Math.min(32, Math.max(8, area.value.split("\n").length + 2)); };
    fit(); area.addEventListener("input", fit);
    const save = el("button", "", "Зберегти"), status = el("span", "muted small", `${dictionary.entries.filter(e => e.enabled).length} діючих рядків`);
    save.addEventListener("click", () => action(async () => {
      try {
        await request(`/api/dictionaries/${kind}`, "PUT", { entries: fromLines(area.value), expected_version: dictionary.version });
        note(`Словник «${title}» збережено. Приймач застосує зміни за кілька секунд.`);
        await dictionaries();
      } catch (error) { status.textContent = error.message; status.className = "alert small"; throw error; }
    }));
    const actions = el("div", "list-actions"); actions.append(save, status);
    const hints = el("ul", "warn-list");
    liveCheck(area, hints, `/api/dictionaries/${kind}/check`);
    left.append(area, actions);
    right.append(el("h2", "", title), el("p", "muted small", help), hints);
    panel.append(left, right);
    box.append(panel);
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
    const area = el("textarea", "list-editor");
    area.value = toLines(list.entries);
    area.spellcheck = false;
    area.setAttribute("aria-label", title);
    const fit = () => { area.rows = Math.min(30, Math.max(8, area.value.split("\n").length + 2)); };
    fit(); area.addEventListener("input", fit);
    const save = el("button", "", "Зберегти"), status = el("span", "muted small", `${list.entries.filter(e => e.enabled).length} діючих рядків`);
    const warnings = el("ul", "warn-list");
    liveCheck(area, warnings, `/api/text-lists/${type}/check`, list.warnings || []);
    save.addEventListener("click", () => action(async () => {
      try {
        const r = await request(`/api/text-lists/${type}`, "PUT", { entries: fromLines(area.value), expected_revision: textRevision });
        textRevision = r.revision;
        note(`«${title}» збережено (ревізія ${r.revision}). Приймач застосує зміни за кілька секунд.`);
        await texts();
      } catch (error) { status.textContent = error.message; status.className = "alert small"; throw error; }
    }));
    const actions = el("div", "list-actions"); actions.append(save, status);
    left.append(area, actions);
    right.append(el("h2", "", title), el("p", "muted small", help), warnings);
    if (type === "callsign_aliases") right.append(aliasCandidates(list.entries));
    if (type === "blacklist" || type === "short_garbage") right.append(noiseCandidates(type, list.entries));
    panel.append(left, right);
    box.append(panel);
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
  // ---------- Навчання за правками ----------
  const LEARNING_QUEUE = ["needs_confirmation", "needs_approval", "conflict"];
  const LEARNING_KIND = { callsign: "позивний", word: "слово", blocked: "—" };
  let learningData = null;
  function learningState(item, need) {
    switch (item.state) {
      case "applies": return ["ok", "Діє в розпізнаванні"];
      case "needs_confirmation": {
        const left = need - item.confirmations;
        const tail = item.kind === "word" ? (item.approved ? " · дозвіл уже є" : " · і дозвіл") : "";
        return ["warn", `Ще ${left} ${left === 1 ? "однакова правка" : "однакові правки"}${tail}`];
      }
      case "needs_approval": return ["warn", "Чекає вашого дозволу"];
      case "conflict": return ["bad", "Те саме слово виправлено інакше в іншій правці"];
      case "disabled": return ["off", "Вимкнено"];
      default: return ["off", `Не застосовується: ${item.blocked_reason || "заборонено перевіркою"}`];
    }
  }
  function learningSources(sources) {
    const box = el("span");
    for (const source of sources) {
      const event = /^db:(\d+)$/.exec(source);
      if (box.childNodes.length) box.append(", ");
      if (event) {
        const link = el("a", "", `№${event[1]}`);
        link.href = `?view=recordings&q=${encodeURIComponent("№" + event[1])}`;
        link.title = "Знайти запис в «Аудіоархіві»";
        box.append(link);
      } else box.append(document.createTextNode("фраза Telegram"));
    }
    if (!sources.length) box.append(document.createTextNode("—"));
    return box;
  }
  async function changeRule(item, path, body) {
    await request(`/api/learning/${item.id}${path}`, "PUT", { ...body, expected_version: item.version });
    await learning();
  }
  function learningActions(item) {
    const box = el("span", "chip-row");
    if (item.kind === "word" && item.state !== "disabled") {
      const b = el("button", "outline-button", item.approved ? "Зняти дозвіл" : "Дозволити");
      b.title = item.approved ? "Заміна перестане діяти" : "Заміна діятиме, коли набере потрібні підтвердження";
      b.addEventListener("click", () => action(() => changeRule(item, "/approval", { approved: !item.approved })));
      box.append(b);
    }
    if (item.kind !== "blocked") {
      const off = item.state === "disabled";
      const b = el("button", "ghost", off ? "Увімкнути" : "Вимкнути");
      b.addEventListener("click", () => action(() => changeRule(item, "", { disabled: !off })));
      box.append(b);
    }
    return box;
  }
  function renderLearning() {
    const data = learningData, c = data.counts || {}, need = data.min_confirmations;
    const cards = $("learning-cards");
    const pick = value => () => {
      $("learning-filter").value = value;
      try { localStorage.setItem("radio345:learning-filter", value); } catch (_) { /* сесія */ }
      renderLearning();
    };
    const learningCard = (c, value, title) => { c.dataset.filter = value; return linked(c, pick(value), title); };
    cards.replaceChildren(
      learningCard(card("Діють у розпізнаванні", String(c.applies || 0), `з ${data.total} навчених замін`, (c.applies || 0) ? "ok" : ""), "applies", "Показати заміни, що діють"),
      learningCard(card("Чекають підтвердження", String(c.needs_confirmation || 0), `потрібно ${need} однакові правки в різних фразах`, (c.needs_confirmation || 0) ? "warn" : ""), "needs_confirmation", "Показати заміни, яким бракує правки"),
      learningCard(card("Чекають дозволу", String(c.needs_approval || 0), "підтверджено, але звичайне слово — потрібна ваша згода", (c.needs_approval || 0) ? "warn" : ""), "needs_approval", "Показати заміни, що чекають дозволу"),
      learningCard(card("Конфлікти", String(c.conflict || 0), "одне слово виправлено по-різному", (c.conflict || 0) ? "bad" : ""), "conflict", "Показати конфлікти"),
      learningCard(card("Вимкнені й заборонені", String((c.disabled || 0) + (c.blocked || 0)), `вимкнено: ${c.disabled || 0} · заборонено перевіркою: ${c.blocked || 0}`), "other", "Показати вимкнені й заборонені"),
    );
    const filter = $("learning-filter").value;
    for (const c of cards.children) c.classList.toggle("picked", c.dataset.filter === filter);
    const shown = data.items.filter(item => filter === "all" || (filter === "queue" ? LEARNING_QUEUE.includes(item.state)
      : filter === "other" ? ["disabled", "blocked"].includes(item.state) : item.state === filter));
    const table = $("learning-items"), head = el("tr");
    table.replaceChildren();
    for (const h of ["Почуте → правильне", "Тип", "Правок", "Стан", "Звідки", ""]) head.append(el("th", "", h));
    table.append(head);
    if (!shown.length) {
      const row = el("tr"), cell = el("td", "muted", "Тут порожньо.");
      cell.colSpan = 6; row.append(cell); table.append(row);
    }
    for (const item of shown) {
      const [tone, text] = learningState(item, need);
      const row = el("tr"), pair = el("td"), state = el("td");
      pair.append(el("b", "", item.original), document.createTextNode(" → "), el("b", "", item.corrected));
      state.append(el("span", `pill ${tone}`, text));
      const sources = el("td"); sources.append(learningSources(item.sources));
      const actions = el("td", "nowrap"); actions.append(learningActions(item));
      row.append(pair, el("td", "", LEARNING_KIND[item.kind] || item.kind), el("td", "", `${item.confirmations} з ${need}`), state, sources, actions);
      table.append(row);
    }
  }
  async function learning() {
    learningData = await request("/api/learning");
    renderLearning();
  }
  $("learning-filter").addEventListener("change", () => { if (learningData) renderLearning(); });
  $("learning-refresh").addEventListener("click", () => action(learning));

  // ---------- Лаунчер: перезапуск і зупинка ----------
  const LAUNCHER = { running: "працює", starting: "запускається", stopping: "зупиняється", cleanup_pending: "завершує служби", inactive: "не керує панеллю" };
  async function launcherState() {
    try {
      const s = await request("/api/launcher");
      $("launcher-state").textContent = s.managed ? (LAUNCHER[s.state] || s.state) : LAUNCHER.inactive;
      $("launcher-restart").disabled = $("launcher-stop").disabled = $("receiver-toggle").disabled = !s.managed || !s.reachable;
      receiverStopped = !!s.receiver_stopped;
      if (s.managed && receiverStopped) $("launcher-state").textContent = "приймач зупинено";
      $("receiver-toggle").innerHTML = receiverStopped
        ? '<svg class="ico solid" viewBox="0 0 24 24" aria-hidden="true"><path d="M8 5.5v13l11-6.5z"/></svg>'
        : '<svg class="ico solid" viewBox="0 0 24 24" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
      $("receiver-toggle").setAttribute("aria-label", receiverStopped ? "Запустити приймач" : "Зупинити лише приймач");
      $("receiver-toggle").title = receiverStopped ? "Запустити приймач" : "Зупинити лише приймач (панель, API й Mini App працюють далі)";
    } catch (_) { $("launcher-state").textContent = "—"; }
  }
  let receiverStopped = false;
  $("receiver-toggle").addEventListener("click", () => action(async () => {
    const start = receiverStopped;
    if (!start && !confirm("Зупинити лише приймач? Ефір не записуватиметься й сповіщень не буде, доки його не запустять знову. Панель, API й Mini App працюють.")) return;
    await request(`/api/launcher/${start ? "receiver-start" : "receiver-stop"}`, "POST");
    note(start ? "Приймач запускається (близько 10 с)…" : "Приймач зупиняється: дочікується черги Telegram…");
    setTimeout(launcherState, 8000);
  }));
  async function waitForComeback() {
    // Панель перезапускається разом з усіма службами; сесія зберігається.
    for (let i = 0; i < 90; i++) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      try { const s = await request("/api/launcher"); if (s.managed && s.state === "running" && i > 2) { location.reload(); return; } } catch (_) {}
    }
    note("Панель не повернулась за 3 хв. Перевірте вікно лаунчера або launcher.log.");
  }
  $("launcher-restart").addEventListener("click", () => action(async () => {
    if (!confirm("Перезапустити всі служби? Приймач, бот і Mini App будуть недоступні близько хвилини. Черга Telegram буде дочекана.")) return;
    await request("/api/launcher/restart", "POST");
    $("launcher-state").textContent = "перезапускається";
    note("Перезапуск… Сторінка оновиться сама, коли панель повернеться.");
    waitForComeback();
  }));
  $("launcher-stop").addEventListener("click", () => action(async () => {
    if (!confirm("Зупинити Radio 345 повністю? Сповіщення не надходитимуть, ця панель теж зупиниться. Запустити знову можна лише на Mac: у папці проєкту командою .venv/bin/python start_radio345.py")) return;
    await request("/api/launcher/stop", "POST");
    $("launcher-state").textContent = "зупиняється";
    note("Лаунчер зупиняється. Запустити знову: .venv/bin/python start_radio345.py у папці проєкту.");
  }));
  launcherState();
  setInterval(launcherState, 30000);

  // ---------- Активність користувача ----------
  const ACTIONS = {
    open: "Відкрито Mini App", correction: "Правка тексту", learning: "Рішення щодо навчання",
    mode: "Режим сповіщень", receiver: "Керування приймачем", dictionary: "Словник",
    rule: "Правило слів", rule_approval: "Дозвіл правила", user: "Зміна користувача",
    settings: "Налаштування розпізнавання", sample: "Зразок сигналу",
    delete: "Видалено повідомлення в себе", hide: "Приховано фразу для всіх",
  };
  const DECISIONS_UA = { confirmed: "навчати", rejected: "не навчати", pending: "без рішення" };
  const when = ts => ts ? new Date(ts * 1000).toLocaleString("uk-UA") : "—";
  function activityTarget(item) {
    const event = /^db:(\d+)$/.exec(item.target || "");
    if (event) {
      const link = el("a", "", `№${event[1]}`);
      link.href = `?view=recordings&q=${encodeURIComponent("№" + event[1])}`;
      link.title = "Знайти запис в «Аудіоархіві»";
      return link;
    }
    if (item.action === "mode") return document.createTextNode(MODES[item.target] || item.target);
    return document.createTextNode(item.target || "");
  }
  function activityDetails(item) {
    const d = item.details || {};
    switch (item.action) {
      case "correction": return d.changed ? `«${d.from}» → «${d.to}»` : "текст не змінено";
      case "learning": return `«${d.original}» → «${d.corrected}»: ${DECISIONS_UA[d.decision] || d.decision}`;
      case "mode": return d.via === "bot" ? "командою в боті" : "у Mini App";
      case "receiver": return d.minutes ? `пауза на ${d.minutes} хв` : "приймання відновлено";
      case "dictionary": return `діючих рядків: ${d.lines}`;
      case "rule": return d.disabled ? "вимкнено" : "увімкнено";
      case "rule_approval": return d.approved ? "дозволено" : "дозвіл знято";
      case "user": return [d.role && ROLES[d.role], d.enabled === false ? "вимкнено" : "", d.notify_mode && MODES[d.notify_mode]].filter(Boolean).join(" · ");
      default: return "";
    }
  }
  // Вибраний користувач запам’ятовується в цьому браузері.
  const ACTIVITY_KEY = "radio345.activityUser";
  let activityFilter = "all", activityRevision = 0, selectedActivityUser = "";
  const remembered = () => { try { return localStorage.getItem(ACTIVITY_KEY) || ""; } catch (_) { return ""; } };
  async function activity(userId) {
    const revision = ++activityRevision;
    userId = userId || selectedActivityUser || remembered();
    let result;
    try { result = await request(`/api/activity${userId ? `?user_id=${encodeURIComponent(userId)}` : ""}`); }
    catch (error) {
      if (revision !== activityRevision) return;
      if (!userId || error.status !== 404) throw error;
      try { localStorage.removeItem(ACTIVITY_KEY); } catch (_) {}
      result = await request("/api/activity");   // користувача вже немає — перший у списку
    }
    if (revision !== activityRevision) return;
    const select = $("activity-user");
    const s = result.selected;
    const signature = JSON.stringify(result.users);
    if (select.dataset.users !== signature) {
      select.replaceChildren(); select.dataset.users = signature;
      for (const u of result.users) {
        const button = el("button", "ghost", `${u.name || u.user_id} · ${ROLES[u.role] || u.role}${u.enabled ? "" : " · вимкнений"}`);
        button.type = "button"; button.dataset.userId = String(u.user_id);
        button.addEventListener("click", () => {
          activityFilter = "all";
          activity(String(u.user_id)).catch(error => note(error.message));
        });
        select.append(button);
      }
    }
    const cards = $("activity-cards"), table = $("activity-items");
    const wrap = table.parentElement, scroll = wrap?.scrollTop;
    cards.replaceChildren(); table.replaceChildren();
    if (!s) { selectedActivityUser = ""; cards.append(el("p", "muted", "Користувачів ще немає.")); return; }
    selectedActivityUser = String(s.user_id);
    try { localStorage.setItem(ACTIVITY_KEY, selectedActivityUser); } catch (_) {}
    for (const button of select.children) {
      const chosen = button.dataset.userId === selectedActivityUser;
      button.classList.toggle("on", chosen); button.setAttribute("aria-pressed", String(chosen));
    }
    const a = s.actions || {}, w = s.actions_week || {};
    const other = Object.entries(a).filter(([k]) => !["open", "correction", "learning", "mode"].includes(k)).reduce((n, [, v]) => n + v, 0);
    const MAIN = ["open", "correction", "learning"];
    const filters = { all: () => true, correction: i => i.action === "correction", learning: i => i.action === "learning",
                      open: i => i.action === "open", other: i => !MAIN.includes(i.action) };
    const FILTER_NAMES = { correction: "правки тексту", learning: "рішення щодо навчання", open: "відкриття Mini App", other: "інші дії" };
    const filterBy = key => { activityFilter = key; renderItems(); };
    const openEdits = () => {
      // Архів: усі записи з правками цього користувача.
      const select = $("recording-edits"), id = String(s.user_id);
      if (![...select.options].some(o => o.value === id)) { const o = el("option", "", `Правки: ${(result.users.find(u => String(u.user_id) === id)?.name || id)}`); o.value = id; select.append(o); }
      select.value = id; $("recording-view").value = "all"; $("recording-period").value = "";
      if (loaded.has("recordings")) { show("recordings"); action(() => recordings(true)); } else show("recordings");
    };
    const filterCard = (c, key, title) => { c.dataset.filter = key; return linked(c, () => filterBy(key), title); };
    cards.append(
      linked(card("Роль", ROLES[s.role] || s.role, `${s.enabled ? "активний" : "вимкнений"} · доступ з ${s.created_at || "—"}`, s.enabled ? "ok" : "bad"), () => show("users"), "Змінити роль і доступ"),
      linked(card("Сповіщення", MODES[s.notify_mode] || s.notify_mode || "—", "поточний режим"), () => show("users"), "Змінити режим сповіщень"),
      filterCard(card("Доставлено сповіщень", String(s.delivered.total), Object.entries(s.delivered.by_kind || {}).map(([k, v]) => `${k}: ${v}`).join(" · ") || "за весь час"), "all", "Показати всі дії"),
      linked(card("Правки тексту", String(a.correction || 0), `зі змінами: ${s.corrections_changed} · за 7 днів: ${w.correction || 0}`), openEdits, "Записи архіву з правками цього користувача"),
      filterCard(card("Рішення щодо навчання", String(a.learning || 0), `за 7 днів: ${w.learning || 0}`), "learning", "Показати лише рішення щодо навчання"),
      filterCard(card("Відкриття Mini App", String(a.open || 0), `останнє: ${when(s.last_activity)}`), "open", "Показати лише відкриття Mini App"),
      filterCard(card("Активних днів", String(s.active_days || 0), s.first_logged ? `журнал з ${when(s.first_logged)}` : "дій ще не записано"), "all", "Показати всі дії"),
      filterCard(card("Інші дії", String(other + (a.mode || 0)), `режим: ${a.mode || 0} · словники, правила, налаштування: ${other}`), "other", "Показати лише інші дії"),
    );
    function renderItems() {
      for (const c of cards.children) c.classList?.toggle("picked", !!c.dataset?.filter && c.dataset.filter === activityFilter && activityFilter !== "all");
      table.replaceChildren();
      const items = s.items.filter(filters[activityFilter] || filters.all);
      const head = el("tr");
      for (const h of ["Час", "Дія", "Об’єкт", "Подробиці"]) head.append(el("th", "", h));
      table.append(head);
      if (activityFilter !== "all") {
        const row = el("tr"), cell = el("td", "small muted");
        cell.colSpan = 4;
        const reset = el("button", "ghost", "показати всі"); reset.type = "button";
        reset.addEventListener("click", () => filterBy("all"));
        cell.append(`Показано: ${FILTER_NAMES[activityFilter]} (${items.length}) · `, reset);
        row.append(cell); table.append(row);
      }
      if (!items.length) {
        const row = el("tr"), cell = el("td", "muted", s.items.length ? "Таких дій серед останніх немає." : "Дій ще не записано. Вони з’являться, щойно користувач відкриє Mini App чи щось змінить.");
        cell.colSpan = 4; row.append(cell); table.append(row);
      }
      for (const item of items) {
        const row = el("tr"), target = el("td");
        target.append(activityTarget(item));
        row.append(el("td", "nowrap", when(item.ts)), el("td", "", ACTIONS[item.action] || item.action), target, el("td", "", activityDetails(item)));
        table.append(row);
      }
    }
    renderItems();
    if (wrap && scroll != null) wrap.scrollTop = scroll;
  }


  async function users() {
    const result = await request("/api/users");
    const table = $("users"), head = el("tr");
    table.replaceChildren();
    for (const h of ["Користувач", "Назва", "Роль", "Сповіщення", "Доступ", ""]) head.append(el("th", "", h));
    table.append(head);
    for (const u of result.items) {
      const row = el("tr"), who = el("td");
      who.append(el("div", "", u.telegram_name || (u.username ? "@" + u.username : "—")),
        el("div", "muted small", `${u.username && u.telegram_name ? "@" + u.username + " · " : ""}${u.user_id}`));
      const name = el("input"); name.maxLength = 80; name.value = u.display_name || ""; name.placeholder = "ім’я з Telegram";
      const role = el("select");
      for (const [k, v] of Object.entries(ROLES)) { const o = el("option", "", v); o.value = k; role.append(o); }
      role.value = u.role;
      const mode = el("select");
      for (const [k, v] of Object.entries(MODES)) { const o = el("option", "", v); o.value = k; mode.append(o); }
      mode.value = u.notify_mode;
      const restrict = () => {
        for (const o of mode.options) o.disabled = o.value === "all" && role.value !== "admin";
        if (mode.value === "all" && role.value !== "admin") mode.value = "commands";
      };
      role.addEventListener("change", restrict); restrict();
      const enabled = el("input"); enabled.type = "checkbox"; enabled.checked = u.enabled;
      const access = el("label", "inline"); access.append(enabled, document.createTextNode(" увімкнено"));
      const save = el("button", "", "Зберегти");
      save.addEventListener("click", () => action(async () => {
        if (u.role === "admin" && (role.value !== "admin" || !enabled.checked)
            && !confirm("Забрати права адміністратора чи доступ? Якщо це ваш обліковий запис, керувати з Mini App ви вже не зможете.")) return;
        await request(`/api/users/${u.user_id}`, "PUT", { role: role.value, enabled: enabled.checked,
          display_name: name.value, notify_mode: mode.value, expected_version: u.version });
        await users(); note("Користувача збережено.");
      }));
      const cells = [who, el("td"), el("td"), el("td"), el("td"), el("td")];
      cells[1].append(name); cells[2].append(role); cells[3].append(mode); cells[4].append(access); cells[5].append(save);
      row.append(...cells); table.append(row);
    }
  }
  $("users-refresh").addEventListener("click", () => action(users));
  $("new-user-role").addEventListener("change", () => {
    const mode = $("new-user-mode"), admin = $("new-user-role").value === "admin";
    for (const o of mode.options) o.disabled = o.value === "all" && !admin;
    if (mode.value === "all" && !admin) mode.value = "commands";
  });
  $("user-add").addEventListener("submit", event => { event.preventDefault(); action(async () => {
    const id = $("new-user-id").value.trim();
    if (!/^[1-9][0-9]{0,18}$/.test(id)) throw new Error("Telegram ID — лише цифри");
    await request(`/api/users/${id}`, "PUT", { role: $("new-user-role").value, enabled: true,
      display_name: $("new-user-name").value, notify_mode: $("new-user-mode").value, expected_version: null });
    $("new-user-id").value = ""; $("new-user-name").value = "";
    await users(); note(`Доступ для ${id} надано.`);
  }); });
  $("new-user-role").dispatchEvent?.(new Event("change"));

  // ---------- Система ----------
  const shortModel = name => (name || "—").replace(/^mlx-community\//, "").replace(/-mlx$/, "");
  const size = bytes => bytes >= 1048576 ? `${(bytes / 1048576).toFixed(1)} МБ` : `${Math.round(bytes / 1024)} КБ`;
  const storageSize = value => value >= 1073741824 ? `${(value/1073741824).toFixed(2)} ГіБ` : `${(value/1048576).toFixed(1)} МіБ`;
  async function storageView() {
    const box = $("storage-status");
    let s;
    try { s = await request("/api/storage"); }
    catch (_) { box.textContent = "Дані сховища недоступні. Після оновлення сервера потрібен перезапуск лаунчера."; return; }
    box.replaceChildren();
    const audio = s.audio;
    if (audio.available) {
      const full = audio.bytes / audio.max_bytes;
      const bar = el("progress"); bar.max = audio.max_bytes; bar.value = Math.min(audio.bytes, audio.max_bytes);
      bar.setAttribute("aria-label", "Заповнення ліміту WAV"); box.append(bar);
      box.append(kv([
        ["WAV за індексом", `${storageSize(audio.bytes)} / ${storageSize(audio.max_bytes)} · ${(full*100).toFixed(1)}%`, full>=0.9 ? "caution" : ""],
        ["До ліміту", storageSize(audio.remaining_bytes)],
        ["Захищено розміткою", `${num(audio.protected_count)} записів · ${storageSize(audio.protected_bytes)}`],
        ["Нерозмічені", `${num(audio.unreviewed_count)} · ${storageSize(audio.unreviewed_bytes)} · до ${audio.retention_days} днів`],
        ["Найближча межа за віком", audio.next_age_expiry_at ? `${new Date(audio.next_age_expiry_at*1000).toLocaleString("uk-UA")}` : "нерозмічених немає"],
        ["Підлягає очищенню зараз", `${num(audio.due_count)} записів · ${storageSize(audio.due_bytes)}`, audio.due_count ? "caution" : ""],
        ["Індекс SQLite та WAL/SHM", storageSize(audio.metadata_bytes)],
      ]));
      if (audio.estimated_full_at) box.append(el("p", "small caution", `Ліміт може бути досягнутий близько ${new Date(audio.estimated_full_at*1000).toLocaleString("uk-UA")} за темпом останньої години. Це оцінка, не розклад.`));
      const g = audio.unreviewed_by_group;
      if (g) box.append(kv([
        ["Нерозмічені: ефір", `${num(g.radio.count)} · ${storageSize(g.radio.bytes)}`],
        ["Нерозмічені: інше мовлення", `${num(g.speech.count)} · ${storageSize(g.speech.bytes)}`],
        ["Нерозмічені: відсіяне програмою", `${num(g.filtered.count)} · ${storageSize(g.filtered.bytes)}`],
      ]));
      box.append(el("p", "small muted", "Очищення виконується після збереження нового аудіо: за віком або обсягом — що настане раніше. Коли місця бракує, першим видаляється відсіяне програмою (тиша, закороткі, чорний список…), потім інше мовлення, ефір — останнім; у кожній групі спершу старіші. Розмічені записи не видаляються, але займають частину ліміту. Ліміт стосується WAV за індексом, не всього каталогу."));
      if (audio.protected_bytes >= audio.max_bytes) box.append(el("p", "alert", "Захищені записи вже займають увесь ліміт: нові нерозмічені можуть видалятися одразу. Розмітка не очищається автоматично."));
    } else box.append(el("p", "muted", "Аудіоархів ще не створено або його індекс недоступний."));
    for (const log of s.logs) box.append(el("p", "small", `${log.name}: ${storageSize(log.bytes)} загалом; активний ${storageSize(log.active_bytes)} / ${storageSize(log.max_file_bytes)}. До ротації близько ${storageSize(log.remaining_to_rotation_bytes)}. Архівів ${log.archive_count}/${log.max_archives}${log.rotations_until_oldest_removed ? `; найстаріший буде замінено через ${log.rotations_until_oldest_removed} ротацій` : ""}.`));
    box.append(el("p", "small muted", "Логи не мають строку в днях: ротація за розміром, до 5 архівів плюс активний файл для кожного журналу. Точний час залежить від кількості нових повідомлень."));
    const h=s.diagnostics,b=s.backups;
    box.append(el("p", "small", `Історія діагностики: ${h.count}/${h.max_sessions} сесій, ${storageSize(h.bytes)} / ${storageSize(h.max_bytes)}, до ${h.retention_days} днів. Будь-який ліміт може спрацювати раніше за інші. Очищення під час запису знімка; вік рахується від останнього оновлення файла.`));
    if (h.next_age_expiry_at) box.append(el("p", "small muted", `Найближча вікова межа діагностики: ${new Date(h.next_age_expiry_at*1000).toLocaleString("uk-UA")}.`));
    box.append(el("p", "small", `Резервні копії: ${b.automatic_count}/${b.keep} автоматичних (${storageSize(b.automatic_bytes)}). Найстаріші понад ліміт видаляються при створенні наступної копії. Інші копії: ${b.manual_count} (${storageSize(b.manual_bytes)}) — автоматично не видаляються.`));
    box.append(el("p", "small muted", `Вільно на диску: ${storageSize(s.disk.free_bytes)}. Це не вільне місце в ліміті архіву. Перегляд нічого не очищає.`));
  }
  async function system() {
    if (!data) await overview();
    const d = data.diagnostics || {}, c = d.counters || {}, g = d.gauges || {};
    let launcher = {};
    try { launcher = await request("/api/launcher"); } catch (_) { /* панель без лаунчера */ }
    const alive = value => value === false ? ["зупинено", "alert"] : value === true ? ["працює", "good"] : ["—", ""];
    const fresh = d.updated_at && now() - d.updated_at < 120;
    const [asr, asrTone] = fresh ? alive(g["workers.transcriber_alive"]) : ["немає свіжих даних", ""];
    const [tg, tgTone] = fresh ? alive(g["workers.telegram_alive"]) : ["немає свіжих даних", ""];
    $("workers").replaceChildren(kv([
      ["Лаунчер", launcher.managed ? (LAUNCHER[launcher.state] || launcher.state) + (launcher.receiver_stopped ? " · приймач зупинено" : "") : "не керує панеллю", launcher.managed ? "good" : ""],
      ["Сесія приймача", d.started_at ? `з ${stamp(d.started_at)} · ${ago(now() - d.started_at)}` : "—"],
      ["Whisper", `${shortModel(data.models.whisper)} · ${g["models.whisper"] || "—"}`],
      ["Qwen", `${shortModel(data.models.qwen)} · ${g["models.qwen"] || "—"}`],
      ["Потік розпізнавання", asr, asrTone], ["Потік Telegram", tg, tgTone],
      ["Втрачено кадрів / сегментів", `${num(g["audio.frames_dropped"] ?? 0)} / ${num(g["audio.segments_dropped"] ?? 0)}`, (g["audio.frames_dropped"] || g["audio.segments_dropped"]) ? "alert" : ""],
      ["Ревізії: словники · списки · налаштування", `${g["dictionaries.revision"] ?? "—"} · ${g["text_lists.revision"] ?? "—"} · ${g["runtime_settings.revision"] ?? "—"}`],
    ]));
    const t = data.telegram_sync || {};
    const pollFailures = Object.entries(c).filter(([k]) => k.startsWith("telegram.getUpdates.") && !k.endsWith("attempts") && !k.endsWith("http_200")).reduce((s, [, v]) => s + v, 0);
    $("telegram").replaceChildren(kv([
      ["Надіслано голосових (сесія)", num(c["telegram.sendVoice.api_confirmed"] || 0)],
      ["Правок у черзі / не вдалося", `${num(t.pending)} / ${num(t.exhausted)}`, t.exhausted ? "alert" : t.pending ? "" : "good"],
      ["Відомих копій повідомлень", num(t.copies)],
      ["Збоїв опитування бота", num(pollFailures), pollFailures ? "alert" : "good"],
      ["Заблокували бота (403)", num(c["telegram.recipient_blocked"] || 0), c["telegram.recipient_blocked"] ? "alert" : ""],
      ["Службових повідомлень адміністраторам", num(c["telegram.alert_sent"] || 0)],
    ]));
    const backups = data.backups || [];
    const latest = backups.reduce((m, b) => (!m || b.created > m.created ? b : m), null);
    $("backups-summary").replaceChildren(kv(latest ? [
      ["Остання", `${ago(now() - latest.created)} тому`, now() - latest.created > 30 * 3600 ? "alert" : "good"],
      ["Копій", `${backups.length} · ${size(backups.reduce((s, b) => s + b.size, 0))}`],
      ["Розмір останньої", size(latest.size)],
    ] : [["Остання", "немає — перша з’являється через 5 хв після старту приймача", "alert"]]));
    $("backups").replaceChildren(...backups.map(b => el("div", "sys-row", `${stamp(b.created)} · ${size(b.size)} · ${b.name}`)));
    const table = $("counters"); table.replaceChildren();
    for (const [k, v] of Object.entries(c).sort()) { const r = el("tr"); r.append(el("td", "", k), el("td", "", num(v))); table.append(r); }
    $("diagnostics").textContent = JSON.stringify(d, null, 2);
    await diagnosticHistory();
    await storageView();
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

  // ---------- Mini App очима ролі (на сторінці «Користувачі») ----------
  function roles() {
    const role = $("role").value;
    $("permissions").textContent = {
      viewer: "Спостерігач: лише стан приймача. Журнал, аудіо й сповіщення недоступні.",
      editor: "Редактор: журнал фраз, аудіо, виправлення й особистий режим сповіщень.",
      admin: "Адміністратор: те саме, що редактор, і пауза приймача. Словники, навчання й користувачі — лише в цій панелі.",
    }[role];
    $("preview").src = `/preview/${role}/`;
  }
  $("role").addEventListener("change", roles);


  // ---------- Черги розмітки з поточного архіву ----------
  // Види «розмітки» показують лише нерозмічене: розмічений запис зникає зі списку.
  const TODO_VIEWS = ["radio", "speech", "filtered"];
  let pendingEdits = "";   // збережений фільтр «правки користувача», доки варіанти ще не завантажено
  const editsValue = () => $("recording-edits").value || pendingEdits;
  function periodSince() {
    const value = $("recording-period").value;
    if (!value) return null;
    if (value === "today") { const d = new Date(); d.setHours(0, 0, 0, 0); return Math.floor(d.getTime() / 1000); }
    return Math.floor(Date.now() / 1000) - Number(value) * 3600;
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
  async function recordings(reset = true) {
    if (reset && reviewCards.some(card => card.dirty) && !confirm("Є незбережені зміни. Оновити список і відкинути їх?")) {
      if (loadedRecordingFilters) for (const id of recordingFilterIds) $(id).value = loadedRecordingFilters[id];
      return;
    }
    if (reset) { recordingOffset = 0; reviewCards.length = 0; updateDraftCount(); $("recordings").replaceChildren(); }
    const since = periodSince();
    const [page, summary] = await Promise.all([
      request(`/api/recordings?offset=${recordingOffset}&view=${$("recording-view").value}` +
        (since ? `&since=${since}` : "") +
        (searchQuery() ? `&q=${encodeURIComponent(searchQuery())}` : "") +
        (editsValue() ? `&edits=${encodeURIComponent(editsValue())}` : "")),
      request("/api/recordings/summary")
    ]);
    if (reset) archiveBaseline = {query: JSON.stringify(recordingFilterIds.map(id => $(id).value)), total: page.total, first: page.items[0]?.id};
    $("archive-update-note").textContent = "Список оновлюється кнопкою, щоб зберегти чернетки й аудіо.";
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
    $("recording-shadow-summary").hidden = true;
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
      audio.src = `/api/recordings/${item.id}/audio`; header.append(audio); card.append(header);
      if (typeof d.notification_score === "number" || d.fix_id) {
        // Надіслане в Telegram і оцінка спектрального фільтра; чистий сигнал можна зробити зразком.
        const tags = el("div", "recording-tags");
        if (d.fix_id) tags.append(el("span", "badge", eventNo ? `надіслано в Telegram · подія №${eventNo[1]}` : "надіслано в Telegram"));
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
      details.append(el("summary", "", "Розбір і метрики"));
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
        mode.addEventListener("change", () => { rolesTouched(); update(); renderChips(); });
        value.addEventListener("input", () => { rolesTouched(); renderChips(); });
        update(); group.append(mode, value);
        return {node: group, read: () => mode.value === "unknown" ? null : mode.value === "absent" ? "" : value.value,
                absent: () => { mode.value = "absent"; update(); },
                values: () => mode.value === "value" ? splitRoles(value.value) : [],
                set: list => { value.value = list.join(", "); mode.value = list.length ? "value" : "unknown"; update(); }};
      }
      const recipient = roleField("Кому", item.reference_recipient), sender = roleField("Від кого", item.reference_sender);

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
      form.append(textField, lint, chips, roles, multi, actions);
      form.addEventListener("submit", event => { event.preventDefault(); submit(); });
      const controller = { card, audio, item, dirty:false, done: item.reference !== null, setCategory, submit,
                           focusText: () => { if (!textField.hidden) input.focus(); }, suggest,
                           refreshHints: () => { if (!textField.hidden) { suggest(); checkText(0); } } };
      reviewCards.push(controller);
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
  rememberValue("recording-compact", "radio345:archive-compact", { checkbox: true });
  const applyCompact = () => $("recordings").classList?.toggle("compact", $("recording-compact").checked);
  $("recording-compact").addEventListener("change", applyCompact);
  applyCompact();

  const loaders = { recordings: () => recordings(true), overview, filter: filterView, dictionaries, codes: dailyCodes, texts, learning, settings: async () => { await settings(); await runtimeSettings(); }, users, activity: () => activity(), system, quality, experiments, preview: roles };

  // ---------- Швидка розмітка архіву: активна картка, клавіші, швидкість ----------
  const REVIEW_PREFS = "radio345:review-prefs";
  let reviewPrefs = { speed: "1", autoplay: true };
  try { reviewPrefs = { ...reviewPrefs, ...JSON.parse(localStorage.getItem(REVIEW_PREFS) || "{}") }; } catch (_) { /* типово */ }
  const savePrefs = () => { try { localStorage.setItem(REVIEW_PREFS, JSON.stringify(reviewPrefs)); } catch (_) { /* сесія */ } };
  $("recording-speed").value = reviewPrefs.speed;
  $("recording-autoplay").checked = reviewPrefs.autoplay;
  $("recording-speed").addEventListener("change", () => {
    reviewPrefs.speed = $("recording-speed").value; savePrefs();
    for (const c of reviewCards) c.audio.playbackRate = Number(reviewPrefs.speed);
  });
  $("recording-autoplay").addEventListener("change", () => { reviewPrefs.autoplay = $("recording-autoplay").checked; savePrefs(); });
  document.addEventListener("play", event => {
    if (event.target instanceof HTMLMediaElement && event.target.closest('[data-panel="recordings"]'))
      event.target.playbackRate = Number(reviewPrefs.speed);
  }, true);

  let activeReview = null;
  function activateReview(controller, scroll = true) {
    activeReview = controller;
    for (const c of reviewCards) c.card.classList.toggle("active", c === controller);
    if (scroll) controller.card.scrollIntoView({ block: "center", behavior: "smooth" });
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

  // ---------- Автооновлення ----------
  let refreshPending = false;
  async function refreshArchiveNotice() {
    if (!archiveBaseline) return;
    const query = JSON.stringify(recordingFilterIds.map(id => $(id).value));
    const params = new URLSearchParams({view: $("recording-view").value, limit: "1"});
    const since = periodSince(); if (since) params.set("since", since);
    if (searchQuery()) params.set("q", searchQuery());
    if ($("recording-edits").value) params.set("edits", $("recording-edits").value);
    const page = await request(`/api/recordings?${params}`);
    if (view !== "recordings" || query !== JSON.stringify(recordingFilterIds.map(id => $(id).value))) return;
    const changed = archiveBaseline.query !== query || page.total !== archiveBaseline.total || page.items[0]?.id !== archiveBaseline.first;
    $("archive-update-note").textContent = changed
      ? "Є нові або змінені записи. Натисніть «Оновити записи», коли завершите правки."
      : "Список актуальний. Картки не перерисовуються автоматично.";
  }
  async function refreshPanel() {
    if (document.visibilityState !== "visible" || busy || refreshPending) return;
    refreshPending = true;
    try {
      await overview();
      if (view === "system") await system();
      else if (view === "filter") await filterView();
      else if (view === "activity") await activity();
      else if (view === "recordings") await refreshArchiveNotice();
    } catch (_) {
      $("updated").textContent = "Не вдалося оновити дані — повторюємо автоматично";
    } finally { refreshPending = false; }
  }
  function schedule() {
    clearInterval(timer);
    timer = setInterval(refreshPanel, REFRESH_MS);
  }
  document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible") refreshPanel(); });
  window.addEventListener("online", refreshPanel);

  // ---------- Вхід ----------
  action(async () => {
    const token = location.hash.slice(1);
    history.replaceState({ view: "overview" }, "", location.pathname + location.search);
    if (token) await request("/session", "POST", { token });
    await overview();
    loaded.add("overview");
    $("content").hidden = false;
    note("Локальний доступ підтверджено.");
    schedule();
    // ?view=codes тощо — одразу потрібний розділ (зручно для закладок).
    let wanted = new URLSearchParams(location.search).get("view");
    // Колишня вкладка «Фрази» тепер — вид «Надіслано в Telegram» в «Аудіоархіві».
    if (wanted === "phrases") { $("recording-view").value = "telegram"; wanted = "recordings"; }
    if (wanted === "sounds") wanted = "filter";   // зразки тепер на сторінці «Сигнали»
    // ?view=recordings&q=1a2b3c4d — посилання одразу на потрібні записи.
    const query = new URLSearchParams(location.search).get("q");
    if (query) $("recording-search").value = query.slice(0, 200);
    if (wanted && titles[wanted] && wanted !== "overview") { busy = false; show(wanted, { history: "replace" }); }
  }).then(() => {
    // Справжню помилку вже показано; підказку про вхід — лише якщо ключа не було.
    if ($("content").hidden && $("feedback").textContent === "Перевіряємо доступ…")
      note("Немає доступу. Відкрийте панель командою: .venv/bin/python -m api.local_dashboard --open");
  });
})();
