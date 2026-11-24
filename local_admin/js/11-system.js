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

  // ---------- Пристрої: Mac і Pi ----------
  const PROCESS_NAMES = { node: "Ефір", asr: "Розпізнавання й бот", panel: "Панель", api: "API Mini App", tunnel: "Тунель Mini App", worker: "Whisper" };
  const CONTROLLED = { mac: ["worker", "node", "asr", "api", "tunnel"], pi: ["node", "asr", "panel", "api", "tunnel"] };
  const VERB_TEXT = { start: "Запустити", stop: "Зупинити", restart: "Перезапустити" };
  const VERB_ICON = { start: "▶", stop: "■", restart: "↻" };
  const HOME_CORE = ["asr", "api", "node"];   // без них дім працює не повністю
  const MOVE_PHASE = { checking: "перевіряю", stopping: "зупиняю служби", copying: "копіюю базу", installing: "записую базу",
    starting: "запускаю служби", rolling_back: "повертаю, як було" };
  const deviceTitle = name => name === "pi" ? "Pi" : "Mac";
  let moveTimer = null;

  function serviceRow(name, process, p) {
    const row = el("div", "service-row"), buttons = el("span", "service-buttons");
    const state = p.alive ? "працює" : p.blocked ? "зайнято на іншому пристрої" : "зупинено";
    if (CONTROLLED[name].includes(process) && (name === "pi" || serviceMode === "r345")) for (const verb of p.alive ? ["restart", "stop"] : ["start"]) {
      const b = el("button", "ghost icon-button", VERB_ICON[verb]); b.type = "button";
      b.title = VERB_TEXT[verb]; b.setAttribute("aria-label", `${VERB_TEXT[verb]}: ${PROCESS_NAMES[process]} на ${deviceTitle(name)}`);
      const run = async () => {
        b.disabled = true;
        await request(`/api/devices/${name}/${process}/${verb}`, "POST");
        note(`${PROCESS_NAMES[process]} на ${deviceTitle(name)}: команду надіслано.`);
        setTimeout(devices, 3000);
      };
      // Запуск і перезапуск — одразу; зупинка й відкриття публічної адреси — другим натисканням.
      if (verb === "stop") armed(b, "Точно?", run);
      else if (process === "tunnel" && verb === "start") armed(b, "Відкрити?", run);
      else b.addEventListener("click", () => action(run));
      buttons.append(b);
    }
    row.append(el("span", p.alive ? "service-dot on" : "service-dot", ""), el("span", "", PROCESS_NAMES[process] || process), el("span", "muted small", state), buttons);
    return row;
  }
  // Звук належить машині: свій список і свій вибір, база його не переносить.
  const audioBoxes = {};
  function audioSection(name, processes) {
    if (audioBoxes[name]) { audioBoxes[name].alive = !!processes.node?.alive; return audioBoxes[name].details; }
    const details = el("details", "small"), body = el("div", "device-audio"), state = { details, alive: !!processes.node?.alive };
    audioBoxes[name] = state;
    const load = async () => {
      body.replaceChildren(el("p", "muted small", "Читаю пристрої…"));
      let data;
      try { data = await request(`/api/devices/${name}/audio`); }
      catch (error) { body.replaceChildren(el("p", "alert small", error.message)); return; }
      const { settings, devices: found, error } = data, selects = {};
      const save = el("button", "ghost small-button", "Зберегти"), result = el("p", "muted small", ""); save.type = "button"; save.disabled = true;
      const field = (key, title, list) => {
        const label = el("label", "", settings.env_override.includes(key) ? `${title} · задано в .env` : title), select = el("select"), chosen = settings.values[key] || "";
        const system = (list || []).find(x => x.default), options = [["", `Системний${system ? ` (${system.name})` : ""}`]];
        for (const x of list || []) options.push([x.name, x.name]);
        const match = (list || []).find(x => x.name === chosen) || (list || []).find(x => chosen && x.name.toLowerCase().includes(chosen.toLowerCase()));
        if (chosen && !match) options.push([chosen, `${chosen} — не знайдено`]);
        for (const [value, text] of options) { const o = el("option", "", text); o.value = value; select.append(o); }
        select.value = chosen ? (match ? match.name : chosen) : ""; select.dataset.saved = select.value;
        select.disabled = settings.env_override.includes(key);
        select.addEventListener("change", () => { save.disabled = !Object.values(selects).some(s => s.value !== s.dataset.saved); });
        selects[key] = select; label.append(select); return label;
      };
      const input = field("RADIO_INPUT_DEVICE", "Вхід ефіру", found?.inputs), output = field("RADIO_OUTPUT_DEVICE", "Вихід передачі", found?.outputs);
      save.addEventListener("click", () => action(async () => {
        const saved = await request(`/api/devices/${name}/audio`, "PUT", { expected_revision: settings.revision,
          values: Object.fromEntries(Object.entries(selects).map(([k, s]) => [k, s.value])) });
        settings.revision = saved.revision; save.disabled = true;
        for (const s of Object.values(selects)) s.dataset.saved = s.value;
        result.textContent = "Збережено. ";
        if (state.alive && (name === "pi" || serviceMode === "r345")) {
          const b = el("button", "ghost small-button", "Перезапустити ефір"); b.type = "button";
          b.addEventListener("click", () => action(async () => { await request(`/api/devices/${name}/node/restart`, "POST"); result.textContent = "Ефір перезапускається."; }));
          result.append(b);
        } else result.textContent += "Діятиме з наступного запуску ефіру.";
      }));
      const notes = [];
      if (error) notes.push(el("p", "alert small", error));
      else if (!(found?.inputs || []).length) notes.push(el("p", "alert small", "Звукових входів немає: ефір тут не запрацює."));
      body.replaceChildren(...notes, input, output, save, result);
    };
    details.addEventListener("toggle", () => { if (details.open) load(); });
    details.append(el("summary", "", "Звук"), body);
    return details;
  }
  const UPDATE_PHASE = { copying: "беру з GitHub", checking: "перевіряю", restarting: "перезапускаю служби" };
  let updateState = { phase: "idle" }, updateTimer = null;
  async function pollUpdate() {
    updateState = await request("/api/devices/pi/update");
    clearTimeout(updateTimer);
    if (updateState.active) updateTimer = setTimeout(() => devices().catch(() => {}), 2000);
  }
  function codeUpdate(moving, d) {
    const box = el("div", "device-update");
    if (updateState.active) { box.append(el("p", "small", `Оновлення коду: ${UPDATE_PHASE[updateState.phase] || "…"}`)); return box; }
    box.append(el("p", "alert small", `Код на Pi інший${d.pi.commit ? `: ${d.pi.commit.slice(0, 7)}` : ""}, на Mac ${(d.mac.commit || "—").slice(0, 7)}.`));
    const b = el("button", "ghost small-button", "Оновити код Pi"); b.type = "button"; b.disabled = moving;
    b.addEventListener("click", () => action(async () => {
      await request("/api/devices/pi/update", "POST"); await devices();
    }));
    box.append(b);
    return box;
  }
  let macUpdate = { phase: "idle" };
  function macCode() {
    const m = macUpdate, box = el("div", "device-update");
    if (m.active) { box.append(el("p", "small", `Оновлення коду: ${UPDATE_PHASE[m.phase] || "…"}`)); return [box]; }
    const parts = [];
    if (m.available) {
      box.append(el("p", "small", `На GitHub новіший код: ${m.available.slice(0, 7)}.`));
      const b = el("button", "ghost small-button", "Оновити код Mac"); b.type = "button";
      armed(b, "Перезапустити служби?", async () => { await request("/api/devices/mac/update", "POST"); await devices(); });
      box.append(b); parts.push(box);
    }
    if (m.phase === "failed") parts.push(el("p", "alert small", m.error));
    if (m.phase === "done" && m.supervisor) parts.push(el("p", "alert small", "Змінився сам r345: ./r345 stop, потім ./r345 up"));
    return parts;
  }
  let piBackups = null;
  function backupLine() {
    const b = piBackups;
    return el("p", "muted small", `Копії баз Pi на Mac: ${num(b.count)} з ${num(b.keep)} · ${storageSize(b.bytes)}${b.age_h == null ? "" : ` · остання ${num(Math.round(b.age_h))} год тому`}`);
  }
  function deviceCard(name, box, d, moving) {
    const device = d[name], home = d.home === name || d.home === "both";
    box.classList.toggle("home", home);
    const head = el("div", "device-head"); head.append(el("h3", "", deviceTitle(name)));
    const parts = [head];
    if (!device) { head.append(el("span", "pill off", "не налаштовано")); box.replaceChildren(head, el("p", "muted small", "Додайте RADIO345_PI_SSH у .env на Mac.")); return; }
    if (!device.reachable) head.append(el("span", "pill bad", "недоступна"));
    else if (home) { const missing = HOME_CORE.some(n => device.processes[n] && !device.processes[n].alive); head.append(el("span", missing ? "pill warn" : "pill ok", d.home === "both" ? "працює на двох!" : "працює тут")); }
    else head.append(el("span", "pill off", "запасний"));
    if (!device.reachable) parts.push(el("p", "muted small", device.error || ""));
    else {
      const on = Object.values(device.processes).filter(p => p.alive).length;
      parts.push(el("p", "device-line", `Служб працює: ${on} з ${Object.keys(device.processes).length}`),
        el("p", "muted small", `Подій ${device.events == null ? "—" : num(device.events)} · остання ${device.last_event ? ago(now() - device.last_event) : "—"}${device.queue ? ` · у черзі ${num(device.queue)}` : ""}`));
      if (name === "mac") parts.push(...macCode());
      if (name === "pi" && d.code_matches === false) parts.push(codeUpdate(moving, d));
      const list = el("div", "service-list");
      for (const [process, p] of Object.entries(device.processes)) list.append(serviceRow(name, process, p));
      const facts = [`вільно ${storageSize(device.disk_free)}`, device.temp_c == null ? "" : `${fix(device.temp_c, 1)} °C`, device.model ? `модель: ${device.model}` : ""].filter(Boolean).join(" · ");
      parts.push(list, el("p", "muted small", facts), audioSection(name, device.processes));
    }
    if (name === "pi" && piBackups?.count) parts.push(backupLine());
    if (name === "pi" && updateState.phase === "failed") parts.push(el("p", "alert small", updateState.error));
    if (name === "pi" && updateState.phase === "done" && updateState.units?.length)
      parts.push(el("p", "alert small", "Змінилися файли служб: на Pi один раз sudo sh deploy/hub/install.sh --units"));
    // «Перенести сюди» — лише на пристрої, який зараз не дім. Недоступна Pi не може прийняти дім, але Mac може забрати.
    if (d.configured && !home && d.home !== "both" && device.reachable && !moving && !updateState.active) {
      if (moveAsk?.target === name) parts.push(moveQuestion(name));
      else {
        const b = el("button", "", "Перенести сюди"); b.type = "button"; b.className = "device-move";
        b.addEventListener("click", () => action(() => moveHome(name)));
        parts.push(b);
      }
    }
    box.replaceChildren(...parts);
  }
  async function moveState() {
    const m = await request("/api/home-move"), line = $("move-state");
    $("move-rollback").hidden = !m.interrupted;
    const route = m.target ? `${deviceTitle(m.source)} → ${deviceTitle(m.target)}` : "";
    line.textContent = m.active ? `Перенесення ${route}: ${MOVE_PHASE[m.phase] || m.phase}…`
      : m.interrupted ? `Перенесення ${route} перервано на кроці «${MOVE_PHASE[m.phase] || m.phase}».`
      : m.phase === "rolled_back" ? `Перенесення ${route} не вдалося, усе повернуто. ${m.error}`
      : m.phase === "failed" ? `Перенесення ${route} не вдалося, перевірте пристрої. ${m.error}` : "";
    line.hidden = !line.textContent;
    line.classList.toggle("alert", m.interrupted || ["failed", "rolled_back"].includes(m.phase));
    clearTimeout(moveTimer);
    if (m.active) moveTimer = setTimeout(() => devices().catch(() => {}), 2000);
    return !!m.active || !!m.interrupted;
  }
  let moveAsk = null;
  function moveQuestion(name) {
    const box = el("div", "device-ask"), yes = el("button", "", "Перенести"), no = el("button", "ghost", "Скасувати"), bar = el("div", "toolbar");
    yes.type = no.type = "button";
    yes.addEventListener("click", () => action(async () => {
      moveAsk = null;
      await request("/api/home-move", "POST", { target: name, accept_warnings: true });
      await devices();
    }));
    no.addEventListener("click", () => { moveAsk = null; action(devices); });
    bar.append(yes, no);
    const list = el("ul", "small");
    for (const w of moveAsk.warnings) list.append(el("li", "", w));
    box.append(el("p", "", `Перенести все на ${deviceTitle(name)}? Бот, Mini App та ефір кілька хвилин не працюватимуть.`), ...(moveAsk.warnings.length ? [list] : []), bar);
    return box;
  }
  async function moveHome(target) {
    const check = await request(`/api/home-move/check?target=${target}`);
    if (check.problems.length) { note(check.problems.join(" ")); return; }
    moveAsk = { target, warnings: check.warnings };   // питання — у самій картці
    await devices();
  }
  async function devices() {
    let d;
    try { d = await request("/api/devices"); } catch (_) { return; }
    if (!d.mac) { $("device-mac").replaceChildren(el("p", "muted small", "Керування пристроями — у панелі на Mac.")); return; }
    const moving = await moveState().catch(() => false);
    macUpdate = await request("/api/devices/mac/update").catch(() => macUpdate);
    if (macUpdate.active) { clearTimeout(updateTimer); updateTimer = setTimeout(() => devices().catch(() => {}), 2000); }
    if (d.configured) { await pollUpdate().catch(() => {}); piBackups = await request("/api/pi-backups").catch(() => null); }
    deviceCard("mac", $("device-mac"), d, moving);
    deviceCard("pi", $("device-pi"), d, moving);
  }
  $("devices-refresh")?.addEventListener?.("click", () => action(devices));
  if ($("move-rollback") && typeof armed === "function") armed($("move-rollback"), "Повернути? Ще раз", async () => { await request("/api/home-move/rollback", "POST"); await devices(); });

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
          if(mode==='skip'&&!twice(b,'Пропустити? Ще раз')) return;
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


