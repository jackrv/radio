"use strict";

(() => {
  const { request, user } = window.Radio345;
  const $ = id => document.getElementById(id);
  let me = null;
  const canEdit = () => ["admin", "editor"].includes(me?.role);
  const isAdmin = () => me?.role === "admin";
  const roles = { viewer: "Спостерігач", editor: "Редактор", admin: "Адміністратор" };
  const states = { active: "Активне", pending: "Очікує підтверджень / перевірка конфліктів", disabled: "Вимкнено" };
  const loaded = new Set();
  const PAGE_SIZE = 10;
  const audioUrls = new Set();
  function releaseAudio() {
    for (const url of audioUrls) URL.revokeObjectURL(url);
    audioUrls.clear();
  }
  window.addEventListener("pagehide", releaseAudio);

  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined) item.textContent = text;
    if (className) item.className = className;
    return item;
  }
  function message(id, text, error = false) {
    $(id).textContent = text;
    $(id).classList.toggle("error-text", error);
  }
  function button(text, action) {
    const item = node("button", text, "outline-button");
    item.type = "button";
    item.addEventListener("click", action);
    return item;
  }
  function localDate(value) {
    return value ? new Date(value.replace(" ", "T") + "Z").toLocaleString("uk-UA") : "—";
  }

  async function overview() {
    try {
      const data = await request("/api/overview");
      $("events-count").textContent = data.events;
      $("rules-count").textContent = data.corrections;
      $("rules-detail").textContent = "Фрази, виправлені оператором";
      // Окремі показники: правки, кандидати, підтвердження, правила й автоматичні зміни Qwen.
      const l = data.learning || {};
      const auto = data.automatic || {};
      $("learning-detail").textContent =
        `Навчання: кандидатів ${l.candidates ?? 0}, підтверджень ${l.confirmations ?? 0}, ` +
        `активних правил ${l.active_rules ?? 0}` + (l.awaiting_approval ? `, очікують дозволу ${l.awaiting_approval}` : "") + ". " +
        (auto.available
          ? `${auto.fresh ? "Поточний запуск приймача" : "Останній запуск приймача (зараз дані не оновлюються)"}: ` +
            `Qwen змінив ${auto.qwen_changed} фраз, правила застосовано ${auto.rules_applied} раз.`
          : "Автоматичні зміни Qwen: немає даних приймача.");
      $("dictionary-count").textContent = data.dictionary_entries;
      message("overview-feedback", `Остання подія: ${localDate(data.last_event_at)} · Твій Telegram ID: ${me.user_id}`);
    } catch (error) { message("overview-feedback", error.message, true); }
  }

  function pagedList(prefix, path, render) {
    let offset = 0;
    let query = "";
    let busy = false;
    async function load() {
      if (busy) return;
      busy = true;
      $(prefix + "-prev").disabled = $(prefix + "-next").disabled = true;
      message(prefix === "rules" ? "dictionary-feedback" : "phrases-feedback", "Завантаження...");
      try {
        const data = await request(`${path}?q=${encodeURIComponent(query)}&offset=${offset}&limit=${PAGE_SIZE}`);
        if (prefix === "phrases") releaseAudio();
        $(prefix + "-list").replaceChildren();
        for (const item of data.items) $(prefix + "-list").append(render(item));
        if (!data.items.length) $(prefix + "-list").append(node("p", "Записів немає.", "subtle-note"));
        $(prefix + "-prev").disabled = offset === 0;
        $(prefix + "-next").disabled = !data.has_more;
        message(prefix === "rules" ? "dictionary-feedback" : "phrases-feedback", `Сторінка ${offset / PAGE_SIZE + 1}`);
      } catch (error) {
        $(prefix + "-list").replaceChildren();
        message(prefix === "rules" ? "dictionary-feedback" : "phrases-feedback", error.message, true);
      } finally { busy = false; }
    }
    $(prefix + "-search").addEventListener("submit", event => {
      event.preventDefault();
      if (busy) return;
      offset = 0; query = $(prefix + "-query").value.trim(); load();
    });
    $(prefix + "-prev").addEventListener("click", () => { if (!busy) { offset = Math.max(0, offset - PAGE_SIZE); load(); } });
    $(prefix + "-next").addEventListener("click", () => { if (!busy) { offset += PAGE_SIZE; load(); } });
    return load;
  }

  const phrases = pagedList("phrases", "/api/phrases", item => {
    const card = node("article", undefined, "data-card");
    card.append(node("div", `${localDate(item.created_at)} · ${item.fix_id.startsWith("mem:") ? "Тимчасова фраза" : item.fix_id}`, "subtle-note"));
    card.append(node("p", item.current_text, "phrase-text"));
    if (item.original_text !== item.current_text) card.append(node("p", `Початковий текст: ${item.original_text}`, "subtle-note phrase-text"));
    if (item.has_audio) {
      const listen = button("▶ Прослухати аудіо", async () => {
        listen.disabled = true;
        listen.textContent = "Завантажуємо аудіо...";
        try {
          const blob = await request(`/api/phrases/audio?fix_id=${encodeURIComponent(item.fix_id)}`, "GET", undefined, "blob");
          if (!card.isConnected) return;
          const player = node("audio");
          player.controls = true;
          const url = URL.createObjectURL(blob);
          audioUrls.add(url);
          player.src = url;
          player.addEventListener("error", () => {
            message("phrases-feedback", "Пристрій не відтворює OGG. Прослухай початкове голосове в Telegram.", true);
            URL.revokeObjectURL(url); audioUrls.delete(url);
          });
          card.append(player);
          listen.hidden = true;
        } catch (error) {
          message("phrases-feedback", error.message, true);
          listen.disabled = false; listen.textContent = "▶ Повторити завантаження аудіо";
        }
      });
      card.append(listen);
      if (isAdmin()) card.append(button("Зразок звуку сповіщення", async () => {
        if (!window.confirm("Використати аудіо як зразок сповіщення? Лише після прослуховування: запис НЕ повинен містити мовлення. Застосується після перезапуску.")) return;
        try {
          await request("/api/notification-sounds/from-phrase", "PUT", {fix_id:item.fix_id, confirmed_isolated:true});
          message("phrases-feedback", "Приватний зразок додано. Він застосовується після перезапуску приймача.");
        } catch(error) { message("phrases-feedback",error.message,true); }
      }));
    } else {
      card.append(node("p", "Аудіозапис не прив’язано до цієї фрази.", "subtle-note"));
    }
    if (canEdit()) card.append(button("Виправити", () => {
      const url = new URL(window.location.href);
      url.searchParams.set("fix_id", item.fix_id);
      window.location.assign(url.toString());
    }));
    return card;
  });

  const ruleKinds = {
    callsign: "Позивний: лише перед дозволеним номером",
    word: "Звичайне слово: застосовується після дозволу",
    blocked: "Не застосовується (суперечить словникам)",
  };
  const rules = pagedList("rules", "/api/word-rules", item => {
    const card = node("article", undefined, "data-card");
    card.append(node("p", `${item.original} → ${item.corrected}`, "phrase-text"));
    const approval = item.kind === "word" ? (item.approved ? " · Дозволено оператором" : " · Очікує дозволу") : "";
    card.append(node("p", `${states[item.status]} · Підтверджень: ${item.confirmations}${approval}`, "subtle-note"));
    card.append(node("p", `${ruleKinds[item.kind] || item.kind}${item.applies ? " · застосовується" : ""}`, "subtle-note"));
    if (canEdit()) {
      const toggle = button(item.status === "disabled" ? "Увімкнути з перевіркою" : "Вимкнути", async () => {
        toggle.disabled = true;
        try {
          await request(`/api/word-rules/${item.id}`, "PUT", { disabled: item.status !== "disabled", expected_version: item.version });
          await rules();
        } catch (error) { message("dictionary-feedback", error.message, true); }
        finally { toggle.disabled = false; }
      });
      card.append(toggle);
      if (item.kind === "word") {
        const approve = button(item.approved ? "Скасувати дозвіл" : "Дозволити застосування", async () => {
          if (!item.approved && !window.confirm(`Замінювати «${item.original}» на «${item.corrected}» у всіх нових фразах?`)) return;
          approve.disabled = true;
          try {
            await request(`/api/word-rules/${item.id}/approval`, "PUT", { approved: !item.approved, expected_version: item.version });
            await rules();
          } catch (error) { message("dictionary-feedback", error.message, true); }
          finally { approve.disabled = false; }
        });
        card.append(approve);
      }
    }
    return card;
  });

  let dictionary = null;
  let dictionaryBusy = false;
  let dirty = false;
  let selectedKind = "callsigns";
  function renderDictionary(focusIndex = null) {
    const list = $("dictionary-entries");
    list.replaceChildren();
    dictionary.entries.forEach((entry, index) => {
      const row = node("div", undefined, "dictionary-row");
      const enabled = node("input");
      enabled.type = "checkbox"; enabled.checked = entry.enabled; enabled.disabled = !canEdit() || dictionaryBusy;
      enabled.setAttribute("aria-label", `Рядок ${index + 1} увімкнений`);
      enabled.addEventListener("change", () => { entry.enabled = enabled.checked; dirty = true; });
      const text = node("input");
      text.value = entry.raw_line; text.maxLength = 500; text.disabled = !canEdit() || dictionaryBusy;
      text.setAttribute("aria-label", `Текст рядка ${index + 1}`);
      text.addEventListener("input", () => { entry.raw_line = text.value; dirty = true; });
      const remove = button("Видалити", () => { dictionary.entries.splice(index, 1); dirty = true; renderDictionary(); });
      remove.disabled = !canEdit() || dictionaryBusy;
      row.append(enabled, text, remove); list.append(row);
      if (index === focusIndex) {
        row.classList.add("dictionary-row-new");
        text.focus({ preventScroll: true });
        text.scrollIntoView({ block: "center", behavior: "instant" });
      }
    });
    if (!dictionary.entries.length) list.append(node("p", "Словник порожній. Додай перший рядок.", "subtle-note"));
  }
  async function loadDictionary() {
    if (dictionaryBusy) return;
    const kind = $("dictionary-kind").value;
    selectedKind = kind;
    const isRules = kind === "rules";
    $("rules-panel").hidden = !isRules;
    $("dictionary-entries").hidden = isRules;
    $("dictionary-save").hidden = $("dictionary-add").hidden = isRules;
    if (isRules) { dictionary = null; dirty = false; await rules(); return; }
    dictionaryBusy = true;
    $("dictionary-kind").disabled = true;
    $("dictionary-save").disabled = $("dictionary-add").disabled = true;
    dictionary = null;
    $("dictionary-entries").replaceChildren();
    message("dictionary-feedback", "Завантаження...");
    try {
      dictionary = await request(`/api/dictionaries/${kind}`);
      dirty = false;
      renderDictionary();
      message("dictionary-feedback", canEdit() ? "Зміни діють без перезапуску: приймач підхопить їх за кілька секунд." : "Режим перегляду. Для змін потрібна роль редактора.");
    } catch (error) { message("dictionary-feedback", error.message, true); }
    finally {
      dictionaryBusy = false;
      $("dictionary-kind").disabled = false;
      $("dictionary-save").disabled = $("dictionary-add").disabled = !canEdit() || !dictionary;
      if (dictionary) renderDictionary();
    }
  }
  $("dictionary-kind").addEventListener("change", () => {
    if (dirty && !window.confirm("Не зберігати зміни словника?")) { $("dictionary-kind").value = selectedKind; return; }
    loadDictionary();
  });
  $("dictionary-refresh").addEventListener("click", () => {
    if (!dirty || window.confirm("Оновити словник і втратити незбережені зміни?")) loadDictionary();
  });
  $("dictionary-add").addEventListener("click", () => {
    if (!dictionary || dictionaryBusy || !canEdit()) return;
    dictionary.entries.push({ raw_line: "", enabled: true });
    dirty = true;
    renderDictionary(dictionary.entries.length - 1);
    message("dictionary-feedback", "Новий рядок додано. Введи значення та збережи словник.");
  });
  $("dictionary-save").addEventListener("click", async () => {
    if (!dictionary || dictionaryBusy || !canEdit()) return;
    dictionaryBusy = true;
    $("dictionary-save").disabled = $("dictionary-add").disabled = $("dictionary-kind").disabled = true;
    renderDictionary();
    try {
      dictionary = await request(`/api/dictionaries/${dictionary.kind}`, "PUT", { entries: dictionary.entries, expected_version: dictionary.version });
      dirty = false;
      message("dictionary-feedback", "Збережено. Приймач застосує словник за кілька секунд, без перезапуску.");
    } catch (error) { message("dictionary-feedback", error.message, true); }
    finally {
      dictionaryBusy = false;
      $("dictionary-save").disabled = $("dictionary-add").disabled = $("dictionary-kind").disabled = false;
      renderDictionary();
    }
  });

  let settingsRevision = null;
  let settingsBusy = false;
  async function settings() {
    if (settingsBusy) return;
    if (!isAdmin()) { message("recognition-feedback", "Налаштування доступні лише адміністратору."); return; }
    settingsBusy = true;
    $("recognition-fields").disabled = true;
    try {
      const data = await request("/api/recognition/settings");
      settingsRevision = data.revision;
      $("setting-llm").checked = data.values.USE_LLM;
      $("setting-vad").value = data.values.VAD_LEVEL;
      $("setting-silence").value = data.values.SILENCE_END_MS;
      $("setting-speech").value = data.values.MIN_SPEECH_MS;
      $("setting-segment").value = data.values.MAX_SEGMENT_S;
      $("recognition-models").textContent = `Whisper: ${data.whisper_model} · Qwen: ${data.correction_model} · Мова: ${data.language}`;
      $("recognition-fields").disabled = false;
      message("recognition-feedback", "Збережені значення для наступного запуску приймача.");
    } catch (error) { message("recognition-feedback", error.message, true); }
    finally { settingsBusy = false; }
  }
  $("recognition-form").addEventListener("submit", async event => {
    event.preventDefault();
    if (!isAdmin() || settingsRevision === null || settingsBusy || $("recognition-fields").disabled) return;
    settingsBusy = true;
    $("recognition-fields").disabled = true;
    try {
      const data = await request("/api/recognition/settings", "PUT", {
        expected_revision: settingsRevision,
        values: { USE_LLM: $("setting-llm").checked, VAD_LEVEL: Number($("setting-vad").value),
          SILENCE_END_MS: Number($("setting-silence").value), MIN_SPEECH_MS: Number($("setting-speech").value),
          MAX_SEGMENT_S: Number($("setting-segment").value) }
      });
      settingsRevision = data.revision;
      message("recognition-feedback", "Збережено. Перезапусти приймач, щоб застосувати налаштування.");
    } catch (error) { message("recognition-feedback", error.message + " Завантаж збережені налаштування перед новою зміною.", true); }
    finally { settingsBusy = false; $("recognition-fields").disabled = false; }
  });
  $("recognition-refresh").addEventListener("click", () => {
    if (!settingsBusy && window.confirm("Завантажити збережені налаштування та замінити значення у формі?")) settings();
  });
  $("overview-refresh").addEventListener("click", () => { if (me) overview(); });

  let usersBusy = false;
  async function users() {
    if (!isAdmin()) { message("users-feedback", "Керування користувачами доступне лише адміністратору."); return; }
    try {
      const data = await request("/api/users");
      $("users-list").replaceChildren();
      for (const item of data.items) {
        const card = node("article", undefined, "data-card");
        card.append(node("h2", item.name || `Telegram ${item.user_id}`));
        const row = node("div", undefined, "toolbar");
        const role = node("select");
        role.setAttribute("aria-label", `Роль ${item.user_id}`);
        for (const [key, name] of Object.entries(roles)) { const option = node("option", name); option.value = key; role.append(option); }
        role.value = item.role;
        const mode = node("select");
        mode.setAttribute("aria-label", `Сповіщення ${item.user_id}`);
        for (const [key, title] of Object.entries({commands:"Команди", watch:"Спостереження", off:"Тиша", all:"Усі (адміністратор)"})) {
          const option = node("option", title); option.value = key; mode.append(option);
        }
        mode.value = item.notify_mode;
        function restrictMode() {
          for (const option of mode.options) option.disabled = option.value === "all" && role.value !== "admin";
          if (mode.value === "all" && role.value !== "admin") mode.value = "commands";
        }
        role.addEventListener("change", restrictMode); restrictMode();
        const label = node("label", undefined, "checkbox-label");
        const enabled = node("input"); enabled.type = "checkbox"; enabled.checked = item.enabled;
        label.append(enabled, node("span", "Доступ увімкнено"));
        const save = button("Зберегти", async () => {
          if (usersBusy || !window.confirm(`Змінити доступ користувача ${item.user_id}?`)) return;
          await saveUser(item.user_id, { role: role.value, enabled: enabled.checked, expected_version: item.version, display_name: nameInput.value, notify_mode: mode.value });
        });
        row.append(role, mode, label, save); card.append(row); $("users-list").append(card);
        card.append(node("p", [item.username ? `@${item.username}` : "", item.telegram_name || "", `ID ${item.user_id}`].filter(Boolean).join(" · "), "subtle-note"));
        const nameInput = node("input");
        nameInput.value = item.display_name || "";
        nameInput.maxLength = 80;
        nameInput.placeholder = "Власна назва (порожньо — ім’я Telegram)";
        nameInput.setAttribute("aria-label", `Назва користувача ${item.user_id}`);
        nameInput.className = "user-name-input";
        card.append(nameInput);
      }
      $("user-add-fields").disabled = false;
      message("users-feedback", "Останнього активного адміністратора вимкнути не можна.");
    } catch (error) { message("users-feedback", error.message, true); }
  }
  async function saveUser(id, payload) {
    if (!isAdmin() || usersBusy) return;
    usersBusy = true;
    $("user-add-fields").disabled = true;
    try {
      await request(`/api/users/${id}`, "PUT", payload);
      $("new-user-id").value = "";
      $("new-user-name").value = "";
      if (String(me.user_id) === id && (payload.role !== "admin" || !payload.enabled)) {
        $("users-list").replaceChildren();
        message("users-feedback", "Твій доступ змінено. Закрий і знову відкрий панель.");
        me = null;
        return;
      }
      await users();
      message("users-feedback", "Користувача збережено.");
    } catch (error) { message("users-feedback", error.message, true); }
    finally { usersBusy = false; $("user-add-fields").disabled = !isAdmin(); }
  }
  $("new-user-role").addEventListener("change", () => {
    const mode = $("new-user-mode");
    for (const option of mode.options) option.disabled = option.value === "all" && $("new-user-role").value !== "admin";
    if (mode.value === "all" && $("new-user-role").value !== "admin") mode.value = "commands";
  });
  $("user-add-form").addEventListener("submit", event => {
    event.preventDefault();
    const id = $("new-user-id").value.trim();
    if (!/^[1-9][0-9]{0,18}$/.test(id)) return;
    if (!window.confirm(`Надати доступ Telegram ID ${id}?`)) return;
    saveUser(id, { role: $("new-user-role").value, enabled: true, expected_version: null, display_name: $("new-user-name").value, notify_mode: $("new-user-mode").value });
  });
  $("users-refresh").addEventListener("click", () => { if (!usersBusy) users(); });

  const loaders = { home: overview, corrections: phrases, dictionaries: loadDictionary, settings, users };
  async function loadView(name) {
    if (me?.role === "viewer") return;
    if (!me || !loaders[name] || (loaded.has(name) && name !== "corrections")) return;
    loaded.add(name);
    await loaders[name]();
  }
  document.addEventListener("radio345:view", event => loadView(event.detail));
  window.addEventListener("beforeunload", event => {
    if (dirty) { event.preventDefault(); event.returnValue = ""; }
  });
  async function start() {
    try {
      me = await user();
      if (me.role === "viewer") { showView("home"); return; }
      if (!isAdmin() && document.querySelector("#view-users.active")) showView("home");
      await overview();
      const current = document.querySelector(".view.active");
      if (current) await loadView(current.id.replace("view-", ""));
    } catch (error) {
      for (const id of ["overview-feedback", "phrases-feedback", "dictionary-feedback", "recognition-feedback", "users-feedback"]) message(id, error.message, true);
    }
  }
  start();
})();
