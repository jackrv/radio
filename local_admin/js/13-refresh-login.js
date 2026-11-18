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
      // Коротко в шапці; пояснення — у підказці при наведенні.
      $("updated").textContent = "⚠ не оновлено · повтор…";
      $("updated").title = "Не вдалося оновити дані (немає зв’язку з панеллю або служби перезапускаються). Повторюємо автоматично кожні 10 с.";
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
