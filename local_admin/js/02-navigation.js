  // ---------- Навігація: розділи й вкладки ----------
  const titles = { recordings: "Аудіоархів", overview: "Огляд", filter: "Сигнали сповіщень", dictionaries: "Словники", codes: "Коди доби", texts: "Фільтри тексту", learning: "Навчання", settings: "Налаштування", users: "Користувачі", activity: "Активність", preview: "Mini App", quality: "Якість по днях", experiments: "Експерименти", system: "Стан", sessions: "Діагностика", storage: "Дані й копії", checks: "Перевірки" };
  const GROUPS = {
    mentions: ["Вибірки", ["mentions"]],
    overview: ["Огляд", ["overview"]],
    recordings: ["Аудіоархів", ["recordings"]],
    recognition: ["Розпізнавання", ["dictionaries", "codes", "texts", "learning", "settings"]],
    signals: ["Сигнали сповіщень", ["filter"]],
    people: ["Люди", ["users", "activity", "preview"]],
    analysis: ["Аналіз", ["quality", "experiments"]],
    system: ["Система", ["system", "sessions", "storage", "checks"]],
  };
  titles.mentions = 'Вибірки';
  // Дані, які змінюються поза цією вкладкою, перечитуються при кожному відкритті.
  const ALWAYS_RELOAD = new Set(["codes", "learning", "users", "activity", "mentions"]);
  const lastTab = {};
  const pageScroller = () => document.querySelector?.('main') || window;
  const pageScrollY = () => pageScroller()===window ? window.scrollY : pageScroller().scrollTop;
  const pageScrollTo = options => pageScroller().scrollTo(options);
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
  function show(name, { history: mode = "push", menu = false } = {}) {
    // Розділ і вкладка можуть зватися однаково («system»): з меню — остання вкладка розділу, інакше — сама вкладка.
    if (GROUPS[name] && (menu || !titles[name])) name = lastTab[name] || GROUPS[name][1][0];
    const group = groupOf(name);
    if (!group) return;
    const changed = name !== view;
    if (changed) {
      rememberField(document.activeElement);
      const state = stateByView[view] ||= {};
      state.y = pageScrollY();
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
          ? pageScrollY() + field.getBoundingClientRect().top - state.fieldTop : state.y || 0;
        pageScrollTo({ top, behavior: "instant" });
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
    if (['texts','dictionaries'].includes(name) && typeof fitRecognition==='function') requestAnimationFrame(()=>fitRecognition());
    if (name === "recordings" && typeof fitArchive === "function") requestAnimationFrame(() => fitArchive());
    if (name === "overview" && typeof fitOverview === "function") requestAnimationFrame(() => fitOverview());
    if (name === "quality" && typeof fitAnalysis === "function") requestAnimationFrame(() => fitAnalysis());
    if (name === "experiments" && typeof fitLab === "function") requestAnimationFrame(() => fitLab());
    const url = name === "overview" ? location.pathname : `${location.pathname}?view=${encodeURIComponent(name)}`;
    if (mode === "push" && changed) history.pushState({ view: name }, "", url);
    else if (mode === "replace") history.replaceState({ view: name }, "", location.pathname + location.search);
    for (const b of document.querySelectorAll("#nav button")) {
      b.classList.toggle("active", b.dataset.group === group);
      if (b.dataset.group === group) b.setAttribute('aria-current', 'page');
      else b.removeAttribute?.('aria-current');
    }
    for (const s of document.querySelectorAll("[data-panel]")) s.hidden = s.dataset.panel !== name;
    $("view-title").textContent = GROUPS[group][0];
    const tabs = $("tabs"), list = GROUPS[group][1];
    tabs.replaceChildren();
    tabs.hidden = list.length < 2;
    for (const tab of list) {
      const b = el("button", tab === name ? "tab active" : "tab", titles[tab]);
      b.type = "button"; b.setAttribute("aria-current", tab === name ? "page" : "false");
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
  for (const b of document.querySelectorAll("#nav button")) b.addEventListener("click", () => show(b.dataset.group, { menu: true }));
  // «Назад» / «Вперед» у браузері перемикають розділи панелі, а не залишають її.
  window.addEventListener("popstate", event => {
    const wanted = event.state?.view || new URLSearchParams(location.search).get("view") || "overview";
    show(titles[wanted] ? wanted : "overview", { history: "none" });
  });

  // Логотип: на широкому екрані згортає меню (14-mentions.js), на вузькому — «додому».
  const wideSidebar = () => matchMedia("(min-width: 901px)").matches;
  function goHome() { if (wideSidebar()) return; show("overview"); pageScrollTo({ top: 0, behavior: "smooth" }); }
  $("brand")?.addEventListener("click", goHome);
  $("brand")?.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); goHome(); } });
