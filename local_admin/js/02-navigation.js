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
    if (name === "recordings" && typeof fitArchive === "function") requestAnimationFrame(() => fitArchive());
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

  // Логотип і назва — «додому»: Огляд і на початок сторінки.
  function goHome() { show("overview"); window.scrollTo?.({ top: 0, behavior: "smooth" }); }
  $("brand")?.addEventListener("click", goHome);
  $("brand")?.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); goHome(); } });
