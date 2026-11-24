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
        ["Захищені: розмітка / очікування ASR", `${num(audio.protected_count)} записів · ${storageSize(audio.protected_bytes)}`],
        ["Без захисту", `${num(audio.unreviewed_count)} · ${storageSize(audio.unreviewed_bytes)} · до ${audio.retention_days} днів`],
        ["Найближча межа за віком", audio.next_age_expiry_at ? `${new Date(audio.next_age_expiry_at*1000).toLocaleString("uk-UA")}` : "нерозмічених немає"],
        ["Підлягає очищенню зараз", `${num(audio.due_count)} записів · ${storageSize(audio.due_bytes)}`, audio.due_count ? "caution" : ""],
        ["Індекс SQLite та WAL/SHM", storageSize(audio.metadata_bytes)],
      ]));
      if (audio.low_rms_retention_seconds != null) box.append(el("p", "small", `Тихі записи без розмітки: до ${Math.round(audio.low_rms_retention_seconds / 60)} хв від запису. Решта нерозмічених — до ${audio.retention_days} днів.`));
      if (audio.estimated_full_at) box.append(el("p", "small caution", `Ліміт може бути досягнутий близько ${new Date(audio.estimated_full_at*1000).toLocaleString("uk-UA")} за темпом останньої години. Це оцінка, не розклад.`));
      const g = audio.unreviewed_by_group;
      if (g) box.append(kv([
        ["Нерозмічені: ефір", `${num(g.radio.count)} · ${storageSize(g.radio.bytes)}`],
        ["Нерозмічені: інше мовлення", `${num(g.speech.count)} · ${storageSize(g.speech.bytes)}`],
        ["Нерозмічені: відсіяне програмою", `${num(g.filtered.count)} · ${storageSize(g.filtered.bytes)}`],
      ]));
      box.append(el("p", "small muted", "Очищення виконується після збереження нового аудіо та раз на хвилину за відсутності нових записів: за віком або обсягом — що настане раніше. Коли місця бракує, першим видаляється відсіяне програмою (тиша, закороткі, чорний список…), потім інше мовлення, ефір — останнім; у кожній групі спершу старіші. Розмічені й записи, що ще очікують розпізнавання, захищені та займають частину ліміту. Ліміт стосується WAV за індексом, не всього каталогу."));
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

  async function telegramRetention() {
    const box = $("telegram-retention"); if (!box) return;
    const state = await request('/api/runtime-settings');
    box.replaceChildren();
    const label = el('label', 'retention-field', 'Видаляти копії фраз через');
    const hours = el('input'); hours.type = 'number'; hours.min = '0'; hours.max = '47';
    hours.value = state.values.TELEGRAM_AUTO_DELETE_HOURS || 0;
    label.append(hours, el('span', 'muted', 'год.'));
    const save = el('button', 'ghost', 'Зберегти');
    save.addEventListener('click', () => action(async () => {
      await request('/api/runtime-settings', 'PUT', {values:{TELEGRAM_AUTO_DELETE_HOURS:Number(hours.value)}, expected_revision:state.revision});
      await telegramRetention(); note('Срок збереження копій у Telegram оновлено.');
    }));
    box.append(label, save, el('p', 'muted small retention-hint', '0 — вимкнено; 1–47 — увімкнено. Лише копії з відомим часом доставки. Аудіоархів і навчання не видаляються.'));
  }
  // Перевірки «Потребує уваги»: які види шукати у фоні та історія рішень «Готово».
  let attentionSettingsAt = 0;
  async function attentionSettings(force = false) {
    const box = $("attention-kinds"); if (!box) return;
    if (!force && Date.now() - attentionSettingsAt < 60000) return;   // фонове оновлення «Система» — раз на хвилину
    attentionSettingsAt = Date.now();
    const a = await request("/api/attention");
    box.replaceChildren(...(a.suggestion_kinds || []).map(kind => {
      const label = el("label", `attention-kind${kind.muted ? " muted-kind" : ""}`);
      const input = el("input"); input.type = "checkbox"; input.checked = !kind.muted;
      input.addEventListener("change", () => action(async () => {
        await request("/api/suggestions/mute", "POST", {kind: kind.kind, muted: !input.checked});
        note(input.checked ? `«${kind.title}» знову перевіряється (з наступною перевіркою).` : `«${kind.title}» більше не перевіряється.`);
        attentionAt = 0; await attentionSettings(true);
      }));
      label.append(input, el("span", "", kind.title));
      return label;
    }));
    const history = $("attention-history");
    history.querySelector("summary").textContent = `Історія рішень оператора${a.suggestions_hidden ? ` (${a.suggestions_hidden})` : ""}`;
    if (history.open) await attentionHistory();
  }
  async function attentionHistory() {
    const list = $("attention-history-list");
    const history = await request("/api/suggestions/resolved");
    const rows = history.items.map(entry => {
      const row = el("div", "attention-item");
      const restore = el("button", "ghost small-button", "Повернути на перевірку"); restore.type = "button";
      restore.addEventListener("click", () => action(async () => {
        await request("/api/suggestions/restore", "POST", {keys: [entry.key]}); attentionAt = 0; await attentionSettings(true);
      }));
      row.append(el("span", "small", `${stamp(entry.at)} · ${entry.title || entry.kind}`), restore);
      return row;
    });
    list.replaceChildren(el("p", "muted small", history.total
      ? `Рішень збережено: ${history.total}. Показано останні ${history.items.length}. «Готово» — рішення оператора, а не автоматичне виправлення.`
      : "Рішень ще немає."), ...rows);
  }
  $("attention-history")?.addEventListener?.("toggle", event => { if (event.target.open) action(attentionHistory); });

  // ---------- Звукові пристрої: список з окремого процесу, вибір діє після перезапуску приймача ----------
  let audioState = null;
  async function audioDevices(force = false) {
    if (audioState && !force) return;
    const result = $("audio-devices-result"), save = $("audio-devices-save");
    result.replaceChildren(el("p", "muted small", "Читаю список пристроїв…"));
    audioState = await request("/api/audio-devices");
    const {settings, devices, error} = audioState;
    const fill = (select, key, list) => {
      const chosen = settings.values[key] || "";
      const system = (list || []).find(d => d.default);
      const options = [["", `Системний за замовчуванням${system ? ` (${system.name})` : ""}`]];
      for (const d of list || []) options.push([d.name, `${d.name} · ${d.channels} кан.`]);
      if (chosen && !(list || []).some(d => d.name === chosen || d.name.toLowerCase().includes(chosen.toLowerCase())))
        options.push([chosen, `${chosen} — зараз не знайдено`]);
      select.replaceChildren(...options.map(([value, text]) => { const o = el("option", "", text); o.value = value; return o; }));
      // Збережене може бути частиною назви: показуємо перший пристрій, що їй відповідає.
      const match = (list || []).find(d => d.name === chosen) || (list || []).find(d => chosen && d.name.toLowerCase().includes(chosen.toLowerCase()));
      select.value = chosen ? (match ? match.name : chosen) : "";
      select.dataset.saved = select.value;
      select.disabled = settings.env_override.includes(key);
      select.title = select.disabled ? `Задано в .env (${key}) — .env має пріоритет над панеллю` : "";
    };
    fill($("audio-input"), "RADIO_INPUT_DEVICE", devices?.inputs);
    fill($("audio-output"), "RADIO_OUTPUT_DEVICE", devices?.outputs);
    save.disabled = true;
    const notes = [];
    if (error) notes.push(el("p", "small alert-text", `${error}. Збережений вибір показано без перевірки.`));
    if (settings.env_override.length) notes.push(el("p", "muted small", `Задано в .env: ${settings.env_override.join(", ")} — ці поля змінюються лише там.`));
    result.replaceChildren(...notes);
  }
  function audioDirty() {
    const dirty = ["audio-input", "audio-output"].some(id => $(id).value !== $(id).dataset.saved);
    $("audio-devices-save").disabled = !dirty;
  }
  $("audio-input")?.addEventListener?.("change", audioDirty);
  $("audio-output")?.addEventListener?.("change", audioDirty);
  $("audio-devices-refresh")?.addEventListener?.("click", () => action(() => audioDevices(true)));
  $("audio-devices-save")?.addEventListener?.("click", () => action(async () => {
    const settings = await request("/api/audio-devices", "PUT", {expected_revision: audioState.settings.revision,
      values: {RADIO_INPUT_DEVICE: $("audio-input").value, RADIO_OUTPUT_DEVICE: $("audio-output").value}});
    audioState.settings = settings;
    for (const id of ["audio-input", "audio-output"]) $(id).dataset.saved = $(id).value;
    $("audio-devices-save").disabled = true;
    const restart = el("button", "ghost small-button", "Перезапустити зараз"); restart.type = "button"; restart.hidden = serviceMode !== "r345";
    restart.addEventListener("click", () => $("launcher-restart").click());
    const line = el("p", "small saved-text", "Збережено. Приймач візьме пристрої після перезапуску. ");
    line.append(restart);
    $("audio-devices-result").replaceChildren(line);
  }));

  // ---------- Пристрої: Mac і Pi ----------
  const PROCESS_NAMES = { node: "Ефір", asr: "Розпізнавання й бот", panel: "Панель", api: "API Mini App", tunnel: "Тунель Mini App", worker: "Whisper" };
  const HOME_TEXT = { mac: "База й бот працюють на Mac.", pi: "База й бот працюють на Pi.", both: "Бот працює і на Mac, і на Pi: зупиніть один, інакше дані розійдуться.", unknown: "Pi не видно: невідомо, чи працює там бот." };
  const CONTROLLED = { mac: ["worker", "node", "asr", "api", "tunnel"], pi: ["node", "asr", "panel", "api", "tunnel"] };
  const VERB_TEXT = { start: "Запустити", stop: "Зупинити", restart: "Перезапустити" };
  function deviceButtons(name, process, alive) {
    const box = el("span", "toolbar");
    for (const verb of alive ? ["restart", "stop"] : ["start"]) {
      const b = el("button", "ghost small-button", VERB_TEXT[verb]); b.type = "button";
      b.addEventListener("click", () => action(async () => {
        const title = PROCESS_NAMES[process], where = name === "pi" ? "Pi" : "Mac";
        const open = process === "tunnel" && verb === "start" ? " Відкриється публічна адреса API Mini App." : "";
        if (!confirm(`${VERB_TEXT[verb]}: ${title} на ${where}?${open}`)) return;
        b.disabled = true;
        await request(`/api/devices/${name}/${process}/${verb}`, "POST");
        note(`${title} на ${where}: команду надіслано.`);
        setTimeout(devices, 3000);
      }));
      box.append(b);
    }
    return box;
  }
  function deviceCard(name, box, device, extra) {
    if (!device) { box.replaceChildren(el("p", "muted small", "Не налаштовано: RADIO345_PI_SSH у .env на Mac.")); return; }
    if (!device.reachable) { box.replaceChildren(el("p", "alert small", device.error || "Недоступно")); return; }
    const rows = Object.entries(device.processes).map(([name, p]) => [PROCESS_NAMES[name] || name,
      (p.alive ? "працює" : p.blocked ? "зупинено: зайнято на іншому пристрої" : "зупинено") + (p.restarts ? ` · перезапусків ${num(p.restarts)}` : ""), p.alive ? "good" : ""]);
    if (!rows.length) rows.push(["Служби", "не запущено", ""]);
    rows.push(["Подій у базі", device.events == null ? "—" : num(device.events)],
      ["Остання подія", device.last_event ? ago(now() - device.last_event) : "—"], ["Фраз у черзі", num(device.queue)],
      ["Вільно на диску", storageSize(device.disk_free)], ...extra);
    const table = kv(rows);
    if (name === "pi" || serviceMode === "r345") for (const process of CONTROLLED[name]) {
      const p = device.processes[process];
      if (p) table.append(el("span", "", PROCESS_NAMES[process]), deviceButtons(name, process, p.alive));
    }
    box.replaceChildren(table);
  }
  // ---------- Перенесення дому ----------
  const MOVE_PHASE = { checking: "перевіряю", stopping: "зупиняю служби", copying: "копіюю базу", installing: "записую базу",
    starting: "запускаю служби", rolling_back: "повертаю, як було" };
  const deviceTitle = name => name === "pi" ? "Pi" : "Mac";
  let moveTimer = null;
  async function moveState(home) {
    const m = await request("/api/home-move");
    const line = $("move-state");
    $("move-rollback").hidden = !m.interrupted;
    if (home !== undefined) {
      $("move-pi").hidden = m.active || m.interrupted || home !== "mac";
      $("move-mac").hidden = m.active || m.interrupted || home === "mac" || home === "both";
    }
    const route = m.target ? `${deviceTitle(m.source)} → ${deviceTitle(m.target)}` : "";
    line.textContent = m.active ? `Перенесення ${route}: ${MOVE_PHASE[m.phase] || m.phase}…`
      : m.interrupted ? `Перенесення ${route} перервано на кроці «${MOVE_PHASE[m.phase] || m.phase}».`
      : m.phase === "done" ? `Останнє перенесення ${route}: виконано ${stamp(m.at)}.`
      : m.phase === "rolled_back" ? `Перенесення ${route} не вдалося, усе повернуто: ${m.error}`
      : m.phase === "failed" ? `Перенесення ${route} не вдалося, перевірте пристрої: ${m.error}` : "";
    line.classList.toggle("alert", m.interrupted || ["failed", "rolled_back"].includes(m.phase));
    clearTimeout(moveTimer);
    if (m.active) { $("move-pi").hidden = $("move-mac").hidden = true; moveTimer = setTimeout(() => moveState().then(() => devices()).catch(() => {}), 2000); }
  }
  async function moveHome(target) {
    const check = await request(`/api/home-move/check?target=${target}`);
    if (check.problems.length) { note(check.problems.join(" ")); return; }
    const lines = [`Перенести базу, бота, Mini App та ефір на ${deviceTitle(target)}? Кілька хвилин вони не працюватимуть.`, ...check.warnings.map(w => `• ${w}`)];
    if (!confirm(lines.join("\n"))) return;
    await request("/api/home-move", "POST", { target, accept_warnings: true });
    note("Перенесення почалося.");
    await moveState();
  }
  $("move-pi")?.addEventListener?.("click", () => action(() => moveHome("pi")));
  $("move-mac")?.addEventListener?.("click", () => action(() => moveHome("mac")));
  $("move-rollback")?.addEventListener?.("click", () => action(async () => {
    if (!confirm("Повернути дім туди, звідки його переносили?")) return;
    await request("/api/home-move/rollback", "POST"); await moveState();
  }));
  async function devices() {
    let d;
    try { d = await request("/api/devices"); } catch (_) { return; }
    if (!d.mac) { $("devices-panel").hidden = true; return; }
    $("devices-panel").hidden = false;
    $("devices-home").textContent = HOME_TEXT[d.home] || "База й бот ніде не працюють.";
    $("devices-home").classList.toggle("alert", d.home === "both" || d.home === "unknown");
    moveState(d.configured ? d.home : "both").catch(() => {});
    deviceCard("mac", $("device-mac"), d.mac, d.mac.model ? [["Модель Whisper", d.mac.model]] : []);
    deviceCard("pi", $("device-pi"), d.pi, d.pi?.reachable ? [["Температура", d.pi.temp_c == null ? "—" : `${fix(d.pi.temp_c, 1)} °C`],
      ["Код", d.code_matches ? "як на Mac" : "відрізняється від Mac", d.code_matches ? "good" : "alert"]] : []);
  }
  $("devices-refresh")?.addEventListener?.("click", () => action(devices));

  async function system() {
    if (!backupTask) backupJob().resume();
    if (!data) await overview();
    const d = data.diagnostics || {}, c = d.counters || {}, g = d.gauges || {};
    await launcherState();
    if (view === "system") devices();   // не чекаємо: Pi може відповідати до 12 с
    const alive = value => value === false ? ["зупинено", "alert"] : value === true ? ["працює", "good"] : ["—", ""];
    const fresh = d.updated_at && now() - d.updated_at < 120;
    const [asr, asrTone] = fresh ? alive(g["workers.transcriber_alive"]) : ["немає свіжих даних", ""];
    const [tg, tgTone] = fresh ? alive(g["workers.telegram_alive"]) : ["немає свіжих даних", ""];
    $("workers").replaceChildren(kv([
      ["Службами керує", SERVICE_MODE[serviceMode] || serviceMode, serviceMode === "none" ? "" : "good"],
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
    if(t.skipped) $('telegram').append(el('p','muted small',`Пропущено за вашим рішенням: ${t.skipped} оновлень старих копій. Нова правка знову стане в чергу.`));
    if(t.exhausted) {
      $('telegram').append(el('p','muted small','Вичерпано 8 спроб оновити виправлений текст у старих повідомленнях. Правки в базі збережені. Оберіть повтор або пропуск цих оновлень.'));
      const actions=el('div','toolbar'), buttons=[];
      for(const [mode,title] of [['retry','Повторити невдалі оновлення'],['skip','Пропустити ці оновлення']]) {
        const b=el('button','ghost',title);b.type='button';buttons.push(b);actions.append(b);
        b.addEventListener('click',()=>action(async()=>{
          if(mode==='skip'&&!confirm('Не повторювати ці невдалі оновлення старих повідомлень? Вони можуть залишитися зі старим текстом. Правки в базі не видаляються; нові правки знову синхронізуватимуться.'))return;
          for(const button of buttons)button.disabled=true;
          try {
            const result=await request(`/api/telegram-sync/${mode}`,'POST');
            note(mode==='retry'?`Повернуто в чергу: ${result.count}. Приймач оновить повідомлення у фоні.`:`Пропущено: ${result.count}. Текст у базі збережено.`);
            data.telegram_sync=(await request('/api/summary')).telegram_sync;
            await system();
          } finally {for(const button of buttons)button.disabled=false;}
        }));
      }
      $('telegram').append(actions);
    }
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
    await telegramRetention();
    // Допоміжні картки не валять оновлення всієї сторінки: помилка — лише в самій картці
    // (наприклад, браузер уже має новий інтерфейс, а служба панелі ще не перезапущена).
    await attentionSettings().catch(error => {
      $("attention-kinds")?.replaceChildren?.(el("p", "alert-text small", `Не вдалося завантажити: ${error.message}`)); });
    await audioDevices().catch(error => {
      $("audio-devices-result")?.replaceChildren?.(el("p", "alert-text small", error.status === 404
        ? "Служба панелі ще не знає вибору пристроїв — перезапустіть її через лаунчер."
        : `Не вдалося прочитати пристрої: ${error.message}`)); });
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

  let backupTask = null;
  function backupJob() {
    if (!backupTask) backupTask = serverJobPanel({button: $("backup-now"), box: $("backup-job-result"), kind:"backup",
      start: () => request("/api/backups", "POST"), render: r => {
        $("backup-job-result").replaceChildren(el("p", "small", `Копію створено: ${r.name}`));
        if (SYSTEM_VIEWS.has(view)) system();
      }});
    return backupTask;
  }
  $("backup-now").addEventListener("click", () => backupJob().run());

  for (const [kind, endpoint, id, render] of [
    ['archive_inventory', '/api/archive-inventory', 'archive-inventory', r => `Індекс: ${num(r.indexed)}; WAV: ${num(r.wav_files)}. Без WAV: ${r.missing.length}; без індексу старше години: ${r.orphans.length} · ${storageSize(r.orphan_bytes)}. Нічого не видалено.`],
  ]) {
    const button = $(`${id}-now`), box = $(`${id}-result`);
    if (!button || !box) continue;
    const task = serverJobPanel({button, box, kind, start:()=>request(endpoint,'POST'),
      render: r => box.replaceChildren(el('p','small',render(r)))});
    button.addEventListener('click', () => task.run());
    task.resume();
  }

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


