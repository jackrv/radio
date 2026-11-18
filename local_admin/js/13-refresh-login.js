  // ---------- Автооновлення ----------
  let refreshPending = false;
  async function refreshArchiveNotice() {
    if (!archiveBaseline) return;
    const query = JSON.stringify(recordingFilterIds.map(id => $(id).value));
    const params = new URLSearchParams({view: $("recording-view").value, limit: "1"});
    const since = periodSince(); if (since) params.set("since", since);
    const until = periodUntil(); if (until) params.set("until", until);
    if (searchQuery()) params.set("q", searchQuery());
    if ($("recording-edits").value) params.set("edits", $("recording-edits").value);
    const page = await request(`/api/recordings?${params}`);
    if (view !== "recordings" || query !== JSON.stringify(recordingFilterIds.map(id => $(id).value))) return;
    const changed = archiveBaseline.query !== query || page.total !== archiveBaseline.total || page.items[0]?.id !== archiveBaseline.first;
    showArchiveFresh(changed);
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
  // Без входу — окремий екран із причиною й способами входу, а не порожній макет.
  function showAccessProblem(error, hadToken) {
    const denied = error?.status === 401 || error?.status === 403;
    $("access-reason").textContent = !denied
      ? `Панель не змогла отримати дані (${error?.message || "немає відповіді"}). Можливо, служби саме перезапускаються — спробуйте за хвилину.`
      : hadToken ? "Посилання з ключем не підійшло: ключ змінено (--rotate-key) або посилання застаріле. Візьміть нове — способи нижче."
      : "Цей браузер ще не входив у панель. Вхід зберігається в самому браузері, тож у новому браузері чи приватному вікні його немає.";
    document.querySelector(".app").hidden = true;
    $("access-screen").hidden = false;
  }
  $("access-retry").addEventListener("click", () => location.reload());
  const token = location.hash.slice(1);
  action(async () => {
    history.replaceState({ view: "overview" }, "", location.pathname + location.search);
    try {
      if (token) await request("/session", "POST", { token });
      await overview();
    } catch (error) { showAccessProblem(error, Boolean(token)); return; }
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
    // &filters=off — показати знайдене без виду, періоду й фільтра правок (вибір у браузері не змінюється).
    if (new URLSearchParams(location.search).get("filters") === "off") {
      $("recording-view").value = "all"; $("recording-period").value = ""; $("recording-edits").value = "";
    }
    if (wanted && titles[wanted] && wanted !== "overview") { busy = false; show(wanted, { history: "replace" }); }
  });
