  // ---------- Черги розмітки з поточного архіву ----------
  // Види «розмітки» показують лише нерозмічене: розмічений запис зникає зі списку.
  const TODO_VIEWS = ["radio", "speech", "filtered"];
  const STATUS_HELP = {
    ready: "Текст дійшов до обробки: шукаються позивні й команди; подія й Telegram — за режимами користувачів",
    ready_fallback: "Обробка тексту дала збій — далі пішов текст Whisper без виправлень",
    empty_text: "Whisper тексту не дав (часто тиша або зациклення)", low_rms: "Затихо — до Whisper не дійшло",
    blacklist: "Текст збігся з фразою-галюцинацією з чорного списку — відкинуто", garbage: "Довге сміття Whisper — відкинуто",
    short_garbage: "Короткий шум зі списку — відкинуто", bare_callsign: "Лише позивний без змісту — відкинуто",
    notification: "Фільтр сигналів визнав звуком сповіщення — до Whisper не дійшло",
    dictionary_echo: "Whisper переписав перелік назв із підказки — відкинуто", low_confidence: "Коротка фраза з низькою впевненістю — відкинуто",
  };
  function copyChip(text, value, what) {
    const chip = el("button", "rec-copy", text); chip.type = "button"; chip.title = `${what}: клік — скопіювати`;
    chip.addEventListener?.("click", event => {
      event.stopPropagation?.();
      navigator.clipboard?.writeText(value).then(() => note(`${what} скопійовано: ${value}`), () => note(value));
    });
    return chip;
  }
  // ---------- «↶ Скасувати» швидку розмітку ----------
  // Кілька швидких натискань поспіль — стек: кожне «Скасувати» (кнопка чи ⌘Z / Ctrl+Z) повертає останнє.
  const undoStack = [];
  let undoToast = null, undoTimer = null;
  const UNDO_MS = 15000;
  function offerUndo(entry) {
    undoStack.push(entry); if (undoStack.length > 10) undoStack.shift();
    renderUndo(true);
  }
  function renderUndo(restart) {
    if (typeof document === "undefined" || !document.body?.append) return;
    if (!undoToast) {
      undoToast = el("div", "undo-toast"); undoToast.setAttribute("role", "status");
      const text = el("span", "undo-text"), button = el("button", "undo-button", "↶ Скасувати"), bar = el("i", "undo-bar");
      button.type = "button"; button.title = "Повернути запис, як був до цієї розмітки (⌘Z / Ctrl+Z)";
      button.addEventListener("click", () => undoLast());
      undoToast.append(text, button, el("kbd", "undo-key", "⌘Z"), bar); undoToast.hidden = true;
      document.body.append(undoToast);
    }
    if (!undoStack.length) { undoToast.hidden = true; clearTimeout(undoTimer); return; }
    // Плашка — впритул праворуч від кнопок типу активної картки, на тому самому рядку (туди щойно клікали);
    // поле тексту під кнопками не закриває. Без картки — по центру екрана.
    const anchor = typeof activeReview !== "undefined" && activeReview?.card?.querySelector?.(".recording-type");
    if (anchor) {
      if (undoToast.parentElement !== anchor) anchor.append(undoToast);
      undoToast.classList.add("anchored");
      const buttons = anchor.querySelector?.(".segmented");
      if (buttons && undoToast.style) undoToast.style.left = `${buttons.offsetWidth + 10}px`;
    }
    else { if (undoToast.parentElement !== document.body) document.body.append(undoToast); undoToast.classList.remove("anchored"); undoToast.style.left = ""; }
    const last = undoStack[undoStack.length - 1];
    undoToast.querySelector(".undo-text").textContent = `Позначено «${last.title}»${undoStack.length > 1 ? ` · і ще ${undoStack.length - 1}` : ""}`;
    undoToast.hidden = false;
    if (restart) {
      const bar = undoToast.querySelector(".undo-bar"); bar.style.animation = "none"; void bar.offsetWidth; bar.style.animation = "";
      clearTimeout(undoTimer);
      undoTimer = setTimeout(() => { undoStack.length = 0; renderUndo(); }, UNDO_MS);
    }
  }
  async function undoLast() {
    const entry = undoStack.pop(); renderUndo(false);
    if (!entry) return;
    try { await entry.run(); note(`Розмітку «${entry.title}» скасовано.`); }
    catch (error) { note(`Не вдалося скасувати: ${error.message}`); }
  }
  // Короткі назви видів — для плитки «У списку» (повна назва — у підказці).
  const VIEW_SHORT = { radio: "Ефір", person: "Правки людини", speech: "Інше мовлення", filtered: "Відсіяне", reviewed: "Розмічені", miniapp: "Mini App",
                       telegram: "Telegram", all: "Усі", false_text: "Хибний текст", missed: "Пропущене", no_roles: "Без ролей",
                       unclear: "Нерозбірливі" };
  // ---------- Пігулки легенди над переліком: клік — такі записи першими ----------
  // Сортування лише на екрані (серед завантажених), стабільне; повторний клік — звичайний порядок.
  let rowMark = null;
  const ROW_MARK = {
    todo: row => row.classList.contains("todo"), done: row => row.classList.contains("done"),
    remarks: row => row.classList.contains("has-remarks") || row.classList.contains("dirty"),
    conflict: row => row.classList.contains("conflict"),
    miniapp: row => row.dataset.miniapp === "1", telegram: row => row.dataset.telegram === "1",
  };
  function applyRowMark() {
    if (typeof document === "undefined" || !document.querySelectorAll) return;
    const box = $("recording-rows"), rows = [...box.children];
    rows.forEach((row, i) => { if (row.dataset.order === undefined) row.dataset.order = String(i); });
    const original = rows.sort((a, b) => Number(a.dataset.order) - Number(b.dataset.order));
    const ordered = rowMark ? [...original.filter(ROW_MARK[rowMark]), ...original.filter(r => !ROW_MARK[rowMark](r))] : original;
    box.append(...ordered);
    for (const pill of document.querySelectorAll(".row-legend [data-mark]")) {
      pill.classList.toggle("on", pill.dataset.mark === rowMark);
      pill.setAttribute("aria-pressed", String(pill.dataset.mark === rowMark));
    }
    box.scrollTop = 0;
  }
  if (typeof document !== "undefined" && document.querySelectorAll) {
    for (const pill of document.querySelectorAll(".row-legend [data-mark]")) {
      const pick = () => { rowMark = rowMark === pill.dataset.mark ? null : pill.dataset.mark; applyRowMark(); };
      pill.addEventListener("click", pick);
      pill.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); pick(); } });
    }
  }
  const ROW_KIND = { speech: "Мовлення", unclear: "Нерозбірливо", noise: "Шум", notification: "Сигнал", room: "Кімната" };
  function recordingRow(item, controller) {
    const row = el("div", "recording-row");
    row.tabIndex = 0; row.setAttribute?.("role", "button");
    // Для сортування за пігулками легенди: правки з Mini App і надіслане в Telegram.
    if (row.dataset) { row.dataset.miniapp = item.corrections?.length ? "1" : ""; row.dataset.telegram = item.data?.fix_id ? "1" : ""; }
    const at = new Date(item.created * 1000);
    const today = at.toDateString() === new Date().toDateString();
    // Час — найпомітніше в рядку: години й хвилини великим, секунди дрібно; не сьогодні — ще й дата.
    const pad = n => String(n).padStart(2, "0");
    const time = el("span", "row-time");
    time.append(el("b", "row-hm", `${pad(at.getHours())}:${pad(at.getMinutes())}`), el("small", "row-sec", `:${pad(at.getSeconds())}`));
    if (!today) time.append(el("small", "row-day", `${pad(at.getDate())}.${pad(at.getMonth() + 1)}`));
    const d = item.data || {};
    const kind = el("span", "row-kind"), metaNode = el("span", "row-meta"), text = el("div", "row-text");
    // Рядок перемальовується з картки: після будь-якої зміни поля (і кнопками) та після збереження.
    function refresh(typed) {
      const event = /^db:(\d+)$/.exec(d.fix_id || "");
      kind.textContent = item.reference != null ? ROW_KIND[item.label] || item.label
        : ((typeof STATUS !== "undefined" && STATUS[item.status]) || item.status);
      const duration = d.signal?.duration_s;
      metaNode.textContent = [item.corrections?.length ? "✎" : "", typeof duration === "number" ? `${duration.toFixed(1).replace(".", ",")} с` : "",
                    event ? `№${event[1]}` : "", d.fix_id ? "✈" : "",
                    // У розмічених (вони йдуть за часом розмітки) — ще й коли розмічено.
                    item.reviewed_at && ["reviewed", "miniapp"].includes($("recording-view").value)
                      ? `✓ ${new Date(item.reviewed_at * 1000).toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" })}` : ""].filter(Boolean).join(" · ");
      // Незбережена правка — як набрано; інакше еталон оператора звичайним, текст Whisper нерозміченого — приглушено.
      const heard = (d.raw_text || "").trim();
      const editing = typed !== undefined && controller.dirty;
      text.className = editing || item.reference != null ? "row-text" : "row-text heard";
      text.textContent = editing ? typed || "—" : item.reference || heard || "Whisper: порожньо — варто прослухати";
      // Суперечність розмітки: «мовлення» без тексту або шум / кімната / сигнал із текстом.
      const reference = (item.reference || "").trim(), speechLike = ["speech", "unclear"].includes(item.label);
      const conflict = item.reference != null && (speechLike ? !reference : Boolean(reference));
      row.classList.toggle("conflict", conflict);
      row.classList.toggle("empty", item.reference == null && !heard);
      row.classList.toggle("doubtful", doubtful(item));
      row.title = conflict ? (speechLike ? "Позначено мовлення, але тексту немає" : "Позначено без мовлення, але є текст")
        : doubtful(item) ? "Whisper не впевнений — найімовірніше, текст треба правити" : "";
    }
    refresh();
    const head = el("div", "row-head");
    head.append(time, kind, metaNode);
    const body = el("div", "row-main"); body.append(head, text);
    row.append(body);
    // Видалення в Telegram — частина рядка праворуч (лише для свіжих надісланих записів).
    if (item.telegram?.copies && item.telegram.deletable) { const recall = telegramRecall(item); recall.classList?.add("row-action"); recall.addEventListener("click", event => event.stopPropagation()); row.append(recall); }
    row.addEventListener("click", () => activateReview(controller, false));
    row.addEventListener("keydown", event => { if (event.key === "Enter") activateReview(controller, false); });   // далі Enter веде до тексту
    $("recording-rows").append(row);
    controller.refreshRow = refresh;
    if (typeof MutationObserver !== "undefined") {
      const sync = () => {
        for (const name of ["active", "done", "dirty", "has-remarks"]) row.classList.toggle(name, controller.card.classList.contains(name));
        row.classList.toggle("todo", !controller.card.classList.contains("done"));
      };
      new MutationObserver(sync).observe(controller.card, { attributes: true, attributeFilter: ["class"] }); sync();
    }
    return row;
  }

  // «Далі одразу» — біля «Зберегти» в кожній картці; одне спільне значення (reviewPrefs.autoplay).
  function autoplayToggle() {
    const on = typeof reviewPrefs === "undefined" ? true : reviewPrefs.autoplay;
    const toggle = el("button", on ? "ghost autoplay-toggle on" : "ghost autoplay-toggle", "⏭ Далі одразу"); toggle.type = "button";
    toggle.title = "Після збереження одразу грати наступний запис";
    toggle.setAttribute?.("aria-pressed", String(on));
    toggle.addEventListener?.("click", () => {
      reviewPrefs.autoplay = !reviewPrefs.autoplay; savePrefs();
      $("recording-autoplay").checked = reviewPrefs.autoplay;
      for (const t of document.querySelectorAll(".autoplay-toggle")) {
        t.className = reviewPrefs.autoplay ? "ghost autoplay-toggle on" : "ghost autoplay-toggle";
        t.setAttribute("aria-pressed", String(reviewPrefs.autoplay));
      }
    });
    return toggle;
  }

  // Швидкість відтворення — біля кожного плеєра; спільна для всіх карток (setSpeed нижче).
  const SPEEDS = ["0.75", "0.85", "1", "1.25", "1.5", "1.75", "2"];
  function speedSelect() {
    const select = el("select", "speed-select");
    select.title = "Швидкість відтворення (клавіші ] і [). Тон голосу не змінюється.";
    select.setAttribute?.("aria-label", "Швидкість відтворення");
    for (const value of SPEEDS) { const o = el("option", "", `${value.replace(".", ",")}×`); o.value = value; select.append?.(o); }
    select.value = typeof reviewPrefs === "undefined" ? "1" : String(reviewPrefs.speed);
    select.addEventListener?.("change", () => { setSpeed(select.value); select.blur?.(); });
    return select;
  }
  // ---------- Власний плеєр у стилі панелі ----------
  // Стандартні елементи <audio> браузер майже не дає стилізувати. Сам <audio> лишається (на ньому
  // клавіші, автопрогравання й пауза мікрофона), керування — своє: кнопка, хвиля запису, час, швидкість.
  // Хвиля малюється з самого WAV (PCM16) — лише коли запис відкрили чи ввімкнули, не для всіх карток.
  const clock = s => !Number.isFinite(s) ? "0:00" : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
  // Лише орієнтир для розмітки: RMS-паузи + вага довжини слова, без ASR/моделі.
  function wavSpeechGuide(buffer) {
    const view = new DataView(buffer);
    let channels = 0, rate = 0, format = 0, bits = 0;
    for (let offset = 12; offset + 8 <= view.byteLength;) {
      const id = String.fromCharCode(...new Uint8Array(buffer, offset, 4)), size = view.getUint32(offset + 4, true);
      const start = offset + 8, end = Math.min(view.byteLength, start + size);
      if (id === "fmt " && end - start >= 16) {
        format = view.getUint16(start, true); channels = view.getUint16(start + 2, true);
        rate = view.getUint32(start + 4, true); bits = view.getUint16(start + 14, true);
      }
      if (id === "data") {
        if (format !== 1 || bits !== 16 || !rate || !channels) return null;
        const frames = Math.floor((end - start) / (2 * channels)), step = Math.max(1, Math.round(rate * .02));
        const stride = Math.max(1, Math.floor(rate / 4000)); // огинаюча, не спектральний аналіз
        const duration = frames / rate, levels = [];
        for (let frame = 0; frame < frames; frame += step) {
          let sum = 0, count = 0;
          for (let f = frame; f < Math.min(frames, frame + step); f += stride) {
            for (let c = 0; c < channels; c++) { const v = view.getInt16(start + (f * channels + c) * 2, true); sum += v * v; count++; }
          }
          levels.push(Math.sqrt(sum / Math.max(1, count)));
        }
        const sorted = [...levels].sort((a,b) => a-b), top = sorted.at(-1) || 0;
        if (!top) return {duration, spans: []};
        const floor = sorted[Math.floor(sorted.length * .15)] || 0;
        const threshold = Math.min(top * .35, Math.max(top * .06, floor * 2));
        const spans = [];
        for (let i = 0; i < levels.length; i++) {
          if (levels[i] < threshold) continue;
          const a = Math.max(0, (i * step) / rate - .04), b = Math.min(duration, ((i+1) * step) / rate + .04);
          const last = spans.at(-1);
          if (last && a - last[1] < .12) last[1] = b;
          else spans.push([a,b]);
        }
        return {duration, spans};
      }
      offset = start + size + (size % 2);
    }
    return null;
  }
  function approximateWordCues(text, guide) {
    if (!guide?.spans?.length) return [];
    const words = [...text.matchAll(/[\p{L}\p{N}]+(?:[’'ʼ`-][\p{L}\p{N}]+)*/gu)];
    const weights = words.map(m => /^\d+$/.test(m[0]) ? Math.max(1, m[0].length) :
      Math.max(.8, (m[0].match(/[аеєиіїоуюяaeiouy]/gi) || []).length + m[0].length * .12));
    const total = weights.reduce((a,b) => a+b, 0), voiced = guide.spans.reduce((sum,[a,b]) => sum+b-a, 0);
    let used = 0;
    const at = value => {
      for (const [a,b] of guide.spans) { if (value < b-a) return a+value; value -= b-a; }
      return guide.spans.at(-1)[1];
    };
    return words.map((m,i) => {
      const start = at(used / total * voiced); used += weights[i];
      return {start, end:at(used / total * voiced), from:m.index, to:m.index+m[0].length};
    });
  }
  function wavPeaks(buffer, bars) {
    const view = new DataView(buffer);
    let offset = 12, bits = 16, channels = 1;
    while (offset + 8 <= view.byteLength) {
      const id = String.fromCharCode(...new Uint8Array(buffer, offset, 4)), size = view.getUint32(offset + 4, true);
      if (id === "fmt ") { channels = view.getUint16(offset + 10, true); bits = view.getUint16(offset + 22, true); }
      if (id === "data") {
        if (bits !== 16) return null;
        const samples = new Int16Array(buffer.slice(offset + 8, offset + 8 + Math.min(size, view.byteLength - offset - 8)));
        const step = Math.max(1, Math.floor(samples.length / channels / bars)), peaks = [];
        for (let b = 0; b < bars; b++) {
          let peak = 0;
          for (let i = b * step * channels; i < Math.min(samples.length, (b + 1) * step * channels); i += channels) peak = Math.max(peak, Math.abs(samples[i]));
          peaks.push(peak);
        }
        const top = Math.max(1, ...peaks);
        return peaks.map(p => Math.sqrt(p / top));   // корінь — тихе мовлення теж видно
      }
      offset += 8 + size + (size % 2);
    }
    return null;
  }
  function deckPlayer(audio, url) {
    const node = el("div", "recording-player deck-player");
    const play = el("button", "deck-play", "▶"); play.type = "button";
    play.title = "Грати / пауза (Пробіл)"; play.setAttribute?.("aria-label", "Грати");
    const wave = el("canvas", "deck-wave"); wave.setAttribute?.("aria-label", "Позиція відтворення — клік, щоб перейти");
    const time = el("span", "deck-time", "0:00");
    // Незбережені картки — пігулка тут, біля плеєра (у шапці вона рвала ряд плиток).
    const drafts = el("button", "deck-drafts", "✓ усе збережено"); drafts.type = "button"; drafts.disabled = true;
    drafts.addEventListener?.("click", () => {
      const first = reviewCards.find(card => card.dirty);
      if (first && typeof activateReview === "function") activateReview(first);
    });
    node.append(play, wave, time, speedSelect(), drafts);
    let peaks = null, loading = false, knownDuration = NaN, frame = 0, guide = null, textGuide = null, disposed = false, blobUrl = null;
    // Кольори хвилі читаються раз (getComputedStyle на кожному кадрі гальмував), полотно — лише коли змінився піксель.
    let colors = null, drawn = "";
    const duration = () => Number.isFinite(audio.duration) ? audio.duration : knownDuration;
    function draw() {
      if (disposed) return;
      textGuide?.(audio.currentTime || 0, guide);
      if (!wave.getContext || !wave.clientWidth) return;
      const ratio = window.devicePixelRatio || 1, w = wave.clientWidth, h = wave.clientHeight;
      if (wave.width !== Math.round(w * ratio)) { wave.width = Math.round(w * ratio); wave.height = Math.round(h * ratio); }
      const progress = duration() > 0 ? Math.min(1, audio.currentTime / duration()) : 0;
      time.textContent = `${clock(audio.currentTime)} / ${clock(duration())}`;
      const key = `${w}|${h}|${ratio}|${Boolean(peaks)}|${Math.round(progress * w)}`;
      if (key === drawn) return;
      drawn = key;
      if (!colors) { const css = getComputedStyle(wave);
        colors = { played: css.getPropertyValue("--wave-played").trim() || "#b8ef86", rest: css.getPropertyValue("--wave-rest").trim() || "rgba(160,175,190,.4)" }; }
      const { played, rest } = colors;
      const ctx = wave.getContext("2d"); ctx.setTransform(ratio, 0, 0, ratio, 0, 0); ctx.clearRect(0, 0, w, h);
      const n = Math.max(8, Math.floor(w / 3)), values = peaks ? peaks : null;
      for (let i = 0; i < n; i++) {
        const v = values ? values[Math.min(values.length - 1, Math.floor(i * values.length / n))] : 0.08;
        const bar = Math.max(2, v * (h - 4));
        ctx.fillStyle = (i + 0.5) / n <= progress ? played : rest;
        ctx.fillRect(i * 3, (h - bar) / 2, 2, bar);
      }
      if (progress > 0) { ctx.fillStyle = played; ctx.fillRect(Math.min(w - 2, progress * w), 0, 2, h); }
    }
    async function loadWave() {
      if (disposed || peaks || loading || typeof fetch === "undefined") return;
      loading = true;
      try {
        const response = await fetch(url, { credentials: "same-origin" });
        const buffer = await response.arrayBuffer();
        if (disposed) return;
        peaks = wavPeaks(buffer, 240);
        guide = wavSpeechGuide(buffer);
        // Звук — із уже завантаженого файлу, без другого запиту (якщо ще не почали грати).
        if (audio.paused && !audio.currentTime && !audio.readyState && typeof URL !== "undefined" && URL.createObjectURL)
          audio.src = blobUrl = URL.createObjectURL(new Blob([buffer], { type: "audio/wav" }));
        const view = new DataView(buffer);   // тривалість із заголовка: видно ще до відтворення
        if (buffer.byteLength > 44) knownDuration = (buffer.byteLength - 44) / Math.max(1, view.getUint32(28, true));
      } catch (_) { /* без хвилі — просто смуга */ }
      loading = false; draw();
    }
    const tick = () => { if(disposed)return; draw(); if (!audio.paused) frame = window.requestAnimationFrame?.(tick); };
    audio.addEventListener?.("play", () => { play.textContent = "❚❚"; play.classList?.add("on"); play.setAttribute?.("aria-label", "Пауза"); loadWave(); tick(); });
    audio.addEventListener?.("pause", () => { play.textContent = "▶"; play.classList?.remove("on"); play.setAttribute?.("aria-label", "Грати"); window.cancelAnimationFrame?.(frame); draw(); });
    for (const name of ["loadedmetadata", "seeked", "ended", "timeupdate"]) audio.addEventListener?.(name, draw);
    play.addEventListener?.("click", () => { if (audio.paused) audio.play().catch(() => {}); else audio.pause(); });
    // Клік чи протягування по хвилі — перейти; до першого відтворення тривалість беремо з WAV.
    const seek = event => {
      const box = wave.getBoundingClientRect(), r = Math.max(0, Math.min(1, (event.clientX - box.left) / box.width));
      if (duration() > 0) { audio.currentTime = r * duration(); draw(); }
    };
    wave.addEventListener?.("pointerdown", event => { seek(event); wave.setPointerCapture?.(event.pointerId);
      const move = e => seek(e), up = () => { wave.removeEventListener("pointermove", move); wave.removeEventListener("pointerup", up); };
      wave.addEventListener("pointermove", move); wave.addEventListener("pointerup", up); });
    const resized = () => { colors = null; drawn = ""; draw(); };
    window.addEventListener?.("resize", resized);
    return { node, drafts, loadWave: () => loadWave().then(draw), draw,
      syncText: () => { if (!disposed) textGuide?.(audio.currentTime || 0, guide); },
      dispose: () => { disposed=true; audio.pause?.(); window.cancelAnimationFrame?.(frame);
        window.removeEventListener?.('resize',resized); if(blobUrl)URL.revokeObjectURL?.(blobUrl); textGuide=null; },
      setTextGuide: callback => { textGuide = callback; draw(); } };
  }
  // Правки людини — пункт виду «person:<ID>»: на сервер іде вид «усі» з фільтром edits=<ID>.
  const editsValue = () => { const v = $("recording-view").value || ""; return v.startsWith("person:") ? v.slice(7) : ""; };
  const apiView = () => editsValue() ? "all" : $("recording-view").value;
  // Пункт «Правки: Ім'я (N)» — є завжди для людей із правками; для переходу зі сторінки людини — додається.
  function personOption(id, name, count) {
    const group = $("recording-people"), value = `person:${id}`;
    let option = [...(group.children || [])].find(o => o.value === value);
    if (!option) { option = el("option", "", ""); option.value = value; group.append(option); }
    option.textContent = option.dataset.title = `Правки: ${name}${count ? ` (${count})` : ""}`;
    return option;
  }
  // Календарні доби («сьогодні», «вчора», «позавчора») — від опівночі; числа — останні N годин.
  const DAYS_BACK = { today: 0, yesterday: 1, daybefore: 2 };
  function midnight(daysBack) { const d = new Date(); d.setHours(0, 0, 0, 0); d.setDate(d.getDate() - daysBack); return Math.floor(d.getTime() / 1000); }
  function periodSince() {
    const value = $("recording-period").value;
    if (!value) return null;
    if (value in DAYS_BACK) return midnight(DAYS_BACK[value]);
    return Math.floor(Date.now() / 1000) - Number(value) * 3600;
  }
  function periodUntil() {
    const value = $("recording-period").value;
    return value in DAYS_BACK && DAYS_BACK[value] > 0 ? midnight(DAYS_BACK[value] - 1) : null;
  }
  let recordingOffset = 0;
  const reviewCards = [];
  let archiveBaseline = null;
  let loadedRecordingFilters = null;
  const recordingFilterIds = ["recording-view", "recording-period", "recording-order", "recording-search"];
  // «Сумнівні першими»: спершу записи, де Whisper найменш упевнений — їх найімовірніше треба правити.
  let archiveSelection = '';
  const orderValue = () => ["doubtful", "old"].includes($("recording-order")?.value) ? $("recording-order").value : "new";
  let confidenceLow = null;   // межа найменш упевнених 25 % розміченого мовлення (з сервера)
  const recordConfidence = d => {
    const values = (d.whisper_segments || []).map(s => s.avg_logprob).filter(v => typeof v === "number");
    return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
  };
  const doubtful = item => {   // лише нерозмічені: розмічене вже перевірено
    if (confidenceLow == null || item.reference != null) return false;
    const c = recordConfidence(item.data || {});
    return c != null && c < confidenceLow;
  };
  // Нові записи — на самій кнопці «Оновити» (окремого рядка немає): текст і колір.
  function showArchiveFresh(fresh) {
    const button = $("recording-refresh");
    button.textContent = fresh ? "● Є нові записи — оновити" : "⟳ Оновити";
    button.classList?.toggle("fresh", fresh);
  }
  let shownDraftCount = null, shownDraftCards = 0;
  function updateDraftCount() {
    const count = reviewCards.filter(card => card.dirty).length;
    // Перемальовувати плашки всіх карток — лише коли число змінилося (інакше кожне натискання чіпало всю сторінку).
    if (count === shownDraftCount && reviewCards.length === shownDraftCards) return;
    shownDraftCount = count; shownDraftCards = reviewCards.length;
    // Пігулка біля плеєра кожної картки — завжди на місці (нічого не стрибає), лише перефарбовується:
    // сіра «усе збережено» або жовта з кількістю; клік веде до першої незбереженої.
    $("archive-drafts-nav").hidden = !count;
    for (const card of reviewCards) {
      const pill = card.draftsPill; if (!pill) continue;
      pill.className = count ? "deck-drafts pending" : "deck-drafts";
      pill.disabled = !count;
      pill.textContent = count ? `● не збережено: ${count} →` : "✓ усе збережено";
      pill.title = count ? `Незбережених карток: ${count}. Клік — до першої з них` : "Незбережених змін немає";
    }
    $("archive-drafts-nav").textContent = String(count);
    $("archive-drafts-nav").title = `Незбережених карток: ${count}`;
  }
  if (typeof window !== "undefined") window.addEventListener("beforeunload", event => {
    if (!reviewCards.some(card => card.dirty)) return;
    event.preventDefault(); event.returnValue = "";
  });
  // Пошук за ID («1a2b3c4d, 5e6f7a8b»), номером події («№82») або текстом — по всьому архіву.
  const searchQuery = () => $("recording-search").value.trim();
  // Видалити надіслану фразу в усіх отримувачів (Telegram дозволяє лише протягом 48 год).
  function telegramRecall(item) {
    const t = item.telegram;
    if (t.pending) return el("span", "badge", `видаляється в Telegram… (${t.pending})`);
    if (!t.exhausted && !t.copies && (t.requested || t.hidden)) return el("span", "badge", "видалено в Telegram");
    if (!t.copies) return el("span", "badge", "у чатах копій немає");
    if (!t.deletable) {
      const old = el("span", "badge", `у чатах: ${t.copies}`);
      old.title = "Минуло понад 48 год — Telegram не дозволяє ботові видалити повідомлення";
      return old;
    }
    const hours = Math.max(1, Math.floor((t.deletable_until * 1000 - Date.now()) / 3600000));
    const b = el("button", "ghost small-button danger-text", t.exhausted ? `Повторити видалення (${t.exhausted})` : `🗑 у всіх (${t.copies})`); b.type = "button";
    b.title = `Видалити в Telegram у всіх: повідомлення зникне в ${t.copies} отримувачів і в журналі. Можна ще ≈${hours} год.`;
    b.addEventListener("click", () => action(async () => {
      if (!confirm(`Видалити це повідомлення в Telegram в усіх отримувачів (${t.copies})? Фраза також зникне з журналу й Mini App. Скасувати не можна.`)) return;
      const r = await request(`/api/recordings/${item.id}/telegram-delete`, "POST");
      item.telegram = {...item.telegram, ...r};
      b.replaceWith(telegramRecall(item));
      note(r.queued ? `Видаляється в ${r.queued} чатах — бот зробить це за кілька секунд (якщо приймач запущено).` : "Фразу приховано; копій у чатах не лишилося.");
    }));
    return b;
  }

  let archiveCodes = null;
  // Доба кодів запису: коди змінюються о change_hour (20:00), тож вечірній запис належить наступній добі.
  function codesDay(created) {
    const at = new Date(created * 1000), hour = archiveCodes?.change_hour ?? 20;
    if (at.getHours() >= hour) at.setDate(at.getDate() + 1);
    return `${at.getFullYear()}-${String(at.getMonth() + 1).padStart(2, "0")}-${String(at.getDate()).padStart(2, "0")}`;
  }
  function codesApron(created) {
    const box = el("div", "codes-apron");
    if (!archiveCodes) { box.hidden = true; return box; }
    const day = codesDay(created), entry = (archiveCodes.items || []).find(e => e.day === day);
    const label = `${day.slice(8, 10)}.${day.slice(5, 7)}`;
    box.append(el("span", "codes-label", `Коди доби ${label}`));
    if (entry?.codes?.length) for (const code of entry.codes) box.append(el("b", "codes-code", code));
    else { box.classList?.add("missing"); box.append(el("span", "codes-missing", "не внесено")); }
    box.title = entry?.codes?.length ? `Коди доби запису (${entry.period?.join(" — ") || label}); постійні не показано. Клік — сторінка кодів`
                                     : "Коди доби запису ще не внесено. Клік — внести";
    box.addEventListener?.("click", event => { event.preventDefault(); if (typeof show === "function") show("codes"); });
    return box;
  }
  async function recordings(reset = true) {
    const selectionExit = $("clear-recording-selection");
    if (selectionExit) selectionExit.hidden = !archiveSelection;
    if (reset && reviewCards.some(card => card.dirty) && !confirm("Є незбережені зміни. Оновити список і відкинути їх?")) {
      if (loadedRecordingFilters) for (const id of recordingFilterIds) $(id).value = loadedRecordingFilters[id];
      return;
    }
    if (reset) { for (const c of reviewCards) c.dispose?.(); recordingOffset = 0; reviewCards.length = 0; updateDraftCount(); $("recordings").replaceChildren(); $("recording-rows").replaceChildren(); }
    const since = periodSince(), until = periodUntil();
    const [page, summary, codes] = await Promise.all([
      request(`/api/recordings?offset=${recordingOffset}&view=${apiView()}&order=${orderValue()}` +
        (archiveSelection ? `&selection=${archiveSelection}` : "") +
        (since ? `&since=${since}` : "") + (until ? `&until=${until}` : "") +
        (searchQuery() ? `&q=${encodeURIComponent(searchQuery())}` : "") +
        (editsValue() ? `&edits=${encodeURIComponent(editsValue())}` : "")),
      request("/api/recordings/summary"),
      request("/api/daily-codes").catch(() => null)   // нагадування над полем тексту: коди доби запису
    ]);
    archiveCodes = codes;
    confidenceLow = page.confidence_low ?? null;
    if (reset) archiveBaseline = {query: JSON.stringify(recordingFilterIds.map(id => $(id).value)), total: page.total, first: page.items[0]?.id};
    showArchiveFresh(false);
    loadedRecordingFilters = Object.fromEntries(recordingFilterIds.map(id => [id, $(id).value]));
    // Люди з правками в Mini App — пунктами виду (обраний лишається обраним).
    { const chosen = $("recording-view").value;
      for (const person of page.editors || []) personOption(person.user_id, person.name, person.corrections);
      $("recording-view").value = chosen; }
    // Кількість записів у кожному виді — за обраний період. Список короткий: порожні й рідкісні види
    // (черга, відсіяне, правки людей) приховані, крім обраного — туди ведуть посилання з інших сторінок.
    { const chosen = $("recording-view").value;
      for (const option of $("recording-view").options) {
        option.dataset.title ||= option.textContent;
        const count = page.views?.[option.value];
        option.textContent = `${option.dataset.title}${typeof count === "number" ? ` · ${count}` : ""}`;
        const rare = option.dataset.rare !== undefined || option.value.startsWith("person:");
        const useful = ["speech", "captured"].includes(option.value) && count > 0;   // не порожні — видимі, хоч і рідкісні
        option.hidden = option.value !== chosen && !useful && (rare || count === 0);
      }
      for (const group of $("recording-view").querySelectorAll?.("optgroup") || [])
        group.hidden = ![...group.children].some(option => !option.hidden); }
    const quality = summary.quality;
    $("recording-summary-details").textContent = `Записів: ${Object.values(summary.statuses).reduce((a,b) => a+b, 0)} · перевірено: ${summary.reviewed} · аудіо: ${(summary.bytes / 1048576).toFixed(1)} МБ · WER Whisper: ${pct(quality.raw_text.wer)} · WER виходу: ${pct(quality.final_text.wer)} · мовлення без тексту: ${summary.missed_speech} · хибний текст на шумі ефіру/сигналі: ${summary.false_text} · кімнатний шум: ${summary.room ?? 0} (з текстом: ${summary.room_false_text ?? 0}) · кілька передач у записі: ${summary.multiple_messages ?? 0} з ${summary.speech_reviewed ?? 0} · точність «кому»: ${pct(summary.roles?.recipient?.accuracy)} (${summary.roles?.recipient?.evaluated || 0}) · «від кого»: ${pct(summary.roles?.sender?.accuracy)} (${summary.roles?.sender?.evaluated || 0}). Неперевірені: ${summary.low_rms_retention_seconds != null ? `тихі — ${Math.round(summary.low_rms_retention_seconds / 60)} хв, решта — ` : ""}${summary.retention_days} днів / ${(summary.max_bytes / 1073741824).toFixed(1)} ГБ; перевірені зберігаються.`;
    // Шапка: плитки — головна мета («Без правок», зі шкалою), сьогоднішня робота, WER до → після, записів у виді.
    // Плитки щоразу створюються наново, тож при оновленні значення «спливають» (анімація в CSS).
    {
      const tile = (label, value, extra, tone = "", help = "") => {
        const box = el("div", `stat-tile ${tone}`.trim());
        box.append(el("span", "stat-label", label), el("span", "stat-value", value));
        if (extra) box.append(el("span", "stat-extra", extra));
        if (help) box.title = help;
        return box;
      };
      const share = v => v == null ? "—" : `${Math.round(v * 100)} %`;
      // Головне значення — сьогоднішній ефір (видно дію свіжих налаштувань); за весь час — дрібно нижче.
      const speech = summary.quality.final_text.reviewed_speech || 0;
      const total = speech && typeof summary.no_edits === "number" ? summary.no_edits / speech : null;
      const todaySpeech = summary.speech_today || 0;
      const clean = todaySpeech ? summary.no_edits_today / todaySpeech : total;
      const goal = tile(todaySpeech ? "Без правок сьогодні" : "Без правок", share(clean),
                        [todaySpeech ? `${num(summary.no_edits_today)} з ${num(todaySpeech)}` : "",
                         total == null ? "" : `за весь час ${share(total)}`].filter(Boolean).join(" · "), "goal",
                        "Фрази, де підсумковий текст збігся з еталоном слово в слово — головна мета розпізнавання. " +
                        "Сьогодні — за днем запису.");
      if (clean != null) {
        const bar = el("span", "stat-bar"), fill = el("i");
        fill.style && fill.style.setProperty?.("--share", `${(clean * 100).toFixed(1)}%`);
        bar.append(fill); goal.append(bar);
      }
      const today = summary.reviewed_today;
      // WER: крупно — сьогоднішній ефір (як «Без правок»), дрібно — за весь час.
      const allBefore = quality.raw_text.wer, allAfter = quality.final_text.wer;
      const qt = summary.quality_today, hasToday = qt?.final_text?.wer != null && qt?.raw_text?.wer != null;
      const before = hasToday ? qt.raw_text.wer : allBefore, after = hasToday ? qt.final_text.wer : allAfter;
      const delta = before != null && after != null ? after - before : null;
      const tiles = [goal,
        tile("Сьогодні", typeof today === "number" ? num(today) : "—", `усього ${num(summary.reviewed)}`, today ? "live compact" : "compact",
             "Записів розмічено сьогодні; нижче — усього перевірених"),
        tile(hasToday ? "WER сьогодні до → після" : "WER до → після", `${pct(before)} → ${pct(after)}`,
             [delta == null ? "" : `обробка: ${delta <= 0 ? "−" : "+"}${Math.abs(delta * 100).toFixed(1).replace(".", ",")} п.п. ${delta <= 0 ? "помилок" : "помилок ⚠"}`,
              hasToday ? `за весь час ${pct(allBefore)} → ${pct(allAfter)}` : ""].filter(Boolean).join(" · "),
             delta == null ? "wide" : delta <= 0 ? "good wide" : "bad wide",
             "Частка помилкових слів: сирий текст Whisper → після обробки (позивні, номери, оформлення). " +
             "Крупно — сьогоднішній ефір за днем запису; дрібно — усі розмічені фрази."),
        tile("У списку", num(page.total), VIEW_SHORT[editsValue() ? "person" : $("recording-view").value] || "", "compact",
             `Скільки записів у списку праворуч — за вибраним видом, періодом і пошуком. Вид: ${$("recording-view").selectedOptions?.[0]?.dataset.title || "—"}`)];
      if (searchQuery()) {
        const clear = el("button", "ghost stat-clear", "✕ зняти"); clear.type = "button";
        clear.addEventListener("click", () => { $("recording-search").value = ""; action(() => recordings(true)); });
        const found = tile("Пошук", `«${searchQuery()}»`, "", "search"); found.append(clear); tiles.push(found);
      }

      $("recording-summary").replaceChildren(...tiles);
    }
    if (!page.total && searchQuery()) $("recordings").append(el("p", "muted", "Нічого не знайдено. Шукайте за початком ID (від 4 знаків), №події або шматком тексту."));
    else if (!page.total) $("recordings").append(el("p", "muted", TODO_VIEWS.includes($("recording-view").value)
      ? "Тут усе розмічено. Оберіть інший вид або довший період."
      : "Записів за цим вибором немає. Оберіть інший вид або довший період."));
    for (const item of page.items) {
      const d = item.data, signal = d.signal || {};
      const card = el("article", "panel recording-card");
      const header = el("div", "recording-header");
      const eventNo = /^db:(\d+)$/.exec(d.fix_id || "");
      // Заголовок: час — найпомітніше, дата дрібно; статус — що програма зробила із записом;
      // ID і №події копіюються кліком.
      const at = new Date(item.created * 1000);
      const title = el("div", "recording-title");
      const status = el("span", "rec-status", STATUS[item.status] || (item.status === "vad_short" ? "Закоротко (VAD)" : item.status));
      status.title = STATUS_HELP[item.status] || "Що програма зробила із записом";
      title.append(el("span", "rec-time", at.toLocaleTimeString("uk-UA")), el("span", "rec-date", at.toLocaleDateString("uk-UA")), status,
                   copyChip(item.id.slice(0, 8), item.id, "ID запису"));
      if (eventNo) title.append(copyChip(`№${eventNo[1]}`, eventNo[1], "Номер події"));
      if (doubtful(item)) {
        const chip = el("span", "rec-doubt", "сумнівно");
        chip.title = "Whisper найменш упевнений (нижня чверть) — серед таких фраз без правок майже немає; уважно прослухайте";
        title.append(chip);
      }
      header.append(title);
      const audio = el("audio"); audio.controls = false; audio.preload = "none";
      audio.src = `/api/recordings/${item.id}/audio`;
      // Плеєр — «фартушок» над карткою (як деталі під нею): завжди на виду, не губиться серед полів.
      const custom = deckPlayer(audio, audio.src);
      const deck = el("div", "recording-deck"); deck.append(audio, custom.node);
      card.append(header);
      // «Фартушок» під карткою: текст Whisper (з «Повернути розпізнане») і технічні деталі.
      const apron = el("div", "recording-apron");
      {
        const sample = el("button", "ghost small-button", "＋ До зразків сигналу"); sample.type = "button";
        sample.title = "Зробити цей звук зразком фільтра сигналів (лише чистий сигнал без мовлення)";
        sample.addEventListener("click", () => action(async () => {
          if (!confirm("Додати цей запис як зразок звуку сповіщення? Лише якщо в ньому чистий сигнал без мовлення. Діє після перезапуску.")) return;
          await request(`/api/recordings/${item.id}/notification-sample`, "POST", { confirmed_isolated: true });
          sample.disabled = true; sample.textContent = "✓ у зразках";
          note("Зразок додано. Він почне діяти після перезапуску (кнопка внизу меню).");
        }));
        apron.sample = sample;
      }
      const details = el("details", "recording-details");
      details.append(el("summary", "", "Технічні деталі"));
      details.append(el("p", "muted small", `Тривалість: ${fix(signal.duration_s)} с · рівень: ${fix(signal.rms_dbfs, 1)} dBFS · кліпінг: ${pct(signal.clipping_ratio)} · Whisper: ${fix(d.whisper_s)} с · Qwen: ${fix(d.qwen_s)} с · черга: ${fix(d.queue_wait_s)} с`));
      for (const fix of item.corrections || []) {
        const when = new Date(fix.ts * 1000).toLocaleString("uk-UA");
        card.append(el("p", "recording-edit small", fix.changed
          ? `✎ Правка в Mini App: ${fix.name} · ${when}: «${fix.from}» → «${fix.to}»`
          : `✎ Відкрито й збережено без змін: ${fix.name} · ${when}`));
      }
      if (!item.corrections && item.reference_source === "miniapp")
        card.append(el("p", "recording-edit small muted", "✎ Еталон із правки в Mini App (автор невідомий: до журналу дій)"));
      const rawLine = el("p", "recording-raw", `Whisper: ${d.raw_text || "Текст відсутній / розпізнавання не виконувалось"}`);
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
        const button = el("button", "ghost"); button.type = "button";
        const [key, ...words] = title.split(" ");
        button.append(el("kbd", "type-key", key), el("span", "type-label", words.join(" ")));
        button.title = `Клавіша ${key}`;
        button.addEventListener("click", () => setCategory(value, true));
        categoryButtons[value] = button; segmented.append(button);
      }
      input.value = item.reference ?? (d.final_text || d.raw_text || "").replace(/\s*\.\s*$/, "");
      input.rows = 2; input.maxLength = 2000; input.placeholder = "Введіть те, що справді чутно…";
      // Поле росте разом із текстом (без внутрішньої прокрутки).
      const grow = () => { if (!input.style) return; input.style.height = "auto"; if (input.scrollHeight) input.style.height = `${input.scrollHeight + 2}px`; };
      input.addEventListener("input", grow);
      input.setAttribute("aria-label", "Еталонна розшифровка");
      const splitRoles = text => (text || "").split(",").map(v => v.trim()).filter(Boolean);
      const sameRole = (a, b) => a.toLocaleLowerCase("uk") === b.toLocaleLowerCase("uk");
      // Ролі підставляються самі (два різні позивні: перший — «Кому», другий — «Від»),
      // доки оператор не змінив їх уручну. Уже розмічені ролі не перезаписуються.
      let rolesAuto = item.reference_recipient == null && item.reference_sender == null;
      let rolesAutoApplied = false;
      function applyAutoRoles(roles) {
        if (!rolesAuto) return;
        // Ролі, які програма сама підставила в нерозмічений запис, — частина вихідного стану, а не правка:
        // щойно відкрита картка не позначається «не збережено».
        const untouched = item.reference === null && savedSnapshot !== undefined && snapshot() === savedSnapshot;
        rolesAutoApplied = Boolean(roles);
        recipient.set(roles?.recipient ? [roles.recipient] : []);
        sender.set(roles?.sender ? [roles.sender] : []);
        if (untouched) savedSnapshot = snapshot();
        updateDirty();
      }
      function rolesTouched() { rolesAuto = false; rolesAutoApplied = false; }
      // Роль без випадних списків: порожнє поле — ще не перевірено; «∅» — у фразі не вказано.
      function roleField(title, reference) {
        const group = el("div", "recording-role"), value = el("input"), none = el("button", "ghost role-none", "∅");
        let absent = reference === "";
        value.type = "text"; value.maxLength = 300; value.value = reference || "";
        value.setAttribute("aria-label", `${title}: позивний`);
        none.type = "button"; none.title = `«${title}» у фразі не вказано (порожнє поле — ще не перевірено)`;
        const update = () => {
          none.className = absent ? "ghost role-none on" : "ghost role-none";
          none.setAttribute("aria-pressed", String(absent));
          value.disabled = absent;
          value.placeholder = absent ? "не вказано у фразі" : "невідомо";
        };
        none.addEventListener("click", () => { absent = !absent; if (absent) value.value = ""; rolesTouched(); update(); renderChips(); updateDirty(); });
        value.addEventListener("input", () => { rolesTouched(); renderChips(); });
        update(); group.append(el("span", "role-title", title), value, none);
        return {node: group, read: () => value.value.trim() ? value.value : (multiBox.checked ? null : ""),
                absent: () => { absent = true; value.value = ""; update(); },
                values: () => absent ? [] : splitRoles(value.value),
                set: list => { absent = false; value.value = list.join(", "); update(); },
                state: () => ({ absent, text: value.value }),
                restore: saved => { absent = saved.absent; value.value = saved.text; update(); },
              };
      }
      const recipient = roleField("Кому", item.reference_recipient), sender = roleField("Від", item.reference_sender);

      // Позивні зі словника, знайдені в набраному тексті: оператор сам відмічає «Кому» / «Від».
      const chips = el("div", "recording-chips");
      let suggestions = [], suggestTimer = null;
      // Ролі одним рядком: позивні з фрази (і вже вказані) з перемикачами «Кому / Від». Не обрано —
      // роль у фразі не вказана; при кількох передачах ролі не оцінюються (див. roleField.read).
      const renamed = new Map();   // підказаний позивний → виправлений оператором варіант
      // Позивний у табличці ролей має бути в самому тексті: інакше поле підсвічено (опечатка чи старий варіант).
      const words = value => ` ${value.toLocaleLowerCase("uk").replace(/[ʼ'’`]/g, "").replace(/[^\p{L}\p{N}]+/gu, " ").trim()} `;
      let roleNameFields = [];
      function markRoleName(field) {
        const name = field.value.trim(), missing = Boolean(name) && !words(input.value).includes(words(name));
        field.classList?.toggle("missing", missing);
        field.title = missing ? `«${name}» немає в тексті — перевірте написання` : "Позивний (можна виправити)";
      }
      function renderChips() {
        chips.replaceChildren(); roleNameFields = [];
        // Кілька передач: ролі не оцінюються — замість чипів лише ця позначка (знімається кліком).
        if (multiBox.checked) {
          // Сама з’являється за «/» у тексті й зникає без нього — без окремого перемикача.
          chips.append(el("span", "multi-badge", "Кілька передач у записі"));
          chips.title = "У тексті є «/»: ролі для такого запису не позначаються"; return;
        }
        const names = [];
        for (const name of [...suggestions.map(n => renamed.get(n) ?? n), ...recipient.values(), ...sender.values()])
          if (!names.some(n => sameRole(n, name))) names.push(name);
        chips.title = rolesAutoApplied ? "Ролі підставлено: позивний після «я» — «Від», перед ним — «Кому» (без «я»: перший — «Кому», другий — «Від»). Змініть, якщо не так." : "";
        if (!names.length) return;
        // Табличка: рядок на позивний — [поле з назвою][Кому][Від]; назву можна виправити.
        const table = el("div", "roles-table");
        for (const name of names) {
          const field = el("input", "role-name"); field.type = "text"; field.value = name; field.maxLength = 80;
          field.title = "Позивний (можна виправити)"; field.setAttribute?.("aria-label", "Позивний");
          field.addEventListener("change", () => {
            const value = field.value.trim();
            if (!value || sameRole(value, name)) { field.value = name; return; }
            const source = suggestions.find(n => sameRole(renamed.get(n) ?? n, name));
            if (source) renamed.set(source, value);
            for (const role of [recipient, sender]) {
              const list = role.values();
              if (list.some(v => sameRole(v, name))) role.set(list.map(v => sameRole(v, name) ? value : v));
            }
            rolesTouched(); updateDirty(); renderChips();
          });
          field.addEventListener("keydown", event => { if (event.key === "Enter") { event.preventDefault(); event.stopPropagation(); field.blur(); } });
          field.addEventListener("input", () => markRoleName(field));
          markRoleName(field); roleNameFields.push(field);
          table.append(field);
          const switcher = el("span", "role-switch");
          // Перемикач: позивний або «Кому», або «Від», або ні те, ні те (повторний клік знімає вибір).
          switcher.setAttribute?.("role", "radiogroup"); switcher.setAttribute?.("aria-label", `Роль: ${name}`);
          for (const [role, other, title] of [[recipient, sender, "Кому"], [sender, recipient, "Від"]]) {
            const chosen = role.values().some(v => sameRole(v, name));
            const toggle = el("button", chosen ? "ghost on" : "ghost", title); toggle.type = "button";
            toggle.setAttribute("role", "radio"); toggle.setAttribute("aria-checked", String(chosen));
            toggle.addEventListener("click", () => {
              rolesTouched();
              const list = role.values().filter(v => !sameRole(v, name));
              role.set(chosen ? list : [...list, name]);
              if (!chosen) {
                const rest = other.values();
                if (rest.some(v => sameRole(v, name))) other.set(rest.filter(v => !sameRole(v, name)));
              }
              updateDirty(); renderChips();
            });
            switcher.append(toggle);
          }
          table.append(switcher);
        }
        chips.append(table);
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
      input.addEventListener("input", () => { suggest(); for (const f of roleNameFields) markRoleName(f); });

      // Перевірка оформлення: пробіли, цифри групами, «з», апостроф, назви словника.
      // Лише підказки: зберегти можна будь-який текст.
      const lint = el("div", "text-check");
      // Підсвітка — шар під прозорим полем із тим самим текстом і переносами (окремої копії тексту немає).
      const markedText = el("div", "transcript-backdrop");
      markedText.setAttribute("aria-hidden", "true");
      const highlightLegend = el("small", "muted highlight-legend", "Позивні · команди · межа · хвиля — перевірити; ≈ слово, що звучить, орієнтовне.");
      let wordNodes = [], activeNodes = [], cueText = null, cueGuide = null, cues = [], markedValue = null, markedSpans = [];
      function markPlayingWord(time, guide) {
        if (markedValue !== input.value) renderHighlights();
        if (cueText !== input.value || cueGuide !== guide) {
          cueText = input.value; cueGuide = guide; cues = approximateWordCues(cueText, guide);
        }
        // Між словами маркер не гасне: на коротких паузах (< 0,2 с) лишається на останньому слові.
        const near = !audio.ended && guide?.spans.some(([a,b]) => a <= time && time < b + .2);
        const cue = near ? cues.findLast(c => c.start <= time) : null;
        const next = cue ? wordNodes.filter(n => n.from < cue.to && n.to > cue.from).map(n => n.node) : [];
        for (const node of activeNodes) if (!next.includes(node)) node.classList.remove?.("transcript-playing");
        for (const node of next) if (!activeNodes.includes(node)) node.classList.add("transcript-playing");
        activeNodes = next;
      }
      function renderHighlights(spans) {
        const text = input.value;
        // Без нових позначок старі лишаються, доки текст той самий; після правки — лише слова для маркера.
        spans = spans ?? (markedValue === text ? markedSpans : []);
        markedValue = text; markedSpans = spans;
        markedText.replaceChildren();
        wordNodes = []; activeNodes = [];
        const words = [...text.matchAll(/[\p{L}\p{N}]+(?:[’'ʼ`-][\p{L}\p{N}]+)*/gu)];
        const cuts = [...new Set([0, text.length, ...words.flatMap(m => [m.index, m.index+m[0].length]), ...spans.flatMap(s => [s.start, s.end])])].sort((a,b) => a-b);
        for (let i = 1; i < cuts.length; i++) {
          const a = cuts[i-1], b = cuts[i];
          if (a < 0 || b > text.length || a === b) continue;
          const kinds = spans.filter(s => s.start <= a && s.end >= b).map(s => s.kind);
          const token = el("span", kinds.map(k => `transcript-${k}`).join(" "), text.slice(a,b));
          wordNodes.push({from:a, to:b, node:token});
          markedText.append(token);
        }
        // Кінцевий перенос рядка textarea показує порожнім рядком — шар має робити так само.
        if (text.endsWith("\n")) markedText.append(el("span", "", " "));
        custom.syncText();
      }
      let lintTimer = null;
      // «Виправити оформлення» — усі рекомендовані правки оформлення одним натисканням; кнопка всередині поля праворуч.
      let formatted = null;
      // Кнопка в полі завжди на місці (нічого не з’являється й не зсуває поле): без правок — приглушена.
      const fixAll = el("button", "fix-all idle", "Оформлення ✓"); fixAll.type = "button"; fixAll.disabled = true;
      fixAll.title = "Правок оформлення немає";
      const showFixAll = ready => {
        fixAll.disabled = !ready; fixAll.className = ready ? "fix-all" : "fix-all idle";
        fixAll.textContent = ready ? "Виправити оформлення" : "Оформлення ✓";
        fixAll.title = ready ? "Застосувати всі правки оформлення (пробіли, коми, цифри групами, апостроф)" : "Правок оформлення немає";
      };
      fixAll.addEventListener("click", () => { if (!formatted) return; input.value = formatted; grow(); updateDirty(); checkText(0); });
      function renderLint(result) {
        renderHighlights(result?.highlights || []);
        lint.replaceChildren();
        const issues = result?.issues || [];
        card.classList.toggle("has-remarks", issues.length > 0);
        formatted = issues.length && result.formatted ? result.formatted : null;
        showFixAll(Boolean(formatted));
        if (!issues.length) return;
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
            fix.addEventListener("click", () => { input.value = issue.fixed_text; grow(); updateDirty(); checkText(0); });
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
      // Ввід, blur та Esc можуть одночасно перевіряти той самий текст.
      // Ділимо лише незавершений запит: після зміни словників нова перевірка буде свіжою.
      const pendingChecks = new Map();
      function textCheck(text) {
        if (pendingChecks.has(text)) return pendingChecks.get(text);
        const pending = request("/api/text-check", "POST", { text, at: item.created, highlights: true });
        pendingChecks.set(text, pending);
        const clear = () => { if (pendingChecks.get(text) === pending) pendingChecks.delete(text); };
        pending.then(clear, clear);
        return pending;
      }
      function checkText(delay = 500) {
        clearTimeout(lintTimer);
        const revision = ++lintRevision;
        lintTimer = setTimeout(async () => {
          if (!["speech", "unclear"].includes(category.value) || !input.value.trim()) { renderLint(null); return; }
          const text = input.value;
          try {
            const result = await textCheck(text);
            if (revision === lintRevision && input.value === text) renderLint(result);
          } catch (_) { if (revision === lintRevision) renderLint(null); }
        }, delay);
      }
      input.addEventListener("input", () => { renderHighlights(); checkText(); });
      if (item.text_check) renderLint(item.text_check);
      // Esc у полі тексту — застосувати правки оформлення (як «Виправити оформлення»), якщо вони є.
      // Перевірку робимо одразу, а не чекаємо паузи після набору; підказки «Можливо, …?» не застосовуються.
      async function applyFormatting() {
        if (spellingPending) await spellingPending;
        if (!["speech", "unclear"].includes(category.value) || !input.value.trim()) return false;
        clearTimeout(lintTimer);
        const text = input.value, revision = ++lintRevision;
        let result;
        try { result = await textCheck(text); } catch (_) { return false; }
        if (revision !== lintRevision || input.value !== text) return false;   // тим часом текст змінили
        renderLint(result);
        if (!formatted || formatted === text) return false;
        input.value = formatted; grow(); updateDirty(); checkText(0);
        return true;
      }
      async function applySpellingOnLeave() {
        if (!["speech", "unclear"].includes(category.value) || !input.value.trim()) return false;
        clearTimeout(lintTimer);
        const text = input.value, revision = ++lintRevision;
        let result;
        try { result = await textCheck(text); } catch (_) { return false; }
        if (revision !== lintRevision || input.value !== text) return false;
        renderLint(result);
        const spelling = (result?.issues || []).find(issue =>
          issue.kind === "spelling" && !issue.manual_only && issue.fixed_text && issue.fixed_text !== text);
        if (!spelling) return false;
        input.value = spelling.fixed_text; grow(); updateDirty(); checkText(0);
        return true;
      }
      // Виправлення написання при виході з поля асинхронне; перехід до «Зберегти» його не запускає.
      let spellingPending = null;
      // Лише якщо текст у полі змінили з моменту входу в нього: фокус губиться й при переході на іншу вкладку чи перемальовуванні
      // списку — тоді нічого не виправляємо сами (інакше картка «незбережена» без вашої правки).
      let valueOnFocus = null;
      input.addEventListener("focus", () => { valueOnFocus = input.value; });
      let savePointerDown = false;
      input.addEventListener("blur", (event = {}) => {
        if (event.relatedTarget === save || savePointerDown) {
          valueOnFocus = null;
          savePointerDown = false;
          return;
        }
        if (valueOnFocus === null || input.value === valueOnFocus) return;   // текст не міняли — нічого не робимо
        valueOnFocus = null;
        spellingPending = applySpellingOnLeave()
          .then(changed => { if (changed) note("Підозріле написання виправлено автоматично."); return changed; })
          .catch(() => false)
          .finally(() => { spellingPending = null; });
      });

      // Кілька передач в одному записі: VAD злив сегменти. Окрема позначка для оцінки.
      const multiBox = el("input"); multiBox.type = "checkbox"; multiBox.checked = item.multiple_messages === 1;
      const multi = el("label", "check small"); multi.append(multiBox, el("span", "", " Кілька передач у записі"));
      // «/» (чи старий «|») у тексті — кілька передач: відмітка ставиться сама й знімається, лише якщо її
      // поставила підстановка. Ручне перемикання вимикає підстановку в цій картці.
      let multiAuto = false, multiTouched = false;
      multiBox.addEventListener("change", () => { multiTouched = true; multiAuto = false; renderChips(); suggest(); });
      input.addEventListener("input", () => {
        if (multiTouched) return;
        const split = /[|/]/.test(input.value);
        if (split && !multiBox.checked) { multiBox.checked = true; multiAuto = true; renderChips(); }
        else if (!split && multiAuto) { multiBox.checked = false; multiAuto = false; renderChips(); }
      });
      multi.title = "Кілька людей або передач у одному записі (злиття сегментів)";
      const save = el("button", "", item.reference === null ? "Зберегти еталон" : "Оновити еталон");
      save.addEventListener("pointerdown", () => { savePointerDown = true; });
      save.addEventListener("pointerup", () => { savePointerDown = false; });
      // Наслідки видно ще до натискання: еталон запису з подією оновить і її, і повідомлення в Telegram.
      { const ev = /^db:(\d+)$/.exec(item.data?.fix_id || ""); if (ev && save.dataset) save.dataset.target = `→ подія №${ev[1]} і Telegram`; }
      const textField = el("label", "recording-text");
      const inputLayer = el("div", "text-input-layer"); inputLayer.append(markedText, input);
      const inputBox = el("div", "text-input-box"); inputBox.append(inputLayer, fixAll);
      // «Почутий текст» і фартушок кодів доби — один рядок над полем.
      const textHead = el("div", "text-head"); textHead.append(el("span", "muted small", "Почутий текст"), codesApron(item.created));
      textField.append(textHead, inputBox, highlightLegend);
      custom.setTextGuide(markPlayingWord);
      renderHighlights();
      checkText(0);
      const roles = el("div", "recording-roles"); roles.append(recipient.node, sender.node);
      const actions = el("div", "recording-actions");
      const saveStatus = el("span", "muted small save-status"); saveStatus.setAttribute("role", "status");
      save.className = "save-primary";
      const dirtyBadge = el("span", "dirty-badge", "Незбережені зміни"); dirtyBadge.hidden = true;
      segmented.setAttribute("aria-label", "Тип запису");
      // «Зберегти» — крайня праворуч, позначка незбережених змін і результат — ліворуч від неї.
      const saveActions = el("div", "save-actions"); saveActions.append(saveStatus, dirtyBadge, save);
      actions.append(saveActions, autoplayToggle());   // «Далі одразу» — під «Зберегти», у тій самій колонці
      const typeRow = el("div", "recording-type"); typeRow.append(segmented, chips);
      function setCategory(value, quick) {
        // Швидка розмітка без мовлення зберігається одразу (клавіші 2/3/5 і клік мишкою однаково):
        // запам'ятовуємо, що було в картці, щоб «↶ Скасувати» повернуло все як було.
        const before = quick && !["speech", "unclear"].includes(value)
          ? { category: category.value, typed: input.value, roles: [recipient.state(), sender.state()], multi: multiBox.checked, saved: savedSnapshot }
          : null;
        category.value = value;
        for (const [key, button] of Object.entries(categoryButtons)) button.className = key === value ? "ghost on" : "ghost";
        const speechLike = ["speech", "unclear"].includes(value);
        textField.hidden = !speechLike; roles.hidden = !speechLike; chips.hidden = !speechLike; multi.hidden = !speechLike; lint.hidden = !speechLike;
        if (!speechLike) { input.value = ""; recipient.absent(); sender.absent(); if (quick) submit(before); }
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
        controller.refreshRow?.(input.value);
      }
      form.addEventListener("input", updateDirty); form.addEventListener("change", updateDirty);
      // Стан збереження — і в рядку для читачів екрана, і дрібно всередині самої кнопки.
      function showSave(text, tone = "muted") {
        saveStatus.textContent = text; saveStatus.className = `${tone} small save-status`;
        save.dataset.status = text; save.dataset.tone = tone;
        // Короткий спалах після успіху (клас знімається й ставиться знову, щоб анімація повторилась).
        if (tone === "ok" && save.classList?.remove) { save.classList.remove("just-saved"); void save.offsetWidth; save.classList.add("just-saved"); }
      }
      async function submit(quickUndo = null) {
        if (saving) return;
        saving = true; save.disabled = true; showSave("Зберігаю…");
        // Стан у базі до збереження — для «Скасувати» швидкої розмітки.
        const previous = { reference: item.reference ?? null, label: item.reference == null ? null : item.label ?? null,
                           reference_recipient: item.reference_recipient ?? null, reference_sender: item.reference_sender ?? null,
                           multiple_messages: item.multiple_messages == null ? null : Boolean(item.multiple_messages),
                           reviewed_at: item.reviewed_at ?? null, reference_source: item.reference_source ?? null };
        const wasDone = controller.done;
        try {
          if (spellingPending) await spellingPending;   // спершу виправлення написання з виходу з поля
          const speechLike = ["speech", "unclear"].includes(category.value);
          const sentSnapshot = snapshot();
          const payload = {reference: input.value, label: category.value, expected_revision: item.revision, reference_recipient: recipient.read(), reference_sender: sender.read(), multiple_messages: speechLike ? multiBox.checked : null};
          const result = await request(`/api/recordings/${item.id}/review`, "PUT", payload);
          savedSnapshot = sentSnapshot;
          // Reviewed rows leave the pending query; keep the next page's offset aligned.
          if (item.reference === null && TODO_VIEWS.includes($("recording-view").value)) recordingOffset = Math.max(0, recordingOffset - 1);
          item.revision = result.revision; item.reference = result.reference; item.label = result.label;
          for (const key of ["reviewed_at", "reference_recipient", "reference_sender", "multiple_messages", "reference_source"]) item[key] = result[key];
          showQuality(result);
          if (quickUndo && typeof offerUndo === "function")
            offerUndo({ title: ROW_KIND[result.label] || result.label, controller, run: () => undoQuick(previous, wasDone, quickUndo) });
          controller.done = true;
          updateDirty();
          card.classList.add("done");
          save.textContent = "Оновити еталон";
          const synced = { updated: " · подію й повідомлення в Telegram оновлено", hidden: " · повідомлення видалене — Telegram не змінено",
                           failed: " · подію оновити не вдалося" }[result.event_sync] || "";
          const learned=result.learning_sync?.status==='failed' ? ' · навчання не синхронізовано, повторіть збереження'
            : result.learning_sync?.candidates ? ` · словесних пар: ${result.learning_sync.candidates}` : '';
          const recall = result.telegram_recall?.status === 'scheduled'
            ? ` · відкликання копій Telegram, якщо вони є, — через ${Math.ceil(result.telegram_recall.after_seconds)} с; швидке скасування мітки зупинить очікування` : '';
          showSave(`✓ Збережено о ${new Date().toLocaleTimeString("uk-UA", { hour: "2-digit", minute: "2-digit" })}${synced}${learned}${recall}`,
                   result.event_sync === "failed" || result.learning_sync?.status==='failed' ? "alert" : "ok");
          if (result.learning_sync?.status==='failed') {
            save.textContent='Повторити синхронізацію';
            note('Еталон збережено, але навчання не синхронізовано. Повторіть збереження цієї картки.');
          } else note(recall ? `Еталон збережено${recall}. Видалені повідомлення не відновлюються.`
            : result.event_sync === "updated" ? "Еталон збережено; подію й повідомлення в Telegram оновлено." : "Еталон збережено.");
          if (!controller.dirty && result.learning_sync?.status!=='failed' && result.event_sync!=='failed' && typeof advanceReview === "function") advanceReview(controller);
        } catch (error) {
          showSave(`${error.uncertain ? "Збереження не підтверджено" : "Не збережено"}: ${error.message}`, "alert"); note(error.message);
        } finally {
          saving = false; save.disabled = false;
        }
      }
      // «↶ Скасувати»: запис у базі — як був до швидкої розмітки; картка — з тим самим текстом і ролями.
      async function undoQuick(previous, wasDone, before) {
        const result = await request(`/api/recordings/${item.id}/restore`, "POST", { ...previous, expected_revision: item.revision });
        for (const key of ["revision", "reference", "label", "reviewed_at", "reference_recipient", "reference_sender", "multiple_messages", "reference_source"])
          item[key] = result[key];
        if (result.reference === null && TODO_VIEWS.includes($("recording-view").value)) recordingOffset += 1;
        controller.done = wasDone; card.classList.toggle("done", wasDone);
        setCategory(before.category, false);
        input.value = before.typed; grow();
        recipient.restore(before.roles[0]); sender.restore(before.roles[1]); multiBox.checked = before.multi;
        renderChips();
        savedSnapshot = before.saved; updateDirty();
        save.textContent = result.reference === null ? "Зберегти еталон" : "Оновити еталон";
        showSave("↶ Розмітку скасовано");
        if (result.reference !== null) showQuality(result);
        if (typeof activateReview === "function") { for (const c of reviewCards) c.audio.pause(); activateReview(controller); }
      }
      const meta = el("div", "recording-meta"); meta.append(roles);   // «кілька передач» — позначка в рядку чипів
      // Зверху тип запису й поле тексту (з «Виправити оформлення»); під полем два блоки:
      // ліворуч підказки, позивні й ролі, праворуч «Зберегти» на висоту лівого блоку.
      const fields = el("div", "recording-fields"); fields.append(textField, lint, meta);
      const body = el("div", "recording-body"); body.append(fields, actions);
      form.append(typeRow, body);
      form.addEventListener("submit", event => { event.preventDefault(); submit(); });
      const controller = { card, audio, item, applyFormatting, applySpellingOnLeave, loadWave: custom.loadWave, dispose: custom.dispose, draftsPill: custom.drafts, dirty:false, done: item.reference !== null, setCategory, submit, grow,
                           focusText: () => { if (!textField.hidden) input.focus(); }, suggest,
                           refreshHints: () => { if (!textField.hidden) { suggest(); checkText(0); } } };
      reviewCards.push(controller);
      controller.row = recordingRow(item, controller);
      card.addEventListener("click", () => { if (typeof activateReview === "function") activateReview(controller, false); });
      if (controller.done) card.classList.add("done");
      setCategory(category.value, false);
      renderChips();
      savedSnapshot = snapshot();
      // Фартушок: розпізнане Whisper (його можна повернути в поле) і технічні деталі.
      const heardText = (d.final_text || d.raw_text || "").replace(/\s*\.\s*$/, "");
      const rawRow = el("div", "apron-raw"); rawRow.append(rawLine);
      if (heardText) {
        const restore = el("button", "ghost small-button", "↺ Повернути розпізнане"); restore.type = "button";
        restore.title = "Підставити в поле текст, який дала програма (замість написаного)";
        restore.addEventListener("click", () => { input.value = heardText; grow(); updateDirty(); checkText(0); suggest(); });
        rawRow.append(restore);
      }
      if (typeof d.notification_score === "number")
        details.append(el("p", "muted small", `Фільтр сигналів: схожість ${fix(d.notification_score)}${d.notification_candidate ? " (кандидат)" : ""} · поза збігом ${fix(d.notification_outside_s)} с · стороннє ${pct(d.notification_extra)}`));
      details.append(apron.sample);
      details.append(el("pre", "", JSON.stringify(d, null, 2)));
      apron.append(rawRow, details);
      card.append(form); $("recordings").append(deck, card, apron);
    }
    recordingOffset += page.items.length;
    $("recording-more").hidden = recordingOffset >= page.total;
    if (typeof fitArchive === "function") fitArchive();
    if (rowMark && typeof applyRowMark === "function") applyRowMark();   // обране сортування легенди — і для нових рядків
    // Одразу перший запис, щоб редактор не був порожнім.
    if (reset && reviewCards.length && typeof activateReview === "function") activateReview(reviewCards[0], false);
  }
  $("recording-refresh").addEventListener("click", () => action(() => recordings(true)));
  // Після кліку на кнопку в архіві (виправити текст, роль, тип, «у словник»…) фокус знімається з поля й
  // кнопки: пробіл та інші клавіші одразу керують плеєром, а не друкують і не «натискають» кнопку знову.
  $("recordings").addEventListener("click", event => {
    if (!event.target?.closest?.("button")) return;
    setTimeout(() => { const active = document.activeElement; if (active && active !== document.body) active.blur?.(); }, 0);
  });
  // Обраний вид і період запам’ятовуються в цьому браузері.
  const VIEW_KEY = "radio345:archive-view";
  const saveArchiveView = () => {
    try { localStorage.setItem(VIEW_KEY, JSON.stringify({ view: $("recording-view").value, period: $("recording-period").value, order: orderValue() })); } catch (_) { /* лише поточна сесія */ }
  };
  try {
    const saved = JSON.parse(localStorage.getItem(VIEW_KEY) || "{}");
    if (saved.view === "recent") saved.view = "reviewed";   // колишній окремий вид
    if (saved.edits === "miniapp") saved.view = "miniapp";   // колишній фільтр «Лише правки з Mini App»
    // Правки людини: пункт з'являється після завантаження архіву — поки що з ID, ім'я підставиться.
    if (/^person:\d{1,18}$/.test(saved.view || "")) personOption(saved.view.slice(7), saved.view.slice(7), 0);
    if ([...$("recording-view").options].some(o => o.value === saved.view)) $("recording-view").value = saved.view;
    if ([...$("recording-period").options].some(o => o.value === saved.period)) $("recording-period").value = saved.period;
    if (["doubtful","old"].includes(saved.order) && $("recording-order")) $("recording-order").value = saved.order;
  } catch (_) { /* типовий вибір */ }
  for (const id of ["recording-view", "recording-period", "recording-order"]) {
    $(id).addEventListener("change", () => { saveArchiveView(); action(() => recordings(true)); });
  }
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

  const loaders = { recordings: () => recordings(true), overview, filter: filterView, dictionaries, codes: dailyCodes, texts, learning, settings: async () => { await settings(); await runtimeSettings(); }, users, activity: () => activity(), system, sessions: system, storage: system, checks: system, quality: async () => { await quality(); await training(); }, experiments, preview: roles };

  // ---------- Швидка розмітка архіву: активна картка, клавіші, швидкість ----------
  const REVIEW_PREFS = "radio345:review-prefs";
  let reviewPrefs = { speed: "1", autoplay: true };
  try { reviewPrefs = { ...reviewPrefs, ...JSON.parse(localStorage.getItem(REVIEW_PREFS) || "{}") }; } catch (_) { /* типово */ }
  const savePrefs = () => { try { localStorage.setItem(REVIEW_PREFS, JSON.stringify(reviewPrefs)); } catch (_) { /* сесія */ } };
  $("recording-autoplay").checked = reviewPrefs.autoplay;
  // Швидкість — спільна для всіх карток, вибір біля кожного плеєра; ] / [ (ї / х) — на крок.
  if (!SPEEDS.includes(String(reviewPrefs.speed))) reviewPrefs.speed = "1";
  function setSpeed(value, announce = false) {
    reviewPrefs.speed = String(value); savePrefs();
    for (const select of document.querySelectorAll(".speed-select")) select.value = reviewPrefs.speed;
    for (const c of reviewCards) c.audio.playbackRate = Number(value);
    if (announce) note(`Швидкість відтворення: ${String(value).replace(".", ",")}×`);
  }
  $("recording-autoplay").addEventListener("change", () => { reviewPrefs.autoplay = $("recording-autoplay").checked; savePrefs(); });
  document.addEventListener("play", event => {
    if (event.target instanceof HTMLMediaElement && event.target.closest('[data-panel="recordings"]'))
      event.target.playbackRate = Number(reviewPrefs.speed);
  }, true);

  let activeReview = null;
  // Широкий екран — перелік і редактор поруч: прокручується перелік, редактор завжди на місці.
  const splitView = () => window.matchMedia?.("(min-width: 1100px)").matches;
  // Широкий екран: архів — точно у висоту вікна (сторінка не прокручується, лише перелік і запис);
  // «Показати ще» — внизу переліку. Вузький — звичайна стрічка, кнопка під картками.
  function fitArchive() {
    if (typeof document === "undefined" || !document.querySelector) return;
    const split = document.querySelector(".archive-split"), list = document.querySelector(".archive-list");
    const main = document.querySelector(".archive-main"), more = $("recording-more");
    if (!split || !list || !main || !more) return;
    const refresh = $("recording-refresh"), filters = document.querySelector(".archive-filters");
    if (splitView() && split.offsetParent !== null) {
      // Висота — до низу вікна з урахуванням усього, що під архівом (нижній відступ main тощо):
      // спершу орієнтовно, далі мінус фактичне переповнення сторінки, щоб вона не прокручувалась.
      const top = split.getBoundingClientRect().top + pageScrollY();
      let height = Math.max(360, Math.floor(window.innerHeight - top - 10));
      split.style.setProperty("--archive-h", `${height}px`);
      const extra = document.querySelector('main').scrollHeight - document.querySelector('main').clientHeight;
      if (extra > 0) { height = Math.max(360, height - extra); split.style.setProperty("--archive-h", `${height}px`); }
      if (pageScrollY() && extra > 0) pageScrollTo({top:0});
      if (more.parentElement !== list) list.append(more);
      // «Оновити» — над переліком, який він і оновлює.
      if (refresh && refresh.parentElement !== list) list.prepend(refresh);
    } else {
      split.style.removeProperty("--archive-h");
      if (more.parentElement !== main) main.append(more);
      if (refresh && filters && refresh.parentElement !== filters) filters.append(refresh);
    }
  }
  window.addEventListener?.("resize", () => fitArchive());
  // Щось над архівом змінило висоту (плашка, шапка розділу) — підганяємо знову; той самий розмір нічого не змінює.
  if (typeof ResizeObserver !== "undefined" && typeof document !== "undefined" && document.querySelector?.("main")) {
    let fitting = false;
    new ResizeObserver(() => {
      if (fitting || !["recordings", "overview", "quality", "experiments"].includes(view)) return;
      fitting = true;
      window.requestAnimationFrame(() => {
        if (view === "recordings") fitArchive(); else if (view === "overview") fitOverview();
        else if (view === "quality") fitAnalysis(); else if (typeof fitLab === "function") fitLab();
        fitting = false;
      });
    }).observe(document.querySelector("main"));
  }
  function activateReview(controller, scroll = true) {
    activeReview = controller;
    if (undoStack.length) renderUndo(false);   // плашка «Скасувати» переходить до нової активної картки
    for (const c of reviewCards) c.card.classList.toggle("active", c === controller);
    // Прихована картка не мала висоти: поле тексту підганяємо, щойно вона показана.
    window.requestAnimationFrame?.(() => controller.grow?.());
    // Нерозмічений запис приходить без перевірки тексту: перевіряємо одразу, щоб «Виправити
    // оформлення» й позивні були готові без першої правки.
    if (!controller.hinted) { controller.hinted = true; controller.refreshHints?.(); }
    window.requestAnimationFrame?.(() => controller.loadWave?.());   // хвиля — щойно плеєр видно
    if (splitView()) controller.row?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    else if (scroll) controller.card.scrollIntoView({ block: "center", behavior: "smooth" });
  }
  function stepReview(delta) {
    if (!reviewCards.length) return;
    const index = activeReview ? reviewCards.indexOf(activeReview) : -1;
    const next = reviewCards[Math.max(0, Math.min(reviewCards.length - 1, index + delta))];
    activateReview(next);
  }
  // ← / → — як у плеєрі: сусідній запис (і розмічений теж) одразу грає, незалежно від автопрогравання.
  function playAdjacent(delta) {
    const index = reviewCards.indexOf(activeReview), next = reviewCards[index + delta];
    if (!next) { note(delta < 0 ? "Це перший запис у переліку." : $("recording-more").hidden ? "Це останній запис у переліку." : "Це останній завантажений запис — натисніть «Показати ще»."); return; }
    activeReview?.audio.pause();
    activateReview(next);
    next.audio.currentTime = 0;
    next.audio.play().catch(() => {});
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
    if (event.key === "Escape" && typing) {
      const editor = activeReview, inText = event.target.tagName === "TEXTAREA" && editor?.card?.contains?.(event.target);
      event.target.blur();
      if (inText) editor.applyFormatting?.().then(changed => { if (changed) note("Оформлення виправлено автоматично (Esc)."); });
      return;
    }
    if ((event.metaKey || event.ctrlKey) && ["z", "я"].includes(event.key.toLowerCase()) && !typing && undoStack.length) {
      event.preventDefault(); undoLast(); return;
    }
    if (typing || event.metaKey || event.ctrlKey || event.altKey) return;
    if (!activeReview) activateReview(reviewCards[0], false);
    const current = activeReview;
    if (event.key === " ") {
      event.preventDefault();
      if (current.audio.paused) current.audio.play().catch(() => {}); else current.audio.pause();
    } else if (["1", "2", "3", "4", "5"].includes(event.key)) {
      event.preventDefault();
      current.setCategory(["speech", "noise", "notification", "unclear", "room"][Number(event.key) - 1], true);
      if (["1", "4"].includes(event.key)) current.focusText();
    } else if (event.key === "j" || event.key === "о" || event.key === "ArrowDown") { event.preventDefault(); stepReview(1); }
    else if (event.key === "k" || event.key === "л" || event.key === "ArrowUp") { event.preventDefault(); stepReview(-1); }
    else if (event.key === "ArrowLeft" || event.key === "ArrowRight") {
      if (event.defaultPrevented || event.shiftKey) return;
      event.preventDefault();
      if (!event.repeat) playAdjacent(event.key === "ArrowRight" ? 1 : -1);   // утримання не «пролітає» перелік
    }
    else if (event.key === "r" || event.key === "к") { current.audio.currentTime = 0; current.audio.play().catch(() => {}); }
    else if (["]", "ї", "[", "х"].includes(event.key)) {
      const step = ["]", "ї"].includes(event.key) ? 1 : -1;
      const index = Math.max(0, Math.min(SPEEDS.length - 1, SPEEDS.indexOf(String(reviewPrefs.speed)) + step));
      setSpeed(SPEEDS[index], true);
    }
    else if (event.key === "Enter") { event.preventDefault(); current.focusText(); }
  });

  // ---------- Відтворення без луни в мікрофон приймача ----------
  // Звук із колонок Mac потрапляє в мікрофон: приймач розпізнав би його знову
  // (дублікати в Telegram і в архіві). Поки грає будь-яке аудіо панелі, просимо
  // приймач закрити мікрофон. Утримання короткострокове й поновлюється, тож
  // закрита вкладка не залишить приймач вимкненим довше ніж на ~10 с.
  const HOLD_KEY = "radio345:hold-while-listening";
  const holdBox = $("hold-while-listening");
  // Квадрат на всю висоту шапки: висоту дає сітка, ширину рівняємо до неї (CSS сам цього надійно не вміє).
  {
    const square = holdBox.closest?.(".mic-square");
    if (square && typeof ResizeObserver !== "undefined") {
      let last = 0;
      new ResizeObserver(() => {
        const side = Math.round(Math.min(150, Math.max(84, square.offsetHeight)));
        if (Math.abs(side - last) > 1) { last = side; square.style.width = `${side}px`; }
      }).observe(square);
    }
  }
  try { holdBox.checked = localStorage.getItem(HOLD_KEY) !== "off"; } catch (_) { /* лишаємо типово */ }
  holdBox.addEventListener("change", () => {
    try { localStorage.setItem(HOLD_KEY, holdBox.checked ? "on" : "off"); } catch (_) { /* лише поточна сесія */ }
    if (!holdBox.checked && playing.size) sendHold(0);
  });
  const playing = new Set();
  let holdTimer = null;
  // Квадрат «мікрофон» показує, що зараз мікрофон справді закрито (грає запис).
  const showHold = () => holdBox.closest?.(".mic-square")?.classList.toggle("holding", holdBox.checked && playing.size > 0);
  holdBox.addEventListener("change", showHold);

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
    playing.add(media); showHold();
    const left = Number.isFinite(media.duration) ? media.duration - media.currentTime : 10;
    sendHold(Math.min(120, left + 1.5));
    if (!holdTimer) holdTimer = setInterval(() => { if (playing.size) sendHold(10); }, 4000);
  }, true);
  // Плеєр, що грає, підсвічено (фартушок над карткою).
  for (const [name, on] of [["play", true], ["pause", false], ["ended", false]])
    document.addEventListener(name, event => event.target?.closest?.(".recording-deck")?.classList.toggle("playing", on), true);
  function stopped(event) {
    if (!playing.delete(event.target) || playing.size) return;
    showHold();
    clearInterval(holdTimer); holdTimer = null;
    sendHold(1);  // 1 с на відлуння кімнати, потім мікрофон відкривається
  }
  document.addEventListener("pause", stopped, true);
  document.addEventListener("ended", stopped, true);
  window.addEventListener("pagehide", () => { if (playing.size) sendHold(0); });

