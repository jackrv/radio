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


